"""
tests/vara_simulator.py

VARA FM TCP simulator for integration testing (tests/test_vara_interface.py).
Mimics the VARA FM command and data TCP ports so you can test
the mux and session layers without real RF hardware.  It sends events the
way VARA does: "CONNECTED <remote> <local>", "BUSY ON|OFF", "PTT ON|OFF".

Usage:
    sim = VARAFMSimulator(cmd_port=8300, data_port=8301)
    sim.start()
    # ... run your tests ...
    sim.stop()

    # To simulate an incoming RF connection:
    sim.simulate_connect("W9XYZ")

    # To simulate incoming data from the remote station:
    sim.inject_data(b"Hello from RF\\r\\n")
"""

import time
import queue
import socket
import logging
import threading
from typing import Optional

log = logging.getLogger(__name__)


class VARAFMSimulator:
    """
    Minimal VARA FM TCP server simulator.

    Accepts exactly one command client and one data client.
    Allows tests to inject events and data.
    """

    def __init__(self, cmd_port: int = 8300, data_port: int = 8301):
        self.cmd_port  = cmd_port
        self.data_port = data_port

        self._cmd_server:  Optional[socket.socket] = None
        self._data_server: Optional[socket.socket] = None
        self._cmd_client:  Optional[socket.socket] = None
        self._data_client: Optional[socket.socket] = None

        self._cmd_tx_queue:  queue.Queue[str]   = queue.Queue()
        self._data_tx_queue: queue.Queue[bytes] = queue.Queue()
        self._cmd_rx_log:    list[str]          = []
        self._data_rx_log:   list[bytes]        = []
        self._data_tx_log:   list[bytes]        = []  # data sent TO the remote station (BBS responses)

        self._stop = threading.Event()

    # ── Lifecycle ─────────────────────────────────────────────────────────

    def start(self, wait_timeout: float = 5.0):
        """Start the simulator servers and wait for clients to connect."""
        self._cmd_server  = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._data_server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

        for srv, port in [
            (self._cmd_server, self.cmd_port),
            (self._data_server, self.data_port),
        ]:
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            srv.bind(("127.0.0.1", port))
            srv.listen(1)
            # Use a short accept() timeout so _accept_loop can check _stop
            # cleanly. Do NOT use wait_timeout here — that would cause accept()
            # to give up before the user clicks Start on the Dashboard.
            srv.settimeout(1.0)

        threading.Thread(target=self._accept_loop, daemon=True).start()
        log.info(f"VARA FM Simulator listening on {self.cmd_port}/{self.data_port}")

    def wait_for_client(self, timeout: float = 5.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._cmd_client and self._data_client:
                return True
            time.sleep(0.1)
        return False

    def stop(self):
        self._stop.set()
        for sock in (
            self._cmd_client, self._data_client,
            self._cmd_server, self._data_server,
        ):
            if sock:
                try: sock.close()
                except OSError: pass

    # ── Simulation controls ───────────────────────────────────────────────

    def simulate_connect(self, remote_call: str, mycall: str = "W1TEST-10"):
        """Inject a CONNECTED event as if an RF station called us."""
        self.send_event(f"CONNECTED {remote_call} {mycall}")

    def simulate_disconnect(self):
        """Inject a DISCONNECTED event."""
        self.send_event("DISCONNECTED")

    def simulate_busy(self, busy: bool):
        self.send_event(f"BUSY {'ON' if busy else 'OFF'}")

    def simulate_ptt(self, active: bool):
        self.send_event(f"PTT {'ON' if active else 'OFF'}")

    def simulate_buffer(self, n: int):
        self.send_event(f"BUFFER {n}")

    def send_event(self, event: str):
        """Send a VARA FM event line to the connected app."""
        self._cmd_tx_queue.put(event + "\r\n")

    def inject_data(self, data: bytes):
        """Send raw data bytes to the connected app on the data channel."""
        self._data_tx_queue.put(data)

    def get_received_commands(self) -> list[str]:
        """Return all commands received from the app so far."""
        return list(self._cmd_rx_log)

    def get_received_data(self) -> bytes:
        """Return all data received FROM the BBS app (remote station transmitting)."""
        return b"".join(self._data_rx_log)

    def get_sent_data(self) -> bytes:
        """Return all data sent TO the BBS app (BBS responses to remote station)."""
        return b"".join(self._data_tx_log)

    def wait_for_command(self, prefix: str, timeout: float = 5.0) -> Optional[str]:
        """Block until a command starting with prefix is received."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            for cmd in self._cmd_rx_log:
                if cmd.upper().startswith(prefix.upper()):
                    return cmd
            time.sleep(0.05)
        return None

    # ── Internal ──────────────────────────────────────────────────────────

    def _accept_loop(self):
        """Accept connections repeatedly — re-accepts if the client reconnects."""
        while not self._stop.is_set():
            # Accept cmd connection
            cmd_client = None
            while not self._stop.is_set() and cmd_client is None:
                try:
                    cmd_client, _ = self._cmd_server.accept()
                except socket.timeout:
                    continue
                except OSError:
                    return

            if self._stop.is_set():
                return

            # Accept data connection
            data_client = None
            while not self._stop.is_set() and data_client is None:
                try:
                    data_client, _ = self._data_server.accept()
                except socket.timeout:
                    continue
                except OSError:
                    return

            if self._stop.is_set():
                return

            # Don't forcibly close old sockets — the interface manages its own
            # end. Just replace references and reset logs.
            self._cmd_client  = cmd_client
            self._data_client = data_client

            # Reset logs for the new connection
            self._cmd_rx_log.clear()
            self._data_rx_log.clear()
            self._data_tx_log.clear()


            for target, name in [
                (self._cmd_reader,   "SimCmdRx"),
                (self._data_reader,  "SimDataRx"),
                (self._cmd_writer,   "SimCmdTx"),
                (self._data_writer,  "SimDataTx"),
            ]:
                threading.Thread(target=target, name=name, daemon=True).start()

    def _cmd_reader(self):
        buf = ""
        while not self._stop.is_set():
            try:
                chunk = self._cmd_client.recv(1024).decode("ascii", errors="replace")
                if not chunk: break
                buf += chunk
                buf = buf.replace("\r\n", "\n").replace("\r", "\n")
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    line = line.strip()
                    if line:
                        self._cmd_rx_log.append(line)
                        log.debug(f"Sim CMD<: {line}")
                        # Auto-respond to DISCONNECT with DISCONNECTED event
                        if line.upper().startswith("DISCONNECT"):
                            self.send_event("DISCONNECTED")
            except OSError:
                break

    def _data_reader(self):
        while not self._stop.is_set():
            try:
                data = self._data_client.recv(4096)
                if not data: break
                self._data_rx_log.append(data)
            except OSError:
                break

    def _cmd_writer(self):
        while not self._stop.is_set():
            try:
                line = self._cmd_tx_queue.get(timeout=0.5)
                self._cmd_client.sendall(line.encode("ascii"))
                log.debug(f"Sim CMD>: {line.strip()}")
            except queue.Empty:
                continue
            except OSError:
                break

    def _data_writer(self):
        while not self._stop.is_set():
            try:
                data = self._data_tx_queue.get(timeout=0.5)
                self._data_client.sendall(data)
                self._data_tx_log.append(data)  # log BBS responses for console display
            except queue.Empty:
                continue
            except OSError:
                break
