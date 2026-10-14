"""
gui/chat_window.py

Sysop-side chat window.  Pops up when a remote operator types CH at the
BBS prompt.  Stays on top, flashes on arrival, and shows the conversation
in a colour-coded log.  The sysop types replies in the input bar and
presses Send (or Enter).

Lifecycle
---------
  1. MainWindow creates a ChatWindow and calls show() when CHAT_REQUEST fires.
  2. MainWindow calls append_message() for every CHAT_MESSAGE event.
  3. MainWindow calls set_ended(reason) when CHAT_ENDED fires.
     The window stays open (read-only) so the sysop can review the transcript.
  4. The sysop closes the window manually.

Signals
-------
  message_sent(str)  — sysop typed and sent a line
  ended()            — sysop clicked End Chat or typed /EXIT
"""

import queue
from datetime import datetime

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QTextEdit, QLineEdit, QPushButton, QFrame,
)
from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QTextCursor, QFont, QKeyEvent

from gui.theme import (
    BG_DEEPEST, BG_ELEVATED,
    ACCENT, GREEN, RED,
    TEXT_PRIMARY, TEXT_MUTED,
    BORDER_NORMAL,
)

# Conversation colours
_COL_REMOTE = "#00a8fc"   # blue — remote station
_COL_SYSOP  = "#23a55a"   # green — sysop
_COL_SYSTEM = "#80848e"   # muted — system messages
_COL_WARN   = "#f0b232"   # yellow — timeout warning

_SYSOP_TIMEOUT = 60       # seconds (must match interactive.py)


