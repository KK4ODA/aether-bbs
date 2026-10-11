"""
tests/test_aether.py

Aether HF integration: config migration, the control-API client and
monitor, and a BBS instance attached to a fake aetherd that speaks the
VARA-compatible host interface (docs/spec/host-interfaces.md in aether-hf).
No radio, sound card or Aether HF installation required.
"""

import json
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.config import AppConfig, ConfigManager, InstanceConfig, migrate_legacy_config
from core.events import EventBus, EventType


# ── Fakes ────────────────────────────────────────────────────────────────────

def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _free_port_pair() -> int:
    """A port p with p and p+1 both free (command + data port)."""
    for _ in range(50):
        p = _free_port()
        try:
            with socket.socket() as a, socket.socket() as b:
                a.bind(("127.0.0.1", p))
                b.bind(("127.0.0.1", p + 1))
            return p
        except OSError:
            continue
    raise RuntimeError("no free port pair")


STATUS = {
    "state": "idle", "callsign": "N0CALL-10", "remote": None,
    "version": "0.2.0-beta.102", "mode": 7, "transmitting": False,
    "channel_busy": False, "frequency_hz": 14105000,
    "bandwidth": {"bandwidth_hz": 2300, "home_hz": 2300, "mismatch": None},
    "host": {"enabled": True, "connected": True, "chat": False},
    "answering": True, "audio_fault": None, "ptt_fault": None,
    "regulatory": {"policy": "rules",
                   "indicator": {"verdict": "legal", "summary": "Legal here"}},
}


class FakeControlAPI:
    """POST /v1/<method> → {"ok": true, "result": ...}."""

    def __init__(self, token: str = ""):
        self.status = json.loads(json.dumps(STATUS))
        self.heard = []
        self.token = token
        api = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(n)
                if api.token and self.headers.get("Authorization") != f"Bearer {api.token}":
                    self._reply(401, {"ok": False, "error": {"code": "unauthorised"}})
                    return
                method = self.path.rsplit("/", 1)[-1]
                if method == "status":
                    self._reply(200, {"ok": True, "result": api.status})
                elif method == "heard.list":
                    self._reply(200, {"ok": True, "result": {"stations": api.heard}})
                else:
                    self._reply(400, {"ok": False, "error": {
                        "code": "unsupported", "message": f"No method {method}",
                        "retryable": False}})

            def _reply(self, code, obj):
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class FakeAetherHost:
    """aetherd's VARA-compatible host interface: command port + data port."""

    def __init__(self, cmd_port: int):
        self.commands: list[str] = []
        self.data_rx = bytearray()
        self._cmd = socket.socket()
        self._cmd.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._cmd.bind(("127.0.0.1", cmd_port))
        self._cmd.listen(1)
        self._data = socket.socket()
        self._data.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._data.bind(("127.0.0.1", cmd_port + 1))
        self._data.listen(1)
        self.cmd_conn = None
        self.data_conn = None
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        self.cmd_conn, _ = self._cmd.accept()
        self.data_conn, _ = self._data.accept()
        self.say("BUFFER 0")
        threading.Thread(target=self._read_data, daemon=True).start()
        buf = b""
        while True:
            try:
                chunk = self.cmd_conn.recv(1024)
            except OSError:
                return
            if not chunk:
                return
            buf += chunk
            while b"\r" in buf:
                line, buf = buf.split(b"\r", 1)
                line = line.strip().decode()
                if not line:
                    continue
                self.commands.append(line)
                if line.upper() == "VERSION":
                    self.say("VERSION Aether HF 0.2.0-beta.102")
                else:
                    self.say("OK")

    def _read_data(self):
        while True:
            try:
                d = self.data_conn.recv(4096)
            except OSError:
                return
            if not d:
                return
            self.data_rx += d

    def say(self, line: str):
        self.cmd_conn.sendall((line + "\r").encode())

    def send_data(self, data: bytes):
        self.data_conn.sendall(data)

    def close(self):
        for s in (self.cmd_conn, self.data_conn, self._cmd, self._data):
            try:
                if s:
                    s.close()
            except OSError:
                pass


def _wait(pred, timeout=15.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.05)
    return False


# ── Config ───────────────────────────────────────────────────────────────────

