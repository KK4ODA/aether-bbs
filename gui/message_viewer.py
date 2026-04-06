"""
gui/message_viewer.py

Three window classes + the AlertManager service:

  MessageViewerWindow   — non-blocking, resizable window for reading any message.
                          Opens via double-click in MessagesTab.
                          Marks message as Read on open.
                          Provides Reply, Mark-as-Unread, and Close actions.

  PriorityAlertWindow   — always-on-top, flashing modal alert window triggered
                          when a message addressed to the Home Tactical Callsign
                          is received.  Plays repeating TTS until acknowledged.

  ReplyDialog           — simple compose window pre-addressed to the original
                          sender.  Includes canned-response quick-insert buttons
                          for non-technical operators.

  AlertManager          — service that subscribes to MESSAGE_RECEIVED events,
                          checks recipient vs home_tactical_callsign, deduplicates
                          per-MID (so reconnects never re-trigger), and emits
                          PRIORITY_MESSAGE_RECEIVED on the event bus when a match
                          is found.  Also drives the TTS repeat loop.
"""

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Optional

from PyQt6.QtWidgets import (
    QDialog, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QTextEdit,
    QPushButton, QFrame, QDialogButtonBox, QSizePolicy, QSplitter,
    QGroupBox, QScrollArea,
)
from PyQt6.QtCore import Qt, QTimer, pyqtSignal, QObject, pyqtSlot
from PyQt6.QtGui import QFont, QColor

from bbs.store import MessageStore
from core.config import AppConfig, AlertConfig
from core.events import EventBus, EventType

log = logging.getLogger(__name__)

# ── Colour constants (match theme.py palette) ─────────────────────────────────
_BG_DARK     = "#2b2d31"
_BG_MEDIUM   = "#313338"
_BG_ELEVATED = "#383a40"
_BG_DEEPEST  = "#1e1f22"
_ACCENT      = "#5865f2"
_GREEN       = "#23a55a"
_RED         = "#f23f43"
_YELLOW      = "#f0b232"
_TEXT_PRI    = "#dbdee1"
_TEXT_MUT    = "#80848e"
_BORDER      = "#4e505a"

# Priority labels and colours
PRIORITY_LABELS = {0: "Normal", 1: "Priority", 2: "Emergency"}
PRIORITY_COLOURS = {0: _TEXT_MUT, 1: _YELLOW, 2: _RED}

# Canned response templates
CANNED_RESPONSES = [
    "Message received.",
    "Copy.",
    "Stand by.",
    "Unable to comply.",
    "Acknowledged. Over.",
]


# ──────────────────────────────────────────────────────────────────────────────
# ReplyDialog
# ──────────────────────────────────────────────────────────────────────────────

