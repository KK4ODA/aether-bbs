#!/usr/bin/env python3
"""
tools/screens.py — screenshots of Aether BBS for the README and radiopost.org.

    python tools/screens.py            # with Aether Term checked out beside this one
    python tools/screens.py --alone    # the board on its own, no caller

Runs the real program offscreen (Qt's offscreen platform: no window is
shown) with sample data in a throw-away folder, and saves PNGs to
docs/screenshots/.  Your own settings, messages and logs are never read
or written.  Nothing is transmitted.

The sample station is N0CALL-10, its sysop N0CALL; the users are N1TEST,
K2TEST and W3TEST (callsigns in a form the FCC never issues).  The
program's internet check (a TCP connection to 8.8.8.8 port 53) is
answered "up" without being sent, so the pictures do not depend on the
network of the machine that makes them.

A session needs a caller.  With Aether Term beside this checkout (or
AETHER_TERM_DIR set), this script runs Term's own tools/screens.py, which
starts Term in local test mode: Term plays the VARA FM modem on
127.0.0.1, this board attaches to it as it would to VARA FM, and Term
calls the board and logs in.  Term's script starts this one with --serve
and tells it when to take each picture, so the dashboard is shown during
a real session.

--serve PORTS (Term's script uses it): the board's one instance is VARA
FM on 127.0.0.1 at the two ports given (CMD,DATA).  Prints "READY" once
the instance listens, then reads commands on stdin: "SHOT <name>" takes
that picture and prints "SHOT <name> <file>"; "QUIT" ends.
"""

import argparse
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SIZE = (1280, 860)              # the window as the program opens it, logical pixels
SCALE = "2"                     # device pixels per logical pixel

STATION = "N0CALL-10"
SYSOP = "N0CALL"
USER, USER_PASSWORD = "N1TEST", "sample-password-1"   # Term's script logs in as this user


def _isolate(home: Path):
    """Point every per-user folder at the throw-away one, before any app
    module is imported (they read it when they first need it)."""
    for var in ("HOME", "USERPROFILE"):
        os.environ[var] = str(home)
    os.environ["APPDATA"] = str(home / "AppData" / "Roaming")
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    os.environ["QT_SCALE_FACTOR"] = SCALE


def _answer_internet_check():
    """The BBS checks its internet link by connecting to 8.8.8.8 port 53
    (the status bar, the notice at log-in, Winlink).  Answer that one
    connection as made, and send nothing."""
    import socket
    real = socket.socket.connect

    def connect(self, address):
        if address == ("8.8.8.8", 53):
            return None
        return real(self, address)
    socket.socket.connect = connect


def _config(work: Path, ports: tuple[int, int] | None) -> str:
    """The sample station's settings, in the program's own file format."""
    inst = ""
    if ports:
        inst = f"""
[[instances]]
name = "Local test"
transport = "VARA_FM"
host = "127.0.0.1"
cmd_port = {ports[0]}
data_port = {ports[1]}
frequency_label = "No radio"
auto_start = false
"""
    else:
        inst = """
[[instances]]
name = "VHF FM"
transport = "VARA_FM"
host = "127.0.0.1"
cmd_port = 47300
data_port = 47301
frequency_label = "145.050 MHz"
auto_start = false
"""
    return f"""
[station]
bbs_callsign = "{STATION}"
sysop_callsign = "{SYSOP}"
grid_square = "FN31"
beacon_text = "Aether BBS (sample)"

[bbs]
message_dir = "{(work / 'messages').as_posix()}"
welcome_message = "Welcome to the N0CALL-10 sample board. Type H for help."

[logging]
level = "INFO"
log_dir = "{(work / 'logs').as_posix()}"

[updater]
enabled = false
{inst}"""


