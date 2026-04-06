"""
vara/ptt.py

PTT (Push-To-Talk) control for VARA HF.

VARA FM controls PTT internally through its own GUI settings.
VARA HF does NOT — it sends "PTT ON" and "PTT OFF" events on the
command port and expects the connecting application to physically
key the radio.  This module bridges that gap.

Supported PTT methods (matching BPQ32 and vARIM conventions):

    VOX       — no-op; radio must be in VOX mode or PTT handled
                by VARA FM internally.
    RTS       — assert RTS line on a serial port (DigiRig, most
                USB sound card interfaces).
    DTR       — assert DTR line on a serial port.
    DTRRTS    — assert both DTR and RTS simultaneously.
    CM108     — toggle GPIO on C-Media CM108/CM119 USB audio chip
                (RA-Board, DRA-30 style interfaces).  Requires
                hidapi.
    HAMLIB    — send PTT commands to a running rigctld instance
                over TCP (Hamlib network protocol).
    OMNIRIG   — use OmniRig COM server (Windows only) for shared
                radio control.  Works alongside WSJT-X, Log4OM,
                and other OmniRig clients.  Requires pywin32.

Each method is implemented as a PTTDriver subclass.  The module
also provides a PTTController that subscribes to PTT_CHANGE events
on the event bus and delegates to the appropriate driver, with a
safety watchdog that releases PTT if the modem stops communicating.

References:
    BPQ32 VARA driver:  https://www.cantab.net/users/john.wiseman/Documents/VARA.html
    vARIM PTT:          https://www.whitemesa.net/varim/varim.html
    Pat (la5nta):       https://github.com/la5nta/pat
"""

import logging
import socket
import threading
import time
from abc import ABC, abstractmethod
from typing import Optional

log = logging.getLogger(__name__)


# ── PTT Driver Interface ─────────────────────────────────────────────────────

class PTTDriver(ABC):
    """Abstract base for all PTT backends."""

    @abstractmethod
    def open(self) -> None:
        """Acquire hardware resources (serial port, TCP socket, etc.)."""

    @abstractmethod
    def close(self) -> None:
        """Release hardware resources and ensure PTT is OFF."""

    @abstractmethod
    def set_ptt(self, on: bool) -> None:
        """Assert (True) or release (False) PTT."""

    @abstractmethod
    def description(self) -> str:
        """Human-readable description for logging."""


# ── VOX (no-op) ──────────────────────────────────────────────────────────────

class VOXDriver(PTTDriver):
    """No-op driver for VOX operation or VARA FM (which handles PTT itself)."""

    def open(self) -> None:
        pass

    def close(self) -> None:
        pass

    def set_ptt(self, on: bool) -> None:
        # Nothing to do — radio uses VOX or VARA FM handles PTT internally
        pass

    def description(self) -> str:
        return "VOX (no hardware PTT)"


# ── Serial port RTS/DTR ──────────────────────────────────────────────────────

