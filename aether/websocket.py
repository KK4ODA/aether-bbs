"""
aether/websocket.py

Just enough of a WebSocket client (RFC 6455, client side) for the Aether HF
control API: JSON text frames, pings answered, no extensions.  The control
API sends events — a probe's answer, for one — only over its WebSocket.
"""

import base64
import json
import os
import socket
import struct
from typing import Optional

from aether.control import API_VERSION, AetherControlError

CRLF = "\r\n"


class WebSocket:
    """One WebSocket to ``ws://host:port/v1``."""

    def __init__(self, host: str, port: int, token: str = "", timeout: float = 5.0):
        try:
            self._sock = socket.create_connection((host, port), timeout=timeout)
        except OSError as e:
            # callers (the instance card's Probe) handle AetherControlError
            raise AetherControlError(
                f"Aether HF control API not reachable at {host}:{port} ({e})",
                code="unreachable") from e
        try:
            self._handshake(host, port, token)
        except OSError as e:
            self.close()
            raise AetherControlError(
                f"Aether HF control API handshake failed ({e})",
                code="unreachable") from e

    def _handshake(self, host: str, port: int, token: str):
        key = base64.b64encode(os.urandom(16)).decode()
        lines = [
            f"GET /{API_VERSION} HTTP/1.1",
            f"Host: {host}:{port}",
            "Upgrade: websocket",
            "Connection: Upgrade",
            f"Sec-WebSocket-Key: {key}",
            "Sec-WebSocket-Version: 13",
        ]
        if token:
            lines.append(f"Authorization: Bearer {token}")
        self._sock.sendall((CRLF.join(lines) + CRLF + CRLF).encode())

        head = b""
        end = (CRLF + CRLF).encode()
        while end not in head:
            chunk = self._sock.recv(1024)
            if not chunk:
                break
            head += chunk
        status = head.split(CRLF.encode(), 1)[0]
        if b" 101 " not in status:
            self.close()
            code = "unauthorised" if b" 401 " in status else "ws_refused"
            raise AetherControlError(
                f"Aether HF control API refused the WebSocket "
                f"({status.decode(errors='replace')})", code=code)
        self._buf = head.split(end, 1)[1] if end in head else b""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        try:
            self._sock.close()
        except OSError:
            pass

    # ── Frames ────────────────────────────────────────────────────────────

    def _send_frame(self, opcode: int, payload: bytes):
        head = bytes([0x80 | opcode])
        n = len(payload)
        if n < 126:
            head += bytes([0x80 | n])
        elif n < 65536:
            head += bytes([0x80 | 126]) + struct.pack("!H", n)
        else:
            head += bytes([0x80 | 127]) + struct.pack("!Q", n)
        mask = os.urandom(4)                     # clients always mask
        body = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self._sock.sendall(head + mask + body)

    def send_json(self, obj: dict):
        self._send_frame(0x1, json.dumps(obj).encode("utf-8"))

    def _read(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self._sock.recv(65536)
            if not chunk:
                raise ConnectionError("WebSocket closed")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def recv_json(self, timeout: float) -> Optional[dict]:
        """The next JSON message, or None on timeout or close."""
        self._sock.settimeout(max(0.1, timeout))
        try:
            while True:
                b0, b1 = self._read(2)
                opcode, n = b0 & 0x0F, b1 & 0x7F
                if n == 126:
                    n = struct.unpack("!H", self._read(2))[0]
                elif n == 127:
                    n = struct.unpack("!Q", self._read(8))[0]
                mask = self._read(4) if b1 & 0x80 else None
                payload = self._read(n)
                if mask:
                    payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
                if opcode == 0x8:                # close
                    return None
                if opcode == 0x9:                # ping
                    self._send_frame(0xA, payload)
                    continue
                if opcode in (0x0, 0x1):
                    try:
                        return json.loads(payload.decode("utf-8"))
                    except ValueError:
                        continue
        except (socket.timeout, ConnectionError, OSError):
            return None
