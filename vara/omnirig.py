"""
vara/omnirig.py

Thread-safe OmniRig client.

All COM calls run on a single dedicated thread to avoid cross-apartment
proxy issues.  Both PTT control and frequency scanning share this one
connection — OmniRig sees exactly one client per radio, which is how it
was designed to work.

Usage:
    client = OmniRigClient(rig_number=1)
    client.start()          # spawns COM thread, connects to OmniRig

    client.set_ptt(True)    # thread-safe — queued to COM thread
    client.set_freq(7122000)
    client.set_mode(0x08000000)
    freq = client.get_freq()

    client.stop()           # releases PTT, disconnects, joins thread
"""

import logging
import queue
import threading
import time
from typing import Any, Optional

log = logging.getLogger(__name__)

# OmniRig PTT constants
PM_RX = 0x00200000   # receive (PTT off)
PM_TX = 0x00400000   # transmit (PTT on)

# OmniRig status constants
ST_NOTCONFIGURED = 0
ST_DISABLED      = 1
ST_PORTBUSY      = 2
ST_NOTRESPONDING = 3
ST_ONLINE        = 4
ST_TRANSCEIVE    = 5

STATUS_NAMES = {
    0: "ST_NOTCONFIGURED",
    1: "ST_DISABLED",
    2: "ST_PORTBUSY",
    3: "ST_NOTRESPONDING",
    4: "ST_ONLINE",
    5: "ST_TRANSCEIVE",
}

# OmniRig mode constants
OMNIRIG_MODES = {
    "LSB":      0x04000000,
    "USB":      0x02000000,
    "CW":       0x00800000,
    "FM":       0x40000000,
    "AM":       0x20000000,
    "DATA-U":   0x08000000,
    "DATA-L":   0x10000000,
    "DATA-USB": 0x08000000,
    "DATA-LSB": 0x10000000,
    "RTTY":     0x10000000,
}


class _Cmd:
    """A command to be executed on the COM thread."""
    __slots__ = ("action", "args", "result", "done")

    def __init__(self, action: str, args: Any = None):
        self.action = action
        self.args = args
        self.result: Any = None
        self.done = threading.Event()


