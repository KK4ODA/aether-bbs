"""
gui/vara_panel.py

VARA Status Panel (FM and HF instances).
Displays one status card per configured instance showing:
  process state, TCP state, PTT, BUSY, current session, traffic.
"""

import logging
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGroupBox,
    QLabel, QPushButton, QScrollArea, QFrame, QSizePolicy,
    QTableWidget, QTableWidgetItem, QHeaderView,
    QLineEdit, QTextEdit, QComboBox
)
from PyQt6.QtCore import Qt, QTimer, pyqtSlot
from PyQt6.QtGui import QColor

from core.config import AppConfig
from core.events import EventBus, EventType
from vara.instance_manager import VARAFMInstanceManager
from vara.process import ProcessState
from gui.theme import STATE_COLORS

log = logging.getLogger(__name__)


def _indicator(color: str, size: int = 14) -> QLabel:
    lbl = QLabel()
    lbl.setFixedSize(size, size)
    lbl.setStyleSheet(
        f"background-color:{color};border-radius:{size//2}px;"
    )
    return lbl


class InstanceCard(QGroupBox):
    """One status card for one VARA FM or HF instance."""

    def __init__(self, instance_name: str, im: VARAFMInstanceManager,
                 inst_cfg=None, parent=None):
        super().__init__(instance_name, parent)
        self._name = instance_name
        self._im   = im
        self._cfg  = inst_cfg  # use fresh config if provided
        self._bytes_tx = 0
        self._bytes_rx = 0
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(2)
        layout.setContentsMargins(4, 4, 4, 4)

        # Prefer fresh config passed at construction; fall back to instance manager
        if self._cfg:
            freq = self._cfg.frequency_label
            transport = getattr(self._cfg, "transport", "VARA_FM")
        else:
            status = self._im.get_status(self._name)
            freq      = status.cfg.frequency_label if status else ""
            transport  = getattr(status.cfg, "transport", "VARA_FM") if status else "VARA_FM"

        # Short badge label from transport
        badge_map = {"VARA_FM": "FM", "VARA_HF": "HF",
                     "SOUNDMODEM": "AGWPE", "AETHER_HF": "AETHER"}
        mode = badge_map.get(transport, transport)

        # Header row: frequency label + transport badge
        header_row = QHBoxLayout()
        self._freq_lbl = QLabel(freq or "—")
        self._freq_lbl.setStyleSheet("font-weight:bold;font-size:10pt;")
        self._static_freq = freq or "—"  # config value to restore when scan stops
        mode_badge = QLabel(f" {mode} ")
        badge_color = {"FM": "#1a6b3a", "HF": "#1a3f6b",
                       "AGWPE": "#6b3a1a", "AETHER": "#3a1a6b"}.get(mode, "#444")
        mode_badge.setStyleSheet(
            f"background:{badge_color};color:white;border-radius:3px;"
            "font-weight:bold;font-size:9pt;padding:1px 4px;"
        )
        header_row.addWidget(self._freq_lbl)
        header_row.addStretch()
        header_row.addWidget(mode_badge)
        layout.addLayout(header_row)

        # Indicator row
        ind_layout = QHBoxLayout()

        self._proc_ind   = _indicator(STATE_COLORS["stopped"])
        self._tcp_ind    = _indicator(STATE_COLORS["disconnected"])
        self._ptt_ind    = _indicator(STATE_COLORS["stopped"])
        self._ptt_ind.setToolTip(
            "PTT — controlled entirely by VARA.\n"
            "Lights red when VARA keys the transmitter.\n"
            "PTT method (VOX, CAT, RTS/DTR, CM108) is configured\n"
            "inside VARA Settings, not in this application."
        )
        self._busy_ind   = _indicator(STATE_COLORS["stopped"])

        for lbl_text, ind in [
            ("Process", self._proc_ind),
            ("TCP",     self._tcp_ind),
            ("PTT",     self._ptt_ind),
            ("BUSY",    self._busy_ind),
        ]:
            col = QVBoxLayout()
            col.setSpacing(2)
            col.addWidget(ind, alignment=Qt.AlignmentFlag.AlignHCenter)
            col.addWidget(QLabel(lbl_text), alignment=Qt.AlignmentFlag.AlignHCenter)
            ind_layout.addLayout(col)

        layout.addLayout(ind_layout)

        # Session info
        self._session_lbl = QLabel("No active session")
        self._session_lbl.setWordWrap(True)
        layout.addWidget(self._session_lbl)

        # Traffic
        self._traffic_lbl = QLabel("TX: 0 B   RX: 0 B")
        self._traffic_lbl.setStyleSheet("color:#888;font-size:9pt;")
        layout.addWidget(self._traffic_lbl)

        # Control buttons
        btn_layout = QHBoxLayout()
        self._btn_start   = QPushButton("Start")
        self._btn_stop    = QPushButton("Stop")
        self._btn_restart = QPushButton("Restart")
        self._btn_abort   = QPushButton("ABORT")
        self._btn_abort.setStyleSheet("color:red;font-weight:bold;")
        self._btn_abort.setToolTip(
            "Forcefully kill the VARA process for this instance.\n"
            "Use only if VARA is unresponsive — a normal Stop\n"
            "is preferred for graceful shutdown."
        )

        self._btn_start.clicked.connect(lambda: self._im.start_instance(self._name))
        self._btn_stop.clicked.connect(lambda: self._im.stop_instance(self._name))
        self._btn_restart.clicked.connect(lambda: self._im.restart_instance(self._name))
        self._btn_abort.clicked.connect(self._on_abort)

        for btn in (self._btn_start, self._btn_stop, self._btn_restart, self._btn_abort):
            btn.setFixedHeight(24)
            btn_layout.addWidget(btn)
        layout.addLayout(btn_layout)


        self.setMinimumWidth(280)

    @pyqtSlot(str)
    def on_process_state(self, state_name: str):
        color = {
            "RUNNING":    STATE_COLORS["running"],
            "CRASHED":    STATE_COLORS["crashed"],
            "STOPPED":    STATE_COLORS["stopped"],
            "STARTING":   STATE_COLORS["connecting"],
            "SIMULATOR":  STATE_COLORS["simulator"],
        }.get(state_name, STATE_COLORS["stopped"])
        self._proc_ind.setStyleSheet(
            f"background-color:{color};border-radius:7px;"
        )

    @pyqtSlot(bool)
    def on_tcp_connected(self, connected: bool):
        color = STATE_COLORS["connected"] if connected else STATE_COLORS["disconnected"]
        self._tcp_ind.setStyleSheet(
            f"background-color:{color};border-radius:7px;"
        )

    @pyqtSlot(bool)
    def on_ptt(self, active: bool):
        color = STATE_COLORS["ptt"] if active else STATE_COLORS["stopped"]
        self._ptt_ind.setStyleSheet(
            f"background-color:{color};border-radius:7px;"
        )

    @pyqtSlot(bool)
    def on_busy(self, busy: bool):
        color = STATE_COLORS["busy"] if busy else STATE_COLORS["stopped"]
        self._busy_ind.setStyleSheet(
            f"background-color:{color};border-radius:7px;"
        )

    @pyqtSlot(str)
    def on_session_connected(self, call: str):
        self._session_lbl.setText(f"Session: {call}")
        self._session_lbl.setStyleSheet("color:#2ECC71;font-weight:bold;")

    @pyqtSlot()
    def on_session_disconnected(self):
        self._session_lbl.setText("No active session")
        self._session_lbl.setStyleSheet("")

    @pyqtSlot(int)
    def on_buffer(self, n: int):
        self._traffic_lbl.setText(
            f"TX: {self._bytes_tx} B   RX: {self._bytes_rx} B   BUF: {n}"
        )

    def _on_abort(self):
        mux = self._im.get_mux(self._name)
        if mux:
            mux.send_cmd("ABORT")
            log.info(f"[{self._name}] ABORT issued by sysop")

    @pyqtSlot(float, int, int)
    def on_scan_freq(self, freq_khz: float, ch_index: int, total: int):
        """Update frequency label with the current scan channel."""
        if freq_khz == int(freq_khz):
            f = str(int(freq_khz))
        else:
            f = f"{freq_khz:.1f}"
        self._freq_lbl.setText(f"📡 {f} kHz ({ch_index + 1}/{total})")
        self._freq_lbl.setStyleSheet("font-weight:bold;font-size:10pt;color:#58a6ff;")

    @pyqtSlot()
    def on_scan_paused(self):
        """Show that scanning is paused (session connected)."""
        current = self._freq_lbl.text()
        # Only modify if we're showing a scan frequency (starts with 📡)
        if current.startswith("📡"):
            # Strip the channel counter to show just the held frequency
            freq_part = current.replace("📡 ", "").split("(")[0].strip()
            self._freq_lbl.setText(f"📡 ⏸ {freq_part}")
            self._freq_lbl.setStyleSheet(
                "font-weight:bold;font-size:10pt;color:#f39c12;"
            )

    @pyqtSlot()
    def on_scan_resumed(self):
        """Next on_scan_freq call will update — just reset colour."""
        self._freq_lbl.setStyleSheet("font-weight:bold;font-size:10pt;color:#58a6ff;")

    @pyqtSlot()
    def on_scan_stopped(self):
        """Restore the static config frequency label when scanning stops."""
        self._freq_lbl.setText(self._static_freq)
        self._freq_lbl.setStyleSheet("font-weight:bold;font-size:10pt;")



