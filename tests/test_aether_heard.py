"""
tests/test_aether_heard.py

The Aether HF stations-heard list on the dashboard: the monitor publishes
Aether's heard.list with SNR, and the dashboard table shows, sorts and
probes from it.  Runs on Qt's offscreen platform; no Aether HF needed.
"""

import json
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from core.events import EventBus, EventType
from aether.monitor import heard_rows
from tests.test_aether import FakeControlAPI

NOW = int(time.time() * 1000)   # for format_age only; station times use _heard()


def _heard():
    """Sample heard.list, timed from now (not from when the module loaded)."""
    now = int(time.time() * 1000)
    return [
        {"callsign": "kk4xyz-7", "first_heard_ms": now - 3_600_000,
         "last_heard_ms": now - 120_000, "count": 4, "snr_db": 6.24,
         "best_snr_db": 11.0, "frequency_hz": 14105000, "mode": 7,
         "activity": "connected", "detail": "", "connected": True,
         "beacons": 2, "last_beacon_ms": now - 900_000, "bandwidth_hz": 2300},
        {"callsign": "W1AW", "first_heard_ms": now - 30_000,
         "last_heard_ms": now - 5_000, "count": 1, "snr_db": -3.5,
         "best_snr_db": -3.5, "frequency_hz": None, "mode": 2,
         "activity": "calling", "detail": "N0CALL-10", "connected": False,
         "beacons": 0, "bandwidth_hz": None},
    ]


# ── monitor ────────────────────────────────────────────────────────────────

def test_heard_rows_keep_snr_and_use_aethers_words():
    rows = heard_rows(_heard() + [{"callsign": ""}])
    assert [r["callsign"] for r in rows] == ["KK4XYZ-7", "W1AW"]
    a, b = rows
    assert (a["snr_db"], a["best_snr_db"]) == (6.24, 11.0)
    assert a["activity"] == "session" and a["connected"] is True
    assert b["frequency_hz"] is None and b["detail"] == "N0CALL-10"


def test_monitor_publishes_heard_list_every_poll():
    from aether.control import AetherControlClient
    from aether.monitor import AetherMonitor
    api = FakeControlAPI()
    api.heard = _heard()
    bus = EventBus()
    got = []
    bus.subscribe(EventType.AETHER_HEARD, got.append)
    try:
        mon = AetherMonitor("HF", AetherControlClient("127.0.0.1", api.port), bus)
        mon.poll_heard()      # the priming poll publishes too
        mon.poll_heard()
        assert len(got) == 2 and got[0].source == "HF"
        assert got[1].data["stations"][1]["snr_db"] == -3.5
    finally:
        api.close()


# ── dashboard table ────────────────────────────────────────────────────────

QtWidgets = pytest.importorskip("PyQt6.QtWidgets")


@pytest.fixture(scope="module")
def qapp():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    yield app


def _panel(stations="sample", instance="HF"):
    from gui.aether_heard import AetherHeardPanel
    p = AetherHeardPanel()
    if stations == "sample":
        stations = _heard()
    if stations is not None:
        p.set_stations(instance, json.dumps({"stations": heard_rows(stations)}))
    return p


def _cells(panel):
    t = panel._table
    return [[t.item(r, c).text() for c in range(t.columnCount())]
            for r in range(t.rowCount())]


def test_formatting():
    from gui.aether_heard import format_age, format_dial, format_snr
    assert format_snr(6.24) == "+6.2 dB" and format_snr(-3.5) == "-3.5 dB"
    assert format_snr(None) == "—"
    assert format_dial(14105000) == "14.1050 MHz" and format_dial(None) == "—"
    assert format_age(NOW - 3_000, NOW) == "just now"
    assert format_age(NOW - 125_000, NOW) == "2 min ago"
    assert format_age(NOW - 7_200_000, NOW) == "2 h ago"
    assert format_age(0, NOW) == "—"


def test_empty_until_heard(qapp):
    p = _panel(stations=None)
    assert not p._empty.isHidden() and p._table.isHidden()


def test_table_shows_snr_newest_first(qapp):
    p = _panel()
    assert p._empty.isHidden() and not p._table.isHidden()
    rows = _cells(p)
    assert rows[0][:5] == ["W1AW", "-3.5 dB", "-3.5 dB", "just now", "calling → N0CALL-10"]
    assert rows[0][5:] == ["—", "—", "1"]
    assert rows[1][:5] == ["KK4XYZ-7", "+6.2 dB", "+11.0 dB", "2 min ago", "session"]
    assert rows[1][5:] == ["14.1050 MHz", "2300", "4"]
    assert p._table.columnCount() == 8                    # no Instance column


def test_sorting_by_snr_is_numeric(qapp):
    from gui.aether_heard import COLUMNS
    now = int(time.time() * 1000)
    p = _panel(_heard() + [{"callsign": "VE3ABC", "last_heard_ms": now - 60_000,
                         "snr_db": 10.0, "best_snr_db": 10.0},
                        {"callsign": "G4NOSNR", "last_heard_ms": now - 1_000}])
    from PyQt6.QtCore import Qt
    p._table.sortByColumn(COLUMNS.index("SNR"), Qt.SortOrder.DescendingOrder)
    assert [r[0] for r in _cells(p)] == ["VE3ABC", "KK4XYZ-7", "W1AW", "G4NOSNR"]
    p._render()                                           # a refresh keeps the sort
    assert [r[0] for r in _cells(p)] == ["VE3ABC", "KK4XYZ-7", "W1AW", "G4NOSNR"]


def test_two_instances_add_instance_column(qapp):
    p = _panel()
    p.set_stations("HF2", json.dumps({"stations": heard_rows(
        [{"callsign": "JA1XYZ", "last_heard_ms": int(time.time() * 1000),
          "snr_db": 1.0}])}))
    assert p._table.columnCount() == 9
    assert _cells(p)[0][0] == "JA1XYZ" and _cells(p)[0][8] == "HF2"
    p.keep_instances(["HF"])                              # HF2 removed in Settings
    assert p._table.columnCount() == 8
    assert "JA1XYZ" not in [r[0] for r in _cells(p)]


def test_probe_from_the_list(qapp):
    p = _panel()
    asked = []
    p.probe_requested.connect(lambda inst, call: asked.append((inst, call)))
    from PyQt6.QtCore import Qt
    item = p._table.item(1, 0)
    assert item.data(Qt.ItemDataRole.UserRole) == "HF"
    p.probe_requested.emit(item.data(Qt.ItemDataRole.UserRole), item.text())
    assert asked == [("HF", "KK4XYZ-7")]


def test_dashboard_shows_list_only_with_an_aether_instance(qapp, tmp_path):
    from core.config import ConfigManager
    from gui.modem_panel import ModemStatusPanel

    class IM:
        def get_status(self, name):
            return None

        def instance_names(self):
            return []

    def panel(toml):
        path = tmp_path / "c.toml"
        path.write_text(toml)
        return ModemStatusPanel(ConfigManager(path).load(), IM(), EventBus())

    vara = panel('[[instances]]\nname = "VHF"\ntransport = "VARA_FM"\n')
    assert vara._aether_heard.isHidden()

    aeth = panel('[[instances]]\nname = "HF"\ntransport = "AETHER_HF"\n')
    assert not aeth._aether_heard.isHidden()
    probed = []
    aeth._cards["HF"].probe = probed.append
    aeth._on_probe_requested("HF", "W1AW")
    assert probed == ["W1AW"]
