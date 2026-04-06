"""
tests/test_security.py

Comprehensive tests for the VARA BBS security hardening:
  Phase 1 — Access control, roles, registration, user management
  Phase 2 — Authentication, rate limiting, mode policy, file validation
"""

import hashlib
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Ensure project root is on path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bbs.access import (
    AccessController, Role, ROLE_HIERARCHY, COMMAND_PERMISSIONS,
    _base_call,
)
from bbs.rate_limiter import RateLimiter, RATE_LIMITS
from bbs.mode_policy import ModePolicy
from bbs.filestore import _validate_file_content
from core.config import AppConfig, SecurityConfig, FileUploadConfig


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_db():
    """Create an in-memory SQLite connection for testing."""
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn, threading.Lock()


def _make_controller(conn=None, lock=None, config=None, event_bus=None):
    """Create an AccessController with test defaults."""
    if conn is None:
        conn, lock = _make_db()
    if config is None:
        config = AppConfig()
    return AccessController(conn, lock, config, event_bus), conn


def _make_config(**sec_kwargs) -> AppConfig:
    """Create an AppConfig with custom SecurityConfig."""
    return AppConfig(security=SecurityConfig(**sec_kwargs))


# ═════════════════════════════════════════════════════════════════════════════
# Phase 1: Access Control, Roles, Registration
# ═════════════════════════════════════════════════════════════════════════════

class TestBaseCallsign(unittest.TestCase):
    """SSID normalization tests."""

    def test_strip_ssid(self):
        self.assertEqual(_base_call("KK4ODA-5"), "KK4ODA")
        self.assertEqual(_base_call("W4ABC-15"), "W4ABC")
        self.assertEqual(_base_call("N0CALL"), "N0CALL")

    def test_case_normalization(self):
        self.assertEqual(_base_call("kk4oda-5"), "KK4ODA")
        self.assertEqual(_base_call("w4abc"), "W4ABC")

    def test_empty(self):
        self.assertEqual(_base_call(""), "")


class TestRoleHierarchy(unittest.TestCase):
    """Verify the role ordering is correct."""

    def test_blocked_is_lowest(self):
        self.assertEqual(ROLE_HIERARCHY[0], Role.BLOCKED)

    def test_sysop_is_highest(self):
        self.assertEqual(ROLE_HIERARCHY[-1], Role.SYSOP)

    def test_order(self):
        expected = [Role.BLOCKED, Role.UNKNOWN, Role.PENDING,
                    Role.APPROVED, Role.TRUSTED, Role.SYSOP]
        self.assertEqual(ROLE_HIERARCHY, expected)

    def test_all_roles_in_hierarchy(self):
        for role in Role:
            self.assertIn(role, ROLE_HIERARCHY)


class TestAccessControllerRoles(unittest.TestCase):
    """Role lookup, set, and sysop fail-safe."""

    def setUp(self):
        self.config = _make_config()
        self.config.station.sysop_callsign = "KK4ODA"
        self.ac, self.conn = _make_controller(config=self.config)

    def test_unknown_by_default(self):
        self.assertEqual(self.ac.get_role("W4ABC"), Role.UNKNOWN)

    def test_sysop_failsafe(self):
        """Config sysop callsign always returns SYSOP regardless of DB."""
        self.assertEqual(self.ac.get_role("KK4ODA"), Role.SYSOP)
        self.assertEqual(self.ac.get_role("KK4ODA-5"), Role.SYSOP)
        self.assertEqual(self.ac.get_role("kk4oda"), Role.SYSOP)

    def test_sysop_failsafe_even_if_blocked(self):
        """Sysop callsign should return SYSOP even if DB says BLOCKED."""
        self.ac.set_role("KK4ODA", Role.BLOCKED, changed_by="TEST")
        self.assertEqual(self.ac.get_role("KK4ODA"), Role.SYSOP)

    def test_set_and_get_role(self):
        self.ac.set_role("W4ABC", Role.APPROVED, changed_by="TEST")
        self.assertEqual(self.ac.get_role("W4ABC"), Role.APPROVED)

    def test_set_role_ssid_normalization(self):
        """Setting role for W4ABC-5 should affect W4ABC base."""
        self.ac.set_role("W4ABC-5", Role.TRUSTED, changed_by="TEST")
        self.assertEqual(self.ac.get_role("W4ABC"), Role.TRUSTED)
        self.assertEqual(self.ac.get_role("W4ABC-7"), Role.TRUSTED)

    def test_role_transitions(self):
        self.ac.set_role("W4XYZ", Role.PENDING, changed_by="TEST")
        self.assertEqual(self.ac.get_role("W4XYZ"), Role.PENDING)

        self.ac.set_role("W4XYZ", Role.APPROVED, changed_by="SYSOP")
        self.assertEqual(self.ac.get_role("W4XYZ"), Role.APPROVED)

        self.ac.set_role("W4XYZ", Role.TRUSTED, changed_by="SYSOP")
        self.assertEqual(self.ac.get_role("W4XYZ"), Role.TRUSTED)

        self.ac.set_role("W4XYZ", Role.BLOCKED, changed_by="SYSOP")
        self.assertEqual(self.ac.get_role("W4XYZ"), Role.BLOCKED)


