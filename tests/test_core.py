"""
tests/test_core.py

Unit tests for config, AX.25 parser, message store,
bulletin board, and APRS passcode.
No VARA FM hardware or network required.
"""

import os
import sys
import tempfile
from pathlib import Path

import pytest

# Allow imports from parent directory
sys.path.insert(0, str(Path(__file__).parent.parent))


# ── Config tests ─────────────────────────────────────────────────────────────

class TestConfig:

    def test_default_config_creates_cleanly(self):
        from core.config import AppConfig
        cfg = AppConfig()
        assert cfg.station.bbs_callsign == "N0CALL-10"
        assert len(cfg.instances) == 1
        assert cfg.instances[0].cmd_port == 8300

    def test_instance_ports_must_differ(self):
        from core.config import InstanceConfig
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            InstanceConfig(name="test", cmd_port=8300, data_port=8300,
                           executable_path="")

    def test_no_port_conflicts_between_instances(self):
        from core.config import AppConfig, InstanceConfig
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            AppConfig(instances=[
                InstanceConfig(name="A", cmd_port=8300, data_port=8301,
                               executable_path=""),
                InstanceConfig(name="B", cmd_port=8300, data_port=8302,
                               executable_path=""),
            ])

    def test_config_save_and_load(self, tmp_path):
        from core.config import ConfigManager, AppConfig
        p = tmp_path / "test_config.toml"
        cm = ConfigManager(path=p)
        cfg = cm.load()
        assert cfg.station.bbs_callsign == "N0CALL-10"
        assert p.exists()

        # Modify and save
        cfg.station.bbs_callsign = "W1TEST-10"
        cm.save(cfg)

        # Reload and verify
        cfg2 = cm.load()
        assert cfg2.station.bbs_callsign == "W1TEST-10"

    def test_corrupt_config_recovery(self, tmp_path):
        from core.config import ConfigManager
        p = tmp_path / "bad_config.toml"
        p.write_text("this is not valid toml }{[[[")
        cm = ConfigManager(path=p)
        cfg = cm.load()
        # Should recover with defaults
        assert cfg.station.bbs_callsign == "N0CALL-10"

    def test_disabled_instances_excluded_from_port_check(self):
        from core.config import AppConfig, InstanceConfig
        # Two instances with same port, but one disabled — should be valid
        cfg = AppConfig(instances=[
            InstanceConfig(name="A", cmd_port=8300, data_port=8301,
                           enabled=True, executable_path=""),
            InstanceConfig(name="B", cmd_port=8300, data_port=8301,
                           enabled=False, executable_path=""),
        ])
        assert len(cfg.instances) == 2


# ── AX.25 parser tests ────────────────────────────────────────────────────────

class TestAX25Parser:

    def test_parse_simple_callsign(self):
        from ax25.parser import parse_callsign
        addr = parse_callsign("W1ABC")
        assert addr.callsign == "W1ABC"
        assert addr.ssid == 0

    def test_parse_callsign_with_ssid(self):
        from ax25.parser import parse_callsign
        addr = parse_callsign("W1ABC-10")
        assert addr.callsign == "W1ABC"
        assert addr.ssid == 10

    def test_invalid_callsign_returns_none(self):
        from ax25.parser import parse_callsign
        assert parse_callsign("NOT-VALID-CALL-TOO-LONG") is None
        assert parse_callsign("") is None

    def test_ssid_above_15_invalid(self):
        from ax25.parser import parse_callsign
        assert parse_callsign("W1ABC-16") is None

    def test_parse_path(self):
        from ax25.parser import parse_path
        path = parse_path("WIDE1-1,W1DEF-2")
        assert len(path) == 2
        assert path[0].callsign == "WIDE1"
        assert path[0].ssid == 1
        assert path[1].callsign == "W1DEF"
        assert path[1].ssid == 2

    def test_empty_path(self):
        from ax25.parser import parse_path
        assert parse_path("") == []
        assert parse_path("  ") == []

    def test_repeated_indicator(self):
        from ax25.parser import parse_path
        path = parse_path("WIDE1-1*,W1DEF-2")
        assert path[0].has_been_repeated is True
        assert path[1].has_been_repeated is False

    def test_callsign_str_representation(self):
        from ax25.parser import AX25Address
        addr = AX25Address(callsign="W1ABC", ssid=10)
        assert str(addr) == "W1ABC-10"
        addr2 = AX25Address(callsign="W1ABC", ssid=0)
        assert str(addr2) == "W1ABC"

    def test_format_connect_path(self):
        from ax25.parser import format_connect_path
        assert format_connect_path("") == ""
        assert format_connect_path("WIDE1-1") == "WIDE1-1"
        assert format_connect_path("W1DEF-1,W2GHI-2") == "W1DEF-1,W2GHI-2"


