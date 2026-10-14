"""
transport/protocol.py

Abstract transport broker protocol.

All transport backends (VARA FM/HF, Soundmodem AGWPE, Soundmodem KISS)
implement this interface so the BBS session layer can work with any modem.
"""

from __future__ import annotations

import logging
from enum import Enum, auto
from typing import Protocol, runtime_checkable

log = logging.getLogger(__name__)


class TransportState(Enum):
    """Connection state for any transport backend."""
    DISCONNECTED  = auto()
    CONNECTING    = auto()
    IDLE          = auto()   # connected to modem, no RF session
    CONNECTED     = auto()   # active RF session with a remote station
    DISCONNECTING = auto()


class TransportType(Enum):
    """Supported transport backends."""
    VARA_FM     = "VARA_FM"
    VARA_HF     = "VARA_HF"
    SOUNDMODEM  = "SOUNDMODEM"
    AETHER_HF   = "AETHER_HF"


@runtime_checkable
class SessionHandler(Protocol):
    """Interface that BBS session handlers must implement.

    Identical to the existing vara.broker.SessionHandler — made
    explicit here so transport backends don't import from vara/.
    """
    def on_cmd_event(self, event: str) -> None: ...
    def on_data(self, data: bytes) -> None: ...


@runtime_checkable
class TransportBroker(Protocol):
    """Interface that all transport backends must implement.

    The BBS dispatcher and instance manager interact with brokers
    exclusively through this protocol.
    """

    # ── Connection lifecycle ──────────────────────────────────────────

    def connect(self) -> None:
        """Connect to the modem (TCP socket, serial port, etc.).

        Blocking with retry.  Emits VARA_CONNECTED or equivalent event
        on success.
        """
        ...

    def disconnect(self) -> None:
        """Disconnect and retire this broker.

        Stops reconnect loops, unsubscribes from events, cleans up.
        """
        ...

    def resume(self) -> None:
        """Re-activate a previously retired broker for reuse."""
        ...

    # ── Session arbitration ───────────────────────────────────────────

    def acquire_session(
        self, handler: SessionHandler, timeout: float = 5.0,
    ) -> bool:
        """Acquire exclusive session ownership.

        Returns True if acquired within *timeout* seconds.
        """
        ...

    def release_session(self, handler: SessionHandler) -> None:
        """Release session ownership and re-enable listening."""
        ...

    # ── Data transfer ─────────────────────────────────────────────────

    def send_cmd(self, cmd: str) -> None:
        """Send a control command to the modem."""
        ...

    def send_data(self, data: bytes) -> None:
        """Send payload data over the active RF session."""
        ...

    # ── Outbound connection ───────────────────────────────────────────

    def connect_station(
        self, remote_call: str, path: str = "",
        busy_timeout: float = 30.0,
    ) -> bool:
        """Initiate an outbound connection to a remote station.

        Returns True if connected, False on timeout/error.
        """
        ...

    # ── Status ────────────────────────────────────────────────────────

    @property
    def state(self) -> TransportState:
        """Current transport state."""
        ...

    @property
    def transport_type(self) -> TransportType:
        """Which transport backend this broker represents."""
        ...

    @property
    def instance_name(self) -> str:
        """Config instance name (e.g. 'Primary')."""
        ...
