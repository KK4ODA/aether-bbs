# VARA BBS

Amateur radio bulletin board system using VARA FM/HF modems with Winlink, APRS, and multi-tier message delivery.

## Features

- Multi-instance VARA FM/HF modem management
- Interactive BBS sessions with message store (SQLite)
- Winlink CMS integration via Pat client (email for RF users)
- Winlink email access for remote users (WLREG opt-in with encrypted password exchange)
- Bulletin board with inter-BBS forwarding
- File storage and transfer with role-based access
- APRS-IS gateway with position beaconing
- Outbound delivery engine (Winlink primary, SMTP fallback)
- Internet connectivity monitoring with user/sysop notifications
- HF PTT control via OmniRig, FLrig, Hamlib, or serial
- PyQt6 GUI with settings, monitoring, and diagnostics
- Push notifications (ntfy, Pushover, SMS)
- HMAC-SHA256 challenge-response authentication
- Role-based access control with operational modes (restricted/open/emcomm)

## Quick Start

1. Install dependencies: `pip install -r requirements.txt`
2. Install Pat for Winlink: https://getpat.io
3. Run: `python main.py` (or double-click `start_bbs.bat`)
4. Configure your station callsign and VARA FM settings in the GUI

## Contributors

- **KK4ODA** (Facundo) — Author
- **Claude** (Anthropic) — Co-developer