class TestPermissions(unittest.TestCase):
    """Command permission matrix tests."""

    def setUp(self):
        self.config = _make_config()
        self.config.station.sysop_callsign = "KK4ODA"
        self.ac, _ = _make_controller(config=self.config)

    def test_sysop_can_do_everything(self):
        for cmd in ["H", "L", "R 1", "S W4ABC", "SB ALL", "FP GENERAL test.txt",
                     "REGISTER", "KB 1", "FD GENERAL test.txt"]:
            self.assertTrue(
                self.ac.has_permission("KK4ODA", cmd),
                f"SYSOP should be able to run {cmd!r}"
            )

    def test_blocked_can_do_nothing(self):
        self.ac.set_role("BAD1", Role.BLOCKED, changed_by="TEST")
        for cmd in ["H", "L", "R 1", "REGISTER"]:
            self.assertFalse(
                self.ac.has_permission("BAD1", cmd),
                f"BLOCKED should not be able to run {cmd!r}"
            )

    def test_approved_can_read(self):
        self.ac.set_role("USER1", Role.APPROVED, changed_by="TEST")
        for cmd in ["H", "L", "R 1", "S W4ABC", "LB", "RB 1", "FA", "FL",
                     "FG GENERAL test.txt", "MH", "I", "CH", "B"]:
            self.assertTrue(
                self.ac.has_permission("USER1", cmd),
                f"APPROVED should be able to run {cmd!r}"
            )

    def test_approved_cannot_write_bulletins_or_upload(self):
        self.ac.set_role("USER1", Role.APPROVED, changed_by="TEST")
        for cmd in ["SB ALL", "KB 1", "FP GENERAL test.txt", "FD GENERAL test.txt"]:
            self.assertFalse(
                self.ac.has_permission("USER1", cmd),
                f"APPROVED should NOT be able to run {cmd!r}"
            )

    def test_trusted_can_write(self):
        self.ac.set_role("USER1", Role.TRUSTED, changed_by="TEST")
        for cmd in ["SB ALL", "KB 1", "FP GENERAL test.txt", "FD GENERAL test.txt"]:
            self.assertTrue(
                self.ac.has_permission("USER1", cmd),
                f"TRUSTED should be able to run {cmd!r}"
            )

    def test_unknown_can_register(self):
        self.assertTrue(self.ac.has_permission("NEWBIE", "REGISTER"))
        self.assertTrue(self.ac.has_permission("NEWBIE", "REG"))

    def test_unknown_cannot_read_messages(self):
        self.assertFalse(self.ac.has_permission("NEWBIE", "L"))
        self.assertFalse(self.ac.has_permission("NEWBIE", "R 1"))

    def test_pending_cannot_use_bbs(self):
        self.ac.set_role("WAIT1", Role.PENDING, changed_by="TEST")
        self.assertFalse(self.ac.has_permission("WAIT1", "L"))
        self.assertFalse(self.ac.has_permission("WAIT1", "H"))

    def test_unknown_command_allowed_for_approved(self):
        """Unknown commands pass through to get 'Unknown command' error."""
        self.ac.set_role("USER1", Role.APPROVED, changed_by="TEST")
        self.assertTrue(self.ac.has_permission("USER1", "XYZZY"))

    def test_unknown_command_blocked_for_pending(self):
        self.ac.set_role("WAIT1", Role.PENDING, changed_by="TEST")
        self.assertFalse(self.ac.has_permission("WAIT1", "XYZZY"))


