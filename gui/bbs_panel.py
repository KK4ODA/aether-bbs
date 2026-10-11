"""
gui/bbs_panel.py

BBS Status Panel: session list, uptime, message counts, heard stations.
"""

import logging
from datetime import datetime
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QTableWidget, QTableWidgetItem, QGroupBox,
    QHeaderView, QPushButton, QSizePolicy, QComboBox,
    QLineEdit, QTextEdit, QDialog, QFormLayout, QDialogButtonBox,
    QMessageBox, QApplication,
)
from PyQt6.QtCore import Qt, QTimer, pyqtSlot
from PyQt6.QtGui import QColor

from core.config import AppConfig
from core.events import EventBus, EventType
from vara.instance_manager import ModemInstanceManager
from bbs.store import MessageStore

log = logging.getLogger(__name__)


class BBSStatusPanel(QWidget):

    def __init__(
        self,
        config: AppConfig,
        im: ModemInstanceManager,
        store: MessageStore,
        bus: EventBus,
        gateway=None,
        access_controller=None,
        parent=None,
    ):
        super().__init__(parent)
        self._config   = config
        self._im       = im
        self._store    = store
        self._bus      = bus
        self._gateway  = gateway
        self._access   = access_controller
        self._start    = datetime.now()
        self._session_count = 0
        self._busy_rejected_count = 0
        self._last_beacon: str | None = None
        self._active_chats: set[str] = set()   # instance names with active chats
        self._session_start_times: dict[str, datetime] = {}  # instance → connect time
        self._build_ui()
        self._wire_events()

        # Refresh counters every 5 seconds
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh_counts)
        self._timer.start(5000)
        self._refresh_counts()

        # Populate user table on startup (not just on events)
        if self._access:
            self._refresh_user_table()

    def _build_ui(self):
        from PyQt6.QtWidgets import QSplitter, QScrollArea, QFrame

        outer = QVBoxLayout(self)
        outer.setSpacing(0)
        outer.setContentsMargins(4, 4, 4, 4)

        # ── Vertical splitter: status sections (top) / user mgmt (bottom)
        self._panel_splitter = QSplitter(Qt.Orientation.Vertical)
        self._panel_splitter.setChildrenCollapsible(False)

        # Top pane: scrollable status sections
        top_scroll = QScrollArea()
        top_scroll.setWidgetResizable(True)
        top_scroll.setFrameShape(QFrame.Shape.NoFrame)
        top_widget = QWidget()
        # 'layout' is the top section layout — every existing
        # layout.addWidget() call below adds to this pane.
        layout = QVBoxLayout(top_widget)
        layout.setSpacing(2)
        layout.setContentsMargins(0, 0, 0, 0)

        # Compact styling — the biggest vertical space wasters are
        # QGroupBox title padding and default QTableWidget row height.
        self.setStyleSheet("""
            QGroupBox {
                margin-top: 8px;
                padding-top: 6px;
                font-weight: bold;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                subcontrol-position: top left;
                padding: 0 3px;
            }
            QTableWidget {
                font-size: 9pt;
            }
            QLabel {
                padding: 0px;
            }
        """)

        # Title
        title = QLabel("BBS Status")
        title.setStyleSheet("font-size:12pt;font-weight:bold;padding:0px;")
        layout.addWidget(title)

        # Summary group
        summary = QGroupBox("Summary")
        summary_layout = QHBoxLayout(summary)
        summary_layout.setContentsMargins(4, 2, 4, 2)
        summary_layout.setSpacing(4)

        self._uptime_lbl   = QLabel("Uptime: —")
        self._msg_count_lbl= QLabel("Messages: 0")
        self._session_lbl  = QLabel("Sessions: 0 total")

        for lbl in (self._uptime_lbl, self._msg_count_lbl, self._session_lbl):
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            summary_layout.addWidget(lbl)
        layout.addWidget(summary)

        # Active sessions table
        sessions_group = QGroupBox("Active Sessions")
        sg_layout = QVBoxLayout(sessions_group)
        sg_layout.setContentsMargins(4, 2, 4, 2)
        sg_layout.setSpacing(2)
        self._sessions_table = QTableWidget(0, 4)
        self._sessions_table.setHorizontalHeaderLabels(
            ["Instance", "Callsign", "Type", "Duration"]
        )
        self._sessions_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )
        self._sessions_table.setMaximumHeight(60)
        self._sessions_table.verticalHeader().setDefaultSectionSize(22)
        self._sessions_table.verticalHeader().setVisible(False)
        self._sessions_table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows
        )
        sg_layout.addWidget(self._sessions_table)

        # Button row under the table
        btn_row = QHBoxLayout()
        self._chat_btn = QPushButton("💬  Start Chat")
        self._chat_btn.setFixedHeight(24)
        self._chat_btn.setEnabled(False)
        self._chat_btn.setToolTip(
            "Open a chat session with the selected remote station.\n"
            "Select a row in the Active Sessions table first."
        )
        self._chat_btn.clicked.connect(self._on_chat_btn_clicked)
        self._chat_btn.setStyleSheet(
            "QPushButton { background:#23a55a; color:#fff; border:none; "
            "border-radius:4px; padding:4px 12px; font-weight:600; }"
            "QPushButton:hover { background:#1a7f42; }"
            "QPushButton:disabled { background:#3b3d44; color:#80848e; }"
        )
        btn_row.addStretch()
        btn_row.addWidget(self._chat_btn)
        sg_layout.addLayout(btn_row)

        self._sessions_table.itemSelectionChanged.connect(self._on_session_selection_changed)
        layout.addWidget(sessions_group)

        # APRS-IS status group
        aprs_group = QGroupBox("APRS-IS Beacon")
        aprs_layout = QVBoxLayout(aprs_group)
        aprs_layout.setContentsMargins(4, 2, 4, 2)
        aprs_layout.setSpacing(1)
        aprs_row1 = QHBoxLayout()
        self._aprs_status_lbl = QLabel("Status: —")
        self._aprs_status_lbl.setToolTip(
            "APRS-IS connection status. When connected, the BBS\n"
            "periodically beacons its position and status to the\n"
            "APRS Internet Service so other operators can find it."
        )
        aprs_row1.addWidget(self._aprs_status_lbl)
        aprs_row1.addStretch()
        aprs_layout.addLayout(aprs_row1)
        aprs_row2 = QHBoxLayout()
        self._aprs_last_lbl = QLabel("Last beacon: —")
        self._aprs_last_lbl.setStyleSheet("color:#888;font-size:9pt;")
        self._aprs_last_lbl.setToolTip(
            "Timestamp of the last APRS beacon sent.\n"
            "Beacons advertise your BBS callsign, frequency,\n"
            "and status on the APRS network."
        )
        self._aprs_send_btn = QPushButton("Send Beacon Now")
        self._aprs_send_btn.setFixedHeight(22)
        self._aprs_send_btn.setToolTip(
            "Force an immediate APRS-IS beacon.\n"
            "Then check aprs.fi for your callsign to confirm it arrived."
        )
        self._aprs_send_btn.clicked.connect(self._on_send_beacon)
        self._aprs_send_btn.setEnabled(False)   # enabled when IS connected
        aprs_row2.addWidget(self._aprs_last_lbl)
        aprs_row2.addStretch()
        aprs_row2.addWidget(self._aprs_send_btn)
        aprs_layout.addLayout(aprs_row2)
        layout.addWidget(aprs_group)
        self._aprs_group = aprs_group
        self._aprs_group.setVisible(self._gateway is not None)

        # ── Send Private Message card ─────────────────────────────────
        # This card uses a distinct background and scoped styling to
        # override the panel-level QLabel{padding:0} and tight spacing.
        compose_card = QWidget()
        compose_card.setObjectName("compose_card")
        compose_card.setStyleSheet("""
            QWidget#compose_card {
                background-color: #383a40;
                border: 1px solid #4e505a;
                border-radius: 6px;
            }
            QWidget#compose_card QLabel {
                padding: 2px 0;
                font-size: 10pt;
                color: #b5bac1;
                background: transparent;
            }
            QWidget#compose_card QLineEdit,
            QWidget#compose_card QTextEdit {
                font-size: 10pt;
                padding: 4px 6px;
            }
        """)
        cg_layout = QVBoxLayout(compose_card)
        cg_layout.setContentsMargins(10, 8, 10, 10)
        cg_layout.setSpacing(6)

        compose_title = QLabel("Send Private Message")
        compose_title.setStyleSheet(
            "font-size:9pt; font-weight:bold; color:#80848e;"
            "letter-spacing:0.5px; padding:0 0 2px 0; background:transparent;"
        )
        cg_layout.addWidget(compose_title)

        # Form layout for To / Subject — labels left-aligned, fields expand
        compose_form = QFormLayout()
        compose_form.setFieldGrowthPolicy(
            QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow
        )
        compose_form.setSpacing(6)
        compose_form.setContentsMargins(0, 0, 0, 0)

        self._compose_to = QLineEdit()
        self._compose_to.setPlaceholderText("Callsign  (e.g. WD5EMA)")
        self._compose_to.setMinimumHeight(30)
        self._compose_to.textChanged.connect(self._on_compose_to_changed)
        compose_form.addRow("To:", self._compose_to)

        self._compose_subject = QLineEdit()
        self._compose_subject.setPlaceholderText("Subject line")
        self._compose_subject.setMinimumHeight(30)
        compose_form.addRow("Subject:", self._compose_subject)

        cg_layout.addLayout(compose_form)

        # Hint label — redirects bulletin attempts to the Bulletins tab
        self._compose_hint = QLabel("")
        self._compose_hint.setStyleSheet(
            "color:#80848e; font-size:8pt; padding:0; background:transparent;"
        )
        cg_layout.addWidget(self._compose_hint)

        # Body — allow it to stretch vertically
        self._compose_body = QTextEdit()
        self._compose_body.setPlaceholderText("Message body...")
        self._compose_body.setMinimumHeight(60)
        cg_layout.addWidget(self._compose_body, stretch=1)

        # Send button — full width for clear call to action
        self._compose_send_btn = QPushButton("📨  Send Message")
        self._compose_send_btn.setMinimumHeight(32)
        self._compose_send_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._compose_send_btn.setStyleSheet(
            "QPushButton { background:#5865f2; color:#fff; border:none; "
            "border-radius:4px; padding:6px 16px; font-weight:600; font-size:10pt; }"
            "QPushButton:hover { background:#4752c4; }"
        )
        self._compose_send_btn.clicked.connect(self._on_compose_send)
        cg_layout.addWidget(self._compose_send_btn)

        layout.addWidget(compose_card)

        # Upload denied notification (auto-clears after 15 seconds)
        self._upload_denied_lbl = QLabel("")
        self._upload_denied_lbl.setWordWrap(True)
        self._upload_denied_lbl.setStyleSheet(
            "color:#e74c3c;font-size:9pt;font-weight:bold;padding:4px;"
        )
        self._upload_denied_lbl.setToolTip(
            "Uploads may be denied if the user's role lacks\n"
            "upload privileges (only TRUSTED and SYSOP can upload)\n"
            "or if the file exceeds the configured size limit."
        )
        self._upload_denied_lbl.setVisible(False)
        layout.addWidget(self._upload_denied_lbl)
        self._upload_denied_timer = QTimer(self)
        self._upload_denied_timer.setSingleShot(True)
        self._upload_denied_timer.timeout.connect(
            lambda: self._upload_denied_lbl.setVisible(False)
        )

        # ── Access request notification ──────────────────────────────
        self._access_request_lbl = QLabel("")
        self._access_request_lbl.setWordWrap(True)
        self._access_request_lbl.setStyleSheet(
            "color:#f39c12;font-size:9pt;font-weight:bold;padding:4px;"
        )
        self._access_request_lbl.setToolTip(
            "Shown when a new callsign connects and requests\n"
            "BBS access. The sysop must approve or block the\n"
            "user in the User Management table below."
        )
        self._access_request_lbl.setVisible(False)
        layout.addWidget(self._access_request_lbl)
        self._access_request_timer = QTimer(self)
        self._access_request_timer.setSingleShot(True)
        self._access_request_timer.timeout.connect(
            lambda: self._access_request_lbl.setVisible(False)
        )

        # ── Finalize top pane and assemble splitter ───────────────────
        layout.addStretch()
        top_scroll.setWidget(top_widget)
        self._panel_splitter.addWidget(top_scroll)

        # ── User management group ────────────────────────────────────
        users_group = QGroupBox("User Management")
        ug_layout = QVBoxLayout(users_group)
        ug_layout.setContentsMargins(4, 2, 4, 2)
        ug_layout.setSpacing(2)

        # Filter row
        filter_row = QHBoxLayout()
        self._user_filter = QComboBox()
        self._user_filter.addItems(
            ["All", "PENDING", "APPROVED", "TRUSTED", "BLOCKED", "SYSOP"]
        )
        self._user_filter.setToolTip(
            "Filter users by access role:\n"
            "  PENDING — awaiting sysop approval\n"
            "  APPROVED — standard BBS access\n"
            "  TRUSTED — can upload files and post bulletins\n"
            "  BLOCKED — denied access\n"
            "  SYSOP — full administrative privileges"
        )
        self._user_filter.currentTextChanged.connect(self._force_refresh_user_table)
        filter_row.addWidget(QLabel("Filter:"))
        filter_row.addWidget(self._user_filter)
        filter_row.addStretch()
        self._refresh_users_btn = QPushButton("🔄 Refresh")
        self._refresh_users_btn.setFixedHeight(22)
        self._refresh_users_btn.clicked.connect(self._force_refresh_user_table)
        filter_row.addWidget(self._refresh_users_btn)
        ug_layout.addLayout(filter_row)

        # User table
        self._user_table = QTableWidget(0, 6)
        self._user_table.setHorizontalHeaderLabels(
            ["Callsign", "Role", "Registered", "Last Auth", "Failures", "Notes"]
        )
        self._user_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )
        self._user_table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows
        )
        self._user_table.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        self._user_table.verticalHeader().setDefaultSectionSize(22)
        self._user_table.verticalHeader().setVisible(False)
        ug_layout.addWidget(self._user_table)

        # Action buttons
        action_row = QHBoxLayout()

        self._add_user_btn = QPushButton("＋ Add User")
        self._add_user_btn.setFixedHeight(24)
        self._add_user_btn.setToolTip("Manually add a callsign to the user database")
        self._add_user_btn.clicked.connect(self._add_user)
        self._add_user_btn.setStyleSheet(
            "QPushButton { background:#f39c12; color:#fff; border:none; "
            "border-radius:4px; padding:4px 10px; font-weight:600; }"
            "QPushButton:hover { background:#d68910; }"
        )
        action_row.addWidget(self._add_user_btn)

        self._gen_pw_btn = QPushButton("🔑 Password")
        self._gen_pw_btn.setFixedHeight(24)
        self._gen_pw_btn.setToolTip(
            "Generate and provision a password for the selected user.\n"
            "Copy the Winlink message to send them their credentials."
        )
        self._gen_pw_btn.clicked.connect(self._provision_password)
        self._gen_pw_btn.setStyleSheet(
            "QPushButton { background:#8e44ad; color:#fff; border:none; "
            "border-radius:4px; padding:4px 10px; font-weight:600; }"
            "QPushButton:hover { background:#7d3c98; }"
        )
        action_row.addWidget(self._gen_pw_btn)

        self._view_pw_btn = QPushButton("👁 View PW")
        self._view_pw_btn.setFixedHeight(24)
        self._view_pw_btn.setToolTip(
            "View the stored password for the selected user.\n"
            "Useful when a user forgets their password."
        )
        self._view_pw_btn.clicked.connect(self._view_password)
        self._view_pw_btn.setStyleSheet(
            "QPushButton { background:#6c5ce7; color:#fff; border:none; "
            "border-radius:4px; padding:4px 10px; font-weight:600; }"
            "QPushButton:hover { background:#5b4cc4; }"
        )
        action_row.addWidget(self._view_pw_btn)

        action_row.addStretch()

        self._approve_btn = QPushButton("✓ Approve")
        self._approve_btn.setFixedHeight(24)
        self._approve_btn.setToolTip("Approve selected user (set to APPROVED)")
        self._approve_btn.clicked.connect(lambda: self._set_user_role("APPROVED"))
        self._approve_btn.setStyleSheet(
            "QPushButton { background:#2ECC71; color:#fff; border:none; "
            "border-radius:4px; padding:4px 10px; font-weight:600; }"
            "QPushButton:hover { background:#27ae60; }"
        )
        action_row.addWidget(self._approve_btn)

        self._promote_btn = QPushButton("⬆ Trusted")
        self._promote_btn.setFixedHeight(24)
        self._promote_btn.setToolTip("Promote selected user to TRUSTED")
        self._promote_btn.clicked.connect(lambda: self._set_user_role("TRUSTED"))
        self._promote_btn.setStyleSheet(
            "QPushButton { background:#3498db; color:#fff; border:none; "
            "border-radius:4px; padding:4px 10px; font-weight:600; }"
            "QPushButton:hover { background:#2980b9; }"
        )
        action_row.addWidget(self._promote_btn)

        self._sysop_btn = QPushButton("⬆ Sysop")
        self._sysop_btn.setFixedHeight(24)
        self._sysop_btn.setToolTip("Promote selected user to SYSOP (full admin)")
        self._sysop_btn.clicked.connect(lambda: self._set_user_role("SYSOP"))
        self._sysop_btn.setStyleSheet(
            "QPushButton { background:#9b59b6; color:#fff; border:none; "
            "border-radius:4px; padding:4px 10px; font-weight:600; }"
            "QPushButton:hover { background:#8e44ad; }"
        )
        action_row.addWidget(self._sysop_btn)

        self._block_btn = QPushButton("⛔ Block")
        self._block_btn.setFixedHeight(24)
        self._block_btn.setToolTip("Block selected user")
        self._block_btn.clicked.connect(lambda: self._set_user_role("BLOCKED"))
        self._block_btn.setStyleSheet(
            "QPushButton { background:#e74c3c; color:#fff; border:none; "
            "border-radius:4px; padding:4px 10px; font-weight:600; }"
            "QPushButton:hover { background:#c0392b; }"
        )
        action_row.addWidget(self._block_btn)

        self._reset_auth_btn = QPushButton("🔓 Reset Auth")
        self._reset_auth_btn.setFixedHeight(24)
        self._reset_auth_btn.setToolTip("Reset auth failure counter for selected user")
        self._reset_auth_btn.clicked.connect(self._reset_auth_failures)
        self._reset_auth_btn.setStyleSheet(
            "QPushButton { background:#9b59b6; color:#fff; border:none; "
            "border-radius:4px; padding:4px 10px; font-weight:600; }"
            "QPushButton:hover { background:#8e44ad; }"
        )
        action_row.addWidget(self._reset_auth_btn)

        self._delete_user_btn = QPushButton("🗑 Delete")
        self._delete_user_btn.setFixedHeight(24)
        self._delete_user_btn.setToolTip("Remove selected user entirely (cannot delete sysop)")
        self._delete_user_btn.clicked.connect(self._delete_user)
        self._delete_user_btn.setStyleSheet(
            "QPushButton { background:#7f8c8d; color:#fff; border:none; "
            "border-radius:4px; padding:4px 10px; font-weight:600; }"
            "QPushButton:hover { background:#616a6b; }"
        )
        action_row.addWidget(self._delete_user_btn)

        ug_layout.addLayout(action_row)

        # Hide user management if no access controller
        users_group.setVisible(self._access is not None)
        self._users_group = users_group
        users_group.setMinimumHeight(180)
        self._user_table.setMinimumHeight(80)
        self._panel_splitter.addWidget(users_group)

        # Give user management ~40% of the panel height
        self._panel_splitter.setSizes([350, 250])
        self._panel_splitter.setStretchFactor(0, 1)
        self._panel_splitter.setStretchFactor(1, 1)
        outer.addWidget(self._panel_splitter)

    def _wire_events(self):
        self._bus.subscribe(EventType.SESSION_CONNECTED,    self._on_connected)
        self._bus.subscribe(EventType.SESSION_DISCONNECTED, self._on_disconnected)
        self._bus.subscribe(EventType.SESSION_STARTED,      self._on_session_started)
        self._bus.subscribe(EventType.SESSION_ENDED,        self._on_session_ended)
        self._bus.subscribe(EventType.MESSAGE_RECEIVED,     self._on_message)
        self._bus.subscribe(EventType.APRS_IS_CONNECTED,    self._on_aprs_connected)
        self._bus.subscribe(EventType.APRS_IS_DISCONNECTED, self._on_aprs_disconnected)
        self._bus.subscribe(EventType.APRS_BEACON_SENT,     self._on_beacon_sent)
        self._bus.subscribe(EventType.CHAT_REQUEST,         self._on_chat_started)
        self._bus.subscribe(EventType.CHAT_ENDED,           self._on_chat_finished)
        self._bus.subscribe(EventType.UPLOAD_DENIED,        self._on_upload_denied)
        self._bus.subscribe(EventType.SESSION_BUSY,          self._on_session_busy)
        self._bus.subscribe(EventType.ACCESS_REQUEST,       self._on_access_request)
        self._bus.subscribe(EventType.AUTH_FAILURE,         self._on_auth_failure)

    def _on_connected(self, event):
        self._session_count += 1
        call = (event.data or {}).get("remote_call", "")
        from PyQt6.QtCore import QMetaObject, Q_ARG
        QMetaObject.invokeMethod(self, "_add_active_session",
            Qt.ConnectionType.QueuedConnection,
            Q_ARG(str, event.source), Q_ARG(str, call))

    def _on_disconnected(self, event):
        from PyQt6.QtCore import QMetaObject, Q_ARG
        QMetaObject.invokeMethod(self, "_remove_active_session",
            Qt.ConnectionType.QueuedConnection, Q_ARG(str, event.source))

    def _on_session_started(self, event):
        """Fired by dispatcher once session type is known — has confirmed callsign."""
        d    = event.data or {}
        call = d.get("remote_call", "")
        stype = d.get("session_type", "")
        from PyQt6.QtCore import QMetaObject, Q_ARG
        QMetaObject.invokeMethod(self, "_update_session_info",
            Qt.ConnectionType.QueuedConnection,
            Q_ARG(str, event.source), Q_ARG(str, call), Q_ARG(str, stype))

    def _on_session_ended(self, event):
        """Fired when the interactive/B2F session object closes."""
        call = (event.data or {}).get("remote_call", "")
        from PyQt6.QtCore import QMetaObject, Q_ARG
        QMetaObject.invokeMethod(self, "_remove_active_session",
            Qt.ConnectionType.QueuedConnection, Q_ARG(str, event.source))

    def _on_message(self, event):
        from PyQt6.QtCore import QMetaObject
        QMetaObject.invokeMethod(self, "_refresh_counts",
            Qt.ConnectionType.QueuedConnection)

    def _on_upload_denied(self, event):
        d = event.data or {}
        call = d.get("callsign", "?")
        fname = d.get("filename", "?")
        area = d.get("area", "?")
        mode = d.get("mode", "?")
        msg = f"⛔ Upload denied: {call} → {area}/{fname}  (mode: {mode})"
        from PyQt6.QtCore import QMetaObject, Q_ARG
        QMetaObject.invokeMethod(self, "_show_upload_denied",
            Qt.ConnectionType.QueuedConnection, Q_ARG(str, msg))

    @pyqtSlot(str)
    def _show_upload_denied(self, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        self._upload_denied_lbl.setText(f"[{ts}]  {msg}")
        self._upload_denied_lbl.setVisible(True)
        self._upload_denied_timer.start(15_000)  # auto-clear after 15s

    def _on_session_busy(self, event):
        """Fired when a connection is rejected because the BBS is at max sessions."""
        self._busy_rejected_count += 1
        d = event.data or {}
        call = d.get("remote_call", "?")
        reason = d.get("reason", "busy")
        msg = f"⛔ BBS BUSY — rejected {call} ({reason})"
        from PyQt6.QtCore import QMetaObject, Q_ARG
        QMetaObject.invokeMethod(self, "_show_upload_denied",
            Qt.ConnectionType.QueuedConnection, Q_ARG(str, msg))

    def _on_aprs_connected(self, event):
        from PyQt6.QtCore import QMetaObject
        QMetaObject.invokeMethod(self, "_set_aprs_connected",
            Qt.ConnectionType.QueuedConnection)

    def _on_aprs_disconnected(self, event):
        from PyQt6.QtCore import QMetaObject
        QMetaObject.invokeMethod(self, "_set_aprs_disconnected",
            Qt.ConnectionType.QueuedConnection)

    def _on_beacon_sent(self, event):
        beacon = (event.data or {}).get("beacon", "")
        from PyQt6.QtCore import QMetaObject, Q_ARG
        QMetaObject.invokeMethod(self, "_update_last_beacon",
            Qt.ConnectionType.QueuedConnection, Q_ARG(str, beacon))

    @pyqtSlot()
    def _set_aprs_connected(self):
        verified = (self._gateway and self._gateway.is_verified)
        if verified:
            self._aprs_status_lbl.setText("Status: ✅ Connected · verified")
            self._aprs_status_lbl.setStyleSheet("color:#2ecc71;")
        else:
            self._aprs_status_lbl.setText(
                "Status: ⚠ Connected but UNVERIFIED — check passcode in Settings → APRS"
            )
            self._aprs_status_lbl.setStyleSheet("color:#e67e22;")
        self._aprs_send_btn.setEnabled(True)

    @pyqtSlot()
    def _set_aprs_disconnected(self):
        self._aprs_status_lbl.setText("Status: ❌ Disconnected")
        self._aprs_status_lbl.setStyleSheet("color:#e74c3c;")
        self._aprs_send_btn.setEnabled(False)

    @pyqtSlot(str)
    def _update_last_beacon(self, beacon: str):
        ts = datetime.now().strftime("%H:%M:%S")
        self._aprs_last_lbl.setText(f"Last beacon: {ts}  {beacon[:60]}")

    def _on_send_beacon(self):
        """Force an immediate APRS-IS beacon."""
        if self._gateway and self._gateway.is_connected:
            import threading
            threading.Thread(
                target=self._gateway._send_beacon,
                name="ManualBeacon",
                daemon=True,
            ).start()

    def _on_compose_to_changed(self, text: str):
        """Show a redirect hint if the sysop types a bulletin group name."""
        from bbs.bulletins import is_bulletin_address
        dest = text.strip().upper()
        if dest and is_bulletin_address(dest):
            self._compose_hint.setText(
                f"📢 \"{dest}\" is a bulletin group — use the Bulletins tab → Post."
            )
            self._compose_hint.setStyleSheet("color:#f39c12; font-size:8pt; padding:0;")
            self._compose_send_btn.setEnabled(False)
        else:
            self._compose_hint.setText("")
            self._compose_hint.setStyleSheet("color:#80848e; font-size:8pt; padding:0;")
            self._compose_send_btn.setEnabled(True)

    def _on_compose_send(self):
        """Save a sysop-composed private message."""
        to_call = self._compose_to.text().strip().upper()
        subject = self._compose_subject.text().strip()
        body = self._compose_body.toPlainText().strip()

        if not to_call:
            self._compose_to.setPlaceholderText("** Callsign is required **")
            return
        if not subject:
            self._compose_subject.setPlaceholderText("** Subject is required **")
            return
        if not body:
            self._compose_body.setPlaceholderText("** Body is required **")
            return

        sysop = self._config.station.sysop_callsign.upper()

        # Personal message only — bulletins are handled by the Bulletins tab
        mid = self._store.save_message(
            from_call=sysop,
            to_call=to_call,
            subject=subject,
            body=body.encode("utf-8"),
            area="PRIVATE",
            instance="SYSOP-GUI",
        )
        log.info(f"GUI: sysop composed message MID={mid} to={to_call}")
        self._bus.emit(EventType.MESSAGE_RECEIVED, source="SYSOP-GUI",
                       data={"from": sysop, "to": to_call, "mid": mid,
                             "subject": subject})

        # Clear form and reset
        self._compose_to.clear()
        self._compose_subject.clear()
        self._compose_body.clear()
        self._compose_to.setPlaceholderText("Callsign (e.g. WD5EMA)")
        self._compose_subject.setPlaceholderText("Subject line")
        self._compose_body.setPlaceholderText("Message body...")

    @pyqtSlot(str, str)
    def _add_active_session(self, instance: str, call: str):
        row = self._sessions_table.rowCount()
        self._sessions_table.insertRow(row)
        call_item = QTableWidgetItem(call)
        call_item.setForeground(QColor("#2ECC71"))
        from PyQt6.QtGui import QFont
        f = call_item.font()
        f.setBold(True)
        call_item.setFont(f)
        self._sessions_table.setItem(row, 0, QTableWidgetItem(instance))
        self._sessions_table.setItem(row, 1, call_item)
        self._sessions_table.setItem(row, 2, QTableWidgetItem("—"))
        self._sessions_table.setItem(row, 3, QTableWidgetItem("0:00"))
        self._session_start_times[instance] = datetime.now()
        # Auto-select so the chat button activates immediately
        self._sessions_table.selectRow(row)

    @pyqtSlot(str, str, str)
    def _update_session_info(self, instance: str, call: str, session_type: str):
        """Update the callsign and session type cells once SESSION_STARTED fires."""
        for row in range(self._sessions_table.rowCount()):
            if self._sessions_table.item(row, 0).text() == instance:
                # Update callsign (column 1)
                call_item = QTableWidgetItem(call)
                call_item.setForeground(QColor("#2ECC71"))
                from PyQt6.QtGui import QFont
                f = call_item.font()
                f.setBold(True)
                call_item.setFont(f)
                self._sessions_table.setItem(row, 1, call_item)
                # Update session type (column 2)
                if session_type:
                    self._sessions_table.setItem(row, 2, QTableWidgetItem(session_type))
                break

    @pyqtSlot(str)
    def _remove_active_session(self, instance: str):
        self._session_start_times.pop(instance, None)
        for row in range(self._sessions_table.rowCount()):
            if self._sessions_table.item(row, 0).text() == instance:
                self._sessions_table.removeRow(row)
                break
        self._on_session_selection_changed()   # re-evaluate button state

    def _on_session_selection_changed(self):
        """Enable the Start Chat button only when a row is selected."""
        has_selection = bool(self._sessions_table.selectedItems())
        self._chat_btn.setEnabled(has_selection)
        self._update_chat_btn()

    def _on_chat_btn_clicked(self):
        """Toggle chat for the currently selected session."""
        selected = self._sessions_table.selectedItems()
        if not selected:
            return
        row = self._sessions_table.currentRow()
        instance_item = self._sessions_table.item(row, 0)
        if not instance_item:
            return
        instance = instance_item.text()

        if instance in self._active_chats:
            # Stop the chat
            self._bus.emit(
                EventType.SYSOP_CHAT_END,
                source="sysop",
                data={"instance": instance},
            )
        else:
            # Start the chat
            self._bus.emit(
                EventType.SYSOP_CHAT_INITIATE,
                source="sysop",
                data={"instance": instance},
            )

    def _on_chat_started(self, event):
        """Called when a chat session becomes active (either side initiated)."""
        from PyQt6.QtCore import QMetaObject, Q_ARG
        QMetaObject.invokeMethod(self, "_mark_chat_active",
            Qt.ConnectionType.QueuedConnection,
            Q_ARG(str, event.source))

    def _on_chat_finished(self, event):
        """Called when a chat session ends."""
        from PyQt6.QtCore import QMetaObject, Q_ARG
        QMetaObject.invokeMethod(self, "_mark_chat_inactive",
            Qt.ConnectionType.QueuedConnection,
            Q_ARG(str, event.source))

    @pyqtSlot(str)
    def _mark_chat_active(self, instance: str):
        self._active_chats.add(instance)
        self._update_chat_btn()

    @pyqtSlot(str)
    def _mark_chat_inactive(self, instance: str):
        self._active_chats.discard(instance)
        self._update_chat_btn()

    def _update_chat_btn(self):
        """Set button label and colour based on whether selected session is chatting."""
        row = self._sessions_table.currentRow()
        if row < 0:
            return
        instance_item = self._sessions_table.item(row, 0)
        if not instance_item:
            return
        instance = instance_item.text()
        if instance in self._active_chats:
            self._chat_btn.setText("🛑  Stop Chat")
            self._chat_btn.setStyleSheet(
                "QPushButton { background:#f23f43; color:#fff; border:none; "
                "border-radius:4px; padding:4px 12px; font-weight:600; }"
                "QPushButton:hover { background:#b72d30; }"
                "QPushButton:disabled { background:#3b3d44; color:#80848e; }"
            )
            self._chat_btn.setToolTip("End the active chat session with this station.")
        else:
            self._chat_btn.setText("💬  Start Chat")
            self._chat_btn.setStyleSheet(
                "QPushButton { background:#23a55a; color:#fff; border:none; "
                "border-radius:4px; padding:4px 12px; font-weight:600; }"
                "QPushButton:hover { background:#1a7f42; }"
                "QPushButton:disabled { background:#3b3d44; color:#80848e; }"
            )
            self._chat_btn.setToolTip(
                "Open a chat session with the selected remote station."
            )

    @pyqtSlot()
    def _refresh_counts(self):
        elapsed = datetime.now() - self._start
        h, rem  = divmod(int(elapsed.total_seconds()), 3600)
        m, s    = divmod(rem, 60)
        self._uptime_lbl.setText(f"Uptime: {h:02d}:{m:02d}:{s:02d}")
        self._msg_count_lbl.setText(f"Messages: {self._store.message_count()}")
        busy_suffix = f" ({self._busy_rejected_count} rejected)" if self._busy_rejected_count else ""
        self._session_lbl.setText(f"Sessions: {self._session_count} total{busy_suffix}")

        # Tick duration column for each active session
        now = datetime.now()
        for row in range(self._sessions_table.rowCount()):
            inst_item = self._sessions_table.item(row, 0)
            if not inst_item:
                continue
            instance = inst_item.text()
            start = self._session_start_times.get(instance)
            if start is None:
                continue
            secs = int((now - start).total_seconds())
            if secs >= 3600:
                sh, srem = divmod(secs, 3600)
                sm, ss = divmod(srem, 60)
                dur_text = f"{sh}:{sm:02d}:{ss:02d}"
            else:
                sm, ss = divmod(secs, 60)
                dur_text = f"{sm}:{ss:02d}"
            dur_item = self._sessions_table.item(row, 3)
            if dur_item:
                dur_item.setText(dur_text)
            else:
                self._sessions_table.setItem(row, 3, QTableWidgetItem(dur_text))

        # Keep user table in sync (catches registrations even if
        # the event-driven refresh misses due to threading)
        if self._access:
            self._refresh_user_table()

    # ── Security event handlers ──────────────────────────────────────

    def _on_access_request(self, event):
        d = event.data or {}
        callsign = d.get("callsign", "?")
        instance = d.get("instance", "")
        ref = d.get("community_ref", "")
        msg = f"NEW Registration request: {callsign}"
        if instance:
            msg += f" (via {instance})"
        if ref and ref.upper() != "NONE":
            msg += f" — community: {ref}"
        from PyQt6.QtCore import QMetaObject, Q_ARG
        QMetaObject.invokeMethod(self, "_show_access_request",
            Qt.ConnectionType.QueuedConnection, Q_ARG(str, msg))

    def _on_auth_failure(self, event):
        d = event.data or {}
        callsign = d.get("callsign", "?")
        from PyQt6.QtCore import QMetaObject, Q_ARG
        QMetaObject.invokeMethod(self, "_show_access_request",
            Qt.ConnectionType.QueuedConnection,
            Q_ARG(str, f"AUTH FAIL: {callsign}"))

    @pyqtSlot(str)
    def _show_access_request(self, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        self._access_request_lbl.setText(f"[{ts}] {msg}")
        self._access_request_lbl.setVisible(True)
        self._access_request_timer.start(20_000)
        # Force rebuild (invalidate cache) and refresh
        self._force_refresh_user_table()

    def _force_refresh_user_table(self):
        """Invalidate cache and rebuild. Used by filter/button/sysop actions."""
        self._user_table_sig = None
        self._refresh_user_table()

    def _refresh_user_table(self):
        if not self._access:
            return
        role_filter = self._user_filter.currentText()
        if role_filter == "All":
            users = self._access.list_users()
        else:
            users = self._access.list_users(role_filter)

        # Skip rebuild if nothing changed (avoids flickering / losing selection)
        new_sig = [(u.get("callsign", ""), u.get("role", ""),
                     u.get("auth_failures", 0), u.get("notes", ""))
                    for u in users]
        if hasattr(self, "_user_table_sig") and self._user_table_sig == new_sig:
            return
        self._user_table_sig = new_sig

        # Preserve current selection
        sel_call = None
        cur_row = self._user_table.currentRow()
        if cur_row >= 0:
            item = self._user_table.item(cur_row, 0)
            if item:
                sel_call = item.text()

        self._user_table.setRowCount(0)
        role_colors = {
            "SYSOP": "#3498db", "TRUSTED": "#2ecc71",
            "APPROVED": "#95a5a6", "PENDING": "#f39c12",
            "BLOCKED": "#e74c3c", "UNKNOWN": "#7f8c8d",
        }
        restore_row = -1
        for u in users:
            row = self._user_table.rowCount()
            self._user_table.insertRow(row)
            call = u.get("callsign", "")
            self._user_table.setItem(row, 0, QTableWidgetItem(call))
            role_item = QTableWidgetItem(u.get("role", ""))
            color = role_colors.get(u.get("role", ""), "#fff")
            role_item.setForeground(QColor(color))
            self._user_table.setItem(row, 1, role_item)
            self._user_table.setItem(row, 2, QTableWidgetItem(
                str(u.get("registered_at", "") or "")[:10]))
            self._user_table.setItem(row, 3, QTableWidgetItem(
                str(u.get("last_auth_at", "") or "")[:19]))
            self._user_table.setItem(row, 4, QTableWidgetItem(
                str(u.get("auth_failures", 0))))
            self._user_table.setItem(row, 5, QTableWidgetItem(
                str(u.get("notes", "") or "")))
            if call == sel_call:
                restore_row = row

        if restore_row >= 0:
            self._user_table.setCurrentCell(restore_row, 0)

    def _set_user_role(self, role_str: str):
        if not self._access:
            return
        row = self._user_table.currentRow()
        if row < 0:
            return
        call_item = self._user_table.item(row, 0)
        if not call_item:
            return
        callsign = call_item.text()
        from bbs.access import Role
        try:
            role = Role(role_str)
        except ValueError:
            return

        sysop = self._config.station.sysop_callsign.upper().split("-")[0]
        self._access.set_role(callsign, role,
                              changed_by=sysop,
                              reason="sysop_gui")
        self._force_refresh_user_table()
        log.info(f"GUI: set {callsign} -> {role_str} by sysop")

    def _reset_auth_failures(self):
        if not self._access:
            return
        row = self._user_table.currentRow()
        if row < 0:
            return
        call_item = self._user_table.item(row, 0)
        if not call_item:
            return
        callsign = call_item.text()
        self._access.record_auth_success(callsign)
        self._force_refresh_user_table()
        log.info(f"GUI: reset auth failures for {callsign}")

    def _delete_user(self):
        if not self._access:
            return
        row = self._user_table.currentRow()
        if row < 0:
            return
        call_item = self._user_table.item(row, 0)
        if not call_item:
            return
        callsign = call_item.text()
        sysop = self._config.station.sysop_callsign.upper()
        reply = QMessageBox.question(
            self, "Delete User",
            f"Remove {callsign} from the user database?\n\n"
            "They will appear as UNKNOWN on next connect.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        ok = self._access.delete_user(callsign, deleted_by=sysop)
        if ok:
            log.info(f"GUI: deleted user {callsign} by sysop")
        else:
            log.warning(f"GUI: delete failed for {callsign} (sysop or not found)")
        self._force_refresh_user_table()

    def _add_user(self):
        """Show a dialog to manually add a callsign to the user database."""
        if not self._access:
            return

        dlg = QDialog(self)
        dlg.setWindowTitle("Add User")
        dlg.setMinimumWidth(380)
        form = QFormLayout(dlg)

        call_edit = QLineEdit()
        call_edit.setPlaceholderText("e.g. KK4ABC (no SSID)")
        call_edit.setMaxLength(10)
        form.addRow("Callsign:", call_edit)

        role_combo = QComboBox()
        role_combo.addItems(["APPROVED", "TRUSTED", "SYSOP", "PENDING", "BLOCKED"])
        form.addRow("Role:", role_combo)

        notes_edit = QLineEdit()
        notes_edit.setPlaceholderText("Optional — community reference, etc.")
        notes_edit.setMaxLength(200)
        form.addRow("Notes:", notes_edit)

        # Password: auto-generate with option to customize
        from bbs.access import AccessController
        pw_edit = QLineEdit()
        pw_edit.setText(AccessController.generate_password())
        pw_edit.setToolTip("Auto-generated. Edit to set a custom password, or clear for none.")
        form.addRow("Password:", pw_edit)

        regen_btn = QPushButton("🎲 Regenerate")
        regen_btn.setFixedHeight(22)
        regen_btn.clicked.connect(
            lambda: pw_edit.setText(AccessController.generate_password()))
        form.addRow("", regen_btn)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(dlg.accept)
        buttons.rejected.connect(dlg.reject)
        form.addRow(buttons)

        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        raw_call = call_edit.text().strip().upper().split("-")[0]
        if not raw_call:
            QMessageBox.warning(self, "Add User", "Callsign cannot be empty.")
            return

        # Check if already exists
        from bbs.access import Role
        existing = self._access.get_role(raw_call)
        if existing != Role.UNKNOWN:
            QMessageBox.warning(
                self, "Add User",
                f"{raw_call} already exists with role {existing.value}.",
            )
            return

        role_str = role_combo.currentText()
        try:
            role = Role(role_str)
        except ValueError:
            return

        sysop = self._config.station.sysop_callsign.upper().split("-")[0]
        self._access.set_role(raw_call, role,
                              changed_by=sysop,
                              reason="sysop_manual_add")

        notes = notes_edit.text().strip()
        if notes:
            self._access.set_notes(raw_call, notes, changed_by=sysop)

        pw = pw_edit.text().strip()
        if pw:
            self._access.set_password(raw_call, pw)
            log.info(f"GUI: manually added user {raw_call} as {role_str} by sysop")
            self._force_refresh_user_table()
            # Show Winlink message for the sysop to send
            self._show_password_message(raw_call, pw)
        else:
            log.info(f"GUI: manually added user {raw_call} as {role_str} (no pw) by sysop")
            self._force_refresh_user_table()

    def _view_password(self):
        """Show the stored password for the selected user."""
        if not self._access:
            return
        row = self._user_table.currentRow()
        if row < 0:
            QMessageBox.information(
                self, "View Password", "Select a user in the table first.")
            return
        call_item = self._user_table.item(row, 0)
        if not call_item:
            return
        callsign = call_item.text()

        pw = self._access.get_decrypted_password(callsign)
        if pw:
            QMessageBox.information(
                self, "Password",
                f"Password for {callsign}:\n\n  {pw}\n\n"
                "You can share this with the user via a secure channel\n"
                "(e.g. Winlink, phone, in person). Never send over RF.")
        else:
            QMessageBox.information(
                self, "Password",
                f"No password set for {callsign}.\n\n"
                "Use the 🔑 Password button to generate one.")

    def _provision_password(self):
        """Generate a password for the selected user and show a Winlink message."""
        if not self._access:
            return
        row = self._user_table.currentRow()
        if row < 0:
            QMessageBox.information(
                self, "Password", "Select a user in the table first.")
            return
        call_item = self._user_table.item(row, 0)
        if not call_item:
            return
        callsign = call_item.text()

        # Confirm if user already has a password
        if self._access.has_password(callsign):
            reply = QMessageBox.question(
                self, "Password",
                f"{callsign} already has a password.\n\n"
                "Generate a new one? This replaces the old password.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return

        sysop = self._config.station.sysop_callsign.upper().split("-")[0]
        pw = self._access.provision_password(callsign, changed_by=sysop)
        self._force_refresh_user_table()
        self._show_password_message(callsign, pw)

    def _show_password_message(self, callsign: str, password: str):
        """Show a dialog with a pre-formatted Winlink message containing
        the user's credentials, with a button to copy to clipboard."""
        bbs_call = self._config.station.bbs_callsign
        sysop_call = self._config.station.sysop_callsign.upper().split("-")[0]

        winlink_addr = f"{callsign}@winlink.org"
        subject = f"Your {bbs_call} BBS credentials"
        body = (
            f"Welcome to {bbs_call} BBS.\n"
            f"\n"
            f"Your account has been approved. Here are your credentials:\n"
            f"\n"
            f"  Callsign : {callsign}\n"
            f"  Password : {password}\n"
            f"\n"
            f"You will be prompted for this password when you connect.\n"
            f"Keep it safe. Do not share it over RF.\n"
            f"\n"
            f"To change your password after logging in, type:\n"
            f"  SETPASS <new_password>\n"
            f"\n"
            f"73 de {sysop_call}, sysop"
        )

        dlg = QDialog(self)
        dlg.setWindowTitle(f"Send Password to {callsign}")
        dlg.setMinimumWidth(460)
        layout = QVBoxLayout(dlg)

        layout.addWidget(QLabel(
            f"<b>Send via Winlink to:</b> {winlink_addr}"))
        layout.addWidget(QLabel(
            f"<b>Subject:</b> {subject}"))

        msg_box = QTextEdit()
        msg_box.setPlainText(body)
        msg_box.setReadOnly(True)
        msg_box.setMinimumHeight(220)
        msg_box.setStyleSheet(
            "QTextEdit { font-family: Consolas, monospace; font-size: 11px; }")
        layout.addWidget(msg_box)

        btn_row = QHBoxLayout()

        copy_msg_btn = QPushButton("📋 Copy Message")
        copy_msg_btn.setFixedHeight(28)
        copy_msg_btn.setStyleSheet(
            "QPushButton { background:#3498db; color:#fff; border:none; "
            "border-radius:4px; padding:4px 14px; font-weight:600; }"
            "QPushButton:hover { background:#2980b9; }"
        )
        copy_msg_btn.clicked.connect(lambda: self._copy_to_clipboard(
            f"To: {winlink_addr}\nSubject: {subject}\n\n{body}",
            copy_msg_btn,
        ))
        btn_row.addWidget(copy_msg_btn)

        copy_pw_btn = QPushButton("🔑 Copy Password Only")
        copy_pw_btn.setFixedHeight(28)
        copy_pw_btn.setStyleSheet(
            "QPushButton { background:#8e44ad; color:#fff; border:none; "
            "border-radius:4px; padding:4px 14px; font-weight:600; }"
            "QPushButton:hover { background:#7d3c98; }"
        )
        copy_pw_btn.clicked.connect(lambda: self._copy_to_clipboard(
            password, copy_pw_btn,
        ))
        btn_row.addWidget(copy_pw_btn)

        btn_row.addStretch()

        close_btn = QPushButton("Close")
        close_btn.setFixedHeight(28)
        close_btn.clicked.connect(dlg.accept)
        btn_row.addWidget(close_btn)

        layout.addLayout(btn_row)
        dlg.exec()

    @staticmethod
    def _copy_to_clipboard(text: str, btn: QPushButton):
        """Copy text to clipboard and flash the button label."""
        clipboard = QApplication.clipboard()
        if clipboard:
            clipboard.setText(text)
        original = btn.text()
        btn.setText("✓ Copied!")
        QTimer.singleShot(1500, lambda: btn.setText(original))
