"""
tests/test_rf_forwarding.py

Bulletin forwarding on the air: BBS A calls neighbour B through a modem
instance and they exchange bulletins with the FWD protocol.  The modem is
a fake speaking the VARA host interface; it answers CONNECT and bridges the
data stream to B's forwarding server, as the air would.
"""

import queue
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))

from test_aether import FakeAetherHost, _free_port_pair, _wait  # noqa: E402

from core.config import AppConfig, ForwardingNeighbourConfig, InstanceConfig
from core.events import EventBus


def _stores(tmp_path, name):
    from bbs.bulletins import BulletinStore
    from bbs.store import MessageStore
    cfg = AppConfig()
    cfg.bbs.message_dir = str(tmp_path / name)
    store = MessageStore(cfg)
    store.initialize()
    return cfg, store, BulletinStore(store)


class FakeModemWithPeer(FakeAetherHost):
    """Answers CONNECT and runs the called BBS's forwarding server."""

    def __init__(self, port, peer_cfg, peer_store, peer_bbs, answer=True):
        self.peer = (peer_cfg, peer_store, peer_bbs)
        self.answer = answer
        self.to_peer: queue.Queue = queue.Queue()
        self.peer_done = threading.Event()
        self.peer_result = None
        super().__init__(port)

    def _accept(self):
        # as FakeAetherHost, with CONNECT / DISCONNECT handled
        self.cmd_conn, _ = self._cmd.accept()
        self.data_conn, _ = self._data.accept()
        threading.Thread(target=self._read_data, daemon=True).start()
        buf = b""
        while True:
            try:
                chunk = self.cmd_conn.recv(1024)
            except OSError:
                return
            if not chunk:
                return
            buf += chunk
            while b"\r" in buf:
                line, buf = buf.split(b"\r", 1)
                line = line.strip().decode()
                if not line:
                    continue
                self.commands.append(line)
                self.say("OK")
                up = line.upper()
                if up.startswith("CONNECT ") and self.answer:
                    me, them = line.split()[1:3]
                    self.say(f"CONNECTED {me} {them} 2300")
                    threading.Thread(target=self._run_peer, args=(me,),
                                     daemon=True).start()
                elif up in ("DISCONNECT", "ABORT"):
                    self.say("DISCONNECTED")

    def _read_data(self):
        buf = b""
        while True:
            try:
                d = self.data_conn.recv(4096)
            except OSError:
                return
            if not d:
                return
            buf += d
            while b"\r" in buf:
                line, buf = buf.split(b"\r", 1)
                self.to_peer.put(line.decode())

    def _run_peer(self, caller):
        from bbs.bulletins import BulletinForwardingServer
        cfg, store, bbs = self.peer
        server = BulletinForwardingServer(cfg, store, bbs, EventBus())

        def recv():
            try:
                return self.to_peer.get(timeout=10)
            except queue.Empty:
                return None

        self.peer_result = server.serve(
            send=lambda line: self.send_data((line + "\r").encode()),
            recv=recv, expected_call=caller)
        self.peer_done.set()


class _IM:
    def __init__(self, mux):
        self.mux = mux

    def get_mux(self, name):
        return self.mux if name == "HF" else None


def _setup(tmp_path, answer=True):
    from bbs.bulletins import BulletinForwarder
    from vara.broker import TNCBroker

    a_cfg, a_store, a_bbs = _stores(tmp_path, "a")
    a_cfg.station.bbs_callsign = "W1AAA-10"
    b_cfg, b_store, b_bbs = _stores(tmp_path, "b")
    b_cfg.station.bbs_callsign = "W2BBB-10"

    port = _free_port_pair()
    modem = FakeModemWithPeer(port, b_cfg, b_store, b_bbs, answer=answer)
    inst = InstanceConfig(name="HF", transport="AETHER_HF",
                          cmd_port=port, data_port=port + 1)
    broker = TNCBroker(inst, a_cfg, EventBus())
    threading.Thread(target=broker.connect, daemon=True).start()
    assert _wait(lambda: "LISTEN ON" in modem.commands, 20)

    fwd = BulletinForwarder(a_cfg, a_store, a_bbs, EventBus())
    fwd.set_instance_manager(_IM(broker))
    nbr = ForwardingNeighbourConfig(bbs_callsign="W2BBB-10", via="RF",
                                    instance="HF")
    return fwd, nbr, broker, modem, (a_store, a_bbs), (b_store, b_bbs)


