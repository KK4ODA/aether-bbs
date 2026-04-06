"""
gui/messages_tab.py

Messages tab — browse, read, reply, delete messages.

New capabilities over the original:
  • Double-click any row to open the message in a standalone MessageViewerWindow.
  • Unread messages are visually distinguished (bold, accent colour).
  • Priority column shows Normal / ⚡Priority / 🚨Emergency.
  • Replied indicator column.
  • Opening a message automatically marks it as Read.
  • Multiple message windows can be open simultaneously (tracked by MID).
  • Reply button in the viewer is pre-addressed and supports canned responses.
"""

import logging
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QSplitter,
    QTableWidget, QTableWidgetItem, QHeaderView,
    QTextEdit, QPushButton, QLabel, QComboBox,
    QLineEdit, QMessageBox, QAbstractItemView,
)
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QFont, QColor, QBrush

from bbs.store import MessageStore
from core.config import AppConfig

log = logging.getLogger(__name__)

_BG_MEDIUM   = "#313338"
_ACCENT      = "#5865f2"
_GREEN       = "#23a55a"
_RED         = "#f23f43"
_YELLOW      = "#f0b232"
_TEXT_PRI    = "#dbdee1"
_TEXT_MUT    = "#80848e"
_BORDER      = "#4e505a"

_COL_UNREAD_BG  = QColor("#1a2540")
_COL_UNREAD_FG  = QColor(_TEXT_PRI)
_COL_PRI_FG     = QColor(_YELLOW)
_COL_EMERG_FG   = QColor(_RED)
_COL_READ_FG    = QColor(_TEXT_MUT)
_COL_REPLIED_FG = QColor(_GREEN)


