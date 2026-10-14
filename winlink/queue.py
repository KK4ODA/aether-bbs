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


def route_new_message(store: MessageStore, mid: str, to_call: str,
                      via_winlink: bool) -> str:
    """Set the delivery path of a message the sysop or an RF user just
    wrote: Winlink (QUEUED) when they chose it or the address is internet
    email, otherwise held for RF pickup (LOCAL)."""
    status = (MessageStatus.QUEUED if via_winlink or "@" in to_call
              else MessageStatus.LOCAL)
    store.set_cms_status(mid, status)
    return status


class OutboundQueue:
    """
    Manages CMS delivery status transitions for messages queued to Winlink.

    Only messages explicitly sent to Winlink are queued: the RF ``SW``
    command, an internet (@) address, or the sysop choosing Winlink when
    writing or replying in the GUI (see :func:`route_new_message`).  Mail
    for a station that collects it over RF, received bulletins and inbound
    email are never sent to the CMS.
    """

    def __init__(self, store: MessageStore, local_callsign: str = ""):
        self._store = store
        self._local_call = local_callsign.upper().split("-")[0]

    def set_local_callsign(self, callsign: str):
        self._local_call = callsign.upper().split("-")[0]

    def queue_for_cms(self, mid: str):
        """Explicitly queue a message for CMS delivery."""
        self._store.set_cms_status(mid, MessageStatus.QUEUED)
        log.info(f"Queued MID={mid} for CMS delivery")

    # ── CMS Poll Interface ───────────────────────────────────────────────

    def get_cms_outbound(self, limit: int = 25) -> list[dict]:
        """
        Get messages ready for the next CMS poll.

        Returns messages with cms_status=QUEUED, sorted by priority
        then date.  Also queues unsent internet-email messages that were
        never routed (written before routing was explicit): an @ address
        cannot be collected over RF, so Winlink is its only way out.
        """
        self._queue_unrouted_email()

        # Return QUEUED messages
        return self._store.get_cms_queued(limit)

    def _queue_unrouted_email(self):
        try:
            with self._store._lock:
                cur = self._store._conn.execute(
                    """SELECT id FROM messages
                       WHERE is_sent=0 AND (cms_status='' OR cms_status IS NULL)
                         AND to_call LIKE '%@%'
                       LIMIT 50""",
                )
                mids = [r[0] for r in cur.fetchall()]
        except Exception as e:
            log.error(f"Failed to query unrouted messages: {e}")
            return
        for mid in mids:
            self.queue_for_cms(mid)

    # ── Status Transitions ───────────────────────────────────────────────

    def mark_sending(self, mid: str):
        """Mark a message as currently being sent in a CMS session."""
        self._store.set_cms_status(mid, MessageStatus.SENDING)

    def mark_sent(self, mid: str):
        """Mark a message as successfully delivered to CMS."""
        self._store.mark_sent(mid)  # sets is_sent=1 and cms_status=SENT

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

    def cms_queue_count(self) -> int:
        """Count of messages queued for CMS delivery."""
        return len(self._store.get_cms_queued(limit=200))

