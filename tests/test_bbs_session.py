"""
tests/test_bbs_session.py

An RF user's BBS session (bbs/interactive.py), driven line by line: message
and bulletin numbering, and how the session ends when the link drops.
"""

import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.config import AppConfig
from core.events import EventBus
from bbs.bulletins import BulletinStore
from bbs.interactive import InteractiveSession
from bbs.store import MessageStore


@pytest.fixture
def bbs(tmp_path):
    cfg = AppConfig()
    cfg.bbs.message_dir = str(tmp_path / "messages")
    store = MessageStore(cfg)
    store.initialize()
    return cfg, store, BulletinStore(store)


class Session:
    """Run an InteractiveSession for *call* on a thread and talk to it."""

    def __init__(self, call, cfg, store, bulletins):
        self.out: list[str] = []
        self.s = InteractiveSession(call, "fm1", cfg, store, EventBus(),
                                    self.out.append, lambda: None,
                                    bulletin_store=bulletins)
        self.s._check_internet = lambda *a, **k: True
        self.t = threading.Thread(target=self.s.run, daemon=True)
        self.t.start()
        time.sleep(0.3)

    def cmd(self, line, wait=0.3):
        """Send *line*; return what the session sent back once it has
        been quiet for *wait* seconds (at most 10 s: CI runners are slow)."""
        mark = len(self.out)
        self.s.feed_line(line)
        seen, quiet_since, deadline = mark, time.time(), time.time() + 10
        while time.time() < deadline:
            time.sleep(0.05)
            if len(self.out) != seen:
                seen, quiet_since = len(self.out), time.time()
            elif time.time() - quiet_since >= wait and seen > mark:
                break
            elif time.time() - quiet_since >= max(wait, 1.0):
                break
        return "".join(self.out[mark:])

    def close(self):
        self.s.feed_line(None)
        self.t.join(timeout=5)


def test_read_cannot_open_another_users_private_message(bbs):
    cfg, store, bulletins = bbs
    victim = store.save_message("W1AAA", "N0VIC", "private", b"TOP SECRET BODY")
    mine = store.save_message("W1AAA", "W1XYZ", "hello", b"for W1XYZ")
    u = Session("W1XYZ", cfg, store, bulletins)
    try:
        listing = u.cmd("L")
        assert "hello" in listing and "private" not in listing
        assert "for W1XYZ" in u.cmd("R 1")
        assert "Message not found" in u.cmd("R 2")
        assert "TOP SECRET" not in "".join(u.out)
        assert not store.get_message(victim)["is_read"]
        assert store.get_message(mine)["is_read"]
    finally:
        u.close()


def test_kill_deletes_the_listed_message(bbs):
    cfg, store, bulletins = bbs
    other = store.save_message("W1AAA", "N0VIC", "not yours", b"x")
    mine = store.save_message("W1AAA", "W1XYZ", "to W1XYZ", b"y")
    u = Session("W1XYZ", cfg, store, bulletins)
    try:
        u.cmd("L")
        assert "deleted" in u.cmd("K 1")
        assert store.get_message(mine) is None          # the one L showed
        assert store.get_message(other) is not None     # not someone else's
    finally:
        u.close()


def test_bulletin_numbers_follow_the_area_listed(bbs):
    cfg, store, bulletins = bbs
    bulletins.save_bulletin("W1AAA", "ALL", "weather", b"WX BODY", area="WX")
    time.sleep(1.1)   # newer, so first in an unfiltered list
    bulletins.save_bulletin("W1AAA", "ALL", "general news", b"GEN BODY", area="GENERAL")
    u = Session("W1XYZ", cfg, store, bulletins)
    try:
        listing = u.cmd("LB WX")
        assert "weather" in listing and "general news" not in listing
        assert "WX BODY" in u.cmd("RB 1")
    finally:
        u.close()


def test_link_drop_in_a_nested_prompt_ends_the_session_at_once(bbs):
    """A drop while the user is typing a message used to leave the session
    (and the BBS's only session slot) waiting out the 10-minute timeout,
    or crash it on the disconnect marker."""
    cfg, store, bulletins = bbs
    u = Session("W1XYZ", cfg, store, bulletins)
    u.cmd("S W1AAA")
    u.cmd("Subject line")
    u.cmd("first line of the body")
    u.s.feed_line(None)                      # RF link drops mid-message
    u.t.join(timeout=5)
    assert not u.t.is_alive()
    assert store.list_messages(to_call="W1AAA") == []   # nothing half-saved


def test_link_drop_during_chat_ends_the_session(bbs):
    cfg, store, bulletins = bbs
    from bbs.interactive import _SYSOP_CHAT_INITIATE
    u = Session("W1XYZ", cfg, store, bulletins)
    u.s._rx_queue.put(_SYSOP_CHAT_INITIATE)      # the sysop opens a chat
    time.sleep(0.5)
    u.s.feed_line(None)                          # RF link drops during it
    u.t.join(timeout=8)
    assert not u.t.is_alive()


def test_winlink_client_is_told_there_is_no_b2f_and_disconnected(bbs):
    """B2F was removed (a real client, Pat, could not exchange mail with
    it).  A Winlink client's banner gets a B2F-style error and a hang-up
    instead of the BBS prompt."""
    cfg, store, bulletins = bbs
    store.save_message("W1AAA", "W1XYZ", "for you", b"your mail")
    u = Session("W1XYZ", cfg, store, bulletins)
    out = u.cmd("[RMS Express-1.7.10.0-B2FHM$]")
    u.t.join(timeout=5)
    assert not u.t.is_alive()
    assert "*** This BBS does not offer Winlink (B2F)" in out
    assert len(store.get_messages_for("W1XYZ")) == 1        # still waiting


