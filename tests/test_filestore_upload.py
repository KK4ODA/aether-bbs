"""
tests/test_filestore_upload.py

File uploads land only in registered file areas under files/.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.config import AppConfig
from bbs.filestore import FileStore


@pytest.fixture
def fs(tmp_path):
    cfg = AppConfig()
    cfg.bbs.message_dir = str(tmp_path / "data" / "messages")
    cfg.file_upload.allow_remote_uploads = True
    cfg.file_upload.permission_mode = "open"
    store = FileStore(cfg)
    store.initialize()
    return store, tmp_path


@pytest.mark.parametrize("area", ["../ESCAPE", "/ESCAPE", "..", "NOSUCHAREA"])
@pytest.mark.parametrize("remote", [True, False])
def test_upload_outside_a_registered_area_is_refused(fs, area, remote):
    store, root = fs
    ok, reason = store.commit_upload(area, "note.txt", "N0CALL", "", b"hello\n",
                                     is_remote=remote)
    assert not ok and "Unknown file area" in reason
    written = [p for p in root.rglob("note.txt")]
    assert written == []
    assert not Path("/ESCAPE").exists()


def test_upload_to_a_registered_area_works(fs):
    store, root = fs
    ok, reason = store.commit_upload("GENERAL", "note.txt", "N0CALL", "", b"hello\n",
                                     is_remote=True)
    assert ok, reason
    assert (root / "data" / "files" / "GENERAL" / "note.txt").read_bytes() == b"hello\n"
