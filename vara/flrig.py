"""
vara/flrig.py — Thread-safe FLrig XML-RPC client for VARA Term.

Communicates with the FLrig application via its XML-RPC server
(default port 12345) for CAT and PTT control.  FLrig supports
hundreds of transceivers and exposes them through a simple XML-RPC
interface that multiple applications can share simultaneously.

Architecture mirrors HamlibClient: all XML-RPC I/O happens on a
single dedicated thread; public methods are thread-safe, queueing
commands and blocking until the I/O thread completes them.

FLrig must be running independently before VARA Term connects.

Usage:
    client = FLrigClient(host="127.0.0.1", port=12345)
    client.start()

    client.set_freq(14_074_000)
    client.set_mode("USB")
    client.set_ptt(True)
    freq = client.get_freq()

    client.stop()
"""

import logging
import queue
import socket
import threading
import time
import xmlrpc.client
from typing import Any, Optional, Tuple

log = logging.getLogger(__name__)

# FLrig default XML-RPC port
DEFAULT_FLRIG_PORT = 12345


class _Cmd:
    """A command to be executed on the I/O thread."""
    __slots__ = ("action", "args", "result", "error", "done")

    def __init__(self, action: str, args: Any = None):
        self.action = action
        self.args = args
        self.result: Any = None
        self.error: Optional[str] = None
        self.done = threading.Event()