class OmniRigClient:
    """Thread-safe OmniRig client.

    Spawns a single daemon thread that owns the COM connection.
    All public methods are safe to call from any thread — they enqueue
    commands and (for getters) block until the COM thread completes them.
    """

    def __init__(self, rig_number: int = 1):
        if rig_number not in (1, 2):
            raise ValueError("rig_number must be 1 or 2")
        self._rig_number = rig_number
        self._queue: queue.Queue[Optional[_Cmd]] = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self._ready = threading.Event()
        self._error: Optional[str] = None
        self._started = False

    # -- Lifecycle ---------------------------------------------------------

    def start(self) -> None:
        """Spawn the COM thread and connect to OmniRig.

        Blocks until the connection is established or fails.
        Raises RuntimeError on failure.
        """
        if self._started:
            return
        self._ready.clear()
        self._error = None
        self._thread = threading.Thread(
            target=self._run,
            name=f"OmniRig-Rig{self._rig_number}",
            daemon=True,
        )
        self._thread.start()
        self._ready.wait(timeout=15.0)
        if self._error:
            raise RuntimeError(self._error)
        self._started = True

    def stop(self) -> None:
        """Release PTT, disconnect, and join the COM thread."""
        if not self._started:
            return
        # Send poison pill
        self._queue.put(None)
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5.0)
        self._started = False

    @property
    def is_started(self) -> bool:
        return self._started

    # -- Thread-safe public API --------------------------------------------

    def set_ptt(self, on: bool) -> bool:
        """Key (True) or unkey (False) the radio.  Returns True on success."""
        target = PM_TX if on else PM_RX
        return self._exec("SET_PTT", target)

    def set_freq(self, freq_hz: int) -> bool:
        """Set VFO-A frequency in Hz.  Returns True on success."""
        return self._exec("SET_FREQ", freq_hz)

    def set_mode(self, mode_val: int) -> bool:
        """Set mode using OmniRig mode constant.  Returns True on success."""
        return self._exec("SET_MODE", mode_val)

    def set_mode_by_name(self, mode_str: str) -> bool:
        """Set mode by name (e.g. 'USB', 'DATA-USB').  Returns True."""
        val = OMNIRIG_MODES.get(mode_str.upper())
        if val is None:
            log.warning(
                f"OmniRig Rig{self._rig_number}: unknown mode "
                f"'{mode_str}', valid: {', '.join(OMNIRIG_MODES)}"
            )
            return False
        return self.set_mode(val)

    def get_freq(self) -> int:
        """Read current VFO-A frequency in Hz.  Returns 0 on failure."""
        return self._exec_get("GET_FREQ")

    def get_status(self) -> int:
        """Read OmniRig rig status word.  Returns 0 on failure."""
        return self._exec_get("GET_STATUS")

    def get_tx(self) -> int:
        """Read current Tx state.  Returns 0 on failure."""
        return self._exec_get("GET_TX")

    # -- Internal helpers --------------------------------------------------

    def _exec(self, action: str, args: Any = None) -> bool:
        """Send a command and wait for completion.  Returns success."""
        if not self._started:
            return False
        cmd = _Cmd(action, args)
        self._queue.put(cmd)
        cmd.done.wait(timeout=5.0)
        return cmd.result is True

    def _exec_get(self, action: str) -> Any:
        """Send a query and return the result."""
        if not self._started:
            return 0
        cmd = _Cmd(action)
        self._queue.put(cmd)
        cmd.done.wait(timeout=5.0)
        return cmd.result if cmd.result is not None else 0

    # -- COM thread --------------------------------------------------------

    def _run(self) -> None:
        """COM thread main loop -- owns the single OmniRig connection.

        Uses STA (Single-Threaded Apartment) with message pumping.
        OmniRig is a COM automation server that may need Windows
        message processing to handle its internal state machine.
        We pump messages both while idle and after each command.
        """
        rig_num = self._rig_number

        try:
            import win32com.client
            import pythoncom
        except ImportError:
            self._error = (
                "pywin32 is required for OmniRig. "
                "Install with: pip install pywin32"
            )
            self._ready.set()
            return

        try:
            pythoncom.CoInitialize()
        except Exception:
            pass  # already initialized

        try:
            omnirig = win32com.client.Dispatch("OmniRig.OmniRigX")
        except Exception as e:
            self._error = (
                f"Cannot connect to OmniRig COM server. "
                f"Is OmniRig installed and running? Error: {e}"
            )
            self._ready.set()
            return

        rig = omnirig.Rig1 if rig_num == 1 else omnirig.Rig2

        # Wait for OmniRig to establish radio communication.
        # OmniRig starts as ST_NOTRESPONDING and transitions to
        # ST_ONLINE or ST_TRANSCEIVE once it completes polling.
        log.info(
            f"OmniRig Rig{rig_num} Dispatch OK -- waiting for "
            f"radio to come online..."
        )
        last_status = -1
        online = False
        for attempt in range(50):  # up to ~5 seconds
            pythoncom.PumpWaitingMessages()
            try:
                status = rig.Status
                if status != last_status:
                    sname = STATUS_NAMES.get(status, f"UNKNOWN({status})")
                    log.info(
                        f"OmniRig Rig{rig_num} status: {sname} "
                        f"(0x{status:04X})"
                    )
                    last_status = status
                if status >= ST_ONLINE:
                    online = True
                    break
            except Exception as e:
                log.warning(f"OmniRig Rig{rig_num} status poll: {e}")
            time.sleep(0.1)

        if online:
            try:
                freq = rig.FreqA
                log.info(
                    f"OmniRig Rig{rig_num} ONLINE -- freq={freq} Hz "
                    f"({freq/1000:.1f} kHz)"
                )
            except Exception:
                pass
        else:
            log.warning(
                f"OmniRig Rig{rig_num} not online after 5s "
                f"(status={STATUS_NAMES.get(last_status, '?')}). "
                f"Commands may not reach the radio. Check OmniRig "
                f"setup: rig type, COM port, baud rate."
            )

        # Signal that we're ready
        self._ready.set()

        # -- Command loop --------------------------------------------------
        # We pump Windows messages regularly so OmniRig can process
        # its internal events (serial polling, state transitions).
        last_status_log = time.monotonic()
        STATUS_LOG_INTERVAL = 10.0  # log status every 10s for diagnostics

        try:
            while True:
                # Always pump messages -- OmniRig needs this
                pythoncom.PumpWaitingMessages()

                # Periodic status monitoring
                now = time.monotonic()
                if now - last_status_log >= STATUS_LOG_INTERVAL:
                    last_status_log = now
                    try:
                        status = rig.Status
                        sname = STATUS_NAMES.get(status, f"UNKNOWN({status})")
                        freq = rig.FreqA
                        if status != last_status:
                            log.info(
                                f"OmniRig Rig{rig_num} status changed: "
                                f"{sname}, freq={freq} Hz"
                            )
                            last_status = status
                        else:
                            log.debug(
                                f"OmniRig Rig{rig_num} status: {sname}, "
                                f"freq={freq} Hz"
                            )
                    except Exception:
                        pass

                try:
                    cmd = self._queue.get(timeout=0.1)
                except queue.Empty:
                    continue

                if cmd is None:
                    # Poison pill -- shut down
                    try:
                        rig.Tx = PM_RX
                        pythoncom.PumpWaitingMessages()
                    except Exception:
                        pass
                    break

                try:
                    if cmd.action == "SET_PTT":
                        rig.Tx = cmd.args
                        pythoncom.PumpWaitingMessages()
                        time.sleep(0.05)
                        pythoncom.PumpWaitingMessages()
                        # Readback
                        try:
                            rb = rig.Tx
                            log.debug(
                                f"OmniRig Rig{rig_num} SET_PTT "
                                f"0x{cmd.args:08X} -> readback "
                                f"0x{rb:08X}"
                            )
                        except Exception:
                            pass
                        cmd.result = True

                    elif cmd.action == "SET_FREQ":
                        rig.FreqA = cmd.args
                        pythoncom.PumpWaitingMessages()
                        time.sleep(0.05)
                        pythoncom.PumpWaitingMessages()
                        # Readback
                        try:
                            rb = rig.FreqA
                            log.debug(
                                f"OmniRig Rig{rig_num} SET_FREQ "
                                f"{cmd.args} Hz -> readback {rb} Hz"
                            )
                        except Exception:
                            pass
                        cmd.result = True

                    elif cmd.action == "SET_MODE":
                        rig.Mode = cmd.args
                        pythoncom.PumpWaitingMessages()
                        time.sleep(0.05)
                        pythoncom.PumpWaitingMessages()
                        try:
                            rb = rig.Mode
                            log.debug(
                                f"OmniRig Rig{rig_num} SET_MODE "
                                f"0x{cmd.args:08X} -> readback "
                                f"0x{rb:08X}"
                            )
                        except Exception:
                            pass
                        cmd.result = True

                    elif cmd.action == "GET_FREQ":
                        cmd.result = rig.FreqA
                    elif cmd.action == "GET_STATUS":
                        cmd.result = rig.Status
                    elif cmd.action == "GET_TX":
                        cmd.result = rig.Tx
                    else:
                        log.warning(
                            f"OmniRig Rig{rig_num}: unknown command "
                            f"{cmd.action}"
                        )
                        cmd.result = None
                except Exception as e:
                    log.error(
                        f"OmniRig Rig{rig_num} {cmd.action} error: {e}"
                    )
                    cmd.result = None
                finally:
                    cmd.done.set()

        finally:
            try:
                rig.Tx = PM_RX  # safety release
            except Exception:
                pass
            try:
                pythoncom.CoUninitialize()
            except Exception:
                pass
            log.info(f"OmniRig Rig{rig_num} COM thread exited")