class TestRegistration(unittest.TestCase):
    """Registration workflow tests."""

    def setUp(self):
        self.config = _make_config()
        self.config.station.sysop_callsign = "KK4ODA"
        self.bus = MagicMock()
        self.ac, _ = _make_controller(config=self.config, event_bus=self.bus)

    def test_register_unknown(self):
        ok, msg = self.ac.register("W4NEW", "Primary")
        self.assertTrue(ok)
        self.assertIn("Registration request received", msg)
        self.assertEqual(self.ac.get_role("W4NEW"), Role.PENDING)

    def test_register_emits_event(self):
        self.ac.register("W4NEW", "Primary")
        self.bus.emit.assert_called()

    def test_register_already_pending(self):
        self.ac.register("W4NEW")
        ok, msg = self.ac.register("W4NEW")
        self.assertFalse(ok)
        self.assertIn("already pending", msg)

    def test_register_already_approved(self):
        self.ac.set_role("W4OLD", Role.APPROVED, changed_by="TEST")
        ok, msg = self.ac.register("W4OLD")
        self.assertFalse(ok)
        self.assertIn("already have access", msg)

    def test_register_blocked(self):
        self.ac.set_role("BAD1", Role.BLOCKED, changed_by="TEST")
        ok, msg = self.ac.register("BAD1")
        self.assertFalse(ok)
        self.assertIn("blocked", msg)

    def test_register_ssid_normalization(self):
        ok, _ = self.ac.register("W4NEW-5")
        self.assertTrue(ok)
        self.assertEqual(self.ac.get_role("W4NEW"), Role.PENDING)
        self.assertEqual(self.ac.get_role("W4NEW-7"), Role.PENDING)

    def test_approval_workflow(self):
        """Full workflow: register → approve → verify."""
        self.ac.register("W4NEW")
        self.assertEqual(self.ac.get_role("W4NEW"), Role.PENDING)

        self.ac.set_role("W4NEW", Role.APPROVED, changed_by="KK4ODA",
                         reason="approved after verification")
        self.assertEqual(self.ac.get_role("W4NEW"), Role.APPROVED)


class TestListUsers(unittest.TestCase):
    """User listing for GUI."""

    def setUp(self):
        self.ac, _ = _make_controller()
        self.ac.set_role("AAA", Role.APPROVED, changed_by="T")
        self.ac.set_role("BBB", Role.TRUSTED, changed_by="T")
        self.ac.set_role("CCC", Role.PENDING, changed_by="T")
        self.ac.set_role("DDD", Role.BLOCKED, changed_by="T")

    def test_list_all(self):
        users = self.ac.list_users()
        calls = {u["callsign"] for u in users}
        self.assertEqual(calls, {"AAA", "BBB", "CCC", "DDD"})

    def test_list_by_role(self):
        pending = self.ac.list_users("PENDING")
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["callsign"], "CCC")

    def test_list_pending(self):
        pending = self.ac.list_pending()
        self.assertEqual(len(pending), 1)


class TestAuditLog(unittest.TestCase):
    """Security audit log tests."""

    def setUp(self):
        self.ac, _ = _make_controller()

    def test_audit_entries_created(self):
        self.ac.set_role("W4ABC", Role.APPROVED, changed_by="SYSOP")
        log = self.ac.get_audit_log("W4ABC")
        self.assertTrue(len(log) > 0)
        events = [e["event"] for e in log]
        self.assertIn("ROLE_CHANGE", events)

    def test_audit_log_limit(self):
        for i in range(10):
            self.ac.audit(f"CALL{i}", "TEST", f"entry_{i}")
        log = self.ac.get_audit_log(limit=5)
        self.assertEqual(len(log), 5)

    def test_audit_log_callsign_filter(self):
        self.ac.audit("AAA", "CONNECT", "test")
        self.ac.audit("BBB", "CONNECT", "test")
        log_a = self.ac.get_audit_log("AAA")
        for entry in log_a:
            self.assertEqual(entry["callsign"], "AAA")


