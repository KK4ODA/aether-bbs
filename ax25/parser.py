"""
ax25/parser.py

AX.25 callsign and path parsing utilities.
VARA FM handles AX.25 framing internally; this module deals with
the addressing information surfaced through VARA FM events and
the application-level protocol headers.
"""

import re
import logging
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger(__name__)

# AX.25 callsign regex: 1-6 alphanumeric chars, optional -SSID (0-15)
_CALLSIGN_RE = re.compile(
    r"^([A-Z0-9]{1,6})(?:-(\d{1,2}))?$", re.IGNORECASE
)


@dataclass
class AX25Address:
    callsign: str
    ssid:     int = 0
    has_been_repeated: bool = False

    def __str__(self) -> str:
        if self.ssid:
            return f"{self.callsign.upper()}-{self.ssid}"
        return self.callsign.upper()

    def base_call(self) -> str:
        """Return callsign without SSID."""
        return self.callsign.upper()


@dataclass
class AX25Frame:
    """Decoded AX.25 addressing information extracted from VARA FM events."""
    source:      AX25Address
    destination: AX25Address
    path:        list[AX25Address] = field(default_factory=list)
    info:        bytes = b""
    frame_type:  str  = "UNKNOWN"  # CONNECT, DATA, DISCONNECT, UI

    def path_string(self) -> str:
        return ",".join(str(a) for a in self.path)

    def __str__(self) -> str:
        parts = [str(self.source), ">", str(self.destination)]
        if self.path:
            parts += [",", self.path_string()]
        return "".join(parts)


# ── Callsign parsing ─────────────────────────────────────────────────────────

def parse_callsign(text: str) -> Optional[AX25Address]:
    """
    Parse a callsign string like W1ABC-10 into an AX25Address.
    Returns None if the string is not a valid callsign.
    """
    text = text.strip().upper()
    m = _CALLSIGN_RE.match(text)
    if not m:
        return None
    call = m.group(1)
    ssid = int(m.group(2)) if m.group(2) else 0
    if ssid > 15:
        return None
    return AX25Address(callsign=call, ssid=ssid)


def parse_path(path_str: str) -> list[AX25Address]:
    """
    Parse a comma-separated digipeater path string.
    e.g. "WIDE1-1,W1DEF-2" → [AX25Address("WIDE1",1), AX25Address("W1DEF",2)]
    """
    if not path_str.strip():
        return []
    addrs = []
    for part in path_str.split(","):
        part = part.strip()
        repeated = part.endswith("*")
        if repeated:
            part = part[:-1]
        addr = parse_callsign(part)
        if addr:
            addr.has_been_repeated = repeated
            addrs.append(addr)
        else:
            log.warning(f"Invalid path element: {part!r}")
    return addrs


def validate_path(path: list[AX25Address], max_hops: int = 2) -> bool:
    """
    Validate that a digipeater path is reasonable.
    Warns if the path exceeds max_hops.
    """
    if len(path) > max_hops:
        log.warning(
            f"Path has {len(path)} hops (max recommended {max_hops}): "
            f"{', '.join(str(a) for a in path)}"
        )
        return False
    return True


def format_connect_path(path_str: str) -> str:
    """
    Validate and normalise a path string for use in a CONNECT command.
    Returns empty string if invalid.
    """
    if not path_str.strip():
        return ""
    addrs = parse_path(path_str)
    if not addrs:
        return ""
    validate_path(addrs)
    return ",".join(str(a) for a in addrs)


# ── Frame factory helpers ─────────────────────────────────────────────────────

def make_frame_from_vara_event(
    event: str,
    instance_name: str = "",
) -> Optional[AX25Frame]:
    """
    Build a basic AX25Frame from a VARA FM CONNECTED event string.
    e.g. "CONNECTED W1ABC-10 W9XYZ"
    """
    parts = event.strip().split()
    if len(parts) < 3:
        return None

    src  = parse_callsign(parts[1])
    dest = parse_callsign(parts[2])
    if not src or not dest:
        return None

    return AX25Frame(
        source=dest,        # the remote station is the source of the call
        destination=src,    # our BBS is the destination
        frame_type="CONNECT",
    )
