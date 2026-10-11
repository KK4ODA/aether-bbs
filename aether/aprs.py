"""
aether/aprs.py

APRS position beacons on the air through Aether HF.

The BBS's position (or status, without one) goes out as an AX.25 UI frame
in an Aether *datagram* (control API ``datagram.send``): every Aether
station that decodes it hands it to its own APRS programs over its KISS
port.  The control API is used rather than Aether's KISS port because that
port drops program frames while a host program — this BBS — holds the
VARA-compatible port without ``CHAT ON`` (Aether's "Winlink priority").

Aether judges each datagram against its rules as a transmission this
station originates: under automatic control only where it may originate.
"""

import base64
import logging
import threading
from typing import Callable, Optional

from aether.control import AetherControlClient, AetherControlError
from ax25.parser import encode_ui_frame
from core.config import AppConfig

log = logging.getLogger(__name__)

# APRS "tocall" for experimental software (APZxxx is reserved for it)
TOCALL = "APZAEB"


def build_aprs_info(cfg: AppConfig) -> str:
    """The APRS information field for the BBS: a position report when a
    position is set, otherwise a status report with the grid square."""
    from aprs.gateway import APRSGateway
    st = cfg.station
    text = st.beacon_text
    sym = cfg.aprs.beacon_symbol or "/B"
    table, code = (sym[0], sym[1]) if len(sym) >= 2 and sym[0] in "/\\" else ("/", "B")
    if st.latitude is not None and st.longitude is not None and (
            st.latitude != 0.0 or st.longitude != 0.0):
        return (f"!{APRSGateway._encode_lat(st.latitude)}{table}"
                f"{APRSGateway._encode_lon(st.longitude)}{code}{text}")
    status = f"{st.grid_square} {text}" if st.grid_square else text
    return f">{status}"


class AetherAPRSBeacon:
    """Sends the BBS's APRS beacon through one Aether HF instance."""

    def __init__(
        self,
        instance_name: str,
        client: AetherControlClient,
        config_fn: Callable[[], AppConfig],
        interval_minutes: int,
    ):
        self._name = instance_name
        self._client = client
        self._config_fn = config_fn
        self._interval_s = max(10, interval_minutes) * 60
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def frame(self) -> bytes:
        cfg = self._config_fn()
        return encode_ui_frame(cfg.station.bbs_callsign, TOCALL,
                               build_aprs_info(cfg).encode("ascii", "replace"))

    def send_once(self) -> bool:
        try:
            frame = self.frame()
        except ValueError as e:
            log.error(f"[{self._name}] APRS over Aether HF: {e}")
            return False
        try:
            res = self._client.call("datagram.send", {
                "data": base64.b64encode(frame).decode(),
                "frame_type": 0,
                "ref": "bbs-aprs",
            }) or {}
        except AetherControlError as e:
            log.warning(f"[{self._name}] APRS beacon not sent by Aether HF: {e}")
            return False
        log.info(f"[{self._name}] APRS beacon queued in Aether HF "
                 f"({res.get('fragments', '?')} fragment(s), "
                 f"~{res.get('air_s', '?')} s on the air)")
        return True

    def start(self):
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name=f"AetherAPRS-{self._name}", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _run(self):
        # Give the modem a minute to come up and settle its noise floor.
        if self._stop.wait(60):
            return
        while not self._stop.is_set():
            self.send_once()
            self._stop.wait(self._interval_s)
