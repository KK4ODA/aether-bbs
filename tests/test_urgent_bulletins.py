"""Urgent bulletins: who gets alerted, and what the Post tab tells the sysop."""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from core.config import AppConfig, NotificationConfig
from core.events import EventBus, EventType
from notifications import SYSOP_GUI, NotificationRouter


def test_only_emcomm_is_an_urgent_area_by_default():
    assert NotificationConfig().urgent_bulletin_areas == ["EMCOMM"]


def _router(areas):
    cfg = AppConfig()
    cfg.notifications.enabled = True
    cfg.notifications.urgent_bulletin_areas = areas
    bus = EventBus()
    NotificationRouter(cfg, bus)
    alerts = []
    bus.subscribe(EventType.PRIORITY_MESSAGE_RECEIVED, alerts.append)
    return bus, alerts


def _post(bus, source, area="ALL", urgent=False):
    bus.emit(EventType.BULLETIN_POSTED, source=source,
             data={"mid": "M1", "from": "W1XYZ", "to_group": area,
                   "subject": "s", "area": area, "is_urgent": urgent})


@pytest.mark.parametrize("area,urgent", [("ALL", False), ("ALL", True),
                                         ("EMCOMM", False)])
def test_the_sysop_is_not_alerted_about_their_own_post(area, urgent):
    bus, alerts = _router(["ALL", "EMCOMM"])
    _post(bus, SYSOP_GUI, area, urgent)
    assert alerts == []


@pytest.mark.parametrize("area,urgent,alerted", [
    ("EMCOMM", False, True),     # urgent area
    ("ALL", True, True),         # flagged urgent
    ("ALL", False, False),       # neither
])
def test_bulletins_from_stations_still_alert(area, urgent, alerted):
    bus, alerts = _router(["EMCOMM"])
    _post(bus, "fm1", area, urgent)
    assert bool(alerts) is alerted


# ── Post tab ────────────────────────────────────────────────────────────────

QtWidgets = pytest.importorskip("PyQt6.QtWidgets")


@pytest.fixture(scope="module")
def qapp():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


class _CM:
    def __init__(self, cfg):
        self.cfg = cfg

    def current(self):
        return self.cfg


def test_post_tab_says_when_the_area_is_urgent(qapp, tmp_path):
    from bbs.bulletins import BulletinStore
    from bbs.store import MessageStore
    from gui.bulletins_tab import PostTab
    cfg = AppConfig()
    cfg.bbs.message_dir = str(tmp_path / "messages")
    cm = _CM(cfg)
    store = MessageStore(cfg)
    store.initialize()
    tab = PostTab(BulletinStore(store), cfg, config_manager=cm)
    tab._group_combo.setCurrentText("EMCOMM")
    assert not tab._area_note.isHidden()
    assert "EMCOMM is an urgent area" in tab._area_note.text()
    tab._group_combo.setCurrentText("ALL")
    assert tab._area_note.isHidden()
    # A list changed in Settings is used without reopening the tab.
    cm.cfg = cfg.model_copy(deep=True)
    cm.cfg.notifications.urgent_bulletin_areas = ["all"]
    tab._update_area_note()
    assert "ALL is an urgent area" in tab._area_note.text()
