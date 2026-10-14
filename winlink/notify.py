"""
winlink/notify.py

Notification hooks for CMS message events.

Subscribes to the event bus and triggers notifications when:
- Messages are successfully sent to CMS
- Messages are received from CMS
- CMS delivery fails after all retries

Notification channels:
- Event bus (for GUI updates — always active)
- Log entries (always active)
- Future: ntfy, Pushover, SMS via AlertConfig
"""

import logging

from core.events import EventBus, EventType, Event

log = logging.getLogger(__name__)


class CMSNotifier:
    """
    Subscribes to CMS events and produces user-facing notifications.

    The notifier listens for CMS_MESSAGE_SENT, CMS_MESSAGE_RECEIVED,
    and CMS_POLL_COMPLETED events and logs summary notifications.

    For GUI integration, the event bus already propagates these events
    to the WinlinkPanel.  This class adds higher-level notification
    logic (e.g., batching, external push).
    """

    def __init__(self, event_bus: EventBus):
        self._bus = event_bus
        self._session_sent = 0
        self._session_received = 0
        self._session_errors: list[str] = []
        self._subscribe()

    def _subscribe(self):
        self._bus.subscribe(EventType.CMS_POLL_STARTED,     self._on_poll_started)
        self._bus.subscribe(EventType.CMS_POLL_COMPLETED,   self._on_poll_completed)
        self._bus.subscribe(EventType.CMS_MESSAGE_SENT,     self._on_message_sent)
        self._bus.subscribe(EventType.CMS_MESSAGE_RECEIVED, self._on_message_received)
        self._bus.subscribe(EventType.CMS_SESSION_ERROR,    self._on_session_error)

    def _on_poll_started(self, event: Event):
        """Reset session counters at the start of each poll."""
        self._session_sent = 0
        self._session_received = 0
        self._session_errors = []

    def _on_message_sent(self, event: Event):
        self._session_sent += 1
        data = event.data or {}
        mid = data.get("mid", "?")
        to = data.get("to", "?")
        log.info(f"📤 CMS: Sent MID={mid} to {to}")

    def _on_message_received(self, event: Event):
        self._session_received += 1
        data = event.data or {}
        mid = data.get("mid", "?")
        from_call = data.get("from", "?")
        subject = data.get("subject", "")
        log.info(f"📥 CMS: Received MID={mid} from {from_call}: {subject}")

    def _on_session_error(self, event: Event):
        data = event.data or {}
        error = data.get("error", "Unknown error")
        self._session_errors.append(error)
        log.warning(f"⚠ CMS session error: {error}")

    def _on_poll_completed(self, event: Event):
        """
        Emit a summary notification after each CMS poll completes.

        This is where external push notifications (ntfy, Pushover)
        would be triggered in a future phase.
        """
        data = event.data or {}
        error = data.get("error")
        sent = data.get("sent", 0)
        received = data.get("received", 0)
        duration = data.get("duration", 0)

        if error:
            log.warning(
                f"CMS poll finished with error after {duration:.1f}s: {error}"
            )
            return

        # Build summary
        parts = []
        if sent:
            parts.append(f"{sent} sent")
        if received:
            parts.append(f"{received} received")
        if not parts:
            parts.append("no new messages")

        summary = f"CMS poll complete ({duration:.1f}s): {', '.join(parts)}"
        log.info(summary)

        # If we received messages, this is worth a notification
        if received > 0:
            self._notify_new_mail(received, data)

    def _notify_new_mail(self, count: int, data: dict):
        """
        Trigger notification for new inbound CMS mail.

        Currently logs at INFO level.  Future: integrate with
        AlertConfig for ntfy/Pushover/SMS push.
        """
        log.info(
            f"🔔 New Winlink mail: {count} message(s) received from CMS"
        )

        # Future hook point for external notifications:
        # if self._alert_config.ntfy_enabled:
        #     send_ntfy(topic, f"New Winlink mail: {count} messages")
        # if self._alert_config.pushover_enabled:
        #     send_pushover(f"New Winlink mail: {count} messages")