class ReplyDialog(QDialog):
    """
    Simple compose window for replying to a message.
    Designed for non-technical operators:
      - Pre-addressed and pre-subject-lined
      - Large obvious Send button
      - One-click canned response insertion
    """

    reply_sent = pyqtSignal(str)   # emits the new MID

    def __init__(
        self,
        store: MessageStore,
        original_mid: str,
        from_call: str,   # who we're replying TO (original sender)
        our_call: str,    # our BBS callsign
        subject: str,
        parent=None,
    ):
        super().__init__(parent)
        self._store       = store
        self._original_mid= original_mid
        self._from_call   = from_call
        self._our_call    = our_call
        self._subject     = subject

        self.setWindowTitle(f"Reply to {from_call}")
        self.setMinimumSize(520, 420)
        self.setWindowFlags(
            self.windowFlags() | Qt.WindowType.Window
        )
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        # Header info strip
        hdr = QFrame()
        hdr.setStyleSheet(
            f"background:{_BG_MEDIUM};border-radius:6px;padding:6px;"
        )
        hdr_layout = QVBoxLayout(hdr)
        hdr_layout.setSpacing(2)
        hdr_layout.addWidget(
            QLabel(f"<b>To:</b>  {self._from_call}",
                   styleSheet=f"color:{_TEXT_PRI};font-size:10pt;")
        )
        hdr_layout.addWidget(
            QLabel(f"<b>From:</b>  {self._our_call}",
                   styleSheet=f"color:{_TEXT_PRI};font-size:10pt;")
        )
        subj = f"RE: {self._subject}" if not self._subject.upper().startswith("RE:") else self._subject
        hdr_layout.addWidget(
            QLabel(f"<b>Subject:</b>  {subj}",
                   styleSheet=f"color:{_TEXT_PRI};font-size:10pt;")
        )
        layout.addWidget(hdr)

        # Canned responses
        canned_box = QGroupBox("Quick Responses")
        canned_layout = QHBoxLayout(canned_box)
        for text in CANNED_RESPONSES:
            btn = QPushButton(text)
            btn.setStyleSheet(
                f"background:{_BG_ELEVATED};color:{_TEXT_PRI};"
                f"border:1px solid {_BORDER};border-radius:4px;padding:4px 8px;"
            )
            btn.clicked.connect(lambda _, t=text: self._insert_canned(t))
            canned_layout.addWidget(btn)
        canned_layout.addStretch()
        layout.addWidget(canned_box)

        # Message body
        body_lbl = QLabel("Message:")
        body_lbl.setStyleSheet(f"color:{_TEXT_PRI};font-weight:bold;")
        layout.addWidget(body_lbl)

        self._body_edit = QTextEdit()
        self._body_edit.setMinimumHeight(160)
        self._body_edit.setFont(QFont("Courier New", 10))
        self._body_edit.setStyleSheet(
            f"background:{_BG_DEEPEST};color:{_TEXT_PRI};"
            f"border:1px solid {_BORDER};border-radius:4px;"
        )
        self._body_edit.setPlaceholderText("Type your reply here…")
        layout.addWidget(self._body_edit)

        # Send / Cancel
        btn_layout = QHBoxLayout()
        cancel_btn = QPushButton("✕  Cancel")
        cancel_btn.setStyleSheet(
            f"background:{_BG_ELEVATED};color:{_TEXT_PRI};"
            f"border:1px solid {_BORDER};border-radius:4px;padding:6px 18px;"
        )
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(cancel_btn)
        btn_layout.addStretch()

        send_btn = QPushButton("📤  SEND REPLY")
        send_btn.setStyleSheet(
            f"background:{_GREEN};color:white;font-size:13pt;font-weight:bold;"
            f"border:none;border-radius:6px;padding:10px 30px;"
        )
        send_btn.setMinimumHeight(48)
        send_btn.clicked.connect(self._send)
        btn_layout.addWidget(send_btn)
        layout.addLayout(btn_layout)

    def _insert_canned(self, text: str):
        self._body_edit.setPlainText(text)
        self._body_edit.setFocus()
        cursor = self._body_edit.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        self._body_edit.setTextCursor(cursor)

    def _send(self):
        body_text = self._body_edit.toPlainText().strip()
        if not body_text:
            return
        subj = f"RE: {self._subject}" if not self._subject.upper().startswith("RE:") else self._subject
        new_mid = self._store.save_message(
            from_call=self._our_call,
            to_call=self._from_call,
            subject=subj,
            body=body_text.encode("utf-8"),
        )
        self._store.mark_replied(self._original_mid, operator=self._our_call)
        log.info(f"Reply sent: MID={new_mid} to={self._from_call}")
        self.reply_sent.emit(new_mid)
        self.accept()


# ──────────────────────────────────────────────────────────────────────────────
# MessageViewerWindow
# ──────────────────────────────────────────────────────────────────────────────

