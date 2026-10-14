"""
tests/test_vara_interface.py

The VARA host interface (vara/interface.py) against a simulated modem
(tests/vara_simulator.py): init commands, an inbound RF session, BUSY and
PTT, and the session ending.  No VARA or radio needed.
"""

import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.config import InstanceConfig
from core.events import EventBus, EventType
from tests.test_aether import _free_port_pair
from tests.vara_simulator import VARAFMSimulator
from vara.interface import VARAFMInterfaceWithCall, VARAFMState


def _wait(cond, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.05)
    return False


def _start_simulator():
    """A running simulator on a free port pair.  A port found free can be
    taken before the simulator binds it (Windows then says WinError 10013),
    so try a fresh pair."""
    for _ in range(10):
        port = _free_port_pair()
        sim = VARAFMSimulator(port, port + 1)
        try:
            sim.start()
            return port, sim
        except OSError:
            sim.stop()
    raise RuntimeError("could not start the VARA simulator")


@pytest.fixture
def link():
    """An interface attached to a simulated modem; yields (sim, iface, events)."""
    port, sim = _start_simulator()
    bus = EventBus()
    events = []
    for et in (EventType.SESSION_CONNECTED, EventType.SESSION_DISCONNECTED,
               EventType.CHANNEL_BUSY, EventType.PTT_CHANGE):
        bus.subscribe(et, events.append)
    cfg = InstanceConfig(name="Test", cmd_port=port, data_port=port + 1,
                         executable_path="", auto_start=False)
    iface = VARAFMInterfaceWithCall(cfg, bus, "W1TEST-10")
    threading.Thread(target=iface.connect, daemon=True).start()
    assert sim.wait_for_client(timeout=5.0), "interface did not connect"
    yield sim, iface, events
    iface.disconnect()
    sim.stop()


def _of(events, et):
    return [e for e in events if e.type == et]


def test_mycall_and_listen_sent_on_init(link):
    sim, _iface, _ = link
    mycall = sim.wait_for_command("MYCALL")
    assert mycall is not None and "W1TEST-10" in mycall
    assert sim.wait_for_command("LISTEN") is not None


def test_inbound_session_reports_the_caller(link):
    sim, iface, events = link
    sim.wait_for_command("LISTEN")
    sim.simulate_connect("W9XYZ")
    assert _wait(lambda: _of(events, EventType.SESSION_CONNECTED))
    assert _of(events, EventType.SESSION_CONNECTED)[0].data["remote_call"] == "W9XYZ"
    assert iface.remote_call == "W9XYZ"
    assert iface.state == VARAFMState.CONNECTED


def test_busy_and_ptt_are_reported(link):
    sim, _iface, events = link
    sim.wait_for_command("LISTEN")
    sim.simulate_busy(True)
    sim.simulate_ptt(True)
    sim.simulate_ptt(False)
    sim.simulate_busy(False)
    assert _wait(lambda: len(_of(events, EventType.PTT_CHANGE)) == 2
                 and len(_of(events, EventType.CHANNEL_BUSY)) == 2)
    assert [e.data["busy"] for e in _of(events, EventType.CHANNEL_BUSY)] == [True, False]
    assert [e.data["ptt"] for e in _of(events, EventType.PTT_CHANGE)] == [True, False]


def test_session_end_returns_to_listening(link):
    sim, iface, events = link
    sim.wait_for_command("LISTEN")
    sim.simulate_connect("W9XYZ")
    assert _wait(lambda: iface.state == VARAFMState.CONNECTED)
    sim.simulate_disconnect()
    assert _wait(lambda: _of(events, EventType.SESSION_DISCONNECTED))
    assert iface.state == VARAFMState.LISTENING
    assert iface.remote_call is None


def test_quiet_command_port_keeps_the_link(monkeypatch):
    """VARA sends IAMALIVE about once a minute, so the command port is often
    quiet for longer than the socket's poll timeout.  That must not count as
    a lost link (it used to drop and re-init the modem every ~35 s)."""
    import vara.interface as vi
    monkeypatch.setattr(vi, "SOCKET_POLL_TIMEOUT", 0.5)
    port, sim = _start_simulator()
    bus = EventBus()
    lost = []
    bus.subscribe(EventType.VARA_DISCONNECTED, lost.append)
    cfg = InstanceConfig(name="Quiet", cmd_port=port, data_port=port + 1,
                         executable_path="", auto_start=False)
    iface = VARAFMInterfaceWithCall(cfg, bus, "W1TEST-10")
    threading.Thread(target=iface.connect, daemon=True).start()
    try:
        assert sim.wait_for_client(timeout=5.0)
        assert sim.wait_for_command("LISTEN") is not None
        mycalls = len([c for c in sim.get_received_commands() if c.startswith("MYCALL")])
        time.sleep(3.0)                      # six poll timeouts with no traffic
        assert lost == []
        assert iface.state == VARAFMState.LISTENING
        # no re-init happened either
        assert len([c for c in sim.get_received_commands()
                    if c.startswith("MYCALL")]) == mycalls
    finally:
        iface.disconnect()
        sim.stop()


