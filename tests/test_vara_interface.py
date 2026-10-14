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


@pytest.fixture
def link():
    """An interface attached to a simulated modem; yields (sim, iface, events)."""
    port = _free_port_pair()
    sim = VARAFMSimulator(port, port + 1)
    sim.start()
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
    port = _free_port_pair()
    sim = VARAFMSimulator(port, port + 1)
    sim.start()
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
