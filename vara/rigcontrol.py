"""
vara/rigcontrol.py

OmniRig-based rig controller for VARA HF instances.

Provides:
  - Set radio frequency and mode via OmniRig COM server
  - Multi-channel frequency scanning with per-channel dwell time
  - Time-of-day channel filtering (UTC)
  - Session-aware pause/resume (holds frequency during connections)
  - Event bus integration (SCAN_FREQ_CHANGED notifications)

The RigController delegates all OmniRig communication to an
OmniRigClient (vara/omnirig.py) which owns the single COM thread.
When both PTT and rig control use OmniRig, they share the same client
instance — one Dispatch, one COM thread, no conflicts.

Usage:
    from vara.rigcontrol import RigController
    rc = RigController(instance_name, rig_control_cfg, event_bus, client)
    rc.start()       # begin scanning
    rc.pause()       # hold current frequency (session connected)
    rc.resume()      # resume scanning (session ended)
    rc.stop()        # stop scanning

References:
    OmniRig:     http://www.dxatlas.com/omnirig/
    omnipyrig:   https://github.com/4Z1KD/omnipyrig
"""

import time
import logging
import threading
from datetime import datetime, timezone

from vara.omnirig import OmniRigClient, OMNIRIG_MODES

log = logging.getLogger(__name__)


def _hour_in_range(hour: int, hours_str: str) -> bool:
    """Check if a UTC hour (0-23) falls within an 'HH-HH' range.

    Supports wrap-around ranges like '22-06' (10 PM to 6 AM).
    '00-24' means always active.
    """
    try:
        parts = hours_str.split("-")
        start = int(parts[0])
        end = int(parts[1])
    except (ValueError, IndexError):
        return True  # malformed -> always active

    if start == 0 and end == 24:
        return True
    if start <= end:
        # Simple range: 08-18 means 08:00 to 17:59
        return start <= hour < end
    else:
        # Wrap-around: 22-06 means 22:00-23:59 or 00:00-05:59
        return hour >= start or hour < end


