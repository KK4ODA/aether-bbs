"""
gui/bulletins_tab.py

Bulletins Tab — sysop view of the bulletin board.

Three sub-tabs:
  1. Browse  — list and read bulletins, filtered by area
  2. Post    — compose and post a new bulletin
  3. Forwarding — neighbour status and forwarding log
"""

import logging
from typing import Optional

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGroupBox, QLabel, QPushButton, QLineEdit,
    QTextEdit, QComboBox, QTabWidget, QFrame,
    QTableWidget, QTableWidgetItem, QHeaderView,
    QAbstractItemView, QCheckBox,
)
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QFont, QColor

from bbs.bulletins import BulletinStore
from core.config import AppConfig
from gui.theme import (
    BG_DEEPEST, TEXT_PRIMARY, TEXT_SECONDARY, TEXT_MUTED, GREEN, RED, BLUE,
    BTN_SUCCESS, BTN_DANGER, BTN_ACCENT,
)

log = logging.getLogger(__name__)

# ── Local aliases ─────────────────────────────────────────────────────────
BTN_GREEN = BTN_SUCCESS
BTN_BLUE  = BTN_ACCENT
BTN_RED   = BTN_DANGER


def _ts_short(iso: str) -> str:
    try:
        return iso[:16].replace("T", " ")
    except Exception:
        return str(iso)


# ── Browse sub-tab ────────────────────────────────────────────────────────────

