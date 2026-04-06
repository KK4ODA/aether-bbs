"""
gui/packet_monitor.py

Activity Monitor tab.

VARA FM is a connected-mode ARQ modem — it handles AX.25 framing internally
and never exposes raw frames to the application.  The only addressing
information the app sees is what VARA FM surfaces through its command channel:
CONNECTED <local> <remote>, DISCONNECTED, PTT TRUE/FALSE, BUSY TRUE/FALSE, etc.

This tab shows:
  - Live activity feed  (connection events, session type, PTT, channel busy,
                         TX/RX text lines and binary transfer summaries)
  - Session log         (completed sessions from the database)
  - MHeard table        (stations that have connected, with counts)
"""

import logging
from datetime import datetime, timezone

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QSplitter,
    QTableWidget, QTableWidgetItem, QHeaderView,
    QLabel, QPushButton, QComboBox, QGroupBox,
    QTextEdit,
)
from PyQt6.QtCore import Qt, QMetaObject, Q_ARG, pyqtSlot
from PyQt6.QtGui import QColor, QFont

from core.events import EventBus, EventType
from vara.instance_manager import VARAFMInstanceManager
from bbs.store import MessageStore

log = logging.getLogger(__name__)

MAX_ACTIVITY_LINES = 500


def _utc_to_local(iso: str) -> str:
    try:
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(iso)[:19]


def _duration(start_iso: str, end_iso) -> str:
    try:
        start = datetime.fromisoformat(start_iso)
        end   = datetime.fromisoformat(end_iso) if end_iso else datetime.now(timezone.utc)
        secs  = int((end - start).total_seconds())
        if secs < 60:    return f"{secs}s"
        if secs < 3600:  return f"{secs // 60}m {secs % 60}s"
        return f"{secs // 3600}h {(secs % 3600) // 60}m"
    except Exception:
        return "—"


