"""
aether — Aether HF modem integration for Aether BBS.

Aether HF (https://github.com/KK4ODA/aether-hf) is an open-source HF data
modem.  Its daemon, ``aetherd``, offers two interfaces the BBS uses:

  * the VARA-compatible host interface (command port, data port = +1),
    which carries BBS sessions exactly as VARA HF's does, and
  * the control API (JSON over HTTP/WebSocket, default port 8515), which
    the BBS polls for the modem's version, state, bandwidth, signal
    reports, faults and the stations it has heard.

Aether HF is installed and set up on its own (its Setup tab owns the
callsign rules, sound card, keying and regulatory profile).  The BBS
connects to it, optionally launches it, and reports what it is doing.
"""

from aether.control import AetherControlClient, AetherControlError
from aether.launcher import find_aether_executable
from aether.monitor import AetherMonitor

__all__ = [
    "AetherControlClient",
    "AetherControlError",
    "AetherMonitor",
    "find_aether_executable",
]
