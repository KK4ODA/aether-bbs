"""
bbs/access.py

Centralized access control for the Aether BBS.

Determines what a callsign is allowed to do based on their role in the
access_control table.  All authorization decisions flow through this class.
No other module should query the access_control table directly for
permission checks.

Shares the MessageStore's SQLite connection (messages.db) for the
access_control and security_audit tables.
"""

import hashlib
import hmac as hmac_mod
import logging
import secrets
import sqlite3
import threading
from datetime import datetime, timezone, timedelta
from enum import Enum
from typing import Optional

log = logging.getLogger(__name__)


# ── Role hierarchy ────────────────────────────────────────────────────────────

class Role(str, Enum):
    SYSOP    = "SYSOP"
    TRUSTED  = "TRUSTED"
    APPROVED = "APPROVED"
    PENDING  = "PENDING"
    UNKNOWN  = "UNKNOWN"
    BLOCKED  = "BLOCKED"


# Ordered by privilege level (index = level)
ROLE_HIERARCHY = [
    Role.BLOCKED, Role.UNKNOWN, Role.PENDING,
    Role.APPROVED, Role.TRUSTED, Role.SYSOP,
]


# ── Permission matrix ─────────────────────────────────────────────────────────
# Key = command prefix (uppercase); Value = minimum role required.

COMMAND_PERMISSIONS: dict[str, Role] = {
    # Navigation / info — APPROVED+
    "H": Role.APPROVED,  "?": Role.APPROVED,  "HELP": Role.APPROVED,
    "I": Role.APPROVED,  "P": Role.APPROVED,  "MH":   Role.APPROVED,
    "B": Role.APPROVED,  "73": Role.APPROVED, "BYE":  Role.APPROVED,
    "QUIT": Role.APPROVED, "EXIT": Role.APPROVED,

    # Personal messages — APPROVED+
    "L": Role.APPROVED,  "R": Role.APPROVED,  "S":  Role.APPROVED,  "SW": Role.APPROVED,
    "K": Role.APPROVED,  "KA": Role.SYSOP,  "A": Role.APPROVED,  "U":  Role.APPROVED,
    "N": Role.APPROVED,

    # Chat — APPROVED+
    "CH": Role.APPROVED,

    # Bulletin read — APPROVED+; write — TRUSTED+
    "LB": Role.APPROVED, "RB": Role.APPROVED, "BA": Role.APPROVED,
    "SB": Role.TRUSTED,  "SUB": Role.TRUSTED, "KB": Role.TRUSTED,

    # File read — APPROVED+; write — TRUSTED+
    "FA": Role.APPROVED, "FL": Role.APPROVED, "FG": Role.APPROVED,
    "FP": Role.TRUSTED,  "FD": Role.TRUSTED,

    # Registration — available to UNKNOWN and PENDING
    "REGISTER": Role.UNKNOWN,
    "REG":      Role.UNKNOWN,
}


# Commands whose minimum role the operational mode sets (ModePolicy
# attribute), in place of the table above.
MODE_POLICY_ROLES = {
    "SB": "min_bulletin_post_role", "SUB": "min_bulletin_post_role",
    "FP": "min_upload_role",
}


# ── Schema ────────────────────────────────────────────────────────────────────

