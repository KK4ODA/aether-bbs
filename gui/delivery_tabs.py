"""
gui/delivery_tabs.py

GUI settings tabs for the Bidirectional Delivery system.

Provides six tabs:
  1. Outbound Delivery — master enable/disable, backend ordering, queue monitor
  2. Winlink Settings  — callsign, password, transport selector (API-upgradeable)
  3. SMTP Providers    — multi-provider config with add/remove, test, logs
  4. Local MTA         — relay config, test, queue depth view
  5. Inbound Email     — IMAP/POP3 polling config, address mapping, sender filter
  6. Winlink Forwarding — ;FW list mode, auto-populate from registered users

All tabs are constructed as QWidget factories that integrate into the
existing SettingsTab sub-tab layout.
"""

import logging
from typing import Optional

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QFormLayout,
    QGroupBox, QLabel, QLineEdit, QSpinBox, QDoubleSpinBox,
    QCheckBox, QComboBox, QPushButton, QTableWidget,
    QTableWidgetItem, QHeaderView, QMessageBox, QTextEdit,
    QListWidget, QSplitter, QFrame, QScrollArea,
)
from PyQt6.QtCore import Qt

log = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
# Tab 1: Outbound Delivery (master controls)
# ═══════════════════════════════════════════════════════════════════════════════

class OutboundDeliveryTab:
    """Builder for the Outbound Delivery settings tab."""

    def __init__(self, config):
        self._config = config
        self.widgets = {}

    def build(self) -> QWidget:
        od = self._config.outbound_delivery
        w = QWidget()
        layout = QVBoxLayout(w)

        # ── Master enable ────────────────────────────────────────────────
        self.widgets["engine_enabled"] = QCheckBox(
            "Enable Outbound Delivery Engine"
        )
        self.widgets["engine_enabled"].setChecked(od.engine_enabled)
        self.widgets["engine_enabled"].setToolTip(
            "When enabled, the delivery engine processes the outbound\n"
            "queue and routes messages through the configured backends.\n"
            "When disabled, messages remain queued but no delivery\n"
            "attempts are made."
        )
        layout.addWidget(self.widgets["engine_enabled"])

        # ── Backend enable/disable ───────────────────────────────────────
        backends_group = QGroupBox("Backend Enable/Disable")
        bg_layout = QVBoxLayout(backends_group)

        self.widgets["winlink_enabled"] = QCheckBox(
            "Winlink CMS via Pat (primary)"
        )
        self.widgets["winlink_enabled"].setChecked(self._config.winlink.enabled)
        self.widgets["winlink_enabled"].setToolTip(
            "Enable Winlink CMS as a delivery backend.\n"
            "Also configured in the Winlink (Pat) settings tab."
        )
        bg_layout.addWidget(self.widgets["winlink_enabled"])

        self.widgets["smtp_enabled"] = QCheckBox(
            "SMTP fallback (used only when Winlink CMS is unavailable)"
        )
        self.widgets["smtp_enabled"].setChecked(od.smtp.enabled)
        self.widgets["smtp_enabled"].setToolTip(
            "When enabled, email-addressed messages fall back to\n"
            "SMTP providers if the Winlink CMS is unreachable."
        )
        bg_layout.addWidget(self.widgets["smtp_enabled"])

        layout.addWidget(backends_group)

        # ── Engine tuning ────────────────────────────────────────────────
        engine_group = QGroupBox("Engine Settings")
        eg_form = QFormLayout(engine_group)

        self.widgets["poll_seconds"] = QSpinBox()
        self.widgets["poll_seconds"].setRange(5, 3600)
        self.widgets["poll_seconds"].setValue(od.engine_poll_seconds)
        self.widgets["poll_seconds"].setSuffix(" seconds")
        self.widgets["poll_seconds"].setToolTip(
            "How often the delivery engine checks the outbound\n"
            "queue for new messages to send (in seconds)."
        )
        eg_form.addRow("Queue Poll Interval:", self.widgets["poll_seconds"])

        self.widgets["expiry_days"] = QSpinBox()
        self.widgets["expiry_days"].setRange(1, 365)
        self.widgets["expiry_days"].setValue(od.message_expiry_days)
        self.widgets["expiry_days"].setSuffix(" days")
        self.widgets["expiry_days"].setToolTip(
            "Messages that remain undelivered after this many\n"
            "days are marked as expired and no longer retried."
        )
        eg_form.addRow("Message Expiry:", self.widgets["expiry_days"])

        layout.addWidget(engine_group)

        # ── Backend health indicators ────────────────────────────────────
        health_group = QGroupBox("Backend Health (live)")
        hg_layout = QVBoxLayout(health_group)

        self.widgets["health_winlink"] = QLabel("Winlink (primary): —")
        self.widgets["health_smtp"] = QLabel("SMTP (fallback): —")

        for lbl in [self.widgets["health_winlink"],
                     self.widgets["health_smtp"]]:
            lbl.setStyleSheet("font-family: monospace; padding: 2px;")
            hg_layout.addWidget(lbl)

        layout.addWidget(health_group)

        # ── Queue depth ──────────────────────────────────────────────────
        queue_group = QGroupBox("Queue Depth")
        qg_layout = QVBoxLayout(queue_group)

        self.widgets["queue_display"] = QLabel(
            "PENDING: 0  |  ATTEMPTING: 0  |  FAILED: 0  |  DELIVERED: 0"
        )
        self.widgets["queue_display"].setStyleSheet(
            "font-family: monospace; padding: 4px;"
        )
        qg_layout.addWidget(self.widgets["queue_display"])

        layout.addWidget(queue_group)

        # ── Info note ────────────────────────────────────────────────────
        note = QLabel(
            "📡 The delivery engine routes messages through backends based\n"
            "on recipient type (callsign → Winlink, email → SMTP).\n"
            "Failover is deterministic: SMTP providers are tried in\n"
            "priority order, then Local MTA, then Winlink.\n"
            "\n"
            "Messages persist in the queue until delivered or expired.\n"
            "All state survives application restarts."
        )
        note.setStyleSheet("color:#aaa; font-size:9pt;")
        layout.addWidget(note)

        layout.addStretch()
        return w

    def set_delivery_engine(self, engine):
        """Wire the delivery engine for live health and queue updates."""
        self._engine = engine
        self._refresh_health()  # initial update

    def start_health_timer(self):
        """Start periodic health refresh. Call after build() and set_delivery_engine()."""
        from PyQt6.QtCore import QTimer
        self._health_timer = QTimer()
        self._health_timer.timeout.connect(self._refresh_health)
        self._health_timer.start(3000)  # refresh every 3s

    def _refresh_health(self):
        """Update health indicators and queue depth from the engine."""
        if not hasattr(self, "_engine") or not self._engine:
            return
        try:
            health = self._engine.get_backend_health_summary()
            wl = health.get("WINLINK", "—")
            smtp = health.get("SMTP", "—")
            color_map = {
                "HEALTHY": "#57f287", "DEGRADED": "#f0b232",
                "UNAVAILABLE": "#ed4245", "DISABLED": "#888",
            }
            wl_color = color_map.get(wl, "#888")
            smtp_color = color_map.get(smtp, "#888")
            self.widgets["health_winlink"].setText(f"Winlink (primary): {wl}")
            self.widgets["health_winlink"].setStyleSheet(
                f"color:{wl_color}; font-family:monospace; padding:2px;")
            self.widgets["health_smtp"].setText(f"SMTP (fallback): {smtp}")
            self.widgets["health_smtp"].setStyleSheet(
                f"color:{smtp_color}; font-family:monospace; padding:2px;")

            # Queue depth
            stats = self._engine.get_queue_depth()
            pending = stats.get("PENDING", 0)
            attempting = stats.get("ATTEMPTING", 0)
            failed = stats.get("FAILED", 0)
            delivered = stats.get("DELIVERED", 0)
            self.widgets["queue_display"].setText(
                f"PENDING: {pending}  |  ATTEMPTING: {attempting}  "
                f"|  FAILED: {failed}  |  DELIVERED: {delivered}"
            )
        except Exception:
            pass

    def get_config_values(self) -> dict:
        return {
            "engine_enabled":      self.widgets["engine_enabled"].isChecked(),
            "engine_poll_seconds": self.widgets["poll_seconds"].value(),
            "message_expiry_days": self.widgets["expiry_days"].value(),
            "smtp_enabled":        self.widgets["smtp_enabled"].isChecked(),
        }


