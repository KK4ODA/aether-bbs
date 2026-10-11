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
