"""
bbs/store.py

SQLite-backed message store.
Thread-safe via WAL mode (multiple concurrent readers + one writer).
"""

import sqlite3
import logging
import threading
from pathlib import Path
from typing import Optional
from datetime import datetime, timedelta, timezone


def _utcnow() -> str:
    """Return current UTC time as an ISO 8601 string (timezone-aware)."""
    return datetime.now(timezone.utc).isoformat()

from core.config import AppConfig
from core.events import EventBus, EventType

log = logging.getLogger(__name__)


# ── Message CMS delivery status ─────────────────────────────────────────────

class MessageStatus:
    """
    CMS delivery status values stored in messages.cms_status.

    Lifecycle:
      (empty)  → QUEUED → SENT           (outbound, accepted by CMS)
      (empty)  → QUEUED → REJECTED       (outbound, CMS had it already)
      (empty)  → QUEUED → FAILED         (outbound, transport error)
      (empty)  → RECEIVED                (inbound from CMS)
      (empty)  → LOCAL                   (RF-only, not CMS-routed)
    """
    NONE      = ""           # no CMS involvement
    QUEUED    = "QUEUED"     # awaiting next CMS poll
    SENDING   = "SENDING"    # currently in a CMS session
    SENT      = "SENT"       # accepted by CMS
    REJECTED  = "REJECTED"   # CMS already had it (= flag)
    FAILED    = "FAILED"     # delivery failed after max retries
    RECEIVED  = "RECEIVED"   # downloaded from CMS
    LOCAL     = "LOCAL"      # local RF delivery only

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS messages (
    id          TEXT    PRIMARY KEY,
    from_call   TEXT    NOT NULL,
    to_call     TEXT    NOT NULL,
    subject     TEXT    NOT NULL DEFAULT '',
    body        BLOB    NOT NULL DEFAULT '',
    date        TEXT    NOT NULL,
    area        TEXT    NOT NULL DEFAULT 'GENERAL',
    instance    TEXT,
    via_path    TEXT,
    is_read     INTEGER NOT NULL DEFAULT 0,
    is_sent     INTEGER NOT NULL DEFAULT 0,
    is_replied  INTEGER NOT NULL DEFAULT 0,
    priority    INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    expires_at  TEXT
);

CREATE TABLE IF NOT EXISTS message_audit (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    mid          TEXT    NOT NULL,
    event        TEXT    NOT NULL,
    operator     TEXT,
    timestamp    TEXT    NOT NULL DEFAULT (datetime('now')),
    notes        TEXT
);

CREATE INDEX IF NOT EXISTS idx_audit_mid ON message_audit(mid);

CREATE TABLE IF NOT EXISTS sessions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    remote_call     TEXT    NOT NULL,
    instance        TEXT,
    session_type    TEXT,
    connected_at    TEXT,
    disconnected_at TEXT,
    msgs_sent       INTEGER DEFAULT 0,
    msgs_recv       INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS heard_stations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    callsign    TEXT    NOT NULL,
    ssid        INTEGER NOT NULL DEFAULT 0,
    instance    TEXT    NOT NULL,
    first_heard TEXT    NOT NULL,
    last_heard  TEXT    NOT NULL,
    hear_count  INTEGER NOT NULL DEFAULT 1,
    grid_square TEXT,
    via_path    TEXT,
    last_event  TEXT,
    UNIQUE(callsign, ssid, instance)
);

CREATE TABLE IF NOT EXISTS user_prefs (
    callsign    TEXT PRIMARY KEY,
    pager_lines INTEGER NOT NULL DEFAULT 20,
    last_read   TEXT,
    last_seen   TEXT
);