# ═══════════════════════════════════════════════════════════════════════════════
# Tab 2: Winlink Settings (API-upgradeable transport selector)
# ═══════════════════════════════════════════════════════════════════════════════
# NOTE: The existing Winlink CMS tab in settings.py already covers
# most Winlink configuration.  This extended version adds the
# transport selector for future CMS API support.

class WinlinkDeliveryTab:
    """Extended Winlink settings with transport selector."""

    def __init__(self, config):
        self._config = config
        self.widgets = {}

    def build(self) -> QWidget:
        cfg = self._config.winlink
        w = QWidget()
        form = QFormLayout(w)

        # ── Transport selector ───────────────────────────────────────────
        transport_group = QGroupBox("Winlink Transport")
        tg_form = QFormLayout(transport_group)

        self.widgets["transport_type"] = QComboBox()
        self.widgets["transport_type"].addItems([
            "Pat Client",
        ])
        self.widgets["transport_type"].setCurrentIndex(0)

        tg_form.addRow("Transport:", self.widgets["transport_type"])

        # ── Status indicator ─────────────────────────────────────────────
        self.widgets["transport_status"] = QLabel("⚪ Not connected")
        self.widgets["transport_status"].setStyleSheet(
            "font-family: monospace; padding: 4px;"
        )
        tg_form.addRow("Status:", self.widgets["transport_status"])

        # ── Test button ──────────────────────────────────────────────────
        self.widgets["test_btn"] = QPushButton("Test Winlink Connection")
        tg_form.addRow("", self.widgets["test_btn"])

        form.addRow(transport_group)

        # ── Info ─────────────────────────────────────────────────────────
        info = QLabel(
            "Winlink delivery uses Pat (getpat.io) to communicate\n"
            "with the Winlink CMS via internet (telnet/TLS).\n"
            "\n"
            "Pat is the primary delivery method for all messages.\n"
            "SMTP providers (if configured) are used as fallback\n"
            "for email addresses when Winlink CMS is unavailable."
        )
        info.setStyleSheet("color:#aaa; font-size:9pt;")
        info.setWordWrap(True)
        form.addRow(info)

        return w

    def get_transport_type(self) -> str:
        return "PAT_CLIENT"


