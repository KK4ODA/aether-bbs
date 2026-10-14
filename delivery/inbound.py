"""
delivery/inbound.py

Inbound message delivery for Aether BBS.

Two inbound paths:

1. IMAP/POP3 Email Poller
   Polls a mailbox on a schedule, parses incoming emails, maps recipient
   local-parts to callsigns, and stores messages in the MessageStore
   for RF pickup.

2. Winlink Forward Manager
   Dynamically manages the CMS ;FW line based on registered BBS users.
   Ensures the CMS delivers mail for all registered callsigns during
   each poll cycle.

Both paths deposit messages into the same MessageStore.  RF users see
inbound emails and Winlink messages identically — they appear as
personal messages in their mailbox.
"""

import email
import email.utils
import email.header
import email.message
import hashlib
import imaplib
import poplib
import logging
import re
import threading
import time
import random
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from bbs.store import MessageStore
from core.events import EventBus, EventType

log = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
# Address Mapping — email address ↔ callsign resolution
# ═══════════════════════════════════════════════════════════════════════════════

# Valid amateur callsign pattern (US/international)
_CALLSIGN_RE = re.compile(
    r'^[A-Z]{1,2}[0-9]{1,4}[A-Z]{1,4}(-\d{1,2})?$',
    re.IGNORECASE,
)


def extract_callsign_from_email(address: str, domain: str = "") -> Optional[str]:
    """
    Extract a callsign from an email address local part.

    Mapping rules:
      kk4oda@yourbbs.net     → KK4ODA
      kk4oda-5@yourbbs.net   → KK4ODA-5
      w1abc@yourbbs.net      → W1ABC
      john.smith@gmail.com   → None (not a callsign)

    If domain is specified, only addresses matching that domain are
    considered.  Addresses at other domains return None.
    """
    if not address or "@" not in address:
        return None

    local_part, addr_domain = address.lower().rsplit("@", 1)

    # If a BBS domain is configured, only map addresses at that domain
    if domain and addr_domain.lower() != domain.lower():
        return None

    # Clean the local part — replace dots/underscores with hyphens
    cleaned = local_part.replace(".", "-").replace("_", "-").upper()

    # Check if it matches a callsign pattern
    if _CALLSIGN_RE.match(cleaned):
        return cleaned

    return None



# ═══════════════════════════════════════════════════════════════════════════════
# IMAP/POP3 Email Poller
# ═══════════════════════════════════════════════════════════════════════════════

class MailProtocol(str, Enum):
    IMAP = "IMAP"
    POP3 = "POP3"


@dataclass
class InboundEmailStats:
    """Statistics for an inbound email poll cycle."""
    fetched:    int = 0
    delivered:  int = 0
    rejected:   int = 0
    errors:     int = 0
    duration:   float = 0.0
    error_msg:  str = ""


