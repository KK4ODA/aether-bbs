"""
vara/interface.py

Low-level TCP interface to a single VARA FM, VARA HF or Aether HF instance
(Aether HF answers on the same published VARA host interface).
Maintains two socket connections:
  - cmd_sock  (port N+0): line-delimited command/event channel
  - data_sock (port N+1): raw byte stream for payload data

All VARA events are parsed here and forwarded to the EventBus.
"""

import time
import queue
import socket
import logging
import threading
from enum import Enum, auto
from typing import Optional

from core.config import InstanceConfig
from core.reconnect import Backoff, retry
from core.events import EventBus, EventType
from core.logging_manager import VARA_CMD_LOGGER_NAME

log      = logging.getLogger(__name__)
vara_log = logging.getLogger(VARA_CMD_LOGGER_NAME)

CONNECT_TIMEOUT  = 10   # seconds
SOCKET_POLL_TIMEOUT = 30.0  # recv() wakes this often to check for stop; not an error
CONNECT_RETRIES  = 5
RETRY_BASE_DELAY = 1.0  # seconds (exponential backoff)
MAX_RETRY_DELAY  = 60.0


def modem_label(cfg) -> str:
    """Human name of the modem behind an instance, for log lines."""
    if getattr(cfg, "is_aether", False):
        return "Aether HF"
    return "VARA HF" if getattr(cfg, "is_hf", False) else "VARA FM"


def init_bandwidth(cfg) -> Optional[int]:
    """The BW<n> to send at initialisation, or None to send none.

    VARA HF always gets one (500, 2300 or 2750).  Aether HF gets one only
    when aether.bandwidth asks for it: otherwise the station keeps its own
    [radio] bandwidth, and still answers a narrower call at 500 Hz.
    """
    if getattr(cfg, "is_aether", False):
        bw = getattr(getattr(cfg, "aether", None), "bandwidth", 0) or 0
        return bw if bw in (500, 2300) else None
    if not getattr(cfg, "is_hf", False):
        return None
    bw = getattr(cfg, "bandwidth", None) or 500
    if bw not in (500, 2300, 2750):
        log.warning(f"[{cfg.name}] Invalid HF bandwidth {bw}, defaulting to 2300")
        bw = 2300
    return bw


class VARAFMState(Enum):
    DISCONNECTED = auto()   # not connected to VARA FM TCP
    CONNECTING   = auto()   # TCP connect in progress
    IDLE         = auto()   # connected, no RF session
    LISTENING    = auto()   # LISTEN ON sent
    CONNECTED    = auto()   # RF session active
    DISCONNECTING= auto()   # waiting for RF disconnect


