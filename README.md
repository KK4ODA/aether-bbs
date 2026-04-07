# VARA BBS

Amateur radio bulletin board system using VARA FM modems with Winlink, APRS, and multi-tier message delivery.

## Features

- Multi-instance VARA FM modem management with TNC broker
- Interactive BBS sessions with message store (SQLite)
- Bulletin board with inter-BBS forwarding
- File storage and transfer with role-based access
- Winlink CMS integration via Pat client
- APRS-IS gateway with position beaconing
- Three-tier outbound delivery engine (RF > Winlink > SMTP)
- Inbound email polling (IMAP/POP3) for email-to-RF delivery
- PyQt6 GUI with settings, monitoring, and session simulator
- Push notifications (ntfy, Pushover, SMS)
- HMAC-SHA256 challenge-response authentication
- Role-based access control with operational modes (restricted/open/emcomm)

## Quick Start

1. Install dependencies: `pip install -r requirements.txt`
2. Run: `python main.py` (or double-click `start_bbs.bat`)
3. Configure your station callsign and VARA FM settings in the GUI

## Contributors

- **KK4ODA** (Facundo) — Author
- **Claude** (Anthropic) — Co-developer