class RigController:
    """
    Manages radio frequency/mode via OmniRig and runs a scan loop.

    The scan loop iterates through configured channels, setting the
    radio frequency and dwelling for the configured time.  Scanning
    pauses when a VARA session connects and resumes when it ends.

    All OmniRig communication is delegated to the OmniRigClient,
    which processes commands on its own COM thread.
    """

    def __init__(self, instance_name: str, rig_cfg, event_bus,
                 omnirig_client: OmniRigClient = None):
        self._name = instance_name
        self._cfg = rig_cfg
        self._bus = event_bus
        self._client = omnirig_client  # shared OmniRigClient

        self._scan_thread = None
        self._stop_event = threading.Event()
        self._pause_event = threading.Event()  # set = paused
        self._resume_event = threading.Event()  # set = running
        self._resume_event.set()  # start in running state

        self._current_freq_khz = 0.0
        self._started = False
        self._owns_client = False  # True if we created our own client

    # -- Public API --------------------------------------------------------

    def start(self) -> None:
        """Begin scanning (if channels configured)."""
        if self._started:
            log.debug(f"[{self._name}] Rig controller already started, skipping")
            return
        if self._cfg.method.upper() != "OMNIRIG":
            log.info(f"[{self._name}] Rig control method is {self._cfg.method}, "
                     f"skipping rig controller")
            return
        if not self._cfg.channels:
            log.info(f"[{self._name}] No scan channels configured, "
                     f"rig controller inactive")
            return

        # Ensure we have an OmniRig client
        if self._client is None:
            self._client = OmniRigClient(
                rig_number=self._cfg.omnirig_rig
            )
            self._owns_client = True
        if not self._client.is_started:
            try:
                self._client.start()
            except RuntimeError as e:
                log.error(f"[{self._name}] OmniRig client failed: {e}")
                return

        self._started = True
        self._stop_event.clear()
        self._pause_event.clear()
        self._resume_event.set()

        log.info(
            f"[{self._name}] Starting scan thread: OmniRig Rig"
            f"{self._cfg.omnirig_rig}, {len(self._cfg.channels)} channels, "
            f"mode={self._cfg.mode}"
        )
        self._scan_thread = threading.Thread(
            target=self._scan_loop,
            name=f"RigScan-{self._name}",
            daemon=True,
        )
        self._scan_thread.start()

    def stop(self) -> None:
        """Stop scanning."""
        if not self._started:
            return
        self._stop_event.set()
        self._resume_event.set()  # unblock if paused
        if self._scan_thread and self._scan_thread.is_alive():
            self._scan_thread.join(timeout=5.0)
        # Only stop the client if we created it ourselves
        if self._owns_client and self._client:
            self._client.stop()
            self._client = None
        self._started = False
        log.info(f"[{self._name}] Rig controller stopped")

    def pause(self) -> None:
        """Pause scanning -- hold current frequency (session active)."""
        if not self._started:
            return
        self._pause_event.set()
        self._resume_event.clear()
        log.info(f"[{self._name}] Scan paused -- holding "
                 f"{self._current_freq_khz:.1f} kHz")

    def resume(self) -> None:
        """Resume scanning after session ends."""
        if not self._started:
            return
        self._pause_event.clear()
        self._resume_event.set()
        log.info(f"[{self._name}] Scan resumed")

    @property
    def current_freq_khz(self) -> float:
        return self._current_freq_khz

    def get_active_channels(self) -> list:
        """Return all configured channels (for APRS beacon reporting)."""
        return list(self._cfg.channels)

    # -- Frequency / mode helpers ------------------------------------------

    def _set_frequency(self, freq_khz: float) -> bool:
        """Set radio frequency via OmniRigClient.  Returns True on success."""
        if not self._client:
            return False
        freq_hz = int(freq_khz * 1000)
        ok = self._client.set_freq(freq_hz)
        if ok:
            self._current_freq_khz = freq_khz
            log.debug(f"[{self._name}] Set FreqA = {freq_hz} Hz "
                      f"({freq_khz:.1f} kHz)")
        else:
            log.error(f"[{self._name}] Failed to set frequency "
                      f"{freq_khz:.1f} kHz")
        return ok

    def _set_mode(self, mode_str: str) -> bool:
        """Set radio mode via OmniRigClient.  Returns True on success."""
        if not self._client:
            return False
        ok = self._client.set_mode_by_name(mode_str)
        if ok:
            log.debug(f"[{self._name}] Set mode = {mode_str}")
        else:
            log.error(f"[{self._name}] Failed to set mode {mode_str}")
        return ok

    # -- Scan loop ---------------------------------------------------------

    def _get_active_channels(self):
        """Filter channels by current UTC hour."""
        now_utc = datetime.now(timezone.utc)
        current_hour = now_utc.hour
        return [ch for ch in self._cfg.channels
                if _hour_in_range(current_hour, ch.hours)]

    def _scan_loop(self) -> None:
        """Main scan thread -- cycles through channels with dwell time."""
        log.info(f"[{self._name}] Scan loop started")

        # Set initial mode
        self._set_mode(self._cfg.mode)

        channel_index = 0
        consecutive_errors = 0
        first_cycle = True
        MAX_ERRORS = 5
        log.info(f"[{self._name}] Entering scan loop with "
                 f"{len(self._cfg.channels)} configured channels")

        try:
            while not self._stop_event.is_set():
                # Wait if paused (session active)
                if self._pause_event.is_set():
                    self._resume_event.wait(timeout=1.0)
                    if self._stop_event.is_set():
                        break
                    continue

                # Get currently active channels
                active = self._get_active_channels()
                if not active:
                    log.debug(f"[{self._name}] No channels active at "
                              f"current UTC hour, waiting")
                    self._stop_event.wait(timeout=5.0)
                    continue

                # Wrap index
                if channel_index >= len(active):
                    channel_index = 0

                ch = active[channel_index]

                # Set frequency and re-assert mode every cycle
                if self._set_frequency(ch.freq_khz):
                    self._set_mode(self._cfg.mode)
                    consecutive_errors = 0

                    lvl = logging.INFO if first_cycle else logging.DEBUG
                    log.log(
                        lvl,
                        f"[{self._name}] Scan -> {ch.freq_khz:.1f} kHz "
                        f"(dwell {ch.dwell_seconds}s, "
                        f"channel {channel_index + 1}/{len(active)})"
                    )
                    if channel_index + 1 >= len(active):
                        first_cycle = False

                    # Notify listeners (GUI scan indicator, etc.)
                    try:
                        from core.events import EventType
                        self._bus.emit(
                            EventType.SCAN_FREQ_CHANGED,
                            source=self._name,
                            data={
                                "freq_khz": ch.freq_khz,
                                "channel_index": channel_index,
                                "total_channels": len(active),
                            },
                        )
                    except Exception:
                        pass
                else:
                    consecutive_errors += 1
                    log.warning(
                        f"[{self._name}] Freq set failed "
                        f"({consecutive_errors}/{MAX_ERRORS})"
                    )
                    if consecutive_errors >= MAX_ERRORS:
                        log.error(
                            f"[{self._name}] Too many OmniRig errors -- "
                            f"backing off 10s"
                        )
                        self._stop_event.wait(timeout=10.0)
                        consecutive_errors = 0

                # Dwell -- sleep in small increments so we can respond
                # to pause/stop quickly
                dwell_end = time.monotonic() + ch.dwell_seconds
                while time.monotonic() < dwell_end:
                    if self._stop_event.is_set() or self._pause_event.is_set():
                        break
                    self._stop_event.wait(timeout=0.25)

                channel_index += 1

        except Exception as e:
            log.error(f"[{self._name}] Scan loop error: {e}", exc_info=True)
        finally:
            self._started = False
            log.info(f"[{self._name}] Scan loop exited")