class BrowseTab(QWidget):

    def __init__(self, bbs: BulletinStore, config: AppConfig, parent=None):
        super().__init__(parent)
        self._bbs    = bbs
        self._config = config
        self._current_mids: list[str] = []
        self._open_viewers: dict = {}
        self._build_ui()
        self._refresh()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(6)
        layout.setContentsMargins(6, 6, 6, 6)

        # Filter bar
        filter_row = QHBoxLayout()
        filter_row.addWidget(QLabel("Area:"))
        self._area_combo = QComboBox()
        self._area_combo.setMinimumWidth(140)
        self._area_combo.currentTextChanged.connect(self._refresh)
        filter_row.addWidget(self._area_combo)
        filter_row.addStretch()

        self._count_lbl = QLabel("0 bulletins")
        filter_row.addWidget(self._count_lbl)

        refresh_btn = QPushButton("⟳ Refresh")
        refresh_btn.setFixedWidth(90)
        refresh_btn.clicked.connect(self._refresh)
        filter_row.addWidget(refresh_btn)
        layout.addLayout(filter_row)

        # Hint
        hint = QLabel("Double-click to open  ·  ● = Unread  ·  🚨 = Urgent")
        hint.setStyleSheet(f"color:{TEXT_MUTED};font-size:8pt;")
        layout.addWidget(hint)

        # Bulletin list table
        self._table = QTableWidget()
        self._table.setColumnCount(6)
        self._table.setHorizontalHeaderLabels(["#", "!", "From", "Group", "Date", "Subject"])
        hdr = self._table.horizontalHeader()
        hdr.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        hdr.setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)  # Subject fills
        hdr.setStretchLastSection(False)
        # Initial widths: #, !, From, Group, Date, Subject(stretch)
        for col, w in [(0, 36), (1, 28), (2, 90), (3, 70), (4, 130)]:
            hdr.resizeSection(col, w)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setAlternatingRowColors(True)
        self._table.verticalHeader().setVisible(False)
        self._table.setShowGrid(False)
        self._table.itemSelectionChanged.connect(self._on_select)
        self._table.itemDoubleClicked.connect(self._on_double_click)
        layout.addWidget(self._table, 1)

        # Preview / action strip
        preview_frame = QFrame()
        preview_frame.setStyleSheet(f"background:{BG_DEEPEST};border-radius:4px;border:none;")
        pv_layout = QVBoxLayout(preview_frame)
        pv_layout.setContentsMargins(8, 6, 8, 6)
        pv_layout.setSpacing(4)

        self._header_lbl = QLabel("")
        self._header_lbl.setStyleSheet(f"color:{TEXT_SECONDARY};font-size:9pt;")
        pv_layout.addWidget(self._header_lbl)

        btn_row = QHBoxLayout()
        self._open_btn = QPushButton("📖  Open Bulletin")
        self._open_btn.setEnabled(False)
        self._open_btn.setStyleSheet(BTN_BLUE)
        self._open_btn.clicked.connect(self._open_selected)
        btn_row.addWidget(self._open_btn)

        self._del_btn = QPushButton("🗑  Delete")
        self._del_btn.setStyleSheet(BTN_RED)
        self._del_btn.setEnabled(False)
        self._del_btn.clicked.connect(self._delete_selected)
        btn_row.addWidget(self._del_btn)
        btn_row.addStretch()
        pv_layout.addLayout(btn_row)
        layout.addWidget(preview_frame)

    def _refresh(self):
        areas = ["(all)"] + self._bbs.list_bulletin_areas()
        current = self._area_combo.currentText()
        self._area_combo.blockSignals(True)
        self._area_combo.clear()
        self._area_combo.addItems(areas)
        if current in areas:
            self._area_combo.setCurrentText(current)
        self._area_combo.blockSignals(False)

        area = self._area_combo.currentText()
        if area == "(all)":
            area = None
        rows = self._bbs.list_bulletins(area=area, limit=200)
        self._current_mids = [r["id"] for r in rows]
        self._count_lbl.setText(f"{len(rows)} bulletin(s)")

        unread = sum(1 for r in rows if not r["is_read"])
        urgent_count = sum(1 for r in rows if r.get("is_urgent"))
        count_parts = [f"{len(rows)} bulletin(s)"]
        if unread:
            count_parts.append(f"{unread} unread")
        if urgent_count:
            count_parts.append(f"{urgent_count} urgent")
        self._count_lbl.setText("  ·  ".join(count_parts))

        # Sort urgent bulletins to top, then by date descending
        rows.sort(key=lambda r: (not r.get("is_urgent", 0), r.get("date", "")),
                  reverse=False)

        self._table.setSortingEnabled(False)
        self._table.setRowCount(0)
        for i, row in enumerate(rows):
            self._table.insertRow(i)
            is_read = bool(row["is_read"])
            is_urgent = bool(row.get("is_urgent", 0))
            cells = [
                str(i + 1),
                "🚨" if is_urgent else "",
                row["from_call"],
                row["to_call"],
                _ts_short(str(row["date"])),
                row["subject"] or "(no subject)",
            ]
            for col, val in enumerate(cells):
                item = QTableWidgetItem(val)
                if is_urgent:
                    # Urgent: red-amber tint
                    item.setForeground(QColor("#F0B232"))
                    if col == 5:
                        font = item.font()
                        font.setBold(True)
                        item.setFont(font)
                    item.setBackground(QColor("#3D2008"))
                elif not is_read:
                    item.setForeground(QColor("#58A6FF"))
                    if col == 5:
                        font = item.font()
                        font.setBold(True)
                        item.setFont(font)
                else:
                    item.setForeground(QColor(TEXT_MUTED))
                self._table.setItem(i, col, item)
            self._table.item(i, 0).setData(Qt.ItemDataRole.UserRole, row["id"])
        self._table.setSortingEnabled(True)

    def _get_selected_mid(self):
        row = self._table.currentRow()
        if row < 0:
            return None
        item = self._table.item(row, 0)
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def _on_select(self):
        mid = self._get_selected_mid()
        if not mid:
            self._open_btn.setEnabled(False)
            self._del_btn.setEnabled(False)
            self._header_lbl.setText("")
            return
        self._open_btn.setEnabled(True)
        self._del_btn.setEnabled(True)
        msg = self._bbs._store.get_message(mid)
        if msg:
            urgent_tag = "  🚨 URGENT" if msg.get("is_urgent") else ""
            self._header_lbl.setText(
                f"MID: {msg['id']}   From: {msg['from_call']}   "
                f"Group: {msg['to_call']}   {str(msg['date'])[:19]}{urgent_tag}\n"
                f"Subject: {msg['subject'] or '(no subject)'}"
            )

    def _on_double_click(self, item):
        self._open_selected()

    def _open_selected(self):
        mid = self._get_selected_mid()
        if not mid:
            return
        if mid in self._open_viewers:
            w = self._open_viewers[mid]
            w.raise_()
            w.activateWindow()
            return
        self._launch_viewer(mid)

    def _launch_viewer(self, mid: str):
        from gui.message_viewer import MessageViewerWindow
        from PyQt6.QtCore import QTimer
        viewer = MessageViewerWindow(
            store=self._bbs._store,
            mid=mid,
            our_call=self._config.station.bbs_callsign,
            config=self._config,
            parent=None,
        )
        self._open_viewers[mid] = viewer
        viewer.closed.connect(lambda m: self._on_viewer_closed(m))
        viewer.show()
        viewer.raise_()
        QTimer.singleShot(200, self._refresh)

    def _on_viewer_closed(self, mid: str):
        self._open_viewers.pop(mid, None)
        self._refresh()

    def _delete_selected(self):
        mid = self._get_selected_mid()
        if not mid:
            return
        self._bbs._store.delete_message(mid)
        self._refresh()
        self._header_lbl.setText("")
        self._open_btn.setEnabled(False)
        self._del_btn.setEnabled(False)



