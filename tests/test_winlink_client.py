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