class VARAStatusPanel(QScrollArea):
    """Scrollable panel containing one InstanceCard per configured instance."""

    def __init__(
        self,
        config: AppConfig,
        im: VARAFMInstanceManager,
        bus: EventBus,
        store=None,
        bulletin_store=None,
        parent=None,
    ):
        super().__init__(parent)
        self._config = config
        self._im     = im
        self._bus    = bus
        self._store  = store
        self._bbs    = bulletin_store
        self._cards: dict[str, InstanceCard] = {}
        self._build_ui()
        self._wire_events()

        # Refresh heard stations every 5 seconds
        if self._store:
            self._heard_timer = QTimer(self)
            self._heard_timer.timeout.connect(self._refresh_heard)
            self._heard_timer.start(5000)
            self._refresh_heard()

    def _build_ui(self):
        container = QWidget()
        layout    = QVBoxLayout(container)
        layout.setSpacing(4)
        layout.setContentsMargins(4, 4, 4, 4)

        # Compact GroupBox styling
        container.setStyleSheet("""
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
            QLabel { padding: 0px; }
        """)

        title = QLabel("VARA Instances")
        title.setStyleSheet("font-size:12pt;font-weight:bold;padding:0px;")
        layout.addWidget(title)

        for inst_cfg in self._config.instances:
            if inst_cfg.enabled:
                card = InstanceCard(inst_cfg.name, self._im,
                                    inst_cfg=inst_cfg, parent=container)
                self._cards[inst_cfg.name] = card
                layout.addWidget(card)

        # Push remaining content toward the bottom so instance cards
        # have room to grow when more are added.
        layout.addStretch()

        # ── Recently Heard Stations ──────────────────────────────────
        if self._store:
            heard_group = QGroupBox("Recently Heard Stations")
            hg_layout = QVBoxLayout(heard_group)
            hg_layout.setContentsMargins(4, 2, 4, 2)
            hg_layout.setSpacing(2)
            self._heard_table = QTableWidget(0, 4)
            self._heard_table.setHorizontalHeaderLabels(
                ["Callsign", "Instance", "Last Heard", "Count"]
            )
            self._heard_table.horizontalHeader().setSectionResizeMode(
                QHeaderView.ResizeMode.Stretch
            )
            self._heard_table.verticalHeader().setDefaultSectionSize(22)
            self._heard_table.verticalHeader().setVisible(False)
            hg_layout.addWidget(self._heard_table)

            heard_btn_row = QHBoxLayout()
            clear_heard_btn = QPushButton("🗑  Clear")
            clear_heard_btn.setFixedHeight(24)
            clear_heard_btn.setToolTip("Clear all recently heard stations.")
            clear_heard_btn.clicked.connect(self._on_clear_heard)
            clear_heard_btn.setStyleSheet(
                "QPushButton { background:#5865f2; color:#fff; border:none; "
                "border-radius:4px; padding:4px 12px; font-weight:600; }"
                "QPushButton:hover { background:#4752c4; }"
            )
            heard_btn_row.addStretch()
            heard_btn_row.addWidget(clear_heard_btn)
            hg_layout.addLayout(heard_btn_row)
            layout.addWidget(heard_group)

        self.setWidget(container)
        self.setWidgetResizable(True)
        self.setMinimumWidth(300)

    def _wire_events(self):
        self._bus.subscribe(EventType.SESSION_CONNECTED,    self._on_connected)
        self._bus.subscribe(EventType.SESSION_DISCONNECTED, self._on_disconnected)
        self._bus.subscribe(EventType.CHANNEL_BUSY,         self._on_busy)
        self._bus.subscribe(EventType.PTT_CHANGE,           self._on_ptt)
        self._bus.subscribe(EventType.BUFFER_UPDATE,        self._on_buffer)
        self._bus.subscribe(EventType.VARA_CONNECTED,       self._on_vara_tcp_up)
        self._bus.subscribe(EventType.VARA_DISCONNECTED,    self._on_vara_tcp_down)
        self._bus.subscribe(EventType.VARA_PROCESS_UP,      self._on_proc_up)
        self._bus.subscribe(EventType.VARA_PROCESS_DOWN,    self._on_proc_down)
        self._bus.subscribe(EventType.SIMULATOR_ACTIVE,     self._on_sim_active)
        self._bus.subscribe(EventType.SIMULATOR_INACTIVE,   self._on_sim_inactive)
        self._bus.subscribe(EventType.CONFIG_CHANGED,       self._on_config_changed)
        self._bus.subscribe(EventType.SCAN_FREQ_CHANGED,    self._on_scan_freq)

    def _on_config_changed(self, event):
        new_cfg = (event.data or {}).get("config")
        if new_cfg:
            self._config = new_cfg
        from PyQt6.QtCore import QMetaObject
        QMetaObject.invokeMethod(
            self, "_rebuild_cards",
            Qt.ConnectionType.QueuedConnection,
        )

    @pyqtSlot()
    def _rebuild_cards(self):
        """Tear down existing cards and recreate from current config."""
        container = self.widget()
        layout = container.layout()

        # Remove old instance cards from the layout (keep the title and stretch)
        for card in self._cards.values():
            layout.removeWidget(card)
            card.deleteLater()
        self._cards.clear()

        # Remove the trailing stretch so we can re-add cards before it
        # (stretch is the last item)
        stretch_item = layout.itemAt(layout.count() - 1)
        if stretch_item and stretch_item.spacerItem():
            layout.removeItem(stretch_item)

        # Add cards for all enabled instances
        for inst_cfg in self._config.instances:
            if inst_cfg.enabled:
                card = InstanceCard(inst_cfg.name, self._im,
                                    inst_cfg=inst_cfg, parent=container)
                self._cards[inst_cfg.name] = card
                layout.addWidget(card)

                # Restore live state from the instance manager
                status = self._im.get_status(inst_cfg.name)
                if status:
                    if status.process_state.name in ("RUNNING",):
                        card.on_process_state("RUNNING")
                    if status.mux is not None:
                        from vara.interface import VARAFMState
                        if status.mux.state != VARAFMState.DISCONNECTED:
                            card.on_tcp_connected(True)
                    if status.current_call:
                        card.on_session_connected(status.current_call)

        layout.addStretch()
        log.info(f"VARA panel rebuilt — {len(self._cards)} instance card(s)")

    # Event callbacks run in background threads → use invokeMethod pattern
    def _card_slot(self, instance, method, *args):
        card = self._cards.get(instance)
        if card:
            from PyQt6.QtCore import QMetaObject, Qt
            QMetaObject.invokeMethod(
                card, method,
                Qt.ConnectionType.QueuedConnection,
                *args,
            )

    def _on_connected(self, event):
        card = self._cards.get(event.source)
        if card:
            call = (event.data or {}).get("remote_call", "")
            from PyQt6.QtCore import QMetaObject, Q_ARG
            QMetaObject.invokeMethod(card, "on_session_connected",
                Qt.ConnectionType.QueuedConnection, Q_ARG(str, call))
            # Pause scan indicator on the card
            QMetaObject.invokeMethod(card, "on_scan_paused",
                Qt.ConnectionType.QueuedConnection)

    def _on_disconnected(self, event):
        card = self._cards.get(event.source)
        if card:
            from PyQt6.QtCore import QMetaObject
            QMetaObject.invokeMethod(card, "on_session_disconnected",
                Qt.ConnectionType.QueuedConnection)
            # Resume scan indicator on the card
            QMetaObject.invokeMethod(card, "on_scan_resumed",
                Qt.ConnectionType.QueuedConnection)

    def _on_busy(self, event):
        card = self._cards.get(event.source)
        if card:
            from PyQt6.QtCore import QMetaObject, Q_ARG
            QMetaObject.invokeMethod(card, "on_busy",
                Qt.ConnectionType.QueuedConnection,
                Q_ARG(bool, (event.data or {}).get("busy", False)))

    def _on_ptt(self, event):
        card = self._cards.get(event.source)
        if card:
            from PyQt6.QtCore import QMetaObject, Q_ARG
            QMetaObject.invokeMethod(card, "on_ptt",
                Qt.ConnectionType.QueuedConnection,
                Q_ARG(bool, (event.data or {}).get("ptt", False)))

    def _on_buffer(self, event):
        card = self._cards.get(event.source)
        if card:
            from PyQt6.QtCore import QMetaObject, Q_ARG
            QMetaObject.invokeMethod(card, "on_buffer",
                Qt.ConnectionType.QueuedConnection,
                Q_ARG(int, (event.data or {}).get("bytes", 0)))

    def _on_scan_freq(self, event):
        card = self._cards.get(event.source)
        if card:
            d = event.data or {}
            from PyQt6.QtCore import QMetaObject, Q_ARG
            QMetaObject.invokeMethod(card, "on_scan_freq",
                Qt.ConnectionType.QueuedConnection,
                Q_ARG(float, d.get("freq_khz", 0.0)),
                Q_ARG(int, d.get("channel_index", 0)),
                Q_ARG(int, d.get("total_channels", 0)))

    def _on_vara_tcp_up(self, event):
        card = self._cards.get(event.source)
        if card:
            from PyQt6.QtCore import QMetaObject, Q_ARG
            QMetaObject.invokeMethod(card, "on_tcp_connected",
                Qt.ConnectionType.QueuedConnection, Q_ARG(bool, True))

    def _on_vara_tcp_down(self, event):
        card = self._cards.get(event.source)
        if card:
            from PyQt6.QtCore import QMetaObject, Q_ARG
            QMetaObject.invokeMethod(card, "on_tcp_connected",
                Qt.ConnectionType.QueuedConnection, Q_ARG(bool, False))

    def _on_proc_up(self, event):
        card = self._cards.get(event.source)
        if card:
            from PyQt6.QtCore import QMetaObject, Q_ARG
            QMetaObject.invokeMethod(card, "on_process_state",
                Qt.ConnectionType.QueuedConnection, Q_ARG(str, "RUNNING"))

    def _on_proc_down(self, event):
        card = self._cards.get(event.source)
        if card and not getattr(card, "_sim_active", False):
            from PyQt6.QtCore import QMetaObject, Q_ARG
            QMetaObject.invokeMethod(card, "on_process_state",
                Qt.ConnectionType.QueuedConnection, Q_ARG(str, "CRASHED"))

    def _on_sim_active(self, event):
        card = self._cards.get(event.source)
        if card:
            card._sim_active = True
            from PyQt6.QtCore import QMetaObject, Q_ARG
            QMetaObject.invokeMethod(card, "on_process_state",
                Qt.ConnectionType.QueuedConnection, Q_ARG(str, "SIMULATOR"))

    def _on_sim_inactive(self, event):
        card = self._cards.get(event.source)
        if card:
            card._sim_active = False
            from PyQt6.QtCore import QMetaObject, Q_ARG
            QMetaObject.invokeMethod(card, "on_process_state",
                Qt.ConnectionType.QueuedConnection, Q_ARG(str, "STOPPED"))

    # ── Heard stations ────────────────────────────────────────────────

    def _refresh_heard(self):
        if not self._store:
            return
        stations = self._store.get_heard_stations(limit=20)
        self._heard_table.setRowCount(0)
        for station in stations:
            row = self._heard_table.rowCount()
            self._heard_table.insertRow(row)
            call = station["callsign"]
            if station["ssid"]:
                call += f"-{station['ssid']}"
            self._heard_table.setItem(row, 0, QTableWidgetItem(call))
            self._heard_table.setItem(row, 1, QTableWidgetItem(station["instance"]))
            self._heard_table.setItem(row, 2, QTableWidgetItem(
                str(station["last_heard"])[:19]))
            self._heard_table.setItem(row, 3, QTableWidgetItem(
                str(station["hear_count"])))

    def _on_clear_heard(self):
        if not self._store:
            return
        self._store.clear_heard_stations()
        self._heard_table.setRowCount(0)

    # ── Bulletin posting removed — consolidated in Bulletins tab ────