class ChatWindow(QWidget):
    """
    Always-on-top chat window between sysop and a remote RF station.
    """

    message_sent = pyqtSignal(str)   # sysop sent a line
    ended        = pyqtSignal()      # sysop ended the session

    # Border colours for the attention flash on arrival
    _FLASH_A = f"border: 3px solid {ACCENT}; border-radius: 10px; background: {BG_DEEPEST};"
    _FLASH_B = f"border: 3px solid {GREEN};  border-radius: 10px; background: {BG_DEEPEST};"

    def __init__(
        self,
        instance_name: str,
        remote_call:   str,
        sysop_call:    str,
        sysop_queue:   queue.Queue,
        parent=None,
    ):
        super().__init__(parent)
        self._instance    = instance_name
        self._remote_call = remote_call
        self._sysop_call  = sysop_call
        self._sysop_queue = sysop_queue
        self._started_at  = datetime.now()
        self._ended       = False
        self._flash_state = False

        # Timeout tracking
        self._last_remote_ts = None   # datetime of last remote line
        self._timeout_secs   = None   # seconds remaining, None = not running

        self.setWindowFlags(
            Qt.WindowType.Window |
            Qt.WindowType.WindowStaysOnTopHint |
            Qt.WindowType.WindowCloseButtonHint,
        )
        self.setWindowTitle(
            f"💬 Chat — {remote_call} via {instance_name}"
        )
        self.setMinimumSize(480, 420)
        self.resize(560, 500)

        self._build_ui()

        # Attention flash for the first 5 seconds
        self._flash_count = 0
        self._flash_timer = QTimer(self)
        self._flash_timer.timeout.connect(self._do_flash)
        self._flash_timer.start(500)

        # 1 Hz timer: updates timeout countdown
        self._tick_timer = QTimer(self)
        self._tick_timer.timeout.connect(self._tick)
        self._tick_timer.start(1000)

        self._append_system(
            f"Chat session started with {remote_call} "
            f"at {self._started_at.strftime('%H:%M:%S')}"
        )

    # ── UI construction ────────────────────────────────────────────────────

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.setSpacing(6)

        # Outer frame (border changes during flash)
        self._frame = QFrame()
        self._frame.setObjectName("chatFrame")
        self._frame.setStyleSheet(self._FLASH_A)
        frame_layout = QVBoxLayout(self._frame)
        frame_layout.setContentsMargins(8, 8, 8, 8)
        frame_layout.setSpacing(6)
        outer.addWidget(self._frame)

        # ── Header ────────────────────────────────────────────────────────
        hdr = QHBoxLayout()
        title = QLabel(f"💬  {self._remote_call}  ↔  {self._sysop_call}")
        title.setStyleSheet(f"font-size:12pt; font-weight:bold; color:{TEXT_PRIMARY};")
        hdr.addWidget(title)
        hdr.addStretch()
        self._status_lbl = QLabel("● Active")
        self._status_lbl.setStyleSheet(f"color:{GREEN}; font-weight:600;")
        hdr.addWidget(self._status_lbl)
        frame_layout.addLayout(hdr)

        # ── Timeout countdown bar ──────────────────────────────────────────
        self._timeout_lbl = QLabel("")
        self._timeout_lbl.setStyleSheet(
            f"color:{_COL_WARN}; font-size:9pt; font-style:italic;"
        )
        self._timeout_lbl.setVisible(False)
        frame_layout.addWidget(self._timeout_lbl)

        # ── Conversation display ───────────────────────────────────────────
        self._log = QTextEdit()
        self._log.setReadOnly(True)
        self._log.setFont(QFont("Courier New", 9))
        self._log.setStyleSheet(
            f"background:{BG_DEEPEST}; border:1px solid {BORDER_NORMAL};"
            f"border-radius:4px; padding:4px;"
        )
        frame_layout.addWidget(self._log, stretch=1)

        # ── Input row ─────────────────────────────────────────────────────
        input_row = QHBoxLayout()
        self._input = _ChatInput()
        self._input.setPlaceholderText(
            "Type a message and press Enter or Send…  (/EXIT to end chat)"
        )
        self._input.setStyleSheet(
            f"background:{BG_ELEVATED}; border:1px solid {BORDER_NORMAL};"
            f"border-radius:4px; padding:4px 8px;"
        )
        self._input.returnPressed_signal.connect(self._on_send)

        self._send_btn = QPushButton("Send")
        self._send_btn.setFixedWidth(70)
        self._send_btn.setStyleSheet(
            f"background:{ACCENT}; color:#fff; border:none; border-radius:4px;"
            f"padding:5px 10px; font-weight:600;"
        )
        self._send_btn.clicked.connect(self._on_send)

        self._end_btn = QPushButton("End Chat")
        self._end_btn.setFixedWidth(90)
        self._end_btn.setStyleSheet(
            f"background:{RED}; color:#fff; border:none; border-radius:4px;"
            f"padding:5px 10px; font-weight:600;"
        )
        self._end_btn.clicked.connect(self._on_end)

        input_row.addWidget(self._input, stretch=1)
        input_row.addWidget(self._send_btn)
        input_row.addWidget(self._end_btn)
        frame_layout.addLayout(input_row)

        self._input.setFocus()

    # ── Public API ─────────────────────────────────────────────────────────

    def append_message(self, direction: str, text: str):
        """Add a line to the conversation log.

        direction: "RX" (remote → sysop), "TX" (sysop → remote),
                   or "SYSTEM".
        """
        ts = datetime.now().strftime("%H:%M:%S")
        if direction == "RX":
            label = self._remote_call
            colour = _COL_REMOTE
        elif direction == "TX":
            label = self._sysop_call
            colour = _COL_SYSOP
        else:
            self._append_system(text)
            return

        html = (
            f'<span style="color:{TEXT_MUTED};">[{ts}]</span> '
            f'<span style="color:{colour}; font-weight:600;">'
            f'{label}:</span> '
            f'<span style="color:{TEXT_PRIMARY};">{_esc(text)}</span>'
        )
        self._append_html(html)

        if direction == "RX":
            self._last_remote_ts = datetime.now()
            self._timeout_secs   = _SYSOP_TIMEOUT
            self._timeout_lbl.setVisible(True)
            self._update_timeout_label()

    def set_ended(self, reason: str = ""):
        """Mark the session as ended.  Disables input but keeps window open."""
        self._ended = True
        self._flash_timer.stop()
        self._timeout_timer_stop()

        self._input.setEnabled(False)
        self._send_btn.setEnabled(False)
        self._end_btn.setEnabled(False)

        ended_at = datetime.now().strftime("%H:%M:%S")
        reason_map = {
            "sysop_exit":   "Sysop ended the session",
            "remote_exit":  "Remote station ended the session",
            "timeout":      "Closed — sysop did not respond in time",
            "rf_disconnect":"RF link disconnected",
        }
        reason_str = reason_map.get(reason, "Session ended")
        self._status_lbl.setText(f"● Session Ended  {ended_at}")
        self._status_lbl.setStyleSheet(f"color:{TEXT_MUTED}; font-weight:600;")
        self._frame.setStyleSheet(
            f"border:2px solid {BORDER_NORMAL}; border-radius:10px;"
            f"background:{BG_DEEPEST};"
        )
        self._timeout_lbl.setVisible(False)
        self._append_system(f"{reason_str} at {ended_at}")

    # ── Slots ──────────────────────────────────────────────────────────────

    def _on_send(self):
        text = self._input.text().strip()
        if not text or self._ended:
            return
        self._input.clear()

        if text.upper() == "/EXIT":
            self._on_end()
            return

        # Send to remote via sysop_queue
        self._sysop_queue.put(text)
        self.message_sent.emit(text)

        # Stop timeout — sysop replied
        self._timeout_timer_stop()

    def _on_end(self):
        if self._ended:
            return
        self._sysop_queue.put(None)   # sentinel: sysop exit
        self.ended.emit()
        # set_ended() will be called by mainwindow when CHAT_ENDED fires

    # ── Timers ─────────────────────────────────────────────────────────────

    def _do_flash(self):
        """Alternate border colour to attract sysop attention on arrival."""
        self._flash_count += 1
        if self._flash_count >= 10:   # 5 seconds
            self._flash_timer.stop()
            self._frame.setStyleSheet(
                f"border:2px solid {ACCENT}; border-radius:10px;"
                f"background:{BG_DEEPEST};"
            )
            return
        self._flash_state = not self._flash_state
        self._frame.setStyleSheet(
            self._FLASH_B if self._flash_state else self._FLASH_A
        )

    def _tick(self):
        """Called every second to update the timeout countdown."""
        if self._timeout_secs is None or self._ended:
            return
        self._timeout_secs -= 1
        if self._timeout_secs <= 0:
            self._timeout_timer_stop()
            return
        self._update_timeout_label()

    def _timeout_timer_stop(self):
        self._timeout_secs = None
        self._timeout_lbl.setVisible(False)

    def _update_timeout_label(self):
        if self._timeout_secs is None:
            return
        colour = _COL_WARN if self._timeout_secs > 15 else RED
        self._timeout_lbl.setStyleSheet(
            f"color:{colour}; font-size:9pt; font-style:italic;"
        )
        self._timeout_lbl.setText(
            f"⏱  Auto-close in {self._timeout_secs}s "
            f"(reply to reset)"
        )

    # ── Internal helpers ───────────────────────────────────────────────────

    def _append_html(self, html: str):
        cursor = self._log.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        self._log.setTextCursor(cursor)
        self._log.insertHtml(html + "<br>")
        self._log.verticalScrollBar().setValue(
            self._log.verticalScrollBar().maximum()
        )

    def _append_system(self, text: str):
        html = (
            f'<span style="color:{_COL_SYSTEM}; font-style:italic;">'
            f'— {_esc(text)} —</span>'
        )
        self._append_html(html)


class _ChatInput(QLineEdit):
    """QLineEdit that emits returnPressed_signal (avoids conflict with Qt's own signal)."""
    returnPressed_signal = pyqtSignal()

    def keyPressEvent(self, event: QKeyEvent):
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.returnPressed_signal.emit()
        else:
            super().keyPressEvent(event)


def _esc(text: str) -> str:
    """Minimal HTML escaping for display in QTextEdit."""
    return (
        text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
    )
