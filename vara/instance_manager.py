"""
vara/instance_manager.py

Orchestrates all configured VARA FM instances:
  - Validates port conflicts before starting
  - Launches VARA FM processes with staggered startup
  - Maintains per-instance VARAFMMux objects
  - Provides unified status reporting
"""

import time
import socket
import logging
import threading
from typing import Optional

from core.config import AppConfig, InstanceConfig
from core.events import EventBus, EventType
from vara.process import VARAFMProcess, ProcessState
from vara.broker import TNCBroker as VARAFMMux

log = logging.getLogger(__name__)

STARTUP_STAGGER_SECS = 2.5


class InstanceStatus:
    def __init__(self, cfg: InstanceConfig):
        self.cfg          = cfg
        self.process_state: ProcessState = ProcessState.STOPPED
        self.mux:           Optional[VARAFMMux] = None
        self.process:       Optional[VARAFMProcess] = None
        self.bytes_tx:      int = 0
        self.bytes_rx:      int = 0
        self.sessions_total: int = 0
        self.current_call:  Optional[str] = None


class PortConflictError(Exception):
    pass


class VARAFMInstanceManager:
    """
    Manages all enabled VARA FM instances.
    Each instance has:
      - A VARAFMProcess (VARAFM.exe lifecycle)
      - A VARAFMMux (TCP session arbitration)
    """

    def __init__(self, config: AppConfig, event_bus: EventBus):
        self._config = config
        self._bus = event_bus
        self._instances: dict[str, InstanceStatus] = {}
        self._lock = threading.Lock()

        self._bus.subscribe(EventType.SESSION_CONNECTED,    self._on_connected)
        self._bus.subscribe(EventType.SESSION_DISCONNECTED, self._on_disconnected)
        self._bus.subscribe(EventType.VARA_PROCESS_UP,      self._on_process_up)
        self._bus.subscribe(EventType.VARA_PROCESS_DOWN,    self._on_process_down)

        # Build instance status objects for enabled instances
        for inst_cfg in config.instances:
            if inst_cfg.enabled:
                self._instances[inst_cfg.name] = InstanceStatus(inst_cfg)

    # ── Lifecycle ─────────────────────────────────────────────────────────

    def start_all(self):
        self._check_port_conflicts()
        enabled = list(self._instances.values())
        log.info(f"Starting {len(enabled)} VARA FM instance(s)")

        for i, status in enumerate(enabled):
            self._start_instance(status)
            if i < len(enabled) - 1:
                log.debug(f"Staggering next instance start by {STARTUP_STAGGER_SECS}s")
                time.sleep(STARTUP_STAGGER_SECS)

    def stop_all(self):
        log.info("Stopping all VARA FM instances")
        for status in self._instances.values():
            self._stop_instance(status)

    def start_instance(self, name: str):
        """Start a single instance by name."""
        status = self._instances.get(name)
        if status:
            self._start_instance(status)
        else:
            log.warning(f"start_instance: unknown instance '{name}'")

    def stop_instance(self, name: str):
        """Stop a single instance by name."""
        status = self._instances.get(name)
        if status:
            self._stop_instance(status)

    def restart_instance(self, name: str):
        """Restart a single instance."""
        self.stop_instance(name)
        time.sleep(2)
        self.start_instance(name)

    # ── Access ────────────────────────────────────────────────────────────

    def get_mux(self, name: str) -> Optional[VARAFMMux]:
        status = self._instances.get(name)
        return status.mux if status else None

    def all_muxes(self) -> dict[str, VARAFMMux]:
        return {
            name: s.mux
            for name, s in self._instances.items()
            if s.mux is not None
        }

    def status_all(self) -> dict[str, InstanceStatus]:
        return dict(self._instances)

    def get_status(self, name: str) -> Optional[InstanceStatus]:
        return self._instances.get(name)

    def instance_names(self) -> list[str]:
        return list(self._instances.keys())

    # ── Internal ──────────────────────────────────────────────────────────

    def _port_is_open(self, host: str, port: int, timeout: float = 0.5) -> bool:
        """Return True if something is already listening on host:port."""
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            return False

    def _start_instance(self, status: InstanceStatus):
        cfg = status.cfg
        log.info(f"Starting instance '{cfg.name}' ({cfg.frequency_label})")

        from vara.interface import VARAFMState

        # If a mux already exists, check its state
        if status.mux is not None:
            if status.mux.state != VARAFMState.DISCONNECTED:
                # Already connected — nothing to do
                log.info(f"[{cfg.name}] Already connected, skipping restart")
                return
            # Mux exists but TCP is down — re-activate it and reconnect in a
            # background thread WITHOUT destroying it (destroying it would close
            # the socket that the simulator is already talking to via its
            # accept'd client)
            log.info(f"[{cfg.name}] Mux disconnected, reconnecting")
            status.mux.resume()
            threading.Thread(
                target=status.mux.connect,
                name=f"MuxConnect-{cfg.name}",
                daemon=True,
            ).start()
            return

        # Create mux
        mux = VARAFMMux(cfg, self._config, self._bus)
        status.mux = mux

        # Create and start process if auto_start is enabled
        if cfg.auto_start and cfg.executable_path:
            already_up = self._port_is_open(cfg.host, cfg.cmd_port)
            if already_up:
                log.info(
                    f"[{cfg.name}] Port {cfg.cmd_port} already in use — "
                    f"skipping VARA FM launch (using existing instance or simulator)"
                )
            else:
                proc = VARAFMProcess(
                    cfg,
                    self._bus,
                    on_state_change=lambda s, n=cfg.name: self._on_proc_state(n, s),
                )
                status.process = proc
                proc.start()
                # Give VARA FM time to open its TCP ports
                time.sleep(3.0)
                # Verify the modem actually bound to the expected port
                if not self._port_is_open(cfg.host, cfg.cmd_port, timeout=2.0):
                    log.error(
                        f"[{cfg.name}] VARA process started but port {cfg.cmd_port} "
                        f"is not open after 3 s. Verify that VARA's internal port "
                        f"settings match the BBS config "
                        f"(expected cmd:{cfg.cmd_port} data:{cfg.data_port})."
                    )
                else:
                    log.info(
                        f"[{cfg.name}] VARA process confirmed on "
                        f"cmd:{cfg.cmd_port} data:{cfg.data_port}"
                    )

        # Connect mux to VARA FM TCP in a background thread
        threading.Thread(
            target=mux.connect,
            name=f"MuxConnect-{cfg.name}",
            daemon=True,
        ).start()

    def _stop_instance(self, status: InstanceStatus):
        cfg = status.cfg
        log.info(f"Stopping instance '{cfg.name}'")
        if status.mux:
            status.mux.disconnect()
            status.mux = None   # force fresh mux on next start
        if status.process:
            status.process.stop()
            status.process = None

    def _on_proc_state(self, name: str, state: ProcessState):
        with self._lock:
            if name in self._instances:
                self._instances[name].process_state = state

    def _check_port_conflicts(self):
        seen: set[int] = set()
        for status in self._instances.values():
            for port in (status.cfg.cmd_port, status.cfg.data_port):
                if port in seen:
                    raise PortConflictError(
                        f"Port {port} is used by more than one enabled instance"
                    )
                seen.add(port)

    def _on_connected(self, event):
        with self._lock:
            s = self._instances.get(event.source)
            if s:
                s.current_call = event.data.get("remote_call")
                s.sessions_total += 1

    def _on_disconnected(self, event):
        with self._lock:
            s = self._instances.get(event.source)
            if s:
                s.current_call = None

    def _on_process_up(self, event):
        with self._lock:
            s = self._instances.get(event.source)
            if s:
                s.process_state = ProcessState.RUNNING

    def _on_process_down(self, event):
        with self._lock:
            s = self._instances.get(event.source)
            if s:
                s.process_state = ProcessState.CRASHED
