"""
core/events.py

Thread-safe publish/subscribe event bus.
All cross-subsystem communication goes through here.
"""

import logging
import threading
from enum import Enum, auto
from dataclasses import dataclass, field
from typing import Any, Callable, Optional
from datetime import datetime

log = logging.getLogger(__name__)


class EventType(Enum):
    # VARA FM instance events
    VARA_CONNECTED      = auto()   # instance connected to VARA FM TCP
    VARA_DISCONNECTED   = auto()   # TCP connection to VARA FM lost
    VARA_PROCESS_UP     = auto()   # VARAFM.exe started
    VARA_PROCESS_DOWN   = auto()   # VARAFM.exe exited/crashed

    # Channel events (from VARA FM)
    SESSION_CONNECTED   = auto()   # remote station connected (RF)
    SESSION_DISCONNECTED= auto()   # remote station disconnected (RF)
    CHANNEL_BUSY        = auto()   # BUSY TRUE/FALSE
    PTT_CHANGE          = auto()   # PTT TRUE/FALSE
    BUFFER_UPDATE       = auto()   # BUFFER n bytes queued
    DATA_TX             = auto()   # text/data sent to remote station
    DATA_RX             = auto()   # text/data received from remote station

    # AX.25 / Packet events
    FRAME_RECEIVED      = auto()   # decoded AX.25 frame
    STATION_HEARD       = auto()   # station appeared in heard log

    # BBS events
    SESSION_STARTED          = auto()   # BBS session handler started
    SESSION_ENDED            = auto()   # BBS session handler completed
    SESSION_BUSY             = auto()   # connection rejected — BBS at max sessions
    MESSAGE_RECEIVED         = auto()   # new message stored
    MESSAGE_SENT             = auto()   # message forwarded to remote
    PRIORITY_MESSAGE_RECEIVED= auto()   # message to home tactical callsign
    BULLETIN_POSTED          = auto()   # bulletin saved (RF or forwarded)

    # Chat events
    CHAT_REQUEST             = auto()   # remote typed CH — sysop chat requested
    CHAT_MESSAGE             = auto()   # a line exchanged in a chat session
    CHAT_ENDED               = auto()   # chat session closed (any reason)
    SYSOP_CHAT_INITIATE      = auto()   # sysop started a chat from the GUI
    SYSOP_CHAT_END           = auto()   # sysop stopped a chat from the GUI

    # Converse (multi-user chat room) events
    CONVERSE_JOIN            = auto()   # user joined a chat room
    CONVERSE_PART            = auto()   # user left a chat room
    CONVERSE_MESSAGE         = auto()   # message broadcast in a chat room

    # File upload events
    UPLOAD_DENIED            = auto()   # upload permission check failed

    # Security / access control events
    ACCESS_REQUEST           = auto()   # new user registration pending
    ACCESS_GRANTED           = auto()   # sysop approved a user
    ACCESS_BLOCKED           = auto()   # user blocked (manual or auto)
    AUTH_FAILURE             = auto()   # authentication failed
    SESSION_PENDING          = auto()   # session for pending/unknown user (limited)
    SESSION_PENDING_CANCELLED = auto()  # pending session ended/cancelled

    # APRS events
    APRS_BEACON_SENT    = auto()
    APRS_FRAME_RECEIVED = auto()
    APRS_IS_CONNECTED   = auto()
    APRS_IS_DISCONNECTED= auto()

    # Winlink CMS events
    CMS_POLL_STARTED    = auto()   # CMS session beginning
    CMS_POLL_COMPLETED  = auto()   # CMS session finished (data has stats)
    CMS_SESSION_ERROR   = auto()   # CMS connection/auth failure
    CMS_MESSAGE_SENT    = auto()   # individual message accepted by CMS
    CMS_MESSAGE_RECEIVED= auto()   # individual message downloaded from CMS

    # Outbound delivery events
    DELIVERY_ATTEMPTED       = auto()   # delivery attempt started
    DELIVERY_SUCCEEDED       = auto()   # message delivered via any backend
    DELIVERY_FAILED          = auto()   # delivery attempt failed
    DELIVERY_FAILOVER        = auto()   # backend failover occurred
    DELIVERY_EXPIRED         = auto()   # message expired after all retries
    DELIVERY_BACKEND_HEALTH  = auto()   # backend health state changed

    # Inbound delivery events
    INBOUND_EMAIL_POLL_STARTED   = auto()   # email poll cycle beginning
    INBOUND_EMAIL_POLL_COMPLETED = auto()   # email poll cycle finished
    INBOUND_EMAIL_RECEIVED       = auto()   # individual email stored for RF user

    # Application events
    CONFIG_CHANGED      = auto()
    SHUTDOWN            = auto()

    # Software update events
    UPDATE_CHECK_STARTED = auto()
    UPDATE_AVAILABLE     = auto()   # data: UpdateCheckResult
    UPDATE_STARTED       = auto()
    UPDATE_COMPLETE      = auto()   # data: UpdateApplyResult
    UPDATE_FAILED        = auto()   # data: error message string
    LOG_ENTRY           = auto()   # for GUI log viewer
    SIMULATOR_ACTIVE    = auto()   # simulator started for an instance
    SIMULATOR_INACTIVE  = auto()   # simulator stopped for an instance

    # Rig control / scanning events
    SCAN_FREQ_CHANGED   = auto()   # radio tuned to a new scan channel


@dataclass
class Event:
    type:      EventType
    source:    str           = ""        # instance name or module name
    data:      Any           = None
    timestamp: datetime      = field(default_factory=datetime.now)


Subscriber = Callable[[Event], None]


class EventBus:
    """
    Thread-safe publish/subscribe bus.

    Subscribers are called synchronously in the publishing thread.
    For GUI subscribers, wrap callbacks with Qt signals to avoid
    cross-thread widget updates.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._subscribers: dict[EventType, list[Subscriber]] = {}

    def subscribe(
        self,
        event_type: EventType,
        callback: Subscriber,
    ) -> None:
        with self._lock:
            self._subscribers.setdefault(event_type, []).append(callback)

    def subscribe_all(self, callback: Subscriber) -> None:
        """Subscribe to every event type."""
        for et in EventType:
            self.subscribe(et, callback)

    def unsubscribe(self, event_type: EventType, callback: Subscriber) -> None:
        with self._lock:
            subs = self._subscribers.get(event_type, [])
            try:
                subs.remove(callback)
            except ValueError:
                pass

    def publish(self, event: Event) -> None:
        with self._lock:
            subs = list(self._subscribers.get(event.type, []))
        for sub in subs:
            try:
                sub(event)
            except Exception as e:
                log.error(
                    f"EventBus subscriber error on {event.type.name}: {e}",
                    exc_info=True,
                )

    def emit(
        self,
        event_type: EventType,
        source: str = "",
        data: Any = None,
    ) -> None:
        """Convenience wrapper."""
        self.publish(Event(type=event_type, source=source, data=data))
