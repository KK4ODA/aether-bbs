"""
transport/agwpe_broker.py

AGWPE transport broker — wraps the AGWPEClient and implements the
TransportBroker interface so the BBS session layer can use Soundmodem
just like VARA FM.
"""

import logging
import threading
import time
from typing import Optional

from core.config import InstanceConfig, AppConfig
from core.reconnect import Backoff, retry
from core.events import EventBus, EventType
from transport.agwpe import AGWPEClient, AGWPEFrame
from transport.protocol import TransportState, TransportType, SessionHandler

log = logging.getLogger(__name__)

RECONNECT_DELAY = 5.0    # first wait; after a lost link it doubles …
RECONNECT_MAX   = 60.0   # … up to once a minute, until it is back


class AGWPEBroker:
    """
    Owns the AGWPEClient for one Soundmodem instance and
    arbitrates session access for the BBS dispatcher.

    Implements the same broker interface as TNCBroker so the
    dispatcher and instance manager can use it interchangeably.
    """

    def __init__(
        self,
        inst_cfg:  InstanceConfig,
        app_cfg:   AppConfig,
        event_bus: EventBus,
    ):
        self.cfg      = inst_cfg
        self._app_cfg = app_cfg
        self._bus     = event_bus
        self._sm      = inst_cfg.soundmodem

        self._my_call = (
            self._sm.my_callsign.upper()
            or app_cfg.station.bbs_callsign.upper()
        )
        if self._sm.my_ssid:
            self._my_call_full = f"{self._my_call}-{self._sm.my_ssid}"
        else:
            self._my_call_full = self._my_call

        self._client = AGWPEClient(
            host=self._sm.host,
            port=self._sm.port,
            on_frame=self._on_frame,
            on_disconnect=self._on_link_lost,
        )

        self._state = TransportState.DISCONNECTED
        self._session_lock   = threading.Lock()
        self._active_handler: Optional[SessionHandler] = None
        self._handler_lock   = threading.Lock()
        self._retired         = False
        self._reconnect_thread: Optional[threading.Thread] = None

        # Track active AX.25 connection
        self._remote_call: Optional[str] = None

    # ── Connection lifecycle ──────────────────────────────────────────

    def connect(self) -> None:
        """Connect to the AGWPE server. Blocking with retry."""
        self._state = TransportState.CONNECTING
        if not retry(self._attach, Backoff(RECONNECT_DELAY, RECONNECT_DELAY),
                     should_stop=lambda: self._retired,
                     on_retry=self._log_retry):
            self._state = TransportState.DISCONNECTED

    def _attach(self) -> bool:
        """One connection attempt: link up, callsign registered."""
        if not self._client.connect():
            return False
        if self._retired:              # stopped while connecting
            self._client.disconnect()
            return False
        self._state = TransportState.IDLE
        self._client.register_callsign(self._my_call_full)
        self._bus.emit(
            EventType.VARA_CONNECTED,
            source=self.cfg.name,
            data={"transport": "SOUNDMODEM", "protocol": "AGWPE"},
        )
        return True

    def _log_retry(self, tries: int, delay: float) -> None:
        log.warning(f"[{self.cfg.name}] AGWPE connect failed, "
                    f"retrying in {delay:g}s")

    def _on_link_lost(self) -> None:
        """The AGWPE server dropped the link (Soundmodem closed or
        crashed): end any session on it, show the link down, reconnect."""
        old_call, self._remote_call = self._remote_call, None
        self._state = TransportState.DISCONNECTED
        if old_call:
            log.info(f"[{self.cfg.name}] AX.25 session with {old_call} "
                     f"lost with the AGWPE link")
            self._bus.emit(EventType.SESSION_DISCONNECTED, source=self.cfg.name,
                           data={"remote_call": old_call})
            with self._handler_lock:
                if self._active_handler:
                    self._active_handler.on_cmd_event("DISCONNECTED")
        self._bus.emit(EventType.VARA_DISCONNECTED, source=self.cfg.name)
        if self._retired:
            return
        if self._reconnect_thread and self._reconnect_thread.is_alive():
            return
        self._reconnect_thread = threading.Thread(
            target=self._reconnect_loop, daemon=True,
            name=f"AGWPE-Reconnect-{self.cfg.name}")
        self._reconnect_thread.start()

    def _reconnect_loop(self) -> None:
        log.warning(f"[{self.cfg.name}] AGWPE link lost; reconnecting "
                    f"in {RECONNECT_DELAY:g}s")
        self._state = TransportState.CONNECTING
        if not retry(self._attach, Backoff(RECONNECT_DELAY, RECONNECT_MAX),
                     should_stop=lambda: self._retired, wait_first=True,
                     on_retry=self._log_retry):
            self._state = TransportState.DISCONNECTED

    def disconnect(self) -> None:
        """Disconnect and retire this broker."""
        self._retired = True
        self._client.disconnect()
        self._state = TransportState.DISCONNECTED
        self._bus.emit(
            EventType.VARA_DISCONNECTED,
            source=self.cfg.name,
        )

    def resume(self) -> None:
        """Re-activate a previously retired broker."""
        self._retired = False

    # ── Session arbitration ───────────────────────────────────────────

    def acquire_session(
        self, handler: SessionHandler, timeout: float = 5.0,
    ) -> bool:
        if not self._session_lock.acquire(timeout=timeout):
            return False
        with self._handler_lock:
            self._active_handler = handler
        return True

    def release_session(self, handler: SessionHandler) -> None:
        with self._handler_lock:
            if self._active_handler is not handler:
                return
            self._active_handler = None
        try:
            self._session_lock.release()
        except RuntimeError:
            pass
        if self._state == TransportState.CONNECTED:   # not if the link dropped
            self._state = TransportState.IDLE
        self._remote_call = None

    # ── Data transfer ─────────────────────────────────────────────────

    def send_cmd(self, cmd: str) -> None:
        """Send a control command.

        For AGWPE, most VARA-style commands are no-ops.
        Handle DISCONNECT specially.
        """
        if cmd.upper() == "DISCONNECT" and self._remote_call:
            self._client.disconnect_station(
                self._my_call_full, self._remote_call,
            )
        elif cmd.upper() == "LISTEN ON":
            pass  # AGWPE always listens after registration

    def send_data(self, data: bytes) -> None:
        """Send data on the active AX.25 connection."""
        if self._remote_call and self._client.connected:
            self._client.send_connected_data(
                self._my_call_full, self._remote_call, data,
            )

    def connect_station(
        self, remote_call: str, path: str = "",
        busy_timeout: float = 30.0,
    ) -> bool:
        """Initiate an outbound AX.25 connection."""
        if not self._client.connected:
            return False
        # TODO: handle via-path for digipeaters
        self._client.connect_station(
            self._my_call_full, remote_call.upper(),
        )
        # Wait for connection confirmation or timeout
        start = time.monotonic()
        while time.monotonic() - start < busy_timeout:
            if self._state == TransportState.CONNECTED:
                return True
            time.sleep(0.5)
        return False

    # ── Status ────────────────────────────────────────────────────────

    @property
    def state(self):
        return self._state

    @property
    def transport_type(self):
        return TransportType.SOUNDMODEM

    @property
    def instance_name(self):
        return self.cfg.name

    @property
    def busy(self) -> bool:
        return self._state == TransportState.CONNECTED

    @property
    def ptt(self) -> bool:
        return False  # Soundmodem handles PTT internally

    @property
    def remote_call(self) -> Optional[str]:
        return self._remote_call

    # ── AGWPE frame handler ───────────────────────────────────────────

    def _on_frame(self, frame: AGWPEFrame):
        """Dispatch incoming AGWPE frames."""
        kind = frame.data_kind

        if kind == "C" and frame.call_from != self._my_call_full:
            # Inbound connection from remote station
            self._remote_call = frame.call_from
            self._state = TransportState.CONNECTED
            log.info(
                f"[{self.cfg.name}] AX.25 CONNECTED {frame.call_from}"
            )
            self._bus.emit(
                EventType.SESSION_CONNECTED,
                source=self.cfg.name,
                data={"remote_call": frame.call_from},
            )
            # Forward to handler
            with self._handler_lock:
                if self._active_handler:
                    self._active_handler.on_cmd_event(
                        f"CONNECTED {self._my_call_full} {frame.call_from}"
                    )

        elif kind == "D":
            # Connected data received
            self._bus.emit(
                EventType.DATA_RX,
                source=self.cfg.name,
                data={"bytes": len(frame.data)},
            )
            with self._handler_lock:
                if self._active_handler:
                    self._active_handler.on_data(frame.data)

        elif kind == "d":
            # Disconnected
            old_call = self._remote_call
            self._remote_call = None
            self._state = TransportState.IDLE
            log.info(
                f"[{self.cfg.name}] AX.25 DISCONNECTED "
                f"{old_call or 'unknown'}"
            )
            self._bus.emit(
                EventType.SESSION_DISCONNECTED,
                source=self.cfg.name,
                data={"remote_call": old_call or ""},
            )
            with self._handler_lock:
                if self._active_handler:
                    self._active_handler.on_cmd_event("DISCONNECTED")

        elif kind == "g":
            # Port info response
            log.info(
                f"[{self.cfg.name}] AGWPE port info: "
                f"{frame.data.decode('ascii', errors='replace')}"
            )

        elif kind == "H":
            # Heard station
            self._bus.emit(
                EventType.STATION_HEARD,
                source=self.cfg.name,
                data={"callsign": frame.call_from},
            )