# ═══════════════════════════════════════════════════════════════════════════════
# Tab 3: SMTP Providers (master-detail with quick-add presets)
# ═══════════════════════════════════════════════════════════════════════════════

class SMTPProvidersTab:
    """Multi-provider SMTP configuration — master-detail layout."""

    # Quick-add presets for common providers
    PRESETS = {
        "Gmail":    {"host": "smtp.gmail.com",                       "port": 587, "tls": "STARTTLS"},
        "SendGrid": {"host": "smtp.sendgrid.net",                   "port": 587, "tls": "STARTTLS"},
        "Mailgun":  {"host": "smtp.mailgun.org",                    "port": 587, "tls": "STARTTLS"},
        "AWS SES":  {"host": "email-smtp.us-east-1.amazonaws.com",  "port": 587, "tls": "STARTTLS"},
        "Outlook":  {"host": "smtp.office365.com",                  "port": 587, "tls": "STARTTLS"},
    }

    def __init__(self, config):
        self._config = config
        self.widgets = {}
        self._data: list[dict] = []
        self._selected: int = -1
        self._loading: bool = False

    def build(self) -> QWidget:
        od = self._config.outbound_delivery
        smtp_cfg = od.smtp

        # Populate data model from config
        for p in smtp_cfg.providers:
            self._data.append({
                "display_name": p.display_name,
                "host":         p.host,
                "port":         p.port,
                "tls_mode":     p.tls_mode,
                "username":     p.username,
                "password":     p.password,
                "sender_email": p.sender_email,
                "sender_name":  p.sender_name,
                "enabled":      p.enabled,
            })

        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setSpacing(6)

        # ── Fallback notice ──────────────────────────────────────────
        notice = QLabel(
            "FALLBACK: SMTP providers are used only when Winlink CMS\n"
            "is unavailable. Winlink (via Pat) is the primary delivery\n"
            "method for all outbound messages including email addresses."
        )
        notice.setStyleSheet("color:#c90; font-size:9pt;")
        notice.setWordWrap(True)
        layout.addWidget(notice)

        # ── Master-detail splitter ────────────────────────────────────
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)

        # -- Left: provider list + buttons --
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(4)

        list_header = QLabel("Providers  (top = highest priority)")
        list_header.setStyleSheet(
            "color:#80848e; font-size:8pt; padding:0 0 2px 2px;"
        )
        left_layout.addWidget(list_header)

        self._provider_list = QListWidget()
        self._provider_list.setFixedWidth(200)
        self._provider_list.setStyleSheet("""
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
        left_layout.addWidget(self._provider_list, stretch=1)

        # Button rows
        add_row = QHBoxLayout()
        add_row.setSpacing(4)
        add_btn = QPushButton("+ Add")
        add_btn.setFixedWidth(60)
        add_btn.clicked.connect(self._add_empty_provider)
        add_row.addWidget(add_btn)

        self._preset_combo = QComboBox()
        self._preset_combo.addItem("Quick Add...")
        self._preset_combo.addItems(list(self.PRESETS.keys()))
        self._preset_combo.setFixedWidth(130)
        self._preset_combo.activated.connect(self._on_preset_selected)
        add_row.addWidget(self._preset_combo)

        left_layout.addLayout(add_row)

        move_row = QHBoxLayout()
        move_row.setSpacing(4)
        rm_btn = QPushButton("Remove")
        rm_btn.setFixedWidth(60)
        rm_btn.clicked.connect(self._remove_selected)
        up_btn = QPushButton("\u25b2")
        up_btn.setFixedWidth(30)
        up_btn.setToolTip("Move up (higher priority)")
        up_btn.clicked.connect(self._move_up)
        down_btn = QPushButton("\u25bc")
        down_btn.setFixedWidth(30)
        down_btn.setToolTip("Move down (lower priority)")
        down_btn.clicked.connect(self._move_down)
        move_row.addWidget(rm_btn)
        move_row.addStretch()
        move_row.addWidget(up_btn)
        move_row.addWidget(down_btn)
        left_layout.addLayout(move_row)

        splitter.addWidget(left)

        # -- Right: detail form --
        detail_scroll = QScrollArea()
        detail_scroll.setWidgetResizable(True)
        detail_scroll.setFrameShape(QFrame.Shape.NoFrame)

        self._detail = QWidget()
        dl = QVBoxLayout(self._detail)
        dl.setSpacing(8)
        dl.setContentsMargins(8, 4, 8, 4)

        # Server group
        server_group = QGroupBox("Server")
        sf = QFormLayout(server_group)
        sf.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        sf.setSpacing(6)

        self._name_edit = QLineEdit()
        self._name_edit.setPlaceholderText("e.g. Gmail, SendGrid")
        self._name_edit.textChanged.connect(self._on_field_changed)
        sf.addRow("Display name:", self._name_edit)

        self._host_edit = QLineEdit()
        self._host_edit.setPlaceholderText("smtp.example.com")
        self._host_edit.setToolTip("SMTP server hostname (e.g. smtp.gmail.com).")
        self._host_edit.textChanged.connect(self._on_field_changed)
        sf.addRow("Host:", self._host_edit)

        port_row = QWidget()
        pr_layout = QHBoxLayout(port_row)
        pr_layout.setContentsMargins(0, 0, 0, 0)
        pr_layout.setSpacing(12)
        self._port_spin = QSpinBox()
        self._port_spin.setRange(1, 65535)
        self._port_spin.setValue(587)
        self._port_spin.setToolTip("SMTP port. Common: 587 (STARTTLS), 465 (SSL), 25 (plain).")
        self._port_spin.valueChanged.connect(self._on_field_changed)
        pr_layout.addWidget(self._port_spin)
        pr_layout.addWidget(QLabel("TLS:"))
        self._tls_combo = QComboBox()
        self._tls_combo.addItems(["STARTTLS", "SSL", "NONE"])
        self._tls_combo.setToolTip(
            "STARTTLS — upgrade plain connection to TLS (port 587)\n"
            "SSL — connect over TLS from the start (port 465)\n"
            "NONE — no encryption (not recommended)"
        )
        self._tls_combo.currentTextChanged.connect(self._on_field_changed)
        pr_layout.addWidget(self._tls_combo)
        pr_layout.addStretch()
        sf.addRow("Port:", port_row)

        dl.addWidget(server_group)

        # Credentials group
        cred_group = QGroupBox("Credentials")
        cf = QFormLayout(cred_group)
        cf.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        cf.setSpacing(6)

        self._user_edit = QLineEdit()
        self._user_edit.setPlaceholderText("SMTP username or API key")
        self._user_edit.textChanged.connect(self._on_field_changed)
        cf.addRow("Username:", self._user_edit)

        self._pass_edit = QLineEdit()
        self._pass_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self._pass_edit.setPlaceholderText("SMTP password")
        self._pass_edit.textChanged.connect(self._on_field_changed)
        cf.addRow("Password:", self._pass_edit)

        dl.addWidget(cred_group)

        # Sender group
        sender_group = QGroupBox("Sender Identity")
        xf = QFormLayout(sender_group)
        xf.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        xf.setSpacing(6)

        self._sender_email = QLineEdit()
        self._sender_email.setPlaceholderText("bbs@yourdomain.com")
        self._sender_email.setToolTip("From address shown to recipients of outbound email.")
        self._sender_email.textChanged.connect(self._on_field_changed)
        xf.addRow("Sender email:", self._sender_email)

        self._sender_name = QLineEdit()
        self._sender_name.setPlaceholderText("Aether BBS")
        self._sender_name.setToolTip(
            "Display name shown to email recipients\n"
            "(e.g. 'Aether BBS' or your callsign)."
        )
        self._sender_name.textChanged.connect(self._on_field_changed)
        xf.addRow("Sender name:", self._sender_name)

        dl.addWidget(sender_group)

        # Options
        self._enabled_chk = QCheckBox("Enabled")
        self._enabled_chk.setToolTip(
            "Enable or disable this SMTP provider.\n"
            "Disabled providers are skipped during delivery."
        )
        self._enabled_chk.stateChanged.connect(self._on_field_changed)
        dl.addWidget(self._enabled_chk)

        dl.addStretch()

        detail_scroll.setWidget(self._detail)
        splitter.addWidget(detail_scroll)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)

        layout.addWidget(splitter, stretch=1)

        # ── Populate list ─────────────────────────────────────────────
        for d in self._data:
            self._provider_list.addItem(self._list_label(d))

        self._provider_list.currentRowChanged.connect(self._on_selection_changed)

        if self._data:
            self._provider_list.setCurrentRow(0)
        else:
            self._detail.setEnabled(False)

        return w

    # ── List label ────────────────────────────────────────────────────

    @staticmethod
    def _list_label(d: dict) -> str:
        name = d.get("display_name", "Provider")
        host = d.get("host", "")
        enabled = d.get("enabled", True)
        status = "" if enabled else "  [off]"
        if host:
            short_host = host.split(".")[0] if "." in host else host
            return f"{name}\n{short_host}{status}"
        return f"{name}{status}"

    # ── Selection change ──────────────────────────────────────────────

    def _on_selection_changed(self, new_row: int):
        self._save_detail()
        self._selected = new_row
        self._load_detail(new_row)

    def _save_detail(self):
        idx = self._selected
        if idx < 0 or idx >= len(self._data):
            return
        d = self._data[idx]
        d["display_name"] = self._name_edit.text()
        d["host"]         = self._host_edit.text()
        d["port"]         = self._port_spin.value()
        d["tls_mode"]     = self._tls_combo.currentText()
        d["username"]     = self._user_edit.text()
        d["password"]     = self._pass_edit.text()
        d["sender_email"] = self._sender_email.text()
        d["sender_name"]  = self._sender_name.text()
        d["enabled"]      = self._enabled_chk.isChecked()
        # Refresh list label
        item = self._provider_list.item(idx)
        if item:
            item.setText(self._list_label(d))

    def _load_detail(self, idx: int):
        if idx < 0 or idx >= len(self._data):
            self._detail.setEnabled(False)
            return
        self._detail.setEnabled(True)
        self._loading = True
        d = self._data[idx]
        self._name_edit.setText(d.get("display_name", ""))
        self._host_edit.setText(d.get("host", ""))
        self._port_spin.setValue(d.get("port", 587))
        tls = d.get("tls_mode", "STARTTLS")
        if tls in ("STARTTLS", "SSL", "NONE"):
            self._tls_combo.setCurrentText(tls)
        self._user_edit.setText(d.get("username", ""))
        self._pass_edit.setText(d.get("password", ""))
        self._sender_email.setText(d.get("sender_email", ""))
        self._sender_name.setText(d.get("sender_name", ""))
        self._enabled_chk.setChecked(d.get("enabled", True))
        self._loading = False

    def _on_field_changed(self, *_args):
        if not self._loading:
            self._save_detail()

    # ── Add / Remove / Move ───────────────────────────────────────────

    def _add_empty_provider(self):
        d = {
            "display_name": f"Provider {len(self._data) + 1}",
            "host": "", "port": 587, "tls_mode": "STARTTLS",
            "username": "", "password": "",
            "sender_email": "", "sender_name": "",
            "enabled": True,
        }
        self._data.append(d)
        self._provider_list.addItem(self._list_label(d))
        self._provider_list.setCurrentRow(len(self._data) - 1)

    def _on_preset_selected(self, index: int):
        if index == 0:
            return  # "Quick Add..." placeholder
        name = self._preset_combo.currentText()
        preset = self.PRESETS.get(name, {})
        d = {
            "display_name": name,
            "host":         preset.get("host", ""),
            "port":         preset.get("port", 587),
            "tls_mode":     preset.get("tls", "STARTTLS"),
            "username": "", "password": "",
            "sender_email": "", "sender_name": "",
            "enabled": True,
        }
        self._data.append(d)
        self._provider_list.addItem(self._list_label(d))
        self._provider_list.setCurrentRow(len(self._data) - 1)
        self._preset_combo.setCurrentIndex(0)  # reset to placeholder

    def _remove_selected(self):
        idx = self._provider_list.currentRow()
        if idx < 0 or idx >= len(self._data):
            return
        self._selected = -1
        self._data.pop(idx)
        self._provider_list.takeItem(idx)
        if self._data:
            new_idx = min(idx, len(self._data) - 1)
            self._provider_list.setCurrentRow(new_idx)
        else:
            self._detail.setEnabled(False)

    def _move_up(self):
        idx = self._provider_list.currentRow()
        if idx <= 0:
            return
        self._save_detail()
        self._data[idx], self._data[idx - 1] = self._data[idx - 1], self._data[idx]
        self._provider_list.item(idx).setText(self._list_label(self._data[idx]))
        self._provider_list.item(idx - 1).setText(self._list_label(self._data[idx - 1]))
        self._selected = -1  # suppress save during row change
        self._provider_list.setCurrentRow(idx - 1)

    def _move_down(self):
        idx = self._provider_list.currentRow()
        if idx < 0 or idx >= len(self._data) - 1:
            return
        self._save_detail()
        self._data[idx], self._data[idx + 1] = self._data[idx + 1], self._data[idx]
        self._provider_list.item(idx).setText(self._list_label(self._data[idx]))
        self._provider_list.item(idx + 1).setText(self._list_label(self._data[idx + 1]))
        self._selected = -1
        self._provider_list.setCurrentRow(idx + 1)

    # ── Read back for settings save ───────────────────────────────────

    def get_providers(self) -> list[dict]:
        """Extract provider configurations — same return format as before."""
        self._save_detail()
        providers = []
        for i, d in enumerate(self._data):
            host = d.get("host", "").strip()
            if not host:
                continue  # skip empty providers
            providers.append({
                "provider_id":      f"smtp_provider_{i}",
                "display_name":     d.get("display_name", "").strip() or f"Provider {i + 1}",
                "host":             host,
                "port":             d.get("port", 587),
                "tls_mode":         d.get("tls_mode", "STARTTLS"),
                "username":         d.get("username", "").strip(),
                "password":         d.get("password", ""),
                "sender_email":     d.get("sender_email", "").strip(),
                "sender_name":      d.get("sender_name", "").strip(),
                "priority":         i,
                "enabled":          d.get("enabled", True),
                "failure_threshold": 3,
                "max_attempts":     5,
            })
        return providers


class InboundEmailTab:
    """Inbound email polling fallback — for when Winlink CMS is unavailable."""

    def __init__(self, config):
        self._config = config
        self.widgets = {}

    def build(self) -> QWidget:
        ie = self._config.outbound_delivery.inbound_email
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setSpacing(8)

        # ── How it works ─────────────────────────────────────────────
        notice = QLabel(
            "<b>How Inbound Email Works</b><br><br>"
            "The BBS polls a single email inbox and delivers messages to "
            "RF users based on the callsign in the recipient address. "
            "Internet users can send email to your BBS users without "
            "needing a Winlink account.<br><br>"
            "<b>Flow:</b> Internet sender emails "
            "<code>yourmail+BRECKEN@gmail.com</code> &rarr; arrives in "
            "your Gmail inbox &rarr; BBS polls it &rarr; extracts "
            "<code>BRECKEN</code> from the address &rarr; stores as a "
            "BBS message &rarr; BRECKEN reads it next time they connect "
            "over RF.<br><br>"
            "<b>Gmail Plus-Addressing (no custom domain needed):</b><br>"
            "Gmail delivers <code>you+anything@gmail.com</code> to "
            "<code>you@gmail.com</code> automatically. The BBS reads "
            "the <code>+callsign</code> part to route the message.<br>"
            "Set <b>BBS Domain</b> below to <code>gmail.com</code> and "
            "give each RF user an address like "
            "<code>youraccount+CALLSIGN@gmail.com</code>.<br><br>"
            "<i>One inbox serves all BBS users. No per-user setup needed.</i>"
        )
        notice.setStyleSheet("color:#b5bac1; font-size:9pt;")
        notice.setWordWrap(True)
        notice.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(notice)

        # ── Enable toggle ─────────────────────────────────────────────
        self.widgets["enabled"] = QCheckBox(
            "Enable Inbound Email Fallback"
        )
        self.widgets["enabled"].setChecked(ie.enabled)
        self.widgets["enabled"].setToolTip(
            "When enabled, the BBS polls a custom mailbox on a schedule\n"
            "as a fallback for receiving internet email when Winlink\n"
            "CMS is unavailable. Requires your own email domain."
        )
        layout.addWidget(self.widgets["enabled"])

        # ── Connection card ───────────────────────────────────────────
        conn_group = QGroupBox("Connection")
        cf = QFormLayout(conn_group)
        cf.setFieldGrowthPolicy(
            QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow
        )
        cf.setSpacing(6)

        # Protocol + SSL on one row
        proto_row = QWidget()
        pr = QHBoxLayout(proto_row)
        pr.setContentsMargins(0, 0, 0, 0)
        pr.setSpacing(12)
        self.widgets["protocol"] = QComboBox()
        self.widgets["protocol"].addItems(["IMAP", "POP3"])
        self.widgets["protocol"].setCurrentText(ie.protocol)
        self.widgets["protocol"].currentTextChanged.connect(
            self._on_protocol_changed
        )
        pr.addWidget(self.widgets["protocol"])
        self.widgets["use_ssl"] = QCheckBox("SSL/TLS")
        self.widgets["use_ssl"].setChecked(ie.use_ssl)
        self.widgets["use_ssl"].toggled.connect(self._on_ssl_toggled)
        pr.addWidget(self.widgets["use_ssl"])
        pr.addStretch()
        cf.addRow("Protocol:", proto_row)

        # Host + Port on one row
        host_row = QWidget()
        hr = QHBoxLayout(host_row)
        hr.setContentsMargins(0, 0, 0, 0)
        hr.setSpacing(8)
        self.widgets["host"] = QLineEdit(ie.host)
        self.widgets["host"].setPlaceholderText("imap.yourdomain.com")
        hr.addWidget(self.widgets["host"], stretch=1)
        hr.addWidget(QLabel(":"))
        self.widgets["port"] = QSpinBox()
        self.widgets["port"].setRange(1, 65535)
        self.widgets["port"].setValue(ie.port)
        self.widgets["port"].setFixedWidth(80)
        hr.addWidget(self.widgets["port"])
        cf.addRow("Server:", host_row)

        self.widgets["username"] = QLineEdit(ie.username)
        self.widgets["username"].setPlaceholderText("bbs@yourdomain.com")
        cf.addRow("Username:", self.widgets["username"])

        self.widgets["password"] = QLineEdit(ie.password)
        self.widgets["password"].setEchoMode(QLineEdit.EchoMode.Password)
        cf.addRow("Password:", self.widgets["password"])

        self.widgets["folder"] = QLineEdit(ie.folder)
        self.widgets["folder"].setPlaceholderText("INBOX")
        self.widgets["folder"].setToolTip("IMAP folder to poll (ignored for POP3)")
        self.widgets["folder"].setEnabled(ie.protocol == "IMAP")
        cf.addRow("Folder:", self.widgets["folder"])

        # Test buttons inline at bottom of the card
        test_row = QWidget()
        tr = QHBoxLayout(test_row)
        tr.setContentsMargins(0, 4, 0, 0)
        tr.setSpacing(8)
        self.widgets["test_btn"] = QPushButton("Test Connection")
        self.widgets["test_btn"].setFixedHeight(26)
        tr.addWidget(self.widgets["test_btn"])
        self.widgets["poll_now_btn"] = QPushButton("Poll Now")
        self.widgets["poll_now_btn"].setFixedHeight(26)
        tr.addWidget(self.widgets["poll_now_btn"])
        self.widgets["test_status"] = QLabel("")
        self.widgets["test_status"].setStyleSheet(
            "color:#80848e; font-size:9pt;"
        )
        tr.addWidget(self.widgets["test_status"], stretch=1)
        cf.addRow("", test_row)

        layout.addWidget(conn_group)

        # ── Delivery Rules card ───────────────────────────────────────
        rules_group = QGroupBox("Delivery Rules")
        rf = QFormLayout(rules_group)
        rf.setFieldGrowthPolicy(
            QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow
        )
        rf.setSpacing(6)

        self.widgets["bbs_domain"] = QLineEdit(ie.bbs_domain)
        self.widgets["bbs_domain"].setPlaceholderText("aetherbbs.example.com")
        self.widgets["bbs_domain"].setToolTip(
            "The domain part of email addresses that map to BBS callsigns.\n\n"
            "Emails to callsign@this-domain are delivered to the matching\n"
            "RF user. Other domains are ignored.\n\n"
            "Example:  kk4oda@aetherbbs.example.com → KK4ODA"
        )
        rf.addRow("BBS Domain:", self.widgets["bbs_domain"])

        # Poll + Size on one row
        poll_row = QWidget()
        plr = QHBoxLayout(poll_row)
        plr.setContentsMargins(0, 0, 0, 0)
        plr.setSpacing(12)
        self.widgets["poll_interval"] = QSpinBox()
        self.widgets["poll_interval"].setRange(1, 1440)
        self.widgets["poll_interval"].setValue(ie.poll_interval)
        self.widgets["poll_interval"].setSuffix(" min")
        plr.addWidget(self.widgets["poll_interval"])
        plr.addWidget(QLabel("Max size:"))
        self.widgets["max_message_size"] = QSpinBox()
        self.widgets["max_message_size"].setRange(1, 500)
        self.widgets["max_message_size"].setValue(ie.max_message_size // 1000)
        self.widgets["max_message_size"].setSuffix(" KB")
        self.widgets["max_message_size"].setToolTip(
            "Maximum email body size. Larger emails are truncated\n"
            "for RF delivery. Keep reasonable — RF bandwidth is limited."
        )
        plr.addWidget(self.widgets["max_message_size"])
        plr.addStretch()
        rf.addRow("Poll every:", poll_row)

        self.widgets["delete_after_fetch"] = QCheckBox(
            "Delete emails from server after processing"
        )
        self.widgets["delete_after_fetch"].setChecked(ie.delete_after_fetch)
        self.widgets["delete_after_fetch"].setToolTip(
            "When checked, emails are deleted after storage in BBS.\n"
            "When unchecked, emails are marked as read (IMAP) or\n"
            "left on server (POP3)."
        )
        rf.addRow("", self.widgets["delete_after_fetch"])

        self.widgets["allowed_senders"] = QTextEdit()
        self.widgets["allowed_senders"].setMaximumHeight(60)
        self.widgets["allowed_senders"].setPlaceholderText(
            "Leave empty = accept all.  One domain/address per line."
        )
        self.widgets["allowed_senders"].setToolTip(
            "Restrict which senders can deliver email to the BBS.\n"
            "Leave empty to accept all senders.\n"
            "Examples:  @yourdomain.com   trusted@gmail.com"
        )
        if ie.allowed_senders:
            self.widgets["allowed_senders"].setPlainText(
                "\n".join(ie.allowed_senders)
            )
        rf.addRow("Allowed senders:", self.widgets["allowed_senders"])

        layout.addWidget(rules_group)

        # Status line
        self.widgets["last_poll_status"] = QLabel("Last poll: —")
        self.widgets["last_poll_status"].setStyleSheet(
            "color:#80848e; font-size:9pt; padding:2px 0;"
        )
        layout.addWidget(self.widgets["last_poll_status"])

        layout.addStretch()
        return w

    def _on_protocol_changed(self, protocol: str):
        current_port = self.widgets["port"].value()
        ssl = self.widgets["use_ssl"].isChecked()
        if protocol == "IMAP":
            if current_port in (110, 995):
                self.widgets["port"].setValue(993 if ssl else 143)
            self.widgets["folder"].setEnabled(True)
        else:
            if current_port in (143, 993):
                self.widgets["port"].setValue(995 if ssl else 110)
            self.widgets["folder"].setEnabled(False)

    def _on_ssl_toggled(self, checked: bool):
        protocol = self.widgets["protocol"].currentText()
        current_port = self.widgets["port"].value()
        if protocol == "IMAP":
            if checked and current_port == 143:
                self.widgets["port"].setValue(993)
            elif not checked and current_port == 993:
                self.widgets["port"].setValue(143)
        else:
            if checked and current_port == 110:
                self.widgets["port"].setValue(995)
            elif not checked and current_port == 995:
                self.widgets["port"].setValue(110)

    def get_config_values(self) -> dict:
        senders_text = self.widgets["allowed_senders"].toPlainText()
        allowed = [s.strip() for s in senders_text.splitlines() if s.strip()]

        return {
            "enabled":            self.widgets["enabled"].isChecked(),
            "protocol":           self.widgets["protocol"].currentText(),
            "host":               self.widgets["host"].text().strip(),
            "port":               self.widgets["port"].value(),
            "use_ssl":            self.widgets["use_ssl"].isChecked(),
            "username":           self.widgets["username"].text().strip(),
            "password":           self.widgets["password"].text(),
            "folder":             self.widgets["folder"].text().strip() or "INBOX",
            "bbs_domain":         self.widgets["bbs_domain"].text().strip(),
            "poll_interval":      self.widgets["poll_interval"].value(),
            "delete_after_fetch": self.widgets["delete_after_fetch"].isChecked(),
            "max_message_size":   self.widgets["max_message_size"].value() * 1000,
            "allowed_senders":    allowed,
        }


# ═══════════════════════════════════════════════════════════════════════════════
# Tab 6: Winlink Forward Manager — compact layout
# ═══════════════════════════════════════════════════════════════════════════════

class WinlinkForwardTab:
    """Winlink ;FW list management — streamlined layout."""

    def __init__(self, config):
        self._config = config
        self.widgets = {}

    def build(self) -> QWidget:
        wf = self._config.outbound_delivery.winlink_forward
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setSpacing(8)

        # ── How it works ─────────────────────────────────────────────
        notice = QLabel(
            "<b>What the ;FW List Does</b><br><br>"
            "When your BBS connects to Winlink CMS, it sends a <code>;FW</code> "
            "line listing the callsigns it accepts mail for. The CMS then "
            "routes any Winlink messages for those callsigns to your BBS.<br><br>"
            "<b>Example:</b> If BRECKEN is on the list, Winlink mail addressed "
            "to BRECKEN is delivered to your BBS. BRECKEN reads it next time "
            "they connect over RF.<br><br>"
            "<span style='color:#ed4245;'><b>Warning — Conflicts with direct "
            "Winlink access:</b></span><br>"
            "If a user has their own Winlink account and checks it "
            "independently (e.g. via VARA TERM), <b>do not add their callsign "
            "to the forward list</b>. Your BBS would claim their mail before "
            "they can pull it themselves.<br><br>"
            "<b>Recommended:</b> Use <b>Manual</b> mode and only forward for "
            "<b>tactical addresses</b> (BRECKEN, SHELTER, etc.) that exist "
            "only on your BBS and don't have independent Winlink accounts."
        )
        notice.setStyleSheet("color:#b5bac1; font-size:9pt;")
        notice.setWordWrap(True)
        notice.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(notice)

        # ── Configuration card ────────────────────────────────────────
        config_group = QGroupBox("Forward List Configuration")
        cf = QFormLayout(config_group)
        cf.setFieldGrowthPolicy(
            QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow
        )
        cf.setSpacing(6)

        self.widgets["mode"] = QComboBox()
        self.widgets["mode"].addItems([
            "Manual — only explicit callsigns",
            "Auto (Approved+) — approved / trusted / sysop",
            "Auto (All) — everyone except blocked",
        ])
        self.widgets["mode"].setToolTip(
            "MANUAL: only the gateway callsign and explicit Forward For list.\n\n"
            "AUTO (Approved+): automatically includes all APPROVED, TRUSTED,\n"
            "and SYSOP users. New users join the list when approved.\n\n"
            "AUTO (All): includes everyone except BLOCKED users.\n"
            "Use with caution — unvetted users receive Winlink mail."
        )
        mode_map = {"MANUAL": 0, "AUTO_APPROVED": 1, "AUTO_ALL": 2}
        self.widgets["mode"].setCurrentIndex(mode_map.get(wf.mode, 0))
        cf.addRow("Mode:", self.widgets["mode"])

        self.widgets["max_calls"] = QSpinBox()
        self.widgets["max_calls"].setRange(1, 50)
        self.widgets["max_calls"].setValue(wf.max_calls)
        self.widgets["max_calls"].setSuffix("  callsigns")
        self.widgets["max_calls"].setToolTip(
            "Maximum callsigns in the ;FW line.\n"
            "CMS practical limit is ~50. List is truncated beyond this."
        )
        cf.addRow("Max forward:", self.widgets["max_calls"])

        layout.addWidget(config_group)

        # ── Live preview card ─────────────────────────────────────────
        preview_group = QGroupBox("Current ;FW List")
        pg = QVBoxLayout(preview_group)
        pg.setSpacing(4)

        self.widgets["fw_preview"] = QTextEdit()
        self.widgets["fw_preview"].setReadOnly(True)
        self.widgets["fw_preview"].setMinimumHeight(80)
        self.widgets["fw_preview"].setMaximumHeight(200)
        self.widgets["fw_preview"].setStyleSheet(
            "QTextEdit { font-family:Consolas; font-size:9pt;"
            "background:#1e1f22; border:1px solid #3b3d44;"
            "border-radius:4px; padding:6px; color:#dbdee1; }"
        )
        self.widgets["fw_preview"].setPlaceholderText(
            "Click Refresh to see the current forward list."
        )
        pg.addWidget(self.widgets["fw_preview"])

        preview_row = QHBoxLayout()
        preview_row.setSpacing(8)
        self.widgets["refresh_btn"] = QPushButton("Refresh Preview")
        self.widgets["refresh_btn"].setFixedHeight(26)
        preview_row.addWidget(self.widgets["refresh_btn"])
        self.widgets["fw_count"] = QLabel("Callsigns: —")
        self.widgets["fw_count"].setStyleSheet(
            "color:#80848e; font-size:9pt;"
        )
        preview_row.addWidget(self.widgets["fw_count"])
        preview_row.addStretch()
        pg.addLayout(preview_row)

        layout.addWidget(preview_group)

        # ── Key info note (replaces the 26-line help block) ───────────
        note = QLabel(
            "The BBS sends a ;FW line to the CMS listing callsigns to\n"
            "receive mail for. Only callsigns in this list get Winlink\n"
            "messages through this gateway."
        )
        note.setWordWrap(True)
        note.setStyleSheet("color:#80848e; font-size:9pt; padding:2px 0;")
        layout.addWidget(note)

        layout.addStretch()
        return w

    def get_config_values(self) -> dict:
        mode_map = {0: "MANUAL", 1: "AUTO_APPROVED", 2: "AUTO_ALL"}
        return {
            "mode":      mode_map.get(self.widgets["mode"].currentIndex(), "MANUAL"),
            "max_calls": self.widgets["max_calls"].value(),
        }
