"""
bbs/filestore.py

SQLite + filesystem-backed file library.
Shared across all instances.

Files are stored on disk under <file_dir>/<area>/<filename>.
Metadata (area, description, uploader, dates, download count) lives in SQLite.
"""

import sqlite3
import hashlib
import logging
import threading
from pathlib import Path
from typing import Optional
from datetime import datetime, timezone

from core.config import AppConfig, ConfigManager
from core.events import EventBus, EventType

log = logging.getLogger(__name__)


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS file_areas (
    name        TEXT PRIMARY KEY,
    description TEXT NOT NULL DEFAULT '',
    enabled     INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS files (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    filename     TEXT    NOT NULL,
    area         TEXT    NOT NULL REFERENCES file_areas(name),
    description  TEXT    NOT NULL DEFAULT '',
    uploader     TEXT    NOT NULL DEFAULT '',
    size_bytes   INTEGER NOT NULL DEFAULT 0,
    sha256       TEXT    NOT NULL DEFAULT '',
    uploaded_at  TEXT    NOT NULL,
    last_dl_at   TEXT,
    dl_count     INTEGER NOT NULL DEFAULT 0,
    UNIQUE(area, filename)
);

CREATE INDEX IF NOT EXISTS idx_files_area ON files(area);
CREATE INDEX IF NOT EXISTS idx_files_uploader ON files(uploader);
"""

DEFAULT_AREAS = [
    ("GENERAL",  "General files"),
    ("SOFTWARE", "Software and utilities"),
    ("DOCS",     "Documentation and manuals"),
    ("WEATHER",  "Weather data and maps"),
    ("MAPS",     "Maps and geographic data"),
]

# ── Schema migration ──────────────────────────────────────────────────────────

_MIGRATIONS = [
    # Add allow_remote_upload column (silent if already exists)
    "ALTER TABLE file_areas ADD COLUMN allow_remote_upload INTEGER NOT NULL DEFAULT 1",
]


# ── Virus scanner ─────────────────────────────────────────────────────────────

def _find_defender() -> str | None:
    """
    Return the full path to MpCmdRun.exe on Windows, or None if not found.
    Checks the standard locations for Windows 10 / 11 / Server 2016+.
    """
    import os
    candidates = [
        r"C:\Program Files\Windows Defender\MpCmdRun.exe",
        r"C:\Program Files (x86)\Windows Defender\MpCmdRun.exe",
    ]
    # Also try the ProgramFiles env var in case of non-standard install
    pf = os.environ.get("ProgramFiles", "")
    if pf:
        candidates.append(os.path.join(pf, "Windows Defender", "MpCmdRun.exe"))

    for path in candidates:
        if os.path.isfile(path):
            return path
    return None


def _virus_scan(data: bytes, config) -> tuple[bool, str]:
    """
    Scan bytes for malware using the platform's built-in security scanner.

    Windows  : Windows Defender (MpCmdRun.exe) — ships with every Windows 10/11.
    macOS    : No public CLI for XProtect — scanning is skipped (logs a warning).
    Linux    : No built-in AV — scanning is skipped (logs a warning).

    Returns (is_clean, threat_name).
    On scanner unavailability the file is allowed through (fail-open) and a
    warning is emitted so the sysop knows scanning is not active.

    Windows Defender exit codes:
      0  = no threats found
      2  = threats found
      other = error (treated as inconclusive — allow + log)
    """
    import sys, subprocess, tempfile, os

    fu = config.file_upload
    if not fu.virus_scan_enabled:
        return True, ""

    if sys.platform != "win32":
        log.warning(
            "Virus scanning is enabled but is only supported on Windows "
            "(Windows Defender). Allowing file through."
        )
        return True, ""

    defender = _find_defender()
    if not defender:
        log.warning(
            "Windows Defender (MpCmdRun.exe) not found. "
            "Ensure Windows Security is installed. Allowing file through."
        )
        return True, ""

    timeout = fu.virus_scan_timeout

    tmp = tempfile.NamedTemporaryFile(
        prefix="bbs_scan_", suffix=".bin", delete=False
    )
    try:
        tmp.write(data)
        tmp.flush()
        tmp_path = tmp.name
    finally:
        tmp.close()

    try:
        result = subprocess.run(
            [defender, "-Scan", "-ScanType", "3", "-File", tmp_path,
             "-DisableRemediation"],
            capture_output=True,
            timeout=timeout,
            text=True,
            creationflags=0x08000000,  # CREATE_NO_WINDOW
        )
        if result.returncode == 0:
            return True, ""
        elif result.returncode == 2:
            # Defender found a threat — parse the threat name from stdout
            threat = "unknown"
            for line in result.stdout.splitlines():
                # Defender output format: "Threat                  : ThreatName"
                if "Threat" in line and ":" in line:
                    threat = line.split(":", 1)[-1].strip()
                    break
            log.warning(f"Windows Defender: INFECTED — {threat}")
            return False, threat
        else:
            log.warning(
                f"Windows Defender returned code {result.returncode}; "
                f"stderr: {result.stderr[:200]!r} — allowing file through"
            )
            return True, ""
    except subprocess.TimeoutExpired:
        log.warning(f"Windows Defender scan timed out after {timeout}s — allowing through")
        return True, ""
    except Exception as e:
        log.warning(f"Windows Defender scan error: {e} — allowing through")
        return True, ""
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


# Maximum file size accepted via RF (5 MB default)
MAX_FILE_BYTES = 5 * 1024 * 1024

# ── Content validation ────────────────────────────────────────────────────────

# Magic bytes for common file types
_MAGIC_BYTES: dict[str, list[bytes]] = {
    ".zip":  [b"PK\x03\x04", b"PK\x05\x06"],
    ".gz":   [b"\x1f\x8b"],
    ".pdf":  [b"%PDF"],
    ".jpg":  [b"\xff\xd8\xff"],
    ".jpeg": [b"\xff\xd8\xff"],
    ".png":  [b"\x89PNG\r\n\x1a\n"],
    ".gif":  [b"GIF87a", b"GIF89a"],
    ".xlsx": [b"PK\x03\x04"],
    ".docx": [b"PK\x03\x04"],
    ".mp3":  [b"ID3", b"\xff\xfb", b"\xff\xf3"],
    ".wav":  [b"RIFF"],
}

# Extensions that must NEVER be accepted
_NEVER_EXECUTABLE = {
    ".exe", ".bat", ".cmd", ".ps1", ".sh", ".vbs", ".msi", ".dll",
    ".com", ".scr", ".pif", ".hta", ".js", ".wsh", ".wsf", ".jar",
    ".py", ".rb", ".pl", ".cpl", ".inf", ".reg", ".lnk",
}


def _validate_file_content(filename: str, data: bytes) -> tuple[bool, str]:
    """
    Validate file content against claimed extension.
    Returns (is_valid, reason).
    """
    ext = Path(filename).suffix.lower()

    # Block executable extensions unconditionally
    if ext in _NEVER_EXECUTABLE:
        return False, f"Executable file type {ext!r} is not permitted."

    # Check magic bytes if we have a signature for this type
    if ext in _MAGIC_BYTES and data:
        signatures = _MAGIC_BYTES[ext]
        if not any(data[:len(sig)] == sig for sig in signatures):
            return False, (
                f"File content does not match expected {ext} format. "
                "Possible extension mismatch."
            )

    # Check for embedded PE executable in non-exe files
    if len(data) >= 2 and data[:2] == b"MZ":
        return False, "File contains Windows executable content."

    return True, ""


class FileStore:
    """
    Manages the BBS file library.
    Files live on disk; metadata in SQLite.
    """

    def __init__(self, config, event_bus: Optional[EventBus] = None,
                 access_controller=None):
        # Accept either AppConfig or ConfigManager for hot-reload support.
        if isinstance(config, ConfigManager):
            self._cm = config
            self._static_config = None
        else:
            self._cm = None
            self._static_config = config
        self._bus    = event_bus
        self._access = access_controller  # AccessController or None
        self._lock   = threading.Lock()
        self._conn:  Optional[sqlite3.Connection] = None

        # Root directory for file storage
        cfg = self._config
        base = Path(cfg.bbs.message_dir).parent
        self._root = base / "files"
        db_dir = Path(cfg.bbs.message_dir)
        db_dir.mkdir(parents=True, exist_ok=True)
        self._db_path = db_dir / "files.db"

    @property
    def _config(self) -> AppConfig:
        """Return the current config, supporting hot reload via ConfigManager."""
        if self._cm is not None:
            return self._cm.current() or AppConfig()
        return self._static_config

    def initialize(self):
        self._conn = sqlite3.connect(
            str(self._db_path),
            check_same_thread=False,
        )
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()
            self._seed_default_areas()
        self._apply_migrations()
        self._root.mkdir(parents=True, exist_ok=True)
        self._cleanup_orphaned_staging()
        log.info(f"FileStore initialised at {self._db_path}, root={self._root}")

    def _cleanup_orphaned_staging(self):
        """Remove leftover files from the staging directory on startup.

        These are partial uploads from sessions that crashed or disconnected
        before commit_upload() could atomic-rename them to the final location.
        """
        staging_dir = self._root / ".staging"
        if not staging_dir.exists():
            return
        removed = 0
        for path in staging_dir.iterdir():
            if path.is_file():
                try:
                    path.unlink()
                    removed += 1
                except OSError as e:
                    log.warning(f"FileStore: failed to remove orphaned staging file {path}: {e}")
        if removed:
            log.info(f"FileStore: cleaned up {removed} orphaned staging file(s)")

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None

    def _apply_migrations(self):
        with self._lock:
            for sql in _MIGRATIONS:
                try:
                    self._conn.execute(sql)
                    self._conn.commit()
                except Exception:
                    pass

    # ── Area remote-upload toggle ─────────────────────────────────────

    def set_area_remote_upload(self, area: str, allowed: bool) -> bool:
        try:
            with self._lock:
                self._conn.execute(
                    "UPDATE file_areas SET allow_remote_upload=? WHERE name=?",
                    (1 if allowed else 0, area.upper()),
                )
                self._conn.commit()
            return True
        except Exception as e:
            log.error(f"set_area_remote_upload error: {e}")
            return False

    def get_area_remote_upload(self, area: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "SELECT COALESCE(allow_remote_upload, 1) FROM file_areas WHERE name=?",
                (area.upper(),),
            )
            row = cur.fetchone()
            return bool(row[0]) if row else True

    def callsign_upload_count(self, callsign: str) -> int:
        with self._lock:
            cur = self._conn.execute(
                "SELECT COUNT(*) FROM files WHERE uploader=?",
                (callsign.upper(),),
            )
            return cur.fetchone()[0]

    # ── Areas ─────────────────────────────────────────────────────────────

    def _seed_default_areas(self):
        """Insert default areas if they don't already exist."""
        now = _utcnow()
        for name, desc in DEFAULT_AREAS:
            self._conn.execute(
                """INSERT OR IGNORE INTO file_areas (name, description, created_at)
                   VALUES (?, ?, ?)""",
                (name, desc, now),
            )
        self._conn.commit()

    def list_areas(self) -> list[dict]:
        with self._lock:
            cur = self._conn.execute(
                """SELECT fa.name, fa.description,
                          COALESCE(fa.allow_remote_upload, 1) AS allow_remote_upload,
                          COUNT(f.id) AS file_count
                   FROM file_areas fa
                   LEFT JOIN files f ON f.area = fa.name
                   WHERE fa.enabled = 1
                   GROUP BY fa.name
                   ORDER BY fa.name"""
            )
            return [dict(r) for r in cur.fetchall()]

    def area_exists(self, area: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "SELECT 1 FROM file_areas WHERE name=? AND enabled=1",
                (area.upper(),),
            )
            return cur.fetchone() is not None

    def create_area(self, name: str, description: str = "") -> bool:
        try:
            with self._lock:
                self._conn.execute(
                    """INSERT OR IGNORE INTO file_areas (name, description, created_at)
                       VALUES (?, ?, ?)""",
                    (name.upper(), description, _utcnow()),
                )
                self._conn.commit()
            area_dir = self._root / name.upper()
            area_dir.mkdir(parents=True, exist_ok=True)
            return True
        except Exception as e:
            log.error(f"FileStore: create_area error: {e}")
            return False

    # ── File listing ──────────────────────────────────────────────────────

    def list_files(self, area: Optional[str] = None) -> list[sqlite3.Row]:
        with self._lock:
            if area:
                cur = self._conn.execute(
                    """SELECT * FROM files WHERE area=?
                       ORDER BY uploaded_at DESC""",
                    (area.upper(),),
                )
            else:
                cur = self._conn.execute(
                    "SELECT * FROM files ORDER BY area, filename"
                )
            return cur.fetchall()

    def get_file_meta(self, area: str, filename: str) -> Optional[sqlite3.Row]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM files WHERE area=? AND filename=?",
                (area.upper(), filename),
            )
            return cur.fetchone()

    def file_exists(self, area: str, filename: str) -> bool:
        return self.get_file_meta(area, filename) is not None

    # ── Upload permission check ─────────────────────────────────────

    @staticmethod
    def _base_callsign(callsign: str) -> str:
        from core.util import base_callsign
        return base_callsign(callsign)

    def _check_upload_permission(self, callsign: str) -> tuple[bool, str]:
        """
        Central permission check for remote (RF) uploads.
        Returns (allowed, reason_string).

        Whitelist matching supports both base callsigns and specific SSIDs:
          - Whitelist "KK4ODA"   matches KK4ODA, KK4ODA-5, KK4ODA-7, etc.
          - Whitelist "KK4ODA-5" matches only KK4ODA-5.
        """
        fu = self._config.file_upload
        call = callsign.upper()
        base = self._base_callsign(call)
        mode = fu.permission_mode.lower()

        if mode == "whitelist":
            whitelist = [c.upper() for c in fu.upload_whitelist]
            # Match if exact call is listed OR base callsign is listed
            if call not in whitelist and base not in whitelist:
                log.warning(
                    f"UPLOAD_AUTH callsign={call} mode=whitelist result=DENIED "
                    f"reason=not_in_whitelist"
                )
                return False, "Upload permission denied. Contact sysop for access."
            # Belt-and-suspenders: even whitelisted users can be explicitly blocked
            blocked = [c.upper() for c in fu.blocked_callsigns]
            if call in blocked or base in blocked:
                log.warning(
                    f"UPLOAD_AUTH callsign={call} mode=whitelist result=DENIED "
                    f"reason=in_blocklist_override"
                )
                return False, "Your callsign is not permitted to upload files."
            log.info(
                f"UPLOAD_AUTH callsign={call} mode=whitelist result=ALLOWED"
            )
            return True, ""

        elif mode == "blocklist":
            blocked = [c.upper() for c in fu.blocked_callsigns]
            if call in blocked or base in blocked:
                log.warning(
                    f"UPLOAD_AUTH callsign={call} mode=blocklist result=DENIED "
                    f"reason=in_blocklist"
                )
                return False, "Your callsign is not permitted to upload files."
            log.info(
                f"UPLOAD_AUTH callsign={call} mode=blocklist result=ALLOWED"
            )
            return True, ""

        elif mode == "open":
            return True, ""

        elif mode == "role_based":
            # Delegate to AccessController for role-based checks
            if self._access is None:
                log.error("UPLOAD_AUTH mode=role_based but no AccessController")
                return False, "Upload policy misconfigured. Contact sysop."
            from bbs.access import Role
            role = self._access.get_role(callsign)
            if role not in (Role.TRUSTED, Role.SYSOP):
                log.warning(
                    f"UPLOAD_AUTH callsign={call} mode=role_based "
                    f"role={role.value} result=DENIED"
                )
                return False, "Upload permission denied. Contact sysop for access."
            # Still check blocklist as override
            blocked = [c.upper() for c in fu.blocked_callsigns]
            if call in blocked or base in blocked:
                log.warning(
                    f"UPLOAD_AUTH callsign={call} mode=role_based result=DENIED "
                    f"reason=in_blocklist_override"
                )
                return False, "Your callsign is not permitted to upload files."
            log.info(f"UPLOAD_AUTH callsign={call} mode=role_based result=ALLOWED")
            return True, ""

        else:
            # Unknown mode — fail closed
            log.error(
                f"UPLOAD_AUTH callsign={call} mode={mode} result=DENIED "
                f"reason=unknown_permission_mode"
            )
            return False, "Upload policy misconfigured. Contact sysop."

    def _emit_upload_denied(self, callsign: str, filename: str,
                            area: str, reason: str):
        """Emit UPLOAD_DENIED event for GUI notification."""
        if self._bus:
            self._bus.emit(EventType.UPLOAD_DENIED, data={
                "callsign": callsign,
                "filename": filename,
                "area": area,
                "reason": reason,
                "mode": self._config.file_upload.permission_mode,
            })

    # ── Upload (PUT) ──────────────────────────────────────────────────────

    def begin_upload(self, area: str, filename: str, uploader: str,
                     description: str, size_bytes: int) -> Optional[Path]:
        """
        Validate and prepare for an upload.
        Returns the destination Path if OK, or None if rejected.
        """
        area = area.upper()
        if not self.area_exists(area):
            return None
        if size_bytes > MAX_FILE_BYTES:
            return None
        # Sanitise filename — alphanumeric, dots, dashes, underscores only
        safe = "".join(c for c in filename if c.isalnum() or c in "._-")
        if not safe or len(safe) > 64:
            return None
        area_dir = self._root / area
        area_dir.mkdir(parents=True, exist_ok=True)
        return area_dir / safe

    def commit_upload(
        self,
        area: str,
        filename: str,
        uploader: str,
        description: str,
        data: bytes,
        is_remote: bool = False,
    ) -> tuple[bool, str]:
        """
        Write file to disk and record metadata.

        Parameters
        ----------
        is_remote : bool
            True when the upload arrives via RF (applies policy checks).
            False (default) for sysop GUI uploads which bypass policy.

        Returns
        -------
        (success, reason) where reason is "" on success or an error string.
        """
        fu = self._config.file_upload
        area = area.upper()

        # ── Policy checks (RF uploads only) ──────────────────────────────
        if is_remote:
            if not fu.allow_remote_uploads:
                self._emit_upload_denied(uploader, filename, area,
                                         "remote_uploads_disabled")
                return False, "Remote uploads are disabled by sysop."

            # Permission check (whitelist / blocklist / open) — FIRST gate
            allowed, perm_reason = self._check_upload_permission(uploader)
            if not allowed:
                self._emit_upload_denied(uploader, filename, area, perm_reason)
                return False, perm_reason

            # Extension allowlist
            from pathlib import Path as _Path
            ext = _Path(filename).suffix.lower()
            allowed = [e.lower() for e in fu.allowed_extensions]
            if allowed and ext not in allowed:
                log.warning(f"Upload blocked: extension {ext!r} not in allowlist")
                joined = ", ".join(fu.allowed_extensions)
                return False, f"File type {ext!r} is not accepted. Allowed: {joined}"

            # Content / magic-byte validation
            content_ok, content_reason = _validate_file_content(filename, data)
            if not content_ok:
                log.warning(f"Upload blocked: content validation: {content_reason}")
                self._emit_upload_denied(uploader, filename, area, content_reason)
                return False, content_reason

            # Size limit
            max_bytes = fu.max_file_bytes
            if len(data) > max_bytes:
                mb = max_bytes / (1024 * 1024)
                return False, f"File exceeds {mb:.1f} MB size limit."

            # Per-callsign quota
            count = self.callsign_upload_count(uploader)
            if count >= fu.max_files_per_callsign:
                quota = fu.max_files_per_callsign
                return False, (
                    f"Upload quota reached ({quota} files). "
                    "Delete some files or contact sysop."
                )

            # Area write-lock
            if not self.get_area_remote_upload(area):
                return False, f"Remote uploads to area {area} are disabled."

            # Virus scan
            is_clean, threat = _virus_scan(data, self._config)
            if not is_clean:
                if fu.quarantine_infected and fu.quarantine_dir:
                    from pathlib import Path as _P
                    from datetime import datetime as _dt
                    qdir = _P(fu.quarantine_dir)
                    qdir.mkdir(parents=True, exist_ok=True)
                    ts = _dt.now().strftime("%Y%m%d_%H%M%S")
                    safe_qname = "".join(
                        c for c in filename if c.isalnum() or c in "._-"
                    )
                    qpath = qdir / f"{ts}_{uploader}_{safe_qname}"
                    try:
                        qpath.write_bytes(data)
                        log.warning(
                            f"Infected file quarantined: {qpath} "
                            f"(threat: {threat})"
                        )
                    except Exception as qe:
                        log.error(f"Quarantine write failed: {qe}")
                return False, f"File rejected by virus scanner ({threat})."

        # ── Write to disk (via staging directory) ─────────────────────────
        safe = "".join(c for c in filename if c.isalnum() or c in "._-")
        # Reject path traversal attempts
        if not safe or safe.startswith(".") or ".." in safe:
            return False, "Invalid filename."
        if not any(c.isalnum() for c in safe):
            return False, "Invalid filename."
        area_dir = self._root / area
        area_dir.mkdir(parents=True, exist_ok=True)
        staging_dir = self._root / ".staging"
        staging_dir.mkdir(parents=True, exist_ok=True)
        staging_path = staging_dir / f"{uploader.upper()}_{safe}"
        dest = area_dir / safe
        try:
            # Write to staging first
            staging_path.write_bytes(data)
            # Atomic move to final location
            staging_path.replace(dest)
            sha = hashlib.sha256(data).hexdigest()
            now = _utcnow()
            with self._lock:
                self._conn.execute(
                    """INSERT OR REPLACE INTO files
                       (filename, area, description, uploader, size_bytes,
                        sha256, uploaded_at, dl_count)
                       VALUES (?,?,?,?,?,?,?,0)""",
                    (safe, area, description, uploader.upper(),
                     len(data), sha, now),
                )
                self._conn.commit()
            remote_label = " [remote]" if is_remote else " [sysop]"
            log.info(
                f"FileStore: uploaded {area}/{safe} "
                f"({len(data)} bytes) by {uploader}{remote_label}"
            )
            return True, ""
        except Exception as e:
            # Clean up staging file on any error
            try:
                if staging_path.exists():
                    staging_path.unlink()
            except OSError:
                pass
            log.error(f"FileStore: commit_upload error: {e}")
            return False, f"Internal error: {e}"

    # ── Download (GET) ────────────────────────────────────────────────────

    def read_file(self, area: str, filename: str) -> Optional[bytes]:
        """Return file bytes and increment download counter."""
        area = area.upper()
        path = self._root / area / filename
        if not path.exists():
            return None
        try:
            data = path.read_bytes()
            now = _utcnow()
            with self._lock:
                self._conn.execute(
                    """UPDATE files SET dl_count=dl_count+1, last_dl_at=?
                       WHERE area=? AND filename=?""",
                    (now, area, filename),
                )
                self._conn.commit()
            return data
        except Exception as e:
            log.error(f"FileStore: read_file error: {e}")
            return None

    # ── Delete ────────────────────────────────────────────────────────────

    def delete_file(self, area: str, filename: str) -> bool:
        area = area.upper()
        path = self._root / area / filename
        try:
            if path.exists():
                path.unlink()
            with self._lock:
                self._conn.execute(
                    "DELETE FROM files WHERE area=? AND filename=?",
                    (area, filename),
                )
                self._conn.commit()
            log.info(f"FileStore: deleted {area}/{filename}")
            return True
        except Exception as e:
            log.error(f"FileStore: delete_file error: {e}")
            return False

    # ── Stats ─────────────────────────────────────────────────────────────

    def file_count(self) -> int:
        with self._lock:
            cur = self._conn.execute("SELECT COUNT(*) FROM files")
            return cur.fetchone()[0]

    def total_bytes(self) -> int:
        with self._lock:
            cur = self._conn.execute(
                "SELECT COALESCE(SUM(size_bytes),0) FROM files"
            )
            return cur.fetchone()[0]
