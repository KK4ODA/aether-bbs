"""
notifications/pushover.py

Push notifications via the Pushover service (https://pushover.net).
One-time $5 purchase for the phone app, no subscription.

Subscribes to PRIORITY_MESSAGE_RECEIVED on the EventBus and POSTs
to the Pushover API for instant iOS/Android delivery via APNS/GCM.

No external dependencies -- uses only urllib from the standard library.
"""

import logging
import threading
from urllib.request import Request, urlopen
from urllib.parse import urlencode
from urllib.error import URLError

from core.events import EventBus, EventType

log = logging.getLogger(__name__)

PUSHOVER_API_URL = "https://api.pushover.net/1/messages.json"

# Pushover priority levels:
#  -2  no notification/alert
#  -1  quiet (no sound/vibration)
#   0  normal
#   1  high (bypass quiet hours)
#   2  emergency (repeats until acknowledged)


class PushoverNotifier:
    """
    Fire-and-forget push notifications via Pushover.

    Subscribes to PRIORITY_MESSAGE_RECEIVED and CONFIG_CHANGED.
    All HTTP work happens on short-lived daemon threads.
    """

    def __init__(self, config, event_bus: EventBus):
        self._cm     = config if hasattr(config, "current") else None
        self._config = config if self._cm is None else None
        self._bus    = event_bus

        self._bus.subscribe(EventType.PRIORITY_MESSAGE_RECEIVED,
                            self._on_priority_message)
        self._bus.subscribe(EventType.CONFIG_CHANGED,
                            self._on_config_changed)
        log.info("PushoverNotifier initialised")

    # -- Public helpers ----------------------------------------------------

    def send_test(self):
        """Send a test notification (called from Settings GUI)."""
        cfg = self._notify_cfg()
        if not cfg.pushover_enabled:
            log.warning("Pushover test skipped -- not enabled")
            return
        self._post(
            title="Aether BBS - Test Notification",
            message="If you see this, Pushover notifications are working!",
            priority=0,  # normal for test
        )

    # -- Event handlers ----------------------------------------------------

    def _on_priority_message(self, event):
        cfg = self._notify_cfg()
        if not cfg.pushover_enabled:
            return

        d = event.data or {}
        mid       = d.get("mid", "?")
        from_call = d.get("from", "UNKNOWN")
        to_call   = d.get("to", "")
        subject   = d.get("subject", "")
        phrase    = d.get("alert_phrase", "Priority message received")
        tactical  = d.get("tactical_callsign", to_call)

        title = f"Aether BBS Alert - {tactical}"
        body_lines = [
            phrase,
            "",
            f"From:    {from_call}",
            f"To:      {to_call}",
            f"Subject: {subject}" if subject else "",
            f"MID:     {mid}",
        ]
        message = "\n".join(line for line in body_lines if line is not None)

        self._post(
            title=title,
            message=message,
            priority=cfg.pushover_priority,
        )

    def _on_config_changed(self, event):
        new_cfg = (event.data or {}).get("config")
        if new_cfg and self._cm is None:
            self._config = new_cfg

    # -- Internal ----------------------------------------------------------

    def _notify_cfg(self):
        if self._cm:
            app = self._cm.current()
            if app:
                return app.notifications
        if self._config:
            return self._config.notifications
        from core.config import NotificationConfig
        return NotificationConfig()

    def _post(self, title: str, message: str, priority: int = 1):
        """POST to Pushover API on a daemon thread."""
        cfg = self._notify_cfg()
        token = cfg.pushover_api_token.strip()
        user  = cfg.pushover_user_key.strip()

        if not token or not user:
            log.warning("Pushover: API token or user key not configured")
            return

        params = {
            "token":    token,
            "user":     user,
            "title":    title,
            "message":  message,
            "priority": str(priority),
            "sound":    cfg.pushover_sound or "siren",
        }

        # Emergency priority (2) requires retry and expire params
        if priority == 2:
            params["retry"]  = "60"    # retry every 60 seconds
            params["expire"] = "3600"  # stop after 1 hour

        def _do_post():
            try:
                data = urlencode(params).encode("utf-8")
                req = Request(PUSHOVER_API_URL, data=data, method="POST")
                with urlopen(req, timeout=15) as resp:
                    log.info(
                        f"Pushover notification sent (HTTP {resp.status})"
                    )
            except URLError as e:
                log.warning(f"Pushover delivery failed: {e}")
            except Exception as e:
                log.error(f"Pushover unexpected error: {e}", exc_info=True)

        threading.Thread(
            target=_do_post,
            name="pushover-post",
            daemon=True,
        ).start()
