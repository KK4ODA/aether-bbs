"""
tests/test_aprs_gateway.py

The APRS-IS connection (aprs/gateway.py) against a fake APRS-IS server:
a quiet but healthy link stays up, and a dropped one is replaced by exactly
one new connection.
"""

import socket
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import aprs.gateway as gw
from aprs.gateway import APRSGateway
from core.config import ConfigManager
from core.events import EventBus, EventType


class FakeAPRSIS:
    """Accepts logins, answers logresp, then stays quiet."""

    def __init__(self):
        self.srv = socket.socket()
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(5)
        self.port = self.srv.getsockname()[1]
        self.clients: list[socket.socket] = []
        self.lines: list[str] = []
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            try:
                c, _ = self.srv.accept()
            except OSError:
                return
            self.clients.append(c)
            c.sendall(b"# aprsc fake\r\n")
            threading.Thread(target=self._serve, args=(c,), daemon=True).start()

    def _serve(self, c):
        buf = b""
        while True:
            try:
                d = c.recv(1024)
            except OSError:
                return
            if not d:
                return
            buf += d
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                text = line.decode(errors="replace").strip()
                self.lines.append(text)
                if text.startswith("user "):
                    c.sendall(b"# logresp N0CALL-10 verified, server FAKE\r\n")

    def drop_all(self):
        for c in self.clients:
            try:
                c.shutdown(socket.SHUT_RDWR)
                c.close()
            except OSError:
                pass

    def close(self):
        self.drop_all()
        self.srv.close()


@pytest.fixture
def gateway(tmp_path, monkeypatch):
    monkeypatch.setattr(gw, "RX_TIMEOUT", 0.3)
    monkeypatch.setattr(gw, "RECONNECT_BASE", 0.5)
    server = FakeAPRSIS()
    path = tmp_path / "c.toml"
    path.write_text(
        f'[aprs]\nenabled = true\nis_server = "127.0.0.1"\nis_port = {server.port}\n'
        '[station]\nbeacon_interval = 0\n')
    cm = ConfigManager(path)
    cm.load()
    bus = EventBus()
    events = []
    bus.subscribe(EventType.APRS_IS_CONNECTED, events.append)
    bus.subscribe(EventType.APRS_IS_DISCONNECTED, events.append)
    g = APRSGateway(cm, bus)
    yield g, server, events
    g.stop()
    server.close()


def _wait(cond, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


def _rx_threads():
    return [t for t in threading.enumerate() if t.name == "APRS-IS-RX" and t.is_alive()]


def test_quiet_link_stays_up_with_keepalives(gateway):
    g, server, events = gateway
    g.start()
    assert _wait(lambda: g.is_connected)
    time.sleep(2.0)                      # several RX timeouts with no traffic
    assert g.is_connected
    assert len(server.clients) == 1      # never reconnected
    assert [e.type for e in events] == [EventType.APRS_IS_CONNECTED]
    assert any(line == "#keepalive" for line in server.lines)


def test_dropped_link_is_replaced_by_one_connection(gateway):
    g, server, events = gateway
    g.start()
    assert _wait(lambda: g.is_connected)
    server.drop_all()
    assert _wait(lambda: len(server.clients) == 2, timeout=8)
    assert _wait(lambda: g.is_connected)
    time.sleep(1.0)
    assert len(server.clients) == 2
    assert len(_rx_threads()) == 1
    assert [e.type for e in events] == [EventType.APRS_IS_CONNECTED,
                                        EventType.APRS_IS_DISCONNECTED,
                                        EventType.APRS_IS_CONNECTED]
