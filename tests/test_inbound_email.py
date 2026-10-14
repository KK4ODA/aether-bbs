"""
tests/test_inbound_email.py

Inbound email (delivery/inbound.py): the same email fetched twice is stored
once, and an IMAP message that fails to process is left unread to be
fetched again.  The mail servers are faked.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.config import AppConfig
from core.events import EventBus, EventType
from bbs.store import MessageStore
import delivery.inbound as inbound
from delivery.inbound import InboundEmailPoller

RAW = (b"From: Ann <ann@example.org>\r\nTo: kk4oda@bbs.example.net\r\n"
       b"Subject: hello\r\nMessage-ID: <abc-123@example.org>\r\n\r\nbody text\r\n")


@pytest.fixture
def poller(tmp_path):
    cfg = AppConfig()
    cfg.bbs.message_dir = str(tmp_path / "messages")
    store = MessageStore(cfg)
    store.initialize()
    from bbs.bulletins import BulletinStore
    BulletinStore(store)          # adds is_bulletin, as at startup
    bus = EventBus()
    seen = []
    bus.subscribe(EventType.INBOUND_EMAIL_RECEIVED, seen.append)
    p = InboundEmailPoller(store, bus)
    p.update_config(enabled=True, protocol="IMAP", host="mail.invalid",
                    bbs_domain="bbs.example.net")
    return p, store, seen


def test_same_email_twice_is_stored_once(poller):
    p, store, seen = poller
    assert p._process_email(RAW) is True
    assert p._process_email(RAW) is True          # e.g. POP3 mailbox kept
    msgs = store.list_messages(to_call="KK4ODA")
    assert len(msgs) == 1 and msgs[0]["subject"] == "hello"
    assert len(seen) == 1                         # announced once


class FakeIMAP:
    def __init__(self, fail):
        self.fail = fail
        self.flags = []
        self.fetch_spec = None

    def login(self, *a): return "OK", []
    def select(self, *a): return "OK", [b"1"]
    def search(self, *a): return "OK", [b"1"]
    def store(self, msg_id, op, flag): self.flags.append(flag)
    def expunge(self): pass
    def logout(self): pass

    def fetch(self, msg_id, spec):
        self.fetch_spec = spec
        if self.fail:
            raise OSError("connection reset mid-fetch")
        return "OK", [(b"1", RAW)]


@pytest.mark.parametrize("fail", [False, True])
def test_imap_marks_read_only_what_it_handled(poller, monkeypatch, fail):
    p, store, _ = poller
    fake = FakeIMAP(fail)
    monkeypatch.setattr(inbound.imaplib, "IMAP4_SSL", lambda *a, **k: fake)
    p._poll_imap()
    assert "PEEK" in fake.fetch_spec              # fetching alone marks nothing read
    if fail:
        assert fake.flags == []                   # left unread: fetched again next poll
    else:
        assert fake.flags == ["\\Seen"]
        assert store.list_messages(to_call="KK4ODA")


def _timers():
    import threading
    # a cancelled timer's thread lingers until it wakes: count live ones
    return [t for t in threading.enumerate()
            if isinstance(t, threading.Timer) and t.is_alive()
            and not t.finished.is_set()]


def test_enabling_inbound_email_on_save_starts_polling(tmp_path):
    """The poller was started only if inbound email was on at startup, so
    turning it on in Settings did nothing until a restart."""
    from types import SimpleNamespace
    import main
    cfg = AppConfig()
    cfg.bbs.message_dir = str(tmp_path / "messages")
    store = MessageStore(cfg)
    store.initialize()
    app = main.BBSApplication.__new__(main.BBSApplication)
    app._inbound_applied = None
    app.inbound_email = InboundEmailPoller(store, EventBus())
    app._apply_internet_email(cfg)
    app.inbound_email.start()                       # as at startup: off
    assert app.inbound_email._timer is None

    on = cfg.model_copy(deep=True)
    on.outbound_delivery.inbound_email.enabled = True
    on.outbound_delivery.inbound_email.host = "imap.example.org"
    app._on_config_changed_email(SimpleNamespace(data={"config": on}))
    try:
        assert app.inbound_email._enabled and app.inbound_email._timer is not None
        assert app.inbound_email._host == "imap.example.org"
        timer = app.inbound_email._timer
        app._on_config_changed_email(SimpleNamespace(data={"config": on}))
        assert app.inbound_email._timer is timer     # unchanged: timer kept
        off = on.model_copy(deep=True)
        off.outbound_delivery.inbound_email.enabled = False
        app._on_config_changed_email(SimpleNamespace(data={"config": off}))
        assert app.inbound_email._timer is None
    finally:
        app.inbound_email.stop()


def test_a_config_change_during_a_poll_leaves_one_poll_timer(poller):
    p, _, _ = poller
    p.start()
    try:
        p.update_config(enabled=True, protocol="IMAP", host="mail.invalid")
        p._schedule_next()          # the running poll finishing afterwards
        mine = [t for t in _timers() if t.function == p._auto_poll_tick]
        assert len(mine) == 1
    finally:
        p.stop()
