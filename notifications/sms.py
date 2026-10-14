"""
notifications/sms.py

SMS notifications via a USB GSM modem (AT command interface).
Sends SMS to one or more phone numbers when a priority alert fires.

Requires: pyserial (already in requirements.txt)

Typical USB GSM modems: Huawei E220, E303, SIM800/900 modules,
or any modem that presents a serial port and accepts AT commands.

The modem must have a SIM card with SMS capability installed.
"""

import time
import logging
import threading

from core.events import EventBus, EventType

log = logging.getLogger(__name__)

# One SMS at a time: concurrent sends would interleave AT commands on the port
_PORT_LOCK = threading.Lock()


def sms_safe_text(text: str) -> str:
    """Message text with control characters removed.  The modem ends the
    SMS at Ctrl+Z (0x1A) and cancels it at ESC, so a subject or callsign
    carrying either (from an RF user or an inbound email) would cut the
    message short and have the rest read as AT commands.  Newlines stay."""
    return "".join(c for c in text if c == "\n" or ord(c) >= 0x20)



class SMSNotifier:
    """
    Fire-and-forget SMS notifications via USB GSM modem.

    Subscribes to PRIORITY_MESSAGE_RECEIVED and CONFIG_CHANGED.
    SMS sending runs on a daemon thread so the EventBus is never blocked.
    """

    def __init__(self, config, event_bus: EventBus):
        self._cm     = config if hasattr(config, "current") else None
        self._config = config if self._cm is None else None
        self._bus    = event_bus

        self._bus.subscribe(EventType.PRIORITY_MESSAGE_RECEIVED,
                            self._on_priority_message)
        self._bus.subscribe(EventType.CONFIG_CHANGED,
                            self._on_config_changed)
        log.info("SMSNotifier initialised")

    # -- Public helpers ----------------------------------------------------

    def send_test(self):
        """Send a test SMS (called from Settings GUI)."""
        cfg = self._notify_cfg()
        if not cfg.sms_enabled:
            log.warning("SMS test skipped -- not enabled")
            return
        self._send_sms("Aether BBS test: SMS notifications are working!")

    def detect_modem(self, port: str, baud: int = 9600) -> str:
        """Try to detect a modem on the given port. Returns status string."""
        try:
            import serial
        except ImportError:
            return "ERROR: pyserial not installed (pip install pyserial)"

        try:
            with serial.Serial(port, baud, timeout=3) as ser:
                # Basic AT check
                ser.write(b"AT\r")
                time.sleep(0.5)
                resp = ser.read(ser.in_waiting or 64).decode("ascii", errors="replace")
                if "OK" not in resp:
                    return f"No response to AT command: {resp.strip()}"

                # Check signal quality
                ser.write(b"AT+CSQ\r")
                time.sleep(0.5)
                csq = ser.read(ser.in_waiting or 128).decode("ascii", errors="replace")

                # Check SIM status
                ser.write(b"AT+CPIN?\r")
                time.sleep(0.5)
                cpin = ser.read(ser.in_waiting or 128).decode("ascii", errors="replace")

                # Modem model
                ser.write(b"AT+CGMM\r")
                time.sleep(0.5)
                model = ser.read(ser.in_waiting or 128).decode("ascii", errors="replace")

                status_parts = ["Modem detected: OK"]
                if "READY" in cpin:
                    status_parts.append("SIM: Ready")
                else:
                    status_parts.append(f"SIM: {cpin.strip()}")

                for line in model.splitlines():
                    line = line.strip()
                    if line and line != "OK" and not line.startswith("AT"):
                        status_parts.append(f"Model: {line}")
                        break

                for line in csq.splitlines():
                    if "+CSQ:" in line:
                        status_parts.append(f"Signal: {line.strip()}")
                        break

                return " | ".join(status_parts)

        except Exception as e:
            return f"ERROR: {e}"

    # -- Event handlers ----------------------------------------------------

    def _on_priority_message(self, event):
        cfg = self._notify_cfg()
        if not cfg.sms_enabled:
            return

        d = event.data or {}
        mid       = d.get("mid", "?")
        from_call = d.get("from", "UNKNOWN")
        to_call   = d.get("to", "")
        subject   = d.get("subject", "")
        phrase    = d.get("alert_phrase", "Priority message received")

        # SMS is limited to 160 chars -- keep it concise
        parts = [f"Aether BBS: {phrase}"]
        parts.append(f"From:{from_call} To:{to_call}")
        if subject:
            remaining = 160 - sum(len(p) + 1 for p in parts) - len(f"MID:{mid}")
            if remaining > 10:
                parts.append(f"Subj:{subject[:remaining]}")
        parts.append(f"MID:{mid}")
        message = " | ".join(parts)

        # Truncate to 160 chars
        if len(message) > 160:
            message = message[:157] + "..."

        self._send_sms(message)

    def _on_config_changed(self, event):
        new_cfg = (event.data or {}).get("config")
        if new_cfg and self._cm is None:
            self._config = new_cfg

    # -- Internal ----------------------------------------------------------

    def _notify_cfg(self):
        if self._cm:
            app = self._cm.current()
            if app:
                return app.notifications
        if self._config:
            return self._config.notifications
        from core.config import NotificationConfig
        return NotificationConfig()

    def _send_sms(self, message: str):
        """Send SMS to all configured phone numbers on a daemon thread."""
        cfg = self._notify_cfg()
        port     = cfg.sms_serial_port.strip()
        baud     = cfg.sms_baud_rate
        numbers  = [n.strip() for n in cfg.sms_phone_numbers if n.strip()]

        if not port:
            log.warning("SMS: serial port not configured")
            return
        if not numbers:
            log.warning("SMS: no phone numbers configured")
            return

        def _do_send():
            try:
                import serial
            except ImportError:
                log.error("SMS: pyserial not installed (pip install pyserial)")
                return

            try:
                with _PORT_LOCK, serial.Serial(port, baud, timeout=5) as ser:
                    # Set text mode
                    ser.write(b"AT+CMGF=1\r")
                    time.sleep(0.5)
                    resp = ser.read(ser.in_waiting or 64).decode("ascii", errors="replace")
                    if "OK" not in resp:
                        log.warning(f"SMS: modem did not accept text mode: {resp.strip()}")
                        return

                    for number in numbers:
                        log.info(f"SMS: sending to {number}")
                        cmd = f'AT+CMGS="{number}"\r'
                        ser.write(cmd.encode("ascii"))
                        time.sleep(0.5)

                        # Read for the '>' prompt
                        prompt = ser.read(ser.in_waiting or 64).decode("ascii", errors="replace")
                        if ">" not in prompt:
                            log.warning(f"SMS: no prompt for {number}: {prompt.strip()}")
                            # Send escape to cancel
                            ser.write(b"\x1b")
                            time.sleep(0.5)
                            continue

                        # Send message text followed by Ctrl+Z
                        ser.write(sms_safe_text(message).encode("ascii", errors="replace"))
                        ser.write(b"\x1a")  # Ctrl+Z to send

                        # Wait for response
                        time.sleep(5)
                        result = ser.read(ser.in_waiting or 256).decode("ascii", errors="replace")

                        if "+CMGS:" in result or "OK" in result:
                            log.info(f"SMS sent to {number}")
                        else:
                            log.warning(f"SMS to {number} may have failed: {result.strip()}")

            except Exception as e:
                log.error(f"SMS send error: {e}", exc_info=True)

        threading.Thread(
            target=_do_send,
            name="sms-send",
            daemon=True,
        ).start()