class SerialPTTDriver(PTTDriver):
    """
    Assert PTT via serial port control lines (RTS and/or DTR).

    This is the most common method for sound card interfaces like
    DigiRig, SignaLink (with serial PTT), and generic USB-to-serial
    adapters wired to the radio's PTT line.

    Matches the PTT RTS / PTT DTR / PTT DTRRTS modes in BPQ32
    and the DTR / RTS modes in vARIM.
    """

    def __init__(self, port: str, use_rts: bool = True, use_dtr: bool = False):
        self._port_name = port
        self._use_rts = use_rts
        self._use_dtr = use_dtr
        self._serial = None

    def open(self) -> None:
        try:
            import serial
        except ImportError:
            raise RuntimeError(
                "pyserial is required for serial PTT control. "
                "Install with: pip install pyserial"
            )
        # Open the serial port with both control lines initially OFF.
        # We don't need to read/write data — just toggle RTS/DTR.
        self._serial = serial.Serial()
        self._serial.port = self._port_name
        self._serial.baudrate = 9600  # irrelevant for PTT-only use
        self._serial.timeout = 0
        # Ensure lines start de-asserted
        self._serial.rts = False
        self._serial.dtr = False
        self._serial.open()
        # Explicitly de-assert after open (some drivers assert on open)
        if self._use_rts:
            self._serial.rts = False
        if self._use_dtr:
            self._serial.dtr = False
        log.info(f"Serial PTT opened: {self._port_name} "
                 f"(RTS={'yes' if self._use_rts else 'no'}, "
                 f"DTR={'yes' if self._use_dtr else 'no'})")

    def close(self) -> None:
        if self._serial and self._serial.is_open:
            try:
                # Safety: ensure PTT is released
                if self._use_rts:
                    self._serial.rts = False
                if self._use_dtr:
                    self._serial.dtr = False
            except Exception:
                pass
            try:
                self._serial.close()
            except Exception:
                pass
            log.info(f"Serial PTT closed: {self._port_name}")
        self._serial = None

    def set_ptt(self, on: bool) -> None:
        if not self._serial or not self._serial.is_open:
            log.warning(f"Serial PTT: port not open, cannot {'key' if on else 'unkey'}")
            return
        try:
            if self._use_rts:
                self._serial.rts = on
            if self._use_dtr:
                self._serial.dtr = on
        except Exception as e:
            log.error(f"Serial PTT error on {self._port_name}: {e}")

    def description(self) -> str:
        lines = []
        if self._use_rts:
            lines.append("RTS")
        if self._use_dtr:
            lines.append("DTR")
        return f"Serial {'+'.join(lines)} on {self._port_name}"


# ── CM108 GPIO (RA-Board / DRA-30) ───────────────────────────────────────────

class CM108Driver(PTTDriver):
    """
    PTT via C-Media CM108/CM119 USB audio chip GPIO.

    Used by RA-Board, DRA-30, and similar ham radio sound card
    interfaces that have a built-in PTT GPIO line.  VARA calls
    this "RA Board PTT".

    On Windows, the device path is typically like:
        \\\\?\\hid#vid_0d8c&pid_000c#...
    On Linux:
        /dev/hidraw0

    Requires the 'hidapi' Python package.
    """

    # CM108 HID report: set GPIO bit 2 (pin 13) for PTT
    # This matches the convention used by VARA and hamlib.
    _GPIO_BIT = 0x04  # GPIO3 — standard PTT pin

    def __init__(self, device_path: str = ""):
        self._device_path = device_path
        self._hid = None

    def open(self) -> None:
        try:
            import hid
        except ImportError:
            raise RuntimeError(
                "hidapi is required for CM108 PTT control. "
                "Install with: pip install hidapi"
            )
        if self._device_path:
            self._hid = hid.device()
            self._hid.open_path(self._device_path.encode())
        else:
            # Auto-detect first C-Media device
            self._hid = hid.device()
            self._hid.open(0x0D8C, 0x000C)  # CM108 vendor/product
        log.info(f"CM108 PTT opened: {self._device_path or 'auto-detected'}")

    def close(self) -> None:
        if self._hid:
            try:
                self._set_gpio(False)
            except Exception:
                pass
            try:
                self._hid.close()
            except Exception:
                pass
            self._hid = None
            log.info("CM108 PTT closed")

    def set_ptt(self, on: bool) -> None:
        if not self._hid:
            log.warning("CM108 PTT: device not open")
            return
        try:
            self._set_gpio(on)
        except Exception as e:
            log.error(f"CM108 PTT error: {e}")

    def _set_gpio(self, on: bool):
        # HID feature report to set GPIO state on CM108.
        # Report format: [0x00, 0x00, gpio_value, gpio_mask, 0x00]
        # gpio_mask selects which bits to change, gpio_value sets them.
        value = self._GPIO_BIT if on else 0x00
        report = bytes([0x00, 0x00, value, self._GPIO_BIT, 0x00])
        self._hid.write(report)

    def description(self) -> str:
        return f"CM108/RA-Board GPIO ({self._device_path or 'auto'})"


# ── Hamlib rigctld ────────────────────────────────────────────────────────────

