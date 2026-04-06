"""
vara/broker.py

TNCBroker — owns the single VARAFMInterface for one VARA FM instance and
arbitrates access between multiple clients:

  1. The BBS dispatcher (internal, uses acquire_session / release_session
     exactly as it did with the old VARAFMMux).
  2. External applications (Winlink Express, packet terminal, …) that
     connect to a local VARA-protocol proxy port pair over TCP.

When broker.enabled is False the proxy server is never started and this
class is functionally identical to the old VARAFMMux — zero behaviour
change for existing code.

Public API is a strict superset of VARAFMMux so it can be used as a
drop-in replacement.
"""

import time
import socket
import logging
import threading
import socketserver
from typing import Optional, Protocol

from core.config import InstanceConfig, AppConfig
from core.events import EventBus, EventType
from vara.interface import VARAFMInterfaceWithCall, VARAFMState
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
    Owns the VARAFMInterface for one VARA FM instance.

    Drop-in replacement for the old VARAFMMux.  Adds an optional TCP proxy
    server so external apps can share access when broker.enabled = True.

    Internal BBS sessions use acquire_session / release_session as before.
    External sessions connect to localhost:broker_cmd_port and talk the
    native VARA FM protocol; the broker proxies their traffic and enforces
    the same single-session lock.
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
        self._handler_lock   = threading.Lock()
        self._relay_stop     = threading.Event()
        self._retired        = False

        # Subscribe to events so we can forward to the active handler
        self._bus.subscribe(EventType.SESSION_CONNECTED,    self._on_session_connected)
        self._bus.subscribe(EventType.SESSION_DISCONNECTED, self._on_session_disconnected)
        self._bus.subscribe(EventType.VARA_CONNECTED,       self._on_vara_connected)
        self._bus.subscribe(EventType.VARA_DISCONNECTED,    self._on_vara_disconnected)

        self._data_relay_thread: Optional[threading.Thread] = None

        # Optional proxy server for external apps
        self._proxy: Optional[_ProxyServer] = None
        if app_cfg.broker.enabled:
            try:
                self._proxy = _ProxyServer(
                    broker   = self,
                    cmd_port = app_cfg.broker.cmd_port,
                    data_port= app_cfg.broker.data_port,
                    name     = inst_cfg.name,
                )
                self._proxy.start()
                log.info(
                    f"[{inst_cfg.name}] TNC proxy listening on "
                    f"cmd:{app_cfg.broker.cmd_port} "
                    f"data:{app_cfg.broker.data_port}"
                )
            except OSError as e:
                log.error(f"[{inst_cfg.name}] Could not start TNC proxy: {e}")

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
        # physically key the radio when PTT ON/OFF events arrive.
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
                    f"Configure PTT in Settings or vara_fm_bbs.toml."
                )
        elif inst_cfg.is_hf:
            log.warning(
                f"[{inst_cfg.name}] VARA HF PTT method is VOX. "
                f"If your radio does not support VOX, configure PTT "
                f"in Settings or vara_fm_bbs.toml."
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
        if self._proxy:
            self._proxy.stop()
            self._proxy = None
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
        if getattr(self.cfg, "is_hf", False):
            bw = getattr(self.cfg, "bandwidth", None) or 500
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
        _mode = "HF" if getattr(self.cfg, "is_hf", False) else "FM"
        log.warning(f"[{self.cfg.name}] VARA {_mode} TCP dropped; attempting reconnect in 5s")
        threading.Thread(
            target=self._reconnect_loop,
            name=f"Reconnect-{self.cfg.name}",
            daemon=True,
        ).start()

    def _reconnect_loop(self):
        time.sleep(5)
        if self._retired:
            return
        _mode = "HF" if getattr(self.cfg, "is_hf", False) else "FM"
        log.info(f"[{self.cfg.name}] Attempting VARA {_mode} reconnect")
        self._interface.connect()
        if not self._retired and self._interface.state != VARAFMState.DISCONNECTED:
            self._start_data_relay()


# ── Proxy server — exposes VARA FM protocol to external apps ──────────────────

class _ClientSession(SessionHandler):
    """
    Represents one external app connected to the proxy.

    Implements SessionHandler so it can call acquire_session / release_session
    on the broker just like the BBS dispatcher does.

    The client sees a standard VARA FM command/data interface:
      - cmd socket  → send VARA commands, receive VARA events
      - data socket → send/receive RF payload bytes

    Session lifecycle:
      1. Client connects cmd socket (data socket optional until needed).
      2. Client sends LISTEN ON → broker acquires session lock (queued if busy).
      3. VARA FM events forwarded to client cmd socket.
      4. Client RF payload forwarded through broker → VARA FM data socket.
      5. On DISCONNECTED event or client disconnect → broker releases lock.
    """

    def __init__(
        self,
        broker:   "TNCBroker",
        cmd_conn: socket.socket,
        name:     str,
    ):
        self._broker    = broker
        self._cmd_conn  = cmd_conn
        self._data_conn: Optional[socket.socket] = None
        self._name      = name
        self._has_lock  = False
        self._stop      = threading.Event()
        self._cmd_buf   = ""

    # ── SessionHandler implementation ─────────────────────────────────────

    def on_cmd_event(self, event: str) -> None:
        """Forward VARA FM events back to the external client."""
        try:
            self._cmd_conn.sendall((event + "\r\n").encode("ascii", errors="replace"))
        except OSError:
            self._stop.set()

        # Auto-release on disconnect
        if event.upper().startswith("DISCONNECTED"):
            self._release()

    def on_data(self, data: bytes) -> None:
        """Forward RF data bytes to the external client's data socket."""
        if self._data_conn:
            try:
                self._data_conn.sendall(data)
            except OSError:
                self._stop.set()

    # ── Main loops ────────────────────────────────────────────────────────

    def run_cmd(self):
        """Read commands from the external client cmd socket."""
        try:
            while not self._stop.is_set():
                try:
                    chunk = self._cmd_conn.recv(1024)
                    if not chunk:
                        break
                    self._cmd_buf += chunk.decode("ascii", errors="replace")
                    self._cmd_buf = self._cmd_buf.replace("\r\n", "\n").replace("\r", "\n")
                    while "\n" in self._cmd_buf:
                        line, self._cmd_buf = self._cmd_buf.split("\n", 1)
                        line = line.strip()
                        if line:
                            self._handle_client_cmd(line)
                except OSError:
                    break
        finally:
            self._release()
            self._cleanup()

    def attach_data(self, data_conn: socket.socket):
        """Attach a data socket to this session after cmd is already running."""
        self._data_conn = data_conn
        threading.Thread(
            target=self.run_data,
            args=(data_conn,),
            name=f"ProxyDataSession-{self._name}-late",
            daemon=True,
        ).start()

    def run_data(self, data_conn: socket.socket):
        """Read data bytes from the external client data socket and forward to VARA FM."""
        self._data_conn = data_conn
        try:
            while not self._stop.is_set():
                try:
                    data = data_conn.recv(4096)
                    if not data:
                        break
                    if self._has_lock:
                        self._broker.send_data(data)
                except OSError:
                    break
        finally:
            self._stop.set()

    # ── Internal ──────────────────────────────────────────────────────────

    def _handle_client_cmd(self, cmd: str):
        upper = cmd.upper()
        log.debug(f"[{self._name}] proxy CMD from client: {cmd}")

        if upper.startswith("LISTEN"):
            # Client wants to become the active listener — acquire the lock
            if not self._has_lock:
                log.info(f"[{self._name}] External client requesting session lock")
                threading.Thread(
                    target=self._acquire_then_forward,
                    args=(cmd,),
                    daemon=True,
                ).start()
            else:
                self._broker.send_cmd(cmd)

        elif upper.startswith("CONNECT "):
            # Outbound connect — acquire lock first if we don't have it
            if not self._has_lock:
                threading.Thread(
                    target=self._acquire_then_forward,
                    args=(cmd,),
                    daemon=True,
                ).start()
            else:
                self._broker.send_cmd(cmd)

        elif upper.startswith("DISCONNECT"):
            self._broker.send_cmd(cmd)
            # Don't release yet — wait for DISCONNECTED event

        elif upper.startswith("MYCALL"):
            # Pass through but don't override the BBS callsign permanently
            if self._has_lock:
                self._broker.send_cmd(cmd)

        else:
            # All other commands (ABORT, etc.) pass through if we hold the lock
            if self._has_lock:
                self._broker.send_cmd(cmd)
            else:
                log.debug(f"[{self._name}] Proxy dropping command (no lock): {cmd}")

    def _acquire_then_forward(self, cmd_after: str):
        """Acquire the session lock (blocking, up to 60s), then send cmd_after."""
        acquired = self._broker.acquire_session(self, timeout=60.0)
        if acquired:
            self._has_lock = True
            log.info(f"[{self._name}] External client acquired session lock")
            # Send a synthetic IDLE/LISTENING acknowledgement so the client
            # knows VARA FM is ready (mirrors what VARA FM sends after init)
            try:
                self._cmd_conn.sendall(b"IAMALIVE\r\n")
            except OSError:
                pass
            self._broker.send_cmd(cmd_after)
        else:
            log.warning(f"[{self._name}] External client session lock timeout")
            try:
                self._cmd_conn.sendall(b"BUSY\r\n")
            except OSError:
                pass

    def _release(self):
        if self._has_lock:
            self._has_lock = False
            self._broker.release_session(self)
            log.info(f"[{self._name}] External client released session lock")

    def _cleanup(self):
        self._stop.set()
        for conn in (self._cmd_conn, self._data_conn):
            if conn:
                try:
                    conn.close()
                except OSError:
                    pass


class _ProxyServer:
    """
    Listens on two loopback ports (cmd + data) and manages external client
    sessions.  Each connected cmd client gets a _ClientSession; the matching
    data connection is paired by arrival order.
    """

    def __init__(
        self,
        broker:    TNCBroker,
        cmd_port:  int,
        data_port: int,
        name:      str,
    ):
        self._broker    = broker
        self._cmd_port  = cmd_port
        self._data_port = data_port
        self._name      = name
        self._stop      = threading.Event()

        # Pairing state — both lists protected by _pair_lock.
        # A cmd session and data socket are matched by whichever arrives second.
        self._unpaired_sessions: list[_ClientSession] = []  # cmd arrived first
        self._unpaired_data:     list[socket.socket]  = []  # data arrived first
        self._pair_lock = threading.Lock()

        self._cmd_server:  Optional[socket.socket] = None
        self._data_server: Optional[socket.socket] = None

    def start(self):
        # Create listening sockets
        self._cmd_server  = self._make_server(self._cmd_port)
        self._data_server = self._make_server(self._data_port)

        threading.Thread(target=self._accept_cmd,  name=f"ProxyCmd-{self._name}",  daemon=True).start()
        threading.Thread(target=self._accept_data, name=f"ProxyData-{self._name}", daemon=True).start()

    def stop(self):
        self._stop.set()
        for s in (self._cmd_server, self._data_server):
            if s:
                try:
                    s.close()
                except OSError:
                    pass

    def _make_server(self, port: int) -> socket.socket:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", port))
        s.listen(5)
        s.settimeout(1.0)
        return s

    def _accept_cmd(self):
        while not self._stop.is_set():
            try:
                conn, addr = self._cmd_server.accept()
                log.info(f"[{self._name}] External app connected (cmd) from {addr}")
                session = _ClientSession(self._broker, conn, self._name)

                with self._pair_lock:
                    if self._unpaired_data:
                        # Data socket already waiting — pair immediately
                        data_conn = self._unpaired_data.pop(0)
                        log.debug(f"[{self._name}] Paired cmd+data (data arrived first)")
                    else:
                        # Data socket not here yet — queue session and wait
                        data_conn = None
                        self._unpaired_sessions.append(session)
                        log.debug(f"[{self._name}] Cmd connected first; waiting for data socket")

                threading.Thread(
                    target=session.run_cmd,
                    name=f"ProxyCmdSession-{self._name}",
                    daemon=True,
                ).start()

                if data_conn:
                    threading.Thread(
                        target=session.run_data,
                        args=(data_conn,),
                        name=f"ProxyDataSession-{self._name}",
                        daemon=True,
                    ).start()

            except socket.timeout:
                continue
            except OSError:
                break

    def _accept_data(self):
        while not self._stop.is_set():
            try:
                conn, addr = self._data_server.accept()
                log.info(f"[{self._name}] External app connected (data) from {addr}")

                with self._pair_lock:
                    if self._unpaired_sessions:
                        # Cmd session already running — attach data socket now
                        session = self._unpaired_sessions.pop(0)
                        log.debug(f"[{self._name}] Paired data+cmd (cmd arrived first)")
                    else:
                        # Cmd socket not here yet — queue data and wait
                        session = None
                        self._unpaired_data.append(conn)
                        log.debug(f"[{self._name}] Data connected first; waiting for cmd socket")

                if session:
                    session.attach_data(conn)

            except socket.timeout:
                continue
            except OSError:
                break