# ═════════════════════════════════════════════════════════════════════════════
# Phase 2: Authentication
# ═════════════════════════════════════════════════════════════════════════════

class TestAuthentication(unittest.TestCase):
    """Password and challenge-response tests."""

    def setUp(self):
        self.config = _make_config(machine_secret="test_secret_key_42")
        self.config.station.sysop_callsign = "KK4ODA"
        self.ac, _ = _make_controller(config=self.config)
        self.ac.set_role("USER1", Role.APPROVED, changed_by="TEST")

    def test_no_password_by_default(self):
        self.assertFalse(self.ac.has_password("USER1"))

    def test_set_and_check_password(self):
        self.ac.set_password("USER1", "mysecretpw")
        self.assertTrue(self.ac.has_password("USER1"))

    def test_set_password_ssid_normalization(self):
        self.ac.set_password("USER1-5", "testpw")
        self.assertTrue(self.ac.has_password("USER1"))
        self.assertTrue(self.ac.has_password("USER1-7"))

    def test_challenge_response_correct(self):
        self.ac.set_password("USER1", "hunter2")
        nonce = self.ac.generate_challenge("USER1")
        self.assertTrue(len(nonce) == 16)  # hex(8 bytes)
        # Client sends raw password
        self.assertTrue(self.ac.verify_challenge("USER1", "hunter2"))

    def test_challenge_response_wrong_password(self):
        self.ac.set_password("USER1", "correct")
        self.ac.generate_challenge("USER1")
        self.assertFalse(self.ac.verify_challenge("USER1", "wrong"))

    def test_challenge_expired(self):
        """Expired challenge should fail."""
        self.ac.set_password("USER1", "pass123")
        self.ac.generate_challenge("USER1")
        # Manually expire the token
        with self.ac._lock:
            self.ac._conn.execute(
                "UPDATE access_control SET token_expires='2020-01-01T00:00:00+00:00' "
                "WHERE callsign='USER1'"
            )
            self.ac._conn.commit()
        self.assertFalse(self.ac.verify_challenge("USER1", "pass123"))

    def test_no_challenge_generated(self):
        """Verify should fail if no challenge was generated."""
        self.ac.set_password("USER1", "pass123")
        # Don't call generate_challenge
        self.assertFalse(self.ac.verify_challenge("USER1", "pass123"))


class TestLockout(unittest.TestCase):
    """Auth failure tracking and auto-block."""

    def setUp(self):
        self.config = _make_config(
            auth_failure_threshold=3,
            auth_lockout_minutes=60,
            auto_block_threshold=5,
            machine_secret="test",
        )
        self.config.station.sysop_callsign = "KK4ODA"
        self.ac, _ = _make_controller(config=self.config)
        self.ac.set_role("USER1", Role.APPROVED, changed_by="TEST")

    def test_no_lockout_initially(self):
        ok, _ = self.ac.check_lockout("USER1")
        self.assertTrue(ok)

    def test_lockout_after_threshold(self):
        for _ in range(3):
            self.ac.record_auth_failure("USER1")
        ok, msg = self.ac.check_lockout("USER1")
        self.assertFalse(ok)
        self.assertIn("Too many", msg)

    def test_success_resets_failures(self):
        for _ in range(2):
            self.ac.record_auth_failure("USER1")
        self.ac.record_auth_success("USER1")
        ok, _ = self.ac.check_lockout("USER1")
        self.assertTrue(ok)

    def test_auto_block(self):
        for _ in range(5):
            self.ac.record_auth_failure("USER1")
        ok, msg = self.ac.check_lockout("USER1")
        self.assertFalse(ok)
        # Should now be BLOCKED
        self.assertEqual(self.ac.get_role("USER1"), Role.BLOCKED)

    def test_blocked_always_denied(self):
        self.ac.set_role("BAD1", Role.BLOCKED, changed_by="TEST")
        ok, _ = self.ac.check_lockout("BAD1")
        self.assertFalse(ok)


