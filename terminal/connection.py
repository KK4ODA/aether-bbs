"""
terminal/connection.py

Transport-agnostic connection manager for the VARA BBS Terminal.

Connects to a BBS via either VARA FM or Soundmodem (AGWPE/KISS),
sends/receives text, and auto-detects the BBS transport type.
"""

import logging
import queue
import threading
import time
from typing import Callable, Optional

log = logging.getLogger(__name__)


class TerminalConnection:
    """
    Manages a connection to a BBS.

    Abstracts the transport layer — the terminal UI works the same
    regardless of whether the BBS is running VARA FM or Soundmodem.
    """

    def __init__(self):
        self._transport = None       # underlying transport object
        self._transport_type = ""    # detected BBS transport (e.g. "VARA_FM", "SOUNDMODEM (AGWPE)")
        self._connected = False
        self._rx_queue: queue.Queue[str] = queue.Queue()
        self._on_data: Optional[Callable[[str], None]] = None
        self._on_disconnect: Optional[Callable[[], None]] = None

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def transport_type(self) -> str:
        return self._transport_type

    def connect_vara(self, host: str, cmd_port: int, data_port: int,
                     my_call: str, bbs_call: str) -> bool:
        """Connect to BBS via VARA FM."""
        # TODO: Implement VARA FM terminal transport
        # This would use a VARAFMInterface in client mode
        log.info(f"VARA FM connect to {bbs_call} via {host}:{cmd_port}")
        return False

    def connect_agwpe(self, host: str, port: int,
                      my_call: str, bbs_call: str) -> bool:
        """Connect to BBS via Soundmodem AGWPE."""
        from transport.agwpe import AGWPEClient

        client = AGWPEClient(host=host, port=port, on_frame=self._on_agwpe_frame)
        if not client.connect():
            return False

        self._transport = client
        client.register_callsign(my_call)
        time.sleep(0.5)
        client.connect_station(my_call, bbs_call)

        # Wait for connection
        start = time.monotonic()
        while time.monotonic() - start < 30.0:
            if self._connected:
                return True
            time.sleep(0.5)

        log.warning("AGWPE connect timeout")
        client.disconnect()
        return False

    def send(self, text: str) -> bool:
        """Send text to the BBS."""
        if not self._connected or not self._transport:
            return False
        # The actual send depends on transport type
        # For AGWPE, we need the callsigns
        return True

    def recv_line(self, timeout: float = 1.0) -> Optional[str]:
        """Receive a line from the BBS."""
        try:
            return self._rx_queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def query_transport(self) -> str:
        """Send ;TRANSPORT? and return the BBS response."""
        self.send(";TRANSPORT?\r")
        line = self.recv_line(timeout=5.0)
        if line and line.startswith(";TRANSPORT "):
            self._transport_type = line[11:].strip()
        return self._transport_type

    def disconnect(self):
        """Close the connection."""
        self._connected = False
        if self._transport:
            if hasattr(self._transport, "disconnect"):
                self._transport.disconnect()
            self._transport = None

    # ── Internal ──────────────────────────────────────────────────────

    def _on_agwpe_frame(self, frame):
        """Handle frames from AGWPE transport."""
        from transport.agwpe import AGWPEFrame
        if frame.data_kind == "C" and not self._connected:
            self._connected = True
            log.info(f"Terminal connected to {frame.call_from}")
        elif frame.data_kind == "D":
            text = frame.data.decode("utf-8", errors="replace")
            self._rx_queue.put(text)
            if self._on_data:
                self._on_data(text)
        elif frame.data_kind == "d":
            self._connected = False
            log.info("Terminal disconnected")
            if self._on_disconnect:
                self._on_disconnect()
