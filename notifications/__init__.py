# notifications — push notification integrations for VARA BBS

"""
notifications/__init__.py

NotificationRouter — translates system events into push notifications.

Subscribes to ACCESS_REQUEST and BULLETIN_POSTED on the EventBus,
checks the NotificationConfig toggles, and re-emits
PRIORITY_MESSAGE_RECEIVED so the existing notification backends
(ntfy, Pushover, SMS) deliver them without modification.
"""

import logging
from core.events import EventBus, EventType

log = logging.getLogger(__name__)


class NotificationRouter:
    """
    Bridges system events to the push notification pipeline.

    Subscribes to:
      - ACCESS_REQUEST     → "New user registration pending sysop approval"
      - BULLETIN_POSTED    → "Urgent bulletin posted" (if area is in urgent list)

    Re-emits PRIORITY_MESSAGE_RECEIVED with formatted data so the
    existing NtfyNotifier, PushoverNotifier, and SMSNotifier handle
    delivery without changes.
    """

    def __init__(self, config, event_bus: EventBus):
        self._cm   = config if hasattr(config, "current") else None
        self._config = config if self._cm is None else None
        self._bus  = event_bus

        self._bus.subscribe(EventType.ACCESS_REQUEST,
                            self._on_access_request)
        self._bus.subscribe(EventType.BULLETIN_POSTED,
                            self._on_bulletin_posted)
        self._bus.subscribe(EventType.CONFIG_CHANGED,
                            self._on_config_changed)
        log.info("NotificationRouter initialised")

    # ── Event handlers ────────────────────────────────────────────────────

    def _on_access_request(self, event):
        cfg = self._notify_cfg()
        if not cfg.notify_on_registration:
            return
        if not self._any_backend_enabled(cfg):
            return

        d = event.data or {}
        callsign = d.get("callsign", "UNKNOWN")
        instance = d.get("instance", "")
        ref      = d.get("community_ref", "")

        self._bus.emit(EventType.PRIORITY_MESSAGE_RECEIVED, data={
            "mid":                "REG",
            "from":               callsign,
            "to":                 "SYSOP",
            "subject":            "New registration pending approval",
            "alert_phrase":       f"New user {callsign} registered and awaiting approval",
            "tactical_callsign":  "SYSOP",
        })
        log.info(f"Registration notification dispatched for {callsign}")

    def _on_bulletin_posted(self, event):
        cfg = self._notify_cfg()
        if not cfg.notify_on_urgent_bulletin:
            return
        if not self._any_backend_enabled(cfg):
            return

        d = event.data or {}
        area       = (d.get("area", "") or "").upper()
        from_call  = d.get("from", "UNKNOWN")
        to_group   = d.get("to_group", area)
        subject    = d.get("subject", "")
        mid        = d.get("mid", "?")
        is_urgent  = d.get("is_urgent", False)

        # Fire if the bulletin is explicitly flagged urgent OR the area
        # is in the sysop's urgent area list
        urgent_areas = {a.upper() for a in cfg.urgent_bulletin_areas}
        if not is_urgent and area not in urgent_areas:
            return

        label = "URGENT bulletin" if is_urgent else f"Bulletin in urgent area {area}"
        self._bus.emit(EventType.PRIORITY_MESSAGE_RECEIVED, data={
            "mid":                mid,
            "from":               from_call,
            "to":                 to_group,
            "subject":            subject,
            "alert_phrase":       f"{label} posted by {from_call}",
            "tactical_callsign":  area,
        })
        log.info(f"Urgent bulletin notification dispatched: "
                 f"area={area} from={from_call} MID={mid} "
                 f"is_urgent={is_urgent}")

    def _on_config_changed(self, event):
        new_cfg = (event.data or {}).get("config")
        if new_cfg and self._cm is None:
            self._config = new_cfg

    # ── Internal ──────────────────────────────────────────────────────────

    def _notify_cfg(self):
        if self._cm:
            app = self._cm.current()
            if app:
                return app.notifications
        if self._config:
            return self._config.notifications
        from core.config import NotificationConfig
        return NotificationConfig()

    @staticmethod
    def _any_backend_enabled(cfg) -> bool:
        """Return True if at least one notification backend is on."""
        return cfg.enabled or cfg.pushover_enabled or cfg.sms_enabled