class TestMigrationFromWhitelist(unittest.TestCase):
    """One-time migration from upload whitelist config."""

    def setUp(self):
        self.config = AppConfig()
        self.config.station.sysop_callsign = "KK4ODA"
        self.config.file_upload = FileUploadConfig(
            upload_whitelist=["W4ABC", "N4XYZ-5"],
            blocked_callsigns=["BAD1"],
        )
        self.ac, _ = _make_controller(config=self.config)

    def test_migration_creates_roles(self):
        self.ac.migrate_from_whitelist(self.config)
        self.assertEqual(self.ac.get_role("W4ABC"), Role.TRUSTED)
        self.assertEqual(self.ac.get_role("N4XYZ"), Role.TRUSTED)
        self.assertEqual(self.ac.get_role("BAD1"), Role.BLOCKED)

    def test_migration_only_runs_once(self):
        self.ac.migrate_from_whitelist(self.config)
        # Manually change a role
        self.ac.set_role("W4ABC", Role.APPROVED, changed_by="SYSOP")
        # Run migration again — should not overwrite
        self.ac.migrate_from_whitelist(self.config)
        self.assertEqual(self.ac.get_role("W4ABC"), Role.APPROVED)

    def test_sysop_gets_explicit_record(self):
        self.ac.migrate_from_whitelist(self.config)
        # Sysop is always SYSOP via fail-safe, but should also have a DB record
        users = self.ac.list_users("SYSOP")
        calls = {u["callsign"] for u in users}
        self.assertIn("KK4ODA", calls)


# ═════════════════════════════════════════════════════════════════════════════
# Phase 2: Rate Limiter
# ═════════════════════════════════════════════════════════════════════════════

class TestRateLimiter(unittest.TestCase):
    """In-memory sliding-window rate limiter."""

    def test_allows_within_limit(self):
        rl = RateLimiter()
        for _ in range(10):
            ok, _ = rl.check("W4ABC", "connect", max_count=10, window_seconds=60)
            self.assertTrue(ok)

    def test_blocks_over_limit(self):
        rl = RateLimiter()
        for _ in range(5):
            rl.check("W4ABC", "test", max_count=5, window_seconds=60)
        ok, count = rl.check("W4ABC", "test", max_count=5, window_seconds=60)
        self.assertFalse(ok)
        self.assertEqual(count, 5)

    def test_ssid_normalization(self):
        rl = RateLimiter()
        for _ in range(5):
            rl.check("W4ABC-5", "test", max_count=5, window_seconds=60)
        ok, _ = rl.check("W4ABC-7", "test", max_count=5, window_seconds=60)
        self.assertFalse(ok)

    def test_multiplier_stricter(self):
        rl = RateLimiter(multiplier=0.5)
        for _ in range(5):
            rl.check("W4ABC", "test", max_count=10, window_seconds=60)
        ok, _ = rl.check("W4ABC", "test", max_count=10, window_seconds=60)
        self.assertFalse(ok)  # effective limit = 10 * 0.5 = 5

    def test_multiplier_permissive(self):
        rl = RateLimiter(multiplier=2.0)
        for _ in range(10):
            ok, _ = rl.check("W4ABC", "test", max_count=10, window_seconds=60)
            self.assertTrue(ok)
        # Should still have room (effective limit = 20)
        ok, _ = rl.check("W4ABC", "test", max_count=10, window_seconds=60)
        self.assertTrue(ok)

    def test_reset_specific_event(self):
        rl = RateLimiter()
        for _ in range(5):
            rl.check("W4ABC", "test", max_count=5, window_seconds=60)
        rl.reset("W4ABC", "test")
        ok, _ = rl.check("W4ABC", "test", max_count=5, window_seconds=60)
        self.assertTrue(ok)

    def test_reset_all_events(self):
        rl = RateLimiter()
        for _ in range(5):
            rl.check("W4ABC", "test", max_count=5, window_seconds=60)
        rl.reset("W4ABC")
        ok, _ = rl.check("W4ABC", "test", max_count=5, window_seconds=60)
        self.assertTrue(ok)

    def test_separate_events(self):
        rl = RateLimiter()
        for _ in range(5):
            rl.check("W4ABC", "connect", max_count=5, window_seconds=60)
        # Different event type should have its own counter
        ok, _ = rl.check("W4ABC", "command", max_count=5, window_seconds=60)
        self.assertTrue(ok)

    def test_separate_callsigns(self):
        rl = RateLimiter()
        for _ in range(5):
            rl.check("W4ABC", "test", max_count=5, window_seconds=60)
        ok, _ = rl.check("N4XYZ", "test", max_count=5, window_seconds=60)
        self.assertTrue(ok)

    def test_default_rate_limits(self):
        """Using default RATE_LIMITS dict."""
        rl = RateLimiter()
        ok, _ = rl.check("W4ABC", "connect")
        self.assertTrue(ok)

    def test_multiplier_setter(self):
        rl = RateLimiter()
        rl.multiplier = 0.5
        self.assertEqual(rl.multiplier, 0.5)
        # Minimum clamp
        rl.multiplier = 0.01
        self.assertEqual(rl.multiplier, 0.1)


