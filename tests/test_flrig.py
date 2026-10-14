"""
tests/test_flrig.py

FLrig PTT client (vara/flrig.py) against a fake FLrig XML-RPC server:
connecting (with its three tries), keying and unkeying.
"""

import socket
import sys
import threading
from pathlib import Path
from xmlrpc.server import SimpleXMLRPCServer

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import core.reconnect
from vara.flrig import FLrigClient


class FakeFLrig:
    def __init__(self, fail_first=0):
        self.ptt = 0
        self.fail_first = fail_first
        self.srv = SimpleXMLRPCServer(("127.0.0.1", 0), logRequests=False,
                                      allow_none=True)
        self.port = self.srv.server_address[1]
        self.srv.register_function(self.get_version, "main.get_version")
        self.srv.register_function(lambda: "14105000", "rig.get_vfo")
        self.srv.register_function(self.set_ptt, "rig.set_ptt")
        self.srv.register_function(lambda: self.ptt, "rig.get_ptt")
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def get_version(self):
        if self.fail_first:
            self.fail_first -= 1
            raise RuntimeError("starting up")
        return "2.0.0"

    def set_ptt(self, on):
        self.ptt = int(on)
        return 0

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


@pytest.fixture(autouse=True)
def no_waits(monkeypatch):
    waits = []
    monkeypatch.setattr(core.reconnect.time, "sleep", waits.append)
    return waits


def test_connects_and_keys_the_radio(no_waits):
    rig = FakeFLrig(fail_first=2)              # FLrig answers on the third try
    client = FLrigClient("127.0.0.1", rig.port)
    try:
        client.start()
        assert client.version == "2.0.0" and no_waits == [1.0, 1.0]
        assert client.set_ptt(True) and rig.ptt == 1
        assert client.set_ptt(False) and rig.ptt == 0
    finally:
        client.stop()
        rig.close()


def test_gives_up_after_three_tries(no_waits):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]              # nothing listening
    client = FLrigClient("127.0.0.1", port)
    with pytest.raises(RuntimeError, match="Is FLrig running"):
        client.start()
    assert no_waits == [1.0, 1.0]


def test_hamlib_resends_a_ptt_command_lost_with_the_link():
    """When rigctld dropped the link, the PTT command was lost (only a
    reconnect followed): a lost "T 0" left the radio keyed."""
    from vara.ptt import HamlibDriver
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(2)
    got = []

    def rigctld():
        first, _ = srv.accept()
        first.recv(64)                         # "T 1" answered …
        first.sendall(b"RPRT 0\n")
        first.close()                          # … then rigctld restarts
        again, _ = srv.accept()
        got.append(again.recv(64))
        again.sendall(b"RPRT 0\n")

    threading.Thread(target=rigctld, daemon=True).start()
    drv = HamlibDriver("127.0.0.1", srv.getsockname()[1])
    drv.open()
    drv.set_ptt(True)
    drv.set_ptt(False)                         # fails on the dead link
    srv.close()
    assert got == [b"T 0\n"]                   # sent again after reconnecting