class TestAetherConfig:

    def test_defaults(self):
        inst = InstanceConfig(transport="AETHER_HF")
        assert inst.is_aether and inst.is_hf
        assert inst.cmd_port == 8300 and inst.data_port == 8301
        assert inst.aether.control_port == 8515
        assert inst.aether.bandwidth == 0
        assert inst.aether.monitor is True

    def test_bad_bandwidth_rejected(self):
        with pytest.raises(Exception):
            InstanceConfig(transport="AETHER_HF", aether={"bandwidth": 2750})

    def test_prototype_fields_migrate(self, tmp_path):
        path = tmp_path / "aether_bbs.toml"
        path.write_text(
            '[[instances]]\n'
            'name = "HF"\n'
            'transport = "AETHER_HF"\n'
            '[instances.aether]\n'
            'cmd_port = 8400\n'
            'data_port = 8401\n'
            'mode = "wide"\n'
            'auto_launch = true\n'
            '[updater]\n'
            'include_aether = true\n',
            encoding="utf-8",
        )
        cfg = ConfigManager(path).load()
        inst = cfg.instances[0]
        assert inst.transport == "AETHER_HF"
        assert (inst.cmd_port, inst.data_port) == (8400, 8401)
        assert inst.aether.control_port == 8515
        assert not hasattr(cfg.updater, "include_aether")

    def test_control_token_encrypted_at_rest(self, tmp_path):
        path = tmp_path / "aether_bbs.toml"
        path.write_text(
            '[[instances]]\nname = "HF"\ntransport = "AETHER_HF"\n'
            '[instances.aether]\ncontrol_token = "s3cret"\n',
            encoding="utf-8",
        )
        cfg = ConfigManager(path).load()
        stored = cfg.instances[0].aether.control_token
        assert stored.startswith("ENC:") and "s3cret" not in path.read_text()
        from core.crypto import decrypt_value
        assert decrypt_value(stored, cfg.security.machine_secret) == "s3cret"

    def test_legacy_config_copied_once(self, tmp_path):
        legacy = tmp_path / "vara-fm-bbs" / "vara_fm_bbs.toml"
        legacy.parent.mkdir()
        legacy.write_text('[station]\nbbs_callsign = "KK4ODA-10"\n', encoding="utf-8")
        new = tmp_path / "aether-bbs" / "aether_bbs.toml"
        assert migrate_legacy_config(new, legacy) is True
        assert legacy.exists() and new.exists()
        assert ConfigManager(new).load().station.bbs_callsign == "KK4ODA-10"
        assert migrate_legacy_config(new, legacy) is False   # never overwrites

    def test_old_default_texts_rebranded(self, tmp_path):
        path = tmp_path / "c.toml"
        path.write_text(
            '[station]\nbeacon_text = "VARA BBS"\n'
            '[bbs]\nwelcome_message = "Welcome to the VARA BBS. Type H for help."\n'
            '[notifications]\ntopic = "vara-bbs"\n', encoding="utf-8")
        cfg = ConfigManager(path).load()
        assert cfg.station.beacon_text == "Aether BBS"
        assert cfg.bbs.welcome_message.startswith("Welcome to the Aether BBS")
        assert cfg.notifications.topic == "vara-bbs"   # subscriptions keep working

    def test_default_config_saves(self, tmp_path):
        cm = ConfigManager(tmp_path / "c.toml")
        cm.save(AppConfig())                 # latitude=None must not break TOML
        assert cm.load().station.beacon_text == "Aether BBS"


# ── Interface helpers ───────────────────────────────────────────────────────

class TestInterfaceHelpers:

    def test_init_bandwidth(self):
        from vara.interface import init_bandwidth
        assert init_bandwidth(InstanceConfig(transport="AETHER_HF")) is None
        assert init_bandwidth(InstanceConfig(transport="AETHER_HF",
                                             aether={"bandwidth": 500})) == 500
        assert init_bandwidth(InstanceConfig(transport="VARA_HF")) == 500
        assert init_bandwidth(InstanceConfig(transport="VARA_FM")) is None

    def test_remote_from_connected_either_order(self):
        from vara.interface import VARAFMInterfaceWithCall
        iface = VARAFMInterfaceWithCall(
            InstanceConfig(transport="AETHER_HF"), EventBus(), "N0CALL-10")
        # inbound: caller first
        assert iface._remote_from_connected(
            "CONNECTED KK4XYZ N0CALL-10 2300".split()) == "KK4XYZ"
        # outbound: this station is the caller
        assert iface._remote_from_connected(
            "CONNECTED N0CALL-10 KK4XYZ 500".split()) == "KK4XYZ"
        # VARA FM's two-callsign form
        assert iface._remote_from_connected(
            "CONNECTED W1ABC N0CALL-10".split()) == "W1ABC"

    def test_launcher_command(self):
        from aether.launcher import build_command
        assert build_command("C:/x/aether-hf.exe", "s.toml") == ["C:/x/aether-hf.exe"]
        assert build_command("/usr/bin/aetherd", "s.toml") == [
            "/usr/bin/aetherd", "--config", "s.toml"]


# ── Control API client and monitor ──────────────────────────────────────────

