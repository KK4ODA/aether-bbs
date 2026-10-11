#!/usr/bin/env python3
"""
tools/vara_simulator.py

VARA Protocol Simulator — acts as a fake VARA FM/HF modem on TCP so the
BBS can be tested end-to-end without an RF connection.

How it works:
  1. Listens on the same cmd/data TCP ports the real VARA modem would
     (default: 8300/8301 for FM, 8310/8311 for HF — match your config).
  2. The BBS connects to these ports and sends its init commands (MYCALL,
     LISTEN ON, etc.) — the simulator responds with OK to each.
  3. You type a callsign to simulate an inbound connection.  The simulator
     sends "CONNECTED <callsign> <bbs_call>" on the cmd channel and then
     relays your keyboard input <-> BBS data channel in real time.
  4. Type /QUIT to simulate a clean RF disconnect.

Usage:
  python tools/vara_simulator.py                     # defaults: FM ports 8300/8301
  python tools/vara_simulator.py --cmd 8310 --data 8311   # HF ports

Start this BEFORE launching the BBS (or with auto_start disabled in
your instance config).  The BBS will connect to the simulator instead
of the real VARA modem.

Tip: To test two instances simultaneously, run two copies on different
port pairs.
"""

import sys
import socket
import select
import threading
import argparse
import time
from datetime import datetime


def log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    print(f"  [{ts}] {msg}")