class HamlibDriver(PTTDriver):
    """
    PTT via Hamlib rigctld TCP protocol.

    rigctld is a network daemon that provides uniform access to
    hundreds of radio models.  This driver connects to it and sends
    the standard PTT commands:

        T 1\\n   → key PTT
        T 0\\n   → release PTT
        t\\n     → query PTT state (response: 0 or 1)

    A successful command returns "RPRT 0\\n".

    This matches the PTT HAMLIB mode in BPQ32 and the RIGCTLD
    mode in vARIM.  It also works with Pat (la5nta) and WSJT-X.
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 4532):
        self._host = host
        self._port = port
        self._sock: Optional[socket.socket] = None
        self._lock = threading.Lock()

    def open(self) -> None:
        self._connect()

    def _connect(self) -> None:
        try:
            sock = socket.create_connection(
                (self._host, self._port), timeout=5.0
            )
            sock.settimeout(3.0)
            self._sock = sock
            log.info(f"Hamlib PTT connected to rigctld at "
                     f"{self._host}:{self._port}")
        except OSError as e:
            raise RuntimeError(
                f"Cannot connect to rigctld at {self._host}:{self._port}: {e}"
            )

    def close(self) -> None:
        with self._lock:
            if self._sock:
                try:
                    self._send_cmd("T 0")  # safety: release PTT
                except Exception:
                    pass
                try:
                    self._sock.close()
                except Exception:
                    pass
                self._sock = None
                log.info("Hamlib PTT disconnected from rigctld")

    def set_ptt(self, on: bool) -> None:
        with self._lock:
            cmd = "T 1" if on else "T 0"
            try:
                resp = self._send_cmd(cmd)
                if resp and "RPRT 0" not in resp:
                    log.warning(f"rigctld PTT command '{cmd}' "
                                f"returned: {resp.strip()}")
            except OSError as e:
                log.error(f"Hamlib PTT error: {e}")
                # Try to reconnect for the next call
                self._try_reconnect()

    def _send_cmd(self, cmd: str) -> str:
        if not self._sock:
            raise OSError("Not connected to rigctld")
        self._sock.sendall((cmd + "\n").encode("ascii"))
        # Read response (rigctld replies with one line)
        data = b""
        while b"\n" not in data:
            chunk = self._sock.recv(256)
            if not chunk:
                raise OSError("rigctld closed connection")
            data += chunk
        return data.decode("ascii", errors="replace")

    def _try_reconnect(self):
        """Attempt to re-establish rigctld connection."""
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None
        try:
            self._connect()
            log.info("Hamlib PTT reconnected to rigctld")
        except Exception as e:
            log.warning(f"Hamlib PTT reconnect failed: {e}")

    def description(self) -> str:
        return f"Hamlib rigctld at {self._host}:{self._port}"


# ── OmniRig PTT ─────────────────────────────────────────────────────────────

class OmniRigDriver(PTTDriver):
    """
    PTT via OmniRig COM automation server (Windows only).

    Delegates to an OmniRigClient which owns the single COM thread.
    The same client can be shared with the rig controller for frequency
    scanning — one OmniRig connection, one COM thread, no conflicts.

    The client may be injected via the constructor (shared mode) or
    created internally (standalone mode, e.g. PTT test button).
    """

    def __init__(self, rig_number: int = 1, client=None):
        if rig_number not in (1, 2):
            raise ValueError("OmniRig rig_number must be 1 or 2")
        self._rig_number = rig_number
        self._client = client          # injected OmniRigClient, or None
        self._owns_client = False       # True if we created it ourselves

    def open(self) -> None:
        if self._client is not None:
            # Shared client — already started by broker
            if not self._client.is_started:
                self._client.start()
            self._owns_client = False
        else:
            # Standalone — create our own (e.g. PTT test button)
            from vara.omnirig import OmniRigClient
            self._client = OmniRigClient(rig_number=self._rig_number)
            self._client.start()
            self._owns_client = True

        status = self._client.get_status()
        if status == 0:
            log.warning(
                f"OmniRig Rig{self._rig_number} status is 0 "
                f"(not configured or offline)"
            )
        else:
            log.info(
                f"OmniRig PTT connected to Rig{self._rig_number} "
                f"(status=0x{status:04X})"
            )

    def close(self) -> None:
        if self._client:
            # Always release PTT
            try:
                self._client.set_ptt(False)
            except Exception:
                pass
            # Only stop the client if we created it
            if self._owns_client:
                self._client.stop()
                self._client = None
        log.info(f"OmniRig PTT disconnected from Rig{self._rig_number}")

    def set_ptt(self, on: bool) -> None:
        if not self._client or not self._client.is_started:
            log.warning("OmniRig PTT: not connected")
            return
        action = "TX (key)" if on else "RX (unkey)"
        target = 0x00400000 if on else 0x00200000
        log.info(f"OmniRig Rig{self._rig_number}: setting {action} "
                 f"(Tx = 0x{target:08X})")
        ok = self._client.set_ptt(on)
        if not ok:
            log.error(f"OmniRig PTT error on Rig{self._rig_number}")
        else:
            # Read back to verify
            try:
                readback = self._client.get_tx()
                log.info(f"OmniRig Rig{self._rig_number}: Tx readback = "
                         f"0x{readback:08X}")
            except Exception:
                pass

    def description(self) -> str:
        return f"OmniRig Rig{self._rig_number}"

    @property
    def client(self):
        """Return the OmniRigClient for sharing with rig control."""
        return self._client


# ── Driver Factory ────────────────────────────────────────────────────────────

def create_ptt_driver(
    method: str,
    port: str = "",
    hamlib_host: str = "127.0.0.1",
    hamlib_port: int = 4532,
    cm108_device: str = "",
    omnirig_rig: int = 1,
    omnirig_client=None,
) -> PTTDriver:
    """
    Create a PTT driver from configuration parameters.

    Parameters match the config schema in PTTConfig.
    omnirig_client is an optional OmniRigClient instance to share
    between PTT and rig control (avoids dual COM connections).
    """
    method = method.upper().strip()

    if method == "VOX" or method == "":
        return VOXDriver()

    elif method == "RTS":
        if not port:
            raise ValueError("Serial port required for RTS PTT")
        return SerialPTTDriver(port, use_rts=True, use_dtr=False)

    elif method == "DTR":
        if not port:
            raise ValueError("Serial port required for DTR PTT")
        return SerialPTTDriver(port, use_rts=False, use_dtr=True)

    elif method == "DTRRTS":
        if not port:
            raise ValueError("Serial port required for DTRRTS PTT")
        return SerialPTTDriver(port, use_rts=True, use_dtr=True)

    elif method == "HAMLIB":
        return HamlibDriver(host=hamlib_host, port=hamlib_port)

    elif method == "CM108":
        return CM108Driver(device_path=cm108_device)

    elif method == "OMNIRIG":
        return OmniRigDriver(rig_number=omnirig_rig, client=omnirig_client)

    else:
        raise ValueError(
            f"Unknown PTT method '{method}'. "
            f"Valid options: VOX, RTS, DTR, DTRRTS, HAMLIB, CM108, OMNIRIG"
        )


# ── PTT Controller ────────────────────────────────────────────────────────────

class PTTController:
    """
    Bridges VARA modem PTT events to physical radio control.

    Subscribes to PTT_CHANGE events on the event bus for a specific
    instance and delegates to the configured PTTDriver.  Includes
    a safety watchdog that automatically releases PTT if the modem
    hasn't communicated within a timeout period — preventing stuck
    transmitters if VARA crashes or TCP drops.

    Usage:
        controller = PTTController(
            instance_name="Secondary HF",
            driver=create_ptt_driver("RTS", port="COM5"),
            event_bus=bus,
            watchdog_timeout=90.0,
        )
        controller.start()    # opens hardware, subscribes to events
        ...
        controller.stop()     # unsubscribes, releases PTT, closes hardware
    """

    # Default watchdog timeout.  VARA HF sends IAMALIVE every ~60 s.
    # BPQ32 uses a similar concept; vARIM calls it "PTT monitor".
    DEFAULT_WATCHDOG_TIMEOUT = 90.0

    def __init__(
        self,
        instance_name: str,
        driver: PTTDriver,
        event_bus,
        watchdog_timeout: float = DEFAULT_WATCHDOG_TIMEOUT,
    ):
        self._name = instance_name
        self._driver = driver
        self._bus = event_bus
        self._watchdog_timeout = watchdog_timeout

        self._ptt_on = False
        self._last_event_time = time.monotonic()
        self._watchdog_timer: Optional[threading.Timer] = None
        self._lock = threading.Lock()
        self._started = False

    def start(self) -> None:
        """Open the PTT hardware and subscribe to events."""
        if self._started:
            return
        try:
            self._driver.open()
        except Exception as e:
            log.error(
                f"[{self._name}] PTT driver failed to open "
                f"({self._driver.description()}): {e}"
            )
            log.warning(
                f"[{self._name}] Falling back to VOX (no hardware PTT). "
                f"Inbound HF connections will fail unless radio is in VOX mode."
            )
            self._driver = VOXDriver()
            self._driver.open()

        self._bus.subscribe(self._bus_event_type("PTT_CHANGE"), self._on_ptt)
        self._started = True
        log.info(
            f"[{self._name}] PTT controller started: {self._driver.description()}"
        )

    def stop(self) -> None:
        """Release PTT, unsubscribe, close hardware."""
        if not self._started:
            return
        self._cancel_watchdog()
        self._bus.unsubscribe(self._bus_event_type("PTT_CHANGE"), self._on_ptt)
        # Ensure PTT is released
        with self._lock:
            if self._ptt_on:
                try:
                    self._driver.set_ptt(False)
                except Exception:
                    pass
                self._ptt_on = False
        try:
            self._driver.close()
        except Exception as e:
            log.warning(f"[{self._name}] PTT driver close error: {e}")
        self._started = False
        log.info(f"[{self._name}] PTT controller stopped")

    def get_omnirig_client(self):
        """Return the OmniRigClient if using OmniRig, else None.

        Used by the broker to share the single OmniRig COM connection
        with the rig controller.
        """
        if isinstance(self._driver, OmniRigDriver):
            return self._driver.client
        return None

    def _on_ptt(self, event) -> None:
        """Event bus callback for PTT_CHANGE events."""
        # Only handle events for our instance
        if event.source != self._name:
            return

        ptt_state = event.data.get("ptt", False) if event.data else False
        self._last_event_time = time.monotonic()

        with self._lock:
            if ptt_state == self._ptt_on:
                return  # no change
            self._ptt_on = ptt_state
            action = "KEYING" if ptt_state else "UNKEYING"

        log.debug(f"[{self._name}] PTT {action} radio via {self._driver.description()}")

        try:
            self._driver.set_ptt(ptt_state)
        except Exception as e:
            log.error(f"[{self._name}] PTT driver error: {e}")

        # Manage watchdog
        if ptt_state:
            self._start_watchdog()
        else:
            self._cancel_watchdog()

    def _start_watchdog(self):
        """Start or reset the PTT safety watchdog timer."""
        self._cancel_watchdog()
        self._watchdog_timer = threading.Timer(
            self._watchdog_timeout, self._watchdog_expired
        )
        self._watchdog_timer.daemon = True
        self._watchdog_timer.start()

    def _cancel_watchdog(self):
        if self._watchdog_timer:
            self._watchdog_timer.cancel()
            self._watchdog_timer = None

    def _watchdog_expired(self):
        """
        Safety cutoff — release PTT if modem hasn't sent PTT OFF
        within the timeout period.  Protects against stuck transmitters
        when VARA crashes, TCP disconnects, or the modem hangs.

        vARIM calls this the "PTT monitor" and BPQ32 has equivalent
        session timeout logic.
        """
        with self._lock:
            if not self._ptt_on:
                return
            self._ptt_on = False

        log.warning(
            f"[{self._name}] PTT WATCHDOG: forcibly releasing PTT after "
            f"{self._watchdog_timeout:.0f}s with no PTT OFF from modem. "
            f"Check VARA HF modem status!"
        )
        try:
            self._driver.set_ptt(False)
        except Exception as e:
            log.error(f"[{self._name}] PTT watchdog release error: {e}")

    @staticmethod
    def _bus_event_type(name: str):
        """Resolve EventType by name — avoids circular import."""
        from core.events import EventType
        return EventType[name]