# ── Post sub-tab ──────────────────────────────────────────────────────────────

class PostTab(QWidget):

    def __init__(self, bbs: BulletinStore, config: AppConfig, event_bus=None,
                 config_manager=None, parent=None):
        super().__init__(parent)
        self._bbs    = bbs
        self._config = config
        self._bus    = event_bus
        self._cm     = config_manager
        self._build_ui()

    def _urgent_areas(self) -> set[str]:
        """Areas the sysop listed as urgent, from the live config."""
        cfg = (self._cm.current() if self._cm else None) or self._config
        return {a.strip().upper()
                for a in cfg.notifications.urgent_bulletin_areas if a.strip()}

    def _update_area_note(self, *_):
        group = self._group_combo.currentText().strip().upper()
        if group and group in self._urgent_areas():
            self._area_note.setText(
                f"🚨 {group} is an urgent area (Settings → Notifications): "
                f"every bulletin posted here is treated as urgent, "
                f"ticked or not.")
            self._area_note.show()
        else:
            self._area_note.hide()

    def showEvent(self, event):
        super().showEvent(event)
        self._update_area_note()     # the urgent list may have changed

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(8)
        layout.setContentsMargins(8, 8, 8, 8)

        form_box = QGroupBox("Post New Bulletin")
        form = QVBoxLayout(form_box)

        # From
        from_row = QHBoxLayout()
        from_row.addWidget(QLabel("From:"))
        self._from_edit = QLineEdit(self._config.station.sysop_callsign.upper())
        from_row.addWidget(self._from_edit)
        form.addLayout(from_row)

        # Group / area
        group_row = QHBoxLayout()
        group_row.addWidget(QLabel("Group:"))
        self._group_combo = QComboBox()
        self._group_combo.setEditable(True)
        areas = [a.name for a in self._config.bbs.bulletin_areas if a.enabled]
        if not areas:
            areas = ["ALL", "WX", "LOCAL", "DX"]
        self._group_combo.addItems(areas)
        group_row.addWidget(self._group_combo)
        group_row.addWidget(QLabel("  (or type a custom group name)"))
        group_row.addStretch()
        form.addLayout(group_row)

        self._area_note = QLabel()
        self._area_note.setWordWrap(True)
        self._area_note.setStyleSheet(f"color:{RED};font-size:9pt;")
        form.addWidget(self._area_note)
        self._group_combo.currentTextChanged.connect(self._update_area_note)
        self._update_area_note()

        # Subject
        subj_row = QHBoxLayout()
        subj_row.addWidget(QLabel("Subject:"))
        self._subj_edit = QLineEdit()
        self._subj_edit.setPlaceholderText("Brief subject line")
        subj_row.addWidget(self._subj_edit)
        form.addLayout(subj_row)

        # Body
        form.addWidget(QLabel("Body:"))
        self._body_edit = QTextEdit()
        self._body_edit.setFont(QFont("Consolas", 9))
        self._body_edit.setPlaceholderText("Bulletin text goes here...")
        self._body_edit.setMinimumHeight(220)
        form.addWidget(self._body_edit)

        # Buttons
        btn_row = QHBoxLayout()
        self._urgent_cb = QCheckBox("🚨 Urgent")
        self._urgent_cb.setToolTip(
            "Flag this bulletin as URGENT.\n"
            "Alerts RF users on connect and, for bulletins from other\n"
            "stations, triggers push notifications."
        )
        btn_row.addWidget(self._urgent_cb)
        btn_row.addStretch()
        clear_btn = QPushButton("Clear")
        clear_btn.clicked.connect(self._clear)
        btn_row.addWidget(clear_btn)
        post_btn = QPushButton("📢  Post Bulletin")
        post_btn.setStyleSheet(BTN_GREEN)
        post_btn.clicked.connect(self._post)
        btn_row.addWidget(post_btn)
        form.addLayout(btn_row)

        layout.addWidget(form_box)

        # Status
        self._status_lbl = QLabel("")
        self._status_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self._status_lbl)
        layout.addStretch()

    def _clear(self):
        self._subj_edit.clear()
        self._body_edit.clear()
        self._urgent_cb.setChecked(False)
        self._status_lbl.setText("")

    def _post(self):
        from core.events import EventType

        from_call = self._from_edit.text().strip().upper()
        group     = self._group_combo.currentText().strip().upper()
        subject   = self._subj_edit.text().strip()
        body      = self._body_edit.toPlainText().strip()

        if not from_call:
            self._status("From callsign required.", error=True)
            return
        if not group:
            self._status("Group/area required.", error=True)
            return
        if not subject:
            self._status("Subject required.", error=True)
            return
        if not body:
            self._status("Body required.", error=True)
            return

        is_urgent = self._urgent_cb.isChecked()

        # Find expiry for this area
        expire_days = None
        for area_cfg in self._config.bbs.bulletin_areas:
            if area_cfg.name.upper() == group:
                expire_days = area_cfg.max_age
                break
        if expire_days is None:
            expire_days = self._config.bbs.max_message_age

        mid = self._bbs.save_bulletin(
            from_call=from_call,
            to_group=group,
            subject=subject,
            body=body.encode("utf-8"),
            area=group,
            expires_days=expire_days,
            is_urgent=is_urgent,
        )

        # Emit BULLETIN_POSTED so notification router and other
        # subscribers are informed (fixes event-emission gap).
        if self._bus:
            from notifications import SYSOP_GUI
            self._bus.emit(EventType.BULLETIN_POSTED, source=SYSOP_GUI,
                           data={
                               "mid":       mid,
                               "from":      from_call,
                               "to_group":  group,
                               "subject":   subject,
                               "area":      group,
                               "is_urgent": is_urgent,
                           })

        urgency = " [URGENT]" if is_urgent else ""
        self._status(f"Bulletin posted{urgency}. MID={mid}", error=False)
        self._clear()

    def _status(self, msg: str, error: bool = False):
        color = RED if error else GREEN
        self._status_lbl.setText(msg)
        self._status_lbl.setStyleSheet(f"color:{color};font-size:9pt;")