# ═════════════════════════════════════════════════════════════════════════════
# Phase 2: Mode Policy
# ═════════════════════════════════════════════════════════════════════════════

class TestModePolicy(unittest.TestCase):
    """Operational mode policy encapsulation."""

    def test_restricted_defaults(self):
        config = _make_config(operational_mode="restricted")
        policy = ModePolicy(config)
        self.assertEqual(policy.mode, "restricted")
        self.assertTrue(policy.require_registration)
        self.assertFalse(policy.auto_approve_on_connect)
        self.assertFalse(policy.require_password)
        self.assertEqual(policy.min_upload_role, "TRUSTED")
        self.assertEqual(policy.rate_limit_multiplier, 1.0)

    def test_open_mode(self):
        config = _make_config(operational_mode="open")
        policy = ModePolicy(config)
        self.assertEqual(policy.mode, "open")
        self.assertFalse(policy.require_registration)
        self.assertTrue(policy.auto_approve_on_connect)
        self.assertEqual(policy.min_upload_role, "APPROVED")
        self.assertEqual(policy.rate_limit_multiplier, 2.0)

    def test_emcomm_mode(self):
        config = _make_config(operational_mode="emcomm")
        policy = ModePolicy(config)
        self.assertEqual(policy.mode, "emcomm")
        self.assertTrue(policy.require_registration)
        self.assertFalse(policy.auto_approve_on_connect)
        self.assertTrue(policy.require_password)
        self.assertEqual(policy.min_upload_role, "SYSOP")
        self.assertEqual(policy.min_bulletin_post_role, "SYSOP")
        self.assertEqual(policy.rate_limit_multiplier, 0.25)

    def test_emcomm_session_timeout_cap(self):
        config = _make_config(operational_mode="emcomm")
        config.bbs.session_timeout = 600
        policy = ModePolicy(config)
        self.assertEqual(policy.session_timeout, 120)  # capped

    def test_restricted_session_timeout_passthrough(self):
        config = _make_config(operational_mode="restricted")
        config.bbs.session_timeout = 300
        policy = ModePolicy(config)
        self.assertEqual(policy.session_timeout, 300)

    def test_restricted_require_registration_false(self):
        """Config checkbox should work in restricted mode."""
        config = _make_config(
            operational_mode="restricted",
            require_registration=False,
        )
        policy = ModePolicy(config)
        self.assertFalse(policy.require_registration)
        self.assertTrue(policy.auto_approve_on_connect)

    def test_emcomm_ignores_require_registration_false(self):
        """EMCOMM always requires registration regardless of config."""
        config = _make_config(
            operational_mode="emcomm",
            require_registration=False,
        )
        policy = ModePolicy(config)
        self.assertTrue(policy.require_registration)
        self.assertFalse(policy.auto_approve_on_connect)

    def test_hot_reload(self):
        config1 = _make_config(operational_mode="restricted")
        policy = ModePolicy(config1)
        self.assertEqual(policy.mode, "restricted")

        config2 = _make_config(operational_mode="emcomm")
        policy.update_config(config2)
        self.assertEqual(policy.mode, "emcomm")

    def test_beacon_suffix(self):
        config = _make_config(operational_mode="emcomm")
        policy = ModePolicy(config)
        self.assertEqual(policy.beacon_suffix, " EMCOMM")

        config2 = _make_config(operational_mode="restricted")
        policy2 = ModePolicy(config2)
        self.assertEqual(policy2.beacon_suffix, "")

    def test_show_sysop_in_banner(self):
        config = _make_config(operational_mode="open")
        policy = ModePolicy(config)
        self.assertTrue(policy.show_sysop_in_banner)

        config2 = _make_config(operational_mode="restricted")
        policy2 = ModePolicy(config2)
        self.assertFalse(policy2.show_sysop_in_banner)

    def test_auto_block_threshold(self):
        config = _make_config(operational_mode="emcomm", auto_block_threshold=20)
        policy = ModePolicy(config)
        self.assertEqual(policy.auto_block_threshold, 5)  # EMCOMM override

        config2 = _make_config(operational_mode="open", auto_block_threshold=20)
        policy2 = ModePolicy(config2)
        self.assertEqual(policy2.auto_block_threshold, 50)  # open override

        config3 = _make_config(operational_mode="restricted", auto_block_threshold=30)
        policy3 = ModePolicy(config3)
        self.assertEqual(policy3.auto_block_threshold, 30)  # config passthrough


