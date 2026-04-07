"""
terminal/main.py

VARA BBS Terminal — entry point.

Standalone client application for connecting to a VARA BBS via
VARA FM or Soundmodem (AGWPE/KISS).  Supports transport auto-detection
and multi-user chat rooms.

Usage:
    python -m terminal.main
"""

import sys
import logging

log = logging.getLogger(__name__)


def main():
    """Launch the VARA BBS Terminal application."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    log.info("VARA BBS Terminal starting")

    # TODO: Full PyQt6 GUI terminal with:
    #   - Connection settings (VARA FM or AGWPE, host/port, callsigns)
    #   - Text terminal panel for BBS interaction
    #   - Chat room panel (converse mode)
    #   - Transport auto-detection via ;TRANSPORT? query
    #   - Connection status bar

    print("VARA BBS Terminal")
    print("=" * 40)
    print()
    print("This is a scaffold for the terminal app.")
    print("Full GUI implementation is forthcoming.")
    print()
    print("Transport auto-detection protocol:")
    print("  Send: ;TRANSPORT?")
    print("  Recv: ;TRANSPORT VARA_FM")
    print("    or: ;TRANSPORT SOUNDMODEM (AGWPE)")
    print()
    print("Available transports:")
    print("  - VARA FM (via VARA modem TCP)")
    print("  - AGWPE   (via Soundmodem/Direwolf TCP)")
    print("  - KISS    (planned)")


if __name__ == "__main__":
    main()
