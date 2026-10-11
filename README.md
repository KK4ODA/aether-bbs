# Aether BBS

Amateur radio bulletin board system for VARA FM, VARA HF, [Aether HF](https://github.com/KK4ODA/aether-hf)
and AX.25 packet, with Winlink integration, APRS, multi-user chat, and multi-tier message
delivery.

> **Formerly VARA BBS.** The program was renamed when it gained first-class support for
> Aether HF, the open-source HF modem. VARA FM and VARA HF are supported exactly as before.
> Upgrading? See [Upgrading from VARA BBS](#upgrading-from-vara-bbs).

## Features

### Transports
- **VARA FM** and **VARA HF** over VARA's TCP host interface
- **Aether HF**, the open-source HF modem: sessions over its VARA-compatible host interface,
  plus its control API for status, faults, signal reports, heard stations, *Update required*
  warnings, and Probe / Beacon buttons on the dashboard
  ([guide](docs/AETHER_HF.md))
- **Soundmodem / Direwolf** AX.25 packet over AGWPE (KISS is planned)
- One transport per instance; run several instances side by side, each on its own ports
- Per-instance PTT for VARA HF (and Aether HF in host-keyed mode): OmniRig, FLrig, Hamlib,
  CM108, or serial RTS/DTR
- HF frequency scanning through OmniRig with time-of-day filtering
- `;TRANSPORT?` query: a terminal client can ask which transport it reached

### BBS
- Interactive BBS sessions with an SQLite message store
- Bulletin board with configurable areas and inter-BBS forwarding, over TCP or on the air
  (VARA FM, VARA HF or Aether HF)
- File storage and transfer with role-based access and virus scanning
- MHeard station log (Aether HF's heard stations are added to it)

### Winlink
- Winlink CMS integration via [Pat](https://getpat.io)
- On-demand CMS polling (sysop via GUI, RF users via the `WL` command)
- Per-user Winlink mail forwarding (`WLREG` with encrypted password exchange)
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
- Secrets encrypted at rest (BBS passwords, Winlink passwords, gateway credentials,
  Aether HF control tokens)

### Notifications
- Push notifications via ntfy (self-hosted, no internet required) — see
  [`tools/ntfy/README_NOTIFICATIONS.txt`](tools/ntfy/README_NOTIFICATIONS.txt)
- Pushover (iOS/Android mobile push)
- SMS via USB GSM modem (AT commands)
- Configurable event triggers (new registrations, urgent bulletins, priority messages)

### Infrastructure
- APRS-IS gateway with position beaconing
- Internet connectivity monitoring with automatic notifications
- PyQt6 GUI with dashboard, message viewer, settings, packet monitor, and log viewer
- TOML configuration with automatic migration and corrupt-config recovery
- Git-based self-update (Settings → Updates)

## Quick Start

1. Install Python 3.11 or later, then the dependencies: `pip install -r requirements.txt`
2. Install your modem: VARA FM/HF, [Aether HF](https://github.com/KK4ODA/aether-hf/releases),
   or Soundmodem/Direwolf
3. Install [Pat](https://getpat.io) if you want Winlink
4. Run `python main.py` (or double-click `start_bbs.bat` on Windows)
5. Set your station callsign, then add an instance per modem under **Settings → Instances**

The configuration lives at:

| OS | Path |
|---|---|
| Windows | `%APPDATA%\aether-bbs\aether_bbs.toml` |
| macOS | `~/Library/Application Support/aether-bbs/aether_bbs.toml` |
| Linux | `~/.config/aether-bbs/aether_bbs.toml` |

Messages, files and logs (`logs/aether_bbs.log`) are kept relative to the folder the BBS runs
from.

## Transport Configuration

Each instance has one transport. The GUI writes these for you; by hand they look like:

```toml
[[instances]]
name = "VHF"
transport = "VARA_FM"       # VARA_FM, VARA_HF, SOUNDMODEM, or AETHER_HF
enabled = true
cmd_port = 8300             # modem command port; data port is cmd_port + 1
data_port = 8301

[[instances]]
name = "HF-VARA"
transport = "VARA_HF"
cmd_port = 8310
data_port = 8311
bandwidth = 2300            # 500, 2300 or 2750
[instances.ptt]
method = "OMNIRIG"          # VOX, RTS, DTR, DTRRTS, HAMLIB, CM108, OMNIRIG, FLRIG

[[instances]]
name = "HF-Aether"
transport = "AETHER_HF"
cmd_port = 8320             # must match Aether's "Host programs" port (8300 by default)
data_port = 8321
auto_start = true           # launch Aether HF if it is not running
[instances.aether]
control_port = 8515         # Aether's control API
bandwidth = 0               # 0 = Aether's own setting; or 500 / 2300

[[instances]]
name = "Packet"
transport = "SOUNDMODEM"
[instances.soundmodem]
protocol = "AGWPE"
host = "127.0.0.1"
port = 8000
```

Ports must be unique across enabled instances.

## Bulletin forwarding

Neighbour BBSs are listed under **Settings → Forwarding**. Each one is called on its own
interval and the two exchange the bulletins the other has not seen (urgent ones first):

- **Via TCP**: over the network to the neighbour's forwarding server (host and port; enable
  this BBS's own server with *server_enabled* to be called).
- **Via RF**: called on the air through a VARA FM, VARA HF or Aether HF instance. When an RF
  neighbour calls this BBS, the session goes straight to the forwarding protocol instead of
  the user menu, so both ends must list each other. A failed call waits for the next
  interval rather than being retried every minute.

```toml
[[bbs.forwarding_neighbours]]
bbs_callsign = "W2BBB-10"
via = "RF"                  # TCP or RF
instance = "HF-Aether"      # the modem instance RF calls go through
interval_minutes = 240
areas = ["ALL", "WX"]       # empty = all areas
```

Neighbours are identified by callsign, as with other packet BBS forwarding: list only
stations you trust.

## Aether HF

Aether HF is installed and set up on its own: its Setup tab owns the rules the station runs
under, the radio, keying and sound card. Turn on **Host programs** (Setup step 5, port 8300)
so the BBS can attach. The BBS then sets `MYCALL` to the BBS callsign, answers calls, and shows
Aether's state, bandwidth, dial, SNR and the rules' verdict on the instance card, with a
warning for anything that would stop it working. Aether HF stations talk only to other Aether
stations on the air; an Aether BBS must never be listed as a VARA gateway.

Full setup, keying options, remote daemons and troubleshooting: [docs/AETHER_HF.md](docs/AETHER_HF.md).

## Upgrading from VARA BBS

Nothing needs to be done by hand:

- On first start, Aether BBS copies `vara-fm-bbs/vara_fm_bbs.toml` to
  `aether-bbs/aether_bbs.toml` in the same parent folder. The old file is left where it was.
- The beacon text and welcome message are renamed only if they were still at the old
  defaults. Your ntfy topic is kept, so phones stay subscribed.
- The main log is now `logs/aether_bbs.log`; older logs stay as `vara_fm_bbs.log`.
- An Aether HF instance from the prototype era (`[instances.aether] cmd_port = 8400`) has its
  ports moved to the instance's `cmd_port`/`data_port`. Point it at the port Aether's
  *Host programs* setting uses (8300 by default).
- Re-running `tools/ntfy/setup_ntfy.bat` replaces the `VARA-BBS-ntfy` task with
  `Aether-BBS-ntfy`.
- APRS-IS logins now identify the software as `AetherBBS <version>`.

## Tests

```bash
python -m pytest tests
```

`tests/test_aether.py` runs the BBS against a fake `aetherd` (host interface and control API),
so it needs no radio or Aether installation. `tools/vara_simulator.py` simulates an RF caller
for manual testing.

## Related Projects

- [Aether HF](https://github.com/KK4ODA/aether-hf) — open-source HF data modem with a
  VARA-compatible host interface
- [Pat](https://getpat.io) — Winlink client used for CMS integration

<!-- tota-support:start -->
## Support

Aether BBS is free and stays free. It is written by KK4ODA, who also runs
[Tiles on the Air](https://tilesontheair.com), a free program for portable
operators, and both are kept going by the people who use them. If Aether BBS is
useful to you, a contribution through the
[Tiles on the Air giving page](https://tilesontheair.com/Giving) helps keep this tool and Tiles
running. Nothing is ever locked behind it, and a bug report or an on-air
report helps just as much.
<!-- tota-support:end -->

## Contributors

- **KK4ODA** (Facundo) — Author
- **Claude** (Anthropic) — Co-developer

## License

Copyright (C) 2026 KK4ODA. Released under the GNU General Public License v3.0, see [LICENSE](LICENSE). The GPL is required because the program is built on PyQt6, which is licensed under GPL v3.
