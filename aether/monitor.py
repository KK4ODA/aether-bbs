"""
aether/monitor.py

Background poller for one Aether HF instance's control API.

Every few seconds it reads ``status`` and condenses it into a summary the
GUI shows on the instance card (EventType.AETHER_STATUS).  It logs, once
per change, the conditions that keep a BBS from working: the host interface
switched off, a sound card or keying fault, no regulatory profile chosen,
or the station not answering calls.  It also feeds stations Aether has heard
(beacons, calls, probes, datagrams) into the BBS's MHeard log, and publishes
Aether's whole stations-heard list, with SNR, for the dashboard
(EventType.AETHER_HEARD).
"""

import logging
import threading
from typing import Optional

from aether.control import AetherControlClient, AetherControlError
from core.events import EventBus, EventType

log = logging.getLogger(__name__)

POLL_INTERVAL_S = 5.0
# Problems about the host program's claim are reported only when two polls
# in a row see them: for a few seconds after the BBS attaches, Aether shows
# its claim before MYCALL and LISTEN ON have been taken.
_SETTLING = ("Aether HF is not answering calls", "Aether HF does not answer calls to")
HEARD_EVERY_N_POLLS = 2          # heard.list every ~10 s


def summarize_status(st: dict) -> dict:
    """Reduce a control-API ``status`` result to what the BBS displays."""
    bw = st.get("bandwidth") or {}
    host = st.get("host") or {}
    reg = st.get("regulatory") or {}
    indicator = reg.get("indicator") or {}
    metrics = st.get("metrics") or {}
    state = str(st.get("state", "")).lower()
    # The host program's claim (ADR-0106): the callsigns it answers for and
    # whether it listens.  ``answering`` is about the station's own first
    # callsign, which a BBS attached under another callsign does not hold.
    host_claim = None
    for claim in st.get("claims") or []:
        if isinstance(claim, dict) and claim.get("kind") == "host":
            host_claim = {
                "callsigns": [str(c).upper() for c in claim.get("callsigns") or []],
                "listening": bool(claim.get("listening", False)),
            }
            break
    return {
        "reachable":     True,
        "version":       st.get("version", ""),
        "state":         state,
        "callsign":      st.get("callsign", ""),
        # status keeps the last station after a session; show it only
        # while there is one
        "remote":        (st.get("remote") or "") if state in (
                             "connecting", "connected", "disconnecting") else "",
        "bandwidth_hz":  bw.get("bandwidth_hz"),
        "mode":          st.get("mode"),
        "frequency_hz":  st.get("frequency_hz"),
        "transmitting":  bool(st.get("transmitting", False)),
        "channel_busy":  bool(st.get("channel_busy", False)),
        "snr_db":        metrics.get("snr_db"),
        "host_enabled":  host.get("enabled"),
        "host_attached": host.get("connected"),
        "answering":     st.get("answering"),
        "not_answering": st.get("not_answering") or "",
        "host_claim":    host_claim,
        "audio_fault":   st.get("audio_fault") or "",
        "ptt_fault":     st.get("ptt_fault") or "",
        "policy":        reg.get("policy", ""),
        "verdict":       indicator.get("verdict", ""),
        "verdict_text":  indicator.get("summary", ""),
        "mismatch":      (bw.get("mismatch") or {}).get("sentence", ""),
        "mismatch_what": (bw.get("mismatch") or {}).get("what", ""),
    }


# Aether HF's own words for what a station was last doing (its Stations tab).
ACTIVITY_TEXT = {
    "beacon": "beacon",
    "calling": "calling",
    "probing": "probing",
    "answering": "answering",
    "connected": "session",
    "datagram": "KISS frame",
}


def _num(value):
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def heard_rows(stations: list[dict]) -> list[dict]:
    """Reduce ``heard.list`` entries to what the dashboard shows."""
    rows = []
    for st in stations:
        call = str(st.get("callsign", "")).upper()
        if not call:
            continue
        activity = str(st.get("activity", ""))
        rows.append({
            "callsign":       call,
            "snr_db":         _num(st.get("snr_db")),
            "best_snr_db":    _num(st.get("best_snr_db")),
            "last_heard_ms":  int(st.get("last_heard_ms") or 0),
            "first_heard_ms": int(st.get("first_heard_ms") or 0),
            "frequency_hz":   _num(st.get("frequency_hz")),
            "bandwidth_hz":   st.get("bandwidth_hz"),
            "activity":       ACTIVITY_TEXT.get(activity, activity),
            "detail":         str(st.get("detail") or ""),
            "count":          int(st.get("count") or 0),
            "beacons":        int(st.get("beacons") or 0),
            "connected":      bool(st.get("connected", False)),
        })
    return rows