CREATE INDEX IF NOT EXISTS idx_messages_to    ON messages(to_call);
CREATE INDEX IF NOT EXISTS idx_messages_area  ON messages(area);
CREATE INDEX IF NOT EXISTS idx_messages_date  ON messages(date);
CREATE INDEX IF NOT EXISTS idx_heard_last     ON heard_stations(last_heard DESC);
"""


def _generate_mid(from_call: str) -> str:
    """Generate a unique 12-char Message ID using UUID4 for collision resistance."""
    import uuid
    return uuid.uuid4().hex[:12].upper()


class MessageStore:

    def __init__(self, config: AppConfig, event_bus: Optional[EventBus] = None):
        self._config = config
        self._bus = event_bus
        self._conn: Optional[sqlite3.Connection] = None
        self._lock = threading.Lock()

        db_dir = Path(config.bbs.message_dir)
        db_dir.mkdir(parents=True, exist_ok=True)
        self._db_path = db_dir / "messages.db"

    def initialize(self):
        self._conn = sqlite3.connect(
            str(self._db_path),
            check_same_thread=False,
        )
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._apply_migrations()
            self._conn.commit()
        log.info(f"MessageStore initialised at {self._db_path}")

    @property
    def conn(self) -> sqlite3.Connection:
        """Shared connection — used by AccessController."""
        return self._conn

    @property
    def lock(self) -> threading.Lock:
        """Shared lock — used by AccessController."""
        return self._lock

    def _apply_migrations(self):
        """Add columns introduced after the initial schema without data loss."""
        new_cols = [
            ("messages", "is_replied", "INTEGER NOT NULL DEFAULT 0"),
            ("messages", "priority",   "INTEGER NOT NULL DEFAULT 0"),
            ("messages", "cms_status", "TEXT NOT NULL DEFAULT ''"),
            ("messages", "cms_attempts", "INTEGER NOT NULL DEFAULT 0"),
            # ── Outbound delivery architecture columns ────────────────────
            ("messages", "delivery_status",           "TEXT NOT NULL DEFAULT ''"),
            ("messages", "delivery_backend_selected", "TEXT NOT NULL DEFAULT ''"),
            ("messages", "delivery_state",            "TEXT NOT NULL DEFAULT ''"),
            # ── User prefs: Winlink opt-in ───────────────────────────────
            ("user_prefs", "winlink_registered", "INTEGER NOT NULL DEFAULT 0"),
            ("user_prefs", "winlink_password", "TEXT NOT NULL DEFAULT ''"),
        ]
        for table, col, definition in new_cols:
            try:
                self._conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN {col} {definition}"
                )
                log.info(f"Schema migration: added {table}.{col}")
            except sqlite3.OperationalError:
                pass  # column already exists — normal on existing databases

        # ── Delivery indexes ──────────────────────────────────────────────
        indexes = [
            ("idx_messages_delivery_status",
             "CREATE INDEX IF NOT EXISTS idx_messages_delivery_status "
             "ON messages(delivery_status)"),
            ("idx_messages_delivery_backend",
             "CREATE INDEX IF NOT EXISTS idx_messages_delivery_backend "
             "ON messages(delivery_backend_selected)"),
        ]
        for idx_name, ddl in indexes:
            try:
                self._conn.execute(ddl)
            except sqlite3.OperationalError:
                pass

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None

    # ── Messages ─────────────────────────────────────────────────────────

    def save_message(
        self,
        from_call: str,
        to_call: str,
        subject: str,
        body: bytes,
        area: str = "GENERAL",
        instance: str = "",
        via_path: str = "",
        mid: Optional[str] = None,
        expires_days: Optional[int] = None,
        priority: int = 0,
    ) -> str:
        if mid is None:
            mid = _generate_mid(from_call)
        now = _utcnow()
        expires = None
        if expires_days:
            expires = (datetime.now(timezone.utc) + timedelta(days=expires_days)).isoformat()
        elif self._config.bbs.max_message_age:
            expires = (
                datetime.now(timezone.utc)
                + timedelta(days=self._config.bbs.max_message_age)
            ).isoformat()

        with self._lock:
            cur = self._conn.execute(
                """INSERT OR IGNORE INTO messages
                   (id,from_call,to_call,subject,body,date,area,instance,
                    via_path,expires_at,created_at,priority)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (mid, from_call.upper(), to_call.upper(), subject,
                 body, now, area.upper(), instance, via_path, expires, now, priority),
            )
            inserted = cur.rowcount > 0
            self._conn.commit()

        if inserted and self._bus:
            self._bus.emit(
                EventType.MESSAGE_RECEIVED,
                data={"mid": mid, "from": from_call, "to": to_call,
                      "subject": subject, "priority": priority},
            )
        if inserted:
            log.info(f"Message saved: MID={mid} from={from_call} to={to_call} priority={priority}")
        else:
            log.debug(f"Message already exists, skipped: MID={mid}")
        return mid

    def get_message(self, mid: str) -> Optional[dict]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM messages WHERE id=?", (mid,)
            )
            row = cur.fetchone()
            return dict(row) if row is not None else None

    def get_messages_for(self, callsign: str) -> list[dict]:
        """Return unsent messages addressed to this callsign."""
        with self._lock:
            cur = self._conn.execute(
                """SELECT * FROM messages
                   WHERE to_call=? AND is_sent=0
                   ORDER BY date ASC""",
                (callsign.upper(),),
            )
            return [dict(r) for r in cur.fetchall()]

    def list_messages(self, area: Optional[str] = None, limit: int = 50,
                      to_call: Optional[str] = None) -> list[dict]:
        with self._lock:
            conditions = ["COALESCE(is_bulletin,0)=0"]
            params: list = []
            if area:
                conditions.append("area=?")
                params.append(area.upper())
            if to_call:
                conditions.append("UPPER(to_call)=?")
                params.append(to_call.upper())
            where = "WHERE " + " AND ".join(conditions)
            params.append(limit)
            cur = self._conn.execute(
                f"""SELECT id, from_call, to_call, subject, date, area,
                           is_read, is_replied, priority,
                           cms_status, cms_attempts
                    FROM messages {where}
                    ORDER BY date DESC LIMIT ?""",
                params,
            )
            return [dict(r) for r in cur.fetchall()]

    def list_areas(self) -> list[str]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT DISTINCT area FROM messages WHERE COALESCE(is_bulletin,0)=0 ORDER BY area"
            )
            return [row[0] for row in cur.fetchall()]

    def mark_read(self, mid: str, operator: str = ""):
        with self._lock:
            self._conn.execute("UPDATE messages SET is_read=1 WHERE id=?", (mid,))
            self._conn.commit()
        self.audit_log(mid, "READ", operator=operator)

    def mark_unread(self, mid: str):
        with self._lock:
            self._conn.execute("UPDATE messages SET is_read=0 WHERE id=?", (mid,))
            self._conn.commit()
        self.audit_log(mid, "MARK_UNREAD")

    def mark_replied(self, mid: str, operator: str = ""):
        with self._lock:
            self._conn.execute(
                "UPDATE messages SET is_replied=1, is_read=1 WHERE id=?", (mid,)
            )
            self._conn.commit()
        self.audit_log(mid, "REPLIED", operator=operator)

    def audit_log(self, mid: str, event: str, operator: str = "", notes: str = ""):
        """Record an audit event for a message (received/opened/acknowledged/replied)."""
        now = _utcnow()
        try:
            with self._lock:
                self._conn.execute(
                    """INSERT INTO message_audit (mid, event, operator, timestamp, notes)
                       VALUES (?, ?, ?, ?, ?)""",
                    (mid, event, operator, now, notes),
                )
                self._conn.commit()
        except Exception as e:
            log.warning(f"Audit log write failed: {e}")

    def get_audit_log(self, mid: str) -> list:
        """Return all audit events for a given message ID."""
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM message_audit WHERE mid=? ORDER BY timestamp ASC",
                (mid,),
            )
            return [dict(row) for row in cur.fetchall()]

    def mark_sent(self, mid: str):
        with self._lock:
            self._conn.execute(
                "UPDATE messages SET is_sent=1, cms_status='SENT' WHERE id=?",
                (mid,),
            )
            self._conn.commit()
        self.audit_log(mid, "SENT")

    def set_cms_status(self, mid: str, status: str, notes: str = ""):
        """Update the CMS delivery status of a message."""
        with self._lock:
            self._conn.execute(
                "UPDATE messages SET cms_status=? WHERE id=?",
                (status, mid),
            )
            self._conn.commit()
        self.audit_log(mid, f"CMS_STATUS_{status}", notes=notes)
        log.debug(f"MID={mid} cms_status → {status}")

    def increment_cms_attempts(self, mid: str) -> int:
        """Increment the CMS send attempt counter. Returns the new count."""
        with self._lock:
            self._conn.execute(
                "UPDATE messages SET cms_attempts = cms_attempts + 1 WHERE id=?",
                (mid,),
            )
            self._conn.commit()
            cur = self._conn.execute(
                "SELECT cms_attempts FROM messages WHERE id=?", (mid,),
            )
            row = cur.fetchone()
            return row[0] if row else 0

    def set_cms_queued(self, mid: str):
        """Mark a message as queued for CMS delivery."""
        self.set_cms_status(mid, MessageStatus.QUEUED)

    def set_cms_failed(self, mid: str, reason: str = ""):
        """Mark a message as failed CMS delivery."""
        self.set_cms_status(mid, MessageStatus.FAILED, notes=reason)

    def get_outbox(self, limit: int = 200) -> list[dict]:
        """
        Return messages in the outbox (sent or pending CMS delivery).
        Includes cms_status and cms_attempts for status display.
        """
        with self._lock:
            cur = self._conn.execute(
                """SELECT id, from_call, to_call, subject, date, area,
                          is_read, is_sent, is_replied, priority,
                          cms_status, cms_attempts
                   FROM messages
                   WHERE cms_status != '' AND cms_status != 'RECEIVED'
                   ORDER BY date DESC LIMIT ?""",
                (limit,),
            )
            return [dict(r) for r in cur.fetchall()]

    def get_messages_by_cms_status(self, status: str, limit: int = 50) -> list[dict]:
        """Get messages with a specific CMS delivery status."""
        with self._lock:
            cur = self._conn.execute(
                """SELECT * FROM messages
                   WHERE cms_status=?
                   ORDER BY priority DESC, date ASC
                   LIMIT ?""",
                (status, limit),
            )
            return [dict(r) for r in cur.fetchall()]

    def get_cms_queued(self, limit: int = 25) -> list[dict]:
        """Get messages queued for CMS delivery."""
        return self.get_messages_by_cms_status(MessageStatus.QUEUED, limit)

    def delete_message(self, mid: str):
        with self._lock:
            self._conn.execute("DELETE FROM messages WHERE id=?", (mid,))
            self._conn.commit()

    def has_message(self, mid: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "SELECT 1 FROM messages WHERE id=?", (mid,)
            )
            return cur.fetchone() is not None

    def expire_old_messages(self):
        now = _utcnow()
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM messages WHERE expires_at IS NOT NULL AND expires_at < ?",
                (now,),
            )
            self._conn.commit()
            if cur.rowcount:
                log.info(f"Expired {cur.rowcount} old message(s)")

    def get_new_messages_for(self, callsign: str, since: Optional[str] = None) -> list[dict]:
        with self._lock:
            if since:
                cur = self._conn.execute(
                    """SELECT id,from_call,subject,date FROM messages
                       WHERE to_call=? AND created_at > ?
                       ORDER BY date ASC""",
                    (callsign.upper(), since),
                )
            else:
                cur = self._conn.execute(
                    """SELECT id,from_call,subject,date FROM messages
                       WHERE to_call=? AND is_read=0
                       ORDER BY date ASC""",
                    (callsign.upper(),),
                )
            return [dict(r) for r in cur.fetchall()]

    # ── Sessions ─────────────────────────────────────────────────────────

    def log_session_start(
        self,
        remote_call: str,
        instance: str,
        session_type: str,
    ) -> int:
        now = _utcnow()
        with self._lock:
            cur = self._conn.execute(
                """INSERT INTO sessions (remote_call,instance,session_type,connected_at)
                   VALUES (?,?,?,?)""",
                (remote_call.upper(), instance, session_type, now),
            )
            self._conn.commit()
            return cur.lastrowid

    def list_sessions(self, limit: int = 200) -> list[sqlite3.Row]:
        """Return recent sessions, newest first."""
        with self._lock:
            cur = self._conn.execute(
                """SELECT * FROM sessions
                   ORDER BY connected_at DESC LIMIT ?""",
                (limit,),
            )
            return cur.fetchall()

    def log_session_end(
        self,
        session_id: int,
        msgs_sent: int = 0,
        msgs_recv: int = 0,
    ):
        now = _utcnow()
        with self._lock:
            self._conn.execute(
                """UPDATE sessions SET disconnected_at=?, msgs_sent=?, msgs_recv=?
                   WHERE id=?""",
                (now, msgs_sent, msgs_recv, session_id),
            )
            self._conn.commit()

    # ── Heard stations ────────────────────────────────────────────────────

    def upsert_heard_station(
        self,
        callsign: str,
        ssid: int,
        instance: str,
        via_path: str = "",
        last_event: str = "CONNECTED",
    ):
        now = _utcnow()
        with self._lock:
            self._conn.execute(
                """INSERT INTO heard_stations
                       (callsign,ssid,instance,first_heard,last_heard,via_path,last_event)
                   VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(callsign,ssid,instance) DO UPDATE SET
                       last_heard=excluded.last_heard,
                       hear_count=hear_count+1,
                       via_path=excluded.via_path,
                       last_event=excluded.last_event""",
                (callsign.upper(), ssid, instance, now, now, via_path, last_event),
            )
            self._conn.commit()

    def get_heard_stations(
        self,
        instance: Optional[str] = None,
        limit: int = 100,
    ) -> list[sqlite3.Row]:
        with self._lock:
            if instance:
                cur = self._conn.execute(
                    """SELECT * FROM heard_stations WHERE instance=?
                       ORDER BY last_heard DESC LIMIT ?""",
                    (instance, limit),
                )
            else:
                cur = self._conn.execute(
                    """SELECT * FROM heard_stations
                       ORDER BY last_heard DESC LIMIT ?""",
                    (limit,),
                )
            return cur.fetchall()

    def clear_heard_stations(self):
        with self._lock:
            self._conn.execute("DELETE FROM heard_stations")
            self._conn.commit()

    # ── User preferences ──────────────────────────────────────────────────

    def get_user_prefs(self, callsign: str) -> dict:
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM user_prefs WHERE callsign=?",
                (callsign.upper(),),
            )
            row = cur.fetchone()
            if row:
                return dict(row)
            return {"callsign": callsign.upper(), "pager_lines": 20,
                    "last_read": None, "last_seen": None}

    def save_user_prefs(self, callsign: str, pager_lines: int = 20, last_read: Optional[str] = None):
        now = _utcnow()
        with self._lock:
            self._conn.execute(
                """INSERT INTO user_prefs (callsign,pager_lines,last_read,last_seen)
                   VALUES (?,?,?,?)
                   ON CONFLICT(callsign) DO UPDATE SET
                       pager_lines=excluded.pager_lines,
                       last_read=COALESCE(excluded.last_read, last_read),
                       last_seen=excluded.last_seen""",
                (callsign.upper(), pager_lines, last_read, now),
            )
            self._conn.commit()

    def set_winlink_registered(self, callsign: str, registered: bool,
                               winlink_password: str = ""):
        """Set Winlink forwarding opt-in for a callsign with their CMS password.

        The password is encrypted at rest using the machine_secret.
        """
        from core.crypto import encrypt_value
        call = callsign.upper()
        machine = self._config.security.machine_secret or ""
        enc_pw = encrypt_value(winlink_password, machine) if winlink_password else ""
        with self._lock:
            self._conn.execute(
                """INSERT INTO user_prefs (callsign, winlink_registered, winlink_password)
                   VALUES (?, ?, ?)
                   ON CONFLICT(callsign) DO UPDATE SET
                       winlink_registered=excluded.winlink_registered,
                       winlink_password=CASE WHEN excluded.winlink_registered=1
                           THEN excluded.winlink_password
                           ELSE '' END""",
                (call, 1 if registered else 0, enc_pw),
            )
            self._conn.commit()
        log.info(f"Winlink registration for {call}: {'ON' if registered else 'OFF'}")

    def get_winlink_registered_calls(self) -> list[dict]:
        """Return all callsigns opted in to Winlink forwarding with passwords.

        Returns list of {"callsign": str, "password": str}.
        Passwords are decrypted from their at-rest encrypted form.
        """
        from core.crypto import decrypt_value
        machine = self._config.security.machine_secret or ""
        with self._lock:
            cur = self._conn.execute(
                "SELECT callsign, winlink_password FROM user_prefs "
                "WHERE winlink_registered=1"
            )
            return [{"callsign": row["callsign"],
                     "password": decrypt_value(row["winlink_password"], machine)}
                    for row in cur.fetchall()]

    def message_count(self) -> int:
        with self._lock:
            cur = self._conn.execute("SELECT COUNT(*) FROM messages")
            return cur.fetchone()[0]