def test_bulletins_cross_both_ways_over_rf(tmp_path):
    fwd, nbr, broker, modem, (a_store, a_bbs), (b_store, b_bbs) = _setup(tmp_path)
    try:
        a_mid = a_bbs.save_bulletin("W1AAA", "ALL", "from A", b"hello B",
                                    area="ALL")
        b_mid = b_bbs.save_bulletin("W2BBB", "WX", "from B", b"rain at B",
                                    area="WX", is_urgent=True)

        fwd._forward_with(nbr)

        assert modem.peer_done.wait(10) and modem.peer_result is True
        assert "CONNECT W1AAA-10 W2BBB-10" in modem.commands
        assert _wait(lambda: "DISCONNECT" in modem.commands, 5)
        assert b_store.has_message(a_mid)
        assert a_store.has_message(b_mid)
        got = [b for b in a_bbs.list_bulletins() if b["id"] == b_mid][0]
        assert got["subject"] == "from B"
        assert not broker.outbound_active
    finally:
        broker.disconnect()
        modem.close()


def test_unanswered_call_is_aborted_and_released(tmp_path, monkeypatch):
    import bbs.bulletins as bulletins
    monkeypatch.setattr(bulletins, "RF_CONNECT_TIMEOUT", 1.0)
    fwd, nbr, broker, modem, *_ = _setup(tmp_path, answer=False)
    try:
        fwd._forward_with(nbr)
        assert _wait(lambda: "ABORT" in modem.commands, 5)
        assert not broker.outbound_active
        assert nbr._last_forwarded_at > 0      # not retried every minute
    finally:
        broker.disconnect()
        modem.close()


def test_refused_call_ends_without_waiting(tmp_path):
    fwd, nbr, broker, modem, *_ = _setup(tmp_path, answer=False)
    orig = modem.say

    def say(line):                      # the modem refuses: OK, then DISCONNECTED
        orig(line)
        if line == "OK" and modem.commands[-1].upper().startswith("CONNECT "):
            orig("DISCONNECTED")
    modem.say = say
    try:
        t0 = time.time()
        fwd._forward_with(nbr)
        assert time.time() - t0 < 20       # not RF_CONNECT_TIMEOUT (180 s)
        assert not broker.outbound_active
    finally:
        broker.disconnect()
        modem.close()


def test_neighbour_matching():
    n = ForwardingNeighbourConfig(bbs_callsign="W2BBB-10", via="RF")
    assert n.matches("w2bbb-10") and not n.matches("W2BBB-1")
    assert ForwardingNeighbourConfig(bbs_callsign="W2BBB").matches("W2BBB-7")
    with pytest.raises(Exception):
        ForwardingNeighbourConfig(via="AIR")


def test_server_refuses_hello_from_another_bbs(tmp_path):
    from bbs.bulletins import BulletinForwardingServer
    cfg, store, bbs = _stores(tmp_path, "s")
    lines = iter(["FWD HELLO K9XYZ-10"])
    sent = []
    ok = BulletinForwardingServer(cfg, store, bbs, EventBus()).serve(
        send=sent.append, recv=lambda: next(lines, None),
        expected_call="W2BBB-10")
    assert ok is False and sent == []