ACCESS_CONTROL_SCHEMA = """
CREATE TABLE IF NOT EXISTS access_control (
    callsign        TEXT    PRIMARY KEY,
    role            TEXT    NOT NULL DEFAULT 'UNKNOWN',
    password_hash   TEXT    NOT NULL DEFAULT '',
    challenge_token TEXT    NOT NULL DEFAULT '',
    token_expires   TEXT,
    registered_at   TEXT,
    approved_at     TEXT,
    approved_by     TEXT,
    blocked_at      TEXT,
    blocked_reason  TEXT    NOT NULL DEFAULT '',
    last_auth_at    TEXT,
    auth_failures   INTEGER NOT NULL DEFAULT 0,
    last_failure_at TEXT,
    notes           TEXT    NOT NULL DEFAULT '',
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_ac_role ON access_control(role);

CREATE TABLE IF NOT EXISTS security_audit (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp   TEXT    NOT NULL DEFAULT (datetime('now')),
    callsign    TEXT    NOT NULL,
    event       TEXT    NOT NULL,
    detail      TEXT    NOT NULL DEFAULT '',
    instance    TEXT    NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_sa_call ON security_audit(callsign);
CREATE INDEX IF NOT EXISTS idx_sa_ts   ON security_audit(timestamp);
CREATE INDEX IF NOT EXISTS idx_sa_evt  ON security_audit(event);
"""


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


from core.util import base_callsign as _base_call


# ── Password encryption helpers ──────────────────────────────────────────────
# Shared implementation lives in core.crypto; import here for local use.

from core.crypto import encrypt_password as _encrypt_password
from core.crypto import decrypt_password as _decrypt_password


# ── AccessController ──────────────────────────────────────────────────────────