def _threads(prefix):
    return [t for t in threading.enumerate() if t.name == prefix and t.is_alive()]


def test_reconnect_does_not_leave_old_io_threads(monkeypatch):
    """After the modem drops and the BBS reconnects there is one reader and
    one writer per socket, not two writers draining the same TX queue."""
    import vara.interface as vi
    monkeypatch.setattr(vi, "SOCKET_POLL_TIMEOUT", 0.3)
    port, sim = _start_simulator()
    bus = EventBus()
    cfg = InstanceConfig(name="Rc", cmd_port=port, data_port=port + 1,
                         executable_path="", auto_start=False)
    iface = VARAFMInterfaceWithCall(cfg, bus, "W1TEST-10")
    try:
        threading.Thread(target=iface.connect, daemon=True).start()
        assert sim.wait_for_client(timeout=5.0)
        sim.wait_for_command("LISTEN")
        # the modem drops the TCP link
        import socket as _socket
        for s in (sim._cmd_client, sim._data_client):
            s.shutdown(_socket.SHUT_RDWR)
            s.close()
        assert _wait(lambda: iface.state == VARAFMState.DISCONNECTED)
        sim._cmd_client = sim._data_client = None
        threading.Thread(target=iface.connect, daemon=True).start()
        assert sim.wait_for_client(timeout=5.0)
        assert _wait(lambda: iface.state != VARAFMState.DISCONNECTED)
        time.sleep(1.0)   # let any stale thread notice its socket is gone
        assert len(_threads("DataTx-Rc")) == 1
        assert len(_threads("DataRx-Rc")) == 1
        assert len(_threads("CmdRx-Rc")) == 1
    finally:
        iface.disconnect()
        sim.stop()


class _FakeDriver:
    def __init__(self):
        self.calls = []

    def set_ptt(self, on):
        self.calls.append(on)

    def description(self):
        return "fake"


def test_ptt_released_when_modem_link_drops():
    from vara.broker import TNCBroker
    from vara.ptt import PTTController
    driver = _FakeDriver()
    ptt = PTTController("HF", driver, EventBus(), watchdog_timeout=90)
    ptt._ptt_on = True                       # keyed when the link dropped
    broker = TNCBroker.__new__(TNCBroker)
    broker.cfg = InstanceConfig(name="HF")
    broker._rig_control = None
    broker._ptt = ptt
    broker._retired = True                   # skip the reconnect thread
    broker._on_vara_disconnected(type("E", (), {"source": "HF"})())
    assert driver.calls == [False] and ptt._ptt_on is False
    ptt.release("again")                     # unkeyed already: nothing to do
    assert driver.calls == [False]


def test_reconnect_keeps_trying_until_the_modem_is_back(monkeypatch):
    import vara.broker as vb
    from vara.broker import TNCBroker
    sleeps = []
    monkeypatch.setattr(vb.time, "sleep", sleeps.append)

    class Iface:
        attempts = 0
        state = VARAFMState.DISCONNECTED

        def connect(self):
            Iface.attempts += 1
            if Iface.attempts == 4:
                Iface.state = VARAFMState.LISTENING

    broker = TNCBroker.__new__(TNCBroker)
    broker.cfg = InstanceConfig(name="HF")
    broker._retired = False
    broker._interface = Iface()
    relays = []
    broker._start_data_relay = lambda: relays.append(1)
    broker._reconnect_loop()
    assert Iface.attempts == 4 and relays == [1]
    assert sleeps == [5.0, 10.0, 20.0, 40.0]


def test_start_on_an_attached_soundmodem_instance_does_nothing(monkeypatch):
    """Pressing Start on a running Soundmodem instance used to open a second
    AGWPE connection (and launch a second Soundmodem) beside the first."""
    from vara.instance_manager import InstanceStatus, ModemInstanceManager
    launched = []
    monkeypatch.setattr("subprocess.Popen", lambda *a, **k: launched.append(a))
    cfg = InstanceConfig(name="Pkt", transport="SOUNDMODEM")
    cfg.soundmodem.auto_launch = True
    cfg.soundmodem.exe_path = __file__          # an existing file
    status = InstanceStatus(cfg)
    attached = type("Broker", (), {"state": type("S", (), {"name": "IDLE"})()})()
    status.mux = attached
    mgr = ModemInstanceManager.__new__(ModemInstanceManager)
    mgr._start_soundmodem_instance(status)
    assert status.mux is attached and launched == []