class MessageViewerWindow(QDialog):
    """
    Non-blocking, resizable message reading window.

    Each window is keyed to a single MID.  Calling code should keep a
    registry (dict[mid, MessageViewerWindow]) so the same MID is never
    opened twice; bring the existing window to front instead.

    Signals:
      closed(mid)      — emitted when the window is closed
      reply_sent(mid)  — emitted when a reply has been sent
    """

    closed      = pyqtSignal(str)   # mid
    reply_sent  = pyqtSignal(str)   # mid

    def __init__(
        self,
        store: MessageStore,
        mid: str,
        our_call: str,
        config: AppConfig,
        parent=None,
    ):
        super().__init__(parent)
        self._store    = store
        self._mid      = mid
        self._our_call = our_call
        self._config   = config
        self._msg      = None

        self.setWindowFlags(
            Qt.WindowType.Window |
            Qt.WindowType.WindowMinimizeButtonHint |
            Qt.WindowType.WindowMaximizeButtonHint |
            Qt.WindowType.WindowCloseButtonHint
        )
        self.setMinimumSize(560, 420)
        self.resize(680, 520)

        self._load_message()
        self._build_ui()

        # Mark as read immediately on open
        if self._msg and not self._msg["is_read"]:
            self._store.mark_read(mid, operator=our_call)
        self._store.audit_log(mid, "OPENED", operator=our_call)

    def _load_message(self):
        self._msg = self._store.get_message(self._mid)
        if self._msg:
            self._msg = dict(self._msg)

    def _build_ui(self):
        if not self._msg:
            self.setWindowTitle("Message Not Found")
            layout = QVBoxLayout(self)
            layout.addWidget(QLabel("Message could not be loaded."))
            return

        m = self._msg
        priority = m.get("priority", 0)
        priority_label = PRIORITY_LABELS.get(priority, "Normal")
        priority_color = PRIORITY_COLOURS.get(priority, _TEXT_MUT)
        is_replied = bool(m.get("is_replied", 0))
        is_read    = bool(m.get("is_read", 0))
        is_urgent  = bool(m.get("is_urgent", 0))

        status_parts = []
        if is_urgent:
            status_parts.append("🚨 URGENT")
        if is_replied:
            status_parts.append("Replied")
        elif is_read:
            status_parts.append("Read")
        else:
            status_parts.append("Unread")

        subject_display = m.get("subject") or "(no subject)"
        title_prefix = "[URGENT] " if is_urgent else ""
        self.setWindowTitle(f"Message — {title_prefix}{subject_display}")

        layout = QVBoxLayout(self)
        layout.setSpacing(8)
        layout.setContentsMargins(12, 12, 12, 12)

        # ── Header strip ──────────────────────────────────────────────────
        hdr_frame = QFrame()
        hdr_frame.setStyleSheet(
            f"background:{_BG_MEDIUM};border-radius:8px;padding:10px;"
        )
        hdr_layout = QVBoxLayout(hdr_frame)
        hdr_layout.setSpacing(4)

        def _row(label: str, value: str, value_color: str = _TEXT_PRI):
            row = QHBoxLayout()
            lbl = QLabel(f"{label}:")
            lbl.setStyleSheet(
                f"color:{_TEXT_MUT};font-size:9pt;font-weight:600;"
                f"min-width:90px;max-width:90px;"
            )
            val = QLabel(value)
            val.setStyleSheet(
                f"color:{value_color};font-size:10pt;font-weight:600;"
            )
            val.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse
            )
            row.addWidget(lbl)
            row.addWidget(val, 1)
            return row

        hdr_layout.addLayout(_row("From",    m["from_call"]))
        hdr_layout.addLayout(_row("To",      m["to_call"]))

        # Timestamp — local + UTC
        date_str = str(m.get("date", ""))
        try:
            dt_utc = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
            dt_local = dt_utc.astimezone()
            ts_display = (
                f"{dt_local.strftime('%Y-%m-%d %H:%M:%S %Z')}  "
                f"({dt_utc.strftime('%H:%M UTC')})"
            )
        except (ValueError, TypeError):
            ts_display = date_str

        hdr_layout.addLayout(_row("Date",    ts_display))
        hdr_layout.addLayout(_row("Subject", m.get("subject") or "(no subject)"))
        hdr_layout.addLayout(_row("MID",     m["id"], _TEXT_MUT))

        # Status / priority row
        status_row = QHBoxLayout()
        status_lbl = QLabel("Status:")
        status_lbl.setStyleSheet(
            f"color:{_TEXT_MUT};font-size:9pt;font-weight:600;min-width:90px;max-width:90px;"
        )
        status_text = "  ".join(status_parts)
        status_val = QLabel(status_text)
        status_color = _RED if is_urgent else _GREEN
        status_val.setStyleSheet(f"color:{status_color};font-size:9pt;font-weight:bold;")
        status_row.addWidget(status_lbl)
        status_row.addWidget(status_val)
        status_row.addStretch()
        if is_urgent:
            urgent_badge = QLabel("  🚨 URGENT  ")
            urgent_badge.setStyleSheet(
                f"background:{_RED};color:white;font-size:9pt;font-weight:bold;"
                f"border-radius:4px;padding:2px 8px;"
            )
            status_row.addWidget(urgent_badge)
        pri_lbl = QLabel(f"⚡ {priority_label}" if priority > 0 else priority_label)
        pri_lbl.setStyleSheet(
            f"color:{priority_color};font-size:9pt;font-weight:bold;"
        )
        status_row.addWidget(pri_lbl)
        hdr_layout.addLayout(status_row)

        layout.addWidget(hdr_frame)

        # ── Message body ──────────────────────────────────────────────────
        body_bytes = m.get("body", b"")
        if isinstance(body_bytes, bytes):
            body_text = body_bytes.decode("utf-8", errors="replace")
        else:
            body_text = str(body_bytes)

        font_size = "12pt" if self._config.alert.large_font else "10pt"
        self._body_edit = QTextEdit()
        self._body_edit.setReadOnly(True)
        self._body_edit.setFont(QFont("Courier New", 12 if self._config.alert.large_font else 10))
        self._body_edit.setStyleSheet(
            f"background:{_BG_DEEPEST};color:{_TEXT_PRI};"
            f"border:1px solid {_BORDER};border-radius:6px;"
            f"font-size:{font_size};padding:8px;"
        )
        self._body_edit.setPlainText(body_text)
        layout.addWidget(self._body_edit, 1)

        # ── Delivery metadata (via_path, instance) ────────────────────────
        meta_parts = []
        if m.get("via_path"):
            meta_parts.append(f"Via: {m['via_path']}")
        if m.get("instance"):
            meta_parts.append(f"Instance: {m['instance']}")
        if m.get("area") and m["area"] not in ("GENERAL", ""):
            meta_parts.append(f"Area: {m['area']}")

        if meta_parts:
            meta_lbl = QLabel("  ·  ".join(meta_parts))
            meta_lbl.setStyleSheet(f"color:{_TEXT_MUT};font-size:8pt;")
            layout.addWidget(meta_lbl)

        # ── Action buttons ────────────────────────────────────────────────
        btn_layout = QHBoxLayout()

        self._unread_btn = QPushButton("📭  Mark as Unread")
        self._unread_btn.setStyleSheet(
            f"background:{_BG_ELEVATED};color:{_TEXT_PRI};"
            f"border:1px solid {_BORDER};border-radius:4px;padding:6px 14px;"
        )
        self._unread_btn.clicked.connect(self._mark_unread)
        btn_layout.addWidget(self._unread_btn)
        btn_layout.addStretch()

        close_btn = QPushButton("✕  Close")
        close_btn.setStyleSheet(
            f"background:{_BG_ELEVATED};color:{_TEXT_PRI};"
            f"border:1px solid {_BORDER};border-radius:4px;padding:6px 16px;"
        )
        close_btn.clicked.connect(self.close)
        btn_layout.addWidget(close_btn)

        reply_btn = QPushButton("↩  REPLY")
        reply_btn.setStyleSheet(
            f"background:{_ACCENT};color:white;font-weight:bold;"
            f"border:none;border-radius:4px;padding:6px 20px;"
        )
        reply_btn.clicked.connect(self._open_reply)
        btn_layout.addWidget(reply_btn)

        layout.addLayout(btn_layout)

    def _mark_unread(self):
        self._store.mark_unread(self._mid)
        self._unread_btn.setText("✓ Marked as Unread")
        self._unread_btn.setEnabled(False)

    def _open_reply(self):
        if not self._msg:
            return
        dlg = ReplyDialog(
            store=self._store,
            original_mid=self._mid,
            from_call=self._msg["from_call"],
            our_call=self._our_call,
            subject=self._msg.get("subject", ""),
            parent=self,
        )
        dlg.reply_sent.connect(self.reply_sent.emit)
        dlg.exec()

    def closeEvent(self, event):
        self.closed.emit(self._mid)
        super().closeEvent(event)