class VARASimulator:
    """Fake VARA modem that speaks the VARA TCP protocol."""

    def __init__(self, cmd_port: int, data_port: int):
        self.cmd_port = cmd_port
        self.data_port = data_port

        self._cmd_srv: socket.socket | None = None
        self._data_srv: socket.socket | None = None
        self._cmd_client: socket.socket | None = None
        self._data_client: socket.socket | None = None

        self._bbs_call = "N0CALL-10"
        self._connected = False
        self._remote_call = ""
        self._running = True

        # BBS -> terminal output (read by display thread)
        self._lock = threading.Lock()

    def start(self):
        """Start listening and wait for the BBS to connect."""
        self._cmd_srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._cmd_srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._cmd_srv.bind(("127.0.0.1", self.cmd_port))
        self._cmd_srv.listen(1)

        self._data_srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._data_srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._data_srv.bind(("127.0.0.1", self.data_port))
        self._data_srv.listen(1)

        print(f"\n  VARA Simulator listening on cmd:{self.cmd_port}  data:{self.data_port}")
        print(f"  Start the BBS now (or restart the instance).\n")
        print(f"  Waiting for BBS to connect...")

        self._cmd_client, _ = self._cmd_srv.accept()
        log("BBS connected to CMD port")
        self._data_client, _ = self._data_srv.accept()
        log("BBS connected to DATA port")

        # Start threads to handle BBS cmd traffic
        threading.Thread(target=self._cmd_reader, daemon=True).start()

        # Send periodic IAMALIVE like the real VARA modem
        threading.Thread(target=self._keepalive_loop, daemon=True).start()

        print()
        self._show_menu()
        self._interactive_loop()

    def _show_menu(self):
        print("  ┌────────────────────────────────────────────┐")
        print("  │  VARA Simulator — Interactive Terminal      │")
        print("  ├────────────────────────────────────────────┤")
        print("  │  Type a callsign to connect, e.g.: W1AW   │")
        print("  │  While connected:                          │")
        print("  │    Type text    → sent to BBS as user      │")
        print("  │    /QUIT        → disconnect cleanly       │")
        print("  │    /DROP        → simulate RF drop         │")
        print("  │  /EXIT          → shut down simulator      │")
        print("  └────────────────────────────────────────────┘")
        print()

    def _interactive_loop(self):
        """Main loop: prompt for callsign, then relay keyboard <-> BBS."""
        while self._running:
            if not self._connected:
                try:
                    raw = input("  callsign> ").strip()
                except (EOFError, KeyboardInterrupt):
                    break
                if not raw:
                    continue
                if raw.upper() == "/EXIT":
                    break
                self._simulate_connect(raw.upper())
            else:
                try:
                    raw = input("")
                except (EOFError, KeyboardInterrupt):
                    self._simulate_disconnect()
                    continue
                if raw.upper() == "/QUIT":
                    self._simulate_disconnect()
                elif raw.upper() == "/DROP":
                    self._simulate_rf_drop()
                elif raw.upper() == "/EXIT":
                    self._simulate_disconnect()
                    break
                else:
                    self._send_data(raw + "\r")

        self._shutdown()

    def _simulate_connect(self, callsign: str):
        """Simulate an inbound RF connection from callsign."""
        self._remote_call = callsign
        self._connected = True
        # Send the VARA CONNECTED event on the cmd channel
        self._send_cmd(f"CONNECTED {callsign} {self._bbs_call}")
        log(f"Simulated RF connect: {callsign}")
        print(f"  ── Connected as {callsign} ── (type commands, /QUIT to disconnect)\n")

        # Start the data display thread
        threading.Thread(target=self._data_display, daemon=True).start()

    def _simulate_disconnect(self):
        """Simulate a clean RF disconnect."""
        if not self._connected:
            return
        self._connected = False
        self._send_cmd("DISCONNECTED")
        log(f"Simulated RF disconnect: {self._remote_call}")
        self._remote_call = ""
        print(f"\n  ── Disconnected ──\n")

    def _simulate_rf_drop(self):
        """Simulate an unclean RF drop (no DISCONNECTED sent first)."""
        if not self._connected:
            return
        self._connected = False
        # Real VARA would just stop sending data, then eventually send
        # DISCONNECTED after its own timeout.  We send it after a brief
        # delay to mimic that behavior.
        log(f"Simulated RF drop: {self._remote_call}")
        self._remote_call = ""
        print(f"\n  ── RF link dropped ──")
        time.sleep(2.0)
        self._send_cmd("DISCONNECTED")
        print(f"  ── VARA sent DISCONNECTED after timeout ──\n")

    # ── TCP I/O ──────────────────────────────────────────────────────

    def _send_cmd(self, line: str):
        """Send a line on the VARA cmd channel (modem -> BBS)."""
        try:
            self._cmd_client.sendall((line + "\r\n").encode("ascii"))
        except OSError:
            log("CMD send failed — BBS disconnected?")

    def _send_data(self, text: str):
        """Send data on the VARA data channel (remote user -> BBS)."""
        try:
            self._data_client.sendall(text.encode("utf-8", errors="replace"))
        except OSError:
            log("DATA send failed — BBS disconnected?")

    def _cmd_reader(self):
        """Read commands from BBS on the cmd channel and respond."""
        buf = ""
        while self._running:
            try:
                chunk = self._cmd_client.recv(1024)
                if not chunk:
                    log("BBS closed CMD connection")
                    break
                buf += chunk.decode("ascii", errors="replace")
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    line = line.strip()
                    if line:
                        self._handle_bbs_command(line)
            except OSError:
                break

    def _handle_bbs_command(self, cmd: str):
        """Process a command from the BBS and respond like VARA would."""
        upper = cmd.upper()
        log(f"BBS CMD> {cmd}")

        if upper.startswith("MYCALL"):
            parts = cmd.split()
            if len(parts) >= 2:
                self._bbs_call = parts[1]
                log(f"BBS callsign set to {self._bbs_call}")
            self._send_cmd("OK")

        elif upper == "LISTEN ON":
            self._send_cmd("OK")
            log("BBS is now listening")

        elif upper == "LISTEN OFF":
            self._send_cmd("OK")

        elif upper == "DISCONNECT":
            if self._connected:
                self._connected = False
                self._send_cmd("DISCONNECTED")
                log(f"BBS requested disconnect from {self._remote_call}")
                self._remote_call = ""
                print(f"\n  ── BBS disconnected the session ──\n")
                # Print the prompt again
                print("  callsign> ", end="", flush=True)
            else:
                self._send_cmd("OK")

        elif upper == "ABORT":
            if self._connected:
                self._connected = False
                self._send_cmd("DISCONNECTED")
                log("BBS sent ABORT")
                self._remote_call = ""
            self._send_cmd("OK")

        elif upper.startswith("WINLINK"):
            self._send_cmd("OK")
            log(f"WINLINK mode: {cmd}")

        elif upper.startswith("BW"):
            self._send_cmd("OK")
            log(f"Bandwidth: {cmd}")

        elif upper.startswith("CONNECT "):
            # BBS is trying to make an outbound connection
            parts = cmd.split()
            remote = parts[1] if len(parts) >= 2 else "UNKNOWN"
            log(f"BBS outbound connect to {remote} — auto-accepting")
            time.sleep(1.0)  # simulate connect delay
            self._remote_call = remote
            self._connected = True
            self._send_cmd(f"CONNECTED {self._bbs_call} {remote}")

        elif upper.startswith("COMPRESSION"):
            self._send_cmd("OK")

        else:
            # Unknown command — respond OK (real VARA does this for most things)
            self._send_cmd("OK")
            log(f"(unhandled command, replied OK)")

    def _data_display(self):
        """Read data from BBS on the data channel and display to terminal."""
        while self._connected and self._running:
            try:
                ready, _, _ = select.select([self._data_client], [], [], 0.5)
                if ready:
                    data = self._data_client.recv(4096)
                    if not data:
                        break
                    # Print BBS output to terminal
                    text = data.decode("utf-8", errors="replace")
                    # Print without adding extra newlines — the BBS sends \r\n
                    sys.stdout.write(text)
                    sys.stdout.flush()
            except OSError:
                break

    def _keepalive_loop(self):
        """Send IAMALIVE every 60s like the real VARA modem."""
        while self._running:
            time.sleep(60)
            if self._running and self._cmd_client:
                try:
                    self._send_cmd("IAMALIVE")
                except OSError:
                    break

    def _shutdown(self):
        self._running = False
        print("\n  Shutting down simulator...")
        for s in (self._cmd_client, self._data_client,
                  self._cmd_srv, self._data_srv):
            if s:
                try:
                    s.close()
                except OSError:
                    pass


def main():
    parser = argparse.ArgumentParser(
        description="VARA Protocol Simulator — test the BBS without RF",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python tools/vara_simulator.py                           # FM defaults (8300/8301)
  python tools/vara_simulator.py --cmd 8310 --data 8311    # HF instance ports
  python tools/vara_simulator.py --cmd 8300 --data 8301 --call W1AW
        """,
    )
    parser.add_argument("--cmd", type=int, default=8300,
                        help="VARA cmd port (default: 8300)")
    parser.add_argument("--data", type=int, default=8301,
                        help="VARA data port (default: 8301)")
    parser.add_argument("--call", type=str, default=None,
                        help="Auto-connect as this callsign on startup")
    args = parser.parse_args()

    print()
    print("  ╔════════════════════════════════════════════╗")
    print("  ║     Aether BBS — RF Connection Simulator     ║")
    print("  ╚════════════════════════════════════════════╝")

    sim = VARASimulator(args.cmd, args.data)

    try:
        if args.call:
            # Deferred auto-connect: wait for BBS, then connect
            def auto_connect():
                time.sleep(3)
                sim._simulate_connect(args.call.upper())
            threading.Thread(target=auto_connect, daemon=True).start()
        sim.start()
    except KeyboardInterrupt:
        sim._shutdown()


if __name__ == "__main__":
    main()