def test_s_holds_for_rf_and_sw_queues_for_winlink(bbs):
    cfg, store, bulletins = bbs
    u = Session("W1XYZ", cfg, store, bulletins)
    try:
        for cmd, to in (("S N0RF", "N0RF"), ("SW W1AW", "W1AW")):
            u.cmd(cmd)
            u.cmd("subject")
            u.cmd("body")
            u.cmd(".")
        status = {to: store.get_message(r["id"])["cms_status"]
                  for to in ("N0RF", "W1AW")
                  for r in store.list_messages(to_call=to)}
        assert status == {"N0RF": "LOCAL", "W1AW": "QUEUED"}
    finally:
        u.close()


def _secured(cfg, store, mode):
    """Access control, mode policy and rate limiter as main.py wires them."""
    from bbs.access import AccessController
    from bbs.mode_policy import ModePolicy
    from bbs.rate_limiter import RateLimiter
    cfg.security.operational_mode = mode
    cfg.security.require_password = False
    access = AccessController(store.conn, store.lock, cfg)
    policy = ModePolicy(cfg)
    return access, policy, RateLimiter(policy.rate_limit_multiplier)


class SecuredSession(Session):
    """A session for *call*; EMCOMM always asks for a password, so a user
    with one set is logged in with it (plaintext fallback)."""

    def __init__(self, call, cfg, store, bulletins, access, policy, limiter,
                 password=None):
        self.out = []
        self.s = InteractiveSession(call, "fm1", cfg, store, EventBus(),
                                    self.out.append, lambda: None,
                                    bulletin_store=bulletins,
                                    access_controller=access, mode_policy=policy,
                                    rate_limiter=limiter)
        self.s._check_internet = lambda *a, **k: True
        self.t = threading.Thread(target=self.s.run, daemon=True)
        self.t.start()
        time.sleep(0.3)
        if password:
            assert ";AUTH OK" in self.cmd(password)


def _post(u, group="ALL"):
    out = u.cmd(f"SB {group}")
    if "denied" in out.lower() or "too many" in out.lower():
        return out
    u.cmd("subject")
    u.cmd("body")
    return out + u.cmd(".")


@pytest.mark.parametrize("mode,may_post", [("emcomm", False), ("restricted", True),
                                          ("open", True)])
def test_bulletin_posting_follows_the_mode(bbs, mode, may_post):
    """EMCOMM is sysop-only posting; it used to let any TRUSTED user post.
    OPEN lets APPROVED users post, as documented."""
    from bbs.access import Role
    cfg, store, bulletins = bbs
    access, policy, limiter = _secured(cfg, store, mode)
    role = Role.APPROVED if mode == "open" else Role.TRUSTED
    access.set_role("W1XYZ", role)
    access.set_password("W1XYZ", "secret")
    u = SecuredSession("W1XYZ", cfg, store, bulletins, access, policy, limiter,
                       password="secret")
    try:
        out = _post(u)
        assert ("Permission denied" in out) is not may_post
        assert (len(bulletins.list_bulletins()) == 1) is may_post
    finally:
        u.close()


def test_emcomm_limits_how_many_messages_a_station_sends(bbs):
    """message_send is 20 an hour, a quarter of that in EMCOMM; it was
    never checked."""
    from bbs.access import Role
    cfg, store, bulletins = bbs
    access, policy, limiter = _secured(cfg, store, "emcomm")
    access.set_role("W1XYZ", Role.APPROVED)
    access.set_password("W1XYZ", "secret")
    u = SecuredSession("W1XYZ", cfg, store, bulletins, access, policy, limiter,
                       password="secret")
    try:
        for _ in range(5):
            u.cmd("S N0RF")
            u.cmd("s")
            u.cmd(".")
        assert "too many messages" in u.cmd("S N0RF")
        assert len(store.list_messages(to_call="N0RF")) == 5
    finally:
        u.close()


def test_emcomm_auto_blocks_sooner(bbs):
    from bbs.access import Role
    cfg, store, _ = bbs
    access, policy, _ = _secured(cfg, store, "emcomm")
    cfg.security.auto_block_threshold = 20
    cfg.security.auth_failure_threshold = 50    # no temporary lockout first
    access.set_role("W1XYZ", Role.APPROVED)
    for _ in range(5):
        access.record_auth_failure("W1XYZ")
    allowed, _ = access.check_lockout("W1XYZ", policy.auto_block_threshold)
    assert not allowed
    assert access.get_role("W1XYZ") == Role.BLOCKED


def test_repeated_password_failures_are_refused_before_the_challenge(bbs):
    from bbs.access import Role
    cfg, store, bulletins = bbs
    access, policy, limiter = _secured(cfg, store, "emcomm")
    access.set_role("W1XYZ", Role.APPROVED)
    access.set_password("W1XYZ", "secret")
    limiter.check("W1XYZ", "auth_failure")        # EMCOMM allows one an hour
    u = SecuredSession("W1XYZ", cfg, store, bulletins, access, policy, limiter)
    try:
        u.t.join(timeout=5)
        text = "".join(u.out)
        assert "Too many failed authentication attempts" in text
        assert ";AUTH HMAC" not in text
    finally:
        u.close()
