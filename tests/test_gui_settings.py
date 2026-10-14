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
        '[[instances]]\nname = "VHF"\n'
    ))
    tab._welcome.setText("edited welcome")
    cfg = tab._build_config()
    assert cfg.bbs.message_dir == "D:/bbsdata/messages"
    assert cfg.bbs.welcome_message == "edited welcome"      # the edit applies
    assert cfg.aprs.beacon_symbol == "\\k"
    assert (cfg.logging.max_bytes, cfg.logging.backup_count) == (100000, 9)


def test_overlay_keeps_unset_fields_and_replaces_lists():
    from core.config import AppConfig, BBSConfig, InstanceConfig, overlay
    base = AppConfig(bbs=BBSConfig(message_dir="X:/m", session_timeout=99),
                     instances=[InstanceConfig(name="A", cmd_port=8300, data_port=8301),
                                InstanceConfig(name="B", cmd_port=8310, data_port=8311)])
    upd = AppConfig(bbs=BBSConfig(session_timeout=300), instances=[InstanceConfig(name="C")])
    out = overlay(base, upd)
    assert (out.bbs.message_dir, out.bbs.session_timeout) == ("X:/m", 300)
    assert [i.name for i in out.instances] == ["C"]


def test_check_for_updates_reports_back(qapp, tmp_path, monkeypatch):
    """The manual check runs on a worker thread; its result must reach the
    tab (it used to stay at "Checking ..." with the button disabled)."""
    import time
    from core.updater import Updater, UpdateCheckResult
    monkeypatch.setattr(Updater, "check_for_updates",
                        lambda self, manual=False: UpdateCheckResult(current_version="9.9.9"))
    cm, tab = _tab(tmp_path, '[[instances]]\nname = "VHF"\n')
    tab._on_check_updates()
    deadline = time.time() + 5
    while time.time() < deadline and not tab._upd_check_btn.isEnabled():
        qapp.processEvents()
        time.sleep(0.02)
    assert tab._upd_check_btn.isEnabled()
    assert "Up to date" in tab._upd_status.text()


def test_one_bad_value_does_not_wipe_the_config(tmp_path):
    """A value that fails validation resets its own section only; the
    callsign and the machine secret (which every stored password depends
    on) survive."""
    from core.config import ConfigManager
    path = tmp_path / "aether_bbs.toml"
    path.write_text(
        '[station]\nbbs_callsign = "KK4ODA-1"\n'
        '[security]\nmachine_secret = "s3cret-abc"\n'
        '[bbs]\nsession_timeout = -5\nwelcome_message = "hi"\n'
        '[aprs]\nbeacon_symbol = "\\\\k"\n')
    cfg = ConfigManager(path).load()
    assert cfg.station.bbs_callsign == "KK4ODA-1"
    assert cfg.security.machine_secret == "s3cret-abc"
    assert cfg.aprs.beacon_symbol == "\\k"
    assert cfg.bbs.session_timeout > 0                 # the bad section reset
    assert list(tmp_path.glob("aether_bbs.toml.bad.*"))  # original kept
    again = ConfigManager(path).load()                  # and the saved file loads
    assert again.security.machine_secret == "s3cret-abc"


def test_revert_puts_the_saved_values_back_in_the_form(qapp, tmp_path, monkeypatch):
    """Revert used to reload the config but leave the edited widgets, so
    the next Save wrote the edits anyway."""
    from gui import settings as settings_mod
    monkeypatch.setattr(settings_mod.QMessageBox, "information",
                        lambda *a, **k: None)
    cm, tab = _tab(tmp_path, '[updater]\nauto_check_interval_hours = 12\n'
                             '[[instances]]\nname = "HF"\n')
    pages = tab._pages.count()
    tab._upd_interval.setValue(99)
    tab._inst_name.setText("EDITED")
    tab._revert()
    assert tab._upd_interval.value() == 12
    cfg = tab._build_config()
    assert cfg.updater.auto_check_interval_hours == 12
    assert cfg.instances[0].name == "HF"
    assert tab._pages.count() == pages          # rebuilt, not duplicated


def test_old_smtp_fallback_settings_are_purged_from_disk(tmp_path):
    """The SMTP fallback is gone; its plain-text provider passwords must not
    stay in the file, and the inbound email settings beside them must."""
    from core.config import ConfigManager
    path = tmp_path / "aether_bbs.toml"
    path.write_text(
        '[outbound_delivery]\nengine_enabled = true\n'
        '[outbound_delivery.inbound_email]\nhost = "imap.example.org"\n'
        '[outbound_delivery.smtp]\nenabled = true\n'
        '[[outbound_delivery.smtp.providers]]\nhost = "smtp.example.org"\n'
        'password = "hunter2"\n', encoding="utf-8")
    cfg = ConfigManager(path).load()
    assert cfg.outbound_delivery.inbound_email.host == "imap.example.org"
    text = path.read_text(encoding="utf-8")
    assert "hunter2" not in text and "smtp" not in text


