"""
tests/test_aether_aprs.py

APRS beacons on the air through Aether HF: the AX.25 UI frame, the APRS
information field, and what is handed to the control API's datagram.send.
"""

import base64
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from aether.aprs import TOCALL, AetherAPRSBeacon, build_aprs_info
from aether.control import AetherControlError
from ax25.parser import encode_ui_frame
from core.config import AppConfig


class _Client:
    def __init__(self, fail=None):
        self.calls = []
        self.fail = fail

    def call(self, method, params=None):
        self.calls.append((method, params))
        if self.fail:
            raise AetherControlError(self.fail, code="refused")
        return {"accepted": True, "fragments": 3, "air_s": 16.2}


def _cfg(**station):
    cfg = AppConfig()
    cfg.station.bbs_callsign = "W1AAA-10"
    cfg.station.beacon_text = "Aether BBS"
    for k, v in station.items():
        setattr(cfg.station, k, v)
    return cfg


def test_ui_frame_layout():
    f = encode_ui_frame("W1AAA-10", "APZAEB", b">hi")
    assert f[:7] == bytes(c << 1 for c in b"APZAEB") + b"\xe0"     # dest, C bit
    assert f[7:14] == bytes(c << 1 for c in b"W1AAA ") + b"\x75"   # SSID 10, last
    assert f[14:16] == b"\x03\xf0" and f[16:] == b">hi"


def test_position_and_status_info():
    pos = build_aprs_info(_cfg(latitude=35.5, longitude=-82.25))
    assert pos.startswith("!3530.00N/08215.00WB") and pos.endswith("Aether BBS")
    assert build_aprs_info(_cfg(grid_square="EM85")) == ">EM85 Aether BBS"


def test_send_once_hands_frame_to_datagram_send():
    client = _Client()
    cfg = _cfg(grid_square="EM85")
    beacon = AetherAPRSBeacon("HF", client, lambda: cfg, 60)
    assert beacon.send_once() is True
    method, params = client.calls[0]
    assert method == "datagram.send" and params["frame_type"] == 0
    assert base64.b64decode(params["data"]) == encode_ui_frame(
        "W1AAA-10", TOCALL, b">EM85 Aether BBS")


def test_refusal_is_reported_not_raised():
    beacon = AetherAPRSBeacon("HF", _Client(fail="answer-only station"),
                              lambda: _cfg(), 60)
    assert beacon.send_once() is False
