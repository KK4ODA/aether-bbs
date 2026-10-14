"""
tests/test_gui_settings.py

Settings → Instances must save every instance field, including the ones the
tab has no widget for (soundcard, auto_restart, restart_delay, host).
Runs the real SettingsTab on Qt's offscreen platform.
"""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

QtWidgets = pytest.importorskip("PyQt6.QtWidgets")


@pytest.fixture(scope="module")
def qapp():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    yield app


def _tab(tmp_path, cfg_text: str):
    from core.config import ConfigManager
    from core.events import EventBus
    from gui.settings import SettingsTab
    path = tmp_path / "aether_bbs.toml"
    path.write_text(cfg_text, encoding="utf-8")
    cm = ConfigManager(path)
    return cm, SettingsTab(cm, cm.load(), EventBus())


def test_fields_without_widgets_survive_save(qapp, tmp_path):
    cm, tab = _tab(tmp_path, (
        '[[instances]]\n'
        'name = "HF"\n'
        'transport = "VARA_HF"\n'
        'host = "192.168.1.20"\n'
        'soundcard = "USB Audio CODEC"\n'
        'auto_restart = false\n'
        'restart_delay = 45\n'
    ))
    tab._inst_freq.setText("14.105")
    inst = tab._build_config().instances[0]
    assert inst.host == "192.168.1.20"
    assert inst.soundcard == "USB Audio CODEC"
    assert inst.auto_restart is False
    assert inst.restart_delay == 45
    assert inst.frequency_label == "14.105"          # edited field applied


def test_new_instance_gets_defaults(qapp, tmp_path):
    from core.config import InstanceConfig
    cm, tab = _tab(tmp_path, '[[instances]]\nname = "VHF"\n')
    tab._add_empty_instance()
    new = tab._build_config().instances[1]
    assert new.auto_restart == InstanceConfig().auto_restart
    assert new.cmd_port == 8310


def test_aether_settings_round_trip(qapp, tmp_path):
    cm, tab = _tab(tmp_path, '[[instances]]\nname = "HF"\n')
    tab._inst_mode.setCurrentText("AETHER_HF")
    tab._inst_ae_bw.setCurrentText("500")
    tab._inst_ae_token.setText("tok123")
    cfg = tab._build_config()
    assert cfg.instances[0].aether.control_token.startswith("ENC:")
    cm.save(cfg)

    from core.events import EventBus
    from gui.settings import SettingsTab
    again = SettingsTab(cm, cm.load(), EventBus())
    assert again._inst_ae_token.text() == "tok123"
    assert again._inst_ae_bw.currentText() == "500"
    assert not again._inst_aether_group.isHidden()


def test_save_keeps_settings_the_tab_has_no_widget_for(qapp, tmp_path):
    """Values only in the TOML file (no widget in Settings) survive a save."""
    cm, tab = _tab(tmp_path, (
        '[bbs]\nmessage_dir = "D:/bbsdata/messages"\n'
        '[aprs]\nbeacon_symbol = "\\\\k"\n'
        '[logging]\nmax_bytes = 100000\nbackup_count = 9\n'
        '[[outbound_delivery.routing_overrides]]\n'
        'pattern = "@example.org"\nbackend = "SMTP"\n'
        '[[instances]]\nname = "VHF"\n'
    ))
    tab._welcome.setText("edited welcome")
    cfg = tab._build_config()
    assert cfg.bbs.message_dir == "D:/bbsdata/messages"
    assert cfg.bbs.welcome_message == "edited welcome"      # the edit applies
    assert cfg.aprs.beacon_symbol == "\\k"
    assert (cfg.logging.max_bytes, cfg.logging.backup_count) == (100000, 9)
    assert [o.pattern for o in cfg.outbound_delivery.routing_overrides] == ["@example.org"]


def test_overlay_keeps_unset_fields_and_replaces_lists():
    from core.config import AppConfig, BBSConfig, InstanceConfig, overlay
    base = AppConfig(bbs=BBSConfig(message_dir="X:/m", session_timeout=99),
                     instances=[InstanceConfig(name="A", cmd_port=8300, data_port=8301),
                                InstanceConfig(name="B", cmd_port=8310, data_port=8311)])
    upd = AppConfig(bbs=BBSConfig(session_timeout=300), instances=[InstanceConfig(name="C")])
    out = overlay(base, upd)
    assert (out.bbs.message_dir, out.bbs.session_timeout) == ("X:/m", 300)
    assert [i.name for i in out.instances] == ["C"]