# ──────────────────────────────────────────────────────────────────────────────
# PriorityAlertWindow
# ──────────────────────────────────────────────────────────────────────────────

class PriorityAlertWindow(QDialog):
    """
    Always-on-top, flashing priority alert window.

    Appears automatically when a message addressed to the Home Tactical
    Callsign is received.  Plays TTS until the operator acknowledges.

    Signals:
      acknowledged(mid)  — emitted on Acknowledge or Reply
    """

    acknowledged = pyqtSignal(str)   # mid

    # Flash palette — alternates between these two border colours
    _FLASH_A = f"border:3px solid {_RED};border-radius:10px;background:{_BG_DEEPEST};"
    _FLASH_B = f"border:3px solid #ff9900;border-radius:10px;background:#2a1010;"

    def __init__(
        self,
        store: MessageStore,
        mid: str,
        our_call: str,
        config: AppConfig,
        audio_manager: "AudioAlertThread",
        parent=None,
    ):
        super().__init__(parent)
        self._store        = store
        self._mid          = mid
        self._our_call     = our_call
        self._config       = config
        self._audio        = audio_manager
        self._flash_state  = False
        self._msg          = dict(store.get_message(mid) or {})

        self.setWindowFlags(
            Qt.WindowType.Window |
            Qt.WindowType.WindowStaysOnTopHint |
            Qt.WindowType.WindowCloseButtonHint
        )
        self.setMinimumSize(520, 380)
        self.resize(600, 460)

        cfg_alert = config.alert
        title_font_size = "14pt" if cfg_alert.large_font else "11pt"
        body_font_size  = "13pt" if cfg_alert.large_font else "10pt"

        self._title_font_size = title_font_size
        self._body_font_size  = body_font_size

        self._build_ui()
        self._store.audit_log(mid, "ALERT_SHOWN")

        # Flash timer (toggles border colour every 600ms)
        self._flash_timer = QTimer(self)
        self._flash_timer.timeout.connect(self._do_flash)
        self._flash_timer.start(600)

    def _build_ui(self):
        m = self._msg
        cfg_alert = self._config.alert

        self.setWindowTitle("⚠  PRIORITY MESSAGE")
        self.setStyleSheet(self._FLASH_A)

        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        layout.setContentsMargins(14, 14, 14, 14)

        # ── Priority banner ───────────────────────────────────────────────
        banner = QLabel("⚠  PRIORITY MESSAGE  ⚠")
        banner_font_size = "20pt" if cfg_alert.large_font else "15pt"

        if cfg_alert.high_contrast:
            banner.setStyleSheet(
                f"background:red;color:white;font-size:{banner_font_size};"
                f"font-weight:900;padding:10px;border-radius:6px;"
                f"letter-spacing:2px;text-align:center;"
            )
        else:
            banner.setStyleSheet(
                f"background:{_RED};color:white;font-size:{banner_font_size};"
                f"font-weight:900;padding:10px;border-radius:6px;"
                f"letter-spacing:2px;text-align:center;"
            )
        banner.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(banner)

        # ── Message header ────────────────────────────────────────────────
        hdr_frame = QFrame()
        hdr_frame.setStyleSheet(
            f"background:{_BG_MEDIUM};border-radius:6px;padding:8px;border:none;"
        )
        hdr_v = QVBoxLayout(hdr_frame)
        hdr_v.setSpacing(4)

        lbl_style = (
            f"color:{_TEXT_PRI};font-size:{self._title_font_size};"
            f"font-weight:bold;"
        )
        hdr_v.addWidget(QLabel(f"From:    {m.get('from_call','?')}", styleSheet=lbl_style))
        hdr_v.addWidget(QLabel(f"To:      {m.get('to_call','?')}",   styleSheet=lbl_style))
        hdr_v.addWidget(QLabel(f"Subject: {m.get('subject','')}", styleSheet=lbl_style))

        date_str = str(m.get("date", ""))
        try:
            dt_utc   = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
            dt_local = dt_utc.astimezone()
            ts = f"{dt_local.strftime('%Y-%m-%d %H:%M:%S %Z')}  ({dt_utc.strftime('%H:%M UTC')})"
        except (ValueError, TypeError):
            ts = date_str
        hdr_v.addWidget(QLabel(f"Time:    {ts}", styleSheet=f"color:{_TEXT_MUT};font-size:9pt;"))
        layout.addWidget(hdr_frame)

        # ── Message body ──────────────────────────────────────────────────
        body_bytes = m.get("body", b"")
        if isinstance(body_bytes, bytes):
            body_text = body_bytes.decode("utf-8", errors="replace")
        else:
            body_text = str(body_bytes)

        self._body_edit = QTextEdit()
        self._body_edit.setReadOnly(True)
        self._body_edit.setFont(
            QFont("Courier New", 13 if cfg_alert.large_font else 10)
        )
        self._body_edit.setStyleSheet(
            f"background:{_BG_DEEPEST};color:{_TEXT_PRI};"
            f"border:1px solid {_BORDER};border-radius:6px;padding:6px;"
            f"font-size:{self._body_font_size};"
        )
        self._body_edit.setPlainText(body_text)
        layout.addWidget(self._body_edit, 1)

        # ── Audio status strip ────────────────────────────────────────────
        if not cfg_alert.visual_only and cfg_alert.audio_enabled:
            audio_lbl = QLabel("🔊 Audio alert active — will repeat until acknowledged")
            audio_lbl.setStyleSheet(f"color:{_YELLOW};font-size:9pt;")
            audio_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            layout.addWidget(audio_lbl)

        # ── Action buttons ────────────────────────────────────────────────
        btn_layout = QHBoxLayout()

        ack_btn = QPushButton("✅  ACKNOWLEDGE")
        ack_style = (
            "background:#e74c3c;color:white;font-size:14pt;font-weight:900;"
            "border:none;border-radius:6px;padding:10px 24px;"
            if cfg_alert.high_contrast
            else
            f"background:{_RED};color:white;font-size:13pt;font-weight:bold;"
            f"border:none;border-radius:6px;padding:10px 24px;"
        )
        ack_btn.setStyleSheet(ack_style)
        ack_btn.setMinimumHeight(52)
        ack_btn.clicked.connect(self._acknowledge)
        btn_layout.addWidget(ack_btn)

        reply_btn = QPushButton("↩  REPLY")
        reply_btn.setStyleSheet(
            f"background:{_ACCENT};color:white;font-size:13pt;font-weight:bold;"
            f"border:none;border-radius:6px;padding:10px 24px;"
        )
        reply_btn.setMinimumHeight(52)
        reply_btn.clicked.connect(self._reply_and_ack)
        btn_layout.addWidget(reply_btn)

        layout.addLayout(btn_layout)

    def _do_flash(self):
        self._flash_state = not self._flash_state
        self.setStyleSheet(self._FLASH_B if self._flash_state else self._FLASH_A)

    def _acknowledge(self):
        self._flash_timer.stop()
        self._audio.stop()
        self._store.mark_read(self._mid, operator=self._our_call)
        self._store.audit_log(self._mid, "ACKNOWLEDGED", operator=self._our_call)
        self.acknowledged.emit(self._mid)
        self.accept()

    def _reply_and_ack(self):
        self._flash_timer.stop()
        self._audio.stop()
        self._store.audit_log(self._mid, "ACKNOWLEDGED", operator=self._our_call)
        dlg = ReplyDialog(
            store=self._store,
            original_mid=self._mid,
            from_call=self._msg.get("from_call", ""),
            our_call=self._our_call,
            subject=self._msg.get("subject", ""),
            parent=self,
        )
        dlg.exec()
        self.acknowledged.emit(self._mid)
        self.accept()

    def closeEvent(self, event):
        self._flash_timer.stop()
        self._audio.stop()
        self._store.audit_log(self._mid, "ALERT_DISMISSED")
        super().closeEvent(event)


