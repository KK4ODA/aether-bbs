"""
gui/delivery_tabs.py

Settings page for inbound email: IMAP/POP3 polling config, address
mapping, sender filter.
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
            "<code>yourmail+KK4ODA@gmail.com</code> &rarr; arrives in "
            "your Gmail inbox &rarr; BBS polls it &rarr; extracts "
            "<code>KK4ODA</code> from the address &rarr; stores as a "
            "BBS message &rarr; KK4ODA reads it next time they connect "
            "over RF.<br><br>"
            "<b>Gmail Plus-Addressing (no custom domain needed):</b><br>"
            "Gmail delivers <code>you+anything@gmail.com</code> to "
            "<code>you@gmail.com</code> automatically. The BBS reads "
            "the <code>+callsign</code> part to route the message.<br>"
            "Set <b>BBS Domain</b> below to <code>gmail.com</code> and "
            "give each RF user an address like "
            "<code>youraccount+CALLSIGN@gmail.com</code>. A tactical "
            "address from Settings &rarr; Station works too "
            "(<code>youraccount+EOC@gmail.com</code>) and raises its "
            "priority alert.<br><br>"
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

        self.widgets["password"] = QLineEdit(
            ie.decrypt_password(self._config.security.machine_secret))
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
            "Example:  kk4oda@aetherbbs.example.com → KK4ODA\n"
            "With plus-addressing:  you+kk4oda@gmail.com → KK4ODA"
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