class TestControlAPI:

    def test_status_and_refusal(self):
        from aether.control import AetherControlClient, AetherControlError
        api = FakeControlAPI()
        try:
            c = AetherControlClient("127.0.0.1", api.port)
            assert c.status()["version"] == "0.2.0-beta.102"
            with pytest.raises(AetherControlError) as ei:
                c.call("nonsense")
            assert ei.value.code == "unsupported"
        finally:
            api.close()

    def test_token(self):
        from aether.control import AetherControlClient, AetherControlError
        api = FakeControlAPI(token="tok")
        try:
            with pytest.raises(AetherControlError) as ei:
                AetherControlClient("127.0.0.1", api.port).status()
            assert ei.value.code == "unauthorised"
            assert AetherControlClient("127.0.0.1", api.port, "tok").ping()
        finally:
            api.close()

    def test_unreachable(self):
        from aether.control import AetherControlClient
        assert AetherControlClient("127.0.0.1", _free_port(), timeout=0.5).ping() is False

    def test_remote_only_during_a_session(self):
        from aether.monitor import summarize_status
        st = json.loads(json.dumps(STATUS))
        st["remote"] = "WC4Y"            # aetherd keeps the last station
        assert summarize_status(st)["remote"] == ""
        st["state"] = "connected"
        assert summarize_status(st)["remote"] == "WC4Y"

    def test_problems(self):
        from aether.monitor import problems, summarize_status
        st = json.loads(json.dumps(STATUS))
        assert problems(summarize_status(st)) == []
        st["host"]["enabled"] = False
        st["audio_fault"] = "The sound device was unplugged."
        st["regulatory"] = {"policy": "unset", "indicator": {}}
        msgs = " ".join(problems(summarize_status(st)))
        assert "Host programs" in msgs and "unplugged" in msgs and "No rules" in msgs

    def test_monitor_emits_status_and_new_heard(self):
        from aether.control import AetherControlClient
        from aether.monitor import AetherMonitor
        api = FakeControlAPI()
        api.heard = [{"callsign": "W1OLD", "last_heard_ms": 1, "activity": "beacon"}]
        bus = EventBus()
        statuses, heard = [], []
        bus.subscribe(EventType.AETHER_STATUS, statuses.append)
        bus.subscribe(EventType.STATION_HEARD, heard.append)
        try:
            mon = AetherMonitor("HF", AetherControlClient("127.0.0.1", api.port), bus)
            summary = mon.poll_once()
            assert summary["reachable"] and summary["bandwidth_hz"] == 2300
            mon.poll_heard()                    # primes: history is not replayed
            assert heard == []
            api.heard.append({"callsign": "KK4XYZ-7", "last_heard_ms": 5,
                              "activity": "calling"})
            mon.poll_heard()
            assert [(e.data["callsign"], e.data["ssid"]) for e in heard] == [("KK4XYZ", 7)]
            assert statuses and statuses[0].source == "HF"
        finally:
            api.close()


# ── A BBS instance attached to a fake aetherd ───────────────────────────────

class TestAetherSession:

    def _attach(self, **aether):
        from vara.broker import TNCBroker
        port = _free_port_pair()
        host = FakeAetherHost(port)
        app = AppConfig()
        app.station.bbs_callsign = "N0CALL-10"
        inst = InstanceConfig(name="HF", transport="AETHER_HF",
                              cmd_port=port, data_port=port + 1, aether=aether)
        bus = EventBus()
        broker = TNCBroker(inst, app, bus)
        threading.Thread(target=broker.connect, daemon=True).start()
        return host, broker, bus

    def test_init_and_inbound_session(self):
        host, broker, bus = self._attach()
        connected, quality = [], []
        bus.subscribe(EventType.SESSION_CONNECTED, connected.append)
        bus.subscribe(EventType.LINK_QUALITY, quality.append)
        try:
            assert _wait(lambda: "LISTEN ON" in host.commands), host.commands
            assert _wait(lambda: "VERSION" in host.commands), host.commands
            assert host.commands[0] == "MYCALL N0CALL-10"
            assert "COMPRESSION OFF" in host.commands
            assert not any(c.startswith("BW") for c in host.commands)

            # An inbound call, as aetherd reports it (§4)
            host.say("SN 9")
            host.say("PENDING")
            host.say("CONNECTED KK4XYZ N0CALL-10 2300")
            host.say("ENCRYPTION DISABLED")
            host.say("BITRATE (9) 1420 BPS")
            assert _wait(lambda: connected)
            assert connected[0].data["remote_call"] == "KK4XYZ"
            assert _wait(lambda: len(quality) >= 2)
            assert quality[0].data == {"snr_db": 9.0}
            assert quality[1].data == {"level": 9, "bps": 1420}

            broker.send_data(b"Welcome\r")
            assert _wait(lambda: bytes(host.data_rx) == b"Welcome\r")
            host.send_data(b"L\r")
            got = b""
            end = time.time() + 5
            while time.time() < end and got != b"L\r":
                got += broker.recv_data(timeout=0.2) or b""
            assert got == b"L\r"
        finally:
            broker.disconnect()
            host.close()

    def test_requested_bandwidth_sent(self):
        host, broker, _ = self._attach(bandwidth=500)
        try:
            assert _wait(lambda: "LISTEN ON" in host.commands), host.commands
            assert "BW500" in host.commands
        finally:
            broker.disconnect()
            host.close()
