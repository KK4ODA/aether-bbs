"""
tests/test_aether_ws.py

The control API's WebSocket form, as aether/websocket.py speaks it: a probe
whose answer arrives as a ``log`` event, a refused probe, and the "Update
required" warning for a station on another link protocol.
"""

import base64
import hashlib
import json
import socket
import struct
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from aether.control import AetherControlClient, AetherControlError

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class FakeWSControl:
    """Answers one WebSocket per connection with ``script(request)``."""

    def __init__(self, script):
        self.script = script
        self.requests = []
        self.srv = socket.socket()
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(4)
        self.port = self.srv.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.srv.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn):
        head = b""
        while b"\r\n\r\n" not in head:
            head += conn.recv(1024)
        key = [l.split(b":", 1)[1].strip() for l in head.split(b"\r\n")
               if l.lower().startswith(b"sec-websocket-key")][0]
        accept = base64.b64encode(hashlib.sha1(key + GUID.encode()).digest())
        conn.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                     b"Connection: Upgrade\r\nSec-WebSocket-Accept: " + accept +
                     b"\r\n\r\n")
        b0, b1 = conn.recv(2)
        n = b1 & 0x7F
        if n == 126:
            n = struct.unpack("!H", conn.recv(2))[0]
        mask = conn.recv(4)
        data = b""
        while len(data) < n:
            data += conn.recv(n - len(data))
        req = json.loads(bytes(b ^ mask[i % 4] for i, b in enumerate(data)))
        self.requests.append(req)
        for msg in self.script(req):
            body = json.dumps(msg).encode()
            hdr = bytes([0x81]) + (bytes([len(body)]) if len(body) < 126
                                   else bytes([126]) + struct.pack("!H", len(body)))
            conn.sendall(hdr + body)
        conn.sendall(bytes([0x89, 2]) + b"hi")      # a ping, answered
        conn.close()

    def close(self):
        self.srv.close()


def test_probe_answer_from_log_event():
    def script(req):
        yield {"id": req["id"], "ok": True, "result": {"accepted": True}}
        yield {"event": "metrics", "data": {"snr_db": 3}}
        yield {"event": "log", "data": {"name": "probe",
               "detail": "KK4XYZ hears us at 7 dB, heard at 9 dB"}}
    api = FakeWSControl(script)
    try:
        text = AetherControlClient("127.0.0.1", api.port).probe("kk4xyz", timeout=5)
        assert text == "KK4XYZ hears us at 7 dB, heard at 9 dB"
        assert api.requests == [{"id": "probe", "method": "probe",
                                 "params": {"remote": "KK4XYZ"}}]
    finally:
        api.close()


def test_probe_refused():
    def script(req):
        yield {"id": req["id"], "ok": False, "error": {
            "code": "regulatory", "message": "Not on this dial.",
            "retryable": False}}
    api = FakeWSControl(script)
    try:
        with pytest.raises(AetherControlError) as ei:
            AetherControlClient("127.0.0.1", api.port).probe("W1ABC", timeout=5)
        assert ei.value.code == "regulatory"
    finally:
        api.close()


def test_update_required_is_called_out():
    from aether.monitor import problems
    msgs = problems({"mismatch": "W1ABC runs link protocol 6; update it.",
                     "mismatch_what": "version"})
    assert msgs == ["Update required — W1ABC runs link protocol 6; update it."]
    assert problems({"mismatch": "A 2300 Hz call to a 500 Hz station.",
                     "mismatch_what": "call"}) == ["A 2300 Hz call to a 500 Hz station."]
