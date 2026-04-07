"""
aprs/gateway.py

APRS-IS gateway:
  - Maintains a persistent TCP connection to rotate.aprs2.net
  - Beacons BBS status on a configurable interval
  - Receives and decodes APRS frames from IS (filtered by configured region)
  - Emits APRS events on the EventBus
  - Reconnects automatically on TCP failure
"""

import time
import socket
import logging
import threading
from datetime import datetime
from typing import Optional

from core.config import AppConfig, ConfigManager
from core.events import EventBus, EventType
from vara.rigcontrol import _hour_in_range

log = logging.getLogger(__name__)

RECONNECT_BASE  = 30    # seconds
RECONNECT_MAX   = 300   # seconds


def aprs_passcode(callsign: str) -> int:
    """
    Compute the APRS-IS passcode for a callsign.
    Algorithm: XOR of alternating pairs of characters.
    """
    call = callsign.upper().split("-")[0]
    code = 0x73E2
    for i, ch in enumerate(call):
        if i % 2 == 0:
            code ^= ord(ch) << 8
        else:
            code ^= ord(ch)
    return code & 0x7FFF


class APRSGateway:
    """
    Manages the APRS-IS connection and periodic BBS beacons.
    """

    def __init__(self, config_manager: ConfigManager, event_bus: EventBus):
        self._cm  = config_manager
        self._bus = event_bus

        self._sock:    Optional[socket.socket] = None
        self._connected = False
        self._verified  = False
        self._stop     = threading.Event()

        self._rx_thread:     Optional[threading.Thread] = None
        self._beacon_thread: Optional[threading.Thread] = None

        # Monotonic timestamp of the last beacon sent by ANY path (auto or manual).
        # The beacon loop reads this so a manual beacon resets the auto countdown.
        self._last_beacon_time: float = 0.0

    @property
    def _config(self) -> AppConfig:
        """Always returns the current live config."""
        return self._cm.current()


    def start(self):
        self._stop.clear()
        threading.Thread(
            target=self._connect_loop,
            name="APRS-IS-Connect",
            daemon=True,
        ).start()
        log.info("APRS gateway started")

    def stop(self):
        """Send an offline status beacon, then shut down the gateway."""
        # Overwrite the cached aprs.fi status with "BBS OFFLINE" so other
        # operators know the BBS is no longer listening.
        if self._connected:
            try:
                call = self._config.station.bbs_callsign
                offline_pkt = f"{call}>APRS,TCPIP*:>BBS OFFLINE"
                self._send_line(offline_pkt)
                log.info(f"APRS offline beacon sent: {offline_pkt}")
            except Exception as e:
                log.debug(f"Could not send offline beacon: {e}")
        self._stop.set()
        self._close()
        log.info("APRS gateway stopped")

    # ── Connection management ─────────────────────────────────────────────

    def _connect_loop(self):
        delay = RECONNECT_BASE
        while not self._stop.is_set():
            try:
                self._connect()
                delay = RECONNECT_BASE   # reset on success
            except Exception as e:
                log.error(f"APRS-IS connect error: {e}")
            if not self._stop.is_set():
                log.info(f"APRS-IS reconnect in {delay}s")
                self._stop.wait(delay)
                delay = min(delay * 2, RECONNECT_MAX)

    def _connect(self):
        cfg = self._config.aprs
        log.info(f"Connecting to APRS-IS {cfg.is_server}:{cfg.is_port}")

        self._sock = socket.create_connection(
            (cfg.is_server, cfg.is_port), timeout=30
        )
        self._sock.settimeout(60)

        # Login — read passcode fresh from live config
        call = self._config.station.bbs_callsign
        cfg_aprs = self._config.aprs
        passcode = (
            cfg_aprs.is_passcode
            if cfg_aprs.is_passcode > 0
            else aprs_passcode(call)
        )
        login = (
            f"user {call} pass {passcode} "
            f"vers VARA-FM-BBS 1.0"
        )
        if cfg.is_filter:
            login += f" filter {cfg.is_filter}"
        self._send_line(login)

        # Read server banner lines until logresp (may be several # comment lines first)
        verified = False
        for _ in range(5):
            banner = self._recv_line(timeout=10)
            log.info(f"APRS-IS: {banner}")
            if "logresp" in banner.lower():
                verified = "verified" in banner.lower() and "unverified" not in banner.lower()
                if verified:
                    log.info("APRS-IS login verified — beacon packets will be accepted")
                else:
                    log.warning(
                        "APRS-IS login UNVERIFIED — beacons will be dropped by server. "
                        "Check your passcode in Settings → APRS."
                    )
                break

        self._connected = True
        self._verified  = verified
        self._bus.emit(EventType.APRS_IS_CONNECTED, source="APRS-IS",
                       data={"verified": verified})

        # Start receiver
        self._rx_thread = threading.Thread(
            target=self._receive_loop,
            name="APRS-IS-RX",
            daemon=True,
        )
        self._rx_thread.start()

        # Start beacon
        self._beacon_thread = threading.Thread(
            target=self._beacon_loop,
            name="APRS-IS-Beacon",
            daemon=True,
        )
        self._beacon_thread.start()

        # Wait for receiver to exit (connection dropped)
        self._rx_thread.join(timeout=60)
        self._connected = False
        self._bus.emit(EventType.APRS_IS_DISCONNECTED, source="APRS-IS")

    def _receive_loop(self):
        buf = ""
        while not self._stop.is_set():
            try:
                chunk = self._sock.recv(1024).decode("ascii", errors="replace")
                if not chunk:
                    break
                buf += chunk
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    line = line.strip()
                    if line and not line.startswith("#"):
                        self._handle_aprs_frame(line)
            except socket.timeout:
                # Send keepalive
                try:
                    self._send_line("#keepalive")
                except OSError:
                    break
            except OSError:
                break

    def _beacon_loop(self):
        interval = self._config.station.beacon_interval * 60
        if interval <= 0:
            return

        # Flush stale aprs.fi status immediately on connect (before first beacon).
        self._stop.wait(5)
        if not self._stop.is_set() and self._connected:
            self._send_status_packet()

        # Wait up to 30 s for the first auto-beacon, but yield immediately if a
        # manual beacon was already sent during that window.
        self._wait_until_due(30)

        while not self._stop.is_set() and self._connected:
            self._send_beacon()
            # Sleep for the configured interval, but wake early if a manual
            # beacon fires and resets _last_beacon_time.
            self._wait_until_due(interval)

    def _wait_until_due(self, desired_interval: float):
        """
        Sleep until at least `desired_interval` seconds have elapsed since
        the last beacon (manual or auto).  Polls every second so that:
          • _stop can interrupt it cleanly
          • a manual beacon that stamps _last_beacon_time is noticed within 1 s
        """
        while not self._stop.is_set():
            elapsed = time.monotonic() - self._last_beacon_time
            remaining = desired_interval - elapsed
            if remaining <= 0:
                return
            # Sleep in 1-second slices so we stay responsive
            self._stop.wait(min(1.0, remaining))

    # ── Beacons ───────────────────────────────────────────────────────────

    # ── Beacon formatting ─────────────────────────────────────────────────

    @staticmethod
    def _encode_lat(lat: float) -> str:
        """Encode decimal latitude to APRS DDMM.MM[NS] format."""
        hemi = "N" if lat >= 0 else "S"
        lat  = abs(lat)
        deg  = int(lat)
        mins = (lat - deg) * 60
        return f"{deg:02d}{mins:05.2f}{hemi}"

    @staticmethod
    def _encode_lon(lon: float) -> str:
        """Encode decimal longitude to APRS DDDMM.MM[EW] format."""
        hemi = "E" if lon >= 0 else "W"
        lon  = abs(lon)
        deg  = int(lon)
        mins = (lon - deg) * 60
        return f"{deg:03d}{mins:05.2f}{hemi}"

    def _build_status_packet(self) -> str:
        """
        Build a standalone APRS Status Report packet (data type '>').

        aprs.fi stores the last status packet INDEPENDENTLY of position packets
        and renders it alongside the station tooltip in a distinct colour.  Any
        previously transmitted '>BBS OFFLINE' status will persist on aprs.fi
        until overwritten by a fresh '>…' packet for the same callsign.

        This packet carries no position information and will not create a map
        icon on its own, but it WILL overwrite the cached status string that
        aprs.fi displays next to the station.

        The grid square (Maidenhead locator) is appended when configured; it is
        the only optional field allowed in a plain Status Report.  Timestamp
        prefixes (MDHM format) are intentionally omitted — they constrain the
        packet to a specific time window and cause spurious expiry on some servers.
        """
        call = self._config.station.bbs_callsign
        grid = self._config.station.grid_square

        # The status packet's job is to:
        #   1. Overwrite any stale '>BBS OFFLINE' cached on aprs.fi.
        #   2. Provide the Maidenhead grid square (optional locator field).
        #
        # The beacon_text (e.g. "DeKalb ARES BBS") is intentionally NOT
        # repeated here — it already appears in the position packet's comment
        # field.  Repeating it causes aprs.fi to display it twice:
        #   "DeKalb ARES BBS.  EM73uu DeKalb ARES BBS."
        #
        # Per APRS 1.0.1 §16, a Status Report may carry a Maidenhead locator
        # (up to 6 chars) followed by an optional free-text comment separated
        # by a space.  We use "Online" as that comment so the status field
        # reads "EM73uu Online" rather than being blank or repeating the name.
        # If no grid is configured the status is simply "Online".
        # Find the frequency label of the first enabled instance.
        # Config is re-read via the manager so a live settings change is
        # picked up on the next beacon without restarting the gateway.
        # Collect frequency labels from ALL enabled instances (not just the first)
        # e.g. "145.530FM HF/500:3585.5,7122,14108" so listeners know all channels.
        # HF instances with scan channels list all scanned frequencies compactly.
        freq_parts = []
        try:
            for inst in self._config.instances:
                if not inst.enabled:
                    continue
                mode = getattr(inst, "mode", "FM").upper()

                if mode == "HF":
                    bw = getattr(inst, "bandwidth", None) or 500
                    # Check for scan channels
                    rc = getattr(inst, "rig_control", None)
                    channels = getattr(rc, "channels", []) if rc else []
                    if channels:
                        # List only channels active at the current UTC hour.
                        # Each channel has an 'hours' field (e.g. "14-02") that
                        # defines its active window — the same filter used by
                        # RigController._get_active_channels() at scan time.
                        utc_hour = datetime.utcnow().hour
                        def _fmt(f):
                            return (f"{f:.1f}".rstrip("0").rstrip(".")
                                    if f != int(f) else str(int(f)))
                        active_chs = [ch for ch in channels
                                      if _hour_in_range(utc_hour, ch.hours)]
                        if active_chs:
                            freqs = ",".join(_fmt(ch.freq_khz) for ch in active_chs)
                            freq_parts.append(f"HF/{bw}:{freqs}")
                    elif inst.frequency_label.strip():
                        # No scan channels — use static frequency label
                        freq_parts.append(
                            f"{inst.frequency_label.strip()}HF/{bw}"
                        )
                elif inst.frequency_label.strip():
                    freq_parts.append(
                        f"{inst.frequency_label.strip()}{mode}"
                    )
        except Exception:
            pass  # never let a bad config break the beacon

        freq = " ".join(freq_parts)

        # Build status: "<grid> <freqs> Online"  (omit blank fields)
        parts = [p for p in [grid, freq, "Online"] if p]
        status = " ".join(parts)

        return f"{call}>APRS,TCPIP*:>{status}"

    def _build_beacon(self) -> str:
        """
        Build the APRS position packet string.

        Data-type identifier choice — '!' vs '=':
        ─────────────────────────────────────────
        '!' → Position without timestamp, NOT messaging-capable.
        '=' → Position without timestamp, MESSAGING-CAPABLE (MSG flag set).

        The MSG flag ('=') tells aprs.fi (and the wider APRS-IS network) that
        this station can receive and respond to APRS message packets.  aprs.fi
        uses this flag to infer that a /B (BBS) station is queryable.  If no
        APRS message handler is running (this BBS does not respond to APRS
        messages), aprs.fi will receive no acknowledgement for any message it
        routes to the station and will mark it as unreachable — which it renders
        as 'BBS OFFLINE' appended to the station info panel.

        We use '!' here because:
        1. This BBS is a VARA-FM BBS, not an APRS message relay.
        2. No APRS message handler is implemented in this software.
        3. Declaring MSG-capability we cannot honour is a protocol lie that
           directly causes the 'BBS OFFLINE' symptom on aprs.fi.

        Symbol '/B' is retained — it is the correct APRS primary-table BBS
        symbol and allows the station to appear in BBS/station lists correctly.
        It does NOT by itself cause the 'OFFLINE' label; the MSG flag does.
        """
        call   = self._config.station.bbs_callsign
        text   = self._config.station.beacon_text
        raw_sym   = self._config.aprs.beacon_symbol or "/B"
        if len(raw_sym) >= 2 and raw_sym[0] in ("/", "\\"):
            sym_table = raw_sym[0]
            sym_code  = raw_sym[1]
        else:
            sym_table = "/"
            sym_code  = "B"
            log.warning(
                f"Invalid beacon_symbol {raw_sym!r} — using /B (BBS). "
                "Set to two chars: table (/ or \\) + symbol code."
            )
        lat = self._config.station.latitude
        lon = self._config.station.longitude

        if lat is not None and lon is not None and (lat != 0.0 or lon != 0.0):
            # ── Position beacon — plots the station on the aprs.fi map ──────
            # Data type '!' = position without timestamp, NOT messaging-capable.
            # This is correct for a BBS that does not handle APRS messages.
            enc_lat = self._encode_lat(lat)
            enc_lon = self._encode_lon(lon)
            info = f"!{enc_lat}{sym_table}{enc_lon}{sym_code}{text}"
        else:
            # ── Status-only beacon (no lat/lon configured) ───────────────────
            # Data type '>' = Status Report.  aprs.fi will update its cached
            # status string for this callsign.  Grid square appended per spec.
            grid = self._config.station.grid_square
            status = text
            if grid:
                status = f"{grid} {status}"
            info = f">{status}"

        return f"{call}>APRS,TCPIP*:{info}"

    def _send_status_packet(self):
        """
        Send a standalone Status Report ('>' packet) to APRS-IS.

        Stamps _last_beacon_time so _wait_until_due() treats this as a beacon
        event — preventing the auto-loop from firing a position beacon
        immediately after the connect-flush status packet goes out.
        """
        status_pkt = self._build_status_packet()
        try:
            self._send_line(status_pkt)
            self._last_beacon_time = time.monotonic()   # resets auto-loop countdown
            self._bus.emit(EventType.APRS_BEACON_SENT, source="APRS-IS",
                           data={"beacon": status_pkt})
            log.info(f"APRS status packet sent: {status_pkt}")
        except OSError as e:
            log.warning(f"APRS status packet send failed: {e}")

    def _send_beacon(self):
        """
        Send a position beacon to APRS-IS.

        Stamps _last_beacon_time so _beacon_loop knows a beacon just went out
        regardless of whether this call came from the auto loop or the manual
        beacon button in the GUI.  This prevents the loop from firing again a
        few seconds after a manual send.

        Also sends a companion status packet every beacon cycle so that
        aprs.fi's cached status string stays current even across server
        reconnections that might replay an older status.
        """
        beacon = self._build_beacon()
        try:
            self._send_line(beacon)
            self._last_beacon_time = time.monotonic()   # reset auto-loop countdown
            self._bus.emit(EventType.APRS_BEACON_SENT, source="APRS-IS",
                           data={"beacon": beacon})
            log.info(f"APRS beacon sent: {beacon}")
        except OSError as e:
            log.warning(f"APRS beacon send failed: {e}")
            return

        # Send a companion status packet 2 seconds after each position beacon.
        # aprs.fi processes them as independent records; the status packet
        # refreshes the 'last status' field without interfering with the map icon.
        # We only do this when we have a real position (lat/lon configured),
        # because in status-only mode _build_beacon() already emits a '>' packet.
        lat = self._config.station.latitude
        lon = self._config.station.longitude
        has_position = lat is not None and lon is not None and (lat != 0.0 or lon != 0.0)
        if has_position:
            self._stop.wait(2)
            if not self._stop.is_set():
                self._send_status_packet()


    # ── Frame handling ────────────────────────────────────────────────────

    def _handle_aprs_frame(self, frame: str):
        self._bus.emit(
            EventType.APRS_FRAME_RECEIVED,
            source="APRS-IS",
            data={"frame": frame, "timestamp": datetime.now().isoformat()},
        )
        log.debug(f"APRS-IS RX: {frame[:80]}")

    # ── Socket helpers ────────────────────────────────────────────────────

    def _send_line(self, line: str):
        if self._sock:
            self._sock.sendall((line + "\r\n").encode("ascii", errors="replace"))

    def _recv_line(self, timeout: float = 30) -> str:
        self._sock.settimeout(timeout)
        buf = b""
        while b"\n" not in buf:
            chunk = self._sock.recv(256)
            if not chunk:
                break
            buf += chunk
        return buf.decode("ascii", errors="replace").strip()

    def _close(self):
        self._connected = False
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def is_verified(self) -> bool:
        return self._verified
