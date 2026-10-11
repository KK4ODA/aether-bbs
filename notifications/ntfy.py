"""
notifications/ntfy.py

Lightweight push-notification client for ntfy (https://ntfy.sh).
Subscribes to PRIORITY_MESSAGE_RECEIVED on the EventBus and POSTs
an alert to a local (or remote) ntfy server.

Supports dual-posting: local server for browser dashboard + cloud
relay (ntfy.sh) for instant iOS push notifications.

No external dependencies — uses only urllib from the standard library.
"""

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


def _ascii_safe(text: str) -> str:
    """Sanitise text for use in HTTP headers (must be latin-1 safe)."""
    return text.encode("ascii", errors="replace").decode("ascii")


class NtfyNotifier:
    """
    Fire-and-forget push notifications via ntfy.

    Subscribes to PRIORITY_MESSAGE_RECEIVED and CONFIG_CHANGED.
    All HTTP work happens on short-lived daemon threads so the
    EventBus is never blocked.

    When cloud_relay is enabled, each notification is posted to
    BOTH the local server and the cloud URL (e.g. ntfy.sh) using
    the same topic name.  This gives instant delivery on iOS while
    keeping a local copy for the browser dashboard.
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
            log.warning("ntfy test skipped -- notifications disabled")
            return
        self._post(
            title="Aether BBS - Test Notification",
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

    def _build_urls(self) -> list[str]:
        """Return list of full ntfy URLs to post to."""
        cfg = self._ntfy_cfg()
        topic = cfg.topic
        urls = [f"{cfg.ntfy_url.rstrip('/')}/{topic}"]
        if cfg.cloud_relay:
            cloud = f"{cfg.cloud_url.rstrip('/')}/{topic}"
            if cloud != urls[0]:
                urls.append(cloud)
        return urls

    def _post(self, title: str, message: str, tags: list[str],
              priority: str = "urgent"):
        """POST to all configured ntfy endpoints on daemon threads."""
        urls = self._build_urls()

        for url in urls:
            def _do_post(target_url=url):
                try:
                    headers = {
                        "Title":    _ascii_safe(title),
                        "Priority": str(PRIORITY_MAP.get(priority, 5)),
                        "Tags":     ",".join(tags),
                    }
                    req = Request(
                        target_url,
                        data=message.encode("utf-8"),
                        headers=headers,
                        method="POST",
                    )
                    with urlopen(req, timeout=10) as resp:
                        log.info(
                            f"ntfy notification sent → {target_url} "
                            f"(HTTP {resp.status})"
                        )
                except URLError as e:
                    log.warning(
                        f"ntfy delivery failed → {target_url}: {e} — "
                        f"is the ntfy server running?"
                    )
                except Exception as e:
                    log.error(f"ntfy unexpected error: {e}", exc_info=True)

            threading.Thread(
                target=_do_post,
                name="ntfy-post",
                daemon=True,
            ).start()