# Sample traffic: (from, to, subject, body, days ago, area)
MESSAGES = [
    ("K2TEST", USER, "Net tonight",
     "Hi,\n\nThe FM net is at 2000 local on the usual repeater.\n"
     "Please check in if you can.\n\n73, K2TEST\n", 0.15, "GENERAL"),
    ("W3TEST", USER, "Antenna parts",
     "I have the coax and the mast clamps you asked about.\n"
     "Pick them up any evening this week.\n\n73\n", 1.2, "GENERAL"),
    ("K2TEST", "W3TEST", "Drill on Saturday",
     "Saturday's drill starts at 0900. Bring a radio, a power supply and\n"
     "something to write on.\n", 2.4, "GENERAL"),
    (USER, "K2TEST", "Re: Net tonight", "Will check in. 73\n", 3.1, "GENERAL"),
    ("W3TEST", "K2TEST", "Repeater battery",
     "The repeater's battery read 12.1 V this morning. I will swap it\n"
     "on Sunday.\n", 0.9, "GENERAL"),
    ("K2TEST", SYSOP, "Access for N4TEST",
     "N4TEST is a new member of the club and asked for an account.\n", 1.9, "GENERAL"),
    (SYSOP, "W3TEST", "Re: Repeater battery", "Thanks. I can help on Sunday.\n",
     0.5, "GENERAL"),
]
BULLETINS = [
    ("K2TEST", "ALL", "Drill on Saturday at 0900",
     "The emergency communications drill is Saturday at 0900 local.\n"
     "Stations check in on the board or on the FM net.\n", 0.6, "ALL", False),
    ("W3TEST", "WX", "Wind advisory tonight",
     "Gusts to 45 mph after 1800. Secure antennas and masts.\n", 0.3, "WX", True),
    ("N0CALL", "LOCAL", "Board hours",
     "The board runs around the clock. Messages are kept 30 days.\n", 5.0, "LOCAL", False),
    ("W3TEST", "DX", "Six metres open in the evenings",
     "Sporadic E most evenings this week, 1800-2100 local.\n", 1.7, "DX", False),
]


def _seed(store, bulletins, access, instance):
    """Users, messages, bulletins and a few heard stations, through the
    program's own stores.  Dates are spread over the past days."""
    from bbs.access import Role
    for call, role in ((USER, Role.APPROVED), ("K2TEST", Role.TRUSTED),
                       ("W3TEST", Role.APPROVED)):
        access.set_role(call, role, changed_by=SYSOP)
    access.set_password(USER, USER_PASSWORD)
    access.register("N4TEST", instance="Local test", community_ref="Sample registration")

    now = datetime.now(timezone.utc)
    dated = []
    for frm, to, subj, body, days, area in MESSAGES:
        dated.append((store.save_message(frm, to, subj, body.encode(), area=area), days))
    for frm, to, subj, body, days, area, urgent in BULLETINS:
        dated.append((bulletins.save_bulletin(frm, to, subj, body.encode(), area=area,
                                              is_urgent=urgent), days))
    with store.lock:
        for mid, days in dated:
            when = (now - timedelta(days=days)).isoformat()
            store.conn.execute("UPDATE messages SET date=?, created_at=? WHERE id=?",
                               (when, when, mid))
        store.conn.commit()
    for call, ssid in (("K2TEST", 0), ("W3TEST", 7)):
        store.upsert_heard_station(call, ssid, instance, last_event="CONNECTED")


