"""
gui/aether_heard.py

Dashboard table of the stations Aether HF has heard: last and best SNR, when,
what they were doing, dial and bandwidth.  Fed by AetherMonitor's
AETHER_HEARD events (Aether's ``heard.list``, every ~10 s).  Right-click a
station to probe it.
"""

import json
import time

from PyQt6.QtCore import Qt, QTimer, pyqtSignal, pyqtSlot
from PyQt6.QtWidgets import (
    QAbstractItemView, QGroupBox, QHeaderView, QLabel, QMenu,
    QTableWidget, QTableWidgetItem, QVBoxLayout,
)

from gui.theme import TEXT_MUTED

MAX_ROWS = 50

COLUMNS = ["Callsign", "SNR", "Best", "Heard", "Activity", "Dial", "BW", "Count"]
INSTANCE_COLUMN = "Instance"


def format_age(ms: int, now_ms: int) -> str:
    """'just now', '42 s ago', '5 min ago', '3 h ago', '2 d ago'."""
    if not ms:
        return "—"
    secs = max(0, (now_ms - ms) // 1000)
    if secs < 10:
        return "just now"
    if secs < 60:
        return f"{secs} s ago"
    if secs < 3600:
        return f"{secs // 60} min ago"
    if secs < 86400:
        return f"{secs // 3600} h ago"
    return f"{secs // 86400} d ago"


def format_snr(db) -> str:
    return "—" if db is None else f"{db:+.1f} dB"


def format_dial(hz) -> str:
    return "—" if not hz else f"{hz / 1e6:.4f} MHz"


class _Item(QTableWidgetItem):
    """A cell that sorts by a value of its own (numbers, timestamps)."""

    def __init__(self, text: str, key=None, align_right=False):
        super().__init__(text)
        self._key = key if key is not None else text
        if align_right:
            self.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

    def __lt__(self, other):
        if isinstance(other, _Item):
            a, b = self._key, other._key
            if type(a) is type(b):
                return a < b
            # a missing value (None → -inf) sorts below any number
            return (a if isinstance(a, (int, float)) else float("-inf")) < (
                b if isinstance(b, (int, float)) else float("-inf"))
        return super().__lt__(other)


class AetherHeardPanel(QGroupBox):
    """'Aether HF — Stations Heard' group on the dashboard."""

    # (instance, callsign): the sysop asked to probe a station
    probe_requested = pyqtSignal(str, str)

    def __init__(self, parent=None):
        super().__init__("Aether HF — Stations Heard", parent)
        self._by_instance: dict[str, list[dict]] = {}
        self._build_ui()
        self._age_timer = QTimer(self)
        self._age_timer.timeout.connect(self._render)
        self._age_timer.start(10_000)

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 2, 4, 2)
        layout.setSpacing(2)

        self._empty = QLabel(
            "Nobody heard yet.  Stations appear here as soon as Aether HF hears "
            "a beacon, a call, a probe, an answer or a session.")
        self._empty.setWordWrap(True)
        self._empty.setStyleSheet(f"color:{TEXT_MUTED}; font-weight:normal;")
        layout.addWidget(self._empty)

        self._table = QTableWidget(0, len(COLUMNS))
        self._table.setHorizontalHeaderLabels(COLUMNS)
        hdr = self._table.horizontalHeader()
        hdr.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        hdr.setStretchLastSection(True)
        self._table.verticalHeader().setVisible(False)
        self._table.verticalHeader().setDefaultSectionSize(22)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setAlternatingRowColors(True)
        self._table.setSortingEnabled(True)
        self._table.sortByColumn(COLUMNS.index("Heard"), Qt.SortOrder.DescendingOrder)
        self._table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._table.customContextMenuRequested.connect(self._on_context_menu)
        self._table.setSizeAdjustPolicy(
            QAbstractItemView.SizeAdjustPolicy.AdjustToContents)
        self._table.setMaximumHeight(320)      # ~12 rows, then it scrolls
        self._table.hide()
        layout.addWidget(self._table)

    # ── data ─────────────────────────────────────────────────────────────

    @pyqtSlot(str, str)
    def set_stations(self, instance: str, payload: str):
        """Replace one instance's list (payload: JSON of AETHER_HEARD data)."""
        try:
            stations = json.loads(payload).get("stations") or []
        except (ValueError, AttributeError):
            return
        self._by_instance[instance] = stations
        self._render()

    def keep_instances(self, names):
        """Forget instances that are no longer configured."""
        for gone in set(self._by_instance) - set(names):
            del self._by_instance[gone]
        self._render()

    def rows(self) -> list[tuple[str, dict]]:
        """(instance, station) pairs, most recently heard first."""
        out = [(inst, st) for inst, sts in self._by_instance.items() for st in sts]
        out.sort(key=lambda p: p[1].get("last_heard_ms") or 0, reverse=True)
        return out[:MAX_ROWS]

    # ── rendering ────────────────────────────────────────────────────────

    def _columns(self) -> list[str]:
        multi = len([i for i, s in self._by_instance.items() if s]) > 1
        return COLUMNS + ([INSTANCE_COLUMN] if multi else [])

    def _render(self):
        rows = self.rows()
        self._empty.setVisible(not rows)
        self._table.setVisible(bool(rows))

        cols = self._columns()
        sort_col = self._table.horizontalHeader().sortIndicatorSection()
        sort_order = self._table.horizontalHeader().sortIndicatorOrder()
        selected = {(it.data(Qt.ItemDataRole.UserRole), it.text())
                    for it in self._table.selectedItems() if it.column() == 0}

        self._table.setSortingEnabled(False)
        if self._table.columnCount() != len(cols):
            self._table.setColumnCount(len(cols))
            self._table.setHorizontalHeaderLabels(cols)
        self._table.setRowCount(len(rows))
        now = int(time.time() * 1000)
        for r, (inst, st) in enumerate(rows):
            call = _Item(st["callsign"])
            call.setData(Qt.ItemDataRole.UserRole, inst)
            call.setToolTip(self._tooltip(inst, st))
            f = call.font()
            f.setBold(True)
            call.setFont(f)
            activity = st.get("activity") or ""
            if st.get("detail"):
                activity += f" → {st['detail']}"
            cells = [
                call,
                _Item(format_snr(st.get("snr_db")), st.get("snr_db"), True),
                _Item(format_snr(st.get("best_snr_db")), st.get("best_snr_db"), True),
                _Item(format_age(st.get("last_heard_ms") or 0, now),
                      st.get("last_heard_ms") or 0),
                _Item(activity),
                _Item(format_dial(st.get("frequency_hz")), st.get("frequency_hz"), True),
                _Item(str(st["bandwidth_hz"]) if st.get("bandwidth_hz") else "—",
                      st.get("bandwidth_hz"), True),
                _Item(str(st.get("count") or 0), st.get("count") or 0, True),
            ]
            if INSTANCE_COLUMN in cols:
                cells.append(_Item(inst))
            for c, item in enumerate(cells):
                self._table.setItem(r, c, item)
            if (inst, st["callsign"]) in selected:
                self._table.selectRow(r)
        self._table.setSortingEnabled(True)
        self._table.sortByColumn(sort_col, sort_order)

    @staticmethod
    def _tooltip(inst: str, st: dict) -> str:
        lines = [f"{st['callsign']} — heard by {inst}"]
        if st.get("first_heard_ms"):
            first = time.strftime("%Y-%m-%d %H:%M",
                                  time.localtime(st["first_heard_ms"] / 1000))
            lines.append(f"First heard {first}")
        if st.get("beacons"):
            lines.append(f"{st['beacons']} beacon(s) heard")
        if st.get("connected"):
            lines.append("A session with this station has been up from here")
        lines.append("Right-click to probe it")
        return "\n".join(lines)

    # ── actions ──────────────────────────────────────────────────────────

    def _on_context_menu(self, pos):
        item = self._table.itemAt(pos)
        if item is None:
            return
        call_item = self._table.item(item.row(), 0)
        inst = call_item.data(Qt.ItemDataRole.UserRole)
        call = call_item.text()
        menu = QMenu(self)
        probe = menu.addAction(f"Probe {call}")
        probe.setToolTip("Ask the station whether it hears us, and how well")
        if menu.exec(self._table.viewport().mapToGlobal(pos)) is probe:
            self.probe_requested.emit(inst, call)