def test_bulletins_cross_both_ways_over_tcp(tmp_path):
    """The same protocol over TCP (it shared the RF path's parsing bugs)."""
    import socket
    from bbs.bulletins import BulletinForwarder, BulletinForwardingServer

    a_cfg, a_store, a_bbs = _stores(tmp_path, "a")
    a_cfg.station.bbs_callsign = "W1AAA-10"
    b_cfg, b_store, b_bbs = _stores(tmp_path, "b")
    b_cfg.station.bbs_callsign = "W2BBB-10"
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    b_cfg.bbs.forwarding.server_enabled = True
    b_cfg.bbs.forwarding.server_port = port
    b_cfg.bbs.forwarding.server_bind = "127.0.0.1"
    server = BulletinForwardingServer(b_cfg, b_store, b_bbs, EventBus())
    server.start()
    try:
        a_mid = a_bbs.save_bulletin("W1AAA", "ALL", "from A", b"x", area="ALL")
        b_mid = b_bbs.save_bulletin("W2BBB", "WX", "from B", b"y", area="WX")
        nbr = ForwardingNeighbourConfig(bbs_callsign="W2BBB-10",
                                        host="127.0.0.1", port=port)
        BulletinForwarder(a_cfg, a_store, a_bbs, EventBus())._forward_with(nbr)
        assert _wait(lambda: b_store.has_message(a_mid), 5)
        assert a_store.has_message(b_mid)
    finally:
        server.stop()


def test_dispatcher_routes_only_rf_neighbours():
    from bbs.dispatcher import SessionDispatcher

    class _Stub:
        _live_config = AppConfig()
    stub = _Stub()
    stub._live_config.bbs.forwarding_neighbours = [
        ForwardingNeighbourConfig(bbs_callsign="W2BBB-10", via="RF"),
        ForwardingNeighbourConfig(bbs_callsign="K9TCP-10", via="TCP"),
        ForwardingNeighbourConfig(bbs_callsign="N0OFF-10", via="RF", enabled=False),
    ]
    pick = lambda c: SessionDispatcher._rf_neighbour(stub, c)
    assert pick("W2BBB-10").bbs_callsign == "W2BBB-10"
    assert pick("K9TCP-10") is None       # TCP neighbours log in like users
    assert pick("N0OFF-10") is None
    assert pick("W2BBB-1") is None


def _scripted(lines):
    """recv() returning *lines* then None; send() collecting into a list."""
    it = iter(lines)
    sent: list[str] = []
    return sent.append, (lambda: next(it, None)), sent


def test_server_never_forwards_a_private_message(tmp_path):
    from bbs.bulletins import BulletinForwardingServer
    cfg, store, bbs = _stores(tmp_path, "s")
    private = store.save_message("W1AAA", "N0VIC", "private", b"TOP SECRET")
    send, recv, sent = _scripted(["FWD HELLO W2BBB-10", f"FWD GET {private}"])
    BulletinForwardingServer(cfg, store, bbs, EventBus()).serve(send=send, recv=recv)
    assert f"FWD NAK {private}" in sent
    assert not any(s.startswith("FWD DATA") or "TOP SECRET" in s for s in sent)


def test_unacknowledged_bulletin_is_offered_again(tmp_path):
    from bbs.bulletins import BulletinForwarder
    cfg, store, bbs = _stores(tmp_path, "a")
    cfg.station.bbs_callsign = "W1AAA-10"
    mid = bbs.save_bulletin("W1AAA", "ALL", "news", b"body", area="ALL")
    nbr = ForwardingNeighbourConfig(bbs_callsign="W2BBB-10")
    # the neighbour asks for it, then the link drops before its ACK
    send, recv, sent = _scripted(["FWD HELLO W2BBB-10", f"FWD GET {mid}"])
    BulletinForwarder(cfg, store, bbs, EventBus())._exchange(send, recv, nbr)
    assert any(s.startswith(f"FWD DATA {mid}") for s in sent)
    assert [r["id"] for r in bbs.get_unsent_bulletins_for("W2BBB-10")] == [mid]


def test_bulletin_cut_off_mid_body_is_not_stored(tmp_path):
    from bbs.bulletins import BulletinForwarder
    cfg, store, bbs = _stores(tmp_path, "b")
    nbr = ForwardingNeighbourConfig(bbs_callsign="W1AAA-10")
    send, recv, sent = _scripted([
        "FWD DATA MID0001 W1AAA ALL - news", "FWD BODY", "first line"])  # no FWD END
    BulletinForwarder(cfg, store, bbs, EventBus())._receive_bulletin(send, recv, "MID0001", nbr)
    assert not store.has_message("MID0001")
    assert not any(s.startswith("FWD ACK") for s in sent)
