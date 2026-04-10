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

    def update_config(self, new_config: AppConfig):
        """Update config and restart instances whose settings changed."""
        old_config = self._config
        self._config = new_config

        for inst_cfg in new_config.instances:
            name = inst_cfg.name
            status = self._instances.get(name)
            if not status:
                continue
            old_cfg = status.cfg

            # Check if rig_control (scan channels) changed
            rig_changed = (
                inst_cfg.rig_control != old_cfg.rig_control
            )
            # Check if transport/ports/exe changed
            transport_changed = (
                inst_cfg.transport != old_cfg.transport
                or inst_cfg.cmd_port != old_cfg.cmd_port
                or inst_cfg.data_port != old_cfg.data_port
                or inst_cfg.executable_path != old_cfg.executable_path
                or inst_cfg.soundmodem != old_cfg.soundmodem
            )

            # Update the stored config reference
            status.cfg = inst_cfg

            if rig_changed or transport_changed:
                log.info(
                    f"[{name}] Config changed "
                    f"({'rig_control' if rig_changed else 'transport'}) "
                    f"— restarting instance"
                )
                threading.Thread(
                    target=self.restart_instance,
                    args=(name,),
                    daemon=True,
                    name=f"InstRestart-{name}",
                ).start()

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
        log.info(f"Starting instance '{cfg.name}' ({cfg.frequency_label}) "
                 f"transport={cfg.transport}")

        # Soundmodem instances use a different broker
        if cfg.is_soundmodem:
            self._start_soundmodem_instance(status)
            return

        # AETHER HF instances use the AETHER modem broker
        if cfg.is_aether:
            self._start_aether_instance(status)
            return

        self._start_vara_instance(status)

    def _start_soundmodem_instance(self, status: InstanceStatus):
        """Start a Soundmodem/AX.25 instance via AGWPE or KISS broker."""
        cfg = status.cfg
        sm = cfg.soundmodem
        protocol = sm.protocol.upper()

        if protocol == "AGWPE":
            from transport.agwpe_broker import AGWPEBroker
            mux = AGWPEBroker(cfg, self._config, self._bus)
        else:
            log.error(f"[{cfg.name}] KISS transport not yet implemented")
            return

        status.mux = mux
        threading.Thread(
            target=mux.connect,
            name=f"SmConnect-{cfg.name}",
            daemon=True,
        ).start()

    def _find_aether_module(self) -> str:
        """Locate the AETHER HF modem module.

        Checks (in order):
          1. Bundled submodule: aether_hf_modem/aether_hf/
          2. Installed as a Python package (importable)

        Returns the path to the aether_hf package directory, or "" if not found.
        """
        import os, importlib

        # Check bundled submodule (relative to the BBS root)
        bbs_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        submodule = os.path.join(bbs_root, "aether_hf_modem", "aether_hf")
        if os.path.isdir(submodule) and os.path.isfile(
                os.path.join(submodule, "__init__.py")):
            return submodule

        # Check if installed as a pip package
        try:
            spec = importlib.util.find_spec("aether_hf")
            if spec and spec.origin:
                return os.path.dirname(spec.origin)
        except (ModuleNotFoundError, ValueError):
            pass

        return ""

    def _start_aether_instance(self, status: InstanceStatus):
        """Start an AETHER HF modem instance.

        AETHER HF exposes a VARA-compatible TCP interface, so from the
        BBS dispatcher's perspective it behaves identically to VARA.

        If auto_launch is True and the AETHER module is found (bundled
        submodule or pip-installed), the modem process is started
        automatically.  Otherwise, the BBS connects to an externally
        running AETHER instance on the configured TCP ports.
        """
        import subprocess, os
        cfg = status.cfg
        acfg = cfg.aether

        # ── Check if AETHER is available ──────────────────────────────
        aether_path = self._find_aether_module()
        if not aether_path:
            log.error(
                f"[{cfg.name}] AETHER HF modem not found. "
                f"Initialize the git submodule:\n"
                f"  git submodule update --init\n"
                f"Or install via pip:\n"
                f"  pip install aether-hf\n"
                f"Instance will attempt to connect to an external AETHER "
                f"process on ports {acfg.cmd_port}/{acfg.data_port}."
            )
            self._bus.emit(
                EventType.VARA_PROCESS_DOWN, source=cfg.name,
                data={"error": "AETHER HF modem not installed. "
                      "Run: git submodule update --init"},
            )
        else:
            log.info(f"[{cfg.name}] AETHER HF found at {aether_path}")

        # ── Auto-launch if configured ─────────────────────────────────
        if acfg.auto_launch and aether_path:
            # Check if already running on the expected ports
            if self._port_is_open(cfg.host, acfg.cmd_port):
                log.info(
                    f"[{cfg.name}] AETHER already running on "
                    f"port {acfg.cmd_port} — using external instance"
                )
            else:
                # Launch the AETHER modem as a subprocess
                modem_root = os.path.dirname(aether_path)
                cmd = [
                    "python", "-m", "aether_hf.host.vara_compat",
                    "--cmd-port", str(acfg.cmd_port),
                    "--data-port", str(acfg.data_port),
                ]
                if acfg.my_callsign:
                    cmd.extend(["--callsign", acfg.my_callsign])
                if acfg.mode:
                    cmd.extend(["--mode", acfg.mode])

                try:
                    log.info(f"[{cfg.name}] Launching AETHER: {' '.join(cmd)}")
                    proc = subprocess.Popen(
                        cmd, cwd=modem_root,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    status.process_state = ProcessState.RUNNING
                    self._bus.emit(
                        EventType.VARA_PROCESS_UP, source=cfg.name,
                        data={"pid": proc.pid, "transport": "AETHER_HF"},
                    )
                    time.sleep(2)  # give it time to bind ports
                except Exception as e:
                    log.error(f"[{cfg.name}] Failed to launch AETHER: {e}")
                    self._bus.emit(
                        EventType.VARA_PROCESS_DOWN, source=cfg.name,
                        data={"error": str(e)},
                    )

        # ── Connect to AETHER's VARA-compatible TCP ports ─────────────
        log.info(f"[{cfg.name}] Connecting to AETHER HF on "
                 f"cmd:{acfg.cmd_port} data:{acfg.data_port}")

        from copy import deepcopy
        aether_cfg = deepcopy(cfg)
        aether_cfg.cmd_port = acfg.cmd_port
        aether_cfg.data_port = acfg.data_port

        mux = VARAFMMux(aether_cfg, self._config, self._bus)
        status.mux = mux

        threading.Thread(
            target=mux.connect,
            name=f"AetherConnect-{cfg.name}",
            daemon=True,
        ).start()

    def _start_vara_instance(self, status: InstanceStatus):
        """Start a VARA FM/HF instance."""
        cfg = status.cfg

        from vara.interface import VARAFMState

        # If a mux already exists, check its state
        if status.mux is not None:
            if status.mux.state != VARAFMState.DISCONNECTED:
                log.info(f"[{cfg.name}] Already connected, skipping restart")
                return
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