def problems(summary: dict, bbs_callsign: str = "") -> list[str]:
    """Operator-facing sentences for what stops the BBS from working.

    ``bbs_callsign`` is the callsign the BBS attaches as (its MYCALL).
    """
    out: list[str] = []
    if summary.get("host_enabled") is False:
        out.append("Host programs are off in Aether HF — turn on "
                   "'Host programs' in Aether's Setup (step 5) so the BBS "
                   "can attach.")
    if summary.get("audio_fault"):
        out.append(f"Sound card: {summary['audio_fault']}")
    if summary.get("ptt_fault"):
        out.append(f"Keying: {summary['ptt_fault']}")
    policy = summary.get("policy")
    if policy == "unset":
        out.append("No rules chosen in Aether HF (Setup step 1) — it will "
                   "transmit nothing until they are.")
    elif policy == "broken":
        out.append("Aether HF's regulatory profile would not load — it will "
                   "transmit nothing. See its log.")
    if summary.get("verdict") == "blocked":
        out.append(f"TX blocked here: {summary.get('verdict_text') or 'see Aether HF'}")
    claim = summary.get("host_claim")
    call = (bbs_callsign or "").upper()
    if summary.get("host_attached") and claim is not None:
        if not claim["listening"]:
            out.append("Aether HF is not answering calls for the program on "
                       "its host port (LISTEN is off).")
        elif call and call not in claim["callsigns"]:
            held = ", ".join(claim["callsigns"]) or "no callsign"
            out.append(f"Aether HF does not answer calls to {call}: the "
                       f"program on its host port answers for {held}. Another "
                       f"program may hold its host port, or {call}.")
    elif summary.get("host_attached") and summary.get("answering") is False:
        # An Aether HF from before program claims (ADR-0106)
        out.append("Aether HF is not answering calls (LISTEN is off).")
    if summary.get("mismatch"):
        if summary.get("mismatch_what") == "version":
            # Another station on a different link protocol: the two cannot
            # connect until one of them updates (Aether ADR-0041).
            out.append(f"Update required — {summary['mismatch']}")
        else:
            out.append(summary["mismatch"])
    return out


class AetherMonitor:
    """Polls one aetherd control API on a daemon thread."""

    def __init__(
        self,
        instance_name: str,
        client: AetherControlClient,
        event_bus: EventBus,
        interval: float = POLL_INTERVAL_S,
        bbs_callsign=None,
    ):
        self._name = instance_name
        # str, or a callable giving the BBS callsign as it is now
        self._bbs_callsign = bbs_callsign
        self._client = client
        self._bus = event_bus
        self._interval = interval
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_problems: list[str] = []
        self._seen_once: list[str] = []
        self._reachable: Optional[bool] = None
        self._heard_seen: dict[str, int] = {}
        self._heard_primed = False
        self._polls = 0
        self.last_summary: dict = {}

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name=f"AetherMon-{self._name}", daemon=True,
        )
        self._thread.start()

    def stop(self):
        self._stop.set()

    def poll_once(self) -> dict:
        """One status poll.  Returns the summary emitted."""
        try:
            summary = summarize_status(self._client.status())
        except AetherControlError as e:
            summary = {"reachable": False, "error": str(e)}
            if self._reachable is not False:
                log.warning(f"[{self._name}] {e}")
            self._reachable = False
        else:
            if self._reachable is not True:
                log.info(
                    f"[{self._name}] Aether HF {summary.get('version') or ''} "
                    f"control API up — {summary.get('callsign') or '?'}, "
                    f"{summary.get('bandwidth_hz') or '?'} Hz"
                )
            self._reachable = True
            call = self._bbs_callsign
            if callable(call):
                call = call()
            found = problems(summary, call or "")
            settled = [p for p in found
                       if not p.startswith(_SETTLING) or p in self._seen_once
                       or p in self._last_problems]
            self._seen_once = found
            self._report_problems(settled)

        summary["problems"] = list(self._last_problems) if summary.get("reachable") else []
        self.last_summary = summary
        self._bus.emit(EventType.AETHER_STATUS, source=self._name, data=summary)
        return summary

    def poll_heard(self):
        """Forward stations Aether heard since the last call to MHeard."""
        try:
            stations = self._client.heard()
        except AetherControlError:
            return
        self._bus.emit(EventType.AETHER_HEARD, source=self._name,
                       data={"stations": heard_rows(stations)})
        for st in stations:
            call = str(st.get("callsign", "")).upper()
            last = int(st.get("last_heard_ms") or 0)
            if not call or self._heard_seen.get(call) == last:
                continue
            self._heard_seen[call] = last
            if not self._heard_primed:
                continue   # don't replay Aether's whole history at start-up
            base, _, ssid = call.partition("-")
            self._bus.emit(
                EventType.STATION_HEARD,
                source=self._name,
                data={
                    "callsign": base,
                    "ssid": int(ssid) if ssid.isdigit() else 0,
                    "via_path": "",
                    "last_event": str(st.get("activity", "HEARD")).upper(),
                },
            )
        self._heard_primed = True

    def _report_problems(self, current: list[str]):
        for p in current:
            if p not in self._last_problems:
                log.warning(f"[{self._name}] Aether HF: {p}")
        for p in self._last_problems:
            if p not in current:
                log.info(f"[{self._name}] Aether HF: resolved — {p}")
        self._last_problems = current

    def _run(self):
        while not self._stop.is_set():
            self._polls += 1
            summary = self.poll_once()
            if summary.get("reachable") and (
                    not self._heard_primed
                    or self._polls % HEARD_EVERY_N_POLLS == 0):
                self.poll_heard()
            self._stop.wait(self._interval)
