"""
core/util.py

Shared utility functions used across multiple BBS subsystems.
"""


def base_callsign(callsign: str) -> str:
    """Strip SSID from a callsign: KK4ODA-5 -> KK4ODA."""
    return callsign.upper().split("-")[0]