# ──────────────────────────────────────────────────────────────────────────────
# AudioAlertThread
# ──────────────────────────────────────────────────────────────────────────────

class AudioAlertThread(threading.Thread):
    """
    Background thread that repeats a TTS spoken alert at a configurable interval.

    Uses pyttsx3 if available; falls back to platform-native TTS via subprocess
    (PowerShell on Windows, 'say' on macOS, espeak on Linux); falls back silently
    to no audio if none of the above are available.

    Thread is stopped by calling .stop().  It is daemon so it dies with the process.
    """

    def __init__(self, config: AlertConfig, phrase: str = "Alert message received"):
        super().__init__(daemon=True, name="AudioAlert")
        self._config   = config
        self._phrase   = phrase
        self._stop_evt = threading.Event()

    def run(self):
        if not self._config.audio_enabled or self._config.visual_only:
            return

        interval = self._config.alert_interval
        while not self._stop_evt.is_set():
            self._speak()
            # Wait for interval or until stopped
            self._stop_evt.wait(interval)

    def stop(self):
        self._stop_evt.set()

    def _speak(self):
        """
        Speak the alert phrase using the best available TTS method.

        Windows — always uses a detached PowerShell subprocess.
        Using pyttsx3 / SAPI5 directly from a background thread causes a
        hard C-level crash because SAPI5's runAndWait() pumps the Windows
        message queue in a way that is incompatible with Qt's event loop
        when called from a non-COM-initialised thread.  A subprocess is
        fully isolated from the Python process and avoids this entirely.

        macOS  — 'say' command via subprocess.
        Linux  — 'espeak' via subprocess.
        All    — pyttsx3 attempted only on non-Windows as a convenience
                 fallback if the system TTS binary is absent.
        """
        if self._stop_evt.is_set():
            return

        import subprocess, sys

        phrase = self._phrase.replace("'", "").replace('"', "")
        volume = int(self._config.volume * 100)

        try:
            if sys.platform == "win32":
                script = (
                    f"Add-Type -AssemblyName System.Speech; "
                    f"$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
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
                return
            elif sys.platform == "darwin":
                subprocess.run(["say", phrase], timeout=15,
                               capture_output=True)
                return
            else:
                subprocess.run(["espeak", "-s", "140", phrase],
                               timeout=15, capture_output=True)
                return
        except FileNotFoundError:
            pass   # fall through to pyttsx3
        except Exception as e:
            log.debug(f"Subprocess TTS error: {e}")
            return

        # Last-resort: pyttsx3 (non-Windows only — safe on Linux/macOS threads)
        try:
            import pyttsx3
            engine = pyttsx3.init()
            engine.setProperty("rate", 150)
            engine.setProperty("volume", float(self._config.volume))
            engine.say(self._phrase)
            engine.runAndWait()
            engine.stop()
        except ImportError:
            log.debug("pyttsx3 not installed and no system TTS binary found")
        except Exception as e:
            log.debug(f"pyttsx3 TTS error: {e}")


# ──────────────────────────────────────────────────────────────────────────────
# AlertManager
# ──────────────────────────────────────────────────────────────────────────────

class _AlertBridge(QObject):
    """Thread-safe bridge: push priority alert notification to Qt main thread."""
    trigger = pyqtSignal(str)   # mid


class AlertManager:
    """
    Subscribes to MESSAGE_RECEIVED events on the EventBus.
    When the recipient matches the Home Tactical Callsign, emits
    PRIORITY_MESSAGE_RECEIVED and fires a Qt signal so the main window
    can open a PriorityAlertWindow safely on the GUI thread.

    Deduplication: each MID is alerted at most once per application session.
    Restarts will not re-alert for messages that are already marked as Read.
    """

    def __init__(
        self,
        store: MessageStore,
        config,
        bus: EventBus,
    ):
        self._store       = store
        # Accept either a ConfigManager (preferred) or a plain AppConfig
        # so existing callers without a ConfigManager still work.
        self._cm          = config if hasattr(config, "current") else None
        self._config      = config if self._cm is None else None
        self._bus         = bus
        self._alerted_mids: set[str] = set()
        self._lock        = threading.Lock()

        self._bridge = _AlertBridge()
        bus.subscribe(EventType.MESSAGE_RECEIVED, self._on_message_received)

    @property
    def trigger_signal(self):
        """Expose the Qt signal so MainWindow can connect a slot to it."""
        return self._bridge.trigger

    def _on_message_received(self, event):
        """Called on the EventBus thread (not the Qt main thread)."""
        data = event.data or {}
        mid     = data.get("mid", "")
        to_call = (data.get("to", "") or "").upper()

        log.info(f"AlertManager: MESSAGE_RECEIVED mid={mid!r} to={to_call!r}")

        if not mid or not to_call:
            log.info("AlertManager: skipping — blank mid or to_call")
            return

        cfg = (self._cm.current() if self._cm else self._config) or self._config
        if cfg is None:
            log.warning("AlertManager: config is None — cannot check tactical callsigns")
            return

        # Find first matching tactical address
        matched_phrase = None
        matched_call   = None
        for ta in cfg.station.tactical_addresses:
            if ta.callsign.upper().strip() == to_call:
                matched_phrase = ta.alert_phrase.strip() or "Priority message received"
                matched_call   = ta.callsign.upper().strip()
                break

        if matched_phrase is None:
            log.info(f"AlertManager: skipping — {to_call!r} not in tactical addresses")
            return

        with self._lock:
            if mid in self._alerted_mids:
                log.info(f"AlertManager: skipping — MID {mid} already alerted this session")
                return

            msg = self._store.get_message(mid)
            if msg and msg["is_read"]:
                log.info(f"AlertManager: skipping — MID {mid} already marked read")
                return

            self._alerted_mids.add(mid)

        log.info(
            f"AlertManager: MATCH — MID={mid} to={to_call} phrase={matched_phrase!r} "
            f"— firing alert"
        )

        self._bus.emit(
            EventType.PRIORITY_MESSAGE_RECEIVED,
            source="AlertManager",
            data={**data, "tactical_callsign": matched_call, "alert_phrase": matched_phrase},
        )

        log.info(f"AlertManager: emitting trigger signal for MID={mid}")
        self._bridge.trigger.emit(mid)