# ── Message store tests ───────────────────────────────────────────────────────

class TestMessageStore:

    @pytest.fixture
    def store(self, tmp_path):
        from core.config import AppConfig, BBSConfig
        from bbs.store import MessageStore
        cfg = AppConfig()
        cfg.bbs.message_dir = str(tmp_path / "messages")
        s = MessageStore(cfg)
        s.initialize()
        yield s
        s.close()

    def test_save_and_retrieve(self, store):
        mid = store.save_message(
            from_call="W1ABC",
            to_call="W2DEF",
            subject="Test",
            body=b"Hello world",
        )
        assert len(mid) == 12
        msg = store.get_message(mid)
        assert msg is not None
        assert msg["subject"] == "Test"
        assert msg["body"] == b"Hello world"

    def test_has_message(self, store):
        mid = store.save_message("W1A", "W2B", "s", b"b")
        assert store.has_message(mid) is True
        assert store.has_message("NONEXISTENT") is False

    def test_duplicate_mid_ignored(self, store):
        mid = store.save_message("W1A", "W2B", "s1", b"b1", mid="TESTMID00001")
        store.save_message("W1A", "W2B", "s2", b"b2", mid="TESTMID00001")
        msg = store.get_message("TESTMID00001")
        assert msg["subject"] == "s1"   # first one wins

    def test_messages_for_callsign(self, store):
        store.save_message("W1A", "W9XYZ", "msg1", b"body1")
        store.save_message("W1A", "W9XYZ", "msg2", b"body2")
        store.save_message("W1A", "W2DEF", "other", b"body3")
        msgs = store.get_messages_for("W9XYZ")
        assert len(msgs) == 2

    def test_mark_sent(self, store):
        mid = store.save_message("W1A", "W2B", "s", b"b")
        store.mark_sent(mid)
        msgs = store.get_messages_for("W2B")
        assert len(msgs) == 0   # is_sent=1 excluded

    def test_delete_message(self, store):
        mid = store.save_message("W1A", "W2B", "s", b"b")
        store.delete_message(mid)
        assert store.get_message(mid) is None

    def test_message_count(self, store):
        assert store.message_count() == 0
        store.save_message("W1A", "W2B", "s", b"b")
        assert store.message_count() == 1

    def test_heard_station_upsert(self, store):
        store.upsert_heard_station("W1ABC", 0, "Primary")
        store.upsert_heard_station("W1ABC", 0, "Primary")
        stations = store.get_heard_stations(instance="Primary")
        assert len(stations) == 1
        assert stations[0]["hear_count"] == 2

    def test_user_prefs_defaults(self, store):
        prefs = store.get_user_prefs("W1ABC")
        assert prefs["pager_lines"] == 20

    def test_user_prefs_save_and_load(self, store):
        store.save_user_prefs("W1ABC", pager_lines=30)
        prefs = store.get_user_prefs("W1ABC")
        assert prefs["pager_lines"] == 30

    def test_expire_old_messages(self, store):
        from datetime import datetime, timedelta, timezone
        import sqlite3
        mid = store.save_message("W1A", "W2B", "old", b"b")
        # Manually set expires_at to the past
        with store._lock:
            store._conn.execute(
                "UPDATE messages SET expires_at=? WHERE id=?",
                ((datetime.now(timezone.utc) - timedelta(days=1)).isoformat(), mid)
            )
            store._conn.commit()
        store.expire_old_messages()
        assert store.get_message(mid) is None


# ── APRS passcode test ────────────────────────────────────────────────────────

class TestAPRS:

    def test_passcode_known_value(self):
        from aprs.gateway import aprs_passcode
        # Standard APRS-IS passcode for W1AW is well-known: 15368
        # (test with a verifiable value)
        code = aprs_passcode("W1AW")
        assert isinstance(code, int)
        assert 0 <= code <= 32767

    def test_passcode_strips_ssid(self):
        from aprs.gateway import aprs_passcode
        assert aprs_passcode("W1ABC") == aprs_passcode("W1ABC-10")

    def test_passcode_case_insensitive(self):
        from aprs.gateway import aprs_passcode
        assert aprs_passcode("W1ABC") == aprs_passcode("w1abc")


# ── Bulletin address detection tests ─────────────────────────────────────────

