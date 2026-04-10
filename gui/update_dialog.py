"""
gui/update_dialog.py

Dialog shown when a software update is available.
Displays the commit log of what's new and offers Update & Restart / Later.
"""

import logging
import threading
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QTextEdit,
    QCheckBox, QPushButton, QProgressBar, QWidget,
)
from PyQt6.QtCore import Qt, pyqtSignal

from core.version import get_version_display

log = logging.getLogger(__name__)


class UpdateDialog(QDialog):
    """Modal dialog presenting available updates and offering to apply them."""

    # Emitted (on the GUI thread) when the background apply finishes.
    _apply_done = pyqtSignal(object)   # UpdateApplyResult

    def __init__(self, check_result, updater, restart_callback, parent=None):
        super().__init__(parent)
        self._result = check_result
        self._updater = updater
        self._restart = restart_callback
        self.setWindowTitle("Software Update")
        self.setMinimumWidth(520)
        self.setMinimumHeight(360)
        self._apply_done.connect(self._on_apply_done)
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)

        # Current version
        layout.addWidget(QLabel(f"Current version:  {get_version_display()}"))
        layout.addWidget(QLabel(
            f"Latest available: {self._result.remote_hash}"
        ))

        layout.addSpacing(8)

        # Commit log
        if self._result.commits:
            layout.addWidget(QLabel(
                f"<b>{len(self._result.commits)} new commit(s):</b>"
            ))
            log_view = QTextEdit()
            log_view.setReadOnly(True)
            lines = []
            for c in self._result.commits:
                lines.append(f"  {c.hash}  {c.subject}  ({c.author})")
            log_view.setPlainText("\n".join(lines))
            log_view.setMaximumHeight(180)
            layout.addWidget(log_view)

        # AETHER section
        if self._result.aether_update_available:
            layout.addSpacing(4)
            layout.addWidget(QLabel(
                f"<b>AETHER HF modem: {len(self._result.aether_commits)} "
                f"new commit(s)</b>"
            ))

        # Include AETHER checkbox
        self._aether_cb = QCheckBox("Also update AETHER HF modem")
        self._aether_cb.setChecked(self._result.aether_update_available)
        self._aether_cb.setEnabled(self._result.aether_update_available)
        layout.addWidget(self._aether_cb)

        layout.addSpacing(8)

        # Progress bar (hidden until update starts)
        self._progress = QProgressBar()
        self._progress.setRange(0, 0)  # indeterminate
        self._progress.setTextVisible(False)
        self._progress.hide()
        layout.addWidget(self._progress)

        # Status label
        self._status = QLabel("")
        self._status.setStyleSheet("color:#e2a84b;")
        layout.addWidget(self._status)

        # Buttons
        btn_row = QHBoxLayout()
        btn_row.addStretch()

        self._later_btn = QPushButton("Later")
        self._later_btn.clicked.connect(self.reject)
        btn_row.addWidget(self._later_btn)

        self._update_btn = QPushButton("Update && Restart")
        self._update_btn.setDefault(True)
        self._update_btn.setStyleSheet(
            "background-color:#5865f2; color:white; padding:6px 18px;"
            "border-radius:4px; font-weight:bold;"
        )
        self._update_btn.clicked.connect(self._on_update_clicked)
        btn_row.addWidget(self._update_btn)

        layout.addLayout(btn_row)

    # ── actions ──────────────────────────────────────────────────────────

    def _on_update_clicked(self):
        self._update_btn.setEnabled(False)
        self._later_btn.setEnabled(False)
        self._progress.show()
        self._status.setText("Pulling latest changes ...")

        include_aether = self._aether_cb.isChecked()
        threading.Thread(
            target=self._do_apply,
            args=(include_aether,),
            daemon=True,
            name="UpdateApply",
        ).start()

    def _do_apply(self, include_aether: bool):
        result = self._updater.apply_update(include_aether=include_aether)
        self._apply_done.emit(result)

    def _on_apply_done(self, result):
        self._progress.hide()
        if result.success:
            self._status.setText(
                f"Updated {result.old_hash} \u2192 {result.new_hash}.  "
                "Restarting ..."
            )
            self._status.setStyleSheet("color:#57f287;")
            # Brief delay so the user sees the message, then restart
            from PyQt6.QtCore import QTimer
            QTimer.singleShot(1500, self._restart)
        else:
            self._status.setText(f"Update failed: {result.error}")
            self._status.setStyleSheet("color:#ed4245;")
            self._update_btn.setEnabled(True)
            self._later_btn.setEnabled(True)
