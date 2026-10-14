"""
gui/delivery_tabs.py

Settings pages for internet email:
  Inbound Email      — IMAP/POP3 polling config, address mapping, sender filter
  Winlink Forwarding — ;FW list mode, auto-populate from registered users
"""

import logging

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QFormLayout,
    QGroupBox, QLabel, QLineEdit, QSpinBox, QCheckBox, QComboBox, QPushButton, QTextEdit,
)
from PyQt6.QtCore import Qt

log = logging.getLogger(__name__)


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
