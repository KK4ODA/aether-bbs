"""
gui/settings.py

Settings tab — all configuration fields grouped by section.
Changes are staged until Save is clicked.
Settings requiring VARA reconnect show a warning indicator.
"""

import logging
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QTabWidget,
    QFormLayout, QLineEdit, QCheckBox, QSpinBox, QDoubleSpinBox,
    QComboBox, QPushButton, QLabel, QGroupBox,
    QFileDialog, QMessageBox, QScrollArea,
    QTableWidget, QTableWidgetItem, QHeaderView,
    QFrame, QDialog, QDialogButtonBox,
    QListWidget, QListWidgetItem, QStackedWidget, QSplitter,
    QAbstractItemView, QSizePolicy,
)
from PyQt6.QtCore import Qt, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QColor, QFont

from core.config import (AppConfig, ConfigManager, InstanceConfig, PTTConfig,
                          RigControlConfig, ScanChannelConfig, SoundmodemConfig,
                          BulletinAreaConfig, ForwardingNeighbourConfig,
                          ForwardingConfig, TacticalAddress, WinlinkConfig,
                          NotificationConfig, AetherConfig)
from core.events import EventBus, EventType

log = logging.getLogger(__name__)


class PTTConfigDialog(QDialog):
    """Modal dialog for editing PTT parameters for a specific method."""

    def __init__(self, parent, method: str, ptt_cfg: "PTTConfig"):
        super().__init__(parent)
        self.setWindowTitle(f"PTT Settings — {method}")
        self.setMinimumWidth(360)
        self._method = method.upper()
        self._ptt_cfg = ptt_cfg

        layout = QVBoxLayout(self)
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)

        # --- Fields shown depend on method ---
        if self._method == "VOX":
            info = QLabel("VOX mode — no configuration needed.\n"
                          "The radio keys via audio level (VOX) or\n"
                          "VARA FM handles PTT internally.")
            info.setStyleSheet("color:#666;")
            form.addRow(info)

        elif self._method in ("RTS", "DTR", "DTRRTS"):
            self._port_edit = QLineEdit(ptt_cfg.port)
            self._port_edit.setPlaceholderText("COM5  or  /dev/ttyUSB0")
            form.addRow("Serial port:", self._port_edit)

        elif self._method == "HAMLIB":
            self._host_edit = QLineEdit(ptt_cfg.hamlib_host)
            self._host_edit.setPlaceholderText("127.0.0.1")
            form.addRow("rigctld host:", self._host_edit)

            self._hamlib_port_spin = QSpinBox()
            self._hamlib_port_spin.setRange(1, 65535)
            self._hamlib_port_spin.setValue(ptt_cfg.hamlib_port)
            form.addRow("rigctld port:", self._hamlib_port_spin)

        elif self._method == "CM108":
            self._cm108_edit = QLineEdit(ptt_cfg.cm108_device)
            self._cm108_edit.setPlaceholderText("blank = auto-detect")
            self._cm108_edit.setToolTip(
                "HID device path for RA-Board / DRA-30.\n"
                "Leave blank to auto-detect the first C-Media device."
            )
            form.addRow("HID device:", self._cm108_edit)

        elif self._method == "OMNIRIG":
            info = QLabel(
                "OmniRig must be installed and running.\n"
                "Configure your radio in OmniRig first."
            )
            info.setStyleSheet("color:#666;")
            form.addRow(info)

            self._omnirig_rig_combo = QComboBox()
            self._omnirig_rig_combo.addItems(["Rig 1", "Rig 2"])
            self._omnirig_rig_combo.setCurrentIndex(
                0 if ptt_cfg.omnirig_rig == 1 else 1
            )
            self._omnirig_rig_combo.setToolTip(
                "OmniRig supports two rig slots.\n"
                "Select which one your HF radio is assigned to."
            )
            form.addRow("OmniRig rig:", self._omnirig_rig_combo)

        elif self._method == "FLRIG":
            info = QLabel(
                "FLrig must be running and connected to your radio.\n"
                "Default XML-RPC port is 12345."
            )
            info.setStyleSheet("color:#666;")
            form.addRow(info)

            self._flrig_host_edit = QLineEdit(ptt_cfg.flrig_host)
            self._flrig_host_edit.setPlaceholderText("127.0.0.1")
            form.addRow("FLrig host:", self._flrig_host_edit)

            self._flrig_port_spin = QSpinBox()
            self._flrig_port_spin.setRange(1, 65535)
            self._flrig_port_spin.setValue(ptt_cfg.flrig_port)
            form.addRow("FLrig port:", self._flrig_port_spin)

        # Watchdog (all methods except VOX)
        if self._method != "VOX":
            self._watchdog_spin = QDoubleSpinBox()
            self._watchdog_spin.setRange(10.0, 600.0)
            self._watchdog_spin.setSingleStep(5.0)
            self._watchdog_spin.setSuffix(" s")
            self._watchdog_spin.setDecimals(0)
            self._watchdog_spin.setValue(ptt_cfg.watchdog_timeout)
            self._watchdog_spin.setToolTip(
                "Safety timer — forcibly releases PTT if modem\n"
                "stops communicating (prevents stuck transmitter)."
            )
            form.addRow("Watchdog timeout:", self._watchdog_spin)

        layout.addLayout(form)

        # OK / Cancel buttons
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok |
            QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def get_updated_config(self) -> "PTTConfig":
        """Return a PTTConfig with fields updated from the dialog."""
        data = self._ptt_cfg.model_dump()
        data["method"] = self._method

        if self._method in ("RTS", "DTR", "DTRRTS"):
            data["port"] = self._port_edit.text().strip()
        elif self._method == "HAMLIB":
            data["hamlib_host"] = self._host_edit.text().strip() or "127.0.0.1"
            data["hamlib_port"] = self._hamlib_port_spin.value()
        elif self._method == "CM108":
            data["cm108_device"] = self._cm108_edit.text().strip()
        elif self._method == "OMNIRIG":
            data["omnirig_rig"] = self._omnirig_rig_combo.currentIndex() + 1
        elif self._method == "FLRIG":
            data["flrig_host"] = self._flrig_host_edit.text().strip() or "127.0.0.1"
            data["flrig_port"] = self._flrig_port_spin.value()

        if self._method != "VOX":
            data["watchdog_timeout"] = self._watchdog_spin.value()

        return PTTConfig(**data)


class ScanChannelDialog(QDialog):
    """Modal dialog for editing scan channel table and rig control settings."""

    def __init__(self, parent, rig_cfg: "RigControlConfig"):
        super().__init__(parent)
        self.setWindowTitle("Scan Channels — Rig Control")
        self.setMinimumWidth(520)
        self.setMinimumHeight(400)
        self._rig_cfg = rig_cfg

        layout = QVBoxLayout(self)

        # ── Rig control method ────────────────────────────────────────
        top_form = QFormLayout()
        top_form.setFieldGrowthPolicy(
            QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow
        )

        self._method_combo = QComboBox()
        self._method_combo.addItems(["NONE", "OMNIRIG"])
        self._method_combo.setCurrentText(rig_cfg.method.upper())
        self._method_combo.setToolTip(
            "NONE = no rig control (manual tuning)\n"
            "OMNIRIG = set freq/mode via OmniRig and scan channels"
        )
        top_form.addRow("Rig control:", self._method_combo)

        self._rig_combo = QComboBox()
        self._rig_combo.addItems(["Rig 1", "Rig 2"])
        self._rig_combo.setCurrentIndex(0 if rig_cfg.omnirig_rig == 1 else 1)
        self._rig_combo.setToolTip("OmniRig rig slot for frequency control")
        top_form.addRow("OmniRig rig:", self._rig_combo)

        self._mode_combo = QComboBox()
        self._mode_combo.addItems([
            "USB", "LSB", "DATA-USB", "DATA-LSB", "FM", "AM", "CW"
        ])
        cur_mode = rig_cfg.mode.upper()
        idx = self._mode_combo.findText(cur_mode)
        if idx >= 0:
            self._mode_combo.setCurrentIndex(idx)
        self._mode_combo.setToolTip(
            "Radio mode to set when scanning.\n"
            "Most VARA HF stations use USB or DATA-USB."
        )
        top_form.addRow("Mode:", self._mode_combo)

        layout.addLayout(top_form)

        # ── Channel table ─────────────────────────────────────────────
        layout.addWidget(QLabel("Scan channels:"))
        self._ch_table = QTableWidget(0, 3)
        self._ch_table.setHorizontalHeaderLabels([
            "Dial Freq (kHz)", "Dwell (s)", "Hours (UTC)"
        ])
        hdr = self._ch_table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self._ch_table.setColumnWidth(1, 80)
        self._ch_table.setColumnWidth(2, 110)
        self._ch_table.verticalHeader().setVisible(False)
        layout.addWidget(self._ch_table)

        # Populate existing channels
        for ch in rig_cfg.channels:
            self._add_channel_row(ch.freq_khz, ch.dwell_seconds, ch.hours)

        # Buttons
        btn_row = QHBoxLayout()
        add_btn = QPushButton("+ Add Channel")
        del_btn = QPushButton("− Remove Selected")
        add_btn.clicked.connect(self._add_empty_channel)
        del_btn.clicked.connect(self._remove_channel)
        btn_row.addWidget(add_btn)
        btn_row.addWidget(del_btn)
        btn_row.addStretch()
        layout.addLayout(btn_row)

        # Helper text
        help_lbl = QLabel(
            "Frequencies in kHz (e.g. 7122.0). "
            "Hours in UTC 'HH-HH' format (e.g. '14-04' for daytime, "
            "'00-24' for always).\n"
            "Scanning pauses when a modem session connects and "
            "resumes when it disconnects."
        )
        help_lbl.setWordWrap(True)
        help_lbl.setStyleSheet("color:#888; font-size:11px;")
        layout.addWidget(help_lbl)

        # OK / Cancel
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok |
            QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _add_channel_row(
        self, freq: float = 7122.0, dwell: float = 5.0, hours: str = "00-24"
    ):
        row = self._ch_table.rowCount()
        self._ch_table.insertRow(row)
        # Freq — display cleanly (no trailing zeros for integers)
        freq_str = f"{freq:.1f}" if freq != int(freq) else f"{int(freq)}.0"
        self._ch_table.setItem(row, 0, QTableWidgetItem(freq_str))
        self._ch_table.setItem(row, 1, QTableWidgetItem(str(dwell)))
        self._ch_table.setItem(row, 2, QTableWidgetItem(hours))

    def _add_empty_channel(self):
        self._add_channel_row()

    def _remove_channel(self):
        row = self._ch_table.currentRow()
        if row >= 0:
            self._ch_table.removeRow(row)

    def get_updated_config(self) -> "RigControlConfig":
        """Return a RigControlConfig with fields updated from the dialog."""
        channels = []
        for row in range(self._ch_table.rowCount()):
            freq_item = self._ch_table.item(row, 0)
            dwell_item = self._ch_table.item(row, 1)
            hours_item = self._ch_table.item(row, 2)
            try:
                freq = float(freq_item.text()) if freq_item else 7122.0
                dwell = float(dwell_item.text()) if dwell_item else 5.0
                hours = (hours_item.text().strip() if hours_item
                         else "00-24") or "00-24"
                channels.append(ScanChannelConfig(
                    freq_khz=freq,
                    dwell_seconds=dwell,
                    hours=hours,
                ))
            except (ValueError, Exception):
                continue  # skip malformed rows

        return RigControlConfig(
            method=self._method_combo.currentText().upper(),
            omnirig_rig=self._rig_combo.currentIndex() + 1,
            mode=self._mode_combo.currentText().upper(),
            channels=channels,
        )