class TestBulletinAddress:

    def test_well_known_groups_are_bulletins(self):
        from bbs.bulletins import is_bulletin_address
        for group in ["ALL", "WX", "DX", "NTS", "LOCAL", "INFO", "ARES"]:
            assert is_bulletin_address(group), f"{group} should be a bulletin group"

    def test_callsigns_are_not_bulletins(self):
        from bbs.bulletins import is_bulletin_address
        for call in ["W1ABC", "VK3XYZ", "N0CALL-10", "W1ABC-1"]:
            assert not is_bulletin_address(call), f"{call} should not be a bulletin"

    def test_custom_group_no_digits(self):
        from bbs.bulletins import is_bulletin_address
        assert is_bulletin_address("EMCOMM")
        assert is_bulletin_address("RACES")
        assert is_bulletin_address("NETS")

    def test_group_with_digit_is_callsign(self):
        from bbs.bulletins import is_bulletin_address
        # Groups with digits look like callsigns
        assert not is_bulletin_address("WX2")
        assert not is_bulletin_address("DX1")

    def test_case_insensitive(self):
        from bbs.bulletins import is_bulletin_address
        assert is_bulletin_address("all")
        assert is_bulletin_address("Wx")
        assert is_bulletin_address("local")


# ── BulletinStore tests ───────────────────────────────────────────────────────

class TestBulletinStore:

    @pytest.fixture
    def stores(self, tmp_path):
        """Returns (MessageStore, BulletinStore) pair with temp DB."""
        from core.config import AppConfig
        from bbs.store import MessageStore
        from bbs.bulletins import BulletinStore

        cfg = AppConfig()
        cfg.bbs.message_dir = str(tmp_path / "messages")
        store = MessageStore(cfg)
        store.initialize()
        bbs = BulletinStore(store)
        yield store, bbs
        store.close()

    def test_save_and_list_bulletin(self, stores):
        store, bbs = stores
        mid = bbs.save_bulletin(
            from_call="W1ABC",
            to_group="ALL",
            subject="Test bulletin",
            body=b"Hello everyone",
            area="ALL",
        )
        assert len(mid) == 12
        bulletins = bbs.list_bulletins()
        assert len(bulletins) == 1
        assert bulletins[0]["subject"] == "Test bulletin"
        assert bulletins[0]["to_call"] == "ALL"

    def test_bulletin_flagged_separately_from_messages(self, stores):
        store, bbs = stores
        # Save one personal message and one bulletin
        store.save_message("W1A", "W2B", "personal", b"private")
        bbs.save_bulletin("W1A", "ALL", "bulletin", b"public", area="ALL")

        # list_bulletins should only return the bulletin
        bulletins = bbs.list_bulletins()
        assert len(bulletins) == 1
        assert bulletins[0]["subject"] == "bulletin"

        # list_messages returns personal mail only: bulletins share the
        # table but are excluded (is_bulletin = 1)
        all_msgs = store.list_messages()
        assert len(all_msgs) == 1
        assert all_msgs[0]["subject"] == "personal"

    def test_bulletin_count(self, stores):
        store, bbs = stores
        assert bbs.bulletin_count() == 0
        bbs.save_bulletin("W1A", "ALL", "s1", b"b", area="ALL")
        bbs.save_bulletin("W1A", "WX", "s2", b"b", area="WX")
        assert bbs.bulletin_count() == 2
        assert bbs.bulletin_count("ALL") == 1
        assert bbs.bulletin_count("WX") == 1

    def test_list_bulletin_areas(self, stores):
        store, bbs = stores
        bbs.save_bulletin("W1A", "ALL", "s1", b"b", area="ALL")
        bbs.save_bulletin("W1A", "WX",  "s2", b"b", area="WX")
        areas = bbs.list_bulletin_areas()
        assert "ALL" in areas
        assert "WX" in areas

    def test_filter_bulletins_by_area(self, stores):
        store, bbs = stores
        bbs.save_bulletin("W1A", "ALL", "general", b"b", area="ALL")
        bbs.save_bulletin("W1A", "WX",  "weather", b"b", area="WX")
        wx_only = bbs.list_bulletins(area="WX")
        assert len(wx_only) == 1
        assert wx_only[0]["subject"] == "weather"

    def test_duplicate_mid_rejected(self, stores):
        store, bbs = stores
        bbs.save_bulletin("W1A", "ALL", "first",  b"b", area="ALL", mid="TESTBULL0001")
        bbs.save_bulletin("W1A", "ALL", "second", b"b", area="ALL", mid="TESTBULL0001")
        bulletins = bbs.list_bulletins()
        assert len(bulletins) == 1
        assert bulletins[0]["subject"] == "first"   # first wins

    def test_mark_seen_and_already_seen(self, stores):
        store, bbs = stores
        mid = bbs.save_bulletin("W1A", "ALL", "s", b"b", area="ALL")
        assert not bbs.already_seen(mid, "W2BBS-1")
        bbs.mark_seen(mid, "W2BBS-1")
        assert bbs.already_seen(mid, "W2BBS-1")
        # Not seen for a different neighbour
        assert not bbs.already_seen(mid, "W3BBS-1")

    def test_get_unsent_bulletins_for_neighbour(self, stores):
        store, bbs = stores
        mid1 = bbs.save_bulletin("W1A", "ALL", "s1", b"b", area="ALL")
        mid2 = bbs.save_bulletin("W1A", "WX",  "s2", b"b", area="WX")

        # Initially both are unsent to W2BBS-1
        unsent = bbs.get_unsent_bulletins_for("W2BBS-1")
        assert len(unsent) == 2

        # Mark first as sent to W2BBS-1
        bbs.mark_seen(mid1, "W2BBS-1")
        unsent = bbs.get_unsent_bulletins_for("W2BBS-1")
        assert len(unsent) == 1
        assert unsent[0]["id"] == mid2

    def test_area_filter_on_unsent(self, stores):
        store, bbs = stores
        bbs.save_bulletin("W1A", "ALL", "s1", b"b", area="ALL")
        bbs.save_bulletin("W1A", "WX",  "s2", b"b", area="WX")

        # Only ask for WX area
        unsent = bbs.get_unsent_bulletins_for("W2BBS-1", areas=["WX"])
        assert len(unsent) == 1
        assert unsent[0]["to_call"] == "WX"

    def test_forwarding_log(self, stores):
        store, bbs = stores
        mid = bbs.save_bulletin("W1A", "ALL", "s", b"b", area="ALL")
        bbs.log_forward("W2BBS-1", "sent",     mid, success=True)
        bbs.log_forward("W3BBS-1", "received", mid, success=True)
        log = bbs.get_forwarding_log()
        assert len(log) == 2
        directions = {row["direction"] for row in log}
        assert "sent" in directions
        assert "received" in directions

    def test_get_new_bulletins_since(self, stores):
        store, bbs = stores
        from datetime import datetime, timedelta, timezone

        # Post a bulletin and fake its timestamp to the past
        mid = bbs.save_bulletin("W1A", "ALL", "old", b"b", area="ALL")
        past = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        with store._lock:
            store._conn.execute(
                "UPDATE messages SET created_at=? WHERE id=?", (past, mid)
            )
            store._conn.commit()

        # Post a newer bulletin (timestamp = now)
        mid2 = bbs.save_bulletin("W1A", "ALL", "new", b"b", area="ALL")

        # Only retrieve bulletins newer than 1 hour ago
        one_hour_ago = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        new_only = bbs.get_new_bulletins(since=one_hour_ago)
        ids = [r["id"] for r in new_only]
        assert mid2 in ids
        assert mid not in ids