def test_reply_goes_back_the_way_the_message_came(qapp, tmp_path):
    """A reply to Winlink mail goes through Winlink; a reply to a local RF
    user waits for RF pickup (it used to be sent to the CMS at the next
    poll, whoever it was for)."""
    from bbs.store import MessageStore
    from core.config import AppConfig
    from gui.message_viewer import ReplyDialog
    cfg = AppConfig()
    cfg.bbs.message_dir = str(tmp_path / "messages")
    store = MessageStore(cfg)
    store.initialize()
    from_cms = store.save_message("W1AW", "KK4ODA", "hi", b"x")
    store.set_cms_status(from_cms, "RECEIVED")
    from_rf = store.save_message("N0RF", "KK4ODA", "hi", b"x")
    routes = {}
    for mid, sender in ((from_cms, "W1AW"), (from_rf, "N0RF")):
        dlg = ReplyDialog(store, mid, sender, "KK4ODA", "hi")
        dlg._body_edit.setPlainText("thanks")
        dlg.reply_sent.connect(lambda new, s=sender: routes.__setitem__(s, new))
        dlg._send()
    assert store.get_message(routes["W1AW"])["cms_status"] == "QUEUED"
    assert store.get_message(routes["N0RF"])["cms_status"] == "LOCAL"


def test_inbound_email_password_is_encrypted_at_rest(qapp, tmp_path):
    from core.config import ConfigManager
    from core.events import EventBus
    from gui.settings import SettingsTab
    path = tmp_path / "aether_bbs.toml"
    path.write_text('[outbound_delivery.inbound_email]\nusername = "bbs"\n'
                    'password = "imap-secret"\n', encoding="utf-8")
    cm = ConfigManager(path)
    cfg = cm.load()
    assert "imap-secret" not in path.read_text(encoding="utf-8")
    ie = cfg.outbound_delivery.inbound_email
    assert ie.password.startswith("ENC:")
    assert ie.decrypt_password(cfg.security.machine_secret) == "imap-secret"

    tab = SettingsTab(cm, cfg, EventBus())
    assert tab._ie_tab.widgets["password"].text() == "imap-secret"
    tab._ie_tab.widgets["password"].setText("new-secret")
    saved = tab._build_config()
    cm.save(saved)
    assert "new-secret" not in path.read_text(encoding="utf-8")
    assert saved.outbound_delivery.inbound_email.decrypt_password(
        saved.security.machine_secret) == "new-secret"


def test_settings_that_need_a_restart_are_named():
    from core.config import AppConfig, ForwardingNeighbourConfig, InstanceConfig
    from gui.settings import restart_needed
    old = AppConfig(instances=[InstanceConfig(name="HF", enabled=False)])
    assert restart_needed(old, old.model_copy(deep=True)) == []

    new = old.model_copy(deep=True)
    new.instances[0].enabled = True
    new.instances.append(InstanceConfig(name="VHF", cmd_port=8310, data_port=8311))
    new.aprs.enabled = True
    new.bbs.forwarding_neighbours = [ForwardingNeighbourConfig(bbs_callsign="W2BBB")]
    new.updater.auto_check_interval_hours = 6
    new.outbound_delivery.inbound_email.host = "imap.example.org"
    new.bbs.welcome_message = "hello"              # live: not listed
    assert restart_needed(old, new) == [
"Enabling modem instance HF",
        "New modem instance VHF"]


def test_an_unchanged_secret_is_saved_unchanged(qapp, tmp_path):
    """Each Save re-encrypted every secret with a new salt, so it always
    looked changed: an Aether instance with a control token was restarted
    on every Save."""
    cm, tab = _tab(tmp_path, '[[instances]]\nname = "HF"\n')
    tab._inst_mode.setCurrentText("AETHER_HF")
    tab._inst_ae_token.setText("tok123")
    tab._ie_tab.widgets["password"].setText("imap-pw")
    tab._wl_password.setText("wl-pw")
    first = tab._build_config()
    cm.save(first)

    from core.events import EventBus
    from gui.settings import SettingsTab
    again = SettingsTab(cm, cm.load(), EventBus())
    second = again._build_config()
    assert second.instances[0].aether == first.instances[0].aether
    assert second.outbound_delivery.inbound_email == first.outbound_delivery.inbound_email
    assert second.winlink.gateway_password == first.winlink.gateway_password

    again._ie_tab.widgets["password"].setText("new-pw")
    third = again._build_config()
    assert third.outbound_delivery.inbound_email.password != \
        first.outbound_delivery.inbound_email.password
    assert third.outbound_delivery.inbound_email.decrypt_password(
        third.security.machine_secret) == "new-pw"