class Board:
    """The program, built as main.py builds it, in this process."""

    def __init__(self, work: Path, ports, out: Path):
        self.out = out
        cfg_dir = Path(os.environ["HOME"]) / ".config" / "aether-bbs"
        if sys.platform == "win32":
            cfg_dir = Path(os.environ["APPDATA"]) / "aether-bbs"
        cfg_dir.mkdir(parents=True, exist_ok=True)
        (cfg_dir / "aether_bbs.toml").write_text(_config(work, ports), encoding="utf-8")

        sys.path.insert(0, str(ROOT))
        _answer_internet_check()
        from PyQt6.QtWidgets import QApplication
        from PyQt6.QtCore import Qt
        QApplication.setHighDpiScaleFactorRoundingPolicy(
            Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
        self.qt = QApplication.instance() or QApplication([sys.argv[0]])
        self.qt.setApplicationName("Aether BBS")
        from gui.theme import apply_theme
        apply_theme(self.qt)

        import main as bbs_main
        app = self.app = bbs_main.BBSApplication()
        app._load_config()
        app._open_store_and_security()
        app._build_bulletins_and_modems()
        _seed(app.message_store, app.bulletin_store, app.access_controller,
              app.config.instances[0].name)
        app._build_dispatcher()
        app._build_internet_links()
        app._build_notifiers()
        app._build_gui()
        app._start_services()
        app._follow_config_changes()
        self.win = app.main_window
        self.win.resize(*SIZE)

    def pump(self, seconds=0.3, until=None, timeout=30.0):
        end = time.time() + (timeout if until else seconds)
        while time.time() < end:
            self.qt.processEvents()
            if until and until():
                return True
            time.sleep(0.02)
        return until is None

    def state(self) -> str:
        muxes = self.app.instance_manager.all_muxes()
        mux = next(iter(muxes.values()), None)
        return mux.state.name if mux else ""

    # ── the pictures ─────────────────────────────────────────────────

    def _show(self, outer: str, inner: str | None = None):
        w = self.win
        tabs = w._tabs
        for i in range(tabs.count()):
            if tabs.tabText(i) == outer:
                tabs.setCurrentIndex(i)
                page = tabs.widget(i)
                if inner and hasattr(page, "count"):
                    for j in range(page.count()):
                        if page.tabText(j) == inner:
                            page.setCurrentIndex(j)
                break
        else:
            raise SystemExit(f"no tab {outer!r}")

    def _messages_select(self, row=0):
        t = self.win._messages_tab
        t.refresh()
        table = getattr(t, "_table", None)
        if table is not None and table.rowCount() > row:
            table.selectRow(row)

    def shot(self, name: str) -> Path:
        views = {
            "dashboard": lambda: self._show("Dashboard"),
            "messages": lambda: (self._show("Messages", "Messages"), self._messages_select()),
            "bulletins": lambda: self._show("Messages", "Bulletins"),
        }
        if name not in views:
            raise SystemExit(f"no picture {name!r}; there are {', '.join(views)}")
        views[name]()
        # the dashboard's counters are refreshed every 5 seconds
        self.pump(5.5 if name == "dashboard" else 0.8)
        path = self.out / f"bbs-{name}.png"
        self.win.grab().save(str(path))
        return path

    def stop(self):
        # not close(): the window asks "Stop the BBS and quit?"
        self.win.hide()
        self.app._shutdown()


def _find_term() -> Path | None:
    for c in (os.environ.get("AETHER_TERM_DIR", ""), ROOT.parent / "aether-term"):
        if c and (Path(c) / "tools" / "screens.py").is_file():
            return Path(c)
    return None


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", type=Path, default=ROOT / "docs" / "screenshots")
    ap.add_argument("--alone", action="store_true", help="no caller: the board on its own")
    ap.add_argument("--serve", metavar="CMD,DATA",
                    help="for Aether Term's script: attach to its local test modem")
    a = ap.parse_args()
    a.out = a.out.resolve()
    a.out.mkdir(parents=True, exist_ok=True)

    if not a.serve and not a.alone:
        term = _find_term()
        if term is None:
            sys.exit("Aether Term is not beside this checkout (set AETHER_TERM_DIR), "
                     "or run with --alone for pictures without a session.")
        # Term drives: it starts this script with --serve and calls the board
        sys.exit(subprocess.call([sys.executable, str(term / "tools" / "screens.py"),
                                  "--bbs", str(ROOT), "--bbs-out", str(a.out)]))

    tmp = Path(tempfile.mkdtemp(prefix="aether-bbs-screens-"))
    _isolate(tmp / "home")
    work = tmp / "work"
    work.mkdir(parents=True)
    os.chdir(work)
    ports = tuple(int(p) for p in a.serve.split(",")) if a.serve else None
    board = Board(work, ports, a.out)
    try:
        if not a.serve:
            board.pump(2.0)
            for name in ("dashboard", "messages", "bulletins"):
                print(board.shot(name))
            return
        if not board.pump(until=lambda: board.state() == "LISTENING", timeout=40):
            sys.exit(f"the instance did not attach: {board.state()}")
        print("READY", flush=True)
        # stdin is read on a thread, so the window keeps updating between commands
        lines: queue.Queue = queue.Queue()
        threading.Thread(target=lambda: [lines.put(x) for x in sys.stdin] + [lines.put("QUIT")],
                         daemon=True).start()
        while True:
            board.pump(0.1)
            try:
                cmd = lines.get_nowait().split()
            except queue.Empty:
                continue
            if cmd[:1] == ["QUIT"]:
                break
            if cmd[:1] == ["SHOT"] and len(cmd) == 2:
                print(f"SHOT {cmd[1]} {board.shot(cmd[1])}", flush=True)
    finally:
        board.stop()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