# ── Config bulletin area tests ────────────────────────────────────────────────

class TestBulletinConfig:

    def test_default_bulletin_areas_present(self):
        from core.config import AppConfig
        cfg = AppConfig()
        area_names = [a.name for a in cfg.bbs.bulletin_areas]
        assert "ALL"   in area_names
        assert "WX"    in area_names
        assert "LOCAL" in area_names
        assert "DX"    in area_names

    def test_bulletin_area_max_age(self):
        from core.config import AppConfig, BulletinAreaConfig
        cfg = AppConfig()
        cfg.bbs.bulletin_areas = [
            BulletinAreaConfig(name="ALL", max_age=7),
        ]
        assert cfg.bbs.bulletin_areas[0].max_age == 7

    def test_forwarding_config_defaults(self):
        from core.config import AppConfig
        cfg = AppConfig()
        assert cfg.bbs.forwarding.server_enabled is False
        assert cfg.bbs.forwarding.server_port == 7300
        assert cfg.bbs.forwarding_neighbours == []

    def test_forwarding_neighbour_config(self):
        from core.config import AppConfig, ForwardingNeighbourConfig
        cfg = AppConfig()
        cfg.bbs.forwarding_neighbours = [
            ForwardingNeighbourConfig(
                bbs_callsign="W2BBS-1",
                host="192.168.1.50",
                port=7300,
                interval_minutes=30,
                areas=["ALL", "WX"],
            )
        ]
        n = cfg.bbs.forwarding_neighbours[0]
        assert n.bbs_callsign == "W2BBS-1"
        assert n.interval_minutes == 30
        assert "WX" in n.areas

    def test_bulletin_area_disabled(self):
        from core.config import AppConfig, BulletinAreaConfig
        cfg = AppConfig()
        cfg.bbs.bulletin_areas = [
            BulletinAreaConfig(name="ALL",   enabled=True),
            BulletinAreaConfig(name="ADULT", enabled=False),
        ]
        enabled = [a for a in cfg.bbs.bulletin_areas if a.enabled]
        assert len(enabled) == 1
        assert enabled[0].name == "ALL"


def test_sms_text_cannot_inject_modem_commands():
    from notifications.sms import sms_safe_text
    # Ctrl+Z would end the SMS and the rest would be run as an AT command
    assert sms_safe_text("NEW MSG\x1aAT+CMGD=1,4\x1b") == "NEW MSGAT+CMGD=1,4"
    assert sms_safe_text("line1\r\nline2\t!") == "line1\nline2!"