class AccessController:
    """
    Manages user roles and enforces access control.

    Thread-safe.  Shares the MessageStore's SQLite connection
    (messages.db) for the access_control and security_audit tables.
    """

    def __init__(self, conn: sqlite3.Connection, lock: threading.Lock,
                 config, event_bus=None):
        self._conn   = conn
        self._lock   = lock
        self._config = config  # AppConfig or ConfigManager
        self._bus    = event_bus
        self._ensure_schema()

    def _ensure_schema(self):
        """Create tables if they don't exist."""
        with self._lock:
            self._conn.executescript(ACCESS_CONTROL_SCHEMA)
            self._conn.commit()

    # ── Config resolution ────────────────────────────────────────────

    def _cfg(self):
        # Duck-type check: if config has .current(), it's a ConfigManager
        if hasattr(self._config, "current") and callable(self._config.current):
            return self._config.current()
        return self._config

    # ── Role lookup ──────────────────────────────────────────────────

    def get_role(self, callsign: str) -> Role:
        """
        Return the effective role for a callsign.
        Sysop callsign from config always returns SYSOP (fail-safe).
        """
        base = _base_call(callsign)
        cfg = self._cfg()

        # Fail-safe: config sysop callsign is always SYSOP
        if base == _base_call(cfg.station.sysop_callsign):
            return Role.SYSOP

        with self._lock:
            cur = self._conn.execute(
                "SELECT role FROM access_control WHERE callsign=?",
                (base,),
            )
            row = cur.fetchone()
            if row:
                try:
                    return Role(row[0])
                except ValueError:
                    return Role.UNKNOWN
            return Role.UNKNOWN

    def has_permission(self, callsign: str, command: str,
                       policy=None) -> bool:
        """Check if callsign is authorized to execute a command.

        ``policy`` (the ModePolicy) sets the minimum role for posting
        bulletins and uploading files in the current operational mode."""
        role = self.get_role(callsign)
        if role == Role.SYSOP:
            return True
        if role == Role.BLOCKED:
            return False

        cmd_upper = command.upper().split()[0] if command.strip() else ""
        required = COMMAND_PERMISSIONS.get(cmd_upper)
        if policy is not None and cmd_upper in MODE_POLICY_ROLES:
            required = Role(getattr(policy, MODE_POLICY_ROLES[cmd_upper]))

        if required is None:
            # Unknown command — allow for APPROVED+ (will get "Unknown command")
            return ROLE_HIERARCHY.index(role) >= ROLE_HIERARCHY.index(Role.APPROVED)

        role_level = ROLE_HIERARCHY.index(role)
        required_level = ROLE_HIERARCHY.index(required)
        return role_level >= required_level

    # ── Role management ──────────────────────────────────────────────

    def set_role(self, callsign: str, new_role: Role,
                 changed_by: str = "SYSTEM", reason: str = ""):
        """Change a user's role.  Logs to security_audit."""
        base = _base_call(callsign)
        now = _utcnow()

        with self._lock:
            cur = self._conn.execute(
                "SELECT role FROM access_control WHERE callsign=?", (base,),
            )
            row = cur.fetchone()
            old_role = row[0] if row else "UNKNOWN"

            if row:
                self._conn.execute(
                    """UPDATE access_control SET
                           role=?, updated_at=?,
                           approved_at  = CASE WHEN ?  IN ('APPROVED','TRUSTED')
                                          THEN ? ELSE approved_at END,
                           approved_by  = CASE WHEN ?  IN ('APPROVED','TRUSTED')
                                          THEN ? ELSE approved_by END,
                           blocked_at   = CASE WHEN ?='BLOCKED'  THEN ? ELSE blocked_at END,
                           blocked_reason=CASE WHEN ?='BLOCKED'  THEN ? ELSE blocked_reason END
                       WHERE callsign=?""",
                    (new_role.value, now,
                     new_role.value, now,
                     new_role.value, changed_by,
                     new_role.value, now,
                     new_role.value, reason,
                     base),
                )
            else:
                self._conn.execute(
                    """INSERT INTO access_control
                       (callsign, role, created_at, updated_at)
                       VALUES (?,?,?,?)""",
                    (base, new_role.value, now, now),
                )

            self._audit_locked(base, "ROLE_CHANGE",
                               f"{old_role}->{new_role.value} by {changed_by}: {reason}")
            self._conn.commit()

        log.info(f"ACCESS role_change {base} {old_role}->{new_role.value} by={changed_by}")

        # Emit GUI event
        if self._bus:
            from core.events import EventType
            etype = (EventType.ACCESS_GRANTED if new_role in (Role.APPROVED, Role.TRUSTED)
                     else EventType.ACCESS_BLOCKED if new_role == Role.BLOCKED
                     else EventType.CONFIG_CHANGED)
            self._bus.emit(etype, data={
                "callsign": base, "role": new_role.value, "by": changed_by,
            })

    def set_notes(self, callsign: str, notes: str,
                  changed_by: str = "SYSTEM") -> None:
        """Update the free-text notes field for a callsign."""
        base = _base_call(callsign)
        now = _utcnow()
        with self._lock:
            self._conn.execute(
                "UPDATE access_control SET notes=?, updated_at=? WHERE callsign=?",
                (notes[:200], now, base),
            )
            self._audit_locked(base, "NOTES_UPDATED", f"by {changed_by}")
            self._conn.commit()

    @staticmethod
    def generate_password(length: int = 16) -> str:
        """Generate a random password safe for transmission over RF.

        Uses uppercase letters and digits only (no ambiguous characters
        like 0/O, 1/I/L) to be easy to read over voice or on screen.
        """
        alphabet = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
        return "".join(secrets.choice(alphabet) for _ in range(length))

    def provision_password(self, callsign: str,
                           changed_by: str = "SYSTEM",
                           length: int = 16) -> str:
        """Generate a random password, store it, and return cleartext.

        The sysop sends the cleartext to the user via Winlink or other
        out-of-band channel.  The password is stored hashed — only this
        return value contains the cleartext.
        """
        pw = self.generate_password(length)
        self.set_password(callsign, pw)
        base = _base_call(callsign)
        self.audit(base, "PASSWORD_PROVISIONED", f"by {changed_by}")
        log.info(f"ACCESS password provisioned for {base} by {changed_by}")
        return pw

    def delete_user(self, callsign: str, deleted_by: str = "SYSTEM") -> bool:
        """Remove a user from the access_control table entirely.
        Returns True if a row was deleted.  The sysop callsign cannot be deleted."""
        base = _base_call(callsign)
        cfg = self._cfg()
        if cfg and base == _base_call(cfg.station.sysop_callsign):
            log.warning(f"ACCESS delete refused: {base} is the sysop")
            return False

        with self._lock:
            cur = self._conn.execute(
                "SELECT role FROM access_control WHERE callsign=?", (base,),
            )
            row = cur.fetchone()
            if not row:
                return False
            old_role = row[0]
            self._conn.execute(
                "DELETE FROM access_control WHERE callsign=?", (base,),
            )
            self._audit_locked(base, "USER_DELETED",
                               f"role was {old_role}, deleted by {deleted_by}")
            self._conn.commit()

        log.info(f"ACCESS user_deleted {base} (was {old_role}) by={deleted_by}")
        return True

    # ── Registration ─────────────────────────────────────────────────

    def register(self, callsign: str, instance: str = "",
                 community_ref: str = "") -> tuple[bool, str]:
        """Process a REGISTER command.  Returns (success, message_to_user)."""
        base = _base_call(callsign)
        role = self.get_role(callsign)

        if role == Role.BLOCKED:
            return False, "Your callsign has been blocked. Contact sysop off-band.\r\n"
        if role in (Role.APPROVED, Role.TRUSTED, Role.SYSOP):
            return False, "You already have access. Type H for help.\r\n"
        if role == Role.PENDING:
            return False, "Your registration is already pending review.\r\n"

        now = _utcnow()
        notes = community_ref.strip()[:200] if community_ref else ""
        with self._lock:
            self._conn.execute(
                """INSERT INTO access_control
                   (callsign, role, registered_at, notes, created_at, updated_at)
                   VALUES (?,?,?,?,?,?)
                   ON CONFLICT(callsign) DO UPDATE SET
                       role='PENDING', registered_at=?, notes=?, updated_at=?""",
                (base, "PENDING", now, notes, now, now, now, notes, now),
            )
            self._audit_locked(base, "REGISTER",
                               f"instance={instance} community_ref={notes}")
            self._conn.commit()

        log.info(f"ACCESS register {base} instance={instance} "
                 f"community_ref={notes!r}")

        if self._bus:
            from core.events import EventType
            self._bus.emit(EventType.ACCESS_REQUEST, data={
                "callsign": base, "instance": instance,
                "community_ref": notes,
            })

        return True, (
            "Thank you. Your registration is pending sysop review.\r\n"
            "Once approved, your password will be sent to you via\r\n"
            f"Winlink at {base}@winlink.org.\r\n"
            "Disconnecting now.\r\n"
        )

    # ── Authentication ───────────────────────────────────────────────

    def has_password(self, callsign: str) -> bool:
        """Check if a password is set for this callsign."""
        base = _base_call(callsign)
        with self._lock:
            cur = self._conn.execute(
                "SELECT password_hash FROM access_control WHERE callsign=?",
                (base,),
            )
            row = cur.fetchone()
            return bool(row and row[0])

    def set_password(self, callsign: str, password: str):
        """Store a hashed password and encrypted copy for the callsign.

        Storage format: ``salt:sha256hex:encrypted_b64``
        - salt:sha256hex  — for legacy plaintext verification fallback
        - encrypted_b64   — reversible encryption for HMAC-SHA256 computation
        """
        base = _base_call(callsign)
        now = _utcnow()
        cfg = self._cfg()
        machine = cfg.security.machine_secret or "default"
        salt = secrets.token_hex(16)
        h = hashlib.sha256(f"{salt}{machine}{password}".encode()).hexdigest()
        enc = _encrypt_password(password, machine, salt)
        stored = f"{salt}:{h}:{enc}"

        with self._lock:
            # Upsert: a station with no access row yet (e.g. the sysop after
            # a callsign change) was told "Password set" while nothing was
            # stored.  A new row gets the table's default role, as no row.
            self._conn.execute(
                """INSERT INTO access_control
                       (callsign, password_hash, created_at, updated_at)
                   VALUES (?,?,?,?)
                   ON CONFLICT(callsign) DO UPDATE SET
                       password_hash=excluded.password_hash,
                       updated_at=excluded.updated_at""",
                (base, stored, now, now),
            )
            self._audit_locked(base, "PASSWORD_SET", "")
            self._conn.commit()
        log.info(f"ACCESS password_set {base}")

    def generate_challenge(self, callsign: str) -> str:
        """Generate and store a 16-byte challenge nonce for authentication.

        Returns the nonce as a 32-character hex string.
        """
        base = _base_call(callsign)
        nonce = secrets.token_hex(16)  # 16 bytes = 32 hex chars
        expires = (datetime.now(timezone.utc) + timedelta(minutes=2)).isoformat()

        with self._lock:
            self._conn.execute(
                """UPDATE access_control SET
                       challenge_token=?, token_expires=?, updated_at=?
                   WHERE callsign=?""",
                (nonce, expires, _utcnow(), base),
            )
            self._conn.commit()
        return nonce

    def verify_challenge(self, callsign: str, response: str) -> bool:
        """Verify an authentication response.

        Supports two modes:
          1. **HMAC mode** — client sends HMAC-SHA256(password, nonce) as
             64 hex characters.  Password never transmitted over RF.
          2. **Plaintext fallback** — client sends raw password (for dumb
             terminals).  Only accepted if ``allow_plaintext_auth`` is True.

        Returns True if authentication succeeded.
        """
        base = _base_call(callsign)
        cfg = self._cfg()
        machine = cfg.security.machine_secret or "default"

        with self._lock:
            cur = self._conn.execute(
                "SELECT password_hash, challenge_token, token_expires "
                "FROM access_control WHERE callsign=?",
                (base,),
            )
            row = cur.fetchone()

        if not row or not row[0] or not row[1]:
            return False

        stored, nonce, expires_str = row[0], row[1], row[2]

        # Check nonce expiry
        if expires_str:
            try:
                if datetime.now(timezone.utc) > datetime.fromisoformat(expires_str):
                    log.warning(f"AUTH nonce expired for {base}")
                    self.record_auth_failure(callsign)
                    return False
            except ValueError:
                pass

        # Invalidate nonce (single-use regardless of outcome)
        with self._lock:
            self._conn.execute(
                "UPDATE access_control SET challenge_token='', "
                "token_expires=NULL WHERE callsign=?",
                (base,),
            )
            self._conn.commit()

        # Parse storage format: "salt:hash" (legacy) or "salt:hash:enc" (new)
        parts = stored.split(":", 2)
        if len(parts) < 2:
            return False
        salt, expected_hash = parts[0], parts[1]
        encrypted_b64 = parts[2] if len(parts) == 3 else None

        response = response.strip()

        # ── Try HMAC-SHA256 mode first ────────────────────────────────
        # If the response is exactly 64 hex chars and we have the
        # encrypted password, try HMAC verification.
        if encrypted_b64 and len(response) == 64:
            try:
                bytes.fromhex(response)  # validate hex
                raw_pw = _decrypt_password(encrypted_b64, machine, salt)
                nonce_bytes = bytes.fromhex(nonce)
                expected_hmac = hmac_mod.new(
                    raw_pw.encode("utf-8"),
                    nonce_bytes,
                    hashlib.sha256,
                ).hexdigest()
                if hmac_mod.compare_digest(expected_hmac, response.lower()):
                    self.record_auth_success(callsign)
                    log.info(f"AUTH HMAC-SHA256 OK for {base}")
                    return True
            except Exception as e:
                log.warning(f"AUTH HMAC verify error for {base}: {e}")

        # ── Plaintext fallback ────────────────────────────────────────
        if not cfg.security.allow_plaintext_auth:
            log.warning(f"AUTH plaintext rejected for {base} "
                        f"(allow_plaintext_auth=False)")
            self.record_auth_failure(callsign)
            return False

        # Verify by hashing the raw response with salt + machine_secret
        attempt_hash = hashlib.sha256(
            f"{salt}{machine}{response}".encode()
        ).hexdigest()

        if hmac_mod.compare_digest(attempt_hash, expected_hash):
            self.record_auth_success(callsign)
            log.info(f"AUTH plaintext OK for {base} "
                     f"(consider upgrading to HMAC client)")
            return True

        self.record_auth_failure(callsign)
        return False

    def get_decrypted_password(self, callsign: str) -> Optional[str]:
        """Return the decrypted BBS password for a callsign, or None."""
        base = _base_call(callsign)
        cfg = self._cfg()
        machine = cfg.security.machine_secret or "default"

        with self._lock:
            cur = self._conn.execute(
                "SELECT password_hash FROM access_control WHERE callsign=?",
                (base,),
            )
            row = cur.fetchone()

        if not row or not row[0]:
            return None

        parts = row[0].split(":", 2)
        if len(parts) < 3:
            return None  # no encrypted form available

        salt, _, encrypted_b64 = parts
        try:
            return _decrypt_password(encrypted_b64, machine, salt)
        except Exception:
            return None

    # ── Failure tracking ─────────────────────────────────────────────

    def check_lockout(self, callsign: str,
                      auto_block_threshold: Optional[int] = None,
                      ) -> tuple[bool, str]:
        """
        Pre-session check.  Returns (allowed, reason).
        Checks blocked status and auth failure rate limiting.
        ``auto_block_threshold``: the operational mode's (ModePolicy);
        defaults to the configured value.
        """
        base = _base_call(callsign)
        cfg = self._cfg()
        sec = cfg.security

        with self._lock:
            cur = self._conn.execute(
                "SELECT role, auth_failures, last_failure_at "
                "FROM access_control WHERE callsign=?",
                (base,),
            )
            row = cur.fetchone()

        if not row:
            return True, ""

        role_str, failures, last_fail = row[0], row[1] or 0, row[2]

        if role_str == "BLOCKED":
            self.audit(base, "CONNECT", "blocked_callsign")
            return False, "Your callsign has been blocked.\r\n"

        # Auto-block — checked BEFORE temporary lockout so it triggers
        # even when the user is also rate-limited
        if auto_block_threshold is None:
            auto_block_threshold = sec.auto_block_threshold
        if failures >= auto_block_threshold:
            self.set_role(base, Role.BLOCKED, changed_by="AUTO",
                          reason=f"auth_failures={failures}")
            return False, "Your callsign has been blocked.\r\n"

        # Temporary lockout on auth failures
        threshold = sec.auth_failure_threshold
        lockout_mins = sec.auth_lockout_minutes
        if failures >= threshold and last_fail:
            try:
                last_dt = datetime.fromisoformat(last_fail)
                if datetime.now(timezone.utc) - last_dt < timedelta(minutes=lockout_mins):
                    self.audit(base, "RATE_LIMITED", f"auth_failures={failures}")
                    return False, (
                        "Too many failed authentication attempts.\r\n"
                        "Try again later or contact sysop.\r\n"
                    )
            except ValueError:
                pass

        return True, ""

    def record_auth_failure(self, callsign: str):
        base = _base_call(callsign)
        now = _utcnow()
        with self._lock:
            self._conn.execute(
                """UPDATE access_control SET
                       auth_failures = auth_failures + 1,
                       last_failure_at = ?, updated_at = ?
                   WHERE callsign = ?""",
                (now, now, base),
            )
            self._audit_locked(base, "AUTH_FAIL", "")
            self._conn.commit()

        if self._bus:
            from core.events import EventType
            self._bus.emit(EventType.AUTH_FAILURE, data={"callsign": base})

    def record_auth_success(self, callsign: str):
        base = _base_call(callsign)
        now = _utcnow()
        with self._lock:
            self._conn.execute(
                """UPDATE access_control SET
                       auth_failures = 0, last_auth_at = ?, updated_at = ?
                   WHERE callsign = ?""",
                (now, now, base),
            )
            self._audit_locked(base, "AUTH_SUCCESS", "")
            self._conn.commit()

    # ── Listing (for GUI) ────────────────────────────────────────────

    def list_users(self, role_filter: Optional[str] = None) -> list[dict]:
        with self._lock:
            if role_filter:
                cur = self._conn.execute(
                    "SELECT * FROM access_control WHERE role=? ORDER BY callsign",
                    (role_filter.upper(),),
                )
            else:
                cur = self._conn.execute(
                    "SELECT * FROM access_control ORDER BY role, callsign"
                )
            return [dict(r) for r in cur.fetchall()]

    def list_pending(self) -> list[dict]:
        return self.list_users("PENDING")

    def get_audit_log(self, callsign: Optional[str] = None,
                      limit: int = 200) -> list[dict]:
        with self._lock:
            if callsign:
                cur = self._conn.execute(
                    "SELECT * FROM security_audit WHERE callsign=? "
                    "ORDER BY timestamp DESC LIMIT ?",
                    (_base_call(callsign), limit),
                )
            else:
                cur = self._conn.execute(
                    "SELECT * FROM security_audit "
                    "ORDER BY timestamp DESC LIMIT ?",
                    (limit,),
                )
            return [dict(r) for r in cur.fetchall()]

    # ── Migration from upload whitelist ──────────────────────────────

    def migrate_from_whitelist(self, config):
        """
        One-time migration: convert upload_whitelist entries to TRUSTED,
        blocked_callsigns to BLOCKED.  Only runs if table is empty.
        """
        with self._lock:
            cur = self._conn.execute("SELECT COUNT(*) FROM access_control")
            if cur.fetchone()[0] > 0:
                return  # Already populated

        fu = config.file_upload

        for call in fu.upload_whitelist:
            self.set_role(call, Role.TRUSTED, changed_by="MIGRATION",
                          reason="from upload_whitelist")
        for call in fu.blocked_callsigns:
            self.set_role(call, Role.BLOCKED, changed_by="MIGRATION",
                          reason="from blocked_callsigns")

        # Ensure sysop has explicit record
        sysop = config.station.sysop_callsign
        if sysop:
            self.set_role(sysop, Role.SYSOP, changed_by="MIGRATION",
                          reason="sysop_callsign")

        n_wl = len(fu.upload_whitelist)
        n_bl = len(fu.blocked_callsigns)
        log.info(f"ACCESS migrated {n_wl} whitelist + {n_bl} blocked entries")

    def ensure_sysop_row(self, config=None) -> bool:
        """Give the configured sysop callsign a SYSOP row in the user table
        if it has none (a changed Sysop Callsign has none), so it is listed
        in User Management and its password can be set there.  Called at
        start-up and on every Settings save.  Returns True if one was made."""
        cfg = config or self._cfg()
        sysop = _base_call(cfg.station.sysop_callsign or "") if cfg else ""
        if not sysop:
            return False
        with self._lock:
            row = self._conn.execute(
                "SELECT role FROM access_control WHERE callsign=?", (sysop,),
            ).fetchone()
        if row and row[0] == Role.SYSOP.value:
            return False
        self.set_role(sysop, Role.SYSOP, changed_by="SYSTEM",
                      reason="sysop_callsign")
        return True

    # ── Audit ────────────────────────────────────────────────────────

    def audit(self, callsign: str, event: str, detail: str = "",
              instance: str = ""):
        """Append to security_audit (acquires lock)."""
        with self._lock:
            self._audit_locked(callsign, event, detail, instance)
            self._conn.commit()

    def _audit_locked(self, callsign: str, event: str, detail: str = "",
                      instance: str = ""):
        """Append to security_audit.  Caller must hold self._lock."""
        self._conn.execute(
            "INSERT INTO security_audit (callsign, event, detail, instance) "
            "VALUES (?,?,?,?)",
            (_base_call(callsign), event, detail[:500], instance),
        )