class VARAFMInterface:
    """
    Manages the two TCP connections to one VARA FM instance.
    Thread-safe: start() spawns background reader/writer threads.
    """

    def __init__(self, cfg: InstanceConfig, event_bus: EventBus):
        self.cfg = cfg
        self._bus = event_bus

        self._cmd_sock:  Optional[socket.socket] = None
        self._data_sock: Optional[socket.socket] = None

        self._state = VARAFMState.DISCONNECTED
        self._busy  = False
        self._ptt   = False

        self._rx_queue: queue.Queue[bytes] = queue.Queue()
        self._tx_queue: queue.Queue[bytes] = queue.Queue()

        self._stop_event   = threading.Event()
        self._connected_event = threading.Event()

        self._cmd_buf  = ""      # incomplete line buffer
        self._lock     = threading.Lock()

        self._remote_call: Optional[str] = None
        self._last_cmd: str = ""           # last command sent (for OK/WRONG context)
        self._init_wrong_count: int = 0    # count of WRONG during init sequence
        self._in_init: bool = False        # True while init commands are being sent

    # ── Public API ─────────────────────────────────────────────────────────

    @property
    def state(self) -> VARAFMState:
        return self._state

    @property
    def busy(self) -> bool:
        return self._busy

    @property
    def ptt(self) -> bool:
        return self._ptt

    @property
    def remote_call(self) -> Optional[str]:
        return self._remote_call

    def connect(self):
        """Connect to VARA FM TCP ports with retry/backoff. Blocking."""
        self._stop_event.clear()
        self._set_state(VARAFMState.CONNECTING)

        attempts = [0]

        def attempt() -> bool:
            attempts[0] += 1
            try:
                self._open_sockets()
                return True
            except OSError as e:
                log.warning(
                    f"[{self.cfg.name}] TCP connect attempt {attempts[0]}/"
                    f"{CONNECT_RETRIES} failed: {e}"
                )
                return False

        # Event.wait, so disconnect() can interrupt a wait between tries
        if retry(attempt, Backoff(RETRY_BASE_DELAY, MAX_RETRY_DELAY),
                 should_stop=self._stop_event.is_set,
                 wait=lambda delay: self._stop_event.wait(timeout=delay),
                 max_attempts=CONNECT_RETRIES):
            log.info(
                f"[{self.cfg.name}] Connected to {modem_label(self.cfg)} "
                f"(cmd:{self.cfg.cmd_port} data:{self.cfg.data_port})"
            )
            self._set_state(VARAFMState.IDLE)
            self._bus.emit(EventType.VARA_CONNECTED, source=self.cfg.name)
            self._start_threads()
            self._init_vara()
            return
        if self._stop_event.is_set():
            return

        log.error(f"[{self.cfg.name}] Could not connect to {modem_label(self.cfg)} "
                  f"after {CONNECT_RETRIES} attempts")
        if getattr(self.cfg, "is_aether", False):
            log.error(
                f"[{self.cfg.name}] Is Aether HF running with 'Host programs' "
                f"on (Setup step 5, port {self.cfg.cmd_port})?"
            )
        self._set_state(VARAFMState.DISCONNECTED)

    def _open_sockets(self):
        """Open the command and data ports (raises OSError)."""
        self._cmd_sock = socket.create_connection(
            (self.cfg.host, self.cfg.cmd_port), timeout=CONNECT_TIMEOUT)
        try:
            self._data_sock = socket.create_connection(
                (self.cfg.host, self.cfg.data_port), timeout=CONNECT_TIMEOUT)
        except OSError:
            self._cmd_sock.close()
            self._cmd_sock = None
            raise
        self._cmd_sock.settimeout(SOCKET_POLL_TIMEOUT)
        self._data_sock.settimeout(SOCKET_POLL_TIMEOUT)

    def disconnect(self):
        """Cleanly disconnect from VARA FM TCP.

        Sends protocol-level DISCONNECT (if an RF session is active) and
        LISTEN OFF before closing the TCP sockets, so the VARA modem
        releases the channel and PTT cleanly.
        """
        self._stop_event.set()
        # Send graceful shutdown commands while sockets are still alive
        if self._cmd_sock is not None:
            try:
                if self._state == VARAFMState.CONNECTED:
                    self._cmd_sock.sendall(b"DISCONNECT\r\n")
                    time.sleep(0.3)
                self._cmd_sock.sendall(b"LISTEN OFF\r\n")
                time.sleep(0.2)
            except OSError:
                pass   # socket may already be dead
        self._close_sockets()
        self._set_state(VARAFMState.DISCONNECTED)
        self._bus.emit(EventType.VARA_DISCONNECTED, source=self.cfg.name)

    def send_cmd(self, cmd: str):
        """Send a VARA FM command on the command channel."""
        if self._cmd_sock is None:
            log.warning(f"[{self.cfg.name}] send_cmd called but not connected: {cmd}")
            return
        try:
            self._last_cmd = cmd
            self._cmd_sock.sendall((cmd + "\r\n").encode("ascii"))
            log.debug(f"[{self.cfg.name}] CMD> {cmd}")
            vara_log.debug(f"[{self.cfg.name}] TX> {cmd}")
        except OSError as e:
            log.error(f"[{self.cfg.name}] send_cmd error: {e}")
            self._handle_tcp_error()

    def send_data(self, data: bytes):
        """Queue data for transmission on the data channel."""
        self._tx_queue.put(data)

    def recv_data(self, timeout: float = 1.0) -> Optional[bytes]:
        """Non-blocking read from RX queue."""
        try:
            return self._rx_queue.get(timeout=timeout)
        except queue.Empty:
            return None

    # ── Internal ──────────────────────────────────────────────────────────

    def _init_vara(self):
        """Send initialisation commands after TCP connect.

        Follows the same command sequence used by Pat (la5nta), BPQ32,
        JNOS, and vARIM — the proven open-source VARA implementations:

            1. MYCALL <call>       — register station callsign
            2. COMPRESSION OFF     — raw data mode (BBS handles its own framing)
            3. BW<n>               — (HF only) set RF bandwidth
            4. LISTEN ON           — accept inbound connections

        JNOS documentation specifically notes that pauses of ≥ 1 s between
        commands are "very important".  VARA must process and acknowledge
        each command before the next is sent; rapid-fire commands can cause
        WRONG rejections or silently dropped state.

        No WINLINK command is sent — VARA HF defaults to raw data mode
        which is correct for BBS operation.

        Aether HF takes the same sequence.  Its MYCALL replaces the callsign
        in Aether's own configuration while the BBS is attached, and until
        LISTEN ON it answers no calls (Aether host interface spec §3).  BW is
        sent only when aether.bandwidth asks for one.
        """
        mode = modem_label(self.cfg)
        log.info(f"[{self.cfg.name}] Initialising {mode} modem...")

        # Reset init failure counter — _handle_event increments on WRONG
        self._init_wrong_count = 0
        self._in_init = True

        def _aborted() -> bool:
            """Check if the TCP connection was lost during init."""
            if self._cmd_sock is None or self._state == VARAFMState.DISCONNECTED:
                log.warning(
                    f"[{self.cfg.name}] {mode} init aborted — "
                    f"TCP connection lost during initialisation"
                )
                self._in_init = False
                return True
            return False

        # Allow VARA modem to finish its own TCP-accept housekeeping
        time.sleep(2.0)
        if _aborted():
            return

        # 1. Set callsign
        self.send_cmd(f"MYCALL {self.cfg.cfg_bbs_callsign()}")
        time.sleep(1.0)
        if _aborted():
            return

        # 2. Disable VARA-level compression — the BBS protocol layer
        #    handles its own framing and any compression (B2F/LZHUF).
        #    VARA defaults to COMPRESSION TEXT (Huffman) which can corrupt
        #    binary data or interfere with higher-level protocols.
        self.send_cmd("COMPRESSION OFF")
        time.sleep(1.0)
        if _aborted():
            return

        # 3. HF-specific: set bandwidth
        bw = init_bandwidth(self.cfg)
        if bw:
            self.send_cmd(f"BW{bw}")
            time.sleep(1.0)
            if _aborted():
                return
            log.info(f"[{self.cfg.name}] {mode} bandwidth set to {bw} Hz")
        elif getattr(self.cfg, "is_aether", False):
            log.info(f"[{self.cfg.name}] Aether HF keeps its own bandwidth "
                     f"(Setup step 4)")

        # Aether HF names itself and its version in answer to VERSION
        if getattr(self.cfg, "is_aether", False):
            self.send_cmd("VERSION")
            time.sleep(0.5)
            if _aborted():
                return

        # 4. Enable inbound connections
        self.send_cmd("LISTEN ON")
        # Wait for the response before declaring success
        time.sleep(1.5)
        if _aborted():
            return

        self._in_init = False

        # Validate init outcome
        if self._init_wrong_count > 0:
            log.error(
                f"[{self.cfg.name}] {mode} init FAILED: "
                f"{self._init_wrong_count} command(s) rejected with WRONG. "
                f"Modem may NOT be listening for connections!"
            )
            # Still set LISTENING as best-effort — the operator can see the
            # error in logs and the dashboard will show the error count.
            # A future improvement would abort here.
        else:
            log.info(
                f"[{self.cfg.name}] {mode} init OK: all commands acknowledged"
            )

        self._set_state(VARAFMState.LISTENING)
        log.info(f"[{self.cfg.name}] {mode} initialised, listening")

    def _start_threads(self):
        # Each thread keeps the socket it was started with and ends when that
        # socket is replaced, so a reconnect never leaves an old writer
        # draining the same TX queue onto the new socket.
        cmd_sock, data_sock = self._cmd_sock, self._data_sock
        for target, sock, name in [
            (self._cmd_reader,  cmd_sock,  f"CmdRx-{self.cfg.name}"),
            (self._data_reader, data_sock, f"DataRx-{self.cfg.name}"),
            (self._data_writer, data_sock, f"DataTx-{self.cfg.name}"),
        ]:
            t = threading.Thread(target=target, args=(sock,), name=name, daemon=True)
            t.start()

    def _cmd_reader(self, sock: socket.socket):
        """Read lines from command socket; parse VARA FM events."""
        while not self._stop_event.is_set() and self._cmd_sock is sock:
            try:
                chunk = sock.recv(1024)
                if not chunk:
                    break
                self._cmd_buf += chunk.decode("ascii", errors="replace")
                # Normalise \r\n and bare \r to \n so the split always works
                self._cmd_buf = self._cmd_buf.replace("\r\n", "\n").replace("\r", "\n")
                while "\n" in self._cmd_buf:
                    line, self._cmd_buf = self._cmd_buf.split("\n", 1)
                    line = line.strip()
                    if line:
                        log.debug(f"[{self.cfg.name}] EVT< {line}")
                        vara_log.debug(f"[{self.cfg.name}] RX< {line}")
                        self._handle_event(line)
            except socket.timeout:
                # A quiet command port is normal: VARA sends IAMALIVE only
                # about once a minute.  The timeout just lets us check stop.
                continue
            except OSError:
                break
        # Report the loss once, and only for the connection still in use
        if not self._stop_event.is_set() and self._cmd_sock is sock:
            self._handle_tcp_error()

    def _data_reader(self, sock: socket.socket):
        """Read raw bytes from data socket into RX queue."""
        while not self._stop_event.is_set() and self._data_sock is sock:
            try:
                data = sock.recv(4096)
                if not data:
                    break
                self._rx_queue.put(data)
            except socket.timeout:
                # Timeouts are normal during idle/listening — just retry.
                continue
            except OSError:
                break

    def _data_writer(self, sock: socket.socket):
        """Drain TX queue to data socket."""
        while not self._stop_event.is_set() and self._data_sock is sock:
            try:
                data = self._tx_queue.get(timeout=0.5)
                sock.sendall(data)
            except queue.Empty:
                continue
            except OSError as e:
                log.error(f"[{self.cfg.name}] data write error: {e}")
                break

    def _handle_event(self, event: str):
        """Parse a VARA FM event line and emit on the EventBus.

        Event format confirmed from live vara_cmd.log capture:
          BUSY ON / BUSY OFF       (not BUSY TRUE/FALSE as some docs suggest)
          PTT ON  / PTT OFF        (not PTT TRUE/FALSE)
          CONNECTED <remote> <local> [WIDE]
          DISCONNECTED
          BUFFER <n>
          IAMALIVE                 (VARA FM keepalive, sent every ~60 s)
          REGISTERED <call>
          LINK REGISTERED
          UNENCRYPTED LINK
          ENCRYPTION DISABLED
          PENDING / CANCELPENDING
          SN <value>
          BITRATE (<n>) <speed> bps TX|RX
          WRONG                    (rejection of an unrecognised command)
          OK                       (acknowledgement of a command)
        """
        upper = event.upper()

        if upper.startswith("CONNECTED"):
            parts = event.split()
            remote = self._remote_from_connected(parts)
            self._remote_call = remote
            self._set_state(VARAFMState.CONNECTED)
            self._connected_event.set()
            self._bus.emit(EventType.SESSION_CONNECTED, source=self.cfg.name,
                           data={"remote_call": remote})
            log.info(f"[{self.cfg.name}] RF session connected: {remote}")

        elif upper.startswith("DISCONNECTED"):
            self._remote_call = None
            self._connected_event.clear()
            self._set_state(VARAFMState.LISTENING)
            self._bus.emit(EventType.SESSION_DISCONNECTED, source=self.cfg.name)
            log.info(f"[{self.cfg.name}] RF session disconnected")

        # VARA FM sends "BUSY ON" / "BUSY OFF"
        elif upper == "BUSY ON":
            self._busy = True
            self._bus.emit(EventType.CHANNEL_BUSY, source=self.cfg.name,
                           data={"busy": True})

        elif upper == "BUSY OFF":
            self._busy = False
            self._bus.emit(EventType.CHANNEL_BUSY, source=self.cfg.name,
                           data={"busy": False})

        # VARA FM sends "PTT ON" / "PTT OFF"
        elif upper == "PTT ON":
            self._ptt = True
            self._bus.emit(EventType.PTT_CHANGE, source=self.cfg.name,
                           data={"ptt": True})

        elif upper == "PTT OFF":
            self._ptt = False
            self._bus.emit(EventType.PTT_CHANGE, source=self.cfg.name,
                           data={"ptt": False})

        elif upper.startswith("BUFFER"):
            parts = event.split()
            n = int(parts[1]) if len(parts) >= 2 and parts[1].isdigit() else 0
            self._bus.emit(EventType.BUFFER_UPDATE, source=self.cfg.name,
                           data={"bytes": n})

        # Known informational events — log OK with command context to verify
        # init sequence acknowledgement; log PENDING for connect attempt detection.
        elif upper == "OK":
            last = getattr(self, '_last_cmd', '?')
            log.info(f"[{self.cfg.name}] VARA OK (after: {last})")

        elif upper == "PENDING":
            log.info(f"[{self.cfg.name}] *** INBOUND CONNECT ATTEMPT DETECTED (PENDING) ***")
            self._bus.emit(EventType.SESSION_PENDING, source=self.cfg.name)

        elif upper == "CANCELPENDING":
            log.warning(f"[{self.cfg.name}] Inbound connect attempt CANCELLED "
                        f"(caller gave up or handshake failed)")
            self._bus.emit(EventType.SESSION_PENDING_CANCELLED, source=self.cfg.name)

        elif upper in ("IAMALIVE",
                       "LINK REGISTERED", "UNENCRYPTED LINK", "ENCRYPTION DISABLED"):
            pass

        elif upper == "MISSING SOUNDCARD":
            log.error(
                f"[{self.cfg.name}] {modem_label(self.cfg)} reports MISSING "
                f"SOUNDCARD — it can neither hear nor transmit. Check the "
                f"sound card in the modem's setup."
            )

        elif upper.startswith("VERSION "):
            log.info(f"[{self.cfg.name}] Modem: {event[8:].strip()}")

        elif upper.startswith("SN "):
            try:
                snr = float(event.split()[1])
            except (IndexError, ValueError):
                return
            self._bus.emit(EventType.LINK_QUALITY, source=self.cfg.name,
                           data={"snr_db": snr})

        elif upper.startswith("BITRATE "):
            # "BITRATE (<level>) <bps> BPS [TX|RX]"
            nums = [t.strip("()") for t in event.split()[1:3]]
            try:
                level, bps = int(nums[0]), int(nums[1])
            except (IndexError, ValueError):
                return
            self._bus.emit(EventType.LINK_QUALITY, source=self.cfg.name,
                           data={"level": level, "bps": bps})

        elif upper == "WRONG":
            last = getattr(self, '_last_cmd', '?')
            if getattr(self, '_in_init', False):
                self._init_wrong_count += 1
                log.error(f"[{self.cfg.name}] VARA REJECTED init command: '{last}' (WRONG)")
            else:
                log.error(f"[{self.cfg.name}] VARA REJECTED command: '{last}' (WRONG)")

        elif upper.startswith(("REGISTERED ", "CQFRAME ")):
            pass

    def _remote_from_connected(self, parts: list[str]) -> str:
        """Pick the other station's callsign out of a CONNECTED line.

        The published form is ``CONNECTED <caller> <called> [<bw>]``: the
        caller comes first whichever side this is, so an inbound session
        names the remote first and an outbound one names it second.  Take
        the first callsign that is not this station's.
        """
        own = self.cfg.cfg_bbs_callsign().upper()
        calls = [p for p in parts[1:3] if not p.isdigit()]
        for c in calls:
            if c.upper() != own:
                return c
        return calls[0] if calls else "UNKNOWN"

    def _handle_tcp_error(self):
        log.error(f"[{self.cfg.name}] TCP connection lost")
        self._close_sockets()
        self._set_state(VARAFMState.DISCONNECTED)
        self._bus.emit(EventType.VARA_DISCONNECTED, source=self.cfg.name)

    def _close_sockets(self):
        for sock in (self._cmd_sock, self._data_sock):
            if sock:
                try:
                    sock.close()
                except OSError:
                    pass
        self._cmd_sock = None
        self._data_sock = None

    def _set_state(self, state: VARAFMState):
        old = self._state
        self._state = state
        if old != state:
            log.info(f"[{self.cfg.name}] State: {old.name} -> {state.name}")

    # ── CSMA-aware outbound connect ────────────────────────────────────────

    def connect_station(
        self,
        remote_call: str,
        path: str = "",
        busy_timeout: float = 30.0,
    ) -> bool:
        """
        Issue CONNECT when channel is clear.
        Implements CSMA-like backoff.
        Returns True if CONNECT was issued.
        """
        import random
        deadline = time.time() + busy_timeout
        while time.time() < deadline:
            if not self._busy:
                jitter = random.uniform(0.3, 1.5)
                time.sleep(jitter)
                if not self._busy:
                    cmd = f"CONNECT {self.cfg.cfg_bbs_callsign()} {remote_call}"
                    if path:
                        cmd += f" VIA {path}"
                    self.send_cmd(cmd)
                    return True
            time.sleep(0.5)
        log.warning(f"[{self.cfg.name}] Channel busy timeout for CONNECT to {remote_call}")
        return False


# Monkey-patch helper so interface can get bbs_callsign from its config
def _cfg_bbs_callsign(self) -> str:
    # InstanceConfig doesn't carry the bbs callsign; the interface is
    # initialised with it separately via set_callsign()
    return getattr(self, "_bbs_callsign", "N0CALL-10")

InstanceConfig.cfg_bbs_callsign = _cfg_bbs_callsign


class VARAFMInterfaceWithCall(VARAFMInterface):
    """VARAFMInterface that holds the BBS callsign."""

    def __init__(self, cfg: InstanceConfig, event_bus: EventBus, bbs_callsign: str):
        super().__init__(cfg, event_bus)
        self.cfg._bbs_callsign = bbs_callsign
