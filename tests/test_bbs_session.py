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
