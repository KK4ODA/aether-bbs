# VARA BBS

Amateur radio bulletin board system with multi-transport support (VARA FM/HF, Soundmodem AX.25, AETHER HF), Winlink integration, APRS, multi-user chat, and multi-tier message delivery.

## Features

### Transport
- **Multi-transport architecture**: VARA FM, VARA HF, Soundmodem (AGWPE/KISS), and AETHER HF
- Each instance configurable as one transport type (not mixed)
- Multi-instance modem management with per-instance PTT and rig control
- Transport auto-detection protocol (`;TRANSPORT?` query for terminal apps)
- HF PTT control via OmniRig, FLrig, Hamlib, CM108, or serial (RTS/DTR)
- HF frequency scanning with time-of-day filtering

### BBS
- Interactive BBS sessions with message store (SQLite)
- Bulletin board with configurable areas and inter-BBS forwarding
- File storage and transfer with role-based access and virus scanning
- MHeard station log

### Winlink
- Winlink CMS integration via Pat client (https://getpat.io)
- On-demand CMS polling (sysop via GUI, RF users via `WL` command)
- Per-user Winlink mail forwarding (WLREG with encrypted password exchange)
- Winlink passwords encrypted at rest using PBKDF2-derived keys
- Outbound delivery engine (Winlink primary, SMTP fallback, inbound email polling)

### Chat
- Sysop-to-user private chat (`CH` command)
- Multi-user chat rooms / converse mode (`CHAT` command, BPQ32-style)
- Room commands: `/WHO`, `/ROOM`, `/EXIT`
- Sysop GUI participation in chat rooms

### Security
- HMAC-SHA256 challenge-response authentication (password never sent over RF)
- Plaintext auth fallback for dumb terminals (configurable)
- Role-based access control: SYSOP, TRUSTED, APPROVED, PENDING, UNKNOWN, BLOCKED
- Operational modes: restricted, open, emcomm
- Rate limiting and auto-block on auth failures
- All passwords encrypted at rest (BBS passwords, Winlink passwords, gateway credentials)

### Notifications
- Push notifications via ntfy (self-hosted, no internet required)
- Pushover (iOS/Android mobile push)
- SMS via USB GSM modem (AT commands)
- Configurable event triggers (new registrations, urgent bulletins, priority messages)

### Infrastructure
- APRS-IS gateway with position beaconing
- Internet connectivity monitoring with automatic notifications
- PyQt6 GUI with dashboard, message viewer, settings, packet monitor, and log viewer
- TOML configuration with automatic migration and corrupt-config recovery

## Quick Start

1. Install dependencies: `pip install -r requirements.txt`
2. Install Pat for Winlink: https://getpat.io
3. Run: `python main.py` (or double-click `start_bbs.bat`)
4. Configure your station callsign and transport settings in the GUI

## Transport Configuration

Each instance is configured with one transport type in the TOML config:

```toml
[[instances]]
name = "VHF"
transport = "VARA_FM"       # VARA_FM, VARA_HF, SOUNDMODEM, or AETHER_HF
enabled = true

# VARA FM/HF settings
cmd_port = 8300
data_port = 8301

# Soundmodem settings (when transport = "SOUNDMODEM")
[instances.soundmodem]
protocol = "AGWPE"          # AGWPE or KISS
host = "127.0.0.1"
port = 8000

# AETHER HF settings (when transport = "AETHER_HF")
[instances.aether]
cmd_port = 8400
data_port = 8401
mode = "wide"               # wide, narrow, or emergency
```

## Terminal App

A companion terminal client lives in `terminal/` for connecting to the BBS via any supported transport. The terminal auto-detects the BBS transport type using the `;TRANSPORT?` protocol query.

## Related Projects

- [AETHER HF](https://github.com/KK4ODA/aether-hf) — Open-source HF modem protocol (alternative to VARA HF)
- [Pat](https://getpat.io) — Winlink client used for CMS integration

<!-- tota-support:start -->
## Support

VARA BBS is free and stays free. It is written by KK4ODA, who also runs
[Tiles on the Air](https://tilesontheair.com), a free program for portable
operators, and both are kept going by the people who use them. If VARA BBS is
useful to you, a contribution through the
[Tiles on the Air giving page](https://tilesontheair.com/Giving) helps keep this tool and Tiles
running. Nothing is ever locked behind it, and a bug report or an on-air
report helps just as much.
<!-- tota-support:end -->

## Contributors

- **KK4ODA** (Facundo) — Author
- **Claude** (Anthropic) — Co-developer