# ── Forwarding sub-tab ────────────────────────────────────────────────────────

class ForwardingTab(QWidget):

    def __init__(self, bbs: BulletinStore, config: AppConfig,
                 on_edit_settings=None, parent=None):
        super().__init__(parent)
        self._bbs    = bbs
        self._config = config
        self._on_edit_settings = on_edit_settings
        self._build_ui()
        self._refresh()

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh)
        self._timer.start(15000)  # refresh every 15s

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(6)
        layout.setContentsMargins(6, 6, 6, 6)

        # Neighbours summary (read-only mirror of Settings → Forwarding)
        nbr_box = QGroupBox("Configured Neighbours  (read-only)")
        nbr_layout = QVBoxLayout(nbr_box)

        nbr_hdr = QHBoxLayout()
        nbr_hdr.addStretch()
        edit_btn = QPushButton("⚙ Edit in Settings")
        edit_btn.setFixedWidth(140)
        edit_btn.setToolTip(
            "Open Settings → Forwarding to add, edit, or remove neighbours")
        if self._on_edit_settings:
            edit_btn.clicked.connect(self._on_edit_settings)
        else:
            edit_btn.setEnabled(False)
        nbr_hdr.addWidget(edit_btn)
        nbr_layout.addLayout(nbr_hdr)

        self._nbr_table = QTableWidget()
        self._nbr_table.setColumnCount(5)
        self._nbr_table.setHorizontalHeaderLabels(
            ["Callsign", "Host:Port", "Interval", "Areas", "Status"]
        )
        nbr_hdr_view = self._nbr_table.horizontalHeader()
        nbr_hdr_view.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        nbr_hdr_view.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)  # Areas fills
        nbr_hdr_view.setStretchLastSection(False)
        for col, w in [(0, 90), (1, 140), (2, 65), (4, 70)]:
            nbr_hdr_view.resizeSection(col, w)
        self._nbr_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._nbr_table.setAlternatingRowColors(True)
        self._nbr_table.verticalHeader().setVisible(False)
        self._nbr_table.setMaximumHeight(140)
        nbr_layout.addWidget(self._nbr_table)
        layout.addWidget(nbr_box)

        # Forwarding log
        log_box = QGroupBox("Forwarding Log  (most recent 100 entries)")
        log_layout = QVBoxLayout(log_box)

        log_hdr = QHBoxLayout()
        log_hdr.addStretch()
        refresh_btn = QPushButton("⟳ Refresh")
        refresh_btn.setFixedWidth(90)
        refresh_btn.clicked.connect(self._refresh)
        log_hdr.addWidget(refresh_btn)
        log_layout.addLayout(log_hdr)

        self._log_table = QTableWidget()
        self._log_table.setColumnCount(5)
        self._log_table.setHorizontalHeaderLabels(
            ["Time", "Neighbour", "Direction", "MID", "OK"]
        )
        log_hdr_view = self._log_table.horizontalHeader()
        log_hdr_view.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        log_hdr_view.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)  # MID fills
        log_hdr_view.setStretchLastSection(False)
        for col, w in [(0, 140), (1, 100), (2, 70), (4, 40)]:
            log_hdr_view.resizeSection(col, w)
        self._log_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._log_table.setAlternatingRowColors(True)
        self._log_table.verticalHeader().setVisible(False)
        log_layout.addWidget(self._log_table)
        layout.addWidget(log_box)

    def _refresh(self):
        # Neighbours
        neighbours = getattr(self._config.bbs, "forwarding_neighbours", [])
        self._nbr_table.setRowCount(0)
        if not neighbours:
            self._nbr_table.insertRow(0)
            item = QTableWidgetItem("No forwarding neighbours configured.")
            item.setForeground(QColor(TEXT_MUTED))
            self._nbr_table.setItem(0, 0, item)
        else:
            for i, n in enumerate(neighbours):
                self._nbr_table.insertRow(i)
                enabled_str = "enabled" if n.enabled else "disabled"
                areas_str = ", ".join(n.areas) if n.areas else "all"
                for col, val in enumerate([
                    n.bbs_callsign,
                    f"{n.host}:{n.port}",
                    f"every {n.interval_minutes}m",
                    areas_str,
                    enabled_str,
                ]):
                    item = QTableWidgetItem(val)
                    if not n.enabled:
                        item.setForeground(QColor(TEXT_MUTED))
                    self._nbr_table.setItem(i, col, item)

        # Forwarding log
        entries = self._bbs.get_forwarding_log(limit=100)
        self._log_table.setRowCount(0)
        if not entries:
            self._log_table.insertRow(0)
            item = QTableWidgetItem("No forwarding activity yet.")
            item.setForeground(QColor(TEXT_MUTED))
            self._log_table.setItem(0, 0, item)
        else:
            for i, row in enumerate(entries):
                self._log_table.insertRow(i)
                ok_str = "✔" if row["success"] else "✘"
                ok_color = GREEN if row["success"] else RED
                for col, (val, color) in enumerate([
                    (_ts_short(str(row["timestamp"])), TEXT_SECONDARY),
                    (row["neighbour"],                 TEXT_PRIMARY),
                    (row["direction"].upper(),
                     BLUE if row["direction"] == "sent" else GREEN),
                    (row["mid"],                       TEXT_PRIMARY),
                    (ok_str,                           ok_color),
                ]):
                    item = QTableWidgetItem(val)
                    item.setForeground(QColor(color))
                    self._log_table.setItem(i, col, item)


