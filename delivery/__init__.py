"""
delivery/__init__.py

Hybrid Three-Tier Resilient Outbound Delivery Architecture
with Bidirectional Email and Winlink Integration.

Outbound (Tier 1/2/3):
  Winlink CMS → Multi-provider SMTP → Local MTA failover

Inbound:
  IMAP/POP3 email polling → callsign mapping → MessageStore
  Winlink CMS ;FW management → auto-populate from registered users
"""

from delivery.models import (
    DeliveryBackendType,
    DeliveryStatus,
    BackendHealth,
    FailoverEvent,
    ProviderAttempt,
)
from delivery.backends import (
    OutboundDeliveryBackend,
    WinlinkBackend,
    SMTPBackend,
    LocalMTABackend,
)
from delivery.engine import DeliveryEngine
from delivery.inbound import (
    InboundEmailPoller,
    WinlinkForwardManager,
    extract_callsign_from_email,
)

__all__ = [
    "DeliveryBackendType",
    "DeliveryStatus",
    "BackendHealth",
    "FailoverEvent",
    "ProviderAttempt",
    "OutboundDeliveryBackend",
    "WinlinkBackend",
    "SMTPBackend",
    "LocalMTABackend",
    "DeliveryEngine",
    "InboundEmailPoller",
    "WinlinkForwardManager",
    "extract_callsign_from_email",
]
