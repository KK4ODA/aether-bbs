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
        mark = len(self.out)
        self.s.feed_line(line)
        time.sleep(wait)
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


def _b2f(store, link_call, banner, replies):
    from bbs.b2f import B2FSession
    it = iter(replies)
    out = []
    s = B2FSession("BBS", store, out.append, lambda t: next(it, None),
                   lambda n, t: b"", send_raw_fn=out.append, remote_call=link_call)
    s.run(first_line=banner)
    return out


def test_b2f_sends_mail_of_the_linked_station_not_the_banner(bbs):
    """Typing another station's callsign in the client banner must not
    fetch (and mark sent) that station's private mail."""
    cfg, store, _ = bbs
    victim = store.save_message("W1AAA", "N0VIC", "secret", b"private body")
    out = _b2f(store, "W1XYZ-7", "[N0VIC-1.0-B2FHM$]", [";PR: 0", "FS +"])
    assert not any(isinstance(o, bytes) and b"private" in o for o in out)
    assert not store.get_message(victim)["is_sent"]


def test_b2f_real_client_banner_gets_its_own_mail(bbs):
    """A real Winlink client's banner ("[RMS Express-...]") used to parse as
    the callsign "RMS EXPRESS", so the station never got its mail."""
    cfg, store, _ = bbs
    mine = store.save_message("W1AAA", "W1XYZ", "for you", b"your mail")
    out = _b2f(store, "W1XYZ-7", "[RMS Express-1.7-B2FHM$]", [";PR: 0", "FS +"])
    assert any(isinstance(o, str) and mine in o for o in out)    # proposed to W1XYZ