# ═════════════════════════════════════════════════════════════════════════════
# Phase 2: File Content Validation (MIME / Magic Bytes)
# ═════════════════════════════════════════════════════════════════════════════

class TestFileContentValidation(unittest.TestCase):
    """MIME magic-byte and executable detection tests."""

    def test_valid_png(self):
        data = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
        ok, _ = _validate_file_content("image.png", data)
        self.assertTrue(ok)

    def test_invalid_png(self):
        data = b"This is not a PNG" + b"\x00" * 100
        ok, reason = _validate_file_content("fake.png", data)
        self.assertFalse(ok)
        self.assertIn("does not match", reason)

    def test_valid_jpg(self):
        data = b"\xff\xd8\xff" + b"\x00" * 100
        ok, _ = _validate_file_content("photo.jpg", data)
        self.assertTrue(ok)

    def test_valid_pdf(self):
        data = b"%PDF-1.4" + b"\x00" * 100
        ok, _ = _validate_file_content("doc.pdf", data)
        self.assertTrue(ok)

    def test_invalid_pdf(self):
        data = b"Not a PDF" + b"\x00" * 100
        ok, reason = _validate_file_content("fake.pdf", data)
        self.assertFalse(ok)

    def test_valid_zip(self):
        data = b"PK\x03\x04" + b"\x00" * 100
        ok, _ = _validate_file_content("archive.zip", data)
        self.assertTrue(ok)

    def test_executable_blocked(self):
        for ext in [".exe", ".bat", ".cmd", ".ps1", ".sh", ".vbs",
                    ".msi", ".dll", ".com", ".scr", ".js", ".jar"]:
            ok, reason = _validate_file_content(f"file{ext}", b"\x00" * 10)
            self.assertFalse(ok, f"{ext} should be blocked")
            self.assertIn("Executable", reason)

    def test_pe_header_in_non_exe(self):
        data = b"MZ" + b"\x00" * 100  # PE header
        ok, reason = _validate_file_content("sneaky.txt", data)
        self.assertFalse(ok)
        self.assertIn("executable content", reason)

    def test_unknown_extension_allowed(self):
        data = b"Some random data"
        ok, _ = _validate_file_content("file.dat", data)
        self.assertTrue(ok)

    def test_empty_data_skips_magic(self):
        ok, _ = _validate_file_content("file.png", b"")
        # Empty data should not fail magic check (it's handled by size limits)
        self.assertTrue(ok)

    def test_txt_file_allowed(self):
        ok, _ = _validate_file_content("readme.txt", b"Hello world")
        self.assertTrue(ok)

    def test_valid_gif87a(self):
        data = b"GIF87a" + b"\x00" * 100
        ok, _ = _validate_file_content("anim.gif", data)
        self.assertTrue(ok)

    def test_valid_gif89a(self):
        data = b"GIF89a" + b"\x00" * 100
        ok, _ = _validate_file_content("anim.gif", data)
        self.assertTrue(ok)


