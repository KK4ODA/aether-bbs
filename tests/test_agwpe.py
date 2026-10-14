"""
tests/test_agwpe.py

The AGWPE (Soundmodem/Direwolf) link: a dropped link ends the packet
session on it, shows the link down, and comes back by itself.  The
AGWPE server is faked over TCP.
"""

import socket
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.config import AppConfig, InstanceConfig
from core.events import EventBus, EventType
from core.reconnect import Backoff, retry
from transport import agwpe_broker as ab
from transport.agwpe import AGWPEFrame, HEADER_SIZE, decode_frame, encode_frame
from transport.protocol import TransportState


def _wait(cond, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.02)
    return False


class FakeAGWPE:
    """An AGWPE server: records the frames each client sends, can send
    frames to the newest client and drop every connection."""

    def __init__(self):
        self.srv = socket.socket()
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(5)
        self.port = self.srv.getsockname()[1]
        self.conns: list[socket.socket] = []
        self.frames: list[AGWPEFrame] = []
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            try:
                c, _ = self.srv.accept()
            except OSError:
                return
            self.conns.append(c)
            threading.Thread(target=self._read, args=(c,), daemon=True).start()

    def _read(self, c):
        while True:
            try:
                hdr = c.recv(HEADER_SIZE, socket.MSG_WAITALL)
                if len(hdr) < HEADER_SIZE:
                    return
                n = int.from_bytes(hdr[28:32], "little")
                data = c.recv(n, socket.MSG_WAITALL) if n else b""
                self.frames.append(decode_frame(hdr, data))
            except OSError:
                return

    def send(self, kind, call_from, call_to="N0BBS", data=b""):
        self.conns[-1].sendall(encode_frame(AGWPEFrame(
            port=0, data_kind=kind, pid=0xF0, call_from=call_from,
            call_to=call_to, data=data)))

    def drop(self):
        for c in self.conns:
            try:
                c.shutdown(socket.SHUT_RDWR)
                c.close()
            except OSError:
                pass

    def registrations(self):
        return [f for f in self.frames if f.data_kind == "X"]

    def close(self):
        self.drop()
        try:
            self.srv.shutdown(socket.SHUT_RDWR)   # wakes the blocked accept()
        except OSError:
            pass
        self.srv.close()


class Handler:
    def __init__(self):
        self.events = []

    def on_cmd_event(self, event):
        self.events.append(event)

    def on_data(self, data):
        pass


@pytest.fixture
def link(monkeypatch):
    monkeypatch.setattr(ab, "RECONNECT_DELAY", 0.2)
    monkeypatch.setattr(ab, "RECONNECT_MAX", 0.4, raising=False)
    server = FakeAGWPE()
    cfg = AppConfig()
    cfg.station.bbs_callsign = "N0BBS"
    inst = InstanceConfig(name="PKT", transport="SOUNDMODEM")
    inst.soundmodem.host, inst.soundmodem.port = "127.0.0.1", server.port
    bus = EventBus()
    seen = []
    for et in (EventType.VARA_CONNECTED, EventType.VARA_DISCONNECTED,
               EventType.SESSION_CONNECTED, EventType.SESSION_DISCONNECTED):
        bus.subscribe(et, lambda e, et=et: seen.append(et))
    broker = ab.AGWPEBroker(inst, cfg, bus)
    broker.connect()
    assert _wait(lambda: len(server.registrations()) == 1)
    yield broker, server, seen
    broker.disconnect()
    server.close()


def test_dropped_link_ends_the_session_and_reconnects(link):
    """A dropped AGWPE link used to go unnoticed: the packet session hung
    until a timeout, the card showed the link up, and only Restart got
    it back."""
    broker, server, seen = link
    handler = Handler()
    assert broker.acquire_session(handler)
    server.send("C", "W1XYZ")
    assert _wait(lambda: broker.state == TransportState.CONNECTED)

    server.drop()                                   # Soundmodem closes
    assert _wait(lambda: "DISCONNECTED" in handler.events)
    assert EventType.SESSION_DISCONNECTED in seen
    assert EventType.VARA_DISCONNECTED in seen
    assert broker.remote_call is None

    broker.release_session(handler)                 # the session ends …
    assert broker.state != TransportState.IDLE      # … the link is still down

    # … and comes back by itself, with the callsign registered again
    assert _wait(lambda: len(server.registrations()) == 2)
    assert _wait(lambda: broker.state == TransportState.IDLE)
    assert _wait(lambda: seen.count(EventType.VARA_CONNECTED) == 2)  # + the reconnect
    assert len([t for t in threading.enumerate()
                if t.name == "AGWPE-Reader" and t.is_alive()]) == 1


def test_reconnect_keeps_trying_until_the_server_is_back(link):
    broker, server, _ = link
    port = server.port
    server.close()                                  # Soundmodem quits
    assert _wait(lambda: broker.state in (TransportState.CONNECTING,
                                          TransportState.DISCONNECTED))
    time.sleep(1.0)                                 # several failed tries
    again = socket.socket()
    again.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    again.bind(("127.0.0.1", port))
    again.listen(1)
    try:
        again.settimeout(5)
        conn, _ = again.accept()                    # it comes back
        conn.close()
    finally:
        again.close()


def test_stopping_the_instance_does_not_reconnect(link):
    broker, server, seen = link
    broker.disconnect()
    time.sleep(0.8)
    assert len(server.conns) == 1
    assert broker.state == TransportState.DISCONNECTED


def test_retry_waits_with_backoff_and_stops():
    waits, calls = [], []
    ok = retry(lambda: calls.append(1) or len(calls) == 4,
               Backoff(5, 20), wait=waits.append, wait_first=True)
    assert ok and len(calls) == 4 and waits == [5, 10, 20, 20]
    waits.clear()
    assert not retry(lambda: False, Backoff(1, 8), wait=waits.append,
                     max_attempts=3)
    assert waits == [1, 2]                          # no wait after the last
    assert not retry(lambda: pytest.fail("ran"), Backoff(1, 1),
                     should_stop=lambda: True)


def test_frame_header_is_the_36_byte_agwpe_layout():
    """The header was packed as 40 bytes (port as 4 + 4 reserved), so every
    frame put its kind at the wrong offset and nothing decoded."""
    raw = encode_frame(AGWPEFrame(port=1, data_kind="D", pid=0xF0,
                                  call_from="N0BBS-1", call_to="W1XYZ",
                                  data=b"hi"))
    assert len(raw) == HEADER_SIZE + 2 == 38
    assert raw[0] == 1 and raw[4:5] == b"D" and raw[6] == 0xF0
    assert raw[8:15] == b"N0BBS-1" and raw[18:23] == b"W1XYZ"
    assert int.from_bytes(raw[28:32], "little") == 2
    back = decode_frame(raw[:HEADER_SIZE], raw[HEADER_SIZE:])
    assert (back.data_kind, back.call_from, back.data) == ("D", "N0BBS-1", b"hi")
