"""
vara/broker.py

TNCBroker — owns the single VARAFMInterface for one VARA FM, VARA HF or
Aether HF instance and arbitrates session access for the BBS dispatcher.
"""

import time
import logging
import threading
from typing import Optional, Protocol

from core.config import InstanceConfig, AppConfig
from core.events import EventBus, EventType
from vara.interface import (VARAFMInterfaceWithCall, VARAFMState,
                            init_bandwidth, modem_label)
from vara.ptt import PTTController, create_ptt_driver
from vara.rigcontrol import RigController

log = logging.getLogger(__name__)


# ── SessionHandler protocol (unchanged from mux.py) ──────────────────────────

class SessionHandler(Protocol):
    """Interface that session handlers must implement."""
    def on_cmd_event(self, event: str) -> None: ...
    def on_data(self, data: bytes) -> None: ...


# ── TNCBroker ─────────────────────────────────────────────────────────────────

class TNCBroker:
    """
    Owns the VARAFMInterface for one VARA FM instance and
    arbitrates session access via acquire_session / release_session.
    """

    def __init__(
        self,
        inst_cfg:   InstanceConfig,
        app_cfg:    AppConfig,
        event_bus:  EventBus,
    ):
        self.cfg       = inst_cfg
        self._app_cfg  = app_cfg
        self._bus      = event_bus

        self._interface = VARAFMInterfaceWithCall(
            inst_cfg, event_bus, app_cfg.station.bbs_callsign
        )

        self._session_lock   = threading.Lock()
        self._active_handler: Optional[SessionHandler] = None
        self._outbound       = False   # an outbound call owns the session
        self._handler_lock   = threading.Lock()
        self._relay_stop     = threading.Event()
        self._retired        = False

        # Subscribe to events so we can forward to the active handler
        self._bus.subscribe(EventType.SESSION_CONNECTED,    self._on_session_connected)
        self._bus.subscribe(EventType.SESSION_DISCONNECTED, self._on_session_disconnected)
        self._bus.subscribe(EventType.VARA_CONNECTED,       self._on_vara_connected)
        self._bus.subscribe(EventType.VARA_DISCONNECTED,    self._on_vara_disconnected)

        self._data_relay_thread: Optional[threading.Thread] = None

        # ── Shared OmniRig client (single COM thread) ─────────────
        # If both PTT and rig control use OmniRig, they MUST share a
        # single OmniRigClient with its dedicated COM thread.  Multiple
        # Dispatch() calls on different threads cause COM apartment
        # conflicts where OmniRig returns <unknown> proxy errors.
        self._omnirig_client = None
        if inst_cfg.is_hf:
            ptt_uses_omnirig = inst_cfg.ptt.method.upper() == "OMNIRIG"
            scan_uses_omnirig = (inst_cfg.rig_control.method.upper() == "OMNIRIG"
                                 and inst_cfg.rig_control.channels)
            if ptt_uses_omnirig or scan_uses_omnirig:
                from vara.omnirig import OmniRigClient
                rig_num = (inst_cfg.ptt.omnirig_rig if ptt_uses_omnirig
                           else inst_cfg.rig_control.omnirig_rig)
                try:
                    self._omnirig_client = OmniRigClient(rig_number=rig_num)
                    self._omnirig_client.start()
                    parts = []
                    if ptt_uses_omnirig:
                        parts.append("PTT")
                    if scan_uses_omnirig:
                        parts.append("scan")
                    log.info(
                        f"[{inst_cfg.name}] OmniRig client started on "
                        f"Rig{rig_num} (shared by {' + '.join(parts)})"
                    )
                except Exception as e:
                    log.error(
                        f"[{inst_cfg.name}] OmniRig client failed: {e}. "
                        f"PTT and scanning will not be available."
                    )
                    self._omnirig_client = None

        # ── PTT control for VARA HF ──────────────────────────────────
        # VARA FM handles PTT internally; VARA HF requires the app to
        # physically key the radio when PTT ON/OFF events arrive.  Aether HF
        # keys the radio itself unless its keying is "host program", in
        # which case it too expects the BBS to key on PTT ON.
        self._ptt: Optional[PTTController] = None
        if inst_cfg.is_hf and inst_cfg.ptt.method.upper() != "VOX":
            ptt_cfg = inst_cfg.ptt
            try:
                driver = create_ptt_driver(
                    method=ptt_cfg.method,
                    port=ptt_cfg.port,
                    hamlib_host=ptt_cfg.hamlib_host,
                    hamlib_port=ptt_cfg.hamlib_port,
                    cm108_device=ptt_cfg.cm108_device,
                    omnirig_rig=ptt_cfg.omnirig_rig,
                    omnirig_client=self._omnirig_client,
                    flrig_host=getattr(ptt_cfg, 'flrig_host', '127.0.0.1'),
                    flrig_port=getattr(ptt_cfg, 'flrig_port', 12345),
                )
                self._ptt = PTTController(
                    instance_name=inst_cfg.name,
                    driver=driver,
                    event_bus=event_bus,
                    watchdog_timeout=ptt_cfg.watchdog_timeout,
                )
                self._ptt.start()
            except Exception as e:
                log.error(
                    f"[{inst_cfg.name}] PTT setup failed: {e}. "
                    f"Radio will not transmit on inbound connections! "
                    f"Configure PTT in Settings or aether_bbs.toml."
                )
        elif inst_cfg.is_aether:
            log.info(
                f"[{inst_cfg.name}] Aether HF keys the radio itself "
                f"(BBS PTT method VOX). If Aether's keying is set to "
                f"'host program', choose a PTT method here instead."
            )
        elif inst_cfg.is_hf:
            log.warning(
                f"[{inst_cfg.name}] VARA HF PTT method is VOX. "
                f"If your radio does not support VOX, configure PTT "
                f"in Settings or aether_bbs.toml."
            )

        # ── Rig control (frequency scanning) for VARA HF ─────────
        # Created here but NOT started -- scanning begins when VARA
        # connects (modem ready, radio path live).
        self._rig_control: Optional[RigController] = None
        if inst_cfg.is_hf:
            rc_cfg = inst_cfg.rig_control
            log.info(
                f"[{inst_cfg.name}] Rig control config: method={rc_cfg.method}, "
                f"rig={rc_cfg.omnirig_rig}, mode={rc_cfg.mode}, "
                f"channels={len(rc_cfg.channels)}"
            )
            if rc_cfg.method.upper() == "OMNIRIG" and rc_cfg.channels:
                try:
                    self._rig_control = RigController(
                        instance_name=inst_cfg.name,
                        rig_cfg=rc_cfg,
                        event_bus=event_bus,
                        omnirig_client=self._omnirig_client,
                    )
                    freqs = ', '.join(f'{ch.freq_khz:.1f}' for ch in rc_cfg.channels)
                    log.info(
                        f"[{inst_cfg.name}] Rig controller ready "
                        f"({len(rc_cfg.channels)} channels: {freqs}) -- "
                        f"scanning will start when VARA connects"
                    )
                except Exception as e:
                    log.error(
                        f"[{inst_cfg.name}] Rig control setup failed: {e}. "
                        f"Frequency scanning will not be available."
                    )

    # ── Connection lifecycle ───────────────────────────────────────────────

    def connect(self):
        """Connect to VARA FM TCP. Blocking with retry."""
        self._interface.connect()
        self._start_data_relay()

    def disconnect(self):
        """Disconnect and retire this broker — stops reconnect loops and unsubscribes."""
        self._retired = True
        self._relay_stop.set()
        self._bus.unsubscribe(EventType.SESSION_CONNECTED,    self._on_session_connected)
        self._bus.unsubscribe(EventType.SESSION_DISCONNECTED, self._on_session_disconnected)
        self._bus.unsubscribe(EventType.VARA_CONNECTED,       self._on_vara_connected)
        self._bus.unsubscribe(EventType.VARA_DISCONNECTED,    self._on_vara_disconnected)
        if self._ptt:
            self._ptt.stop()
            self._ptt = None
        if self._rig_control:
            self._rig_control.stop()
            self._rig_control = None
        if self._omnirig_client:
            self._omnirig_client.stop()
            self._omnirig_client = None
        self._interface.disconnect()

    def resume(self):
        """Re-activate a previously disconnected/retired broker for reuse."""
        self._retired = False
        self._bus.unsubscribe(EventType.SESSION_CONNECTED,    self._on_session_connected)
        self._bus.unsubscribe(EventType.SESSION_DISCONNECTED, self._on_session_disconnected)
        self._bus.unsubscribe(EventType.VARA_CONNECTED,       self._on_vara_connected)
        self._bus.unsubscribe(EventType.VARA_DISCONNECTED,    self._on_vara_disconnected)
        self._bus.subscribe(EventType.SESSION_CONNECTED,    self._on_session_connected)
        self._bus.subscribe(EventType.SESSION_DISCONNECTED, self._on_session_disconnected)
        self._bus.subscribe(EventType.VARA_CONNECTED,       self._on_vara_connected)
        self._bus.subscribe(EventType.VARA_DISCONNECTED,    self._on_vara_disconnected)

    # ── Session arbitration (same API as old VARAFMMux) ────────────────────

    def acquire_session(
        self,
        handler: SessionHandler,
        timeout: float = 5.0,
    ) -> bool:
        """
        Acquire exclusive session ownership for a handler.
        Returns True if acquired, False if timed out.
        """
        handler_name = type(handler).__name__
        log.info(f"[{self.cfg.name}] Session lock requested by {handler_name} "
                 f"(currently locked={self._session_lock.locked()})")
        if not self._session_lock.acquire(timeout=timeout):
            current = type(self._active_handler).__name__ if self._active_handler else "None"
            log.error(f"[{self.cfg.name}] SESSION LOCK TIMEOUT for {handler_name} "
                      f"— lock held by {current}")
            return False

        with self._handler_lock:
            self._active_handler = handler

        log.info(f"[{self.cfg.name}] Session acquired by {handler_name}")
        self._reinit_vara()
        return True

    def release_session(self, handler: SessionHandler):
        """Release session ownership."""
        handler_name = type(handler).__name__
        with self._handler_lock:
            if self._active_handler is not handler:
                log.warning(f"[{self.cfg.name}] release_session called by non-owner "
                            f"{handler_name}")
                return
            self._active_handler = None

        try:
            self._session_lock.release()
        except RuntimeError:
            pass  # already released

        log.info(f"[{self.cfg.name}] Session released by {handler_name}")
        self._interface.send_cmd("LISTEN ON")

    # ── Outbound calls ─────────────────────────────────────────────────────

    def begin_outbound(self, handler: SessionHandler, timeout: float = 15.0) -> bool:
        """Take the session for a call this station places (e.g. RF
        forwarding).  While it is held, CONNECTED/DISCONNECTED go to
        ``handler`` and the dispatcher does not treat the session as an
        inbound caller."""
        if not self.acquire_session(handler, timeout=timeout):
            return False
        self._outbound = True
        return True

    def end_outbound(self, handler: SessionHandler):
        self._outbound = False
        self.release_session(handler)

    @property
    def outbound_active(self) -> bool:
        return self._outbound

    # ── Command / data passthrough ─────────────────────────────────────────

    def send_cmd(self, cmd: str):
        self._interface.send_cmd(cmd)

    def send_data(self, data: bytes):
        self._interface.send_data(data)

    def recv_data(self, timeout: float = 1.0):
        return self._interface.recv_data(timeout=timeout)

    def connect_station(
        self,
        remote_call: str,
        path: str = "",
        busy_timeout: float = 30.0,
    ) -> bool:
        return self._interface.connect_station(remote_call, path, busy_timeout)

    # ── Status ─────────────────────────────────────────────────────────────

    @property
    def state(self) -> VARAFMState:
        return self._interface.state

    @property
    def busy(self) -> bool:
        return self._interface.busy

    @property
    def ptt(self) -> bool:
        return self._interface.ptt

    @property
    def remote_call(self):
        return self._interface.remote_call

    # ── Internal ──────────────────────────────────────────────────────────

    def _reinit_vara(self):
        """Re-send initialisation commands when a new handler takes over.

        Neither MYCALL nor LISTEN ON should be sent while an RF session is
        already active — VARA FM rejects both with WRONG mid-connection.
        This guard also avoids the spurious WRONG log entries that made
        the command log harder to read.

        Uses the same command sequence as _init_vara() in the interface
        (MYCALL → COMPRESSION OFF → BW → LISTEN ON) with ≥ 1 s pauses
        between commands, matching the timing used by Pat, BPQ32, and JNOS.
        """
        if self._interface.state == VARAFMState.CONNECTED:
            return
        self._interface._in_init = True
        self._interface._init_wrong_count = 0
        time.sleep(1.0)
        self._interface.send_cmd(f"MYCALL {self._app_cfg.station.bbs_callsign}")
        time.sleep(1.0)
        # Ensure raw data mode (no VARA-level Huffman compression)
        self._interface.send_cmd("COMPRESSION OFF")
        time.sleep(1.0)
        # Restore HF-specific settings that may not persist between sessions
        bw = init_bandwidth(self.cfg)
        if bw:
            self._interface.send_cmd(f"BW{bw}")
            time.sleep(1.0)
        self._interface.send_cmd("LISTEN ON")
        time.sleep(1.0)
        self._interface._in_init = False
        if self._interface._init_wrong_count > 0:
            log.error(f"[{self.cfg.name}] reinit FAILED: "
                      f"{self._interface._init_wrong_count} command(s) rejected")

    def _start_data_relay(self):
        """Relay incoming data bytes to the active handler."""
        self._relay_stop.clear()
        self._data_relay_thread = threading.Thread(
            target=self._data_relay,
            name=f"DataRelay-{self.cfg.name}",
            daemon=True,
        )
        self._data_relay_thread.start()

    def _data_relay(self):
        while not self._relay_stop.is_set():
            data = self._interface.recv_data(timeout=0.5)
            if data:
                with self._handler_lock:
                    if self._active_handler:
                        try:
                            self._active_handler.on_data(data)
                        except Exception as e:
                            log.error(f"[{self.cfg.name}] handler on_data error: {e}")

    def _on_session_connected(self, event):
        if event.source != self.cfg.name:
            return
        # Pause frequency scanning — hold current frequency for the session
        if self._rig_control:
            self._rig_control.pause()
        with self._handler_lock:
            if self._active_handler:
                try:
                    self._active_handler.on_cmd_event(
                        f"CONNECTED {self._app_cfg.station.bbs_callsign} "
                        f"{event.data.get('remote_call', '')}"
                    )
                except Exception as e:
                    log.error(f"[{self.cfg.name}] handler on_cmd_event error: {e}")

    def _on_session_disconnected(self, event):
        if event.source != self.cfg.name:
            return
        # Resume frequency scanning
        if self._rig_control:
            self._rig_control.resume()
        with self._handler_lock:
            if self._active_handler:
                try:
                    self._active_handler.on_cmd_event("DISCONNECTED")
                except Exception as e:
                    log.error(f"[{self.cfg.name}] handler DISCONNECTED event error: {e}")

    def _on_vara_connected(self, event):
        if event.source != self.cfg.name:
            return
        # Start frequency scanning now that the modem is connected
        # and the radio path is live.
        if self._rig_control:
            log.info(
                f"[{self.cfg.name}] VARA connected — starting rig controller"
            )
            try:
                self._rig_control.start()
            except Exception as e:
                log.error(
                    f"[{self.cfg.name}] Rig controller start failed: {e}"
                )
        else:
            log.debug(
                f"[{self.cfg.name}] VARA connected — no rig controller configured"
            )

    def _on_vara_disconnected(self, event):
        if event.source != self.cfg.name:
            return
        # Stop scanning — modem is gone, no point tuning the radio
        if self._rig_control:
            self._rig_control.stop()
        if self._retired:
            return
        log.warning(f"[{self.cfg.name}] {modem_label(self.cfg)} TCP dropped; "
                    f"attempting reconnect in 5s")
        threading.Thread(
            target=self._reconnect_loop,
            name=f"Reconnect-{self.cfg.name}",
            daemon=True,
        ).start()

    def _reconnect_loop(self):
        time.sleep(5)
        if self._retired:
            return
        log.info(f"[{self.cfg.name}] Attempting {modem_label(self.cfg)} reconnect")
        self._interface.connect()
        if not self._retired and self._interface.state != VARAFMState.DISCONNECTED:
            self._start_data_relay()

