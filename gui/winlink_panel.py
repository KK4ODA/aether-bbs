"""
gui/winlink_panel.py

Winlink status panel — shows Pat connection status, last poll time,
queue depth, and provides a manual poll button.
"""

import logging
from datetime import datetime

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QGroupBox, QFrame,
)
from PyQt6.QtCore import Qt, pyqtSignal, pyqtSlot, QTimer

from core.events import EventBus, EventType, Event

log = logging.getLogger(__name__)


class WinlinkPanel(QWidget):
    """
    Compact Winlink status panel for embedding in the main window.
    Shows: enabled/disabled, Pat status, last poll, queue count, poll button.
    """

    # Qt signal for cross-thread GUI updates
    _update_signal = pyqtSignal(dict)

    def __init__(self, event_bus: EventBus, parent=None):
        super().__init__(parent)
        self._bus = event_bus
        self._poller = None   # set via set_poller()
        self._enabled = False

        self._update_signal.connect(self._apply_update)
        self._init_ui()
        self._subscribe_events()

    def set_poller(self, poller):
        """Connect to the PatClient instance."""
        self._poller = poller
        self._enabled = poller.is_enabled if poller else False
        self._refresh_status()

    def _init_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # Wrap everything in a group box for visual consistency with BBS panel
        group = QGroupBox("Winlink (Pat)")
        group.setStyleSheet(
            "QGroupBox { font-size: 10pt; font-weight: bold; "
            "border: 1px solid #444; border-radius: 4px; "
            "margin-top: 8px; padding-top: 14px; } "
            "QGroupBox::title { subcontrol-origin: margin; "
            "left: 10px; padding: 0 4px; }"
        )
        inner = QVBoxLayout(group)
        inner.setContentsMargins(12, 8, 12, 10)
        inner.setSpacing(6)

        # ── Status row ─────────────────────────────────────────────
        status_row = QHBoxLayout()
        status_lbl = QLabel("Status:")
        status_lbl.setStyleSheet("color: #aaa; font-size: 11px; font-weight: normal;")
        status_row.addWidget(status_lbl)

        self._status_label = QLabel("Disabled")
        self._status_label.setStyleSheet("color: #888; font-weight: bold;")
        status_row.addWidget(self._status_label)
        status_row.addStretch()
        inner.addLayout(status_row)

        # ── Separator ──────────────────────────────────────────────
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet("color: #333;")
        inner.addWidget(sep)

        # ── Info row ───────────────────────────────────────────────
        info = QHBoxLayout()

        self._last_poll_label = QLabel("Last poll: —")
        self._last_poll_label.setStyleSheet("color: #aaa; font-size: 11px; font-weight: normal;")
        info.addWidget(self._last_poll_label)

        info.addStretch()

        self._queue_label = QLabel("Queue: 0")
        self._queue_label.setStyleSheet("color: #aaa; font-size: 11px; font-weight: normal;")
        info.addWidget(self._queue_label)

        inner.addLayout(info)

        # ── Stats + Poll button row ────────────────────────────────
        stats = QHBoxLayout()

        self._sent_label = QLabel("↑ 0 sent")
        self._sent_label.setStyleSheet("color: #5dade2; font-size: 11px; font-weight: normal;")
        stats.addWidget(self._sent_label)

        self._recv_label = QLabel("↓ 0 received")
        self._recv_label.setStyleSheet("color: #2ecc71; font-size: 11px; font-weight: normal;")
        stats.addWidget(self._recv_label)

        stats.addStretch()

        self._poll_btn = QPushButton("Poll Now")
        self._poll_btn.setFixedWidth(90)
        self._poll_btn.setStyleSheet(
            "QPushButton { background: #2C3E50; color: white; padding: 4px 10px; "
            "border: none; border-radius: 4px; font-size: 11px; font-weight: 600; } "
            "QPushButton:hover { background: #34495E; } "
            "QPushButton:disabled { background: #555; color: #888; }"
        )
        self._poll_btn.clicked.connect(self._on_poll_clicked)
        stats.addWidget(self._poll_btn)

        inner.addLayout(stats)

        # ── Error bar (hidden by default) ──────────────────────────
        self._error_label = QLabel("")
        self._error_label.setStyleSheet(
            "color: #e74c3c; font-size: 11px; padding: 4px 0px; font-weight: normal;"
        )
        self._error_label.setWordWrap(True)
        self._error_label.hide()
        inner.addWidget(self._error_label)

        outer.addWidget(group)

    def _subscribe_events(self):
        self._bus.subscribe(EventType.CMS_POLL_STARTED,    self._on_poll_started)
        self._bus.subscribe(EventType.CMS_POLL_COMPLETED,  self._on_poll_completed)
        self._bus.subscribe(EventType.CMS_SESSION_ERROR,   self._on_session_error)
        self._bus.subscribe(EventType.CMS_MESSAGE_SENT,    self._on_message_sent)
        self._bus.subscribe(EventType.CMS_MESSAGE_RECEIVED,self._on_message_received)

    # ── Event handlers (run in background threads → signal to GUI) ───────

    def _on_poll_started(self, event: Event):
        self._update_signal.emit({
            "action": "poll_started",
            "data": event.data,
        })

    def _on_poll_completed(self, event: Event):
        self._update_signal.emit({
            "action": "poll_completed",
            "data": event.data,
        })

    def _on_session_error(self, event: Event):
        self._update_signal.emit({
            "action": "error",
            "data": event.data,
        })

    def _on_message_sent(self, event: Event):
        self._update_signal.emit({"action": "msg_sent", "data": event.data})

    def _on_message_received(self, event: Event):
        self._update_signal.emit({"action": "msg_received", "data": event.data})

    # ── GUI thread updates ───────────────────────────────────────────────

    @pyqtSlot(dict)
    def _apply_update(self, update: dict):
        action = update.get("action", "")
        data = update.get("data") or {}

        if action == "poll_started":
            mode = data.get("mode", "poll")
            client = data.get("client_call", "")
            if mode == "gateway" and client:
                self._status_label.setText(f"Gateway: {client}…")
            else:
                self._status_label.setText("Connecting…")
            self._status_label.setStyleSheet("color: #f39c12; font-weight: bold;")
            self._poll_btn.setEnabled(False)
            self._poll_btn.setText("Polling…")
            self._error_label.hide()

        elif action == "poll_completed":
            error = data.get("error")
            sent = data.get("sent", 0)
            received = data.get("received", 0)
            duration = data.get("duration", 0)

            now = datetime.now().strftime("%H:%M:%S")
            self._last_poll_label.setText(f"Last poll: {now}")

            if error:
                self._status_label.setText("Error")
                self._status_label.setStyleSheet("color: #e74c3c; font-weight: bold;")
                self._error_label.setText(f"⚠ {error}")
                self._error_label.show()
            else:
                self._status_label.setText("OK")
                self._status_label.setStyleSheet("color: #2ecc71; font-weight: bold;")
                self._error_label.hide()

            self._sent_label.setText(f"↑ {sent} sent")
            self._recv_label.setText(f"↓ {received} received")

            self._poll_btn.setEnabled(True)
            self._poll_btn.setText("Poll Now")
            self._update_queue_count()

        elif action == "error":
            self._status_label.setText("Error")
            self._status_label.setStyleSheet("color: #e74c3c; font-weight: bold;")
            err_msg = data.get("error", "Unknown error")
            self._error_label.setText(f"⚠ {err_msg}")
            self._error_label.show()
            self._poll_btn.setEnabled(True)
            self._poll_btn.setText("Poll Now")

    def _on_poll_clicked(self):
        if self._poller:
            self._poller.poll_now()
        else:
            log.warning("Poll button clicked but no poller configured")

    def _refresh_status(self):
        if self._enabled:
            self._status_label.setText("Idle")
            self._status_label.setStyleSheet("color: #aaa; font-weight: bold;")
            self._poll_btn.setEnabled(True)
        else:
            self._status_label.setText("Disabled")
            self._status_label.setStyleSheet("color: #888; font-weight: bold;")
            self._poll_btn.setEnabled(False)
        self._update_queue_count()

    def _update_queue_count(self):
        """Refresh the queue count label from the poller's outbound queue."""
        count = 0
        if self._poller and self._poller.outbound_queue:
            try:
                count = self._poller.outbound_queue.cms_queue_count()
            except Exception:
                pass
        self._queue_label.setText(f"Queue: {count}")

    def set_enabled(self, enabled: bool):
        self._enabled = enabled
        self._refresh_status()
