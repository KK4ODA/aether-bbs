"""
ax25/router.py

Packet routing logic:
  - Heard station log updates
  - Digipeater path validation for outbound connects
  - Frame logging to the packet logger
"""

import logging
from datetime import datetime

from core.events import EventBus, EventType
from ax25.parser import AX25Frame, parse_callsign, parse_path

log = logging.getLogger(__name__)
pkt_log = logging.getLogger("packet")


class PacketRouter:
    """
    Processes AX.25 addressing information from VARA FM events.
    Logs frames to the packet logger.
    Notifies the EventBus of heard-station events.
    """

    def __init__(self, event_bus: EventBus):
        self._bus = event_bus

    def log_frame(
        self,
        frame: AX25Frame,
        instance: str,
        direction: str = "RX",
    ) -> None:
        """Write a frame to the packet log and emit FRAME_RECEIVED event."""
        pkt_log.info(
            f"{direction}  [{instance}]  {frame}  "
            f"type={frame.frame_type}  bytes={len(frame.info)}"
        )
        self._bus.emit(
            EventType.FRAME_RECEIVED,
            source=instance,
            data={
                "frame":     str(frame),
                "direction": direction,
                "type":      frame.frame_type,
                "source":    str(frame.source),
                "dest":      str(frame.destination),
                "path":      frame.path_string(),
                "bytes":     len(frame.info),
                "timestamp": datetime.now().isoformat(),
            },
        )

    def log_heard_station(
        self,
        callsign: str,
        instance: str,
        via_path: str = "",
    ) -> None:
        """Emit a STATION_HEARD event for database logging."""
        addr = parse_callsign(callsign)
        if not addr:
            return
        self._bus.emit(
            EventType.STATION_HEARD,
            source=instance,
            data={
                "callsign":  addr.base_call(),
                "ssid":      addr.ssid,
                "via_path":  via_path,
                "timestamp": datetime.now().isoformat(),
            },
        )
        log.debug(f"Heard station: {addr} via [{instance}]")

    def validate_inbound_path(
        self,
        path_str: str,
        max_hops: int = 2,
    ) -> bool:
        """
        Validate an inbound connect path.
        Logs a warning for excessively long paths.
        """
        if not path_str:
            return True
        addrs = parse_path(path_str)
        if len(addrs) > max_hops:
            log.warning(
                f"Inbound connection has {len(addrs)}-hop path "
                f"(max {max_hops}): {path_str}"
            )
            return False
        return True

    def build_connect_path(self, raw_path: str) -> str:
        """
        Validate and normalise an outbound connect path.
        Returns empty string for direct connects.
        """
        if not raw_path or not raw_path.strip():
            return ""
        addrs = parse_path(raw_path)
        if not addrs:
            log.warning(f"Invalid connect path: {raw_path!r}")
            return ""
        return ",".join(str(a) for a in addrs)
