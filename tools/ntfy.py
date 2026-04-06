"""
notifications/ntfy.py

Lightweight push-notification client for ntfy (https://ntfy.sh).
Subscribes to PRIORITY_MESSAGE_RECEIVED on the EventBus and POSTs
an alert to a local (or remote) ntfy server.

No external dependencies — uses only urllib from the standard library.
"""

import json
import logging
import threading
from urllib.request import Request, urlopen
from urllib.error import URLError

from core.events import EventBus, EventType

log = logging.getLogger(__name__)

# Map config priority names → ntfy integer priorities
PRIORITY_MAP = {
    "min":     1,
    "low":     2,
    "default": 3,
    "high":    4,
    "urgent":  5,
}


class NtfyNotifier:
    """
    Fire-and-forget push notifications via ntfy.

    Subscribes to PRIORITY_MESSAGE_RECEIVED and CONFIG_CHANGED.
    All HTTP work happens on short-lived daemon threads so the
    EventBus is never blocked.
    """

    def __init__(self, config, event_bus: EventBus):
        # Accept ConfigManager or plain AppConfig
        self._cm   = config if hasattr(config, "current") else None
        self._config = config if self._cm is None else None
        self._bus  = event_bus

        self._bus.subscribe(EventType.PRIORITY_MESSAGE_RECEIVED,
                            self._on_priority_message)
        self._bus.subscribe(EventType.CONFIG_CHANGED,
                            self._on_config_changed)
        log.info("NtfyNotifier initialised")

    # ── Public helpers ────────────────────────────────────────────────────

    def send_test(self):
        """Send a test notification (called from Settings GUI)."""
        cfg = self._ntfy_cfg()
        if not cfg.enabled:
            log.warning("ntfy test skipped — notifications disabled")
            return
        self._post(
            title="VARA BBS — Test Notification",
            message="If you see this, ntfy push notifications are working!",
            tags=["white_check_mark", "radio"],
            priority=cfg.priority,
        )

    # ── Event handlers ────────────────────────────────────────────────────

    def _on_priority_message(self, event):
        cfg = self._ntfy_cfg()
        if not cfg.enabled:
            return

        d = event.data or {}
        mid       = d.get("mid", "?")
        from_call = d.get("from", "UNKNOWN")
        to_call   = d.get("to", "")
        subject   = d.get("subject", "")
        phrase    = d.get("alert_phrase", "Priority message received")
        tactical  = d.get("tactical_callsign", to_call)

        title = f"⚠ VARA BBS Alert — {tactical}"
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
            tags=["rotating_light", "radio"],
            priority=cfg.priority,
        )

    def _on_config_changed(self, event):
        new_cfg = (event.data or {}).get("config")
        if new_cfg and self._cm is None:
            self._config = new_cfg

    # ── Internal ──────────────────────────────────────────────────────────

    def _ntfy_cfg(self):
        """Return the live NotificationConfig."""
        if self._cm:
            app = self._cm.current()
            if app:
                return app.notifications
        if self._config:
            return self._config.notifications
        # Fallback — return a disabled config
        from core.config import NotificationConfig
        return NotificationConfig(enabled=False)

    def _post(self, title: str, message: str, tags: list[str],
              priority: str = "urgent"):
        """POST to the ntfy server on a daemon thread."""
        cfg = self._ntfy_cfg()
        url = f"{cfg.ntfy_url.rstrip('/')}/{cfg.topic}"

        def _do_post():
            try:
                headers = {
                    "Title":    title,
                    "Priority": str(PRIORITY_MAP.get(priority, 5)),
                    "Tags":     ",".join(tags),
                }
                req = Request(
                    url,
                    data=message.encode("utf-8"),
                    headers=headers,
                    method="POST",
                )
                with urlopen(req, timeout=10) as resp:
                    log.info(
                        f"ntfy notification sent → {url} "
                        f"(HTTP {resp.status})"
                    )
            except URLError as e:
                log.warning(
                    f"ntfy delivery failed → {url}: {e} — "
                    f"is the ntfy server running?"
                )
            except Exception as e:
                log.error(f"ntfy unexpected error: {e}", exc_info=True)

        threading.Thread(
            target=_do_post,
            name="ntfy-post",
            daemon=True,
        ).start()
