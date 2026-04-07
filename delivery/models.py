"""
delivery/models.py

Data models for the outbound delivery architecture.
All types are backend-neutral — no Telnet, SMTP, or protocol-specific
details leak into the shared model layer.
"""

import json
import logging
from enum import Enum
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger(__name__)


# ── Backend identifiers ─────────────────────────────────────────────────────

class DeliveryBackendType(str, Enum):
    """Identifies a delivery backend without leaking protocol details."""
    WINLINK   = "WINLINK"
    SMTP      = "SMTP"
    LOCAL_MTA = "LOCAL_MTA"


class WinlinkTransportType(str, Enum):
    """Winlink transport adapter selection."""
    PAT_CLIENT = "PAT_CLIENT"


# ── Delivery status lifecycle ────────────────────────────────────────────────

class DeliveryStatus(str, Enum):
    """
    Backend-neutral delivery status.

    Lifecycle:
      PENDING → ATTEMPTING → DELIVERED           (success)
      PENDING → ATTEMPTING → BACKEND_FAILED →
          FAILOVER → ATTEMPTING → DELIVERED       (failover success)
      PENDING → … → EXPIRED                       (all backends exhausted / TTL)
    """
    PENDING         = "PENDING"           # awaiting first delivery attempt
    ATTEMPTING      = "ATTEMPTING"         # currently being sent
    DELIVERED       = "DELIVERED"          # confirmed delivery via any backend
    BACKEND_FAILED  = "BACKEND_FAILED"    # current backend failed, failover pending
    FAILOVER        = "FAILOVER"          # transitioning to next backend
    EXPIRED         = "EXPIRED"           # all attempts exhausted or TTL expired


# ── Backend health ───────────────────────────────────────────────────────────

class BackendHealth(str, Enum):
    """Observable health state for each delivery backend."""
    HEALTHY     = "HEALTHY"       # accepting messages
    DEGRADED    = "DEGRADED"      # intermittent failures
    UNAVAILABLE = "UNAVAILABLE"   # all attempts failing
    DISABLED    = "DISABLED"      # turned off by sysop


# ── Failover events (backend-abstracted) ─────────────────────────────────────

class FailoverEventType(str, Enum):
    """
    Backend-abstracted failure events.
    The failover engine consumes these — never protocol-specific errors.
    """
    SMTP_PROVIDER_FAILURE       = "SMTP_PROVIDER_FAILURE"
    WINLINK_TRANSPORT_FAILURE   = "WINLINK_TRANSPORT_FAILURE"
    LOCAL_MTA_FAILURE           = "LOCAL_MTA_FAILURE"
    BACKEND_EXHAUSTED           = "BACKEND_EXHAUSTED"
    DELIVERY_CONFIRMED          = "DELIVERY_CONFIRMED"
    MESSAGE_EXPIRED             = "MESSAGE_EXPIRED"


@dataclass
class FailoverEvent:
    """Immutable record of a failover state transition."""
    event_type:   FailoverEventType
    backend:      DeliveryBackendType
    provider_id:  str   = ""           # e.g. "smtp_provider_1" or "telnet_b2f"
    message_id:   str   = ""
    error:        str   = ""
    timestamp:    str   = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict:
        return {
            "event_type":  self.event_type.value,
            "backend":     self.backend.value,
            "provider_id": self.provider_id,
            "message_id":  self.message_id,
            "error":       self.error,
            "timestamp":   self.timestamp,
        }


# ── Per-provider attempt tracking ────────────────────────────────────────────

