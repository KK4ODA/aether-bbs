"""
delivery/__init__.py

Internet email for RF users:
  IMAP/POP3 email polling → callsign mapping → MessageStore
  Winlink CMS ;FW management → auto-populate from registered users
"""

from delivery.inbound import (
    InboundEmailPoller,
    WinlinkForwardManager,
    extract_callsign_from_email,
)

__all__ = [
    "InboundEmailPoller",
    "WinlinkForwardManager",
    "extract_callsign_from_email",
]
