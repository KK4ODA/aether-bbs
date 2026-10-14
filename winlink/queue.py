"""
winlink/queue.py

OutboundQueue — manages message routing and CMS delivery status.

Integrates with MessageStore's cms_status field to track the full
lifecycle of outbound messages:
  QUEUED → SENDING → SENT/REJECTED/FAILED

Routing rules determine which messages should go via CMS vs stay
local for RF pickup.
"""

import logging

from bbs.store import MessageStore, MessageStatus

log = logging.getLogger(__name__)

# Maximum CMS delivery attempts before marking FAILED
MAX_CMS_ATTEMPTS = 5


class OutboundQueue:
    """
    Routes outbound messages to the appropriate delivery path and
    manages CMS delivery status transitions.

    Routing rules:
    1. Messages with SMTP: addresses (internet email) → CMS
    2. Messages to non-local callsigns → CMS
    3. Messages to local callsigns → hold for RF pickup (LOCAL)
    4. Messages flagged for HF → deferred to forwarding window (future)
    """

    def __init__(self, store: MessageStore, local_callsign: str = ""):
        self._store = store
        self._local_call = local_callsign.upper().split("-")[0]

    def set_local_callsign(self, callsign: str):
        self._local_call = callsign.upper().split("-")[0]

    # ── Routing & Queuing ────────────────────────────────────────────────

    def route_message(self, mid: str) -> str:
        """
        Evaluate a newly saved message and set its initial CMS status.

        Returns the assigned status (QUEUED or LOCAL).
        Called after save_message() to determine delivery path.
        """
        msg = self._store.get_message(mid)
        if not msg:
            log.warning(f"route_message: MID={mid} not found")
            return MessageStatus.NONE

        to = msg.get("to_call", "").upper()

        # Already has a CMS status? Skip re-routing.
        existing = msg.get("cms_status", "")
        if existing and existing != MessageStatus.NONE:
            return existing

        # Messages with @ are internet email → always CMS
        if "@" in to:
            self._store.set_cms_status(mid, MessageStatus.QUEUED)
            log.info(f"Routed MID={mid} → CMS (SMTP address)")
            return MessageStatus.QUEUED

        # Messages to the BBS itself → local
        if to == self._local_call:
            self._store.set_cms_status(mid, MessageStatus.LOCAL)
            return MessageStatus.LOCAL

        # Messages to other callsigns → CMS
        if to:
            self._store.set_cms_status(mid, MessageStatus.QUEUED)
            log.info(f"Routed MID={mid} → CMS (non-local callsign {to})")
            return MessageStatus.QUEUED

        return MessageStatus.NONE

    def queue_for_cms(self, mid: str):
        """Explicitly queue a message for CMS delivery."""
        self._store.set_cms_status(mid, MessageStatus.QUEUED)
        log.info(f"Queued MID={mid} for CMS delivery")

    def requeue_failed(self, mid: str):
        """Re-queue a FAILED message for another CMS delivery attempt."""
        msg = self._store.get_message(mid)
        if not msg:
            return
        if msg.get("cms_status") == MessageStatus.FAILED:
            self._store.set_cms_status(mid, MessageStatus.QUEUED)
            log.info(f"Re-queued FAILED MID={mid} for CMS retry")

    # ── CMS Poll Interface ───────────────────────────────────────────────

    def get_cms_outbound(self, limit: int = 25) -> list[dict]:
        """
        Get messages ready for the next CMS poll.

        Returns messages with cms_status=QUEUED, sorted by priority
        then date.  Also picks up any unsent messages that haven't
        been routed yet (cms_status empty) and routes them on the fly.
        """
        # First, route any unrouted unsent messages
        self._route_unrouted()

        # Return QUEUED messages
        return self._store.get_cms_queued(limit)

    def _route_unrouted(self):
        """Find unsent messages with no cms_status and route them."""
        try:
            with self._store._lock:
                cur = self._store._conn.execute(
                    """SELECT id, from_call, to_call FROM messages
                       WHERE is_sent=0 AND (cms_status='' OR cms_status IS NULL)
                       LIMIT 50""",
                )
                unrouted = [dict(r) for r in cur.fetchall()]
        except Exception as e:
            log.error(f"Failed to query unrouted messages: {e}")
            return

        for msg in unrouted:
            self.route_message(msg["id"])

    # ── Status Transitions ───────────────────────────────────────────────

    def mark_sending(self, mid: str):
        """Mark a message as currently being sent in a CMS session."""
        self._store.set_cms_status(mid, MessageStatus.SENDING)

    def mark_sent(self, mid: str):
        """Mark a message as successfully delivered to CMS."""
        self._store.mark_sent(mid)  # sets is_sent=1 and cms_status=SENT

    def mark_rejected(self, mid: str):
        """Mark a message as rejected by CMS (already had it)."""
        self._store.set_cms_status(mid, MessageStatus.REJECTED)
        # Also mark as "sent" since CMS confirmed it has the message
        with self._store._lock:
            self._store._conn.execute(
                "UPDATE messages SET is_sent=1 WHERE id=?", (mid,),
            )
            self._store._conn.commit()

    def mark_failed(self, mid: str, reason: str = ""):
        """
        Record a failed delivery attempt.  If max attempts exceeded,
        mark as FAILED; otherwise leave as QUEUED for retry.
        """
        attempts = self._store.increment_cms_attempts(mid)
        if attempts >= MAX_CMS_ATTEMPTS:
            self._store.set_cms_failed(mid, reason)
            log.warning(
                f"MID={mid} failed after {attempts} attempts — "
                f"marked FAILED: {reason}"
            )
        else:
            # Reset to QUEUED for next poll attempt
            self._store.set_cms_status(
                mid, MessageStatus.QUEUED,
                notes=f"attempt {attempts}/{MAX_CMS_ATTEMPTS}: {reason}",
            )
            log.info(
                f"MID={mid} delivery attempt {attempts}/{MAX_CMS_ATTEMPTS} "
                f"failed: {reason}"
            )

    def mark_received(self, mid: str):
        """Mark an inbound message as received from CMS."""
        self._store.set_cms_status(mid, MessageStatus.RECEIVED)

    # ── Rollback ─────────────────────────────────────────────────────────

    def rollback_sending(self):
        """
        Reset any SENDING messages back to QUEUED.
        Called on session error / disconnect to avoid stuck messages.
        """
        sending = self._store.get_messages_by_cms_status(
            MessageStatus.SENDING, limit=50,
        )
        for msg in sending:
            self._store.set_cms_status(msg["id"], MessageStatus.QUEUED,
                                       notes="rollback after session error")
        if sending:
            log.info(f"Rolled back {len(sending)} SENDING → QUEUED")

    # ── Convenience ──────────────────────────────────────────────────────

    def get_local_pending(self, callsign: str) -> list[dict]:
        """Get messages waiting for local RF pickup by a specific callsign."""
        return self._store.get_messages_for(callsign)

    def cms_queue_count(self) -> int:
        """Count of messages queued for CMS delivery."""
        return len(self._store.get_cms_queued(limit=200))

    def get_status_summary(self) -> dict[str, int]:
        """Return counts of messages in each CMS status."""
        summary = {}
        for status in [MessageStatus.QUEUED, MessageStatus.SENDING,
                        MessageStatus.SENT, MessageStatus.REJECTED,
                        MessageStatus.FAILED, MessageStatus.RECEIVED,
                        MessageStatus.LOCAL]:
            msgs = self._store.get_messages_by_cms_status(status, limit=9999)
            if msgs:
                summary[status] = len(msgs)
        return summary