# ── Top-level BulletinsTab ────────────────────────────────────────────────────

class BulletinsTab(QWidget):
    """
    Main bulletin board tab.  Houses Browse, Post, and Forwarding sub-tabs.
    """

    def __init__(
        self,
        bbs: Optional[BulletinStore],
        config: AppConfig,
        event_bus=None,
        config_manager=None,
        parent=None,
    ):
        super().__init__(parent)
        self._bbs    = bbs
        self._config = config
        self._bus    = event_bus
        self._cm     = config_manager
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        if not self._bbs:
            lbl = QLabel("Bulletin board not initialised.")
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lbl.setStyleSheet(f"color:{TEXT_SECONDARY};font-size:11pt;")
            layout.addWidget(lbl)
            return

        tabs = QTabWidget()
        tabs.addTab(
            BrowseTab(self._bbs, self._config, parent=self),
            "📋 Browse"
        )
        tabs.addTab(
            PostTab(self._bbs, self._config, event_bus=self._bus,
                    config_manager=self._cm, parent=self),
            "📢 Post"
        )
        tabs.addTab(
            ForwardingTab(self._bbs, self._config,
                          on_edit_settings=self._open_forwarding_settings,
                          parent=self),
            "🔄 Forwarding"
        )
        layout.addWidget(tabs)

    def _open_forwarding_settings(self):
        """Navigate to Settings → Forwarding sub-tab."""
        # Walk up to find the outermost QTabWidget (the main tab bar).
        # We must skip inner QTabWidgets (e.g. the Messages group).
        from PyQt6.QtWidgets import QTabWidget
        w = self.parent()
        main_tabs = None
        while w is not None:
            if isinstance(w, QTabWidget):
                main_tabs = w  # keep walking — want the outermost one
            w = w.parent()
        if main_tabs is None:
            return

        # Find Settings tab (labelled "⚙️  Settings")
        for i in range(main_tabs.count()):
            if "Settings" in main_tabs.tabText(i):
                main_tabs.setCurrentIndex(i)
                settings_widget = main_tabs.widget(i)
                # Find the sub-tabs QTabWidget inside Settings
                for child in settings_widget.findChildren(QTabWidget):
                    for j in range(child.count()):
                        if "Forwarding" in child.tabText(j):
                            child.setCurrentIndex(j)
                            return
                return
