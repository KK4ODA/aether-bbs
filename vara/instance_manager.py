"""
vara/instance_manager.py

Orchestrates all configured modem instances (VARA FM/HF, Soundmodem,
Aether HF):
  - Validates port conflicts before starting
  - Launches modem processes with staggered startup
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
        self.subprocess:    Optional[object] = None   # raw Popen (Soundmodem, Aether HF)
        self.aether_client: Optional[object] = None   # AetherControlClient
        self.aether_monitor: Optional[object] = None  # AetherMonitor
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
        log.info(f"Starting {len(enabled)} modem instance(s)")

        for i, status in enumerate(enabled):
            self._start_instance(status)
            if i < len(enabled) - 1:
                log.debug(f"Staggering next instance start by {STARTUP_STAGGER_SECS}s")
                time.sleep(STARTUP_STAGGER_SECS)

    def stop_all(self):
        log.info("Stopping all modem instances")
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
                or inst_cfg.aether != old_cfg.aether
                or inst_cfg.host != old_cfg.host
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

        # Aether HF: VARA-compatible host interface + control API
        if cfg.is_aether:
            self._start_aether_instance(status)
            return

        self._start_vara_instance(status)

    def _start_soundmodem_instance(self, status: InstanceStatus):
        """Start a Soundmodem/AX.25 instance via AGWPE or KISS broker.

        If the soundmodem config has ``auto_launch=True`` and a valid
        ``exe_path``, the executable is started before connecting.
        """
        cfg = status.cfg
        sm = cfg.soundmodem
        protocol = sm.protocol.upper()

        # Auto-launch soundmodem executable if configured
        if sm.auto_launch and sm.exe_path:
            import os, subprocess
            if os.path.isfile(sm.exe_path):
                try:
                    proc = subprocess.Popen([sm.exe_path], shell=False)
                    status.subprocess = proc
                    log.info(
                        f"[{cfg.name}] Soundmodem started: {sm.exe_path} "
                        f"(PID {proc.pid})"
                    )
                    status.process_state = ProcessState.RUNNING
                    self._bus.emit(
                        EventType.VARA_PROCESS_UP, source=cfg.name,
                        data={"pid": proc.pid, "exe": sm.exe_path},
                    )
                    # Give the process time to open its AGWPE port
                    import time
                    time.sleep(3.0)
                except Exception as e:
                    log.error(
                        f"[{cfg.name}] Failed to launch soundmodem: {e}"
                    )
            else:
                log.warning(
                    f"[{cfg.name}] Soundmodem exe not found: {sm.exe_path}"
                )

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

    def _start_aether_instance(self, status: InstanceStatus):
        """Start an Aether HF instance.

        Aether HF's daemon (aetherd) answers on a VARA-compatible host
        interface — the instance's cmd_port / data_port, 8300/8301 unless
        changed — so BBS sessions run through the same TNCBroker as VARA HF.
        Alongside it, the control API (aether.control_port, 8515) is polled
        for the modem's state, faults and heard stations.

        With auto_start and an executable path (or an Aether HF found where
        its installer puts it), the BBS starts Aether HF when nothing is
        listening on the host port yet.
        """
        import subprocess
        from aether.control import AetherControlClient
        from aether.launcher import build_command, find_aether_executable
        from aether.monitor import AetherMonitor
        from core.crypto import decrypt_value
        from vara.interface import VARAFMState

        cfg = status.cfg
        acfg = cfg.aether

        if status.mux is not None:
            if status.mux.state != VARAFMState.DISCONNECTED:
                log.info(f"[{cfg.name}] Already attached to Aether HF, skipping start")
                return
            status.mux.disconnect()
            status.mux = None
        if status.aether_monitor:
            status.aether_monitor.stop()
            status.aether_monitor = None

        # ── Control API client and monitor ───────────────────────────
        token = ""
        if acfg.control_token:
            try:
                token = decrypt_value(acfg.control_token,
                                      self._config.security.machine_secret)
            except Exception as e:
                log.error(f"[{cfg.name}] Could not decrypt Aether control token: {e}")
        client = AetherControlClient(cfg.host, acfg.control_port, token)
        status.aether_client = client

        # ── Auto-launch ──────────────────────────────────────────────
        # Whether Aether is up is asked of the control API, never by opening
        # the host port: aetherd takes one host at a time, and a probe
        # connection could make it refuse the BBS's own attach with WRONG.
        running = client.ping()
        if cfg.auto_start and not running:
            exe = cfg.executable_path or find_aether_executable()
            if not exe:
                log.error(
                    f"[{cfg.name}] Aether HF is not installed (or its path is "
                    f"not set). Install it from "
                    f"https://github.com/KK4ODA/aether-hf/releases and set "
                    f"the executable path in Settings → Instances."
                )
                self._bus.emit(
                    EventType.VARA_PROCESS_DOWN, source=cfg.name,
                    data={"error": "Aether HF not installed"},
                )
            else:
                cmd = build_command(exe, acfg.config_file)
                try:
                    log.info(f"[{cfg.name}] Launching Aether HF: {' '.join(cmd)}")
                    proc = subprocess.Popen(
                        cmd,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    status.subprocess = proc
                    status.process_state = ProcessState.RUNNING
                    self._bus.emit(
                        EventType.VARA_PROCESS_UP, source=cfg.name,
                        data={"pid": proc.pid, "exe": exe,
                              "transport": "AETHER_HF"},
                    )
                    # The daemon is up within a few seconds (the desktop app
                    # starts it after its window opens).
                    for _ in range(30):
                        if client.ping():
                            break
                        time.sleep(0.5)
                    else:
                        log.warning(
                            f"[{cfg.name}] Aether HF started but its control "
                            f"API (port {acfg.control_port}) is not answering "
                            f"yet; the BBS will keep trying to attach."
                        )
                except Exception as e:
                    log.error(f"[{cfg.name}] Failed to launch Aether HF: {e}")
                    self._bus.emit(
                        EventType.VARA_PROCESS_DOWN, source=cfg.name,
                        data={"error": str(e)},
                    )
        elif running:
            status.process_state = ProcessState.RUNNING
            self._bus.emit(
                EventType.VARA_PROCESS_UP, source=cfg.name,
                data={"transport": "AETHER_HF", "external": True},
            )

        if acfg.monitor:
            monitor = AetherMonitor(cfg.name, client, self._bus)
            status.aether_monitor = monitor
            monitor.start()

        # ── Attach to the VARA-compatible host interface ─────────────
        log.info(f"[{cfg.name}] Connecting to Aether HF host interface on "
                 f"{cfg.host} cmd:{cfg.cmd_port} data:{cfg.data_port}")
        mux = VARAFMMux(cfg, self._config, self._bus)
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
        if status.aether_monitor:
            status.aether_monitor.stop()
            status.aether_monitor = None
        if (status.aether_client and status.subprocess
                and status.subprocess.poll() is None):
            # An Aether HF the BBS started: let the daemon end any session on
            # the air (its DISC and identifier) before the process goes.
            self._shutdown_aether(cfg.name, status.aether_client)
        status.aether_client = None
        if status.subprocess:
            try:
                import os, subprocess as _sp
                pid = status.subprocess.pid
                if status.subprocess.poll() is None:
                    log.info(f"[{cfg.name}] Terminating modem process PID {pid}")
                    if os.name == "nt":
                        _sp.call(["taskkill", "/F", "/T", "/PID", str(pid)],
                                 stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)
                    else:
                        status.subprocess.terminate()
                    status.subprocess.wait(timeout=5)
            except Exception as e:
                log.error(f"[{cfg.name}] Error stopping modem process: {e}")
            status.subprocess = None

    @staticmethod
    def _shutdown_aether(name: str, client, wait_s: float = 12.0):
        """Ask aetherd to stop via its control API and wait for it to go."""
        from aether.control import AetherControlError
        try:
            client.call("shutdown")
            log.info(f"[{name}] Asked Aether HF to shut down")
        except AetherControlError as e:
            log.debug(f"[{name}] Aether HF shutdown request: {e}")
            return
        end = time.time() + wait_s
        while time.time() < end and client.ping():
            time.sleep(0.5)

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
