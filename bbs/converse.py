"""
bbs/converse.py

Multi-user chat rooms (BPQ32-style converse mode).

Manages named chat rooms where multiple RF users and the sysop GUI
can exchange messages in real time.  Each participant has a send_fn
callable that delivers text to them.
"""

import logging
import threading
from dataclasses import dataclass, field
from typing import Callable, Optional

from core.events import EventBus, EventType

log = logging.getLogger(__name__)

DEFAULT_ROOM = "LOCAL"
MAX_ROOMS = 10


@dataclass
class Member:
    """A participant in a chat room."""
    callsign:    str
    send_fn:     Callable[[str], None]   # sends text to this member
    member_type: str = "RF"              # "RF" or "SYSOP"
    instance:    str = ""                # instance name (for RF members)


class ChatRoom:
    """A single named chat room with multiple participants."""

    def __init__(self, name: str, event_bus: Optional[EventBus] = None):
        self.name = name
        self._bus = event_bus
        self._members: dict[str, Member] = {}   # callsign → Member
        self._lock = threading.Lock()

    @property
    def member_count(self) -> int:
        with self._lock:
            return len(self._members)

    def list_members(self) -> list[str]:
        """Return list of member callsigns."""
        with self._lock:
            return list(self._members.keys())

    def join(self, member: Member) -> None:
        """Add a member to the room. Broadcasts join notification."""
        with self._lock:
            self._members[member.callsign] = member

        self._broadcast_system(f"*** {member.callsign} joined {self.name}")

        if self._bus:
            self._bus.emit(EventType.CONVERSE_JOIN, data={
                "room": self.name,
                "callsign": member.callsign,
                "member_type": member.member_type,
                "members": self.list_members(),
            })

        log.info(f"CONVERSE {member.callsign} joined room {self.name}")

    def part(self, callsign: str, reason: str = "") -> None:
        """Remove a member from the room. Broadcasts part notification."""
        with self._lock:
            member = self._members.pop(callsign, None)
        if not member:
            return

        msg = f"*** {callsign} left {self.name}"
        if reason:
            msg += f" ({reason})"
        self._broadcast_system(msg)

        if self._bus:
            self._bus.emit(EventType.CONVERSE_PART, data={
                "room": self.name,
                "callsign": callsign,
                "reason": reason,
                "members": self.list_members(),
            })

        log.info(f"CONVERSE {callsign} left room {self.name}: {reason}")

    def broadcast(self, from_call: str, text: str) -> None:
        """Send a message to all room members except the sender."""
        line = f"<{from_call}> {text}\r\n"

        with self._lock:
            targets = [
                m for call, m in self._members.items()
                if call != from_call
            ]

        for member in targets:
            try:
                member.send_fn(line)
            except Exception as e:
                log.warning(
                    f"CONVERSE send to {member.callsign} failed: {e}"
                )

        if self._bus:
            self._bus.emit(EventType.CONVERSE_MESSAGE, data={
                "room": self.name,
                "from": from_call,
                "text": text,
            })

    def _broadcast_system(self, msg: str) -> None:
        """Send a system message to ALL room members (including the actor)."""
        line = f"{msg}\r\n"
        with self._lock:
            targets = list(self._members.values())

        for member in targets:
            try:
                member.send_fn(line)
            except Exception:
                pass


class ConversationManager:
    """Manages all active chat rooms across the BBS."""

    def __init__(self, event_bus: Optional[EventBus] = None):
        self._bus = event_bus
        self._rooms: dict[str, ChatRoom] = {}
        self._lock = threading.Lock()

    def get_or_create_room(self, name: str = DEFAULT_ROOM) -> ChatRoom:
        """Get an existing room or create a new one."""
        name = name.upper().strip() or DEFAULT_ROOM
        with self._lock:
            if name not in self._rooms:
                if len(self._rooms) >= MAX_ROOMS:
                    # Evict empty rooms first
                    self._cleanup_empty()
                if len(self._rooms) >= MAX_ROOMS:
                    log.warning(f"CONVERSE max rooms ({MAX_ROOMS}) reached")
                    return self._rooms.get(DEFAULT_ROOM,
                                           self._create_room(DEFAULT_ROOM))
                return self._create_room(name)
            return self._rooms[name]

    def find_user_room(self, callsign: str) -> Optional[ChatRoom]:
        """Find which room a user is in, if any."""
        with self._lock:
            for room in self._rooms.values():
                if callsign in room.list_members():
                    return room
        return None

    def remove_user(self, callsign: str, reason: str = "disconnected") -> None:
        """Remove a user from any room they're in."""
        room = self.find_user_room(callsign)
        if room:
            room.part(callsign, reason)
            self._cleanup_empty()

    def list_rooms(self) -> list[dict]:
        """Return info about all active rooms."""
        with self._lock:
            return [
                {"name": r.name, "members": r.member_count}
                for r in self._rooms.values()
                if r.member_count > 0
            ]

    def _create_room(self, name: str) -> ChatRoom:
        room = ChatRoom(name, self._bus)
        self._rooms[name] = room
        log.info(f"CONVERSE room '{name}' created")
        return room

    def _cleanup_empty(self) -> None:
        """Remove rooms with no members."""
        empty = [
            name for name, room in self._rooms.items()
            if room.member_count == 0
        ]
        for name in empty:
            del self._rooms[name]
            log.info(f"CONVERSE room '{name}' closed (empty)")
