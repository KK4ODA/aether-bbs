"""
bbs/dispatcher.py

Session Dispatcher.
Listens for SESSION_CONNECTED events from all VARA FM instances,
reads the first line to determine session type, then hands off to
the appropriate handler (B2F or Interactive).
"""

import queue
import logging
import threading
import time
from typing import Optional

from core.config import AppConfig, ConfigManager
from core.events import EventBus, EventType
from vara.instance_manager import ModemInstanceManager
from vara.mux import VARAFMMux
from bbs.store import MessageStore
from bbs.b2f import B2FSession
from bbs.interactive import InteractiveSession
from bbs.bulletins import BulletinStore
from bbs.filestore import FileStore
from ax25.router import PacketRouter

log = logging.getLogger(__name__)

LINE_TIMEOUT   = 10.0   # seconds to wait for first line
_MAX_DISPLAY   = 200    # max chars shown in DATA_TX/DATA_RX events

# Sentinel pushed into _line_queue on RF disconnect (distinct from None timeout)
_DISCONNECTED = object()


class InstanceSessionHandler:
    """
    Active session state for one VARA FM instance.
    Buffers incoming bytes and splits into lines for handlers.
    """

    def __init__(self, mux: VARAFMMux, instance_name: str,
                 event_bus: Optional[EventBus] = None):
        self.mux           = mux
        self.instance_name = instance_name
        self._bus          = event_bus
        self._line_queue:  queue.Queue[str] = queue.Queue()
        self._raw_queue:   queue.Queue[bytes] = queue.Queue()
        self._buf          = b""
        self._raw_mode     = False   # True while B2F is receiving binary body
        self._active_session: Optional[InteractiveSession] = None
        self._lock         = threading.Lock()

    def on_cmd_event(self, event: str):
        """Called by VARAFMMux on VARA FM command events."""
        if event.startswith("DISCONNECTED"):
            # Signal end of input to dispatch-level reader
            self._line_queue.put(_DISCONNECTED)
            # Also wake the interactive session if it is blocked in _get_line
            with self._lock:
                if self._active_session:
                    self._active_session.feed_line(None)

    def on_data(self, data: bytes):
        """Called by VARAFMMux when data arrives from the remote station."""
        log.info(f"[{self.instance_name}] RX {len(data)} bytes: {data[:80]!r}")
        if self._raw_mode:
            # B2F is receiving binary message body — route only to raw queue
            self._raw_queue.put(data)
            if self._bus:
                self._bus.emit(EventType.DATA_RX, source=self.instance_name,
                               data={"raw": True, "bytes": len(data)})
            return
        self._buf += data
        # NOTE: do NOT put data in _raw_queue here — that queue is exclusively
        # for binary B2F body transfers (_raw_mode=True).  Putting line-mode
        # data there as well causes B2F sessions to see duplicate/corrupt data.
        # Split into lines and feed to interactive session if active
        while b"\r" in self._buf or b"\n" in self._buf:
            for sep in (b"\r\n", b"\r", b"\n"):
                if sep in self._buf:
                    line, self._buf = self._buf.split(sep, 1)
                    line_str = line.decode("utf-8", errors="replace")
                    self._line_queue.put(line_str)
                    if self._bus and line_str.strip():
                        display = line_str[:_MAX_DISPLAY]
                        self._bus.emit(
                            EventType.DATA_RX,
                            source=self.instance_name,
                            data={"text": display, "raw": False},
                        )
                    with self._lock:
                        if self._active_session:
                            self._active_session.feed_line(line_str)
                    break

    def recv_line(self, timeout: float = LINE_TIMEOUT) -> Optional[str]:
        """Return next line, or None on timeout. Raises _Disconnected on RF disconnect."""
        try:
            val = self._line_queue.get(timeout=timeout)
            if val is _DISCONNECTED:
                return None   # caller treats None as "session over"
            return val
        except queue.Empty:
            return None

    def recv_raw(self, n_bytes: int, timeout: float = 120) -> bytes:
        """Read exactly n_bytes from raw queue."""
        result = b""
        deadline = threading.Event()
        while len(result) < n_bytes:
            try:
                chunk = self._raw_queue.get(timeout=1.0)
                result += chunk
            except queue.Empty:
                if not self.mux.state.name == "CONNECTED":
                    break
        return result[:n_bytes]

    def send_text(self, text: str):
        self.mux.send_data(text.encode("utf-8", errors="replace"))
        if self._bus:
            # Split multi-line output into individual display lines,
            # filtering out blank lines from \r\n sequences.
            for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
                if line.strip():
                    display = line[:_MAX_DISPLAY]
                    self._bus.emit(
                        EventType.DATA_TX,
                        source=self.instance_name,
                        data={"text": display, "raw": False},
                    )

    def send_bytes(self, data: bytes):
        self.mux.send_data(data)
        if self._bus:
            self._bus.emit(EventType.DATA_TX, source=self.instance_name,
                           data={"raw": True, "bytes": len(data)})

    def set_raw_mode(self, enabled: bool):
        """Switch between line mode and raw binary mode for incoming data."""
        self._raw_mode = enabled
        if not enabled:
            self._buf = b""  # discard any partial line buffer

    def set_active_session(self, session: Optional[InteractiveSession]):
        with self._lock:
            self._active_session = session