class InboundEmailPoller:
    """
    Polls an email mailbox (IMAP or POP3) on a schedule.

    For each incoming email:
    1. Parse From, To, Subject, Body
    2. Map the To address local-part to a callsign
    3. Store in MessageStore for RF pickup
    4. Mark/delete the email on the server

    The BBS domain (e.g., yourbbs.net) determines which addresses
    are considered local.  Emails to addresses outside the BBS domain
    are ignored.

    Example:
      Email to kk4oda@aetherbbs.example.com → stored for KK4ODA
      Email to w1abc@aetherbbs.example.com  → stored for W1ABC
      Email to someone@gmail.com          → ignored (not our domain)
    """

    def __init__(
        self,
        store:     MessageStore,
        event_bus: EventBus,
    ):
        self._store = store
        self._bus = event_bus
        self._timer: Optional[threading.Timer] = None
        self._running = False
        self._lock = threading.Lock()
        self._last_stats: Optional[InboundEmailStats] = None

        # Configuration (updated via update_config)
        self._enabled = False
        self._protocol = MailProtocol.IMAP
        self._host = ""
        self._port = 993
        self._use_ssl = True
        self._username = ""
        self._password = ""
        self._folder = "INBOX"
        self._bbs_domain = ""
        self._poll_interval = 5       # minutes
        self._delete_after_fetch = False
        self._max_message_size = 50000  # bytes — RF-friendly limit
        self._allowed_senders: list[str] = []  # empty = accept all

        # Access controller reference for callsign validation
        self._access_controller = None

    def update_config(
        self,
        enabled:       bool   = False,
        protocol:      str    = "IMAP",
        host:          str    = "",
        port:          int    = 993,
        use_ssl:       bool   = True,
        username:      str    = "",
        password:      str    = "",
        folder:        str    = "INBOX",
        bbs_domain:    str    = "",
        poll_interval: int    = 5,
        delete_after_fetch: bool = False,
        max_message_size: int = 50000,
        allowed_senders: Optional[list[str]] = None,
    ):
        self._enabled = enabled
        self._protocol = MailProtocol(protocol.upper()) if protocol else MailProtocol.IMAP
        self._host = host
        self._port = port
        self._use_ssl = use_ssl
        self._username = username
        self._password = password
        self._folder = folder
        self._bbs_domain = bbs_domain
        self._poll_interval = max(1, poll_interval)
        self._delete_after_fetch = delete_after_fetch
        self._max_message_size = max_message_size
        self._allowed_senders = allowed_senders or []

        if self._running:
            self._cancel_timer()
            if self._enabled:
                self._schedule_next()

    def set_access_controller(self, ac) -> None:
        """Inject the access controller for callsign validation."""
        self._access_controller = ac

    def start(self):
        self._running = True
        if self._enabled:
            self._schedule_next()
        log.info(
            f"Inbound email poller started "
            f"(enabled={self._enabled}, protocol={self._protocol.value}, "
            f"interval={self._poll_interval}min)"
        )

    def stop(self):
        self._running = False
        self._cancel_timer()
        log.info("Inbound email poller stopped")

    def poll_now(self) -> Optional[InboundEmailStats]:
        """Trigger an immediate poll in a background thread."""
        if not self._enabled:
            log.warning("Inbound email poll requested but disabled")
            return None

        t = threading.Thread(
            target=self._do_poll, daemon=True, name="InboundEmail-Poll"
        )
        t.start()
        return None  # async

    def poll_now_blocking(self) -> InboundEmailStats:
        """Trigger an immediate poll, blocking until complete."""
        return self._do_poll()

    @property
    def is_enabled(self) -> bool:
        return self._enabled

    # ── Internal ─────────────────────────────────────────────────────────────

    def _schedule_next(self):
        # one timer only: a config change during a poll must not leave two
        # poll chains running
        self._cancel_timer()
        if not self._running or not self._enabled:
            return
        delay = self._poll_interval * 60 + random.uniform(-15, 15)
        delay = max(30, delay)
        self._timer = threading.Timer(delay, self._auto_poll_tick)
        self._timer.daemon = True
        self._timer.start()

    def _cancel_timer(self):
        if self._timer:
            self._timer.cancel()
            self._timer = None

    def _auto_poll_tick(self):
        if self._running:
            self._do_poll()
            self._schedule_next()

    def _do_poll(self) -> InboundEmailStats:
        """Execute one inbound email poll cycle."""
        if not self._lock.acquire(blocking=False):
            log.debug("Inbound email poll skipped — already in progress")
            return InboundEmailStats(error_msg="Already polling")

        stats = InboundEmailStats()
        start = time.monotonic()

        try:
            self._bus.emit(
                EventType.INBOUND_EMAIL_POLL_STARTED,
                source="inbound_email",
                data={"host": self._host, "protocol": self._protocol.value},
            )

            if self._protocol == MailProtocol.IMAP:
                stats = self._poll_imap()
            else:
                stats = self._poll_pop3()

            stats.duration = time.monotonic() - start
            self._last_stats = stats

            self._bus.emit(
                EventType.INBOUND_EMAIL_POLL_COMPLETED,
                source="inbound_email",
                data={
                    "fetched":   stats.fetched,
                    "delivered": stats.delivered,
                    "rejected":  stats.rejected,
                    "errors":    stats.errors,
                    "duration":  stats.duration,
                    "error":     stats.error_msg,
                },
            )

            if stats.delivered > 0:
                log.info(
                    f"Inbound email: delivered {stats.delivered} of "
                    f"{stats.fetched} fetched ({stats.duration:.1f}s)"
                )
            return stats

        except Exception as e:
            stats.error_msg = str(e)
            stats.duration = time.monotonic() - start
            self._last_stats = stats
            log.error(f"Inbound email poll failed: {e}", exc_info=True)
            return stats
        finally:
            self._lock.release()

    # ── IMAP implementation ──────────────────────────────────────────────────

    def _poll_imap(self) -> InboundEmailStats:
        """Fetch and process emails via IMAP."""
        stats = InboundEmailStats()

        try:
            if self._use_ssl:
                conn = imaplib.IMAP4_SSL(self._host, self._port, timeout=30)
            else:
                conn = imaplib.IMAP4(self._host, self._port, timeout=30)
        except Exception as e:
            stats.error_msg = f"IMAP connect failed: {e}"
            return stats

        try:
            conn.login(self._username, self._password)
            conn.select(self._folder)

            # Search for all unseen messages
            typ, data = conn.search(None, "UNSEEN")
            if typ != "OK" or not data[0]:
                return stats

            msg_ids = data[0].split()
            stats.fetched = len(msg_ids)

            for msg_id in msg_ids:
                try:
                    # PEEK: the server marks the mail read only once it is
                    # handled below, so one that fails is fetched again
                    typ, msg_data = conn.fetch(msg_id, "(BODY.PEEK[])")
                    if typ != "OK":
                        stats.errors += 1
                        continue

                    raw_email = msg_data[0][1]
                    if len(raw_email) > self._max_message_size:
                        log.warning(
                            f"Inbound email too large ({len(raw_email)} bytes), "
                            f"skipping"
                        )
                        stats.rejected += 1
                        conn.store(msg_id, "+FLAGS", "\\Seen")   # handled: refused
                        continue

                    delivered = self._process_email(raw_email)

                    if delivered:
                        stats.delivered += 1
                        if self._delete_after_fetch:
                            conn.store(msg_id, "+FLAGS", "\\Deleted")
                        else:
                            conn.store(msg_id, "+FLAGS", "\\Seen")
                    else:
                        stats.rejected += 1
                        conn.store(msg_id, "+FLAGS", "\\Seen")   # handled: refused

                except Exception as e:
                    log.error(f"Failed to process email {msg_id}: {e}")
                    stats.errors += 1

            if self._delete_after_fetch:
                conn.expunge()

        except imaplib.IMAP4.error as e:
            stats.error_msg = f"IMAP error: {e}"
        finally:
            try:
                conn.logout()
            except Exception:
                pass

        return stats

    # ── POP3 implementation ──────────────────────────────────────────────────

    def _poll_pop3(self) -> InboundEmailStats:
        """Fetch and process emails via POP3."""
        stats = InboundEmailStats()

        try:
            if self._use_ssl:
                conn = poplib.POP3_SSL(self._host, self._port, timeout=30)
            else:
                conn = poplib.POP3(self._host, self._port, timeout=30)
        except Exception as e:
            stats.error_msg = f"POP3 connect failed: {e}"
            return stats

        try:
            conn.user(self._username)
            conn.pass_(self._password)

            msg_count, _ = conn.stat()
            stats.fetched = msg_count

            for i in range(1, msg_count + 1):
                try:
                    resp, lines, octets = conn.retr(i)

                    if octets > self._max_message_size:
                        log.warning(
                            f"POP3 message {i} too large ({octets} bytes), skipping"
                        )
                        stats.rejected += 1
                        continue

                    raw_email = b"\r\n".join(lines)
                    delivered = self._process_email(raw_email)

                    if delivered:
                        stats.delivered += 1
                        if self._delete_after_fetch:
                            conn.dele(i)
                    else:
                        stats.rejected += 1

                except Exception as e:
                    log.error(f"Failed to process POP3 message {i}: {e}")
                    stats.errors += 1

        except poplib.error_proto as e:
            stats.error_msg = f"POP3 error: {e}"
        finally:
            try:
                conn.quit()
            except Exception:
                pass

        return stats

    # ── Email processing ─────────────────────────────────────────────────────

    def _process_email(self, raw: bytes) -> bool:
        """
        Parse a raw email and store it for the target callsign.

        Returns True if the message was stored successfully.
        """
        try:
            msg = email.message_from_bytes(raw)
        except Exception as e:
            log.error(f"Failed to parse email: {e}")
            return False

        # Extract sender
        from_addr = self._decode_address(msg.get("From", ""))
        from_display, from_email = email.utils.parseaddr(from_addr)

        # Check sender allowlist (if configured)
        if self._allowed_senders:
            sender_ok = False
            for allowed in self._allowed_senders:
                if allowed.lower() in from_email.lower():
                    sender_ok = True
                    break
            if not sender_ok:
                log.info(f"Inbound email from {from_email} rejected (not in allowlist)")
                return False

        # Extract recipients — check To, CC for our domain
        all_recipients = []
        for header in ("To", "Cc"):
            raw_addrs = msg.get_all(header, [])
            for raw_addr in raw_addrs:
                decoded = self._decode_address(raw_addr)
                _, addr = email.utils.parseaddr(decoded)
                if addr:
                    all_recipients.append(addr)

        # Map recipients to callsigns
        target_callsigns = []
        for addr in all_recipients:
            callsign = extract_callsign_from_email(addr, self._bbs_domain)
            if callsign:
                # Validate the callsign is a registered user (if AC available)
                if self._access_controller:
                    from bbs.access import Role
                    role = self._access_controller.get_role(callsign)
                    if role in (Role.BLOCKED, Role.UNKNOWN):
                        log.info(
                            f"Inbound email for {callsign} rejected "
                            f"(role={role.value})"
                        )
                        continue
                target_callsigns.append(callsign)

        if not target_callsigns:
            log.debug(f"Inbound email from {from_email}: no valid BBS recipients")
            return False

        # Extract subject
        subject = self._decode_header(msg.get("Subject", "(no subject)"))

        # Extract body — prefer plain text
        body_text = self._extract_body(msg)
        if not body_text:
            body_text = "(empty message)"

        # Truncate to RF-friendly size
        if len(body_text) > self._max_message_size:
            body_text = body_text[:self._max_message_size - 50]
            body_text += "\r\n\r\n[Message truncated for RF delivery]"

        # Build the from identity for display on RF terminals
        from_identity = from_display or from_email
        if from_display and from_email:
            from_identity = f"{from_display} <{from_email}>"

        # Store for each target callsign.  The MID comes from the email's
        # Message-ID (or its bytes), so the same email fetched again — a
        # POP3 mailbox kept on the server, an IMAP retry — is not stored
        # twice.
        email_key = (msg.get("Message-ID") or "").strip() or \
            hashlib.sha1(raw).hexdigest()
        stored = False
        for callsign in target_callsigns:
            mid = "E" + hashlib.sha1(
                f"{email_key}|{callsign}".encode("utf-8", "replace")
            ).hexdigest()[:11].upper()
            if self._store.has_message(mid):
                stored = True          # already delivered on an earlier poll
                continue
            try:
                mid = self._store.save_message(
                    mid=mid,
                    from_call=from_identity[:40],  # truncate for BBS display
                    to_call=callsign,
                    subject=subject[:80],
                    body=body_text.encode("utf-8", errors="replace"),
                    area="INBOX",
                    instance="email",
                    via_path=f"email:{from_email}",
                )

                self._store.audit_log(
                    mid, "INBOUND_EMAIL",
                    notes=f"from={from_email} to={callsign} via={self._protocol.value}",
                )

                log.info(
                    f"Inbound email stored: MID={mid} "
                    f"from={from_email} to={callsign} "
                    f"subject={subject[:40]!r}"
                )

                self._bus.emit(
                    EventType.INBOUND_EMAIL_RECEIVED,
                    source="inbound_email",
                    data={
                        "mid": mid,
                        "from": from_email,
                        "to": callsign,
                        "subject": subject,
                    },
                )
                stored = True

            except Exception as e:
                log.error(f"Failed to store inbound email for {callsign}: {e}")

        return stored

    def _decode_header(self, raw: str) -> str:
        """Decode an RFC 2047 encoded header value."""
        try:
            parts = email.header.decode_header(raw)
            decoded = []
            for part, charset in parts:
                if isinstance(part, bytes):
                    decoded.append(part.decode(charset or "utf-8", errors="replace"))
                else:
                    decoded.append(part)
            return " ".join(decoded)
        except Exception:
            return str(raw)

    def _decode_address(self, raw: str) -> str:
        """Decode an address header that may have RFC 2047 encoded display name."""
        return self._decode_header(raw)

    def _extract_body(self, msg: email.message.Message) -> str:
        """
        Extract the plain-text body from an email.
        Falls back to stripping HTML tags for HTML-only messages.
        """
        # Walk MIME parts, prefer text/plain
        if msg.is_multipart():
            plain_parts = []
            for part in msg.walk():
                ct = part.get_content_type()
                cd = str(part.get("Content-Disposition", ""))

                # Skip attachments
                if "attachment" in cd:
                    continue

                if ct == "text/plain":
                    payload = part.get_payload(decode=True)
                    if payload:
                        charset = part.get_content_charset() or "utf-8"
                        plain_parts.append(
                            payload.decode(charset, errors="replace")
                        )

            if plain_parts:
                return "\r\n".join(plain_parts)

            # No plain text found — try HTML with tag stripping
            for part in msg.walk():
                if part.get_content_type() == "text/html":
                    payload = part.get_payload(decode=True)
                    if payload:
                        charset = part.get_content_charset() or "utf-8"
                        html = payload.decode(charset, errors="replace")
                        return self._strip_html(html)

        else:
            payload = msg.get_payload(decode=True)
            if payload:
                charset = msg.get_content_charset() or "utf-8"
                text = payload.decode(charset, errors="replace")
                if msg.get_content_type() == "text/html":
                    return self._strip_html(text)
                return text

        return ""

    def _strip_html(self, html: str) -> str:
        """Crude HTML→text conversion for RF display."""
        import re as _re
        # Remove script/style blocks
        text = _re.sub(r'<(script|style)[^>]*>.*?</\1>', '', html,
                        flags=_re.DOTALL | _re.IGNORECASE)
        # Convert common block elements to newlines
        text = _re.sub(r'<(br|p|div|tr|li|h[1-6])[^>]*>', '\r\n', text,
                        flags=_re.IGNORECASE)
        # Strip remaining tags
        text = _re.sub(r'<[^>]+>', '', text)
        # Decode common entities
        text = text.replace("&amp;", "&").replace("&lt;", "<")
        text = text.replace("&gt;", ">").replace("&nbsp;", " ")
        text = text.replace("&quot;", '"')
        # Collapse whitespace
        text = _re.sub(r'\n\s*\n', '\r\n\r\n', text)
        return text.strip()

    # ── Test connection ──────────────────────────────────────────────────────

    def test_connection(self) -> dict:
        """Test mailbox connectivity. Returns dict with success/error/details."""
        start = time.monotonic()

        if not self._host:
            return {"success": False, "error": "No mail server configured"}

        try:
            if self._protocol == MailProtocol.IMAP:
                return self._test_imap(start)
            else:
                return self._test_pop3(start)
        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "duration": time.monotonic() - start,
            }

    def _test_imap(self, start: float) -> dict:
        if self._use_ssl:
            conn = imaplib.IMAP4_SSL(self._host, self._port, timeout=15)
        else:
            conn = imaplib.IMAP4(self._host, self._port, timeout=15)

        try:
            conn.login(self._username, self._password)
            conn.select(self._folder, readonly=True)

            # Count messages
            typ, data = conn.search(None, "ALL")
            total = len(data[0].split()) if typ == "OK" and data[0] else 0
            typ2, data2 = conn.search(None, "UNSEEN")
            unseen = len(data2[0].split()) if typ2 == "OK" and data2[0] else 0

            return {
                "success": True,
                "protocol": "IMAP",
                "total_messages": total,
                "unseen_messages": unseen,
                "folder": self._folder,
                "duration": time.monotonic() - start,
            }
        finally:
            try:
                conn.logout()
            except Exception:
                pass

    def _test_pop3(self, start: float) -> dict:
        if self._use_ssl:
            conn = poplib.POP3_SSL(self._host, self._port, timeout=15)
        else:
            conn = poplib.POP3(self._host, self._port, timeout=15)

        try:
            conn.user(self._username)
            conn.pass_(self._password)
            count, size = conn.stat()

            return {
                "success": True,
                "protocol": "POP3",
                "total_messages": count,
                "mailbox_size": size,
                "duration": time.monotonic() - start,
            }
        finally:
            try:
                conn.quit()
            except Exception:
                pass
