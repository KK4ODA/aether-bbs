"""
vara/process.py

Manages the lifecycle of a single VARAFM.exe or VARAHF.exe process:
  - start / stop / restart
  - crash detection and auto-restart
  - state reporting via EventBus
"""

import time
import logging
import threading
import subprocess
from enum import Enum, auto
from pathlib import Path
from typing import Optional, Callable

from core.config import InstanceConfig
from core.events import EventBus, EventType

log = logging.getLogger(__name__)


class ProcessState(Enum):
    STOPPED   = auto()
    STARTING  = auto()
    RUNNING   = auto()
    CRASHED   = auto()
    STOPPING  = auto()


class VARAFMProcess:
    """
    Supervises a single VARAFM.exe process.
    Emits VARA_PROCESS_UP / VARA_PROCESS_DOWN events.
    """

    def __init__(
        self,
        cfg: InstanceConfig,
        event_bus: EventBus,
        on_state_change: Optional[Callable[[ProcessState], None]] = None,
    ):
        self.cfg = cfg
        self._bus = event_bus
        self._on_state_change = on_state_change
        self._proc: Optional[subprocess.Popen] = None
        self._monitor_thread: Optional[threading.Thread] = None
        self._stop_requested = threading.Event()
        self._state = ProcessState.STOPPED
        self._lock = threading.Lock()

    # ── Public API ────────────────────────────────────────────────────────

    @property
    def state(self) -> ProcessState:
        return self._state

    def start(self) -> bool:
        """Launch VARAFM.exe. Returns True if launched successfully."""
        with self._lock:
            if self._state in (ProcessState.RUNNING, ProcessState.STARTING):
                log.warning(f"[{self.cfg.name}] start() called but already {self._state.name}")
                return False

            exe_path = self.cfg.executable_path
            if not exe_path:
                # Auto-detect default install location based on mode
                if getattr(self.cfg, "is_hf", False):
                    exe_path = r"C:\VARA HF\VARA HF.exe"
                else:
                    exe_path = r"C:\VARA FM\VARA FM.exe"
            exe = Path(exe_path)
            mode_str = "VARA HF" if getattr(self.cfg, "is_hf", False) else "VARA FM"
            if not exe.exists():
                log.error(f"[{self.cfg.name}] {mode_str} executable not found: {exe}")
                return False

            self._stop_requested.clear()
            self._set_state(ProcessState.STARTING)

            try:
                self._proc = subprocess.Popen(
                    [str(exe)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                log.info(f"[{self.cfg.name}] {mode_str} started (PID {self._proc.pid})")
                self._set_state(ProcessState.RUNNING)
                self._bus.emit(EventType.VARA_PROCESS_UP, source=self.cfg.name,
                               data={"pid": self._proc.pid})
            except OSError as e:
                log.error(f"[{self.cfg.name}] Failed to start {mode_str}: {e}")
                self._set_state(ProcessState.STOPPED)
                return False

            self._monitor_thread = threading.Thread(
                target=self._monitor,
                name=f"ProcMon-{self.cfg.name}",
                daemon=True,
            )
            self._monitor_thread.start()
            return True

    def stop(self):
        """Gracefully stop VARAFM.exe."""
        self._stop_requested.set()
        with self._lock:
            if self._proc and self._proc.poll() is None:
                self._set_state(ProcessState.STOPPING)
                log.info(f"[{self.cfg.name}] Stopping VARA process (PID {self._proc.pid})")
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    log.warning(f"[{self.cfg.name}] VARA process did not exit cleanly; killing")
                    self._proc.kill()
        self._set_state(ProcessState.STOPPED)
        self._bus.emit(EventType.VARA_PROCESS_DOWN, source=self.cfg.name,
                       data={"reason": "stopped"})

    def is_running(self) -> bool:
        return self._state == ProcessState.RUNNING

    # ── Internal ──────────────────────────────────────────────────────────

    def _monitor(self):
        """Block until process exits, then handle restart logic."""
        if self._proc is None:
            return
        ret = self._proc.wait()   # blocks until exit

        if self._stop_requested.is_set():
            return  # intentional stop, already handled

        log.warning(f"[{self.cfg.name}] VARA process exited unexpectedly (rc={ret})")
        self._set_state(ProcessState.CRASHED)
        self._bus.emit(EventType.VARA_PROCESS_DOWN, source=self.cfg.name,
                       data={"reason": "crash", "returncode": ret})

        if self.cfg.auto_restart:
            log.info(
                f"[{self.cfg.name}] Auto-restart in {self.cfg.restart_delay}s"
            )
            time.sleep(self.cfg.restart_delay)
            if not self._stop_requested.is_set():
                self.start()

    def _set_state(self, state: ProcessState):
        self._state = state
        if self._on_state_change:
            try:
                self._on_state_change(state)
            except Exception as e:
                log.error(f"[{self.cfg.name}] on_state_change error: {e}")