class SessionDispatcher:
    """
    Listens for RF connections across all instances and dispatches
    them to the appropriate handler.
    """

    def __init__(
        self,
        config,
        instance_manager: ModemInstanceManager,
        store: MessageStore,
        event_bus: EventBus,
        bulletin_store: Optional[BulletinStore] = None,
        file_store: Optional[FileStore] = None,
        access_controller=None,
        mode_policy=None,
        rate_limiter=None,
        outbound_queue=None,
        pat_client=None,
        converse_manager=None,
    ):
        self._cm       = config if isinstance(config, ConfigManager) else None
        self._config   = config if self._cm is None else config.current()
        self._im       = instance_manager
        self._store    = store
        self._bbs      = bulletin_store
        self._files    = file_store
        self._bus      = event_bus
        self._access   = access_controller
        self._policy   = mode_policy
        self._rate_limiter = rate_limiter
        self._outbound_queue = outbound_queue
        self._pat_client = pat_client
        self._converse = converse_manager
        self._router   = PacketRouter(event_bus)
        self._handlers: dict[str, InstanceSessionHandler] = {}
        self._running  = False

        # ── Global session gate ──────────────────────────────────────────
        # Limits total concurrent BBS sessions across ALL transports.
        # The per-transport lock in TNCBroker serialises within one modem;
        # this semaphore prevents cross-instance concurrency that would
        # hit the shared MessageStore / FileStore without isolation.
        cfg = self._config if self._cm is None else config.current()
        max_sess = cfg.bbs.max_sessions if cfg else 1
        self._global_gate = threading.Semaphore(max_sess)
        self._max_sessions = max_sess
        self._active_count_lock = threading.Lock()
        self._active_session_count = 0
        self._session_watchdog_timeout = (
            cfg.bbs.session_watchdog_timeout if cfg else 1800
        )

    @property
    def _live_config(self) -> AppConfig:
        return (self._cm.current() or self._config) if self._cm else self._config

    @property
    def active_session_count(self) -> int:
        with self._active_count_lock:
            return self._active_session_count

    def start(self):

        self._running = True
        self._bus.subscribe(EventType.SESSION_CONNECTED,    self._on_connected)
        self._bus.subscribe(EventType.SESSION_DISCONNECTED, self._on_disconnected)
        self._bus.subscribe(EventType.STATION_HEARD,        self._on_station_heard)

        # Build handlers for all active muxes
        for name, mux in self._im.all_muxes().items():
            self._handlers[name] = InstanceSessionHandler(mux, name, self._bus)
        log.info(
            f"SessionDispatcher started ({len(self._handlers)} instance(s), "
            f"max_sessions={self._max_sessions}, "
            f"watchdog={self._session_watchdog_timeout}s)"
        )
        if self._max_sessions > 1:
            log.warning(
                f"max_sessions={self._max_sessions} — concurrent sessions share "
                f"a non-isolated message store. Data integrity is not guaranteed "
                f"above max_sessions=1."
            )

    def stop(self):
        self._running = False
        self._bus.unsubscribe(EventType.SESSION_CONNECTED,    self._on_connected)
        self._bus.unsubscribe(EventType.SESSION_DISCONNECTED, self._on_disconnected)

    # ── Event handlers ────────────────────────────────────────────────────

    def _on_connected(self, event):
        if not self._running:
            return
        instance_name = event.source
        remote_call   = (event.data or {}).get("remote_call", "UNKNOWN")

        log.info(f"[{instance_name}] Incoming connection from {remote_call}")

        mux = self._im.get_mux(instance_name)
        if mux is None:
            log.error(f"No mux for instance {instance_name!r}")
            return
        if getattr(mux, "outbound_active", False):
            # Our own outbound call (RF forwarding) came up: it is already
            # handled by whoever placed it.
            log.info(f"[{instance_name}] Outbound session with {remote_call} "
                     f"is up")
            return

        # ── Per-transport lock (serialises within one modem) ─────────────
        handler = InstanceSessionHandler(mux, instance_name, self._bus)
        self._handlers[instance_name] = handler

        if not mux.acquire_session(handler, timeout=15.0):
            log.warning(f"[{instance_name}] Could not acquire session lock; ignoring connect")
            mux.send_cmd("DISCONNECT")
            return

        # ── Global session gate (limits total concurrent sessions) ───────
        if not self._global_gate.acquire(blocking=True, timeout=3.0):
            log.warning(
                f"[{instance_name}] BBS BUSY — rejecting {remote_call} "
                f"(active={self.active_session_count}/{self._max_sessions})"
            )
            self._send_busy_rejection(handler, mux, remote_call, instance_name)
            mux.release_session(handler)
            return

        # Increment active counter
        with self._active_count_lock:
            self._active_session_count += 1

        log.info(
            f"[{instance_name}] Global session gate acquired for {remote_call} "
            f"(active={self.active_session_count}/{self._max_sessions})"
        )

        # Log heard station
        self._router.log_heard_station(remote_call, instance_name)
        self._store.upsert_heard_station(
            remote_call.split("-")[0],
            int(remote_call.split("-")[1]) if "-" in remote_call else 0,
            instance_name,
        )

        # Dispatch in background thread (guarded wrapper releases the gate)
        threading.Thread(
            target=self._dispatch_session_guarded,
            args=(handler, remote_call, instance_name, mux),
            name=f"Session-{instance_name}-{remote_call}",
            daemon=True,
        ).start()

    def _send_busy_rejection(
        self,
        handler: InstanceSessionHandler,
        mux: VARAFMMux,
        remote_call: str,
        instance_name: str,
    ):
        """Send a BBS BUSY message over RF and disconnect the remote station."""
        bbs_call = self._live_config.station.bbs_callsign
        try:
            handler.send_text(
                "\r\n"
                "*** BBS BUSY ***\r\n"
                "Another session is currently active.\r\n"
                "Please try again later.\r\n"
                "\r\n"
                f"73 de {bbs_call}\r\n"
            )
        except Exception:
            pass  # RF may already be gone

        # Allow time for the message to be transmitted before disconnect
        time.sleep(2.0)
        mux.send_cmd("DISCONNECT")

        self._bus.emit(
            EventType.SESSION_BUSY,
            source=instance_name,
            data={"remote_call": remote_call, "reason": "global_limit"},
        )

    def _dispatch_session_guarded(
        self,
        handler: InstanceSessionHandler,
        remote_call: str,
        instance_name: str,
        mux: VARAFMMux,
    ):
        """Wrapper around _dispatch_session that manages the global session
        gate and a watchdog timer for stale session protection."""
        watchdog = threading.Timer(
            self._session_watchdog_timeout,
            self._force_disconnect_watchdog,
            args=(mux, instance_name, remote_call),
        )
        watchdog.daemon = True
        watchdog.start()

        try:
            self._dispatch_session(handler, remote_call, instance_name, mux)
        finally:
            watchdog.cancel()
            with self._active_count_lock:
                self._active_session_count = max(0, self._active_session_count - 1)
            try:
                self._global_gate.release()
            except ValueError:
                pass  # already released (shouldn't happen)
            log.info(
                f"[{instance_name}] Global session gate released for {remote_call} "
                f"(active={self.active_session_count}/{self._max_sessions})"
            )

    def _force_disconnect_watchdog(
        self,
        mux: VARAFMMux,
        instance_name: str,
        remote_call: str,
    ):
        """Called by the watchdog timer when a session exceeds the maximum
        allowed duration.  Forces a disconnect so the global gate is freed."""
        log.error(
            f"[{instance_name}] Session watchdog EXPIRED for {remote_call} "
            f"after {self._session_watchdog_timeout}s — forcing disconnect"
        )
        try:
            mux.send_cmd("DISCONNECT")
        except Exception:
            pass
        # The session thread's finally block in _dispatch_session_guarded
        # will release the semaphore when the DISCONNECTED event propagates
        # and the session exits.

    def _dispatch_session(
        self,
        handler: InstanceSessionHandler,
        remote_call: str,
        instance_name: str,
        mux: VARAFMMux,
    ):
        # ── Pre-session security gate ────────────────────────────────
        if self._access:
            allowed, reason = self._access.check_lockout(
                remote_call,
                self._policy.auto_block_threshold if self._policy else None)
            if not allowed:
                try:
                    handler.send_text(f"\r\n{reason}\r\n")
                except Exception:
                    pass
                mux.send_cmd("DISCONNECT")
                return

        if self._rate_limiter:
            ok, count = self._rate_limiter.check(remote_call, "connect")
            if not ok:
                log.warning(
                    f"[{instance_name}] Rate limited {remote_call} "
                    f"(connect count={count})"
                )
                if self._access:
                    self._access.audit(remote_call, "RATE_LIMITED",
                                       f"connect count={count}",
                                       instance_name)
                try:
                    handler.send_text(
                        "\r\nToo many connections. Try again later.\r\n"
                    )
                except Exception:
                    pass
                mux.send_cmd("DISCONNECT")
                return

        neighbour = self._rf_neighbour(remote_call)
        if neighbour is not None:
            self._run_forwarding_session(handler, remote_call, instance_name,
                                         mux, neighbour)
            return

        self._bus.emit(EventType.SESSION_STARTED, source=instance_name,
                       data={"remote_call": remote_call,
                             "session_type": "Interactive"})
        try:
            # Start the interactive session immediately so the banner and
            # welcome message are transmitted without delay.  B2F detection
            # is handled inside the interactive session: if the remote sends
            # a "[WL2K...]" line as their first command, the session hands
            # off to a B2F handler transparently.
            log.info(f"[{instance_name}] Starting interactive session with {remote_call}")
            self._run_interactive_session(handler, remote_call, instance_name, mux)
        except Exception as e:
            log.error(f"[{instance_name}] Session dispatch error: {e}", exc_info=True)
            try:
                handler.send_text("\r\nAn error occurred. Disconnecting.\r\n")
            except Exception:
                pass
            mux.send_cmd("DISCONNECT")
        finally:
            mux.release_session(handler)
            handler.set_active_session(None)

    def _rf_neighbour(self, remote_call: str):
        """The enabled RF forwarding neighbour ``remote_call`` is, or None."""
        for n in getattr(self._live_config.bbs, "forwarding_neighbours", []):
            if n.enabled and n.via == "RF" and n.matches(remote_call):
                return n
        return None

    def _run_forwarding_session(self, handler, remote_call, instance_name,
                                mux, neighbour):
        """A neighbour BBS called to forward bulletins: answer with the
        forwarding protocol instead of the user interface."""
        from bbs.bulletins import BulletinForwardingServer, RF_LINE_TIMEOUT
        log.info(f"[{instance_name}] Forwarding session from neighbour "
                 f"{remote_call}")
        self._bus.emit(EventType.SESSION_STARTED, source=instance_name,
                       data={"remote_call": remote_call,
                             "session_type": "Forwarding"})
        server = BulletinForwardingServer(self._live_config, self._store,
                                          self._bbs, self._bus)
        try:
            server.serve(
                send=lambda line: handler.send_text(line + "\r"),
                recv=lambda: handler.recv_line(timeout=RF_LINE_TIMEOUT),
                expected_call=neighbour.bbs_callsign,
            )
        except Exception as e:
            log.error(f"[{instance_name}] Forwarding session error: {e}",
                      exc_info=True)
        finally:
            time.sleep(2.0)   # let the last line go out before the DISC
            mux.send_cmd("DISCONNECT")
            mux.release_session(handler)
            handler.set_active_session(None)

    def _run_b2f_session(
        self,
        handler: InstanceSessionHandler,
        remote_call: str,
        first_line: str,
        mux: VARAFMMux,
    ):
        session = B2FSession(
            bbs_callsign=self._config.station.bbs_callsign,
            store=self._store,
            send_fn=handler.send_text,
            recv_fn=handler.recv_line,
            recv_raw=handler.recv_raw,
            send_raw_fn=handler.send_bytes,
            set_raw_mode=handler.set_raw_mode,
            bulletin_store=self._bbs,
            remote_call=remote_call,
        )
        session.run(first_line=first_line)
        # Send DISCONNECT so VARA (or the simulator) issues a DISCONNECTED
        # response, which interface.py turns into a SESSION_DISCONNECTED event.
        # Without this the simulator tab never sees the session end and its
        # B2F/scenario buttons remain disabled.
        if mux is not None:
            mux.send_cmd("DISCONNECT")

    def _run_interactive_session(
        self,
        handler: InstanceSessionHandler,
        remote_call: str,
        instance_name: str,
        mux,
    ):
        # Build the B2F handoff callable.
        # WL2K clients are routed to the local B2F session handler.
        # (CMS gateway bridging is now handled by Pat externally.)
        def b2f_handoff(first_line: str):
            log.info(
                f"[{instance_name}] Switching to B2F "
                f"for {remote_call}"
            )
            self._bus.emit(
                EventType.SESSION_STARTED, source=instance_name,
                data={"remote_call": remote_call,
                      "session_type": "B2F"},
            )
            self._run_b2f_session(
                handler, remote_call, first_line, mux,
            )

        # Resolve transport label for this instance
        transport_label = "VARA_FM"
        for inst in self._live_config.instances:
            if inst.name == instance_name:
                transport_label = inst.transport
                if inst.is_soundmodem:
                    transport_label = f"SOUNDMODEM ({inst.soundmodem.protocol})"
                break

        session = InteractiveSession(
            remote_call=remote_call,
            instance_name=instance_name,
            config=self._live_config,
            store=self._store,
            event_bus=self._bus,
            send_text_fn=handler.send_text,
            disconnect_fn=lambda: mux.send_cmd("DISCONNECT"),
            bulletin_store=self._bbs,
            file_store=self._files,
            b2f_handoff_fn=b2f_handoff,
            access_controller=self._access,
            mode_policy=self._policy,
            rate_limiter=self._rate_limiter,
            outbound_queue=self._outbound_queue,
            pat_client=self._pat_client,
            transport_label=transport_label,
            converse_manager=self._converse,
        )
        handler.set_active_session(session)
        session.run()

    def _on_disconnected(self, event):
        instance_name = event.source
        handler = self._handlers.get(instance_name)
        if handler:
            handler.on_cmd_event("DISCONNECTED")

    def _on_station_heard(self, event):
        data = event.data or {}
        callsign = data.get("callsign", "")
        if callsign:
            self._store.upsert_heard_station(
                callsign,
                data.get("ssid", 0),
                event.source,
                data.get("via_path", ""),
            )
