# Aether BBS Manual

This manual is for the sysop who installs and runs Aether BBS, and (in
[chapter 9](#9-rf-user-guide)) for the stations that connect to it. It describes
version 1.3.x. Setting names, labels and defaults are the ones in the program; where
the [README](../README.md) or [docs/AETHER_HF.md](AETHER_HF.md) already cover a topic in
depth, this manual links to them.

## Contents

1. [Overview](#1-overview)
2. [Installing and updating](#2-installing-and-updating)
3. [First start: station and modem instances](#3-first-start-station-and-modem-instances)
4. [The main window](#4-the-main-window)
5. [Users and security](#5-users-and-security)
6. [Messages, bulletins and files](#6-messages-bulletins-and-files)
7. [Winlink via Pat](#7-winlink-via-pat)
8. [Internet email, APRS, chat, alerts and notifications](#8-internet-email-aprs-chat-alerts-and-notifications)
9. [RF user guide](#9-rf-user-guide)
10. [Testing without a radio](#10-testing-without-a-radio)
11. [Troubleshooting](#11-troubleshooting)
12. [Configuration file reference](#12-configuration-file-reference)

---

## 1. Overview

Aether BBS is a bulletin board system for amateur radio. Stations connect to it over the
air, log in, read and send personal messages, read and post bulletins, download and
upload files, chat, and pick up Winlink mail. The sysop runs it from a desktop window
(PyQt6) on Windows, macOS or Linux.

The BBS does not contain a modem. It talks to modem programs running on the same
computer (or on the network) over their TCP host interfaces:

| Transport | Modem program | How the BBS talks to it |
|---|---|---|
| `VARA_FM` | VARA FM | VARA TCP host interface (command port + data port) |
| `VARA_HF` | VARA HF | VARA TCP host interface; the BBS keys the radio (PTT) |
| `AETHER_HF` | [Aether HF](https://github.com/KK4ODA/aether-hf) | Aether's VARA-compatible host interface, plus its control API |
| `SOUNDMODEM` | Soundmodem or Direwolf | AGWPE (AX.25 connected mode). KISS is not supported. |

Each modem is one **instance** in the BBS. You can run several instances side by side
(for example a VHF VARA FM port and an HF Aether port), each on its own TCP ports. All
instances share one message store, one user database and one set of settings.

Other programs the BBS can work with:

- **Pat** ([getpat.io](https://getpat.io)) carries Winlink mail between the BBS and the
  Winlink CMS, over the internet (telnet) only. See [chapter 7](#7-winlink-via-pat).
- **OmniRig, FLrig, Hamlib `rigctld`** for PTT and (OmniRig) frequency scanning on HF.
- **ntfy, Pushover, a USB GSM modem** for alerts to the sysop's phone.
- **APRS-IS** for position/status beacons.

How the pieces fit:

```
  RF station ──radio── modem program ──TCP── Aether BBS ──HTTP/CLI── Pat ──telnet── Winlink CMS
                       (VARA FM/HF,                │
                        Aether HF,                 ├── APRS-IS (beacons)
                        Soundmodem)                ├── IMAP/POP3 mailbox (inbound email)
                                                   └── ntfy / Pushover / SMS (alerts)
```

---

## 2. Installing and updating

### 2.1 Installers

Download the file for your computer from the
[latest release](https://github.com/KK4ODA/aether-bbs/releases/latest):

| Computer | File | How |
|---|---|---|
| Windows 10/11 (64-bit) | `AetherBBS-<version>-windows-setup.exe` | Run it. It installs for you alone, with no administrator prompt, unless you choose all users. |
| Mac, Apple silicon | `AetherBBS-<version>-macos.dmg` | Drag **Aether BBS** into Applications. |
| Mac, Intel | `AetherBBS-<version>-macos-intel.dmg` | Same. |
| Linux x86-64 | `AetherBBS-<version>-linux-x86_64.tar.gz` | Unpack in a folder you own, run `AetherBBS/AetherBBS`. `AetherBBS/install-desktop-entry.sh` adds a menu entry. |

The installers are not code-signed. On Windows click **More info → Run anyway**; on a Mac
use **System Settings → Privacy & Security → Open Anyway**. Linux needs `libxcb-cursor0`
(Debian/Ubuntu) or `xcb-util-cursor` (Fedora/Arch).

Then install your modem program(s) and, for Winlink, Pat.

### 2.2 From source

Install Python 3.11 or later, then in the source folder:

```bash
pip install -r requirements.txt
python main.py
```

On Windows, `start_bbs.bat` does the same. `pywin32` is installed on Windows only; it is
needed for OmniRig. `pyttsx3` is optional (spoken alerts fall back to PowerShell, `say` or
`espeak`).

### 2.3 Where things are kept

The settings file is `aether_bbs.toml` in:

| OS | Folder |
|---|---|
| Windows | `%APPDATA%\aether-bbs\` |
| macOS | `~/Library/Application Support/aether-bbs/` |
| Linux | `~/.config/aether-bbs/` |

The station's data:

| What | Location (relative to the data folder) |
|---|---|
| Messages, bulletins, users, audit log | `messages/messages.db` |
| File library index | `messages/files.db` |
| Library files | `files/<AREA>/` |
| Logs | `logs/aether_bbs.log`, plus `packet_frames.log`, `vara_cmd.log`, `chat.log` when enabled |
| Startup crash report | `crash.log` |

The data folder is the settings folder above for an installed copy, and the source folder
when you run from source. Uninstalling leaves the data in place.

**Back up `aether_bbs.toml`.** Its `[security] machine_secret` is the key for every stored
password and secret (BBS passwords, Winlink passwords, the mailbox password, Aether
control tokens). Without it they cannot be decrypted.

If the settings file is damaged, the BBS saves it as `aether_bbs.toml.bad.<date_time>`,
keeps every section that is still valid, resets only the broken ones, and always keeps the
machine secret. The log says which sections were reset.

Upgrading from VARA BBS (the old name) needs no action: see
[Upgrading from VARA BBS](../README.md#upgrading-from-vara-bbs) in the README.

### 2.4 Updates

With **Check for updates on startup** on (the default), the BBS asks GitHub at start-up
whether a newer release exists. If one does, a dialog shows what's new with
**Update now**, **Remind me later** and **Skip this version**. A yellow **UPDATE v…**
label also appears in the status bar; click it to reopen the dialog.

**Settings → About & Updates**:

| Setting | Default | Meaning |
|---|---|---|
| Check for Updates (button) | | Check now |
| Check for updates on startup | on | |
| Auto-check interval | 0 hours | Periodic check while running; 0 = startup only (max 168) |
| Offer | Releases only (recommended) | "Every change on GitHub (git copies only)" follows the `master` branch, for a git clone only |
| Ask again about v… | | Appears when you skipped a version |

What **Update now** does depends on how you installed (installer, source ZIP, git clone);
the details are in [Updating](../README.md#updating) in the README.

---

## 3. First start: station and modem instances

Settings are in the **Settings** tab. The left sidebar lists the pages; nothing takes
effect until you press **💾 Save Settings** at the bottom. **↩ Revert** throws away
unsaved edits. Most settings apply on Save without a restart; the Save dialog tells you
when one needs a restart (log level, log folder).

### 3.1 Station

**Settings → Station**:

| Field | Default | Notes |
|---|---|---|
| BBS Callsign | `N0CALL-10` | The callsign the BBS answers on (sent to the modem as `MYCALL`). Changing it restarts the modems when you save. |
| Sysop Callsign | `N0CALL` | Your own call. Its base callsign (any SSID) always has the SYSOP role. |
| Grid Square | empty | Shown in the connect banner, `I` command and APRS status |
| Latitude / Longitude | 0 | For the APRS position beacon. Leave both at 0 to send a status beacon without position. |
| Beacon Interval | 10 min | APRS-IS beacon interval; 0 = off |
| Beacon Text | `Aether BBS` | Text in the APRS beacon |

**Tactical Addresses** (same page) is a table of *Callsign (max 8)*, *Alert Phrase* and
*Advertise*. A message addressed to one of these calls opens a priority alert on the
sysop's screen, speaks the phrase, and sends push notifications (see
[8.4](#84-priority-alerts)). Advertised ones are listed to RF users after they connect.
RF users can also adopt a tactical address for their session with `TACTICAL`
([9.5](#95-tactical-addresses)).

> A new settings file contains one tactical address, `BRECKEN`. Remove it or replace it
> with your own (**✕ Remove**).

### 3.2 BBS

**Settings → BBS**:

| Field | Default | Meaning |
|---|---|---|
| Welcome Message | `Welcome to the Aether BBS. Type H for help.` | Sent after connect |
| Session Timeout | 600 s (30–3600) | Idle time at the prompt before the BBS says `Session timeout. 73 de BBS.` and disconnects. EMCOMM mode caps it at 120 s. |
| Message Max Age | 30 days | Expiry date given to every new message, and to bulletins in areas without their own Max Age. Expired items are removed only when you press **🗑 Expire Old** in the Messages tab. |
| Max Sessions | 1 (1–16) | Concurrent sessions across all instances. Others get `*** BBS BUSY ***`. Read at start-up: restart after changing it. Values above 1 share a message store that is not transaction-isolated. |
| Session Watchdog | 1800 s | Any session older than this is force-disconnected |
| Connect Banner | on | ASCII banner with station, sysop, grid and frequency label |

### 3.3 Adding a modem instance

A new settings file has one instance, **Primary**: VARA FM on ports 8300/8301, enabled,
Auto Start off. Edit it, or add your own.

**Settings → Instances**: the list on the left shows each instance as
`Name · frequency [FM|HF|AETHER|AGWPE]`. **+ Add** creates `Instance N` (VARA FM, command
port 8300 + 10 × the number of existing instances); **− Remove** deletes the selected one.
Select an instance to edit it on the right.

**Identity** group (all transports):

| Field | Notes |
|---|---|
| Name | Shown on the dashboard card and in logs. Also the instance name RF forwarding neighbours refer to. |
| Transport | `VARA_FM`, `VARA_HF`, `SOUNDMODEM` or `AETHER_HF` |
| Frequency label | Free text such as `145.050` or `14.105`, shown in the connect banner, on the card and in the APRS status. Greyed out when a scan table sets the frequency. |
| Enabled | Disabled instances are not started |
| Auto Start | Launch the modem program (Exe path) when the BBS starts, unless it is already running. Not used by Soundmodem instances, which have their own *Auto-launch* box. |

**VARA Modem** group (VARA FM, VARA HF; titled *Aether HF host interface* for Aether HF):

| Field | Notes |
|---|---|
| Exe path | Modem executable, for Auto Start. **Browse…** to pick it. |
| CMD port | The modem's command port (default 8300) |
| Data port | Always CMD port + 1 (read-only) |
| Default path | AX.25 digipeater path; blank = direct |
| Bandwidth (Hz) | 500, 2300 or 2750. Sent to VARA HF as `BW<n>` at start-up. Ignored for VARA FM. Hidden for Aether HF. |

There is no Host field in this form: the modem host is `127.0.0.1` unless you set `host` for
the instance in `aether_bbs.toml` ([chapter 12](#12-configuration-file-reference)).

Port numbers must be unique across all instances; the form shows a red warning and Save
refuses while two instances share a port. This includes the hidden CMD/Data ports of a
Soundmodem instance.

Saving applies instance changes at once: removed or disabled instances stop, changed ones
restart (on a change of transport, ports, exe path, host, Soundmodem or Aether settings,
rig control, PTT on HF, VARA HF bandwidth, or the BBS callsign), and new ones start.

### 3.4 What happens when an instance starts

For VARA and Aether HF instances the BBS:

1. Starts the modem program if Auto Start is on, an Exe path is set, and the modem is not
   already running (VARA: nothing listening on the CMD port; Aether HF: its control API
   does not answer). For VARA it then waits 3 s and logs an error if the port is
   still closed.
2. Opens the command and data ports, trying 5 times with waits of 1, 2, 4 and 8 s
   (about 15 s in all). If all five fail, the instance stays disconnected: start the modem,
   then press **Start** (or **Restart**) on the instance's dashboard card.
3. Sends `MYCALL <BBS callsign>`, `COMPRESSION OFF`, `BW<n>` (VARA HF; Aether HF only if
   asked), `VERSION` (Aether HF), and `LISTEN ON`. Any `WRONG` reply is logged as
   *VARA REJECTED init command* and the init is reported as failed.
4. Listens for calls. If the TCP link drops later, the BBS reconnects on its own (first try
   after 5 s, backing off to once a minute).

Only one program can use a VARA or Aether host port at a time. Close Winlink Express,
Pat's own VARA connection, VarAC and so on before the BBS attaches.

### 3.5 VARA FM

- Set VARA FM's own TCP ports to match CMD/Data port (8300/8301 by default).
- VARA FM keys the radio itself: leave PTT alone (the PTT group is hidden for FM).
- Bandwidth is not sent to VARA FM.

### 3.6 VARA HF and PTT

VARA HF does not key the radio: the BBS does, when VARA reports `PTT ON`/`PTT OFF`. The
**PTT Control (HF only)** group appears for HF instances:

| Method | What it uses | Set up with **⚙ Setup…** |
|---|---|---|
| VOX | Nothing (radio VOX) | — |
| RTS, DTR, DTRRTS | Serial port control lines (DigiRig and similar) | Serial port (`COM5`, `/dev/ttyUSB0`) |
| HAMLIB | `rigctld` over TCP | rigctld host (127.0.0.1), port (4532) |
| CM108 | CM108/RA-Board USB GPIO | HID device (blank = first C-Media device) |
| OMNIRIG | OmniRig COM server (Windows, needs pywin32) | OmniRig rig: Rig 1 or Rig 2 |
| FLRIG | FLrig XML-RPC | FLrig host (127.0.0.1), port (12345) |

Every method except VOX has a **Watchdog timeout** (default 90 s, 10–600 s) that releases
PTT if the modem stops talking, so the transmitter cannot stick on. PTT is also released if
the modem link drops. **🔴 Test PTT (2 s)** keys the radio for two seconds with the
selected method; it does nothing for VOX, VARA FM or Soundmodem instances.

### 3.7 Rig control and frequency scanning (HF)

**Rig Control (HF only) → 📡 Scan Channels…** opens a dialog:

| Field | Values |
|---|---|
| Rig control | `NONE` (manual tuning) or `OMNIRIG` |
| OmniRig rig | Rig 1 / Rig 2 |
| Mode | USB, LSB, DATA-USB, DATA-LSB, FM, AM, CW (default USB) |
| Channel table | Dial Freq (kHz, 1800–30000), Dwell (s, 1–120), Hours (UTC, `HH-HH`, e.g. `14-04`, `00-24`) |

With OMNIRIG and at least one channel, the BBS tunes through the channels that are active
at the current UTC hour, staying on each for its dwell time. Scanning pauses while a
session is connected and resumes after it. The status bar shows the scan state.

Only one program can hold a radio's CAT port. Do not give the BBS OmniRig control of a
radio that another program (Aether HF, FLrig) already controls on the same port.

### 3.8 Aether HF

Aether HF is set up in its own program first (rules, radio, keying, sound card, and
**Host programs** on, port 8300). The BBS-side fields are:

| Field | Default | Notes |
|---|---|---|
| Bandwidth (Hz) | Aether's own setting | Or 500 / 2300 to ask for one while attached |
| Control API port | 8515 | Aether's `[control]` port |
| Control token | empty | Only for an Aether HF on another computer with a non-loopback control bind. Stored encrypted. |
| Show Aether HF status on the dashboard | on | Polls the control API for status, faults and heard stations |
| APRS beacon on the air, every … min | off, 60 min (10–1440) | APRS position as an Aether datagram |
| aetherd config | empty | `--config` for a standalone `aetherd` started by Auto Start |
| **Find installed** / **Test connection** | | Locate the program; ask its control API for status and problems |

When you choose `AETHER_HF` and Exe path is empty, the BBS looks for an installed Aether HF
and fills it in. The PTT group is used only when Aether's keying is set to *host program*;
otherwise leave the method at VOX. Everything else (keying, Probe and Beacon, the
stations-heard table, Update required, remote daemons, troubleshooting) is in
[docs/AETHER_HF.md](AETHER_HF.md).

VARA HF and Aether HF both default to port 8300. To run both, give one of them a different
port pair, in the modem and in the BBS.

### 3.9 Soundmodem / Direwolf (AX.25 packet)

Choose `SOUNDMODEM`. The **Soundmodem (AGWPE)** group replaces the VARA group:

| Field | Default | Notes |
|---|---|---|
| Host | 127.0.0.1 | Where Soundmodem/Direwolf runs |
| AGWPE Port | 8000 | Soundmodem and Direwolf both default to 8000 |
| Protocol | AGWPE | The only choice; KISS is not supported |
| Auto-launch Soundmodem | off | Start the program at Exe Path with the instance |
| Exe Path | empty | Soundmodem / Direwolf executable |

The BBS registers the BBS callsign with the AGWPE server (unless `soundmodem.my_callsign`
is set in the file) and accepts AX.25 connected-mode sessions. If the AGWPE link drops, it
reconnects on its own. The soundmodem keys the radio; no PTT settings apply.

---

## 4. The main window

The window title shows the BBS callsign and the number of instances, or the active
sessions. There are four top-level tabs.

### 4.1 Dashboard

**Left: Modem Instances.** One card per enabled instance, titled with its name:

- Header: frequency label and a transport badge (`FM`, `HF`, `AETHER`, `AGWPE`).
- Four lights:

  | Light | Meaning |
  |---|---|
  | Process | Green: a modem program the BBS started is running. Yellow: starting. Red: it crashed. Grey: stopped, or the modem was started outside the BBS. |
  | TCP | Green: the BBS is connected to the modem's host interface. Grey: not connected. |
  | PTT | Red while the transmitter is keyed |
  | BUSY | Orange while the modem reports the channel busy |

- `Session: <call>` or `No active session`; `TX: … B  RX: … B  BUF: n`.
- `Link: SNR … · level … · bps` when the modem reports `SN` / `BITRATE`.
- For Aether HF: the status line, warnings in amber, and **Probe…** / **Beacon** buttons
  ([AETHER_HF.md §3](AETHER_HF.md#3-what-the-bbs-does-with-aether-hf)).
- Buttons: **Start**, **Stop**, **Restart**, and **ABORT** (sends `ABORT` to the modem: the
  current session or call ends at once, without an orderly disconnect; prefer Stop).

Below the cards: **Aether HF — Stations Heard** (when an Aether instance has status on) and
**Recently Heard Stations** (Callsign, Instance, Last Heard, Count) with **🗑 Clear**.

Under the cards is the **Winlink (Pat)** panel: Status (`Disabled`, `Idle`, `Connecting…`,
`OK`, `Error`), Last poll, Queue (messages waiting for Winlink), sent/received counts of
the last poll, any error, and **Poll Now** ([chapter 7](#7-winlink-via-pat)).

**Right: BBS Status.**

- **Summary**: uptime, message count, sessions since start.
- **Active Sessions** (Instance, Callsign, Type, Duration). Select a row and press
  **💬 Start Chat** to open a chat with that station ([8.3](#83-chat)).
- **APRS-IS Beacon** (shown when APRS is enabled): status, last beacon time,
  **Send Beacon Now**.
- **Send Private Message**: To, Subject, body, **Send via Winlink** checkbox,
  **📨 Send Message**. Unchecked, the message waits at the BBS for the station to collect
  it over RF; internet addresses always go via Winlink.
- Red/orange notices for denied uploads and new registration requests.
- **User Management** ([5.4](#54-user-management)).

### 4.2 Messages

Three sub-tabs:

- **Messages**: all messages with Area and Show filters (All messages, Unread only,
  Replied, CMS Queued, CMS Sent, CMS Failed), **🔄 Refresh**, **🗑 Expire Old**,
  **📖 Open Message**, **🗑 Delete**. The *CMS* column shows the Winlink status. Opening a
  message lets you reply (with optional Winlink delivery) or mark it unread.
- **Bulletins**: **📋 Browse** (by area; open, delete), **📢 Post** (From, Group, Subject,
  Body, **🚨 Urgent**, **📢 Post Bulletin**), **🔄 Forwarding** (the configured neighbours,
  read-only, with **⚙ Edit in Settings**, and the last 100 forwarding log entries).
- **Files**: the file library ([6.4](#64-file-library)).

### 4.3 Settings

The settings pages, in sidebar order: Station, Instances, BBS, Bulletins, Forwarding,
File Upload, Winlink (Pat), APRS, Inbound Email, Security, Alerts, Notifications, Logging,
About & Updates.

### 4.4 Diagnostics

- **Activity Monitor**: live activity per instance, filterable (TX/RX data, Connections,
  Busy/Clear, PTT, System, Messages), a session log, and *MHeard — Stations That Have
  Connected*.
- **Log**: the program log with Level, Filter, Exclude, Mute, Auto-scroll and Clear.

### 4.5 Status bar

Left: the latest status message (for example `HF: modem CRASHED`). Right: one `● name`
indicator per instance (green listening or connected, with the caller's call; yellow
starting/stopping; red crashed; grey stopped), `NET: OK` / `NET: DOWN` (a TCP check to
8.8.8.8:53 every 30 s), the update label, and the scan indicator.

---

## 5. Users and security

### 5.1 Roles

Every callsign has one role, stored per **base callsign** (SSID removed: KK4ODA-5 and
KK4ODA-7 are the same user).

| Role | Can do |
|---|---|
| SYSOP | Everything, including `KA`, deleting anyone's messages, bulletins and files. The configured Sysop Callsign is always SYSOP, whatever the database says. |
| TRUSTED | Everything an APPROVED user can, plus post bulletins (`SB`, `SUB`), kill bulletins (`KB`), upload (`FP`) and delete files (`FD`), subject to the mode (below) |
| APPROVED | Read and send personal mail, read bulletins and files, chat, Winlink commands |
| PENDING | Has registered; may only disconnect |
| UNKNOWN | Not in the database; may only `REGISTER` or disconnect (when registration is required) |
| BLOCKED | Told `Access denied. 73.` and disconnected |

`KB` and `FD` still only let a non-sysop remove their own bulletins and files.

### 5.2 Operational modes

**Settings → Security → Operational Mode**:

| | Restricted (default) | Open | EMCOMM |
|---|---|---|---|
| Unknown callsigns | Must `REGISTER` and be approved, if *Require registration* is on; auto-approved if it is off | Auto-approved as APPROVED on first connect | Must register (checkbox ignored) |
| Password required | Per *Require password* | Per *Require password* | Always |
| Minimum role to post bulletins (`SB`/`SUB`) | TRUSTED | APPROVED | SYSOP |
| Minimum role to upload files (`FP`) | TRUSTED | APPROVED | SYSOP |
| Session timeout | Setting | Setting | Setting, at most 120 s |
| Rate limits (5.6) | × 1 | × 2 | × 0.25 |
| Auto-block after failures | Setting (default 20) | 50 | 5 |

The mode changes on Save; no restart is needed. `I` shows the current mode to RF users.
File uploads also pass the File Upload policy ([6.4](#64-file-library)).

### 5.3 Registration

In Restricted or EMCOMM mode an unknown station sees *Access Restricted* and may type
`REGISTER` (or `REG`) or `BYE`. On `REGISTER` the BBS asks for a community trust
reference ("a groups.io or Facebook group where you have been active, or NONE"), records
the base callsign as PENDING, says the password will be sent via Winlink to
`<CALL>@winlink.org`, and disconnects. Unknown and pending sessions time out after 120 s.

The sysop then sees an orange notice on the dashboard, the user appears as PENDING in User
Management, and a push notification goes out if enabled ([8.5](#85-push-and-sms-notifications)).
Registration is limited to 3 attempts per day per callsign (scaled by the mode).

### 5.4 User management

The **User Management** box on the dashboard lists users (Callsign, Role, Registered,
Last Auth, Failures, Notes) with a role filter and **🔄 Refresh**. Select a row, then:

| Button | Action |
|---|---|
| **＋ Add User** | Add a callsign (no SSID) with a role, notes and a password. A 16-character password is generated (letters and digits without look-alikes); **🎲 Regenerate**, edit it, or clear it for none. |
| **🔑 Password** | Generate a new password for the selected user (asks first if one exists). |
| **👁 View PW** | Show the user's stored password, for a user who forgot it. |
| **✓ Approve** | Set APPROVED |
| **⬆ Trusted** | Set TRUSTED |
| **⬆ Sysop** | Set SYSOP |
| **⛔ Block** | Set BLOCKED |
| **🔓 Reset Auth** | Clear the failure counter |
| **🗑 Delete** | Remove the user (not the sysop). They are UNKNOWN on next connect. |

After **Add User** (with a password) or **🔑 Password**, a dialog shows a ready-made
message to `<CALL>@winlink.org` with the credentials, and **📋 Copy Message** /
**🔑 Copy Password Only**. The BBS does not send it for you: send it yourself by Winlink
(for example from **Send Private Message** with *Send via Winlink* checked), phone or in
person. Never over RF in the clear.

Approving a user does not give them a password. With *Require password* on (the default)
an approved user without one is told the password has not been provisioned and is
disconnected, so approve **and** provision.

### 5.5 Passwords and authentication

**Settings → Security → Authentication**:

| Setting | Default | Meaning |
|---|---|---|
| Require registration for new callsigns | on | Restricted mode only (see 5.2) |
| Require password for approved users | on | Users without a password are disconnected. Always on in EMCOMM. |
| Allow plaintext password fallback | on | Accept a typed password. Anyone listening can read it. Off = HMAC only. |

Passwords belong to the base callsign. Users can change theirs with `SETPASS` (at least 4
characters) once logged in.

When a user with a password connects, the BBS sends:

```
;AUTH HMAC-SHA256 <32 hex characters>
Enter password (or HMAC client will respond automatically):
>
```

The 32 hex characters are a random 16-byte challenge, valid for 2 minutes and usable once.
The client answers with `;AUTH <response>` (or just `<response>`), where the response is
HMAC-SHA256 keyed with the password (UTF-8) over the 16 challenge bytes, as 64 lowercase
hex characters. The password itself never goes on the air. Aether Term does this
automatically when it has a saved password. If plaintext fallback is on, the user can
instead type the password. The BBS replies `;AUTH OK` or `;AUTH FAIL` (and disconnects on
failure). No answer within 120 s counts as a failure.

**Why the sysop is challenged too.** Callsigns are not verified on the air: anyone can
transmit as your call. The sysop callsign gets no exception, so a password is the only
thing between a stranger and SYSOP access. Set one for your sysop callsign. If no password
is set and *Require password* is off, the sysop (like any user) gets in without one.

Passwords are stored as a salted hash plus an encrypted copy (needed to check HMAC answers
and for View PW), keyed by the machine secret.

### 5.6 Lockouts, auto-block and rate limits

Each failed login adds to the user's failure counter; a successful login resets it.

- **Lockout**: at *Auth failure lockout threshold* (default 5) failures, connections are
  refused with *Too many failed authentication attempts* until *Lockout duration*
  (default 60 min) has passed since the last failure.
- **Auto-block**: at *Auto-block after failures* (default 20; 50 in Open, 5 in EMCOMM) the
  callsign becomes BLOCKED. This is checked before the lockout. Unblock it by setting
  another role (Approve) and use **🔓 Reset Auth**.

The rate limits are kept in memory per base callsign and reset when the BBS restarts:

| Event | Limit (Restricted) | Message to the user |
|---|---|---|
| Connections | 10 per hour | `Too many connections. Try again later.` |
| Failed logins | 5 per hour | `Too many failed authentication attempts.` |
| Messages sent | 20 per hour | `You have sent too many messages. Try again later.` |
| Bulletins posted | 5 per hour | `You have posted too many bulletins. ...` |
| File uploads | 10 per hour | `You have uploaded too many files. ...` |
| Registrations | 3 per day | `Too many registration attempts. ...` |
| Commands | 120 per minute | `Rate limit exceeded. Please slow down.` |

Open mode doubles these; EMCOMM multiplies them by 0.25 (never below 1). Connections,
lockouts, role changes, denied commands and logins are written to the security audit
table in `messages.db`.

---

## 6. Messages, bulletins and files

### 6.1 Personal messages

A personal message is addressed to a callsign. `S <call>` from an RF user, or **Send
Private Message** from the sysop, stores it at the BBS for the addressee to collect over
RF. A message to an internet address, or one sent with `SW` / *Send via Winlink*, is
queued for Winlink instead ([chapter 7](#7-winlink-via-pat)).

Mail is matched to the callsign exactly as the station connects, SSID included. Mail
received from Winlink is filed under the base callsign (no SSID).

### 6.2 Bulletins and areas

A bulletin is addressed to a group (ALL, WX, LOCAL, …) and readable by every logged-in
user. **Settings → Bulletins** defines the areas (Area Name, Description, Max Age in days,
Enabled) with **+ Add Area** / **− Remove Selected**. The defaults are ALL, WX, LOCAL and
DX, 14 days each. A user posting to a group that is not a configured, enabled area is
warned and asked to confirm. A bulletin expires after its area's Max Age (or Message Max
Age if the area is not configured); expired items are removed with **🗑 Expire Old**.

`SUB` posts an urgent bulletin (marked `!` in `LB`). Bulletins in the *Urgent areas* list
(**Settings → Notifications**, default EMCOMM, WX, ARES) count as urgent too. Users are told
about new and urgent bulletins when they connect.

### 6.3 Bulletin forwarding

Neighbour BBSs are listed under **Settings → Forwarding**. Each one is called every
*Interval* minutes and the two exchange the bulletins the other has not seen, urgent ones
first.

**Inbound Forwarding Server**: *Accept inbound forwarding connections* (off), *Listen
Port* (7300), *Bind Address* (0.0.0.0, all interfaces; 127.0.0.1 for local testing).

**Outbound Forwarding Neighbours** table: BBS Callsign, Via (TCP or RF), Instance (for
RF; VARA FM, VARA HF or Aether HF), Host, Port (7300), Interval (60 min, minimum 5),
Areas (comma-separated, blank = all), Enabled.

Only enabled neighbours are accepted when they call in, by TCP or RF. When an RF neighbour
calls this BBS, the session goes straight to the forwarding protocol instead of the user
menu, so both ends must list each other. Neighbours are identified by callsign alone:
list only stations you trust. A neighbour entered without an SSID matches any SSID.

More detail: [Bulletin forwarding](../README.md#bulletin-forwarding) in the README. To
try the protocol without a second BBS, run `python tools/test_forwarding.py --port 7300`
against your own forwarding server.

### 6.4 File library

The **Files** sub-tab shows the file areas and their files. The default areas are
GENERAL, SOFTWARE, DOCS, WEATHER and MAPS. **➕ New Area** adds one; double-click an area's
*RF Upload* column to allow or stop RF uploads to it. **⬆ Upload File** adds a file as the
sysop; **📂 Open File**, **⬇ Save to Disk** and **🗑 Delete File** act on the selection. Sysop
uploads skip the upload policy below.

**Settings → File Upload** controls RF uploads (`FP`):

| Setting | Default | Notes |
|---|---|---|
| Allow remote (RF) uploads | on | Master switch |
| Max file size | 5 MB (1–50) | |
| Per-callsign quota | 20 files | Files one callsign may have stored |
| Upload Authorization Mode | Whitelist | **Whitelist**: only callsigns in the list (a base call matches all SSIDs). **Blocklist**: everyone except listed. **Open**: anyone. **Role-based**: TRUSTED and SYSOP only. |
| Upload Whitelist | empty | With an empty whitelist in Whitelist mode, every RF upload is refused, the sysop's included |
| Allowed File Extensions | `.txt .pdf .doc .docx .jpg .jpeg .png .gif .zip .gz .csv .xlsx .mp3 .wav .ics .kml .gpx .rtf` | Empty list = all |
| Blocked Callsigns | empty | Denied in Blocklist mode; overrides the whitelist in Whitelist mode |
| Virus scanning | off | Windows only, with Windows Defender (`MpCmdRun.exe`). Scan timeout 30 s; on timeout or if Defender is missing the file is accepted and a warning logged. **🔍 Test Windows Defender** scans the EICAR test string. |
| Quarantine infected files | on | Into *Quarantine dir* (`quarantine`) instead of discarding |

An RF upload must pass, in order: the command's minimum role for the mode (5.2), the
master switch, the authorization mode, the extension list, a content check, size, quota,
the area's RF-upload switch, and the virus scan.

---

## 7. Winlink via Pat

The BBS reaches the Winlink CMS through [Pat](https://getpat.io), over the internet only
(`pat connect telnet`). It never sends Winlink traffic over RF: the modem stays free for
BBS sessions. Winlink clients (Winlink Express and similar, B2F) cannot exchange mail with
the BBS itself; one that reaches the command prompt is told `*** This BBS does not offer Winlink (B2F)
message exchange. Use a Winlink gateway.` and disconnected.

### 7.1 Setup

Install Pat, then **Settings → Winlink (Pat)**:

| Field | Default | Notes |
|---|---|---|
| Enable Winlink Integration (via Pat) | off | |
| Pat Executable | empty | Full path to Pat. The status line under it shows `Pat <version>`, `Pat found at: …` or `Pat not found`. Pat does not start until this path is set. |
| Pat HTTP Address | `localhost:8080` | Pat's web/API address |
| Auto-launch Pat when BBS starts | on | Starts `pat http --addr …` unless Pat already answers there |
| **Test Pat Connection** | | Asks Pat's API for its inbox |
| Callsign | empty | The gateway (station) Winlink account, e.g. `KK4ODA-5` |
| Password | empty | That account's Winlink password, stored encrypted |
| Grid Square | empty | Pat's locator |
| Forward For | empty | Extra callsigns to fetch mail for (Pat's auxiliary addresses) |
| CMS Timeout | 120 s (30–600) | Limit for one `pat connect` |

With *Auto-launch Pat* on, the BBS writes into Pat's `config.json` at start-up: `mycall`,
`secure_login_password`, `locator`, `http_addr`, `auxiliary_addresses` (Forward For), a
`telnet` connect alias if missing, and an empty `schedule` (the BBS decides when to sync).
It looks for that file at `%LOCALAPPDATA%\pat\config.json`, `~/.wl2k/config.json`, then
next to the Pat executable. If you also use this Pat for your own mail, keep that in mind,
or turn auto-launch off and run Pat yourself.

Pat is started, and the inbox check begins, when the BBS starts with Winlink enabled.
After enabling Winlink for the first time, restart Aether BBS.

### 7.2 How mail flows

**Outbound.** A message is queued for Winlink when an RF user sends it with `SW <call>` or
to an address containing `@`, or the sysop ticks *Send via Winlink*. It then shows as
*CMS Queued* and counts in the panel's *Queue*. Queued messages go out at the next CMS sync:
the BBS hands up to 25 of them to Pat (`pat compose`), then runs `pat connect telnet`.
A message Pat accepted is marked *Sent*; one that fails five times is marked *Failed*.
Messages sent while there is no internet stay queued in Pat until a later sync succeeds.

**When a sync runs.** Only on demand:

- the sysop presses **Poll Now** in the Winlink (Pat) panel (syncs as the gateway callsign
  and the Forward For calls), or
- an RF user types `WL` (syncs for that user, below).

There is no scheduled sync. This is deliberate: it avoids pulling mail that users want to
collect through another Winlink gateway. A sync is tried up to 3 times (waiting about
5, then 10 s, between tries). With no internet the sync is skipped and logged.

**Inbound.** Every 15 seconds the BBS reads Pat's local inbox through its HTTP API (this
never contacts the CMS). New messages are stored for the base callsign they are addressed
to, and tagged `[WL]` in the user's `L` list. A user connected under exactly that
callsign is told at the next prompt.

### 7.3 Per-user Winlink: WLREG, WL, WLOFF

A user who wants their own Winlink mail through the BBS:

1. Must have a BBS password (`SETPASS`, or one provisioned by the sysop).
2. Types `WLREG`. The BBS sends `;WLREG NONCE <32 hex>`; the terminal program asks for the
   user's Winlink password and sends it back encrypted with a key derived from the BBS
   password and the nonce (`;WLREG <hex>`). Typing anything else cancels. The Winlink
   password is stored encrypted, for the callsign exactly as connected (SSID included).
3. Types `WL` whenever they want mail. The BBS temporarily switches Pat's identity to the
   user's callsign and Winlink password, adds their active tactical address if they set
   one, runs a sync (30–120 s), restores Pat's configuration, and reports
   `Winlink sync complete: n sent, n new messages received.` Only one sync runs at a time;
   a second gets `CMS sync already in progress. Try again shortly.`
4. `WLOFF` turns it off again.

### 7.4 What is not supported

- Winlink B2F clients connecting to the BBS (see above).
- CMS access over RF (Pat is driven over telnet only).
- Automatic, scheduled CMS polling.

---

## 8. Internet email, APRS, chat, alerts and notifications

### 8.1 Inbound email

**Settings → Inbound Email** lets internet senders reach RF users without Winlink. The BBS
polls one mailbox and stores each email for the callsign in the recipient address, for RF
pickup. Replies from RF users to internet addresses go out through Winlink.

| Field | Default | Notes |
|---|---|---|
| Enable Inbound Email Fallback | off | |
| Protocol / SSL/TLS | IMAP, on | IMAP or POP3 |
| Server | empty, port 993 | |
| Username / Password | empty | Password stored encrypted |
| Folder | INBOX | IMAP only |
| **Test Connection**, **Poll Now** | | |
| BBS Domain | empty | Only addresses at this domain are mapped. `kk4oda@your.domain` → KK4ODA, `kk4oda-5@…` → KK4ODA-5 |
| Poll every | 5 min | |
| Max size | 50 KB | Longer bodies are truncated |
| Delete emails from server after processing | off | Off: marked read (IMAP) or left (POP3) |
| Allowed senders | empty (all) | One address or domain per line |

The local part of the address must look like a callsign, and the callsign must have a
role other than UNKNOWN or BLOCKED; other mail is skipped. The same email fetched twice is
stored once.

### 8.2 APRS-IS

**Settings → APRS**: *Enable APRS-IS Gateway* (off), *IS Server* (`rotate.aprs2.net`),
*IS Port* (14580), *Passcode* (-1 = receive only; a valid passcode is needed to beacon),
*IS Filter*. The gateway connects and disconnects on Save.

Every *Beacon Interval* minutes (Station page) the BBS beacons the BBS callsign with the
BBS symbol (`/B`): a position beacon when latitude/longitude are set, otherwise a status
beacon with grid and beacon text, plus a status packet with grid and frequency labels. The
beacon is not messaging-capable: the BBS does not answer APRS messages. **Send Beacon Now**
on the dashboard sends one at once.

APRS on the air through Aether HF is a per-instance setting ([3.8](#38-aether-hf)).

### 8.3 Chat

**Sysop chat.** An RF user types `CH`; a chat window opens on the sysop's screen. The sysop
can also start one: select the session in **Active Sessions** and press **💬 Start Chat**.
Type in the window and press **Send**; **End Chat** (or `/EXIT`) returns the user to the
BBS. The user leaves with `/EXIT`. If the user writes and the sysop does not answer within
60 s, the chat closes with `Sysop did not respond.` The window stays open for review.
With *Log sysop ↔ remote chat conversations* on, chats are appended to `logs/chat.log`.

**Chat rooms.** `CHAT [room]` (or `CONVERSE`) joins a multi-user room, `LOCAL` by default.
In a room, `/WHO` lists members, `/ROOM <name>` switches room, `/EXIT` or `/QUIT` leaves;
any other line goes to everyone in the room. Up to 10 rooms exist at once. Several RF users
can only be in a room together if *Max Sessions* allows more than one session.

### 8.4 Priority alerts

A message to a tactical address ([3.1](#31-station)) opens a **⚠ PRIORITY MESSAGE** window
with **✅ ACKNOWLEDGE** and **↩ REPLY**, and repeats the spoken phrase until acknowledged.
Each message alerts once per run.

**Settings → Alerts**:

| Setting | Default |
|---|---|
| Enable audio alerts (TTS announcements and session tones) | on |
| Play tone on RF session connect / disconnect | on |
| Repeat interval | 10 s |
| Volume | 0.85 |
| High-contrast alert (red/white) | off |
| Large font in alert windows (14pt+) | off |

**🔊 Test Alert Sound** speaks a test phrase.

### 8.5 Push and SMS notifications

**Settings → Notifications** sends the same events to one or more backends:

- a message to a tactical address,
- *New user registration awaiting sysop approval* (on by default),
- *Urgent bulletin posted* (on by default): a `SUB` bulletin or one in an *Urgent areas*
  area (default `EMCOMM, WX, ARES`).

| Backend | Settings (defaults) |
|---|---|
| ntfy | Enable (off), Server URL (`http://127.0.0.1:8080`), Topic (`aether-bbs`), Priority (`urgent`), *Also relay to cloud (ntfy.sh)* (off), Cloud URL (`https://ntfy.sh`) |
| Pushover | Enable (off), API Token, User Key, Priority (1, range -2…2), Sound (`siren`) |
| SMS (USB GSM modem) | Enable (off), Serial Port, Baud Rate (9600), Phone Numbers (comma-separated, international format). **🔍 Detect Modem** checks the modem. Messages are cut to 160 characters. |

Each backend has a **📲 Send Test Notification** (or **Send Test SMS**) button; enable the
backend first. For a local ntfy server on Windows, see
[`tools/ntfy/README_NOTIFICATIONS.txt`](../tools/ntfy/README_NOTIFICATIONS.txt).

---

## 9. RF user guide

This chapter is for stations connecting to an Aether BBS. Your sysop can tell you its
callsign, frequency and modem (VARA FM, VARA HF, Aether HF or AX.25 packet).

### 9.1 Connecting

Connect to the BBS callsign with your terminal program over the matching modem. On
connect you see (if the sysop enabled them) a banner with the station, sysop, grid and
frequency, then the welcome message, `Hello <you>, you are connected to <BBS>`, your access
level, and any advertised alert addresses.

- **New station:** in most setups you see *Access Restricted*. Type `REGISTER`, answer the
  question about a community group (or `NONE`), and you are disconnected. The sysop
  reviews your request and sends your password by Winlink to `<YOURCALL>@winlink.org`.
- **Pending:** you are told your registration is pending; type `BYE`.
- **Blocked:** `Access denied. 73.`

Only one session may be allowed at a time; if someone else is on you get
`*** BBS BUSY ***`. Winlink programs (Winlink Express etc.) cannot exchange mail with the
BBS; use a Winlink gateway for that.

### 9.2 Logging in

If you have a password, the BBS sends `;AUTH HMAC-SHA256 <challenge>`:

- **Aether Term** with your password saved answers automatically. Your password is never
  transmitted.
- Any other terminal: type your password and press Enter, if the sysop allows plaintext
  login. It goes over the air in the clear.

`;AUTH OK` means you are in; `;AUTH FAIL` ends the session. You have 120 s to answer.
Repeated failures lock you out for a while and can block your callsign: ask the sysop.

Your password is shared by all your SSIDs. Change it with `SETPASS <newpassword>`
(4 characters or more).

After login you are told about waiting messages, new bulletins, urgent bulletins, and an
internet outage at the BBS (Winlink and email then wait until it is back).

### 9.3 Commands

Type commands at the `>` prompt. Case does not matter. `H` or `?` shows the help.
Numbers in `R`, `K`, `RB` and `KB` refer to the last `L` or `LB` list.

**Messages**

| Command | What it does |
|---|---|
| `L [area]` | List your messages (`*` = unread, `[WL]` = Winlink). The sysop sees all. |
| `R <n>` | Read message n |
| `S <call>` | Send a message held at the BBS for that station to collect |
| `S user@domain` | Send to an internet address (always via Winlink) |
| `SW <call>` | Send via Winlink |
| `K <n>` | Delete message n (yours, to or from you) |
| `KA` | Delete all messages (sysop only; asks Y/N) |
| `A` | List message areas |
| `U` | Messages addressed to you |
| `N` | New messages since your last visit |

When sending, the BBS asks for `Subject:` then the text; end with `.` on a line by itself.

**Bulletins**

| Command | What it does |
|---|---|
| `LB [area]` | List bulletins (`!` = urgent, `*` = unread) |
| `RB <n>` | Read bulletin n |
| `SB <group>` | Post a bulletin (ALL, WX, LOCAL, DX …) |
| `SUB <group>` | Post an URGENT bulletin |
| `KB <n>` | Delete your bulletin n (sysop: any) |
| `BA` | List bulletin areas with counts |

Posting needs TRUSTED in Restricted mode, APPROVED in Open mode, SYSOP in EMCOMM mode.

**Files**

| Command | What it does |
|---|---|
| `FA` | List file areas |
| `FL [area]` | List files |
| `FG <area> <file>` | Download a file |
| `FP <area> <file> [description]` | Upload a file |
| `FD <area> <file>` | Delete a file you uploaded (sysop: any) |

Downloads start with `FILE <name> <size> <crc32>`. The BBS then waits for your next line:
`CANCEL` aborts, anything else (Enter) starts the transfer. The file follows as base64
lines of 60 characters, then `EOF <name>`. The CRC-32 is of the original bytes, as 8
lowercase hex digits.

Uploads: after `FP`, the BBS says it is ready; your program sends
`PUTFILE <area> <file> <size> <crc32> [description]`, waits for `GO`, sends base64 lines,
then `EOF`. The BBS answers `OK …` or `ERROR <reason>`. `CANCEL` aborts. This needs a
terminal program that implements it.

**Winlink**

| Command | What it does |
|---|---|
| `WLREG` | Register your Winlink password for mail through this BBS (needs a BBS password; your terminal program asks for the Winlink password) |
| `WL` | Check the Winlink CMS for your mail now (30–120 s) |
| `WLOFF` | Stop Winlink forwarding for your callsign |

**Chat and other**

| Command | What it does |
|---|---|
| `CH` | Chat with the sysop; `/EXIT` to return |
| `CHAT [room]` or `CONVERSE [room]` | Join a chat room (default LOCAL); `/WHO`, `/ROOM <name>`, `/EXIT` |
| `MH` | Stations recently heard on this port |
| `I` | BBS information: callsign, sysop, grid, transport, port, your access, mode, counts |
| `P <n>` | Lines per page, 5–100 (default 20). At `-- More --` press Enter, or `Q` to stop. Remembered for your callsign. |
| `TACTICAL <addr>` / `TACTICAL OFF` | Use a tactical address for this session (see 9.5) |
| `SETPASS [password]` | Set or change your password |
| `REGISTER` or `REG` | Request access |
| `B`, `BYE`, `73`, `QUIT`, `EXIT` | Disconnect |
| `H`, `?`, `HELP` | Help |

Commands that your access level does not allow answer `Permission denied for this
command.`

**For terminal-program authors:** `;TRANSPORT?` returns `;TRANSPORT <VARA_FM|VARA_HF|AETHER_HF|SOUNDMODEM (AGWPE)>`,
also while the login challenge is waiting. `;TACTICAL <addr>` answers
`;TACTICAL OK <addr>`, `;TACTICAL OK cleared` or `;TACTICAL FAIL not configured`.

### 9.4 Session timeout

If you send nothing for the session timeout (600 s by default, at most 120 s in EMCOMM
mode), the BBS sends `Session timeout. 73 de BBS.` and disconnects the link. While you sit
at the prompt the BBS resends the prompt every 60 s to keep the modem link alive. Any
session is ended after the session watchdog (30 min by default).

### 9.5 Tactical addresses

The sysop may define tactical addresses (for example a shelter or net call), listed after
login as *Alert addresses*. `S <address>` sends a message there and alerts the sysop.

`TACTICAL <address>` (one the BBS has configured) makes it your identity for the session:
messages you send with `S`/`SW` are stored as from that address, and `WL` checks Winlink for
it as well as your own call, using your Winlink password. `TACTICAL OFF` clears it.

---

## 10. Testing without a radio

### 10.1 Aether Term local test mode

Aether Term can play the part of a VARA FM modem on the same computer, so you can connect
to your BBS without a radio. Nothing is transmitted.

1. In Aether BBS, **Settings → Instances → + Add**:
   Name `Local test`, Transport `VARA_FM`, CMD port `8500` (Data port becomes `8501`),
   Auto Start off, Enabled on. The host is `127.0.0.1` by default (there is no Host field
   for VARA instances). **💾 Save Settings**.
2. In Aether Term, choose *Local test* and start it first (its ports must match, 8500/8501
   by default; *Connect as* sets the callsign you test with).
3. The BBS instance connects. It tries 5 times over about 15 s and then gives up; if Aether
   Term was not ready, press **Start** on the *Local test* card on the dashboard.
4. Press Connect in Aether Term.

Use *Connect as* to test as the sysop (password challenge), as a stranger (registration),
or as an approved user. Disable the instance when you are done.

### 10.2 tools/vara_simulator.py

`tools/vara_simulator.py` is a console stand-in for a VARA modem: it answers the BBS's
init commands, and when you type a callsign it simulates an inbound connection and relays
your keyboard to the BBS. `/QUIT` simulates a disconnect.

```bash
python tools/vara_simulator.py                         # ports 8300/8301
python tools/vara_simulator.py --cmd 8500 --data 8501  # another port pair
python tools/vara_simulator.py --call W1AW             # connect as W1AW automatically
```

Point an instance (Auto Start off) at the same ports. `tests/vara_simulator.py` is a
different file: a simulator class used by the automated tests, not a tool to run by hand.

### 10.3 Other tools

- `tools/test_forwarding.py`: a fake neighbour BBS for the forwarding server ([6.3](#63-bulletin-forwarding)).
- `tools/diag_omnirig.py`, `tools/test_omnirig_ptt.py`, `tools/test_omnirig_rigcontrol.py`:
  OmniRig checks (Windows).
- `python -m pytest tests` runs the test suite (install `requirements-dev.txt` first).

---

## 11. Troubleshooting

### 11.1 Logs

The main log is `logs/aether_bbs.log` in the data folder ([2.3](#23-where-things-are-kept)),
also visible in **Diagnostics → Log**. The default level is WARNING, which leaves out
connection and init progress. For troubleshooting set **Settings → Logging → Log Level** to
INFO or DEBUG and restart.

| Logging setting | Default | Writes |
|---|---|---|
| Log decoded packet frames | on | `packet_frames.log` |
| Log raw VARA FM command port traffic | off | `vara_cmd.log`: every command-port line (useful for disconnects and WRONG) |
| Log sysop ↔ remote chat conversations | on | `chat.log` |

Log level and folder changes need a restart. A crash during start-up writes `crash.log`.

### 11.2 Common problems

| Symptom or log line | Cause and fix |
|---|---|
| `TCP connect attempt n/5 failed`, then `Could not connect to VARA FM after 5 attempts` | The modem is not running, or its ports differ from CMD/Data port. Start it (or turn on Auto Start with an Exe path), check its TCP port settings, then press **Start** on the card. |
| `VARA process started but port 8300 is not open after 3 s` | VARA started but uses other ports: match its settings to the instance. |
| `VARA REJECTED init command: '…' (WRONG)` / `init FAILED` | The modem refused a start-up command (for example a bandwidth it does not support, or a callsign it will not accept). The modem may not answer calls. Check the BBS callsign and Bandwidth; turn on the raw VARA log to see the exchange. |
| `reports MISSING SOUNDCARD` | The modem cannot open its sound card. Fix the audio device in the modem's own setup. |
| `Is Aether HF running with 'Host programs' on` | See [AETHER_HF.md §6](AETHER_HF.md#6-troubleshooting). |
| TCP light green, calls not answered | Another program holds the modem's host port, or init failed (see WRONG). For Aether HF check the card's warnings. |
| `Port … is used by more than one enabled instance` | Two enabled instances share a port (Soundmodem instances included). Change one. |
| `Soundmodem over KISS is not supported` | Set the protocol to AGWPE and the port to the program's AGWPE port (8000). |
| `AGWPE connect failed, retrying` | Soundmodem/Direwolf is not running or its AGWPE server is off. The BBS keeps retrying. |
| `Winlink enabled but Pat executable path is empty` | Set *Pat Executable* and restart. |
| `Pat executable not found at: …` | Fix the path. |
| `Pat exited immediately` / panel shows `Pat not running` | Run Pat by hand to see its error; check that nothing else uses the Pat HTTP address. |
| `No internet — Pat sync skipped` / status bar `NET: DOWN` | The BBS cannot reach 8.8.8.8:53. Winlink and email wait; RF users are told on connect. |
| `CMS sync failed` / `Pat connect failed` | Check the Winlink callsign and password, and the CMS Timeout on slow links. |
| An approved user is disconnected with *password has not yet been provisioned* | Give them a password (**🔑 Password**) and send it. |
| A user cannot log in | Look at *Failures* in User Management; **🔓 Reset Auth**. If BLOCKED, set a role again. |
| RF uploads always refused | In Whitelist mode the whitelist must list the caller (sysop included). Check the role needed for the mode and the area's RF-upload switch. |
| `*** BBS BUSY ***` | Another session is active and Max Sessions is 1. |
| Settings reset after an edit by hand | The file failed validation; the original is `aether_bbs.toml.bad.<date_time>` and the log names the sections that were reset. |

---

## 12. Configuration file reference

`aether_bbs.toml` is written by the Settings tab; you rarely need to edit it. Edit it only
while the BBS is stopped. Secrets in it are stored with an `ENC:` prefix; a plaintext value
you enter by hand (Winlink password, mailbox password, control token, tactical Winlink
password) is encrypted at the next start. Below are the main keys and defaults.

**`[station]`**

| Key | Default |
|---|---|
| `bbs_callsign` | `"N0CALL-10"` |
| `sysop_callsign` | `"N0CALL"` |
| `grid_square` | `""` |
| `latitude`, `longitude` | unset |
| `beacon_interval` | `10` (minutes, 0 = off) |
| `beacon_text` | `"Aether BBS"` |
| `[[station.tactical_addresses]]` | `callsign` (max 8), `alert_phrase` (`"Priority message received"`), `advertised` (`true`), `winlink_password`. Default: one entry, `BRECKEN`. |

**`[bbs]`**

| Key | Default |
|---|---|
| `welcome_message` | `"Welcome to the Aether BBS. Type H for help."` |
| `show_connect_banner` | `true` |
| `session_timeout` | `600` (s, min 30) |
| `message_dir` | `"messages"` |
| `max_message_age` | `30` (days) |
| `max_sessions` | `1` |
| `session_watchdog_timeout` | `1800` (s, min 60) |
| `[[bbs.bulletin_areas]]` | `name`, `description`, `enabled` (`true`), `max_age` (`14`). Default ALL, WX, LOCAL, DX. |
| `[bbs.forwarding]` | `server_enabled` (`false`), `server_port` (`7300`), `server_bind` (`"0.0.0.0"`) |
| `[[bbs.forwarding_neighbours]]` | `bbs_callsign`, `via` (`"TCP"` or `"RF"`), `instance`, `host` (`"127.0.0.1"`), `port` (`7300`), `enabled` (`true`), `interval_minutes` (`60`, min 5), `areas` (`[]` = all) |

**`[[instances]]`** (one block per instance)

| Key | Default | Notes |
|---|---|---|
| `name` | `"Primary"` | |
| `transport` | `"VARA_FM"` | `VARA_FM`, `VARA_HF`, `SOUNDMODEM`, `AETHER_HF` |
| `enabled` | `true` | |
| `frequency_label` | `""` | |
| `executable_path` | `""` | |
| `host` | `"127.0.0.1"` | Not in the GUI |
| `cmd_port`, `data_port` | `8300`, `8301` | 1024–65535; must differ; unique across enabled instances (Soundmodem too) |
| `auto_start` | `false` | |
| `auto_restart`, `restart_delay` | `true`, `10` | Restart a modem program the BBS started if it exits (not in the GUI) |
| `default_path` | `""` | |
| `bandwidth` | 500 for HF, 2300 for FM | VARA HF: 500, 2300, 2750 |
| `[instances.ptt]` | `method = "VOX"`, `port = ""`, `hamlib_host = "127.0.0.1"`, `hamlib_port = 4532`, `cm108_device = ""`, `omnirig_rig = 1`, `flrig_host = "127.0.0.1"`, `flrig_port = 12345`, `watchdog_timeout = 90.0` | |
| `[instances.rig_control]` | `method = "NONE"`, `omnirig_rig = 1`, `mode = "USB"`, `channels = []` | channel: `freq_khz`, `dwell_seconds`, `hours` |
| `[instances.soundmodem]` | `protocol = "AGWPE"`, `host = "127.0.0.1"`, `port = 8000`, `my_callsign = ""`, `my_ssid = 0`, `exe_path = ""`, `auto_launch = false` | `my_callsign` empty = BBS callsign |
| `[instances.aether]` | `control_port = 8515`, `control_token = ""`, `monitor = true`, `bandwidth = 0`, `config_file = ""`, `aprs_beacon = false`, `aprs_interval = 60` | see [AETHER_HF.md](AETHER_HF.md#2-add-an-aether-hf-instance-to-the-bbs) |

**`[security]`**

| Key | Default |
|---|---|
| `operational_mode` | `"restricted"` (`open`, `emcomm`) |
| `require_password` | `true` |
| `require_registration` | `true` |
| `auth_failure_threshold` | `5` |
| `auth_lockout_minutes` | `60` |
| `auto_block_threshold` | `20` |
| `allow_plaintext_auth` | `true` |
| `machine_secret` | generated on first start: keep it |

**`[winlink]`**: `enabled` (`false`), `gateway_callsign` (`""`), `gateway_password` (`""`,
encrypted), `grid_square` (`""`), `forward_calls` (`[]`), `pat_exe_path` (`""`),
`pat_http_addr` (`"localhost:8080"`), `auto_launch_pat` (`true`), `cms_timeout` (`120`).

**`[outbound_delivery.inbound_email]`**: `enabled` (`false`), `protocol` (`"IMAP"`),
`host` (`""`), `port` (`993`), `use_ssl` (`true`), `username`, `password` (encrypted),
`folder` (`"INBOX"`), `bbs_domain` (`""`), `poll_interval` (`5` min), `delete_after_fetch`
(`false`), `max_message_size` (`50000` characters), `allowed_senders` (`[]`).

**`[aprs]`**: `enabled` (`false`), `is_server` (`"rotate.aprs2.net"`), `is_port` (`14580`),
`is_passcode` (`-1`), `is_filter` (`""`), `beacon_symbol` (`"/B"`).

**`[file_upload]`**: `allow_remote_uploads` (`true`), `permission_mode` (`"whitelist"`;
`blocklist`, `open`, `role_based`), `upload_whitelist` (`[]`), `max_file_bytes`
(`5242880`), `max_files_per_callsign` (`20`), `allowed_extensions` (see 6.4),
`blocked_callsigns` (`[]`), `virus_scan_enabled` (`false`), `virus_scan_timeout` (`30`),
`quarantine_infected` (`true`), `quarantine_dir` (`"quarantine"`).

**`[notifications]`**: `enabled` (`false`), `ntfy_url` (`"http://127.0.0.1:8080"`),
`topic` (`"aether-bbs"`), `priority` (`"urgent"`), `cloud_relay` (`false`), `cloud_url`
(`"https://ntfy.sh"`), `pushover_enabled` (`false`), `pushover_api_token`,
`pushover_user_key`, `pushover_priority` (`1`), `pushover_sound` (`"siren"`),
`sms_enabled` (`false`), `sms_serial_port` (`""`), `sms_baud_rate` (`9600`),
`sms_phone_numbers` (`[]`), `notify_on_registration` (`true`),
`notify_on_urgent_bulletin` (`true`), `urgent_bulletin_areas` (`["EMCOMM", "WX", "ARES"]`).

**`[alert]`**: `audio_enabled` (`true`), `alert_interval` (`10` s), `volume` (`0.85`),
`visual_only` (`false`), `high_contrast` (`false`), `large_font` (`false`),
`session_sounds` (`true`).

**`[logging]`**: `level` (`"WARNING"`), `log_dir` (`"logs"`), `max_bytes` (`5242880`),
`backup_count` (`5`), `packet_log` (`true`), `vara_cmd_log` (`false`), `chat_log` (`true`).

**`[gui]`**: `theme` (`"dark"`), `log_lines` (`500`), `confirm_restart` (`true`).

**`[updater]`**: `check_on_startup` (`true`), `auto_check_interval_hours` (`0`),
`channel` (`"release"` or `"latest"`), `branch` (`"master"`), `repo`
(`"KK4ODA/aether-bbs"`), `skipped_version` (`""`).

Example instance blocks for each transport are in
[Transport Configuration](../README.md#transport-configuration) in the README. Note that
a Soundmodem instance there needs its own unused `cmd_port`/`data_port` (see
[3.3](#33-adding-a-modem-instance)), or it clashes with an instance on 8300/8301.
