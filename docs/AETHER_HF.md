# Aether HF with Aether BBS

[Aether HF](https://github.com/KK4ODA/aether-hf) is an open-source HF data modem with a
publicly documented air interface. Its daemon, `aetherd`, answers on VARA HF's published
host interface, so Aether BBS runs a session over Aether HF the same way it runs one over
VARA HF, and adds what Aether's control API makes possible: the modem's state, bandwidth,
signal reports, faults, the rules' verdict and the stations it heard, on the BBS dashboard.

On the air, Aether talks only to other Aether stations. The compatibility is in the
software, not the signal: a VARA station cannot connect to an Aether BBS, and an Aether
gateway must never be listed as a VARA one.

> Aether HF is in beta. Two stations connect only when they run the same *link protocol*;
> a release that changes it is marked **Update required**. Keep the BBS's Aether HF and
> your users' copies current.

---

## 1. Install and set up Aether HF

1. Download the installer from
   [Aether HF releases](https://github.com/KK4ODA/aether-hf/releases) and follow its
   [install guide](https://github.com/KK4ODA/aether-hf/blob/master/docs/user/install.md).
   On Windows it installs for the current user under `%LOCALAPPDATA%\Aether HF\`.
2. Work through Aether's **Setup** tab:
   * **Step 1, the rules.** Aether transmits nothing until you say which rules the station
     runs under, how it is controlled and your licence class. An unattended BBS is usually
     **automatic control**: Aether then keeps it to the automatic-control sub-bands
     (§97.221(b) under the US profile) and judges every transmission before keying.
   * **Step 2, the radio.** Sound card and keying (CAT, serial RTS/DTR, CM108 or
     `rigctld`/FLRig). Let Aether key the radio unless you have a reason not to (§4).
   * **Step 4, bandwidth.** 2300 Hz or 500 Hz. A 2300 Hz station still answers a 500 Hz call
     at 500 Hz.
   * **Step 5, Host programs: on**, port **8300** (the BBS connects here; the data port is
     8301). This is off by default because it lets other software key the radio.
3. Save. Aether's callsign does not matter much: while the BBS is attached it sets
   `MYCALL` to the BBS callsign.

## 2. Add an Aether HF instance to the BBS

**Settings → Instances → + Add**, then:

| Field | Value |
|---|---|
| Transport | `AETHER_HF` |
| Exe path | Aether HF's application, filled in when you pick the transport if it is installed (or *Find installed*) |
| Auto Start | Starts Aether HF with the BBS if it is not already running |
| CMD port | Aether's *Host programs* port, 8300 unless you changed it (data port = CMD + 1) |
| Bandwidth | *Aether's own setting* (recommended), or 500 / 2300 to ask for one while the BBS is attached |
| Control API port | 8515 (Aether's `[control]` port) |
| Control token | Only for an Aether HF on another computer with a non-loopback control bind |
| Show Aether HF status | Polls the control API for the dashboard and MHeard |

Press **Test connection**: it asks Aether's control API for its status and lists anything
that would stop the BBS working (host interface off, sound card or keying fault, no rules
chosen, TX blocked at this dial). Save, and the instance starts.

The same settings in `aether_bbs.toml`:

```toml
[[instances]]
name = "HF-Aether"
transport = "AETHER_HF"
enabled = true
auto_start = true
executable_path = 'C:\Users\you\AppData\Local\Aether HF\aether-hf.exe'
host = "127.0.0.1"
cmd_port = 8300            # Aether's "Host programs" port
data_port = 8301           # always cmd_port + 1

[instances.aether]
control_port = 8515        # Aether's control API
control_token = ""         # remote aetherd only; stored encrypted
monitor = true             # dashboard status + MHeard
aprs_beacon = false        # APRS position as an Aether datagram
aprs_interval = 60         # minutes (10–1440)
bandwidth = 0              # 0 = Aether's own; 500 or 2300 = ask for it
config_file = ""           # --config, standalone aetherd only
```

VARA HF and Aether HF both default to port 8300. To run both, give one of them another
port pair (in the modem and in the BBS); the BBS refuses two enabled instances on one port.

## 3. What the BBS does with Aether HF

**Attaching.** The BBS opens the host interface and sends `MYCALL <bbs callsign>`,
`COMPRESSION OFF`, `BW<n>` (only when *Bandwidth* asks for one), `LISTEN ON` and `VERSION`.
Until `LISTEN ON`, Aether answers no calls while a host is attached, so this matters. Aether
accepts one host program at a time; close Winlink Express, Pat or VarAC on that port first.

**Sessions.** An inbound call arrives as `PENDING` then `CONNECTED <caller> <called> <bw>`;
the BBS takes the caller as the remote station and runs the normal BBS session. `SN` and
`BITRATE` lines show as *Link: SNR … · level … · bps* on the instance card.

**The dashboard card** for an Aether instance shows a line such as
`Aether HF 0.2.0-beta.102 · N0CALL-10 · 2300 Hz · 14.105 MHz · idle · TX legal`, and below it,
in amber, anything that needs attention. The same conditions are written to the log once
each, with a matching *resolved* line when they clear.

**Probe and Beacon.** The card's *Probe…* asks another Aether station whether it hears the
BBS and how well: one short transmission each way on Aether's tone floor, no session. The
answer appears on the card, for example `KK4XYZ hears us at 7 dB, heard at 9 dB`. *Beacon*
sends one beacon with the BBS callsign; Aether refuses beacons under automatic control
(§97.203(d)), and the card says so.

**APRS on the air.** *APRS beacon on the air, every … min* (off by default) sends the BBS's
APRS position, from the Station settings, as an AX.25 frame in an Aether datagram. Every
Aether station that decodes it hands it to its own APRS programs over its KISS port. The BBS
uses Aether's control API rather than its KISS port, because that port holds back program
frames while a host program such as the BBS is attached. Aether judges each beacon as a
transmission this station originates: under automatic control, only where the rules allow
it. Datagrams wait while a session is up.

**Update required.** When a station on another Aether link protocol calls the BBS, or answers
it, the card shows *Update required* with Aether's sentence naming which station must update.
The two cannot connect until then.

**Stations heard.** The dashboard's *Aether HF — Stations Heard* table shows Aether's own
list, refreshed about every 10 seconds: each station's last and best SNR, when it was last
heard, what it was doing (beacon, calling, probing, answering, session, KISS frame) and whom
it was calling, its dial and bandwidth, and how often it was heard. Click a column to sort;
hover over a callsign for when it was first heard and whether a session has been up with it;
right-click it to **Probe** it. With two Aether HF instances an *Instance* column says which
one heard it. The table needs *Show Aether HF status* (`monitor = true`).

**MHeard.** Stations Aether hears (beacons, calls, probes, datagrams, sessions) are added to
the BBS's heard list under the instance's name.

**Bulletin forwarding.** A forwarding neighbour set to *via RF* with this instance is
called through Aether HF on its interval (see the README's *Bulletin forwarding*). Under
automatic control, Aether's rules decide where the BBS may originate a call; a call they
refuse ends at once and is tried again at the next interval.

**Starting and stopping.** Auto Start launches the executable only when Aether's control API
is not already answering; the BBS checks the control API, never the host port, because a
probe connection to the host port could take the one host slot. Stopping the instance stops
a process the BBS started; an Aether HF you started yourself keeps running.

## 4. Keying: who keys the radio

* **Aether keys the radio** (recommended). Leave the instance's PTT method at `VOX`, which
  for Aether means "the BBS does not key". Aether's rules check stays on.
* **Host program keys the radio.** Aether's Setup step 2 *host program* makes Aether say
  `PTT ON` / `PTT OFF` and wait for the host to key. Choose a PTT method on the BBS instance
  (RTS/DTR, CAT via OmniRig, FLrig, Hamlib, CM108) as for VARA HF. Aether's rules check is off
  in this mode: you check every transmission.

Only one program can hold the radio's CAT port. If Aether reads the dial over CAT, do not
also give the BBS OmniRig rig control or scanning on the same port; share the radio through
`rigctld` or FLRig instead.

## 5. Aether HF on another computer

Set the instance's `host` in `aether_bbs.toml` to that computer's address. Aether's host
port and control API listen on loopback by default; binding them elsewhere is done in
Aether's own configuration, and a non-loopback control API requires a token, which goes in
*Control token*. The host interface has no password, so only expose it on a network you
control.

## 6. Troubleshooting

| Symptom | Fix |
|---|---|
| *Host programs are off in Aether HF* | Aether Setup step 5: Host programs on, port matching the BBS CMD port |
| *Could not connect to Aether HF … Is Aether HF running* | Start Aether HF (or enable Auto Start); check the port; close any other program attached to it |
| Attached, but calls are not answered | Check the card for *not answering calls*; check Aether's log for calls to another SSID or another bandwidth (the card shows Aether's *mismatch* sentence) |
| *No rules chosen* / *TX blocked here* | Aether Setup step 1, or tune to a dial the rules allow (automatic control keeps to the automatic sub-bands) |
| *control API not reachable* | Aether is not running, or `control_port` differs from Aether's `[control]` port; sessions still work without it |
| *MISSING SOUNDCARD* in the log | Aether's sound card would not open: check it in Aether's Setup step 2 |
| The other station cannot connect | Both stations must run the same Aether link protocol: the card says *Update required* and which one; update it |
| Probe says *no answer* | The other station is not listening, is on another dial or bandwidth, or cannot hear the BBS; try again or check the band |

Aether HF's own documentation:
[host interfaces](https://github.com/KK4ODA/aether-hf/blob/master/docs/spec/host-interfaces.md),
[control API](https://github.com/KK4ODA/aether-hf/blob/master/docs/spec/control-api.md),
[host programs](https://github.com/KK4ODA/aether-hf/blob/master/docs/user/host-programs.md),
[FCC regulatory controls](https://github.com/KK4ODA/aether-hf/blob/master/docs/user/fcc-regulatory-controls.md).