class MessagesTab(QWidget):

    def __init__(self, store: MessageStore, config: AppConfig = None, parent=None):
        super().__init__(parent)
        self._store  = store
        self._config = config
        self._open_viewers: dict = {}
        self._build_ui()
        self.refresh()

    # ── UI construction ───────────────────────────────────────────────────

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        # Controls bar
        ctrl = QHBoxLayout()
        ctrl.addWidget(QLabel("Area:"))
        self._area_combo = QComboBox()
        self._area_combo.addItem("All areas")
        self._area_combo.currentTextChanged.connect(self.refresh)
        self._area_combo.setFixedWidth(160)
        ctrl.addWidget(self._area_combo)

        ctrl.addWidget(QLabel("Show:"))
        self._filter_combo = QComboBox()
        self._filter_combo.addItems([
            "All messages", "Unread only", "Replied",
            "CMS Queued", "CMS Sent", "CMS Failed",
        ])
        self._filter_combo.currentTextChanged.connect(self.refresh)
        self._filter_combo.setFixedWidth(140)
        ctrl.addWidget(self._filter_combo)

        refresh_btn = QPushButton("🔄 Refresh")
        refresh_btn.clicked.connect(self.refresh)
        ctrl.addWidget(refresh_btn)

        expire_btn = QPushButton("🗑 Expire Old")
        expire_btn.clicked.connect(self._expire_old)
        ctrl.addWidget(expire_btn)

        ctrl.addStretch()

        self._unread_lbl = QLabel("")
        self._unread_lbl.setStyleSheet(
            f"color:{_YELLOW};font-weight:bold;font-size:9pt;"
        )
        ctrl.addWidget(self._unread_lbl)
        layout.addLayout(ctrl)

        hint = QLabel("Double-click to open  ·  Bold = Unread  ·  ⚡ = Priority  ·  🚨 = Emergency  ·  CMS = Winlink status")
        hint.setStyleSheet(f"color:{_TEXT_MUT};font-size:8pt;")
        layout.addWidget(hint)

        # Message table: #, Status, Priority, From, To, Subject, Date, Area, CMS
        self._msg_table = QTableWidget(0, 9)
        self._msg_table.setHorizontalHeaderLabels(
            ["#", "Status", "⚡", "From", "To", "Subject", "Date", "Area", "CMS"]
        )
        hdr = self._msg_table.horizontalHeader()
        hdr.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        hdr.setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)  # Subject fills
        hdr.setStretchLastSection(False)
        # Initial column widths: #, Status, Pri, From, To, Subject(stretch), Date, Area, CMS
        for col, w in [(0, 36), (1, 55), (2, 30), (3, 90), (4, 90),
                       (6, 130), (7, 70), (8, 55)]:
            hdr.resizeSection(col, w)

        self._msg_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._msg_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._msg_table.setAlternatingRowColors(True)
        self._msg_table.verticalHeader().setVisible(False)
        self._msg_table.setShowGrid(False)
        self._msg_table.setWordWrap(False)
        self._msg_table.setSortingEnabled(True)
        self._msg_table.setMinimumHeight(250)
        self._msg_table.itemSelectionChanged.connect(self._on_selection)
        self._msg_table.itemDoubleClicked.connect(self._on_double_click)
        layout.addWidget(self._msg_table, 1)

        # Preview / actions strip
        preview_frame = QWidget()
        preview_frame.setStyleSheet(
            f"background:{_BG_MEDIUM};border-radius:6px;"
        )
        pv_layout = QVBoxLayout(preview_frame)
        pv_layout.setContentsMargins(8, 6, 8, 6)
        pv_layout.setSpacing(4)

        self._header_lbl = QLabel("")
        self._header_lbl.setStyleSheet(
            f"font-family:'Courier New';font-size:9pt;color:{_TEXT_MUT};"
        )
        pv_layout.addWidget(self._header_lbl)

        pv_btn_row = QHBoxLayout()
        self._open_btn = QPushButton("📖  Open Message")
        self._open_btn.setEnabled(False)
        self._open_btn.setStyleSheet(
            f"QPushButton:enabled  {{ background:{_ACCENT};color:white;font-weight:bold;"
            f"border:none;border-radius:4px;padding:6px 18px; }}"
            f"QPushButton:disabled {{ background:{_BG_MEDIUM};color:{_TEXT_MUT};"
            f"border:1px solid {_BORDER};border-radius:4px;padding:6px 18px; }}"
        )
        self._open_btn.clicked.connect(self._open_selected)
        pv_btn_row.addWidget(self._open_btn)

        self._del_btn = QPushButton("🗑 Delete")
        self._del_btn.setEnabled(False)
        self._del_btn.setStyleSheet(
            f"QPushButton:enabled  {{ background:{_RED};color:white;font-weight:bold;"
            f"border:none;border-radius:4px;padding:6px 14px; }}"
            f"QPushButton:disabled {{ background:{_BG_MEDIUM};color:{_TEXT_MUT};"
            f"border:1px solid {_BORDER};border-radius:4px;padding:6px 14px; }}"
        )
        self._del_btn.clicked.connect(self._delete_selected)
        pv_btn_row.addWidget(self._del_btn)
        pv_btn_row.addStretch()

        self._status_lbl = QLabel("")
        self._status_lbl.setStyleSheet(f"color:{_TEXT_MUT};font-size:9pt;")
        pv_btn_row.addWidget(self._status_lbl)
        pv_layout.addLayout(pv_btn_row)
        layout.addWidget(preview_frame)

    # ── Data loading ──────────────────────────────────────────────────────

    def refresh(self):
        area_text   = self._area_combo.currentText()
        filter_text = self._filter_combo.currentText()
        area = None if area_text == "All areas" else area_text

        all_msgs = self._store.list_messages(area=area, limit=500)

        if filter_text == "Unread only":
            msgs = [m for m in all_msgs if not m["is_read"]]
        elif filter_text == "Replied":
            msgs = [m for m in all_msgs if m.get("is_replied", 0)]
        elif filter_text == "CMS Queued":
            msgs = [m for m in all_msgs if m.get("cms_status") == "QUEUED"]
        elif filter_text == "CMS Sent":
            msgs = [m for m in all_msgs if m.get("cms_status") in ("SENT", "REJECTED")]
        elif filter_text == "CMS Failed":
            msgs = [m for m in all_msgs if m.get("cms_status") == "FAILED"]
        else:
            msgs = list(all_msgs)

        unread_count = sum(1 for m in all_msgs if not m["is_read"])
        self._unread_lbl.setText(
            f"  {unread_count} unread" if unread_count else ""
        )

        self._msg_table.setSortingEnabled(False)
        self._msg_table.setRowCount(0)
        for i, m in enumerate(msgs):
            row = self._msg_table.rowCount()
            self._msg_table.insertRow(row)
            self._populate_row(row, i + 1, m)
        self._msg_table.setSortingEnabled(True)

        # Refresh area combo (preserve selection)
        areas = self._store.list_areas()
        self._area_combo.blockSignals(True)
        cur = self._area_combo.currentText()
        self._area_combo.clear()
        self._area_combo.addItem("All areas")
        for a in areas:
            self._area_combo.addItem(a)
        idx = self._area_combo.findText(cur)
        if idx >= 0:
            self._area_combo.setCurrentIndex(idx)
        self._area_combo.blockSignals(False)

    def _populate_row(self, row: int, seq: int, m):
        is_read    = bool(m["is_read"])
        is_replied = bool(m.get("is_replied", 0) or 0)
        priority   = int(m.get("priority", 0) or 0)

        if is_replied:
            status_str = "↩ Replied"
        elif is_read:
            status_str = "✓ Read"
        else:
            status_str = "● Unread"

        pri_map = {0: "", 1: "⚡ Priority", 2: "🚨 Emergency"}
        pri_str = pri_map.get(priority, "")

        # CMS delivery status display
        cms_status = m.get("cms_status", "") or ""
        _CMS_DISPLAY = {
            "":         "",
            "QUEUED":   "📤 Queued",
            "SENDING":  "⏳ Sending",
            "SENT":     "✅ Sent",
            "REJECTED": "🔁 Duplicate",
            "FAILED":   "❌ Failed",
            "RECEIVED": "📥 CMS",
            "LOCAL":    "📻 Local",
        }
        cms_str = _CMS_DISPLAY.get(cms_status, cms_status)

        cells = [
            (0, str(seq)),
            (1, status_str),
            (2, pri_str),
            (3, m["from_call"]),
            (4, m["to_call"]),
            (5, m.get("subject") or "(no subject)"),
            (6, str(m.get("date", ""))[:16]),
            (7, m["area"]),
            (8, cms_str),
        ]
        for col, text in cells:
            item = QTableWidgetItem(text)
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self._msg_table.setItem(row, col, item)

        self._msg_table.item(row, 0).setData(Qt.ItemDataRole.UserRole, m["id"])

        for col in range(9):
            item = self._msg_table.item(row, col)
            if not item:
                continue
            if priority == 2:
                item.setForeground(QBrush(_COL_EMERG_FG))
            elif priority == 1:
                item.setForeground(QBrush(_COL_PRI_FG))
            elif is_replied:
                item.setForeground(QBrush(_COL_REPLIED_FG))
            elif not is_read:
                item.setBackground(QBrush(_COL_UNREAD_BG))
                item.setForeground(QBrush(_COL_UNREAD_FG))
                if col == 5:
                    font = item.font()
                    font.setBold(True)
                    item.setFont(font)
            else:
                item.setForeground(QBrush(_COL_READ_FG))

        # Color the CMS status cell specifically
        cms_item = self._msg_table.item(row, 8)
        if cms_item and cms_status:
            _CMS_COLORS = {
                "QUEUED":   QColor("#f39c12"),   # amber
                "SENDING":  QColor("#f39c12"),
                "SENT":     QColor("#2ecc71"),   # green
                "REJECTED": QColor("#95a5a6"),   # grey
                "FAILED":   QColor("#e74c3c"),   # red
                "RECEIVED": QColor("#5dade2"),   # blue
                "LOCAL":    QColor("#aaa"),
            }
            color = _CMS_COLORS.get(cms_status)
            if color:
                cms_item.setForeground(QBrush(color))

    # ── Selection / interaction ───────────────────────────────────────────

    def _get_selected_mid(self):
        row = self._msg_table.currentRow()
        if row < 0:
            return None
        item = self._msg_table.item(row, 0)
        if not item:
            return None
        return item.data(Qt.ItemDataRole.UserRole)

    def _on_selection(self):
        mid = self._get_selected_mid()
        if not mid:
            self._open_btn.setEnabled(False)
            self._del_btn.setEnabled(False)
            self._header_lbl.setText("")
            self._status_lbl.setText("")
            return

        self._open_btn.setEnabled(True)
        self._del_btn.setEnabled(True)

        msg = self._store.get_message(mid)
        if msg:
            self._header_lbl.setText(
                f"MID: {msg['id']}   From: {msg['from_call']}   "
                f"To: {msg['to_call']}   {str(msg['date'])[:19]}\n"
                f"Subject: {msg['subject'] or '(no subject)'}"
            )
            parts = []
            if msg.get("is_replied"):
                parts.append("Replied")
            elif msg["is_read"]:
                parts.append("Read")
            else:
                parts.append("Unread")
            pri = int(msg.get("priority", 0) or 0)
            if pri:
                parts.append(["", "⚡ Priority", "🚨 Emergency"][min(pri, 2)])
            self._status_lbl.setText("  ·  ".join(parts))

    def _on_double_click(self, item):
        self._open_selected()

    def _open_selected(self):
        mid = self._get_selected_mid()
        if not mid:
            return
        if mid in self._open_viewers:
            viewer = self._open_viewers[mid]
            viewer.raise_()
            viewer.activateWindow()
            return
        self._launch_viewer(mid)

    def _launch_viewer(self, mid: str):
        from gui.message_viewer import MessageViewerWindow
        our_call = (
            self._config.station.bbs_callsign
            if self._config else "N0CALL"
        )
        viewer = MessageViewerWindow(
            store=self._store,
            mid=mid,
            our_call=our_call,
            config=self._config,
            parent=None,
        )
        self._open_viewers[mid] = viewer
        viewer.closed.connect(lambda m: self._on_viewer_closed(m))
        viewer.reply_sent.connect(lambda _: self.refresh())
        viewer.show()
        viewer.raise_()
        QTimer.singleShot(200, self.refresh)

    def _on_viewer_closed(self, mid: str):
        self._open_viewers.pop(mid, None)
        self.refresh()

    def open_message_by_mid(self, mid: str):
        """Called from MainWindow (e.g. after priority alert acknowledgement)."""
        if mid in self._open_viewers:
            viewer = self._open_viewers[mid]
            viewer.raise_()
            viewer.activateWindow()
            return
        self._launch_viewer(mid)

    # ── Delete / expire ───────────────────────────────────────────────────

    def _delete_selected(self):
        mid = self._get_selected_mid()
        if not mid:
            return
        reply = QMessageBox.question(
            self, "Delete Message", f"Delete message {mid}?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self._store.delete_message(mid)
            self.refresh()

    def _expire_old(self):
        self._store.expire_old_messages()
        self.refresh()
        QMessageBox.information(self, "Done", "Old messages expired.")
