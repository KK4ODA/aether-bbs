"""
transport/agwpe.py

AGWPE (AGW Packet Engine) TCP protocol client.

Connects to Soundmodem, Direwolf, QtSoundModem, or any AGWPE-compatible
TNC over TCP.  Handles the binary frame protocol for AX.25 packet radio.

Reference: http://www.interbbs.com/pdf/AGWPE-TCP-IP%20API%20v27.pdf
"""

import struct
import socket
import logging
import threading
from dataclasses import dataclass
from typing import Callable, Optional

log = logging.getLogger(__name__)

# AGWPE header: 4 bytes port + 4 bytes reserved + 1 byte data_kind + 1 byte reserved +
#               1 byte PID + 1 byte reserved + 10 bytes callFrom + 10 bytes callTo +
#               4 bytes dataLen + 4 bytes user = 36 bytes total
HEADER_FMT = "<I I c x B x 10s 10s I i"
HEADER_SIZE = 36


@dataclass
class AGWPEFrame:
    """Decoded AGWPE frame."""
    port:      int
    data_kind: str       # single ASCII char: 'C', 'D', 'd', 'G', 'g', etc.
    pid:       int
    call_from: str
    call_to:   str
    data:      bytes
    user:      int = 0


def _encode_call(callsign: str) -> bytes:
    """Encode a callsign into 10-byte null-padded field."""
    return callsign.encode("ascii")[:10].ljust(10, b"\x00")


def _decode_call(raw: bytes) -> str:
    """Decode a 10-byte null-padded callsign field."""
    return raw.split(b"\x00", 1)[0].decode("ascii", errors="replace")


def encode_frame(frame: AGWPEFrame) -> bytes:
    """Encode an AGWPEFrame into bytes for transmission."""
    header = struct.pack(
        HEADER_FMT,
        frame.port,
        0,  # reserved
        frame.data_kind.encode("ascii"),
        frame.pid,
        _encode_call(frame.call_from),
        _encode_call(frame.call_to),
        len(frame.data),
        frame.user,
    )
    return header + frame.data


def decode_frame(header: bytes, data: bytes) -> AGWPEFrame:
    """Decode an AGWPE frame from header + data bytes."""
    (port, _res1, kind_b, pid,
     call_from_b, call_to_b, data_len, user) = struct.unpack(HEADER_FMT, header)
    return AGWPEFrame(
        port=port,
        data_kind=kind_b.decode("ascii"),
        pid=pid,
        call_from=_decode_call(call_from_b),
        call_to=_decode_call(call_to_b),
        data=data,
        user=user,
    )


class AGWPEClient:
    """
    Low-level AGWPE TCP client.

    Maintains a persistent TCP connection to an AGWPE-compatible server.
    Frames are read in a background thread and dispatched to callbacks.
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 8000,
        on_frame: Optional[Callable[[AGWPEFrame], None]] = None,
    ):
        self._host = host
        self._port = port
        self._on_frame = on_frame
        self._sock: Optional[socket.socket] = None
        self._reader_thread: Optional[threading.Thread] = None
        self._running = False
        self._write_lock = threading.Lock()

    @property
    def connected(self) -> bool:
        return self._sock is not None and self._running

    def connect(self, timeout: float = 10.0) -> bool:
        """Connect to the AGWPE server. Returns True on success."""
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(timeout)
            s.connect((self._host, self._port))
            s.settimeout(None)
            self._sock = s
            self._running = True
            self._reader_thread = threading.Thread(
                target=self._reader_loop,
                daemon=True,
                name="AGWPE-Reader",
            )
            self._reader_thread.start()
            log.info(f"AGWPE connected to {self._host}:{self._port}")
            return True
        except Exception as e:
            log.error(f"AGWPE connect failed: {e}")
            self._sock = None
            return False

    def disconnect(self):
        """Close the AGWPE connection."""
        self._running = False
        if self._sock:
            try:
                self._sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        log.info("AGWPE disconnected")

    def send_frame(self, frame: AGWPEFrame) -> bool:
        """Send a frame to the AGWPE server. Thread-safe."""
        if not self._sock:
            return False
        raw = encode_frame(frame)
        with self._write_lock:
            try:
                self._sock.sendall(raw)
                return True
            except OSError as e:
                log.error(f"AGWPE send error: {e}")
                return False

    # ── Convenience methods ───────────────────────────────────────────

    def register_callsign(self, callsign: str, port: int = 0) -> bool:
        """Register a callsign with the TNC ('X' frame)."""
        return self.send_frame(AGWPEFrame(
            port=port, data_kind="X", pid=0,
            call_from=callsign, call_to="", data=b"",
        ))

    def unregister_callsign(self, callsign: str, port: int = 0) -> bool:
        """Unregister a callsign ('x' frame)."""
        return self.send_frame(AGWPEFrame(
            port=port, data_kind="x", pid=0,
            call_from=callsign, call_to="", data=b"",
        ))

    def connect_station(self, my_call: str, remote_call: str,
                        port: int = 0, pid: int = 0xF0) -> bool:
        """Initiate an AX.25 connection to a remote station ('C' frame)."""
        return self.send_frame(AGWPEFrame(
            port=port, data_kind="C", pid=pid,
            call_from=my_call, call_to=remote_call, data=b"",
        ))

    def disconnect_station(self, my_call: str, remote_call: str,
                           port: int = 0) -> bool:
        """Disconnect from a remote station ('d' frame)."""
        return self.send_frame(AGWPEFrame(
            port=port, data_kind="d", pid=0,
            call_from=my_call, call_to=remote_call, data=b"",
        ))

    def send_connected_data(self, my_call: str, remote_call: str,
                            data: bytes, port: int = 0,
                            pid: int = 0xF0) -> bool:
        """Send data on an active connection ('D' frame)."""
        return self.send_frame(AGWPEFrame(
            port=port, data_kind="D", pid=pid,
            call_from=my_call, call_to=remote_call, data=data,
        ))

    def request_port_info(self) -> bool:
        """Request port information ('G' frame)."""
        return self.send_frame(AGWPEFrame(
            port=0, data_kind="G", pid=0,
            call_from="", call_to="", data=b"",
        ))

    # ── Background reader ─────────────────────────────────────────────

    def _reader_loop(self):
        """Read and dispatch AGWPE frames from the server."""
        while self._running:
            try:
                header = self._recv_exact(HEADER_SIZE)
                if header is None:
                    break
                # Extract data length from header
                data_len = struct.unpack_from("<I", header, 28)[0]
                data = b""
                if data_len > 0:
                    data = self._recv_exact(data_len)
                    if data is None:
                        break
                frame = decode_frame(header, data)
                if self._on_frame:
                    try:
                        self._on_frame(frame)
                    except Exception as e:
                        log.error(f"AGWPE frame handler error: {e}")
            except Exception as e:
                if self._running:
                    log.error(f"AGWPE reader error: {e}")
                break

        if self._running:
            log.warning("AGWPE connection lost")
            self._running = False

    def _recv_exact(self, n: int) -> Optional[bytes]:
        """Read exactly *n* bytes from the socket."""
        buf = bytearray()
        while len(buf) < n:
            try:
                chunk = self._sock.recv(n - len(buf))
                if not chunk:
                    return None
                buf.extend(chunk)
            except OSError:
                return None
        return bytes(buf)