class SettingsTab(QScrollArea):
    settings_saved = pyqtSignal()

    def __init__(self, cm: ConfigManager, config: AppConfig, bus: EventBus,
                 notifiers=None, delivery_engine=None, parent=None):
        super().__init__(parent)
        self._cm     = cm
        self._config = config
        self._bus    = bus
        self._dirty  = False
        self._notifiers = notifiers or {}
        self._delivery_engine = delivery_engine

        container = QWidget()
        outer     = QVBoxLayout(container)
        outer.setSpacing(4)
        outer.setContentsMargins(6, 6, 6, 6)

        # Compact GroupBox styling — reduce the title padding that
        # adds ~20px of dead space above every group's content.
        container.setStyleSheet("""
            QGroupBox {
                margin-top: 10px;
                padding-top: 8px;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                subcontrol-position: top left;
                padding: 0 4px;
            }
        """)

        # ── Sidebar + content area ────────────────────────────────────
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)

        # Left: navigation sidebar
        self._nav = QListWidget()
        self._nav.setFixedWidth(165)
        self._nav.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self._nav.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        self._nav.setFocusPolicy(Qt.FocusPolicy.ClickFocus)

        # Sidebar styling — uses explicit colours from the Discord dark
        # palette so hover/selected states always contrast with light text.
        self._nav.setStyleSheet("""
            QListWidget {
                background-color: #1e1f22;
                border: none;
                border-right: 1px solid #3b3d44;
                outline: none;
                font-size: 12px;
                padding-top: 2px;
                color: #b5bac1;
            }
            QListWidget::item {
                padding: 5px 10px;
                border-radius: 4px;
                margin: 1px 4px;
                color: #b5bac1;
            }
            QListWidget::item:selected {
                background-color: #5865f2;
                color: #ffffff;
            }
            QListWidget::item:hover:!selected {
                background-color: #404249;
                color: #dbdee1;
            }
        """)

        # Right: stacked content pages
        self._pages = QStackedWidget()

        splitter.addWidget(self._nav)
        splitter.addWidget(self._pages)
        splitter.setStretchFactor(0, 0)   # sidebar: fixed
        splitter.setStretchFactor(1, 1)   # content: stretches

        # ── Populate pages ────────────────────────────────────────────
        # Each entry is one of:
        #   (label, builder, is_child) — a selectable page
        #   "SECTION NAME"            — a non-selectable section header
        page_defs = [
            "STATION",
            ("Station",            self._station_tab,       False),
            ("Instances",          self._instances_tab,     False),
            ("BBS",                self._bbs_tab,           False),
            "CONTENT",
            ("Bulletins",          self._bulletins_tab,     False),
            ("Forwarding",         self._forwarding_tab,    False),
            ("File Upload",        self._file_upload_tab,   False),
            "CONNECTIVITY",
            ("Winlink (Pat)",      self._winlink_tab,       False),
            ("APRS",               self._aprs_tab,          False),
            "DELIVERY",
            ("Outbound Delivery",  self._delivery_tab,      False),
            ("  SMTP Fallback",    self._smtp_providers_tab,  True),
            ("  Inbound Email Fallback", self._inbound_email_tab, True),
            ("  Winlink Fwd Mgr",  self._winlink_forward_tab, True),
            "SECURITY & OPS",
            ("Security",           self._security_tab,      False),
            ("Alerts",             self._alerts_tab,        False),
            ("Notifications",      self._notifications_tab, False),
            ("Logging",            self._logging_tab,       False),
            "ABOUT",
            ("About & Updates",    self._updater_tab,       False),
        ]

        page_index = 0
        _first_header = True
        for entry in page_defs:
            if isinstance(entry, str):
                # Section header with integrated divider line.
                # Uses a single QLabel with border-top as the divider,
                # rendered via setItemWidget so the border is reliable.
                header_item = QListWidgetItem()
                header_item.setFlags(Qt.ItemFlag.NoItemFlags)

                header_lbl = QLabel(f"  {entry}")
                if _first_header:
                    header_lbl.setStyleSheet(
                        "color:#e2a84b; font-size:8pt; font-weight:bold;"
                        "letter-spacing:1px; background:transparent;"
                        "padding: 2px 0 0 0;"
                    )
                    # 34px = 12px item padding/margin + 8pt text + internal padding
                    header_item.setSizeHint(header_item.sizeHint().__class__(0, 34))
                else:
                    header_lbl.setStyleSheet(
                        "color:#e2a84b; font-size:8pt; font-weight:bold;"
                        "letter-spacing:1px; background:transparent;"
                        "border-top: 1px solid #4e505a;"
                        "padding: 6px 0 0 0;"
                    )
                    # 46px = 12px item overhead + border + padding + text
                    header_item.setSizeHint(header_item.sizeHint().__class__(0, 46))

                self._nav.addItem(header_item)
                self._nav.setItemWidget(header_item, header_lbl)
                _first_header = False
                continue

            label, builder, is_child = entry
            item = QListWidgetItem(label)
            if is_child:
                # Indent child items with a smaller font
                font = item.font()
                font.setPointSize(max(font.pointSize() - 1, 8))
                item.setFont(font)

            # Store mapping: item → page index
            item.setData(Qt.ItemDataRole.UserRole, page_index)
            self._nav.addItem(item)

            # Wrap each page in a scroll area for consistent behaviour
            page_widget = builder()
            page_scroll = QScrollArea()
            page_scroll.setWidget(page_widget)
            page_scroll.setWidgetResizable(True)
            page_scroll.setFrameShape(QFrame.Shape.NoFrame)
            self._pages.addWidget(page_scroll)
            page_index += 1

        # Connect sidebar selection → content page
        self._nav.currentItemChanged.connect(self._on_nav_changed)
        self._nav.setCurrentRow(0)

        outer.addWidget(splitter, stretch=1)

        # Save / Revert buttons
        btn_layout = QHBoxLayout()
        self._save_btn   = QPushButton("💾 Save Settings")
        self._revert_btn = QPushButton("↩ Revert")
        self._save_btn.clicked.connect(self._save)
        self._revert_btn.clicked.connect(self._revert)
        self._save_btn.setStyleSheet("font-weight:bold;background:#1a7f42;color:white;padding:6px;border-color:#23a55a;")
        btn_layout.addStretch()
        btn_layout.addWidget(self._revert_btn)
        btn_layout.addWidget(self._save_btn)
        outer.addLayout(btn_layout)

        self.setWidget(container)
        self.setWidgetResizable(True)

    # ── Sidebar navigation handler ────────────────────────────────────────

    def _on_nav_changed(self, current: QListWidgetItem, _previous):
        """Switch the stacked content page when a sidebar item is clicked."""
        if current is None:
            return
        idx = current.data(Qt.ItemDataRole.UserRole)
        if idx is not None:
            self._pages.setCurrentIndex(idx)

    # ── Station settings ──────────────────────────────────────────────────

    def _station_tab(self) -> QWidget:
        w = QWidget()
        outer = QVBoxLayout(w)
        outer.setSpacing(6)
        outer.setContentsMargins(0, 0, 0, 0)

        form = QFormLayout()
        cfg = self._config.station

        self._bbs_call  = QLineEdit(cfg.bbs_callsign)
        self._bbs_call.setToolTip(
            "The callsign this BBS identifies as (e.g. KK4ODA-5).\n"
            "Remote stations connect to this callsign over RF."
        )
        self._sysop_call= QLineEdit(cfg.sysop_callsign)
        self._sysop_call.setToolTip(
            "Your personal callsign as the system operator.\n"
            "Shown in the BBS INFO command and APRS beacons."
        )
        self._grid      = QLineEdit(cfg.grid_square)
        self._grid.setToolTip(
            "Maidenhead grid square (e.g. EM73).\n"
            "Used for APRS position beacons and BBS INFO."
        )

        self._latitude  = QDoubleSpinBox()
        self._latitude.setToolTip("Station latitude in decimal degrees.\nPositive = North, Negative = South.")
        self._latitude.setRange(-90.0, 90.0)
        self._latitude.setDecimals(6)
        self._latitude.setSingleStep(0.001)
        self._latitude.setSuffix("°  (+ N  / − S)")
        self._latitude.setValue(cfg.latitude if cfg.latitude is not None else 0.0)
        self._latitude.setSpecialValueText("")

        self._longitude = QDoubleSpinBox()
        self._longitude.setToolTip("Station longitude in decimal degrees.\nPositive = East, Negative = West.")
        self._longitude.setRange(-180.0, 180.0)
        self._longitude.setDecimals(6)
        self._longitude.setSingleStep(0.001)
        self._longitude.setSuffix("°  (+ E  / − W)")
        self._longitude.setValue(cfg.longitude if cfg.longitude is not None else 0.0)

        self._beacon_iv = QSpinBox()
        self._beacon_iv.setToolTip(
            "Minutes between APRS position beacons.\n"
            "Set to 0 to disable automatic beaconing."
        )
        self._beacon_iv.setRange(0, 120)
        self._beacon_iv.setValue(cfg.beacon_interval)
        self._beacon_iv.setSuffix(" min (0=off)")
        self._beacon_txt= QLineEdit(cfg.beacon_text)
        self._beacon_txt.setToolTip(
            "Custom text appended to APRS beacon packets.\n"
            "e.g. 'Aether BBS 145.530 MHz'"
        )

        form.addRow("BBS Callsign:",    self._bbs_call)
        form.addRow("Sysop Callsign:",  self._sysop_call)
        form.addRow("Grid Square:",     self._grid)
        form.addRow("Latitude:",        self._latitude)
        form.addRow("Longitude:",       self._longitude)
        form.addRow("Beacon Interval:", self._beacon_iv)
        form.addRow("Beacon Text:",     self._beacon_txt)

        outer.addLayout(form)

        # ── Tactical addresses table ──────────────────────────────────
        tac_group = QGroupBox("Tactical Addresses")
        tac_layout = QVBoxLayout(tac_group)

        tac_note = QLabel(
            "Messages addressed to these callsigns trigger a priority alert. "
            "Each address can have its own spoken alert phrase (TTS)."
        )
        tac_note.setWordWrap(True)
        tac_note.setStyleSheet("color:#888; font-size:9pt;")
        tac_layout.addWidget(tac_note)

        self._tactical_table = QTableWidget(0, 3)
        self._tactical_table.setToolTip(
            "Callsigns that trigger priority alerts when a message\n"
            "is addressed to them (e.g. EMCOMM, SHELTER, WEATHER).\n"
            "Advertise = include in APRS beacon for discovery.\n"
            "Tactical addresses share the gateway Winlink password."
        )
        self._tactical_table.setHorizontalHeaderLabels(
            ["Callsign (max 8)", "Alert Phrase (spoken by TTS)", "Advertise"])
        self._tactical_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self._tactical_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self._tactical_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self._tactical_table.setMaximumHeight(110)
        self._tactical_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)

        for ta in cfg.tactical_addresses:
            self._add_tactical_row(ta.callsign, ta.alert_phrase, ta.advertised)

        tac_btn_row = QHBoxLayout()
        add_tac_btn = QPushButton("＋ Add")
        add_tac_btn.setFixedWidth(80)
        add_tac_btn.clicked.connect(lambda: self._add_tactical_row())
        rm_tac_btn  = QPushButton("✕ Remove")
        rm_tac_btn.setFixedWidth(80)
        rm_tac_btn.clicked.connect(self._remove_tactical_row)
        tac_btn_row.addWidget(add_tac_btn)
        tac_btn_row.addWidget(rm_tac_btn)
        tac_btn_row.addStretch()

        tac_layout.addWidget(self._tactical_table)
        tac_layout.addLayout(tac_btn_row)
        outer.addWidget(tac_group)

        loc_note = QLabel("Lat/Lon used for APRS position beacon. Leave at 0 to omit position.")
        loc_note.setStyleSheet("color:#888; font-size:9pt;")
        outer.addWidget(loc_note)

        note = QLabel("⚠ Changing BBS callsign requires a modem restart")
        note.setStyleSheet("color:orange;")
        outer.addWidget(note)

        outer.addStretch()
        return w

    # ── Instances settings ────────────────────────────────────────────────

    def _instances_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setSpacing(6)

        note = QLabel(
            "Each entry defines one modem instance (VARA FM, VARA HF, "
            "Soundmodem or Aether HF). "
            "Select an instance on the left to edit its settings. "
            "Port numbers must be unique."
        )
        note.setWordWrap(True)
        note.setStyleSheet("color:#80848e; font-size:9pt;")
        layout.addWidget(note)

        # ── Data model — list of mutable dicts ────────────────────────
        self._inst_data: list[dict] = []
        self._inst_selected: int = -1
        self._inst_loading: bool = False  # guard against feedback loops

        for cfg in self._config.instances:
            self._inst_data.append({
                "name":             cfg.name,
                "transport":        getattr(cfg, "transport", "VARA_FM"),
                "mode":             "",  # legacy, cleared
                "frequency_label":  cfg.frequency_label,
                "executable_path":  cfg.executable_path,
                "cmd_port":         cfg.cmd_port,
                "data_port":        cfg.data_port,
                "default_path":     cfg.default_path,
                "bandwidth":        getattr(cfg, "bandwidth", None)
                                    or (500 if getattr(cfg, "is_hf", False) else 2300),
                "ptt":              getattr(cfg, "ptt", PTTConfig()),
                "rig_control":      getattr(cfg, "rig_control", RigControlConfig()),
                "soundmodem":       getattr(cfg, "soundmodem", SoundmodemConfig()),
                "aether":           self._aether_for_edit(cfg),
                "host":             getattr(cfg, "host", "127.0.0.1"),
                # The saved instance: fields this tab does not edit
                # (soundcard, auto_restart, restart_delay, …) come from it
                "_base":            cfg,
                "auto_start":       cfg.auto_start,
                "enabled":          cfg.enabled,
            })

        # ── Master-detail splitter ────────────────────────────────────
        inst_splitter = QSplitter(Qt.Orientation.Horizontal)
        inst_splitter.setChildrenCollapsible(False)

        # -- Left: instance list + add/remove buttons --
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(4)

        self._inst_list = QListWidget()
        self._inst_list.setFixedWidth(210)
        self._inst_list.setStyleSheet("""
            QListWidget {
                background-color: #1e1f22;
                border: 1px solid #3b3d44;
                border-radius: 4px;
                outline: none;
            }
            QListWidget::item {
                padding: 6px 8px;
                border-radius: 4px;
                margin: 2px 4px;
                color: #dbdee1;
            }
            QListWidget::item:selected {
                background-color: #5865f2;
                color: #ffffff;
            }
            QListWidget::item:hover:!selected {
                background-color: #404249;
            }
        """)
        left_layout.addWidget(self._inst_list, stretch=1)

        btn_row = QHBoxLayout()
        add_btn = QPushButton("+ Add")
        del_btn = QPushButton("− Remove")
        add_btn.setFixedWidth(80)
        del_btn.setFixedWidth(80)
        add_btn.clicked.connect(self._add_empty_instance)
        del_btn.clicked.connect(self._remove_instance)
        btn_row.addWidget(add_btn)
        btn_row.addWidget(del_btn)
        btn_row.addStretch()
        left_layout.addLayout(btn_row)

        inst_splitter.addWidget(left)

        # -- Right: detail form in scroll area --
        detail_scroll = QScrollArea()
        detail_scroll.setWidgetResizable(True)
        detail_scroll.setFrameShape(QFrame.Shape.NoFrame)

        self._inst_detail = QWidget()
        dl = QVBoxLayout(self._inst_detail)
        dl.setSpacing(8)
        dl.setContentsMargins(8, 4, 8, 4)

        # ── Identity group ────────────────────────────────────────
        id_group = QGroupBox("Identity")
        id_form = QFormLayout(id_group)
        id_form.setFieldGrowthPolicy(
            QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow
        )

        self._inst_name = QLineEdit()
        self._inst_name.setToolTip("Friendly name for this instance (e.g. Primary, HF-40m).")
        self._inst_name.textChanged.connect(self._on_inst_field_changed)
        id_form.addRow("Name:", self._inst_name)

        self._inst_mode = QComboBox()
        self._inst_mode.addItems(["VARA_FM", "VARA_HF", "SOUNDMODEM", "AETHER_HF"])
        self._inst_mode.setToolTip(
            "VARA_FM — VHF/UHF via VARA FM modem\n"
            "VARA_HF — HF via VARA HF modem (needs PTT control)\n"
            "SOUNDMODEM — AX.25 packet via Soundmodem/Direwolf (AGWPE)\n"
            "AETHER_HF — HF via the Aether HF modem (open source, beta)"
        )
        self._inst_mode.currentTextChanged.connect(self._on_inst_mode_changed)
        id_form.addRow("Transport:", self._inst_mode)

        self._inst_freq = QLineEdit()
        self._inst_freq.setPlaceholderText("e.g. 145.050 or 14.105")
        self._inst_freq.setToolTip("Frequency label shown in APRS beacon")
        self._inst_freq.textChanged.connect(self._on_inst_field_changed)
        id_form.addRow("Frequency label:", self._inst_freq)

        chk_row_widget = QWidget()
        chk_row = QHBoxLayout(chk_row_widget)
        chk_row.setContentsMargins(0, 0, 0, 0)
        self._inst_enabled = QCheckBox("Enabled")
        self._inst_enabled.setToolTip("Enable or disable this instance.\nDisabled instances are not started.")
        self._inst_auto_start = QCheckBox("Auto Start")
        self._inst_auto_start.setToolTip(
            "Automatically launch the modem executable\nwhen the BBS starts "
            "(skipped when the modem is already running).")
        self._inst_enabled.stateChanged.connect(self._on_inst_field_changed)
        self._inst_auto_start.stateChanged.connect(self._on_inst_field_changed)
        chk_row.addWidget(self._inst_enabled)
        chk_row.addWidget(self._inst_auto_start)
        chk_row.addStretch()
        id_form.addRow("", chk_row_widget)

        dl.addWidget(id_group)

        # ── VARA Modem group ──────────────────────────────────────
        self._inst_vara_group = QGroupBox("Modem")
        vara_group = self._inst_vara_group
        vara_form = QFormLayout(vara_group)
        vara_form.setFieldGrowthPolicy(
            QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow
        )

        exe_widget = QWidget()
        exe_lay = QHBoxLayout(exe_widget)
        exe_lay.setContentsMargins(0, 0, 0, 0)
        self._inst_exe = QLineEdit()
        self._inst_exe.setToolTip(
            "Full path to the modem executable\n"
            "(e.g. C:\\VARA FM\\VARAFM.exe, or for Aether HF\n"
            "%LOCALAPPDATA%\\Aether HF\\aether-hf.exe).")
        self._inst_exe.textChanged.connect(self._on_inst_field_changed)
        browse_btn = QPushButton("Browse…")
        browse_btn.setFixedWidth(70)
        browse_btn.clicked.connect(self._browse_exe)
        exe_lay.addWidget(self._inst_exe)
        exe_lay.addWidget(browse_btn)
        vara_form.addRow("Exe path:", exe_widget)

        self._inst_cmd_port = QSpinBox()
        self._inst_cmd_port.setRange(1024, 65534)
        self._inst_cmd_port.setToolTip(
            "TCP port for the VARA command/control channel.\nDefault: 8300.\n"
            "For Aether HF this is the 'Host programs' port\n"
            "in Aether's Setup (step 5).")
        self._inst_cmd_port.valueChanged.connect(self._on_inst_field_changed)
        self._inst_cmd_port.valueChanged.connect(
            lambda v: self._inst_data_port.setValue(v + 1))
        vara_form.addRow("CMD port:", self._inst_cmd_port)

        self._inst_data_port = QSpinBox()
        self._inst_data_port.setRange(1025, 65535)
        self._inst_data_port.setReadOnly(True)
        self._inst_data_port.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
        self._inst_data_port.setToolTip("Data port is always CMD port + 1.")
        self._inst_data_port.setStyleSheet("background: #3a3a3a; color: #888;")
        vara_form.addRow("Data port:", self._inst_data_port)

        self._inst_default_path = QLineEdit()
        self._inst_default_path.setPlaceholderText("blank = direct")
        self._inst_default_path.setToolTip("AX.25 digipeater path")
        self._inst_default_path.textChanged.connect(self._on_inst_field_changed)
        vara_form.addRow("Default path:", self._inst_default_path)

        self._inst_bw = QComboBox()
        self._inst_bw.addItems(["500", "2300", "2750"])
        self._inst_bw.setToolTip("VARA HF bandwidth (Hz). Ignored for FM.")
        self._inst_bw.currentTextChanged.connect(self._on_inst_field_changed)
        self._inst_bw_label = QLabel("Bandwidth (Hz):")
        vara_form.addRow(self._inst_bw_label, self._inst_bw)

        dl.addWidget(vara_group)

        # ── Aether HF group ──────────────────────────────────────
        self._inst_aether_group = QGroupBox("Aether HF")
        ae_form = QFormLayout(self._inst_aether_group)
        ae_form.setFieldGrowthPolicy(
            QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow
        )

        ae_note = QLabel(
            "Aether HF is set up in its own window (callsign rules, radio, "
            "keying, sound card). Turn on <b>Host programs</b> in its Setup "
            "(step 5) with the CMD port above. The BBS sets MYCALL and "
            "answers calls while attached."
        )
        ae_note.setWordWrap(True)
        ae_note.setStyleSheet("color:#80848e; font-size:9pt;")
        ae_form.addRow(ae_note)

        self._inst_ae_bw = QComboBox()
        self._inst_ae_bw.addItems(["Aether's own setting", "500", "2300"])
        self._inst_ae_bw.setToolTip(
            "Bandwidth the BBS asks for (BW command) while attached.\n"
            "Aether's own setting: leave Aether on Setup step 4's bandwidth.\n"
            "A 2300 Hz station still answers a 500 Hz call at 500 Hz."
        )
        self._inst_ae_bw.currentTextChanged.connect(self._on_inst_field_changed)
        ae_form.addRow("Bandwidth (Hz):", self._inst_ae_bw)

        self._inst_ae_ctl_port = QSpinBox()
        self._inst_ae_ctl_port.setRange(1, 65535)
        self._inst_ae_ctl_port.setValue(8515)
        self._inst_ae_ctl_port.setToolTip(
            "Aether HF control API port ([control] in station.toml).\n"
            "Default: 8515. Used for status, faults and heard stations."
        )
        self._inst_ae_ctl_port.valueChanged.connect(self._on_inst_field_changed)
        ae_form.addRow("Control API port:", self._inst_ae_ctl_port)

        self._inst_ae_token = QLineEdit()
        self._inst_ae_token.setEchoMode(QLineEdit.EchoMode.Password)
        self._inst_ae_token.setPlaceholderText("not needed on this computer")
        self._inst_ae_token.setToolTip(
            "Control API token — only for an Aether HF on another computer\n"
            "whose [control] bind is not loopback. Stored encrypted."
        )
        self._inst_ae_token.textChanged.connect(self._on_inst_field_changed)
        ae_form.addRow("Control token:", self._inst_ae_token)

        self._inst_ae_monitor = QCheckBox("Show Aether HF status on the dashboard")
        self._inst_ae_monitor.setToolTip(
            "Poll the control API for the modem's state, bandwidth, SNR,\n"
            "faults and the stations it has heard (added to MHeard)."
        )
        self._inst_ae_monitor.toggled.connect(self._on_inst_field_changed)
        ae_form.addRow(self._inst_ae_monitor)

        aprs_widget = QWidget()
        aprs_row = QHBoxLayout(aprs_widget)
        aprs_row.setContentsMargins(0, 0, 0, 0)
        self._inst_ae_aprs = QCheckBox("APRS beacon on the air, every")
        self._inst_ae_aprs.setToolTip(
            "Send the BBS position (Station settings) as an APRS frame in an\n"
            "Aether datagram. Other Aether stations hand it to their APRS\n"
            "programs. Aether's rules decide where this station may send it.")
        self._inst_ae_aprs.toggled.connect(self._on_inst_field_changed)
        self._inst_ae_aprs_iv = QSpinBox()
        self._inst_ae_aprs_iv.setRange(10, 1440)
        self._inst_ae_aprs_iv.setValue(60)
        self._inst_ae_aprs_iv.setSuffix(" min")
        self._inst_ae_aprs_iv.valueChanged.connect(self._on_inst_field_changed)
        aprs_row.addWidget(self._inst_ae_aprs)
        aprs_row.addWidget(self._inst_ae_aprs_iv)
        aprs_row.addStretch()
        ae_form.addRow(aprs_widget)

        self._inst_ae_config = QLineEdit()
        self._inst_ae_config.setPlaceholderText("optional — standalone aetherd only")
        self._inst_ae_config.setToolTip(
            "Passed as --config when Auto Start launches the standalone\n"
            "aetherd daemon. The desktop app uses its own station.toml."
        )
        self._inst_ae_config.textChanged.connect(self._on_inst_field_changed)
        ae_form.addRow("aetherd config:", self._inst_ae_config)

        ae_btn_widget = QWidget()
        ae_btn_row = QHBoxLayout(ae_btn_widget)
        ae_btn_row.setContentsMargins(0, 0, 0, 0)
        ae_find_btn = QPushButton("Find installed")
        ae_find_btn.setToolTip("Look for Aether HF where its installer puts it")
        ae_find_btn.clicked.connect(self._find_aether_exe)
        ae_test_btn = QPushButton("Test connection")
        ae_test_btn.setToolTip("Ask Aether HF's control API for its status")
        ae_test_btn.clicked.connect(self._test_aether)
        ae_btn_row.addWidget(ae_find_btn)
        ae_btn_row.addWidget(ae_test_btn)
        ae_btn_row.addStretch()
        ae_form.addRow("", ae_btn_widget)

        self._ae_test_label = QLabel("")
        self._ae_test_label.setWordWrap(True)
        self._ae_test_label.setStyleSheet("color:#80848e;")
        ae_form.addRow("", self._ae_test_label)

        self._inst_aether_group.setVisible(False)
        dl.addWidget(self._inst_aether_group)

        # ── Soundmodem (AGWPE) group ─────────────────────────────
        self._inst_soundmodem_group = QGroupBox("Soundmodem (AGWPE)")
        sm_form = QFormLayout(self._inst_soundmodem_group)

        self._inst_sm_host = QLineEdit()
        self._inst_sm_host.setPlaceholderText("127.0.0.1")
        self._inst_sm_host.setToolTip("IP address of the Soundmodem/Direwolf host.\nUsually 127.0.0.1 for local.")
        self._inst_sm_host.textChanged.connect(self._on_inst_field_changed)
        sm_form.addRow("Host:", self._inst_sm_host)

        self._inst_sm_port = QSpinBox()
        self._inst_sm_port.setRange(1, 65535)
        self._inst_sm_port.setValue(8000)
        self._inst_sm_port.setToolTip("TCP port for the AGWPE interface.\nSoundmodem default: 8000, Direwolf default: 8000.")
        self._inst_sm_port.valueChanged.connect(self._on_inst_field_changed)
        sm_form.addRow("AGWPE Port:", self._inst_sm_port)

        self._inst_sm_protocol = QComboBox()
        self._inst_sm_protocol.addItems(["AGWPE"])
        self._inst_sm_protocol.setToolTip(
            "AGWPE: Soundmodem and Direwolf both provide it (port 8000).\n"
            "KISS is not supported: use the program's AGWPE port.")
        self._inst_sm_protocol.currentTextChanged.connect(self._on_inst_field_changed)
        sm_form.addRow("Protocol:", self._inst_sm_protocol)

        self._inst_sm_auto_launch = QCheckBox("Auto-launch Soundmodem")
        self._inst_sm_auto_launch.setToolTip("Start the Soundmodem executable\nautomatically with this instance.")
        self._inst_sm_auto_launch.toggled.connect(self._on_inst_field_changed)
        sm_form.addRow(self._inst_sm_auto_launch)

        self._inst_sm_exe = QLineEdit()
        self._inst_sm_exe.setPlaceholderText("Path to Soundmodem.exe")
        self._inst_sm_exe.setToolTip("Full path to the Soundmodem / Direwolf executable.")
        self._inst_sm_exe.textChanged.connect(self._on_inst_field_changed)
        sm_form.addRow("Exe Path:", self._inst_sm_exe)

        self._inst_soundmodem_group.setVisible(False)
        dl.addWidget(self._inst_soundmodem_group)

        # ── PTT Control group ─────────────────────────────────────
        self._inst_ptt_group = QGroupBox("PTT Control  (HF only)")
        ptt_form = QFormLayout(self._inst_ptt_group)

        self._inst_ptt_method = QComboBox()
        self._inst_ptt_method.addItems([
            "VOX", "RTS", "DTR", "DTRRTS", "HAMLIB", "CM108", "OMNIRIG", "FLRIG"
        ])
        self._inst_ptt_method.setToolTip(
            "PTT control method for VARA HF.\n"
            "VOX = radio handles PTT (or VARA FM)\n"
            "RTS/DTR = serial port line (DigiRig, etc.)\n"
            "HAMLIB = rigctld over TCP\n"
            "CM108 = RA-Board USB GPIO\n"
            "OMNIRIG = OmniRig COM server (shared control)\n"
            "FLRIG = FLrig XML-RPC server"
        )
        self._inst_ptt_method.currentTextChanged.connect(
            self._on_inst_field_changed
        )
        ptt_form.addRow("Method:", self._inst_ptt_method)

        ptt_btn_widget = QWidget()
        ptt_btn_row = QHBoxLayout(ptt_btn_widget)
        ptt_btn_row.setContentsMargins(0, 0, 0, 0)
        ptt_setup_btn = QPushButton("⚙ Setup…")
        ptt_setup_btn.setToolTip("Open PTT configuration for this instance")
        ptt_setup_btn.clicked.connect(self._open_ptt_dialog)
        ptt_test_btn = QPushButton("🔴 Test PTT (2 s)")
        ptt_test_btn.setToolTip(
            "Keys the radio for 2 seconds to verify PTT wiring."
        )
        ptt_test_btn.clicked.connect(self._test_ptt)
        ptt_btn_row.addWidget(ptt_setup_btn)
        ptt_btn_row.addWidget(ptt_test_btn)
        ptt_btn_row.addStretch()
        ptt_form.addRow("", ptt_btn_widget)

        self._ptt_test_label = QLabel("")
        self._ptt_test_label.setStyleSheet("color:#80848e;")
        ptt_form.addRow("", self._ptt_test_label)

        dl.addWidget(self._inst_ptt_group)

        # ── Rig Control group ─────────────────────────────────────
        self._inst_rig_group = QGroupBox("Rig Control  (HF only)")
        rig_layout = QVBoxLayout(self._inst_rig_group)

        scan_btn = QPushButton("📡 Scan Channels…")
        scan_btn.setToolTip("Configure frequency scanning for this instance")
        scan_btn.clicked.connect(self._open_scan_dialog)
        rig_layout.addWidget(scan_btn)

        self._inst_scan_label = QLabel("")
        self._inst_scan_label.setStyleSheet("color:#80848e; font-size:9pt;")
        rig_layout.addWidget(self._inst_scan_label)

        dl.addWidget(self._inst_rig_group)

        # ── Port warning ──────────────────────────────────────────
        self._port_warning = QLabel("")
        self._port_warning.setStyleSheet("color:#f23f43; font-weight:bold;")
        dl.addWidget(self._port_warning)

        dl.addStretch()

        detail_scroll.setWidget(self._inst_detail)
        inst_splitter.addWidget(detail_scroll)
        inst_splitter.setStretchFactor(0, 0)   # list: fixed
        inst_splitter.setStretchFactor(1, 1)   # detail: stretches

        layout.addWidget(inst_splitter, stretch=1)

        # ── Populate list widget ──────────────────────────────────
        for d in self._inst_data:
            self._inst_list.addItem(self._inst_list_label(d))

        self._inst_list.currentRowChanged.connect(
            self._on_inst_selection_changed
        )

        if self._inst_data:
            self._inst_list.setCurrentRow(0)
        else:
            self._set_inst_detail_enabled(False)

        return w

    # ── Instance list label ───────────────────────────────────────────────

    @staticmethod
    def _inst_list_label(d: dict) -> str:
        transport = d.get("transport", "")
        if not transport and d.get("mode"):
            transport = f"VARA_{d['mode'].upper()}"
        transport = transport or "VARA_FM"
        # Short display label
        short = {"VARA_FM": "FM", "VARA_HF": "HF",
                 "SOUNDMODEM": "AGWPE", "AETHER_HF": "AETHER"}.get(
                     transport, transport)
        name = d.get("name", "Instance")
        freq = d.get("frequency_label", "")
        parts = [name]
        if freq:
            parts.append(freq)
        return f"{' · '.join(parts)}  [{short}]"

    # ── Selection change: save old → load new ─────────────────────────────

    def _on_inst_selection_changed(self, new_row: int):
        self._save_inst_detail()
        self._inst_selected = new_row
        self._load_inst_detail(new_row)

    def _save_inst_detail(self):
        """Write current form values back to _inst_data[_inst_selected]."""
        idx = self._inst_selected
        if idx < 0 or idx >= len(self._inst_data):
            return
        d = self._inst_data[idx]
        d["name"]             = self._inst_name.text()
        d["transport"]        = self._inst_mode.currentText().upper()
        d["mode"]             = ""  # legacy field, cleared
        d["frequency_label"]  = self._inst_freq.text()
        d["executable_path"]  = self._inst_exe.text()
        d["cmd_port"]         = self._inst_cmd_port.value()
        d["data_port"]        = self._inst_cmd_port.value() + 1
        d["default_path"]     = self._inst_default_path.text()
        d["bandwidth"]        = int(self._inst_bw.currentText())
        d["auto_start"]       = self._inst_auto_start.isChecked()
        d["enabled"]          = self._inst_enabled.isChecked()
        # Soundmodem settings
        sm = d.get("soundmodem", SoundmodemConfig())
        d["soundmodem"] = sm.model_copy(update={
            "host": self._inst_sm_host.text() or "127.0.0.1",
            "port": self._inst_sm_port.value(),
            "protocol": self._inst_sm_protocol.currentText(),
            "auto_launch": self._inst_sm_auto_launch.isChecked(),
            "exe_path": self._inst_sm_exe.text().strip(),
        })
        # Aether HF settings
        ae_bw = self._inst_ae_bw.currentText()
        d["aether"] = d.get("aether", AetherConfig()).model_copy(update={
            "bandwidth": int(ae_bw) if ae_bw.isdigit() else 0,
            "control_port": self._inst_ae_ctl_port.value(),
            "control_token": self._inst_ae_token.text().strip(),
            "monitor": self._inst_ae_monitor.isChecked(),
            "config_file": self._inst_ae_config.text().strip(),
            "aprs_beacon": self._inst_ae_aprs.isChecked(),
            "aprs_interval": self._inst_ae_aprs_iv.value(),
        })
        # PTT method from combo; rest of PTTConfig preserved from dialog
        ptt = d["ptt"]
        d["ptt"] = ptt.model_copy(
            update={"method": self._inst_ptt_method.currentText()}
        )
        # Update list widget label
        item = self._inst_list.item(idx)
        if item:
            item.setText(self._inst_list_label(d))

    def _load_inst_detail(self, idx: int):
        """Populate the detail form from _inst_data[idx]."""
        if idx < 0 or idx >= len(self._inst_data):
            self._set_inst_detail_enabled(False)
            return
        self._set_inst_detail_enabled(True)
        self._inst_loading = True  # suppress _on_inst_field_changed
        d = self._inst_data[idx]

        self._inst_name.setText(d["name"])
        # Show transport (or derive from legacy mode field)
        transport = d.get("transport", "")
        if not transport and d.get("mode"):
            transport = f"VARA_{d['mode'].upper()}"
        self._inst_mode.setCurrentText(transport or "VARA_FM")
        self._inst_freq.setText(d.get("frequency_label", ""))
        self._inst_exe.setText(d.get("executable_path", ""))
        self._inst_cmd_port.setValue(d.get("cmd_port", 8300))
        self._inst_data_port.setValue(d.get("data_port", 8301))
        self._inst_default_path.setText(d.get("default_path", ""))

        bw = str(d.get("bandwidth", 2300))
        if bw in ("500", "2300", "2750"):
            self._inst_bw.setCurrentText(bw)
        else:
            self._inst_bw.setCurrentText("2300")

        ptt_cfg = d.get("ptt", PTTConfig())
        method = (ptt_cfg.method or "VOX").upper()
        if method in ("VOX", "RTS", "DTR", "DTRRTS", "HAMLIB", "CM108", "OMNIRIG", "FLRIG"):
            self._inst_ptt_method.setCurrentText(method)

        # Soundmodem settings
        sm = d.get("soundmodem", SoundmodemConfig())
        self._inst_sm_host.setText(getattr(sm, "host", "127.0.0.1"))
        self._inst_sm_port.setValue(getattr(sm, "port", 8000))
        proto = getattr(sm, "protocol", "AGWPE")
        if proto == "AGWPE":
            self._inst_sm_protocol.setCurrentText(proto)
        self._inst_sm_auto_launch.setChecked(getattr(sm, "auto_launch", False))
        self._inst_sm_exe.setText(getattr(sm, "exe_path", ""))

        # Aether HF settings
        ae = d.get("aether", AetherConfig())
        self._inst_ae_bw.setCurrentText(
            str(ae.bandwidth) if ae.bandwidth else "Aether's own setting")
        self._inst_ae_ctl_port.setValue(ae.control_port)
        self._inst_ae_token.setText(ae.control_token)
        self._inst_ae_monitor.setChecked(ae.monitor)
        self._inst_ae_config.setText(ae.config_file)
        self._inst_ae_aprs.setChecked(ae.aprs_beacon)
        self._inst_ae_aprs_iv.setValue(ae.aprs_interval)
        self._ae_test_label.setText("")

        self._inst_auto_start.setChecked(d.get("auto_start", False))
        self._inst_enabled.setChecked(d.get("enabled", True))

        # Scan channel info
        rc = d.get("rig_control", RigControlConfig())
        has_scan = (rc.method.upper() == "OMNIRIG" and len(rc.channels) > 0)
        if has_scan:
            self._inst_scan_label.setText(
                f"📡 {len(rc.channels)} channel(s) configured"
            )
            self._inst_freq.setEnabled(False)
            self._inst_freq.setToolTip(
                "Frequency is set by the scan table. "
                "Click Scan Channels to edit."
            )
        else:
            self._inst_scan_label.setText("")
            self._inst_freq.setEnabled(True)
            self._inst_freq.setToolTip(
                "Frequency label shown in APRS beacon"
            )

        self._inst_loading = False
        self._update_inst_ptt_state()

    def _set_inst_detail_enabled(self, enabled: bool):
        """Enable or disable the entire detail form."""
        self._inst_detail.setEnabled(enabled)
        if not enabled:
            self._ptt_test_label.setText("")
            self._port_warning.setText("")

    # ── Mode change: update PTT/Rig control state ─────────────────────────

    def _on_inst_mode_changed(self, _text: str):
        if not self._inst_loading:
            self._dirty = True
            self._update_inst_ptt_state()

    def _update_inst_ptt_state(self):
        """Show/hide groups based on transport type."""
        transport = self._inst_mode.currentText().upper()
        is_hf = transport in ("VARA_HF", "AETHER_HF")
        is_aether = transport == "AETHER_HF"
        is_soundmodem = transport == "SOUNDMODEM"
        self._inst_ptt_group.setVisible(is_hf)
        self._inst_rig_group.setVisible(is_hf)
        self._inst_vara_group.setVisible(not is_soundmodem)
        self._inst_soundmodem_group.setVisible(is_soundmodem)
        self._inst_aether_group.setVisible(is_aether)
        # Aether's bandwidth is chosen in its own group
        self._inst_bw.setVisible(not is_aether)
        self._inst_bw_label.setVisible(not is_aether)
        self._inst_vara_group.setTitle(
            "Aether HF host interface (VARA-compatible)" if is_aether
            else "VARA Modem")
        self._inst_ptt_group.setTitle(
            "PTT Control  (only when Aether's keying is 'host program')"
            if is_aether else "PTT Control  (HF only)")
        if not is_hf:
            self._inst_ptt_method.setCurrentText("VOX")
        if (is_aether and not self._inst_loading
                and not self._inst_exe.text().strip()):
            self._find_aether_exe(quiet=True)

    # ── Generic field change handler ──────────────────────────────────────

    def _on_inst_field_changed(self, *_args):
        if not self._inst_loading:
            self._dirty = True
            self._validate_ports()

    # ── PTT dialog ────────────────────────────────────────────────────────

    def _open_ptt_dialog(self):
        idx = self._inst_selected
        if idx < 0 or idx >= len(self._inst_data):
            return
        d = self._inst_data[idx]
        method = self._inst_ptt_method.currentText()
        current_cfg = d.get("ptt", PTTConfig())

        dlg = PTTConfigDialog(self, method, current_cfg)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            d["ptt"] = dlg.get_updated_config()
            self._dirty = True

    # ── Scan channel dialog ───────────────────────────────────────────────

    def _open_scan_dialog(self):
        idx = self._inst_selected
        if idx < 0 or idx >= len(self._inst_data):
            return
        d = self._inst_data[idx]
        current_cfg = d.get("rig_control", RigControlConfig())

        dlg = ScanChannelDialog(self, current_cfg)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            d["rig_control"] = dlg.get_updated_config()
            self._dirty = True
            # Refresh scan label and freq field state
            self._load_inst_detail(idx)

    # ── Add / Remove instance ─────────────────────────────────────────────

    def _add_empty_instance(self):
        base_port = 8300 + len(self._inst_data) * 10
        d = {
            "name":             f"Instance {len(self._inst_data) + 1}",
            "transport":        "VARA_FM",
            "mode":             "",
            "frequency_label":  "",
            "executable_path":  "",
            "cmd_port":         base_port,
            "data_port":        base_port + 1,
            "default_path":     "",
            "bandwidth":        2300,
            "ptt":              PTTConfig(),
            "rig_control":      RigControlConfig(),
            "soundmodem":       SoundmodemConfig(),
            "aether":           AetherConfig(),
            "auto_start":       False,
            "enabled":          True,
        }
        self._inst_data.append(d)
        self._inst_list.addItem(self._inst_list_label(d))
        self._inst_list.setCurrentRow(len(self._inst_data) - 1)
        self._dirty = True

    def _remove_instance(self):
        idx = self._inst_list.currentRow()
        if idx < 0 or idx >= len(self._inst_data):
            return
        self._inst_selected = -1  # prevent save during removal
        self._inst_data.pop(idx)
        self._inst_list.takeItem(idx)
        # Select nearest remaining item
        if self._inst_data:
            new_idx = min(idx, len(self._inst_data) - 1)
            self._inst_list.setCurrentRow(new_idx)
        else:
            self._set_inst_detail_enabled(False)
        self._dirty = True

    # ── Test PTT ──────────────────────────────────────────────────────────

    def _test_ptt(self):
        """Key the selected instance's PTT for 2 seconds to verify wiring."""
        import threading

        idx = self._inst_selected
        if idx < 0 or idx >= len(self._inst_data):
            self._ptt_test_label.setText("⚠ Select an instance first.")
            self._ptt_test_label.setStyleSheet("color:orange;")
            return

        d = self._inst_data[idx]
        method = self._inst_ptt_method.currentText()
        name = d["name"]
        transport = d.get("transport", "VARA_FM")

        if method == "VOX":
            self._ptt_test_label.setText(
                "⚠ PTT method is VOX — nothing to test. "
                "Select RTS, DTR, HAMLIB, CM108, OMNIRIG, or FLRIG."
            )
            self._ptt_test_label.setStyleSheet("color:orange;")
            return

        if transport in ("VARA_FM", "SOUNDMODEM"):
            self._ptt_test_label.setText(
                "ℹ FM/Soundmodem handles PTT internally — test not needed."
            )
            self._ptt_test_label.setStyleSheet("color:#80848e;")
            return

        ptt_cfg = d.get("ptt", PTTConfig())

        self._ptt_test_label.setText(f"🔴 Testing PTT on [{name}]…")
        self._ptt_test_label.setStyleSheet("color:red;font-weight:bold;")

        def _run_test():
            from vara.ptt import create_ptt_driver
            driver = None
            try:
                driver = create_ptt_driver(
                    method=method,
                    port=ptt_cfg.port,
                    hamlib_host=ptt_cfg.hamlib_host,
                    hamlib_port=ptt_cfg.hamlib_port,
                    cm108_device=ptt_cfg.cm108_device,
                    omnirig_rig=ptt_cfg.omnirig_rig,
                    flrig_host=ptt_cfg.flrig_host,
                    flrig_port=ptt_cfg.flrig_port,
                )
                driver.open()
                driver.set_ptt(True)
                import time
                time.sleep(2.0)
                driver.set_ptt(False)
                driver.close()
                self._ptt_test_label.setText(
                    f"✅ PTT test OK — [{name}] keyed for 2 s via {method}."
                )
                self._ptt_test_label.setStyleSheet(
                    "color:#23a55a;font-weight:bold;"
                )
            except Exception as e:
                if driver:
                    try:
                        driver.set_ptt(False)
                        driver.close()
                    except Exception:
                        pass
                self._ptt_test_label.setText(f"❌ PTT test FAILED: {e}")
                self._ptt_test_label.setStyleSheet(
                    "color:#f23f43;font-weight:bold;"
                )

        threading.Thread(
            target=_run_test, name="PTT-Test", daemon=True
        ).start()

    # ── Browse for VARA executable ────────────────────────────────────────

    def _browse_exe(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select Modem Executable",
            filter="Executables (*.exe);;All files (*)"
        )
        if path:
            self._inst_exe.setText(path)

    # ── Aether HF helpers ─────────────────────────────────────────────────

    def _aether_for_edit(self, cfg) -> AetherConfig:
        """The instance's AetherConfig with its control token decrypted."""
        ae = getattr(cfg, "aether", None) or AetherConfig()
        if ae.control_token:
            from core.crypto import decrypt_value
            try:
                plain = decrypt_value(ae.control_token,
                                      self._config.security.machine_secret)
            except Exception:
                plain = ""
            ae = ae.model_copy(update={"control_token": plain})
        return ae

    def _find_aether_exe(self, quiet: bool = False):
        from aether.launcher import find_aether_executable
        path = find_aether_executable()
        if path:
            self._inst_exe.setText(path)
            self._ae_test_label.setText(f"Found {path}")
            self._ae_test_label.setStyleSheet("color:#2ECC71;")
        elif not quiet:
            self._ae_test_label.setText(
                "Aether HF not found. Install it from "
                "github.com/KK4ODA/aether-hf/releases, or Browse… to it."
            )
            self._ae_test_label.setStyleSheet("color:orange;")

    def _test_aether(self):
        import threading
        from aether.control import AetherControlClient, AetherControlError
        from aether.monitor import problems, summarize_status
        from PyQt6.QtCore import QMetaObject, Q_ARG

        host = "127.0.0.1"
        idx = self._inst_selected
        if 0 <= idx < len(self._inst_data):
            host = self._inst_data[idx].get("host", "127.0.0.1") or "127.0.0.1"
        client = AetherControlClient(
            host, self._inst_ae_ctl_port.value(),
            self._inst_ae_token.text().strip(),
        )
        self._ae_test_label.setText("Asking Aether HF…")
        self._ae_test_label.setStyleSheet("color:#80848e;")

        def _work():
            try:
                sm = summarize_status(client.status())
            except AetherControlError as e:
                text, ok = f"✗ {e}", False
            else:
                lines = [
                    f"✓ Aether HF {sm.get('version') or ''} — "
                    f"{sm.get('callsign') or '?'}, "
                    f"{sm.get('bandwidth_hz') or '?'} Hz, {sm.get('state') or '?'}"
                ]
                lines += [f"⚠ {p}" for p in problems(sm)]
                text, ok = "\n".join(lines), len(lines) == 1
            QMetaObject.invokeMethod(
                self, "_show_aether_test",
                Qt.ConnectionType.QueuedConnection,
                Q_ARG(str, text), Q_ARG(bool, ok),
            )

        threading.Thread(target=_work, daemon=True, name="AetherTest").start()

    @pyqtSlot(str, bool)
    def _show_aether_test(self, text: str, ok: bool):
        self._ae_test_label.setText(text)
        self._ae_test_label.setStyleSheet(
            "color:#2ECC71;" if ok else "color:orange;")

    # ── Port validation (across all instances) ────────────────────────────

    def _validate_ports(self):
        """Check for port conflicts across all instances in _inst_data."""
        # First, save current form values so we validate live state
        self._save_inst_detail()

        seen: dict[int, str] = {}  # port → instance name
        conflicts: list[str] = []
        for d in self._inst_data:
            name = d["name"]
            for port_key in ("cmd_port", "data_port"):
                port = d[port_key]
                if port in seen:
                    conflicts.append(
                        f"Port {port}: {seen[port]} ↔ {name}"
                    )
                else:
                    seen[port] = name

        if conflicts:
            self._port_warning.setText(
                "⚠ Port conflicts: " + "; ".join(conflicts)
            )
        else:
            self._port_warning.setText("")

    def _add_tactical_row(self, callsign: str = "", phrase: str = "Priority message received",
                          advertised: bool = True):
        """Add a row to the tactical addresses table."""
        row = self._tactical_table.rowCount()
        self._tactical_table.insertRow(row)
        cs_item = QTableWidgetItem(callsign.upper()[:8])
        ph_item = QTableWidgetItem(phrase)
        adv_item = QTableWidgetItem()
        adv_item.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
        adv_item.setCheckState(Qt.CheckState.Checked if advertised else Qt.CheckState.Unchecked)
        self._tactical_table.setItem(row, 0, cs_item)
        self._tactical_table.setItem(row, 1, ph_item)
        self._tactical_table.setItem(row, 2, adv_item)
        self._tactical_table.scrollToBottom()
        self._dirty = True

    def _remove_tactical_row(self):
        """Remove selected row from the tactical addresses table."""
        rows = sorted({idx.row() for idx in self._tactical_table.selectedIndexes()}, reverse=True)
        for row in rows:
            self._tactical_table.removeRow(row)
        if not rows:
            # Remove last row if nothing selected
            if self._tactical_table.rowCount() > 0:
                self._tactical_table.removeRow(self._tactical_table.rowCount() - 1)
        self._dirty = True

    # ── BBS settings ──────────────────────────────────────────────────────

    def _bbs_tab(self) -> QWidget:
        from PyQt6.QtWidgets import QCheckBox
        w = QWidget()
        form = QFormLayout(w)
        cfg  = self._config.bbs

        self._welcome = QLineEdit(cfg.welcome_message)
        self._welcome.setToolTip(
            "Text shown to stations when they connect to the BBS.\n"
            "Keep it short — sent over RF on every session."
        )
        self._timeout = QSpinBox()
        self._timeout.setRange(30, 3600)
        self._timeout.setValue(cfg.session_timeout)
        self._timeout.setSuffix(" seconds")
        self._timeout.setToolTip(
            "How long to wait for input before disconnecting an idle session.\n"
            "Recommended: 600s (10 min) for slow RF links.\n"
            "Default: 600s"
        )
        self._msg_age = QSpinBox()
        self._msg_age.setRange(1, 365)
        self._msg_age.setValue(cfg.max_message_age)
        self._msg_age.setSuffix(" days")
        self._msg_age.setToolTip(
            "Messages older than this are automatically purged.\n"
            "Keeps the mailbox manageable on limited storage."
        )
        self._max_sess = QSpinBox()
        self._max_sess.setRange(1, 16)
        self._max_sess.setValue(cfg.max_sessions)
        self._max_sess.setToolTip(
            "Maximum concurrent BBS sessions across all transports.\n"
            "Default: 1 (recommended). Values > 1 allow simultaneous\n"
            "sessions on different modems but the shared message store\n"
            "is NOT transactionally isolated — data integrity is not\n"
            "guaranteed with concurrent sessions."
        )

        self._watchdog = QSpinBox()
        self._watchdog.setRange(60, 7200)
        self._watchdog.setValue(cfg.session_watchdog_timeout)
        self._watchdog.setSuffix(" sec")
        self._watchdog.setToolTip(
            "Force-disconnect sessions that exceed this duration.\n"
            "Prevents zombie sessions from permanently blocking the BBS.\n"
            "Default: 1800s (30 min). Use higher values for slow HF links."
        )

        self._show_banner = QCheckBox("Show ASCII art banner on RF connect")
        self._show_banner.setChecked(cfg.show_connect_banner)
        self._show_banner.setToolTip(
            "Sends a 499-byte ASCII art header to stations when they connect via RF.\n"
            "Displays station callsign, sysop, grid square, and frequency.\n"
            "Uncheck for slower links or Winlink-only operation."
        )

        form.addRow("Welcome Message:",   self._welcome)
        form.addRow("Session Timeout:",   self._timeout)
        form.addRow("Message Max Age:",   self._msg_age)
        form.addRow("Max Sessions:",      self._max_sess)
        form.addRow("Session Watchdog:",  self._watchdog)
        form.addRow("Connect Banner:",    self._show_banner)
        return w

    # ── Bulletins settings ────────────────────────────────────────────────

    def _bulletins_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)

        note = QLabel(
            "Define the bulletin areas accepted by this BBS. "
            "Each area can have its own expiry age. "
            "Disable an area to stop accepting bulletins addressed to it."
        )
        note.setWordWrap(True)
        note.setStyleSheet("color:#888;")
        layout.addWidget(note)

        self._bull_table = QTableWidget(0, 4)
        self._bull_table.setToolTip(
            "Bulletin areas this BBS accepts. Common groups: ALL, WX, EMCOMM.\n"
            "Each area can have its own max age and can be enabled/disabled."
        )
        self._bull_table.setHorizontalHeaderLabels(
            ["Area Name", "Description", "Max Age (days)", "Enabled"]
        )
        hdr = self._bull_table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self._bull_table.verticalHeader().setVisible(False)
        layout.addWidget(self._bull_table)

        for area in self._config.bbs.bulletin_areas:
            self._add_bulletin_area_row(area)

        btn_row = QHBoxLayout()
        add_btn = QPushButton("+ Add Area")
        del_btn = QPushButton("− Remove Selected")
        add_btn.clicked.connect(self._add_bullet_area)
        del_btn.clicked.connect(self._remove_bullet_area)
        btn_row.addWidget(add_btn)
        btn_row.addWidget(del_btn)
        btn_row.addStretch()
        layout.addLayout(btn_row)

        tip = QLabel(
            "Well-known groups: ALL  WX  DX  NTS  LOCAL  INFO  ARES  RACES  EMCOMM"
        )
        tip.setStyleSheet("color:#888; font-style:italic;")
        layout.addWidget(tip)
        return w

    def _add_bulletin_area_row(self, cfg: BulletinAreaConfig):
        row = self._bull_table.rowCount()
        self._bull_table.insertRow(row)
        self._bull_table.blockSignals(True)
        self._bull_table.setItem(row, 0, QTableWidgetItem(cfg.name))
        self._bull_table.setItem(row, 1, QTableWidgetItem(cfg.description))
        age_spin = QSpinBox()
        age_spin.setRange(1, 365)
        age_spin.setValue(cfg.max_age)
        age_spin.setSuffix(" days")
        self._bull_table.setCellWidget(row, 2, age_spin)
        chk = QTableWidgetItem()
        chk.setCheckState(
            Qt.CheckState.Checked if cfg.enabled else Qt.CheckState.Unchecked
        )
        self._bull_table.setItem(row, 3, chk)
        self._bull_table.blockSignals(False)

    def _add_bullet_area(self):
        self._add_bulletin_area_row(
            BulletinAreaConfig(name="NEW", description="", max_age=14, enabled=True)
        )

    def _remove_bullet_area(self):
        row = self._bull_table.currentRow()
        if row >= 0:
            self._bull_table.removeRow(row)

    # ── Forwarding settings ───────────────────────────────────────────────

    def _forwarding_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)

        # Server config
        srv_box = QGroupBox("Inbound Forwarding Server")
        srv_form = QFormLayout(srv_box)
        self._fwd_srv_en = QCheckBox("Accept inbound forwarding connections")
        self._fwd_srv_en.setChecked(self._config.bbs.forwarding.server_enabled)
        self._fwd_srv_en.setToolTip(
            "Allow neighbour BBSs to connect and exchange bulletins.\n"
            "Disable if this BBS operates standalone."
        )
        self._fwd_srv_port = QSpinBox()
        self._fwd_srv_port.setRange(1024, 65535)
        self._fwd_srv_port.setValue(self._config.bbs.forwarding.server_port)
        self._fwd_srv_port.setToolTip(
            "TCP port for inbound forwarding connections from neighbour BBSs.\n"
            "Port-forward through your router if neighbours connect via internet."
        )
        srv_form.addRow("", self._fwd_srv_en)
        srv_form.addRow("Listen Port:", self._fwd_srv_port)
        self._fwd_srv_bind = QLineEdit(
            getattr(self._config.bbs.forwarding, "server_bind", "0.0.0.0"))
        self._fwd_srv_bind.setPlaceholderText("0.0.0.0")
        self._fwd_srv_bind.setToolTip(
            "Bind address: 0.0.0.0 = all interfaces (reachable remotely), "
            "127.0.0.1 = localhost only (for testing)")
        srv_form.addRow("Bind Address:", self._fwd_srv_bind)
        srv_note = QLabel(
            "Enable this to let neighbour BBSs connect and push/pull bulletins. "
            "Default binds on all interfaces (0.0.0.0). Port-forward through "
            "your router if the neighbour BBS is on the internet."
        )
        srv_note.setWordWrap(True)
        srv_note.setStyleSheet("color:#888;")
        srv_form.addRow("", srv_note)
        layout.addWidget(srv_box)

        # Neighbour table
        nbr_box = QGroupBox("Outbound Forwarding Neighbours")
        nbr_layout = QVBoxLayout(nbr_box)

        nbr_note = QLabel(
            "Each row is a neighbour BBS to call periodically. "
            "Via TCP uses Host/Port; via RF calls it on the air through the "
            "chosen modem instance (VARA FM, VARA HF or Aether HF), and its "
            "calls to this BBS are answered with the forwarding protocol. "
            "Areas: comma-separated list (e.g. ALL,WX) — leave blank for all areas."
        )
        nbr_note.setWordWrap(True)
        nbr_note.setStyleSheet("color:#888;")
        nbr_layout.addWidget(nbr_note)

        self._fwd_table = QTableWidget(0, 8)
        self._fwd_table.setToolTip(
            "Neighbour BBSs to exchange bulletins with on a schedule.\n"
            "Areas: comma-separated list of bulletin areas to forward (blank = all)."
        )
        self._fwd_table.setHorizontalHeaderLabels(
            ["BBS Callsign", "Via", "Instance", "Host", "Port",
             "Interval (min)", "Areas", "Enabled"]
        )
        fhdr = self._fwd_table.horizontalHeader()
        for col, mode in enumerate((
                "contents", "contents", "contents", "stretch",
                "contents", "contents", "stretch", "contents")):
            fhdr.setSectionResizeMode(
                col, QHeaderView.ResizeMode.Stretch if mode == "stretch"
                else QHeaderView.ResizeMode.ResizeToContents)
        self._fwd_table.verticalHeader().setVisible(False)
        nbr_layout.addWidget(self._fwd_table)

        for nbr in self._config.bbs.forwarding_neighbours:
            self._add_neighbour_row(nbr)

        nbr_btn_row = QHBoxLayout()
        add_nbr = QPushButton("+ Add Neighbour")
        del_nbr = QPushButton("− Remove Selected")
        add_nbr.clicked.connect(self._add_neighbour)
        del_nbr.clicked.connect(self._remove_neighbour)
        nbr_btn_row.addWidget(add_nbr)
        nbr_btn_row.addWidget(del_nbr)
        nbr_btn_row.addStretch()
        nbr_layout.addLayout(nbr_btn_row)
        layout.addWidget(nbr_box)
        return w

    def _add_neighbour_row(self, cfg: ForwardingNeighbourConfig):
        row = self._fwd_table.rowCount()
        self._fwd_table.insertRow(row)
        self._fwd_table.blockSignals(True)
        self._fwd_table.setItem(row, 0, QTableWidgetItem(cfg.bbs_callsign))

        via = QComboBox()
        via.addItems(["TCP", "RF"])
        via.setCurrentText(cfg.via)
        via.setToolTip("TCP: over the network (Host/Port)\n"
                       "RF: called on the air through the instance")
        self._fwd_table.setCellWidget(row, 1, via)

        inst = QComboBox()
        inst.setEditable(True)
        inst.addItems([i.name for i in self._config.instances
                       if not i.is_soundmodem])
        inst.setCurrentText(cfg.instance)
        inst.setToolTip("Modem instance RF calls go through\n"
                        "(VARA FM, VARA HF or Aether HF)")
        self._fwd_table.setCellWidget(row, 2, inst)

        self._fwd_table.setItem(row, 3, QTableWidgetItem(cfg.host))

        port_spin = QSpinBox()
        port_spin.setRange(1024, 65535)
        port_spin.setValue(cfg.port)
        self._fwd_table.setCellWidget(row, 4, port_spin)

        iv_spin = QSpinBox()
        iv_spin.setRange(5, 1440)
        iv_spin.setValue(cfg.interval_minutes)
        iv_spin.setSuffix(" min")
        self._fwd_table.setCellWidget(row, 5, iv_spin)

        self._fwd_table.setItem(row, 6, QTableWidgetItem(", ".join(cfg.areas)))
        chk = QTableWidgetItem()
        chk.setCheckState(
            Qt.CheckState.Checked if cfg.enabled else Qt.CheckState.Unchecked
        )
        self._fwd_table.setItem(row, 7, chk)
        self._fwd_table.blockSignals(False)

    def _add_neighbour(self):
        self._add_neighbour_row(ForwardingNeighbourConfig())

    def _remove_neighbour(self):
        row = self._fwd_table.currentRow()
        if row >= 0:
            self._fwd_table.removeRow(row)



    # ── Alerts settings ───────────────────────────────────────────────────

    # ── Push Notifications settings ──────────────────────────────────────

    def _notifications_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setSpacing(6)

        cfg = self._config.notifications

        intro = QLabel(
            "Push notifications are sent when a priority message arrives "
            "(matching a tactical address). Multiple backends can be active "
            "simultaneously."
        )
        intro.setWordWrap(True)
        intro.setStyleSheet("color:#888; font-size:9pt;")
        layout.addWidget(intro)

        # ── ntfy group ────────────────────────────────────────────────
        ntfy_box = QGroupBox("ntfy  (local + cloud relay)")
        ntfy_form = QFormLayout(ntfy_box)

        self._ntfy_en = QCheckBox("Enable ntfy notifications")
        self._ntfy_en.setChecked(cfg.enabled)
        self._ntfy_en.setToolTip(
            "Enable push notifications via ntfy (self-hosted or cloud).\n"
            "Requires a running ntfy server or ntfy.sh cloud account."
        )
        ntfy_form.addRow("", self._ntfy_en)

        self._ntfy_url = QLineEdit(cfg.ntfy_url)
        self._ntfy_url.setPlaceholderText("http://127.0.0.1:8080")
        self._ntfy_url.setToolTip("URL of your local ntfy server")
        ntfy_form.addRow("Server URL:", self._ntfy_url)

        self._ntfy_topic = QLineEdit(cfg.topic)
        self._ntfy_topic.setPlaceholderText("aether-bbs")
        self._ntfy_topic.setToolTip("ntfy topic name for notifications")
        ntfy_form.addRow("Topic:", self._ntfy_topic)

        self._ntfy_priority = QComboBox()
        self._ntfy_priority.addItems(["min", "low", "default", "high", "urgent"])
        self._ntfy_priority.setToolTip(
            "Notification priority level sent to ntfy.\n"
            "'urgent' overrides Do Not Disturb on most devices."
        )
        idx = self._ntfy_priority.findText(cfg.priority)
        if idx >= 0:
            self._ntfy_priority.setCurrentIndex(idx)
        ntfy_form.addRow("Priority:", self._ntfy_priority)

        self._ntfy_cloud = QCheckBox("Also relay to cloud (ntfy.sh) for iOS push")
        self._ntfy_cloud.setChecked(cfg.cloud_relay)
        self._ntfy_cloud.setToolTip(
            "Posts notifications to both local and ntfy.sh cloud servers.\n"
            "Required for iOS push — the ntfy iOS app needs the cloud relay."
        )
        ntfy_form.addRow("", self._ntfy_cloud)

        self._ntfy_cloud_url = QLineEdit(cfg.cloud_url)
        self._ntfy_cloud_url.setPlaceholderText("https://ntfy.sh")
        self._ntfy_cloud_url.setToolTip(
            "Cloud relay URL — notifications are posted to BOTH local\n"
            "and cloud servers using the same topic name."
        )
        ntfy_form.addRow("Cloud URL:", self._ntfy_cloud_url)

        ntfy_test_btn = QPushButton("📲  Send Test Notification")
        ntfy_test_btn.clicked.connect(self._test_ntfy)
        ntfy_form.addRow("", ntfy_test_btn)

        ntfy_note = QLabel(
            "ntfy is free, self-hosted, and open source. Install from "
            "ntfy.sh or run via Docker. Enable cloud relay for instant "
            "iOS notifications via ntfy.sh."
        )
        ntfy_note.setWordWrap(True)
        ntfy_note.setStyleSheet("color:#888; font-size:9pt;")
        ntfy_form.addRow("", ntfy_note)
        layout.addWidget(ntfy_box)

        # ── Pushover group ────────────────────────────────────────────
        push_box = QGroupBox("Pushover  (iOS / Android)")
        push_form = QFormLayout(push_box)

        self._push_en = QCheckBox("Enable Pushover notifications")
        self._push_en.setChecked(cfg.pushover_enabled)
        self._push_en.setToolTip(
            "Enable push notifications via Pushover (iOS/Android).\n"
            "Requires a Pushover account and API token from pushover.net."
        )
        push_form.addRow("", self._push_en)

        self._push_token = QLineEdit(cfg.pushover_api_token)
        self._push_token.setPlaceholderText("API token from pushover.net")
        self._push_token.setEchoMode(QLineEdit.EchoMode.Password)
        self._push_token.setToolTip(
            "Create an application at pushover.net to get an API token"
        )
        push_form.addRow("API Token:", self._push_token)

        self._push_user = QLineEdit(cfg.pushover_user_key)
        self._push_user.setPlaceholderText("User key from pushover.net")
        self._push_user.setEchoMode(QLineEdit.EchoMode.Password)
        self._push_user.setToolTip(
            "Your personal Pushover user key (not the API token).\n"
            "Found on your Pushover dashboard after login."
        )
        push_form.addRow("User Key:", self._push_user)

        self._push_priority = QSpinBox()
        self._push_priority.setRange(-2, 2)
        self._push_priority.setValue(cfg.pushover_priority)
        self._push_priority.setToolTip(
            "-2 = no alert  |  -1 = quiet  |  0 = normal\n"
            " 1 = high (bypass quiet hours)  |  2 = emergency (repeats)"
        )
        push_form.addRow("Priority:", self._push_priority)

        self._push_sound = QLineEdit(cfg.pushover_sound)
        self._push_sound.setPlaceholderText("siren")
        self._push_sound.setToolTip(
            "Pushover sound name (siren, alien, cosmic, etc.)\n"
            "See pushover.net/api#sounds for full list"
        )
        push_form.addRow("Sound:", self._push_sound)

        push_test_btn = QPushButton("📲  Send Test Notification")
        push_test_btn.clicked.connect(self._test_pushover)
        push_form.addRow("", push_test_btn)

        push_note = QLabel(
            "Pushover requires a one-time $5 app purchase (no subscription). "
            "Instant delivery via APNS/GCM."
        )
        push_note.setWordWrap(True)
        push_note.setStyleSheet("color:#888; font-size:9pt;")
        push_form.addRow("", push_note)
        layout.addWidget(push_box)

        # ── SMS group ─────────────────────────────────────────────────
        sms_box = QGroupBox("SMS  (USB GSM modem)")
        sms_form = QFormLayout(sms_box)

        self._sms_en = QCheckBox("Enable SMS notifications")
        self._sms_en.setChecked(cfg.sms_enabled)
        self._sms_en.setToolTip(
            "Send SMS alerts via a USB GSM modem.\n"
            "Works off-grid — no internet required."
        )
        sms_form.addRow("", self._sms_en)

        self._sms_port = QLineEdit(cfg.sms_serial_port)
        self._sms_port.setPlaceholderText("COM5  or  /dev/ttyUSB0")
        self._sms_port.setToolTip(
            "Serial port for the USB GSM modem.\n"
            "Common: COM3-COM9 on Windows, /dev/ttyUSB0 on Linux."
        )
        sms_form.addRow("Serial Port:", self._sms_port)

        self._sms_baud = QSpinBox()
        self._sms_baud.setRange(300, 115200)
        self._sms_baud.setValue(cfg.sms_baud_rate)
        self._sms_baud.setSuffix(" baud")
        self._sms_baud.setToolTip(
            "Serial baud rate for the GSM modem.\n"
            "Most USB modems default to 9600 or 115200."
        )
        sms_form.addRow("Baud Rate:", self._sms_baud)

        self._sms_numbers = QLineEdit(
            ", ".join(cfg.sms_phone_numbers) if cfg.sms_phone_numbers else ""
        )
        self._sms_numbers.setPlaceholderText("+15551234567, +15559876543")
        self._sms_numbers.setToolTip(
            "Comma-separated phone numbers in international format.\n"
            "Each number receives the priority alert SMS."
        )
        sms_form.addRow("Phone Numbers:", self._sms_numbers)

        sms_btn_row = QHBoxLayout()
        sms_detect_btn = QPushButton("🔍  Detect Modem")
        sms_detect_btn.clicked.connect(self._detect_sms_modem)
        sms_test_btn = QPushButton("📲  Send Test SMS")
        sms_test_btn.clicked.connect(self._test_sms)
        sms_btn_row.addWidget(sms_detect_btn)
        sms_btn_row.addWidget(sms_test_btn)
        sms_btn_row.addStretch()
        sms_form.addRow("", sms_btn_row)

        sms_note = QLabel(
            "Requires a USB GSM modem (SIM800, Huawei E220/E303, etc.) "
            "with a SIM card that has SMS capability. Messages are "
            "truncated to 160 characters."
        )
        sms_note.setWordWrap(True)
        sms_note.setStyleSheet("color:#888; font-size:9pt;")
        sms_form.addRow("", sms_note)
        layout.addWidget(sms_box)

        # ── Event triggers group ──────────────────────────────────────
        trigger_box = QGroupBox("Event Triggers")
        trigger_form = QFormLayout(trigger_box)

        trigger_intro = QLabel(
            "Choose which system events trigger a push notification. "
            "At least one backend above must be enabled."
        )
        trigger_intro.setWordWrap(True)
        trigger_intro.setStyleSheet("color:#888; font-size:9pt;")
        trigger_form.addRow("", trigger_intro)

        self._notify_registration = QCheckBox(
            "New user registration awaiting sysop approval"
        )
        self._notify_registration.setChecked(cfg.notify_on_registration)
        self._notify_registration.setToolTip(
            "Send a push notification when a new station registers\n"
            "and is waiting for sysop approval."
        )
        trigger_form.addRow("", self._notify_registration)

        self._notify_urgent_bulletin = QCheckBox(
            "Urgent bulletin posted (to areas listed below)"
        )
        self._notify_urgent_bulletin.setChecked(cfg.notify_on_urgent_bulletin)
        self._notify_urgent_bulletin.setToolTip(
            "Send a push notification when a bulletin is posted\n"
            "to one of the urgent areas listed below.\n"
            "Also alerts RF users on connect if urgent bulletins exist."
        )
        trigger_form.addRow("", self._notify_urgent_bulletin)

        self._urgent_areas = QLineEdit(
            ", ".join(cfg.urgent_bulletin_areas)
            if cfg.urgent_bulletin_areas else ""
        )
        self._urgent_areas.setPlaceholderText("EMCOMM, WX, ARES")
        self._urgent_areas.setToolTip(
            "Comma-separated bulletin area names considered urgent.\n"
            "Bulletins posted to these areas trigger a push notification\n"
            "and an on-connect alert for RF users."
        )
        trigger_form.addRow("Urgent areas:", self._urgent_areas)

        trigger_note = QLabel(
            "RF users are always notified on connect if unread urgent "
            "bulletins exist, regardless of the push notification toggle."
        )
        trigger_note.setWordWrap(True)
        trigger_note.setStyleSheet("color:#888; font-size:9pt;")
        trigger_form.addRow("", trigger_note)
        layout.addWidget(trigger_box)

        layout.addStretch()
        return w

    # ── Notification test buttons ─────────────────────────────────────────

    def _test_ntfy(self):
        notifier = self._notifiers.get("ntfy")
        if not notifier:
            QMessageBox.warning(self, "ntfy Test", "ntfy notifier not available.")
            return
        if not self._ntfy_en.isChecked():
            QMessageBox.information(
                self, "ntfy Test",
                "ntfy is disabled. Enable it and save settings first."
            )
            return
        # Apply current UI values before testing
        self._apply_notification_config_live()
        notifier.send_test()
        QMessageBox.information(
            self, "ntfy Test",
            "Test notification sent — check your ntfy client."
        )

    def _test_pushover(self):
        notifier = self._notifiers.get("pushover")
        if not notifier:
            QMessageBox.warning(self, "Pushover Test",
                                "Pushover notifier not available.")
            return
        if not self._push_en.isChecked():
            QMessageBox.information(
                self, "Pushover Test",
                "Pushover is disabled. Enable it and save settings first."
            )
            return
        if not self._push_token.text().strip() or not self._push_user.text().strip():
            QMessageBox.warning(
                self, "Pushover Test",
                "API token and user key are required."
            )
            return
        self._apply_notification_config_live()
        notifier.send_test()
        QMessageBox.information(
            self, "Pushover Test",
            "Test notification sent — check your phone."
        )

    def _test_sms(self):
        notifier = self._notifiers.get("sms")
        if not notifier:
            QMessageBox.warning(self, "SMS Test",
                                "SMS notifier not available.")
            return
        if not self._sms_en.isChecked():
            QMessageBox.information(
                self, "SMS Test",
                "SMS is disabled. Enable it and save settings first."
            )
            return
        if not self._sms_port.text().strip():
            QMessageBox.warning(self, "SMS Test",
                                "Serial port is required.")
            return
        self._apply_notification_config_live()
        notifier.send_test()
        QMessageBox.information(
            self, "SMS Test",
            "Test SMS queued — check the configured phone numbers."
        )

    def _detect_sms_modem(self):
        notifier = self._notifiers.get("sms")
        port = self._sms_port.text().strip()
        if not port:
            QMessageBox.warning(self, "Modem Detection",
                                "Enter a serial port first.")
            return
        if not notifier:
            QMessageBox.warning(self, "Modem Detection",
                                "SMS notifier not available.")
            return
        result = notifier.detect_modem(port, self._sms_baud.value())
        QMessageBox.information(self, "Modem Detection", result)

    def _apply_notification_config_live(self):
        """Push current notification UI values into the live config
        so that send_test() picks them up without requiring a full save."""
        cfg = self._cm.current()
        if not cfg:
            return
        ncfg = NotificationConfig(
            enabled=self._ntfy_en.isChecked(),
            ntfy_url=self._ntfy_url.text().strip() or "http://127.0.0.1:8080",
            topic=self._ntfy_topic.text().strip() or "aether-bbs",
            priority=self._ntfy_priority.currentText(),
            cloud_relay=self._ntfy_cloud.isChecked(),
            cloud_url=self._ntfy_cloud_url.text().strip() or "https://ntfy.sh",
            pushover_enabled=self._push_en.isChecked(),
            pushover_api_token=self._push_token.text().strip(),
            pushover_user_key=self._push_user.text().strip(),
            pushover_priority=self._push_priority.value(),
            pushover_sound=self._push_sound.text().strip() or "siren",
            sms_enabled=self._sms_en.isChecked(),
            sms_serial_port=self._sms_port.text().strip(),
            sms_baud_rate=self._sms_baud.value(),
            sms_phone_numbers=[
                n.strip() for n in self._sms_numbers.text().split(",")
                if n.strip()
            ],
            notify_on_registration=self._notify_registration.isChecked(),
            notify_on_urgent_bulletin=self._notify_urgent_bulletin.isChecked(),
            urgent_bulletin_areas=[
                a.strip().upper() for a in self._urgent_areas.text().split(",")
                if a.strip()
            ],
        )
        # Patch the live config in-place
        data = cfg.model_dump()
        data["notifications"] = ncfg.model_dump()
        patched = AppConfig.model_validate(data)
        self._cm.save(patched)

    # ── Alerts settings ──────────────────────────────────────────────────

    def _alerts_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setSpacing(6)

        cfg = self._config.alert

        # Audio alerts group
        audio_box = QGroupBox("Audio Alerts")
        audio_form = QFormLayout(audio_box)

        self._alert_audio_en = QCheckBox(
            "Enable audio alerts (TTS announcements and session tones)"
        )
        self._alert_audio_en.setChecked(cfg.audio_enabled and not cfg.visual_only)
        self._alert_audio_en.setToolTip(
            "Master switch for all audio: TTS priority announcements\n"
            "and session connect/disconnect tones.\n"
            "Uncheck for visual-only alerts (on-screen banners, no sound)."
        )
        self._alert_audio_en.toggled.connect(self._on_audio_toggled)
        audio_form.addRow("", self._alert_audio_en)

        self._session_sounds = QCheckBox("Play tone on RF session connect / disconnect")
        self._session_sounds.setChecked(cfg.session_sounds)
        self._session_sounds.setToolTip(
            "Short notification tones when a remote station connects or\n"
            "disconnects.  Independent of the priority TTS alerts above."
        )
        audio_form.addRow("", self._session_sounds)

        # visual_only is now derived from the audio toggle — hidden from UI
        self._alert_visual_only = QCheckBox()
        self._alert_visual_only.setChecked(cfg.visual_only)
        self._alert_visual_only.setVisible(False)

        self._alert_interval = QSpinBox()
        self._alert_interval.setRange(1, 300)
        self._alert_interval.setValue(cfg.alert_interval)
        self._alert_interval.setSuffix(" seconds")
        self._alert_interval.setToolTip("How often to repeat the TTS announcement")
        audio_form.addRow("Repeat interval:", self._alert_interval)

        self._alert_volume = QDoubleSpinBox()
        self._alert_volume.setRange(0.0, 1.0)
        self._alert_volume.setDecimals(2)
        self._alert_volume.setSingleStep(0.05)
        self._alert_volume.setValue(cfg.volume)
        self._alert_volume.setToolTip("TTS volume (0.0 = silent, 1.0 = maximum)")
        audio_form.addRow("Volume:", self._alert_volume)

        test_btn = QPushButton("🔊  Test Alert Sound")
        test_btn.setToolTip("Play a test announcement using the current settings")
        test_btn.clicked.connect(self._test_alert_audio)
        audio_form.addRow("", test_btn)

        audio_note = QLabel(
            "Requires pyttsx3 (pip install pyttsx3) or platform TTS "
            "(PowerShell/say/espeak). Falls back to silent if none available."
        )
        audio_note.setWordWrap(True)
        audio_note.setStyleSheet("color:#888; font-size:9pt;")
        audio_form.addRow("", audio_note)
        layout.addWidget(audio_box)

        # Visual / accessibility group
        vis_box = QGroupBox("Visual & Accessibility")
        vis_form = QFormLayout(vis_box)

        self._alert_high_contrast = QCheckBox("High-contrast alert (red/white)")
        self._alert_high_contrast.setChecked(cfg.high_contrast)
        self._alert_high_contrast.setToolTip(
            "Use maximum-saturation red/white for alert banners.\n"
            "Improves visibility in bright sunlight or field conditions."
        )
        vis_form.addRow("", self._alert_high_contrast)

        self._alert_large_font = QCheckBox("Large font in alert windows (14pt+)")
        self._alert_large_font.setChecked(cfg.large_font)
        self._alert_large_font.setToolTip(
            "Use 14pt+ font in alert pop-ups for readability.\n"
            "Helpful for operators with impaired vision."
        )
        vis_form.addRow("", self._alert_large_font)

        vis_note = QLabel(
            "High-contrast mode uses maximum saturation red/white for "
            "visibility in bright or low-contrast environments.\n"
            "Large font aids readability for operators with impaired vision."
        )
        vis_note.setWordWrap(True)
        vis_note.setStyleSheet("color:#888; font-size:9pt;")
        vis_form.addRow("", vis_note)
        layout.addWidget(vis_box)

        layout.addStretch()
        return w

    def _on_audio_toggled(self, checked: bool):
        """Sync visual_only and session sounds with the audio master switch."""
        self._alert_visual_only.setChecked(not checked)
        self._session_sounds.setEnabled(checked)

    def _test_alert_audio(self):
        """One-shot TTS test using a detached subprocess.

        Never uses pyttsx3/SAPI5 directly - calling runAndWait() from
        a background thread crashes the process on Windows due to COM
        apartment mismatches with Qt. A subprocess is fully isolated.
        """
        import sys, subprocess, threading

        if self._alert_visual_only.isChecked() or not self._alert_audio_en.isChecked():
            from PyQt6.QtWidgets import QMessageBox
            QMessageBox.information(
                self, "Audio Test",
                "Audio alerts are disabled.\n"
                "Enable audio and uncheck Visual-only to test."
            )
            return

        volume = int(self._alert_volume.value() * 100)
        phrase = "Test alert. Audio is working."

        def _run():
            try:
                if sys.platform == "win32":
                    script = (
                        "Add-Type -AssemblyName System.Speech; "
                        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
                        f"$s.Volume = {volume}; "
                        f"$s.Speak('{phrase}');"
                    )
                    subprocess.run(
                        ["powershell", "-NoProfile", "-NonInteractive",
                         "-Command", script],
                        timeout=20,
                        capture_output=True,
                        creationflags=subprocess.CREATE_NO_WINDOW,
                    )
                elif sys.platform == "darwin":
                    subprocess.run(["say", phrase],
                                   timeout=15, capture_output=True)
                else:
                    subprocess.run(["espeak", "-s", "140", phrase],
                                   timeout=15, capture_output=True)
            except FileNotFoundError:
                log.warning("TTS binary not found (powershell / say / espeak)")
            except Exception as exc:
                log.warning(f"Test audio error: {exc}")

        threading.Thread(target=_run, daemon=True, name="TTS-Test").start()


    # ── File Upload security settings ────────────────────────────────────────

    def _file_upload_tab(self) -> QWidget:
        from PyQt6.QtWidgets import (
            QScrollArea, QCheckBox, QSpinBox, QDoubleSpinBox,
            QPlainTextEdit, QRadioButton, QButtonGroup,
        )
        outer = QScrollArea()
        outer.setWidgetResizable(True)
        w = QWidget()
        outer.setWidget(w)
        layout = QVBoxLayout(w)
        layout.setSpacing(6)
        layout.setContentsMargins(6, 6, 6, 6)
        fu = self._config.file_upload

        # ── Global toggles ────────────────────────────────────────────────
        global_box = QGroupBox("General Upload Policy")
        gf = QFormLayout(global_box)

        self._fu_allow_remote = QCheckBox("Allow remote (RF) uploads")
        self._fu_allow_remote.setChecked(fu.allow_remote_uploads)
        self._fu_allow_remote.setToolTip(
            "Master switch. Uncheck to block ALL RF uploads from any station."
        )
        gf.addRow("", self._fu_allow_remote)

        self._fu_max_size = QSpinBox()
        self._fu_max_size.setRange(1, 50)
        self._fu_max_size.setValue(fu.max_file_bytes // (1024 * 1024))
        self._fu_max_size.setSuffix(" MB")
        self._fu_max_size.setToolTip("Maximum size for a single RF-uploaded file.")
        gf.addRow("Max file size:", self._fu_max_size)

        self._fu_quota = QSpinBox()
        self._fu_quota.setRange(1, 500)
        self._fu_quota.setValue(fu.max_files_per_callsign)
        self._fu_quota.setSuffix(" files")
        self._fu_quota.setToolTip(
            "Maximum number of files a single callsign may have stored at once."
        )
        gf.addRow("Per-callsign quota:", self._fu_quota)

        layout.addWidget(global_box)

        # ── Permission mode ──────────────────────────────────────────────
        mode_box = QGroupBox("Upload Authorization Mode")
        mf = QVBoxLayout(mode_box)

        self._fu_mode_whitelist = QRadioButton(
            "Whitelist (recommended) — only listed callsigns may upload"
        )
        self._fu_mode_blocklist = QRadioButton(
            "Blocklist (legacy) — all callsigns except listed ones may upload"
        )
        self._fu_mode_open = QRadioButton(
            "Open — any connected station may upload (not recommended)"
        )
        self._fu_mode_role = QRadioButton(
            "Role-based — delegate to Security role system (TRUSTED+ only)"
        )

        # Group them so only one can be selected
        mode_group = QButtonGroup(self)
        mode_group.addButton(self._fu_mode_whitelist)
        mode_group.addButton(self._fu_mode_blocklist)
        mode_group.addButton(self._fu_mode_open)
        mode_group.addButton(self._fu_mode_role)

        current_mode = fu.permission_mode.lower()
        if current_mode == "blocklist":
            self._fu_mode_blocklist.setChecked(True)
        elif current_mode == "open":
            self._fu_mode_open.setChecked(True)
        elif current_mode == "role_based":
            self._fu_mode_role.setChecked(True)
        else:
            self._fu_mode_whitelist.setChecked(True)

        self._fu_mode_whitelist.setToolTip(
            "Only callsigns in the whitelist below may upload files via RF.\n"
            "This is the most secure option. All other stations are denied."
        )
        self._fu_mode_blocklist.setToolTip(
            "Any station may upload UNLESS their callsign is in the blocked list.\n"
            "Less secure: new/unknown callsigns can upload by default."
        )
        self._fu_mode_open.setToolTip(
            "Any connected station may upload files. No permission checks.\n"
            "Not recommended for production use."
        )

        mf.addWidget(self._fu_mode_whitelist)
        mf.addWidget(self._fu_mode_blocklist)
        mf.addWidget(self._fu_mode_open)
        mf.addWidget(self._fu_mode_role)
        layout.addWidget(mode_box)

        # ── Upload whitelist ─────────────────────────────────────────────
        wl_box = QGroupBox("Upload Whitelist  (whitelist mode only)")
        wl = QVBoxLayout(wl_box)
        wl.addWidget(QLabel(
            "One callsign per line (case-insensitive).  "
            "Only these stations may upload files via RF.\n"
            "Base callsigns (e.g. KK4ODA) match all SSIDs (KK4ODA-5, KK4ODA-7, etc.).\n"
            "Specific SSIDs (e.g. KK4ODA-5) match only that SSID."
        ))
        self._fu_whitelist = QPlainTextEdit(
            "\n".join(fu.upload_whitelist)
        )
        self._fu_whitelist.setFixedHeight(80)
        self._fu_whitelist.setFont(QFont("Consolas", 9))
        self._fu_whitelist.setToolTip(
            "Callsigns authorized to upload files when in whitelist mode.\n"
            "Sysop GUI uploads always bypass this list."
        )
        wl.addWidget(self._fu_whitelist)
        if not fu.upload_whitelist and current_mode == "whitelist":
            wl_warn = QLabel(
                "⚠  Whitelist is empty — ALL remote uploads will be denied "
                "until at least one callsign is added."
            )
            wl_warn.setWordWrap(True)
            wl_warn.setStyleSheet("color:#E3B341;font-size:8pt;")
            wl.addWidget(wl_warn)
        layout.addWidget(wl_box)

        # ── Extension allowlist ───────────────────────────────────────────
        ext_box = QGroupBox("Allowed File Extensions  (RF uploads only)")
        ef = QVBoxLayout(ext_box)
        ef.addWidget(QLabel(
            "One extension per line, including the dot (e.g. .txt).  "
            "Clear the list to allow all extensions (not recommended)."
        ))
        self._fu_extensions = QPlainTextEdit(
            "\n".join(fu.allowed_extensions)
        )
        self._fu_extensions.setFixedHeight(100)
        self._fu_extensions.setFont(QFont("Consolas", 9))
        self._fu_extensions.setToolTip(
            "Extensions remote stations may upload. Sysop GUI uploads bypass this."
        )
        ef.addWidget(self._fu_extensions)
        layout.addWidget(ext_box)

        # ── Blocked callsigns ─────────────────────────────────────────────
        blk_box = QGroupBox("Blocked Callsigns  (blocklist mode; override in whitelist mode)")
        bl = QVBoxLayout(blk_box)
        bl.addWidget(QLabel(
            "One callsign per line (case-insensitive).\n"
            "Blocklist mode: these stations are denied upload access.\n"
            "Whitelist mode: overrides the whitelist — a callsign on both lists is denied."
        ))
        self._fu_blocked = QPlainTextEdit(
            "\n".join(fu.blocked_callsigns)
        )
        self._fu_blocked.setFixedHeight(60)
        self._fu_blocked.setFont(QFont("Consolas", 9))
        bl.addWidget(self._fu_blocked)
        layout.addWidget(blk_box)

        # ── Virus scanning ────────────────────────────────────────────────
        import sys as _sys
        _on_windows = _sys.platform == "win32"

        if _on_windows:
            av_title = "Virus Scanning  (Windows Defender)"
        else:
            av_title = "Virus Scanning  (Windows only — not available on this platform)"

        av_box = QGroupBox(av_title)
        av = QFormLayout(av_box)

        self._fu_av_enabled = QCheckBox("Enable virus scanning for RF uploads")
        self._fu_av_enabled.setChecked(fu.virus_scan_enabled)
        self._fu_av_enabled.setEnabled(_on_windows)
        self._fu_av_enabled.setToolTip(
            "Uses Windows Defender (MpCmdRun.exe) which ships with "
            "every copy of Windows 10 and 11. No third-party software required."
            if _on_windows else
            "Virus scanning is only available on Windows (Windows Defender). "
            "Not supported on macOS or Linux."
        )
        av.addRow("", self._fu_av_enabled)

        self._fu_av_timeout = QSpinBox()
        self._fu_av_timeout.setRange(5, 300)
        self._fu_av_timeout.setValue(fu.virus_scan_timeout)
        self._fu_av_timeout.setSuffix(" seconds")
        self._fu_av_timeout.setEnabled(_on_windows)
        self._fu_av_timeout.setToolTip(
            "Maximum time to wait for Defender to complete the scan. "
            "Timeout = allow through and log a warning."
        )
        av.addRow("Scan timeout:", self._fu_av_timeout)

        self._fu_quarantine = QCheckBox("Quarantine infected files instead of discarding")
        self._fu_quarantine.setChecked(fu.quarantine_infected)
        self._fu_quarantine.setEnabled(_on_windows)
        self._fu_quarantine.setToolTip(
            "Infected files are saved to the quarantine directory for review "
            "rather than silently discarded."
        )
        av.addRow("", self._fu_quarantine)

        self._fu_qdir = QLineEdit(fu.quarantine_dir)
        self._fu_qdir.setEnabled(_on_windows)
        self._fu_qdir.setToolTip(
            "Directory where infected files are moved. "
            "Relative paths are relative to the BBS working directory."
        )
        av.addRow("Quarantine dir:", self._fu_qdir)

        # Test button
        test_scan_row = QHBoxLayout()
        test_scan_btn = QPushButton("🔍 Test Windows Defender")
        test_scan_btn.setEnabled(_on_windows)
        test_scan_btn.setToolTip(
            "Scan the EICAR test string through Windows Defender to verify it is "
            "working and its definitions are current."
        )
        test_scan_btn.clicked.connect(self._test_virus_scanner)
        test_scan_row.addWidget(test_scan_btn)
        test_scan_row.addStretch()
        av.addRow("", test_scan_row)

        if not _on_windows:
            unavail = QLabel(
                "⚠  Virus scanning requires Windows Defender and is not "
                "available on macOS or Linux. RF uploads on those platforms "
                "are accepted without scanning."
            )
            unavail.setWordWrap(True)
            unavail.setStyleSheet("color:#E3B341;font-size:8pt;")
            av.addRow("", unavail)
        else:
            av_note = QLabel(
                "Windows Defender scans each uploaded file before it is stored. "
                "If Defender is not found or times out the file is allowed through "
                "and a warning is written to the log."
            )
            av_note.setWordWrap(True)
            av_note.setStyleSheet("color:#8B949E;font-size:8pt;")
            av.addRow("", av_note)

        layout.addWidget(av_box)
        layout.addStretch()
        return outer

    def _test_virus_scanner(self):
        """Scan the EICAR test string through Windows Defender to verify it's active."""
        import subprocess, tempfile, os, sys
        from PyQt6.QtWidgets import QMessageBox

        if sys.platform != "win32":
            QMessageBox.information(
                self, "Not Available",
                "Virus scanning is only supported on Windows (Windows Defender)."
            )
            return

        from bbs.filestore import _find_defender
        defender = _find_defender()
        if not defender:
            QMessageBox.critical(
                self, "Defender Not Found",
                "Windows Defender (MpCmdRun.exe) was not found.\n\n"
                "Ensure Windows Security is installed and up to date.\n"
                "Expected location:\n"
                "  C:\\Program Files\\Windows Defender\\MpCmdRun.exe"
            )
            return

        timeout = self._fu_av_timeout.value()

        # EICAR test string — detected by every AV engine as a test threat
        eicar = (
            b"X5O!P%@AP[4\\PZX54(P^)7CC)7}"
            b"$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"
        )
        tmp = tempfile.NamedTemporaryFile(
            prefix="eicar_test_", suffix=".txt", delete=False
        )
        try:
            tmp.write(eicar)
            tmp.flush()
            tmp_path = tmp.name
        finally:
            tmp.close()

        try:
            result = subprocess.run(
                [defender, "-Scan", "-ScanType", "3", "-File", tmp_path,
                 "-DisableRemediation"],
                capture_output=True,
                timeout=timeout,
                text=True,
                creationflags=0x08000000,
            )
            if result.returncode == 2:
                QMessageBox.information(
                    self, "Defender Test Passed",
                    "✔ Windows Defender is working correctly.\n\n"
                    "The EICAR test file was detected as a threat.\n"
                    f"Defender path: {defender}"
                )
            elif result.returncode == 0:
                QMessageBox.warning(
                    self, "Defender Test — No Detection",
                    "⚠ Defender ran but did NOT detect the EICAR test file.\n\n"
                    "This may mean definitions are outdated. Run Windows Update\n"
                    "and try again.\n"
                    f"Defender path: {defender}"
                )
            else:
                QMessageBox.warning(
                    self, "Defender Test — Unexpected Result",
                    f"⚠ Defender returned exit code {result.returncode}.\n\n"
                    f"Output: {result.stdout[:300]}\n"
                    f"Stderr: {result.stderr[:200]}"
                )
        except subprocess.TimeoutExpired:
            QMessageBox.warning(
                self, "Defender Test — Timeout",
                f"Defender did not respond within {timeout}s.\n"
                "Try increasing the scan timeout."
            )
        except Exception as e:
            QMessageBox.critical(self, "Defender Test Error", str(e))
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


    # ── Security settings ──────────────────────────────────────────────────

    def _security_tab(self) -> QWidget:
        from PyQt6.QtWidgets import (
            QScrollArea, QCheckBox, QSpinBox, QRadioButton, QButtonGroup,
        )
        outer = QScrollArea()
        outer.setWidgetResizable(True)
        w = QWidget()
        outer.setWidget(w)
        layout = QVBoxLayout(w)
        layout.setSpacing(6)
        layout.setContentsMargins(6, 6, 6, 6)
        sec = self._config.security

        # ── Operational mode ─────────────────────────────────────────
        mode_box = QGroupBox("Operational Mode")
        mf = QVBoxLayout(mode_box)

        self._sec_mode_restricted = QRadioButton(
            "Restricted — registration + approval required (default)"
        )
        self._sec_mode_open = QRadioButton(
            "Open — anyone can connect and use the BBS"
        )
        self._sec_mode_emcomm = QRadioButton(
            "EMCOMM — emergency lockdown (strict rate limits, passwords required)"
        )

        sec_mode_group = QButtonGroup(self)
        sec_mode_group.addButton(self._sec_mode_restricted)
        sec_mode_group.addButton(self._sec_mode_open)
        sec_mode_group.addButton(self._sec_mode_emcomm)

        mode = sec.operational_mode.lower()
        if mode == "open":
            self._sec_mode_open.setChecked(True)
        elif mode == "emcomm":
            self._sec_mode_emcomm.setChecked(True)
        else:
            self._sec_mode_restricted.setChecked(True)

        self._sec_mode_restricted.setToolTip(
            "New callsigns must REGISTER and be approved by sysop before access.\n"
            "Recommended for most deployments."
        )
        self._sec_mode_open.setToolTip(
            "Any station that connects is auto-approved.\n"
            "Suitable for public-access bulletin boards."
        )
        self._sec_mode_emcomm.setToolTip(
            "Maximum security. Passwords required, aggressive rate limits,\n"
            "file uploads and bulletin posting restricted to sysop only."
        )

        mf.addWidget(self._sec_mode_restricted)
        mf.addWidget(self._sec_mode_open)
        mf.addWidget(self._sec_mode_emcomm)
        layout.addWidget(mode_box)

        # ── Registration & authentication ────────────────────────────
        auth_box = QGroupBox("Authentication")
        af = QFormLayout(auth_box)

        self._sec_require_registration = QCheckBox("Require registration for new callsigns")
        self._sec_require_registration.setChecked(sec.require_registration)
        self._sec_require_registration.setToolTip(
            "When checked, unknown callsigns must use the REGISTER command.\n"
            "When unchecked, unknown callsigns get basic access immediately."
        )
        af.addRow("", self._sec_require_registration)

        self._sec_require_password = QCheckBox("Require password for approved users")
        self._sec_require_password.setChecked(sec.require_password)
        self._sec_require_password.setToolTip(
            "When checked, approved users must set a password on first\n"
            "login and authenticate on every subsequent connection.\n"
            "Users who do not set a password will be disconnected."
        )
        af.addRow("", self._sec_require_password)

        self._sec_allow_plaintext = QCheckBox("Allow plaintext password fallback")
        self._sec_allow_plaintext.setChecked(sec.allow_plaintext_auth)
        self._sec_allow_plaintext.setToolTip(
            "When checked, users can type their password in cleartext\n"
            "as a fallback for dumb terminals that cannot compute HMAC.\n"
            "WARNING: Anyone monitoring the frequency can capture the\n"
            "password. Uncheck to require HMAC-SHA256 authentication only."
        )
        af.addRow("", self._sec_allow_plaintext)

        layout.addWidget(auth_box)

        # ── Rate limiting / auto-block ───────────────────────────────
        limit_box = QGroupBox("Rate Limiting & Auto-Block")
        lf = QFormLayout(limit_box)

        self._sec_fail_threshold = QSpinBox()
        self._sec_fail_threshold.setRange(1, 100)
        self._sec_fail_threshold.setValue(sec.auth_failure_threshold)
        self._sec_fail_threshold.setToolTip(
            "Number of consecutive auth failures before temporary lockout."
        )
        lf.addRow("Auth failure lockout threshold:", self._sec_fail_threshold)

        self._sec_lockout_mins = QSpinBox()
        self._sec_lockout_mins.setRange(1, 1440)
        self._sec_lockout_mins.setValue(sec.auth_lockout_minutes)
        self._sec_lockout_mins.setSuffix(" min")
        self._sec_lockout_mins.setToolTip(
            "Duration of temporary lockout after exceeding failure threshold."
        )
        lf.addRow("Lockout duration:", self._sec_lockout_mins)

        self._sec_auto_block = QSpinBox()
        self._sec_auto_block.setRange(5, 1000)
        self._sec_auto_block.setValue(sec.auto_block_threshold)
        self._sec_auto_block.setToolTip(
            "Total cumulative auth failures before the callsign is\n"
            "automatically BLOCKED (requires sysop to unblock)."
        )
        lf.addRow("Auto-block after failures:", self._sec_auto_block)

        layout.addWidget(limit_box)
        layout.addStretch()
        return outer

    # ── Winlink (Pat) settings ───────────────────────────────────────────

    def _winlink_tab(self) -> QWidget:
        w    = QWidget()
        form = QFormLayout(w)
        cfg  = self._config.winlink

        self._wl_enabled = QCheckBox("Enable Winlink Integration (via Pat)")
        self._wl_enabled.setChecked(cfg.enabled)
        self._wl_enabled.setToolTip(
            "Enable Winlink CMS integration using the Pat client.\n"
            "Allows BBS users to send/receive Winlink messages."
        )

        self._wl_callsign = QLineEdit(cfg.gateway_callsign)
        self._wl_callsign.setPlaceholderText("e.g. KK4ODA-5")
        self._wl_callsign.setToolTip(
            "Your Winlink gateway callsign (with SSID if applicable).\n"
            "This is the callsign registered with the Winlink CMS."
        )

        self._wl_password = QLineEdit(
            cfg.decrypt_gateway_password(self._config.security.machine_secret))
        self._wl_password.setEchoMode(QLineEdit.EchoMode.Password)
        self._wl_password.setPlaceholderText("Winlink secure login password")
        self._wl_password.setToolTip(
            "Your Winlink account password for CMS authentication.\n"
            "Stored encrypted in the config file."
        )

        self._wl_grid = QLineEdit(cfg.grid_square)
        self._wl_grid.setPlaceholderText("e.g. EM73vk")
        self._wl_grid.setToolTip(
            "Maidenhead grid square for your station location.\n"
            "Used by Pat for CMS gateway selection (e.g. EM73vk)."
        )

        # Pat executable path with browse button
        self._wl_pat_exe = QLineEdit(cfg.pat_exe_path)
        self._wl_pat_exe.setPlaceholderText("Path to pat.exe")
        self._wl_pat_exe.setToolTip(
            "Full path to the Pat executable (getpat.io).\n"
            "Leave blank to auto-detect from PATH."
        )
        pat_browse = QPushButton("Browse...")
        pat_browse.clicked.connect(self._browse_pat_exe)
        pat_exe_row = QHBoxLayout()
        pat_exe_row.addWidget(self._wl_pat_exe, 1)
        pat_exe_row.addWidget(pat_browse)

        self._wl_pat_addr = QLineEdit(cfg.pat_http_addr)
        self._wl_pat_addr.setPlaceholderText("localhost:8080")
        self._wl_pat_addr.setToolTip(
            "Address of Pat's HTTP API (host:port).\n"
            "Default: localhost:8080. Change if Pat uses a different port."
        )

        self._wl_auto_launch = QCheckBox("Auto-launch Pat when BBS starts")
        self._wl_auto_launch.setChecked(cfg.auto_launch_pat)
        self._wl_auto_launch.setToolTip(
            "Automatically start Pat as a subprocess when the BBS launches.\n"
            "Uncheck if you run Pat separately or as a system service."
        )

        self._wl_fwd_calls = QLineEdit(", ".join(cfg.forward_calls))
        self._wl_fwd_calls.setPlaceholderText("Comma-separated extra callsigns")
        self._wl_fwd_calls.setToolTip(
            "Additional callsigns to check for Winlink mail besides\n"
            "the gateway callsign. Useful for tactical addresses."
        )

        self._wl_cms_timeout = QSpinBox()
        self._wl_cms_timeout.setRange(30, 600)
        self._wl_cms_timeout.setValue(cfg.cms_timeout)
        self._wl_cms_timeout.setSuffix(" seconds")
        self._wl_cms_timeout.setToolTip(
            "Timeout for CMS poll operations via Pat.\n"
            "Increase if you have a slow internet connection."
        )

        # Pat status label
        self._wl_pat_status = QLabel("")
        self._wl_pat_status.setStyleSheet("font-size:9pt;")
        self._update_pat_status()

        # Test connection button
        test_btn = QPushButton("Test Pat Connection")
        test_btn.clicked.connect(self._test_pat_connection)

        form.addRow("", self._wl_enabled)

        pat_group = QGroupBox("Pat Client")
        pat_form = QFormLayout(pat_group)
        pat_form.addRow("Pat Executable:", pat_exe_row)
        pat_form.addRow("Pat HTTP Address:", self._wl_pat_addr)
        pat_form.addRow("", self._wl_auto_launch)
        pat_form.addRow("", self._wl_pat_status)
        pat_form.addRow("", test_btn)
        form.addRow(pat_group)

        acct_group = QGroupBox("Winlink Account")
        acct_form = QFormLayout(acct_group)
        acct_form.addRow("Callsign:", self._wl_callsign)
        acct_form.addRow("Password:", self._wl_password)
        acct_form.addRow("Grid Square:", self._wl_grid)
        form.addRow(acct_group)

        cms_group = QGroupBox("CMS Settings")
        cms_form = QFormLayout(cms_group)
        cms_form.addRow("Forward For:", self._wl_fwd_calls)
        cms_form.addRow("CMS Timeout:", self._wl_cms_timeout)
        form.addRow(cms_group)

        note = QLabel(
            "Winlink integration uses Pat (getpat.io) to handle all CMS\n"
            "communication including B2F protocol and authentication.\n"
            "\n"
            "CMS sync is on-demand only: use 'Poll Now' in the Winlink\n"
            "panel, or RF users can type WL to check their mail.\n"
            "This prevents consuming mail that users may want to\n"
            "retrieve via other Winlink gateways.\n"
            "\n"
            "Changes take effect on next Save. Pat will be restarted if running."
        )
        note.setStyleSheet("color:#aaa;font-size:9pt;")
        form.addRow("", note)

        return w

    def _instance_updates(self, d: dict, freq_label: str) -> dict:
        """The instance fields this tab edits, as InstanceConfig values."""
        return {
            "name":            d["name"],
            "transport":       d.get("transport", "VARA_FM"),
            "mode":            "",
            "frequency_label": freq_label,
            "executable_path": d.get("executable_path", ""),
            "cmd_port":        d.get("cmd_port", 8300),
            "data_port":       d.get("data_port", 8301),
            "default_path":    d.get("default_path", ""),
            "bandwidth":       d.get("bandwidth", 2300),
            "ptt":             d.get("ptt", PTTConfig()),
            "rig_control":     d.get("rig_control", RigControlConfig()),
            "soundmodem":      d.get("soundmodem", SoundmodemConfig()),
            "aether":          self._aether_for_save(d.get("aether", AetherConfig())),
            "host":            d.get("host", "127.0.0.1"),
            "auto_start":      d.get("auto_start", False),
            "enabled":         d.get("enabled", True),
        }

    def _aether_for_save(self, ae: AetherConfig) -> AetherConfig:
        """Encrypt the control token before it is written to the config."""
        machine = self._config.security.machine_secret
        if (ae.control_token and machine
                and not ae.control_token.startswith("ENC:")):
            from core.crypto import encrypt_value
            ae = ae.model_copy(update={"control_token": encrypt_value(
                ae.control_token, machine)})
        return ae

    def _encrypt_winlink_password(self, plaintext: str) -> str:
        """Encrypt a Winlink password for storage in the config file."""
        from core.crypto import encrypt_value
        machine = self._config.security.machine_secret
        return encrypt_value(plaintext, machine)

    def _browse_pat_exe(self):
        """Open file dialog to select pat.exe."""
        path, _ = QFileDialog.getOpenFileName(
            self, "Select Pat Executable",
            self._wl_pat_exe.text() or "",
            "Executables (*.exe);;All Files (*)",
        )
        if path:
            self._wl_pat_exe.setText(path)
            self._update_pat_status()

    def _update_pat_status(self):
        """Check Pat installation and update status label."""
        import os
        exe = self._wl_pat_exe.text().strip()
        if not exe:
            # Try auto-detect
            from winlink.pat_client import PatClient
            found = PatClient.find_pat_exe()
            if found:
                self._wl_pat_status.setText(f"Pat found at: {found}")
                self._wl_pat_status.setStyleSheet("color:#4a4;font-size:9pt;")
            else:
                self._wl_pat_status.setText("Pat not found - set the path above")
                self._wl_pat_status.setStyleSheet("color:#a44;font-size:9pt;")
        elif os.path.isfile(exe):
            from winlink.pat_client import PatClient
            version = PatClient.get_pat_version(exe)
            if version:
                self._wl_pat_status.setText(f"Pat {version}")
            else:
                self._wl_pat_status.setText("Pat found")
            self._wl_pat_status.setStyleSheet("color:#4a4;font-size:9pt;")
        else:
            self._wl_pat_status.setText(f"File not found: {exe}")
            self._wl_pat_status.setStyleSheet("color:#a44;font-size:9pt;")

    def _test_pat_connection(self):
        """Test connectivity to Pat HTTP API."""
        addr = self._wl_pat_addr.text().strip() or "localhost:8080"
        try:
            from urllib.request import Request, urlopen
            url = f"http://{addr}/api/mailbox/in"
            req = Request(url)
            resp = urlopen(req, timeout=5)
            resp.read()
            QMessageBox.information(
                self, "Pat Connection",
                f"Successfully connected to Pat at {addr}",
            )
        except Exception as e:
            err = str(e)
            if "10061" in err or "Connection refused" in err:
                msg = f"Pat is not running on {addr}.\nStart Pat first or check the address."
            elif "timed out" in err.lower():
                msg = f"Pat at {addr} is not responding (timeout)."
            else:
                msg = f"Cannot connect to Pat at {addr}:\n{err}"
            QMessageBox.warning(self, "Pat Connection", msg)

    # ── Outbound Delivery settings tabs ─────────────────────────────────────

    def _delivery_tab(self) -> QWidget:
        from gui.delivery_tabs import OutboundDeliveryTab
        self._od_tab = OutboundDeliveryTab(self._config)
        w = self._od_tab.build()
        if self._delivery_engine:
            self._od_tab.set_delivery_engine(self._delivery_engine)
            self._od_tab.start_health_timer()
        return w

    def _smtp_providers_tab(self) -> QWidget:
        from gui.delivery_tabs import SMTPProvidersTab
        self._smtp_tab = SMTPProvidersTab(self._config)
        return self._smtp_tab.build()

    def _inbound_email_tab(self) -> QWidget:
        from gui.delivery_tabs import InboundEmailTab
        self._ie_tab = InboundEmailTab(self._config)
        return self._ie_tab.build()

    def _winlink_forward_tab(self) -> QWidget:
        from gui.delivery_tabs import WinlinkForwardTab
        self._wf_tab = WinlinkForwardTab(self._config)
        return self._wf_tab.build()

    # ── APRS settings ─────────────────────────────────────────────────────

    def _aprs_tab(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        cfg  = self._config.aprs

        self._aprs_en  = QCheckBox("Enable APRS-IS Gateway")
        self._aprs_en.setChecked(cfg.enabled)
        self._aprs_en.setToolTip(
            "Connect to APRS-IS to beacon BBS status and receive APRS messages.\n"
            "Requires internet access to reach the APRS-IS network."
        )
        self._is_srv   = QLineEdit(cfg.is_server)
        self._is_srv.setToolTip(
            "APRS-IS server hostname (e.g. rotate.aprs2.net).\n"
            "Use a regional server for lower latency."
        )
        self._is_port  = QSpinBox()
        self._is_port.setRange(1, 65535)
        self._is_port.setValue(cfg.is_port)
        self._is_port.setToolTip(
            "APRS-IS server port. Standard: 14580 (filtered feed).\n"
            "Use 14580 unless your server requires a different port."
        )
        self._is_pc    = QSpinBox()
        self._is_pc.setRange(-1, 99999)
        self._is_pc.setValue(cfg.is_passcode)
        self._is_pc.setToolTip(
            "APRS-IS passcode for your callsign (required for TX).\n"
            "Generate at aprs-is.net or similar tools. -1 = receive only."
        )
        self._is_filter= QLineEdit(cfg.is_filter)
        self._is_filter.setToolTip(
            "APRS-IS server-side filter string (e.g. r/35.0/-85.0/100).\n"
            "Limits received packets to a geographic area or type."
        )

        form.addRow("", self._aprs_en)
        form.addRow("IS Server:",   self._is_srv)
        form.addRow("IS Port:",     self._is_port)
        form.addRow("Passcode:",    self._is_pc)
        form.addRow("IS Filter:",   self._is_filter)
        return w

    # ── Logging settings ──────────────────────────────────────────────────

    def _logging_tab(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        cfg  = self._config.logging

        self._log_level = QComboBox()
        self._log_level.addItems(["DEBUG", "INFO", "WARNING", "ERROR"])
        self._log_level.setCurrentText(cfg.level)
        self._log_level.setToolTip(
            "Minimum severity written to the log file.\n"
            "INFO is recommended. Use DEBUG for troubleshooting."
        )
        self._log_dir   = QLineEdit(cfg.log_dir)
        self._log_dir.setToolTip(
            "Directory where log files are written.\n"
            "Relative paths are relative to the BBS working directory."
        )
        self._pkt_log   = QCheckBox("Log decoded packet frames")
        self._pkt_log.setChecked(cfg.packet_log)
        self._pkt_log.setToolTip(
            "Write decoded packet data to a separate packet.log file.\n"
            "Useful for diagnosing RF link issues. Disable in normal operation."
        )

        self._vara_cmd_log = QCheckBox("Log raw VARA FM command port traffic")
        self._vara_cmd_log.setChecked(cfg.vara_cmd_log)
        self._vara_cmd_log.setToolTip(
            "Writes every raw line sent to and received from the VARA FM TCP\n"
            "command port to vara_cmd.log in the log directory.\n"
            "Enable this when diagnosing unexpected disconnects or session\n"
            "timeout issues to see exactly what VARA FM is sending.\n"
            "Disable when not needed — leave off in normal operation."
        )

        self._chat_log = QCheckBox("Log sysop ↔ remote chat conversations")
        self._chat_log.setChecked(cfg.chat_log)
        self._chat_log.setToolTip(
            "Appends a timestamped record of every CH chat session to\n"
            "chat.log in the log directory.  Enabled by default."
        )

        form.addRow("Log Level:",    self._log_level)
        form.addRow("Log Directory:",self._log_dir)
        form.addRow("",              self._pkt_log)
        form.addRow("",              self._vara_cmd_log)
        form.addRow("",              self._chat_log)

        note = QLabel("⚠ Log level and directory changes take effect on next restart")
        note.setStyleSheet("color:orange;")
        form.addRow("", note)
        return w

    # ── Outbound Delivery config builder ─────────────────────────────────────

    def _build_outbound_delivery_config(self):
        from delivery.config import (
            OutboundDeliveryConfig, SMTPDeliveryConfig,
            SMTPProviderModel,
            InboundEmailConfig, WinlinkForwardConfig,
        )

        # Get values from the delivery tab widgets
        od_vals = {}
        if hasattr(self, "_od_tab"):
            od_vals = self._od_tab.get_config_values()

        # Get SMTP providers from the SMTP tab
        smtp_providers = []
        if hasattr(self, "_smtp_tab"):
            for pd in self._smtp_tab.get_providers():
                smtp_providers.append(SMTPProviderModel(
                    provider_id=pd["provider_id"],
                    display_name=pd["display_name"],
                    host=pd["host"],
                    port=pd["port"],
                    tls_mode=pd["tls_mode"],
                    username=pd["username"],
                    password=pd["password"],
                    sender_email=pd["sender_email"],
                    sender_name=pd["sender_name"],
                    enabled=pd["enabled"],
                    priority=pd["priority"],
                    failure_threshold=pd.get("failure_threshold", 3),
                    max_attempts=pd.get("max_attempts", 5),
                ))

        # Get MTA config from the MTA tab


        # Get inbound email config
        ie_vals = {}
        if hasattr(self, "_ie_tab"):
            ie_vals = self._ie_tab.get_config_values()

        # Get winlink forward config
        wf_vals = {}
        if hasattr(self, "_wf_tab"):
            wf_vals = self._wf_tab.get_config_values()

        return OutboundDeliveryConfig(
            engine_enabled=od_vals.get("engine_enabled", False),
            engine_poll_seconds=od_vals.get("engine_poll_seconds", 30),
            message_expiry_days=od_vals.get("message_expiry_days", 7),
            smtp=SMTPDeliveryConfig(
                enabled=od_vals.get("smtp_enabled", False),
                providers=smtp_providers,
            ),
            inbound_email=InboundEmailConfig(
                enabled=ie_vals.get("enabled", False),
                protocol=ie_vals.get("protocol", "IMAP"),
                host=ie_vals.get("host", ""),
                port=ie_vals.get("port", 993),
                use_ssl=ie_vals.get("use_ssl", True),
                username=ie_vals.get("username", ""),
                password=ie_vals.get("password", ""),
                folder=ie_vals.get("folder", "INBOX"),
                bbs_domain=ie_vals.get("bbs_domain", ""),
                poll_interval=ie_vals.get("poll_interval", 5),
                delete_after_fetch=ie_vals.get("delete_after_fetch", False),
                max_message_size=ie_vals.get("max_message_size", 50000),
                allowed_senders=ie_vals.get("allowed_senders", []),
            ),
            winlink_forward=WinlinkForwardConfig(
                mode=wf_vals.get("mode", "MANUAL"),
                max_calls=wf_vals.get("max_calls", 50),
            ),
        )

    # ── Save / Revert ─────────────────────────────────────────────────────

    # ── About & Updates ───────────���────────────────────────────────────────

    def _updater_tab(self) -> QWidget:
        from core.version import get_version_display
        w = QWidget()
        form = QFormLayout(w)
        cfg = self._config.updater

        # Version display
        ver_label = QLabel(f"<b>{get_version_display()}</b>")
        ver_label.setStyleSheet("font-size: 13pt;")
        form.addRow("Version:", ver_label)

        form.addRow("", QLabel(""))  # spacer

        # Check for updates button + status
        btn_row = QWidget()
        btn_lay = QHBoxLayout(btn_row)
        btn_lay.setContentsMargins(0, 0, 0, 0)
        self._upd_check_btn = QPushButton("Check for Updates")
        self._upd_check_btn.setFixedWidth(180)
        self._upd_check_btn.clicked.connect(self._on_check_updates)
        btn_lay.addWidget(self._upd_check_btn)
        self._upd_status = QLabel("")
        self._upd_status.setStyleSheet("color:#b5bac1;")
        btn_lay.addWidget(self._upd_status)
        btn_lay.addStretch()
        form.addRow("", btn_row)

        form.addRow("", QLabel(""))  # spacer

        # Config toggles
        self._upd_check_startup = QCheckBox("Check for updates on startup")
        self._upd_check_startup.setChecked(cfg.check_on_startup)
        form.addRow("", self._upd_check_startup)


        self._upd_interval = QSpinBox()
        self._upd_interval.setRange(0, 168)
        self._upd_interval.setValue(cfg.auto_check_interval_hours)
        self._upd_interval.setSuffix(" hours")
        self._upd_interval.setToolTip(
            "Periodic background check interval (0 = startup only)"
        )
        form.addRow("Auto-check interval:", self._upd_interval)

        return w

    def _on_check_updates(self):
        """Manual 'Check for Updates' button clicked."""
        self._upd_check_btn.setEnabled(False)
        self._upd_status.setText("Checking ...")
        self._upd_status.setStyleSheet("color:#e2a84b;")

        import threading
        from core.updater import Updater
        from pathlib import Path

        def _check():
            updater = Updater(
                repo_dir=Path(__file__).resolve().parent.parent,
                event_bus=self._bus,
                config=self._config.updater,
            )
            result = updater.check_for_updates()
            # Schedule GUI update on main thread via QTimer
            from PyQt6.QtCore import QTimer
            QTimer.singleShot(0, lambda: self._on_check_result(result))

        threading.Thread(target=_check, daemon=True, name="ManualUpdateCheck").start()

    def _on_check_result(self, result):
        self._upd_check_btn.setEnabled(True)
        if result.error:
            self._upd_status.setText(f"Error: {result.error}")
            self._upd_status.setStyleSheet("color:#ed4245;")
        elif result.update_available:
            n = len(result.commits)
            self._upd_status.setText(
                f"{n} new commit(s) available!  Use the status bar indicator to update."
            )
            self._upd_status.setStyleSheet("color:#57f287;")
        else:
            self._upd_status.setText("Up to date.")
            self._upd_status.setStyleSheet("color:#57f287;")

    def _save(self):
        if self._port_warning.text():
            QMessageBox.warning(self, "Port Conflict",
                "Please fix port conflicts before saving.")
            return

        try:
            cfg = self._build_config()
            self._cm.save(cfg)
            self._config = cfg
            self._dirty  = False
            self._bus.emit(EventType.CONFIG_CHANGED, data={"config": cfg})
            QMessageBox.information(self, "Saved", "Settings saved successfully.")
            log.info("Settings saved by sysop")
        except Exception as e:
            QMessageBox.critical(self, "Save Failed", str(e))
            log.error(f"Settings save failed: {e}")

    def _revert(self):
        cfg = self._cm.reload()
        self._config = cfg
        # Simplest revert: rebuild the tab (recreate widgets)
        QMessageBox.information(self, "Reverted",
            "Settings reverted to last saved state. Please close and reopen settings.")

    def _build_config(self) -> AppConfig:
        from core.config import (StationConfig, BBSConfig, APRSConfig,
                                  LoggingConfig, GUIConfig, InstanceConfig,
                                  PTTConfig, RigControlConfig,
                                  BulletinAreaConfig, ForwardingNeighbourConfig,
                                  ForwardingConfig, AlertConfig,
                                  FileUploadConfig, WinlinkConfig, SecurityConfig,
                                  UpdaterConfig)
        from delivery.config import OutboundDeliveryConfig

        # ── Instances ─────────────────────────────────────────────────────
        # Save current detail form to the model before building config
        self._save_inst_detail()

        instances = []
        for d in self._inst_data:
            freq_label = d.get("frequency_label", "")
            # Strip scan placeholders so they don't persist to TOML
            if (freq_label.strip().startswith("📡")
                    or freq_label.strip() == "(from scan table)"):
                freq_label = ""

            base = d.get("_base") or InstanceConfig()
            instances.append(InstanceConfig.model_validate({
                **base.model_dump(),
                **self._instance_updates(d, freq_label),
            }))

        # ── Bulletin areas ────────────────────────────────────────────────
        bulletin_areas = []
        for row in range(self._bull_table.rowCount()):
            def bcell(c): return (self._bull_table.item(row, c) or QTableWidgetItem("")).text()
            def bchk(c): return (self._bull_table.item(row, c) or QTableWidgetItem()).checkState() == Qt.CheckState.Checked
            age_widget = self._bull_table.cellWidget(row, 2)
            max_age = age_widget.value() if age_widget else 14
            name = bcell(0).strip().upper()
            if name:
                bulletin_areas.append(BulletinAreaConfig(
                    name=name,
                    description=bcell(1),
                    max_age=max_age,
                    enabled=bchk(3),
                ))

        # ── Forwarding neighbours ─────────────────────────────────────────
        neighbours = []
        for row in range(self._fwd_table.rowCount()):
            def fcell(c): return (self._fwd_table.item(row, c) or QTableWidgetItem("")).text()
            def fchk(c): return (self._fwd_table.item(row, c) or QTableWidgetItem()).checkState() == Qt.CheckState.Checked
            via_widget  = self._fwd_table.cellWidget(row, 1)
            inst_widget = self._fwd_table.cellWidget(row, 2)
            port_widget = self._fwd_table.cellWidget(row, 4)
            iv_widget   = self._fwd_table.cellWidget(row, 5)
            port = port_widget.value() if port_widget else 7300
            iv   = iv_widget.value()   if iv_widget   else 60
            call = fcell(0).strip().upper()
            if call and call != "N0CALL-1":
                raw_areas = [a.strip().upper() for a in fcell(6).split(",") if a.strip()]
                neighbours.append(ForwardingNeighbourConfig(
                    bbs_callsign=call,
                    via=via_widget.currentText() if via_widget else "TCP",
                    instance=inst_widget.currentText().strip() if inst_widget else "",
                    host=fcell(3).strip() or "127.0.0.1",
                    port=port,
                    interval_minutes=iv,
                    areas=raw_areas,
                    enabled=fchk(7),
                ))

        # ── File upload policy ───────────────────────────────────────────
        fu_exts = [
            e.strip() for e in
            self._fu_extensions.toPlainText().splitlines()
            if e.strip()
        ]
        fu_blocked = [
            c.strip().upper() for c in
            self._fu_blocked.toPlainText().splitlines()
            if c.strip()
        ]
        # Determine permission mode from radio buttons
        if self._fu_mode_blocklist.isChecked():
            fu_mode = "blocklist"
        elif self._fu_mode_open.isChecked():
            fu_mode = "open"
        elif self._fu_mode_role.isChecked():
            fu_mode = "role_based"
        else:
            fu_mode = "whitelist"
        # Parse whitelist
        fu_whitelist = [
            c.strip().upper() for c in
            self._fu_whitelist.toPlainText().splitlines()
            if c.strip()
        ]

        return AppConfig(
            station=StationConfig(
                bbs_callsign=self._bbs_call.text(),
                sysop_callsign=self._sysop_call.text(),
                grid_square=self._grid.text(),
                latitude=self._latitude.value() or None,
                longitude=self._longitude.value() or None,
                beacon_interval=self._beacon_iv.value(),
                beacon_text=self._beacon_txt.text(),
                tactical_addresses=[
                    TacticalAddress(
                        callsign=(self._tactical_table.item(r, 0).text() or "").upper()[:8],
                        alert_phrase=(self._tactical_table.item(r, 1).text()
                                      or "Priority message received"),
                        advertised=(self._tactical_table.item(r, 2) is not None
                                    and self._tactical_table.item(r, 2).checkState()
                                    == Qt.CheckState.Checked),
                    )
                    for r in range(self._tactical_table.rowCount())
                    if self._tactical_table.item(r, 0)
                    and self._tactical_table.item(r, 0).text().strip()
                ],
            ),
            bbs=BBSConfig(
                welcome_message=self._welcome.text(),
                show_connect_banner=self._show_banner.isChecked(),
                session_timeout=self._timeout.value(),
                max_message_age=self._msg_age.value(),
                max_sessions=self._max_sess.value(),
                session_watchdog_timeout=self._watchdog.value(),
                bulletin_areas=bulletin_areas,
                forwarding=ForwardingConfig(
                    server_enabled=self._fwd_srv_en.isChecked(),
                    server_port=self._fwd_srv_port.value(),
                    server_bind=self._fwd_srv_bind.text().strip() or "0.0.0.0",
                ),
                forwarding_neighbours=neighbours,
            ),
            aprs=APRSConfig(
                enabled=self._aprs_en.isChecked(),
                is_server=self._is_srv.text(),
                is_port=self._is_port.value(),
                is_passcode=self._is_pc.value(),
                is_filter=self._is_filter.text(),
            ),
            alert=AlertConfig(
                audio_enabled=self._alert_audio_en.isChecked(),
                visual_only=self._alert_visual_only.isChecked(),
                alert_interval=self._alert_interval.value(),
                volume=self._alert_volume.value(),
                high_contrast=self._alert_high_contrast.isChecked(),
                large_font=self._alert_large_font.isChecked(),
                session_sounds=self._session_sounds.isChecked(),
            ),
            logging=LoggingConfig(
                level=self._log_level.currentText(),
                log_dir=self._log_dir.text(),
                packet_log=self._pkt_log.isChecked(),
                vara_cmd_log=self._vara_cmd_log.isChecked(),
                chat_log=self._chat_log.isChecked(),
            ),
            winlink=WinlinkConfig(
                enabled=self._wl_enabled.isChecked(),
                gateway_callsign=self._wl_callsign.text().strip().upper(),
                gateway_password=self._encrypt_winlink_password(
                    self._wl_password.text()),
                grid_square=self._wl_grid.text().strip(),
                pat_exe_path=self._wl_pat_exe.text().strip(),
                pat_http_addr=self._wl_pat_addr.text().strip() or "localhost:8080",
                auto_launch_pat=self._wl_auto_launch.isChecked(),
                forward_calls=[
                    c.strip().upper()
                    for c in self._wl_fwd_calls.text().split(",")
                    if c.strip()
                ],
                cms_timeout=self._wl_cms_timeout.value(),
            ),
            outbound_delivery=self._build_outbound_delivery_config(),
            file_upload=FileUploadConfig(
                allow_remote_uploads=self._fu_allow_remote.isChecked(),
                permission_mode=fu_mode,
                upload_whitelist=fu_whitelist,
                max_file_bytes=self._fu_max_size.value() * 1024 * 1024,
                max_files_per_callsign=self._fu_quota.value(),
                allowed_extensions=fu_exts,
                blocked_callsigns=fu_blocked,
                virus_scan_enabled=self._fu_av_enabled.isChecked(),
                virus_scan_timeout=self._fu_av_timeout.value(),
                quarantine_infected=self._fu_quarantine.isChecked(),
                quarantine_dir=self._fu_qdir.text().strip() or "quarantine",
            ),
            notifications=NotificationConfig(
                enabled=self._ntfy_en.isChecked(),
                ntfy_url=self._ntfy_url.text().strip() or "http://127.0.0.1:8080",
                topic=self._ntfy_topic.text().strip() or "aether-bbs",
                priority=self._ntfy_priority.currentText(),
                cloud_relay=self._ntfy_cloud.isChecked(),
                cloud_url=self._ntfy_cloud_url.text().strip() or "https://ntfy.sh",
                pushover_enabled=self._push_en.isChecked(),
                pushover_api_token=self._push_token.text().strip(),
                pushover_user_key=self._push_user.text().strip(),
                pushover_priority=self._push_priority.value(),
                pushover_sound=self._push_sound.text().strip() or "siren",
                sms_enabled=self._sms_en.isChecked(),
                sms_serial_port=self._sms_port.text().strip(),
                sms_baud_rate=self._sms_baud.value(),
                sms_phone_numbers=[
                    n.strip() for n in self._sms_numbers.text().split(",")
                    if n.strip()
                ],
                notify_on_registration=self._notify_registration.isChecked(),
                notify_on_urgent_bulletin=self._notify_urgent_bulletin.isChecked(),
                urgent_bulletin_areas=[
                    a.strip().upper()
                    for a in self._urgent_areas.text().split(",")
                    if a.strip()
                ],
            ),
            security=SecurityConfig(
                operational_mode=(
                    "open" if self._sec_mode_open.isChecked()
                    else "emcomm" if self._sec_mode_emcomm.isChecked()
                    else "restricted"
                ),
                require_registration=self._sec_require_registration.isChecked(),
                require_password=self._sec_require_password.isChecked(),
                auth_failure_threshold=self._sec_fail_threshold.value(),
                auth_lockout_minutes=self._sec_lockout_mins.value(),
                auto_block_threshold=self._sec_auto_block.value(),
                machine_secret=self._config.security.machine_secret,
                allow_plaintext_auth=self._sec_allow_plaintext.isChecked(),
            ),
            updater=UpdaterConfig(
                check_on_startup=self._upd_check_startup.isChecked(),
                auto_check_interval_hours=self._upd_interval.value(),
                branch=self._config.updater.branch,
            ),
            instances=instances,
        )