# ═════════════════════════════════════════════════════════════════════════════
# Integration: SecurityConfig in AppConfig
# ═════════════════════════════════════════════════════════════════════════════

class TestSecurityConfig(unittest.TestCase):
    """Verify SecurityConfig model validation."""

    def test_defaults(self):
        sc = SecurityConfig()
        self.assertEqual(sc.operational_mode, "restricted")
        self.assertFalse(sc.require_password)
        self.assertTrue(sc.require_registration)
        self.assertEqual(sc.auth_failure_threshold, 5)
        self.assertEqual(sc.auth_lockout_minutes, 60)
        self.assertEqual(sc.auto_block_threshold, 20)
        self.assertEqual(sc.machine_secret, "")

    def test_mode_validation(self):
        """Only restricted/open/emcomm should be valid."""
        sc = SecurityConfig(operational_mode="open")
        self.assertEqual(sc.operational_mode, "open")
        sc2 = SecurityConfig(operational_mode="emcomm")
        self.assertEqual(sc2.operational_mode, "emcomm")

    def test_appconfig_has_security(self):
        config = AppConfig()
        self.assertIsInstance(config.security, SecurityConfig)


# ═════════════════════════════════════════════════════════════════════════════
# Thread Safety
# ═════════════════════════════════════════════════════════════════════════════

class TestThreadSafety(unittest.TestCase):
    """Verify concurrent access doesn't corrupt state."""

    def test_concurrent_role_changes(self):
        ac, _ = _make_controller()
        errors = []

        def set_roles(prefix, role):
            try:
                for i in range(50):
                    call = f"{prefix}{i}"
                    ac.set_role(call, role, changed_by="THREAD")
                    got = ac.get_role(call)
                    if got != role:
                        errors.append(f"{call}: expected {role}, got {got}")
            except Exception as e:
                errors.append(str(e))

        threads = [
            threading.Thread(target=set_roles, args=("AA", Role.APPROVED)),
            threading.Thread(target=set_roles, args=("BB", Role.TRUSTED)),
            threading.Thread(target=set_roles, args=("CC", Role.BLOCKED)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        self.assertEqual(errors, [])

    def test_concurrent_rate_limiter(self):
        rl = RateLimiter()
        results = []

        def check_many(call):
            for _ in range(100):
                ok, _ = rl.check(call, "test", max_count=1000, window_seconds=60)
                results.append(ok)

        threads = [
            threading.Thread(target=check_many, args=(f"CALL{i}",))
            for i in range(5)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        # All should succeed (1000 limit per call, only 100 attempts each)
        self.assertTrue(all(results))


# ═════════════════════════════════════════════════════════════════════════════
# Permission Matrix Exhaustive Check
# ═════════════════════════════════════════════════════════════════════════════

class TestPermissionMatrixCompleteness(unittest.TestCase):
    """Verify every command in HELP_TEXT has a permission entry."""

    def test_all_commands_have_permissions(self):
        # Extract command prefixes from COMMAND_PERMISSIONS
        defined_cmds = set(COMMAND_PERMISSIONS.keys())
        # These commands should be in the matrix
        expected = {"H", "?", "HELP", "L", "R", "S", "K", "A", "U", "N",
                    "LB", "RB", "SB", "KB", "BA",
                    "FA", "FL", "FG", "FP", "FD",
                    "MH", "I", "P", "CH",
                    "B", "73", "BYE", "QUIT", "EXIT",
                    "REGISTER", "REG"}
        for cmd in expected:
            self.assertIn(cmd, defined_cmds,
                          f"Command {cmd!r} missing from COMMAND_PERMISSIONS")


if __name__ == "__main__":
    unittest.main()
