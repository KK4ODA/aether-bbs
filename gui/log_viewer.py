"""
gui/log_viewer.py

Filterable, color-coded log viewer.
Reads from the shared gui_log_queue populated by the logging framework.
"""

import logging
import logging.handlers
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout,
    QTextEdit, QLineEdit, QComboBox,
    QPushButton, QLabel, QCheckBox
)
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QColor, QTextCharFormat, QFont

from core.logging_manager import gui_log_queue

log = logging.getLogger(__name__)

LEVEL_COLORS = {
    "DEBUG":    "#888888",
    "INFO":     "#CCCCCC",
    "WARNING":  "#F39C12",
    "ERROR":    "#E74C3C",
    "CRITICAL": "#C0392B",
}


class LogViewerTab(QWidget):

    def __init__(self, parent=None):
        super().__init__(parent)
        self._min_level = logging.DEBUG
        self._filter    = ""
        self._exclude   = ""
        self._muted_loggers: set[str] = set()
        self._auto_scroll = True
        self._build_ui()

        # Poll log queue 4x/second
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._drain_queue)
        self._timer.start(250)

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(4)

        # Controls row 1: level, search, auto-scroll, clear
        ctrl_layout = QHBoxLayout()

        ctrl_layout.addWidget(QLabel("Level:"))
        self._level_combo = QComboBox()
        self._level_combo.addItems(["DEBUG", "INFO", "WARNING", "ERROR"])
        self._level_combo.setCurrentText("INFO")
        self._level_combo.currentTextChanged.connect(self._on_level_changed)
        ctrl_layout.addWidget(self._level_combo)

        ctrl_layout.addWidget(QLabel("Filter:"))
        self._filter_edit = QLineEdit()
        self._filter_edit.setPlaceholderText("show only lines matching…")
        self._filter_edit.textChanged.connect(self._on_filter_changed)
        ctrl_layout.addWidget(self._filter_edit)

        ctrl_layout.addWidget(QLabel("Exclude:"))
        self._exclude_edit = QLineEdit()
        self._exclude_edit.setPlaceholderText("hide lines matching…")
        self._exclude_edit.setToolTip(
            "Hide log lines containing this text (case-insensitive)")
        self._exclude_edit.textChanged.connect(self._on_exclude_changed)
        ctrl_layout.addWidget(self._exclude_edit)

        self._autoscroll_chk = QCheckBox("Auto-scroll")
        self._autoscroll_chk.setChecked(True)
        self._autoscroll_chk.toggled.connect(lambda v: setattr(self, "_auto_scroll", v))
        ctrl_layout.addWidget(self._autoscroll_chk)

        clear_btn = QPushButton("Clear")
        clear_btn.clicked.connect(self._clear)
        ctrl_layout.addWidget(clear_btn)

        layout.addLayout(ctrl_layout)

        # Controls row 2: mute toggles for known noisy loggers
        mute_layout = QHBoxLayout()
        mute_layout.addWidget(QLabel("Mute:"))

        self._mute_checks: dict[str, QCheckBox] = {}
        for logger_name, label in [
            ("vara.omnirig", "OmniRig"),
            ("vara.mux",     "VARA Mux"),
            ("aether.monitor", "Aether HF status"),
            ("bbs.dispatcher", "Dispatcher"),
            ("bbs.bulletins",  "Bulletins"),
        ]:
            cb = QCheckBox(label)
            cb.setToolTip(f"Hide all log lines from {logger_name}")
            cb.setStyleSheet("QCheckBox { font-size: 8pt; }")
            cb.toggled.connect(lambda checked, name=logger_name:
                               self._on_mute_toggled(name, checked))
            self._mute_checks[logger_name] = cb
            mute_layout.addWidget(cb)

        mute_layout.addStretch()
        layout.addLayout(mute_layout)

        # Log display
        self._text = QTextEdit()
        self._text.setReadOnly(True)
        self._text.setFont(QFont("Courier New", 9))
        self._text.setStyleSheet("background:#1A1A2E;color:#CCCCCC;")
        layout.addWidget(self._text)

    def _drain_queue(self):
        count = 0
        while not gui_log_queue.empty() and count < 100:
            try:
                record = gui_log_queue.get_nowait()
                self._append_record(record)
                count += 1
            except Exception:
                break

    def _append_record(self, record: logging.LogRecord):
        if record.levelno < self._min_level:
            return

        # Mute by logger name (exact match or parent match)
        rec_name = record.name or ""
        for muted in self._muted_loggers:
            if rec_name == muted or rec_name.startswith(muted + "."):
                return

        msg = self.format_record(record)

        # Positive filter (show only matching)
        if self._filter and self._filter.lower() not in msg.lower():
            return

        # Negative filter (hide matching)
        if self._exclude and self._exclude.lower() in msg.lower():
            return

        color = LEVEL_COLORS.get(record.levelname, "#CCCCCC")
        self._text.setTextColor(QColor(color))
        self._text.append(msg)

        if self._auto_scroll:
            bar = self._text.verticalScrollBar()
            bar.setValue(bar.maximum())

    @staticmethod
    def format_record(record: logging.LogRecord) -> str:
        return (
            f"{record.asctime if hasattr(record, 'asctime') else ''}"
            f"  {record.levelname:<8}"
            f"  {record.name:<25}"
            f"  {record.getMessage()}"
        )

    def _on_level_changed(self, level_str: str):
        self._min_level = getattr(logging, level_str, logging.DEBUG)

    def _on_filter_changed(self, text: str):
        self._filter = text

    def _on_exclude_changed(self, text: str):
        self._exclude = text

    def _on_mute_toggled(self, logger_name: str, muted: bool):
        if muted:
            self._muted_loggers.add(logger_name)
        else:
            self._muted_loggers.discard(logger_name)

    def _clear(self):
        self._text.clear()