@dataclass
class ProviderAttempt:
    """Tracks delivery attempts for one provider (SMTP or otherwise)."""
    provider_id:     str
    backend:         DeliveryBackendType
    attempt_count:   int   = 0
    last_attempt:    str   = ""
    last_error:      str   = ""
    consecutive_failures: int = 0
    backoff_until:   str   = ""      # ISO timestamp — skip attempts until this time

    def to_dict(self) -> dict:
        return {
            "provider_id":         self.provider_id,
            "backend":             self.backend.value,
            "attempt_count":       self.attempt_count,
            "last_attempt":        self.last_attempt,
            "last_error":          self.last_error,
            "consecutive_failures": self.consecutive_failures,
            "backoff_until":       self.backoff_until,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ProviderAttempt":
        return cls(
            provider_id=d.get("provider_id", ""),
            backend=DeliveryBackendType(d.get("backend", "SMTP")),
            attempt_count=d.get("attempt_count", 0),
            last_attempt=d.get("last_attempt", ""),
            last_error=d.get("last_error", ""),
            consecutive_failures=d.get("consecutive_failures", 0),
            backoff_until=d.get("backoff_until", ""),
        )


def _parse_winlink_transport(value: str) -> WinlinkTransportType:
    """Parse transport type with migration from old TELNET_B2F/CMS_API values."""
    try:
        return WinlinkTransportType(value)
    except ValueError:
        return WinlinkTransportType.PAT_CLIENT


# ── Per-message failover state (persisted in SQLite) ─────────────────────────

@dataclass
class MessageDeliveryState:
    """
    Complete delivery state for one message.
    Serialised to JSON and stored in the delivery_state column.
    Persists across application restarts.
    """
    message_id:             str
    delivery_status:        DeliveryStatus         = DeliveryStatus.PENDING
    selected_backend:       Optional[DeliveryBackendType] = None
    winlink_transport_type: WinlinkTransportType   = WinlinkTransportType.PAT_CLIENT
    provider_attempts:      dict[str, ProviderAttempt] = field(default_factory=dict)
    failover_history:       list[dict]             = field(default_factory=list)
    created_at:             str                    = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    last_updated:           str                    = ""

    def record_attempt(self, provider_id: str, backend: DeliveryBackendType,
                       error: str = ""):
        """Record a delivery attempt for a provider."""
        now = datetime.now(timezone.utc).isoformat()
        pa = self.provider_attempts.get(provider_id)
        if pa is None:
            pa = ProviderAttempt(provider_id=provider_id, backend=backend)
            self.provider_attempts[provider_id] = pa

        pa.attempt_count += 1
        pa.last_attempt = now
        if error:
            pa.last_error = error
            pa.consecutive_failures += 1
        else:
            pa.consecutive_failures = 0
        self.last_updated = now

    def record_failover(self, event: FailoverEvent):
        """Append a failover event to the history."""
        self.failover_history.append(event.to_dict())
        self.last_updated = datetime.now(timezone.utc).isoformat()

    def total_attempts(self) -> int:
        return sum(pa.attempt_count for pa in self.provider_attempts.values())

    def to_json(self) -> str:
        return json.dumps({
            "message_id":             self.message_id,
            "delivery_status":        self.delivery_status.value,
            "selected_backend":       self.selected_backend.value if self.selected_backend else None,
            "winlink_transport_type": self.winlink_transport_type.value,
            "provider_attempts":      {k: v.to_dict() for k, v in self.provider_attempts.items()},
            "failover_history":       self.failover_history,
            "created_at":             self.created_at,
            "last_updated":           self.last_updated,
        })

    @classmethod
    def from_json(cls, raw: str) -> "MessageDeliveryState":
        try:
            d = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            log.warning(f"Failed to parse delivery state JSON: {raw!r}")
            return cls(message_id="unknown")

        pa_dict = {}
        for k, v in d.get("provider_attempts", {}).items():
            pa_dict[k] = ProviderAttempt.from_dict(v)

        return cls(
            message_id=d.get("message_id", ""),
            delivery_status=DeliveryStatus(d.get("delivery_status", "PENDING")),
            selected_backend=(
                DeliveryBackendType(d["selected_backend"])
                if d.get("selected_backend") else None
            ),
            winlink_transport_type=_parse_winlink_transport(
                d.get("winlink_transport_type", "PAT_CLIENT")
            ),
            provider_attempts=pa_dict,
            failover_history=d.get("failover_history", []),
            created_at=d.get("created_at", ""),
            last_updated=d.get("last_updated", ""),
        )


# ── Delivery result ──────────────────────────────────────────────────────────

@dataclass
class DeliveryResult:
    """Outcome of a single delivery attempt through any backend."""
    success:      bool   = False
    backend:      Optional[DeliveryBackendType] = None
    provider_id:  str    = ""
    error:        str    = ""
    duration:     float  = 0.0
    details:      dict   = field(default_factory=dict)