class FLrigClient:
    """Thread-safe FLrig XML-RPC client.

    A single daemon thread owns the XML-RPC connection to FLrig.
    All public methods are safe to call from any thread — they queue
    commands and (for getters) block until the I/O thread completes them.

    The API surface matches HamlibClient so FLrig can be used as a
    drop-in replacement for CAT/PTT operations.
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = DEFAULT_FLRIG_PORT,
    ):
        self._host = host
        self._port = port

        self._queue: queue.Queue[Optional[_Cmd]] = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self._proxy: Optional[xmlrpc.client.ServerProxy] = None
        self._ready = threading.Event()
        self._error: Optional[str] = None
        self._started = False
        self._version: str = ""

    # ── Lifecycle ─────────────────────────────────────────────────

    def start(self) -> None:
        """Connect to FLrig's XML-RPC server.

        Blocks until the connection is verified or fails.
        Raises RuntimeError on failure.
        """
        if self._started:
            return

        self._ready.clear()
        self._error = None
        self._thread = threading.Thread(
            target=self._run,
            name="FLrig-IO",
            daemon=True,
        )
        self._thread.start()
        self._ready.wait(timeout=10.0)
        if self._error:
            raise RuntimeError(self._error)
        self._started = True

    def stop(self) -> None:
        """Release PTT, disconnect, and join the I/O thread."""
        if not self._started:
            return
        self._queue.put(None)  # poison pill
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5.0)
        self._started = False

    @property
    def is_started(self) -> bool:
        return self._started

    @property
    def version(self) -> str:
        """FLrig version string, available after start()."""
        return self._version

    # ── Thread-safe public API ────────────────────────────────────
    #    Matches HamlibClient interface for drop-in usage.

    def set_ptt(self, on: bool) -> bool:
        """Key (True) or unkey (False) the radio.  Returns True on success."""
        return self._exec("SET_PTT", 1 if on else 0)

    def set_freq(self, freq_hz: int) -> bool:
        """Set VFO frequency in Hz.  Returns True on success."""
        return self._exec("SET_FREQ", freq_hz)

    def set_mode(self, mode: str, passband: int = 0) -> bool:
        """Set mode (e.g. 'USB', 'LSB', 'CW', 'FM', 'AM').
        passband is ignored (FLrig manages bandwidth internally).
        Returns True on success.
        """
        return self._exec("SET_MODE", mode)

    def get_freq(self) -> int:
        """Read current VFO frequency in Hz.  Returns 0 on failure."""
        return self._exec_get("GET_FREQ")

    def get_mode(self) -> Tuple[str, int]:
        """Read current mode and passband.  Returns ('', 0) on failure."""
        result = self._exec_get("GET_MODE")
        if isinstance(result, tuple):
            return result
        return ("", 0)

    def get_ptt(self) -> bool:
        """Read current PTT state.  Returns False on failure."""
        result = self._exec_get("GET_PTT")
        return result == 1

    def get_info(self) -> str:
        """Read rig info / FLrig version string.  Returns '' on failure."""
        result = self._exec_get("GET_INFO")
        return result if isinstance(result, str) else ""

    def get_strength(self) -> int:
        """Read S-meter in dBm.  Returns 0 on failure."""
        return self._exec_get("GET_STRENGTH")

    # ── Internal helpers ──────────────────────────────────────────

    def _exec(self, action: str, args: Any = None) -> bool:
        if not self._started:
            return False
        cmd = _Cmd(action, args)
        self._queue.put(cmd)
        cmd.done.wait(timeout=5.0)
        return cmd.result is True

    def _exec_get(self, action: str) -> Any:
        if not self._started:
            return 0
        cmd = _Cmd(action)
        self._queue.put(cmd)
        cmd.done.wait(timeout=5.0)
        return cmd.result if cmd.result is not None else 0

    # ── I/O thread ────────────────────────────────────────────────

    def _run(self) -> None:
        """I/O thread main loop — owns the XML-RPC connection to FLrig."""
        url = f"http://{self._host}:{self._port}"

        # Connect to FLrig (with socket timeout to prevent indefinite hangs)
        old_timeout = socket.getdefaulttimeout()
        socket.setdefaulttimeout(10.0)
        retries = 3
        for attempt in range(retries):
            try:
                self._proxy = xmlrpc.client.ServerProxy(url, allow_none=True)
                # Verify connection by requesting the version
                self._version = self._proxy.main.get_version()
                log.info(
                    f"Connected to FLrig at {url} (version: {self._version})"
                )
                break
            except Exception as e:
                if attempt < retries - 1:
                    log.info(
                        f"FLrig connect attempt {attempt + 1} failed, retrying..."
                    )
                    time.sleep(1.0)
                else:
                    self._error = (
                        f"Cannot connect to FLrig at {url}. "
                        f"Is FLrig running?  Error: {e}"
                    )
                    self._ready.set()
                    socket.setdefaulttimeout(old_timeout)
                    return

        socket.setdefaulttimeout(old_timeout)

        # Try to read initial frequency for info
        try:
            freq = self._proxy.rig.get_vfo()
            freq_hz = int(float(freq))
            log.info(f"FLrig: Radio freq = {freq_hz / 1000:.1f} kHz")
        except Exception:
            pass

        self._ready.set()

        # -- Command loop --
        try:
            while True:
                try:
                    cmd = self._queue.get(timeout=0.2)
                except queue.Empty:
                    continue

                if cmd is None:
                    # Poison pill — release PTT and shut down
                    try:
                        self._proxy.rig.set_ptt(0)
                    except Exception:
                        pass
                    break

                try:
                    self._dispatch(cmd)
                except Exception as e:
                    log.error(f"FLrig {cmd.action} error: {e}")
                    cmd.result = None
                    cmd.error = str(e)
                finally:
                    cmd.done.set()

        finally:
            # Safety: release PTT on exit
            try:
                if self._proxy:
                    self._proxy.rig.set_ptt(0)
            except Exception:
                pass
            self._proxy = None
            log.info("FLrig I/O thread exited")

    def _dispatch(self, cmd: _Cmd):
        """Execute a command on the I/O thread."""
        if not self._proxy:
            cmd.result = None
            return

        if cmd.action == "SET_PTT":
            try:
                result = self._proxy.rig.set_ptt(cmd.args)
                log.info(f"FLrig rig.set_ptt({cmd.args}) → {result!r}")
                cmd.result = True
                # Verify PTT state actually changed
                try:
                    actual = self._proxy.rig.get_ptt()
                    expected = cmd.args
                    if int(actual) != int(expected):
                        log.warning(
                            f"FLrig PTT verify: expected {expected}, got {actual}")
                        cmd.result = False
                except Exception as e:
                    log.debug(f"FLrig PTT verify read-back failed: {e}")
            except Exception as e:
                log.error(f"FLrig set_ptt({cmd.args}) error: {e}")
                cmd.result = False

        elif cmd.action == "SET_FREQ":
            try:
                # FLrig expects frequency as a double
                self._proxy.main.set_frequency(float(cmd.args))
                cmd.result = True
            except Exception as e:
                log.error(f"FLrig set_freq error: {e}")
                cmd.result = False

        elif cmd.action == "SET_MODE":
            try:
                self._proxy.rig.set_mode(cmd.args)
                cmd.result = True
            except Exception as e:
                log.error(f"FLrig set_mode error: {e}")
                cmd.result = False

        elif cmd.action == "GET_FREQ":
            try:
                freq = self._proxy.rig.get_vfo()
                cmd.result = int(float(freq))
            except Exception:
                cmd.result = 0

        elif cmd.action == "GET_MODE":
            try:
                mode = self._proxy.rig.get_mode()
                # FLrig returns mode as a string; passband not available
                cmd.result = (mode, 0)
            except Exception:
                cmd.result = ("", 0)

        elif cmd.action == "GET_PTT":
            try:
                ptt = self._proxy.rig.get_ptt()
                cmd.result = int(ptt)
            except Exception:
                cmd.result = 0

        elif cmd.action == "GET_INFO":
            try:
                version = self._proxy.main.get_version()
                info = self._proxy.rig.get_info()
                cmd.result = f"FLrig {version}: {info}"
            except Exception:
                cmd.result = ""

        elif cmd.action == "GET_STRENGTH":
            try:
                smeter = self._proxy.rig.get_smeter()
                # FLrig returns S-meter as a string like "S5" or "-73"
                # Try to parse numeric dBm value
                s = str(smeter).strip()
                cmd.result = int(float(s))
            except Exception:
                cmd.result = 0

        else:
            cmd.result = None