class ActivityMonitorTab(QWidget):

    def __init__(self, im: VARAFMInstanceManager, store: MessageStore,
                 bus: EventBus, parent=None):
        super().__init__(parent)
        self._im    = im
        self._store = store
        self._bus   = bus
        self._filter_inst = ""
        self._build_ui()
        self._wire_events()
        self._refresh_sessions()
        self._refresh_mheard()

    # ── UI ────────────────────────────────────────────────────────────────

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(6)

        # Controls row
        ctrl = QHBoxLayout()
        ctrl.addWidget(QLabel("Instance:"))
        self._inst_combo = QComboBox()
        self._inst_combo.addItem("All instances")
        for name in self._im.instance_names():
            self._inst_combo.addItem(name)
        self._inst_combo.currentTextChanged.connect(self._on_inst_filter)
        ctrl.addWidget(self._inst_combo)

        refresh_btn = QPushButton("🔄 Refresh")
        refresh_btn.clicked.connect(self._on_refresh)
        ctrl.addWidget(refresh_btn)

        clear_btn = QPushButton("🗑 Clear Activity")
        clear_btn.clicked.connect(lambda: self._activity.clear())
        ctrl.addWidget(clear_btn)

        ctrl.addStretch()
        layout.addLayout(ctrl)

        # Event filter row
        from PyQt6.QtWidgets import QCheckBox
        filter_row = QHBoxLayout()
        filter_row.addWidget(QLabel("Show:"))

        self._filter_data    = QCheckBox("▲▼ TX/RX data")
        self._filter_session = QCheckBox("📶 Connections")
        self._filter_busy    = QCheckBox("📻 Busy/Clear")
        self._filter_ptt     = QCheckBox("🔴 PTT")
        self._filter_system  = QCheckBox("✅ System")
        self._filter_message = QCheckBox("✉ Messages")

        # Defaults: data and connections on, busy/ptt off (they're noisy)
        self._filter_data.setChecked(True)
        self._filter_session.setChecked(True)
        self._filter_busy.setChecked(False)
        self._filter_ptt.setChecked(False)
        self._filter_system.setChecked(True)
        self._filter_message.setChecked(True)

        for cb in (self._filter_data, self._filter_session, self._filter_busy,
                   self._filter_ptt, self._filter_system, self._filter_message):
            cb.setStyleSheet("QCheckBox { font-size: 8pt; }")
            filter_row.addWidget(cb)

        filter_row.addStretch()
        layout.addLayout(filter_row)

        # Three-pane splitter
        splitter = QSplitter(Qt.Orientation.Vertical)

        # Live activity feed
        act_box = QGroupBox("Live Activity")
        act_layout = QVBoxLayout(act_box)
        self._activity = QTextEdit()
        self._activity.setReadOnly(True)
        self._activity.setFont(QFont("Consolas", 9))
        self._activity.setStyleSheet(
            "QTextEdit { background:#0d1117; color:#c9d1d9; border:none; }"
        )
        act_layout.addWidget(self._activity)
        splitter.addWidget(act_box)

        # Session log
        sess_box = QGroupBox("Session Log")
        sess_layout = QVBoxLayout(sess_box)
        self._sess_table = QTableWidget(0, 7)
        self._sess_table.setHorizontalHeaderLabels([
            "Connected", "Remote", "Instance", "Type",
            "Duration", "Msgs TX", "Msgs RX",
        ])
        hdr = self._sess_table.horizontalHeader()
        hdr.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        hdr.setSectionResizeMode(6, QHeaderView.ResizeMode.Stretch)  # Msgs RX fills
        hdr.setStretchLastSection(False)
        # Initial widths: Connected, Remote, Instance, Type, Duration, Msgs TX
        for col, w in [(0, 130), (1, 90), (2, 100), (3, 55), (4, 65), (5, 60)]:
            hdr.resizeSection(col, w)
        self._sess_table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows)
        self._sess_table.setAlternatingRowColors(True)
        self._sess_table.setSortingEnabled(True)
        sess_layout.addWidget(self._sess_table)
        splitter.addWidget(sess_box)

        # MHeard
        mh_box = QGroupBox("MHeard — Stations That Have Connected")
        mh_layout = QVBoxLayout(mh_box)
        self._mheard_table = QTableWidget(0, 6)
        self._mheard_table.setHorizontalHeaderLabels([
            "Callsign", "Instance", "Last Seen", "Sessions", "Via Path", "Last Event"
        ])
        mh_hdr = self._mheard_table.horizontalHeader()
        mh_hdr.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        mh_hdr.setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)  # Last Event fills
        mh_hdr.setStretchLastSection(False)
        # Initial widths: Callsign, Instance, Last Seen, Sessions, Via Path
        for col, w in [(0, 90), (1, 100), (2, 130), (3, 65), (4, 100)]:
            mh_hdr.resizeSection(col, w)
        self._mheard_table.setAlternatingRowColors(True)
        mh_layout.addWidget(self._mheard_table)
        splitter.addWidget(mh_box)

        splitter.setSizes([200, 260, 140])
        layout.addWidget(splitter)

        self._status_lbl = QLabel("")
        self._status_lbl.setStyleSheet("color:#8b949e; font-size:9pt;")
        layout.addWidget(self._status_lbl)

    # ── Event wiring ──────────────────────────────────────────────────────

    def _wire_events(self):
        sub = self._bus.subscribe
        sub(EventType.SESSION_CONNECTED,    self._on_rf_connect)
        sub(EventType.SESSION_DISCONNECTED, self._on_rf_disconnect)
        sub(EventType.SESSION_STARTED,      self._on_session_started)
        sub(EventType.SESSION_ENDED,        self._on_session_ended)
        sub(EventType.CHANNEL_BUSY,         self._on_busy)
        sub(EventType.PTT_CHANGE,           self._on_ptt)
        sub(EventType.VARA_CONNECTED,       self._on_vara_up)
        sub(EventType.VARA_DISCONNECTED,    self._on_vara_down)
        sub(EventType.VARA_PROCESS_UP,      self._on_proc_up)
        sub(EventType.VARA_PROCESS_DOWN,    self._on_proc_down)
        sub(EventType.MESSAGE_RECEIVED,     self._on_message)
        sub(EventType.STATION_HEARD,        self._on_station_heard)
        sub(EventType.DATA_TX,              self._on_data_tx)
        sub(EventType.DATA_RX,              self._on_data_rx)
        sub(EventType.SESSION_BUSY,         self._on_session_busy)

    # ── Event callbacks (background threads → queue to main thread) ───────

    def _on_rf_connect(self, event):
        call = (event.data or {}).get("remote_call", "?")
        self._log(f"📶  RF CONNECTED    {call} → [{event.source}]", "#2ecc71",
                  category="session")

    def _on_rf_disconnect(self, event):
        self._log(f"📶  RF DISCONNECTED [{event.source}]", "#e74c3c",
                  category="session")
        QMetaObject.invokeMethod(self, "_refresh_sessions",
            Qt.ConnectionType.QueuedConnection)

    def _on_session_started(self, event):
        d    = event.data or {}
        call  = d.get("remote_call", "?")
        stype = d.get("session_type", "interactive")
        self._log(f"🖥  SESSION STARTED {call} ({stype}) [{event.source}]", "#58a6ff",
                  category="session")

    def _on_session_ended(self, event):
        call = (event.data or {}).get("remote_call", "?")
        self._log(f"🖥  SESSION ENDED   {call} [{event.source}]", "#8b949e",
                  category="session")
        QMetaObject.invokeMethod(self, "_refresh_sessions",
            Qt.ConnectionType.QueuedConnection)

    def _on_busy(self, event):
        busy = (event.data or {}).get("busy", False)
        if busy:
            self._log(f"📻  CHANNEL BUSY   [{event.source}]", "#e67e22",
                      category="busy")
        else:
            self._log(f"📻  CHANNEL CLEAR  [{event.source}]", "#8b949e",
                      category="busy")

    def _on_ptt(self, event):
        active = (event.data or {}).get("ptt", False)
        if active:
            self._log(f"🔴  PTT ON         [{event.source}]", "#e74c3c",
                      category="ptt")
        else:
            self._log(f"⚫  PTT OFF        [{event.source}]", "#8b949e",
                      category="ptt")

    def _on_vara_up(self, event):
        self._log(f"✅  VARA FM TCP connected  [{event.source}]", "#2ecc71",
                  category="system")

    def _on_vara_down(self, event):
        self._log(f"❌  VARA FM TCP lost       [{event.source}]", "#e74c3c",
                  category="system")

    def _on_proc_up(self, event):
        self._log(f"▶   VARA FM process started [{event.source}]", "#2ecc71",
                  category="system")

    def _on_proc_down(self, event):
        self._log(f"■   VARA FM process stopped [{event.source}]", "#8b949e",
                  category="system")

    def _on_message(self, event):
        d   = event.data or {}
        mid = d.get("mid", "?")
        frm = d.get("from", "?")
        self._log(f"✉   Message stored  MID={mid} from {frm}", "#f39c12",
                  category="message")

    def _on_station_heard(self, event):
        QMetaObject.invokeMethod(self, "_refresh_mheard",
            Qt.ConnectionType.QueuedConnection)

    def _on_data_tx(self, event):
        d = event.data or {}
        if d.get("raw"):
            n = d.get("bytes", 0)
            self._log(f"  ▲ TX [{event.source}]  «{n} bytes binary»", "#6e7681",
                      category="data")
        else:
            text = d.get("text", "")
            self._log(f"  ▲ TX [{event.source}]  {text}", "#79c0ff",
                      category="data")

    def _on_data_rx(self, event):
        d = event.data or {}
        if d.get("raw"):
            n = d.get("bytes", 0)
            self._log(f"  ▼ RX [{event.source}]  «{n} bytes binary»", "#6e7681",
                      category="data")
        else:
            text = d.get("text", "")
            self._log(f"  ▼ RX [{event.source}]  {text}", "#f0883e",
                      category="data")

    def _on_session_busy(self, event):
        call = (event.data or {}).get("remote_call", "?")
        self._log(f"⛔  BBS BUSY — rejected {call} [{event.source}]", "#da3633",
                  category="session")

    # ── Event filter ──────────────────────────────────────────────────

    # Category constants
    _CAT_DATA    = "data"
    _CAT_SESSION = "session"
    _CAT_BUSY    = "busy"
    _CAT_PTT     = "ptt"
    _CAT_SYSTEM  = "system"
    _CAT_MESSAGE = "message"

    def _is_filtered(self, category: str) -> bool:
        """Return True if this category should be suppressed."""
        filters = {
            self._CAT_DATA:    self._filter_data,
            self._CAT_SESSION: self._filter_session,
            self._CAT_BUSY:    self._filter_busy,
            self._CAT_PTT:     self._filter_ptt,
            self._CAT_SYSTEM:  self._filter_system,
            self._CAT_MESSAGE: self._filter_message,
        }
        cb = filters.get(category)
        return cb is not None and not cb.isChecked()

    # ── Logging helper ────────────────────────────────────────────────────

    def _log(self, text: str, color: str = "#c9d1d9", category: str = "system"):
        if self._is_filtered(category):
            return
        ts = datetime.now().strftime("%H:%M:%S")
        QMetaObject.invokeMethod(self, "_append_activity",
            Qt.ConnectionType.QueuedConnection,
            Q_ARG(str, ts), Q_ARG(str, text), Q_ARG(str, color))

    @pyqtSlot(str, str, str)
    def _append_activity(self, ts: str, text: str, color: str):
        doc = self._activity.document()
        if doc.blockCount() >= MAX_ACTIVITY_LINES:
            cursor = self._activity.textCursor()
            cursor.movePosition(cursor.MoveOperation.Start)
            cursor.select(cursor.SelectionType.LineUnderCursor)
            cursor.removeSelectedText()
            cursor.deleteChar()
        self._activity.append(
            f'<span style="color:#484f58">{ts}</span> '
            f'<span style="color:{color}">{text}</span>'
        )

    # ── Refresh slots (must run on main thread) ───────────────────────────

    def _on_refresh(self):
        self._refresh_sessions()
        self._refresh_mheard()

    @pyqtSlot()
    def _refresh_sessions(self):
        sessions = self._store.list_sessions(limit=200)
        if self._filter_inst:
            sessions = [s for s in sessions if s["instance"] == self._filter_inst]

        self._sess_table.setSortingEnabled(False)
        self._sess_table.setRowCount(0)
        active = 0

        for s in sessions:
            row = self._sess_table.rowCount()
            self._sess_table.insertRow(row)
            is_active = s["disconnected_at"] is None
            if is_active:
                active += 1
            dur = _duration(s["connected_at"], s["disconnected_at"])
            values = [
                _utc_to_local(s["connected_at"]),
                s["remote_call"],
                s["instance"] or "—",
                s["session_type"] or "interactive",
                f"▶ {dur}" if is_active else dur,
                str(s["msgs_sent"]),
                str(s["msgs_recv"]),
            ]
            for col, val in enumerate(values):
                item = QTableWidgetItem(val)
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                if is_active:
                    item.setForeground(QColor("#2ecc71"))
                self._sess_table.setItem(row, col, item)

        self._sess_table.setSortingEnabled(True)
        self._status_lbl.setText(
            f"{len(sessions)} session(s) in log  •  {active} active"
        )

    @pyqtSlot()
    def _refresh_mheard(self):
        inst = self._filter_inst or None
        stations = self._store.get_heard_stations(instance=inst, limit=100)
        self._mheard_table.setRowCount(0)
        for s in stations:
            row = self._mheard_table.rowCount()
            self._mheard_table.insertRow(row)
            call = s["callsign"]
            if s["ssid"]:
                call += f"-{s['ssid']}"
            values = [
                call,
                s["instance"],
                _utc_to_local(s["last_heard"]),
                str(s["hear_count"]),
                s["via_path"] or "direct",
                s["last_event"] or "—",
            ]
            for col, val in enumerate(values):
                item = QTableWidgetItem(val)
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self._mheard_table.setItem(row, col, item)

    def _on_inst_filter(self, text: str):
        self._filter_inst = "" if text == "All instances" else text
        self._refresh_sessions()
        self._refresh_mheard()


# Alias so mainwindow.py import doesn't need changing
PacketMonitorTab = ActivityMonitorTab
