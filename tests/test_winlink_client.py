"""
tests/test_winlink_client.py

PatClient (winlink/pat_client.py): reading Pat's inbox into the BBS, and
recovering the outbound queue.  Pat itself is faked.
"""

import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.config import AppConfig
from core.events import EventBus
from bbs.store import MessageStore, MessageStatus
import winlink.pat_client as pc
from winlink.pat_client import PatClient

INBOX = [{"MID": "ABC123DEF456", "Subject": "hello", "From": {"Addr": "W1AW@winlink.org"},
          "To": [{"Addr": "KK4ODA@winlink.org"}], "Body": "body text"}]


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)          # the seen-IDs file is kept in the cwd
    cfg = AppConfig()
    cfg.bbs.message_dir = str(tmp_path / "messages")
    store = MessageStore(cfg)
    store.initialize()
    c = PatClient(store, EventBus())
    c.update_config(enabled=False, callsign="KK4ODA", password="", grid_square="",
                    pat_exe_path="", pat_http_addr="127.0.0.1:1", auto_launch_pat=False,
                    forward_calls=[], cms_timeout=60)
    monkeypatch.setattr(pc, "urlopen",
                        lambda req, timeout=0: io.BytesIO(json.dumps(INBOX).encode()))
    return c, store


def test_inbox_message_not_stored_is_tried_again(client, monkeypatch):
    c, store = client
    real_save = store.save_message
    monkeypatch.setattr(store, "save_message",
                        lambda **kw: (_ for _ in ()).throw(OSError("disk full")))
    assert c._check_inbox() == 0
    monkeypatch.setattr(store, "save_message", real_save)
    assert c._check_inbox() == 1                 # not skipped as "seen"
    assert store.get_message("ABC123DEF456")["subject"] == "hello"
    assert c._check_inbox() == 0                 # and stored only once


def test_start_puts_stuck_sending_messages_back_in_the_queue(client):
    c, store = client
    mid = store.save_message("KK4ODA", "W1AW", "out", b"x")
    store.set_cms_status(mid, MessageStatus.SENDING)
    c.start()
    try:
        assert store.get_message(mid)["cms_status"] == MessageStatus.QUEUED
    finally:
        c.stop()


def test_pat_timeout_is_reported_not_crashed(client, monkeypatch):
    c, _ = client
    c._pat_exe_path = "/bin/true"
    def boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="pat", timeout=60)
    monkeypatch.setattr(pc.subprocess, "run", boom)
    monkeypatch.setattr(c, "_check_internet", lambda *a, **k: True)
    assert c._sync_cms() is False


def test_only_mail_sent_to_winlink_is_queued(client, tmp_path):
    """Every CMS poll used to queue any unsent message to a callsign other
    than the gateway's, then mark it sent: mail for local RF users,
    bulletins and inbound email left the BBS and vanished from RF pickup."""
    from bbs.bulletins import BulletinStore
    from winlink.queue import route_new_message
    c, store = client
    bulletins = BulletinStore(store)
    for_rf_user = store.save_message("W1AAA", "N0RF", "pick me up", b"x")
    emailed_in = store.save_message("bob@example.org", "N0RF", "email", b"x",
                                    area="INBOX", instance="email")
    bulletin = bulletins.save_bulletin("W1AAA", "ALL", "news", b"x", area="ALL")
    written_before = store.save_message("N0RF", "bob@example.org", "old", b"x")
    sw = store.save_message("N0RF", "W1AW", "via winlink", b"x")
    route_new_message(store, sw, "W1AW", via_winlink=True)
    local = store.save_message("KK4ODA", "N0RF", "local", b"x")
    route_new_message(store, local, "N0RF", via_winlink=False)

    queued = {m["id"] for m in c.outbound_queue.get_cms_outbound()}
    assert queued == {sw, written_before}
    for mid in (for_rf_user, emailed_in, bulletin, local):
        assert store.get_message(mid)["cms_status"] in ("", "LOCAL")


def test_queued_message_reaches_pat_compose(client, monkeypatch):
    """Bodies are stored as bytes; pat compose was handed bytes and every
    queued message failed with an AttributeError."""
    from winlink.queue import route_new_message
    c, store = client
    mid = store.save_message("KK4ODA", "W1AW", "hi", "héllo".encode())
    route_new_message(store, mid, "W1AW", via_winlink=True)
    fed = []

    class FakePat:
        returncode = 0
        def __init__(self, *a, **k): pass
        def communicate(self, input=None, timeout=None):
            fed.append(input)
            return b"", b""
    monkeypatch.setattr(pc.subprocess, "Popen", FakePat)
    c._pat_exe_path = sys.executable                  # any existing file
    assert c._send_queued_messages() == 1
    assert fed == ["héllo".encode()]
    assert store.get_message(mid)["cms_status"] == MessageStatus.SENT
