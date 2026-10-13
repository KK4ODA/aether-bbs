"""
gui/update_dialog.py

Dialog shown when a software update is available — automatically at
startup, or from the status-bar indicator / Settings → About & Updates.
Shows what's new and offers Update now / Remind me later / Skip this version.
"""

import logging
import threading
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QTextEdit,
    QPushButton, QProgressBar,
)
from PyQt6.QtCore import QTimer, pyqtSignal

log = logging.getLogger(__name__)


class UpdateDialog(QDialog):
    """Modal dialog presenting an available update and offering to install it."""

    # Emitted from the worker thread; delivered on the GUI thread.
    _apply_done = pyqtSignal(object)   # UpdateApplyResult
    _progress_msg = pyqtSignal(str)

    def __init__(self, check_result, updater, restart_callback, parent=None,
                 on_skip=None, active_sessions=0):
        super().__init__(parent)
        self._result = check_result
        self._updater = updater
        self._restart = restart_callback
        self._on_skip = on_skip
        self._active_sessions = active_sessions
        self.setWindowTitle("Aether BBS Update")
        self.setMinimumWidth(560)
        self.setMinimumHeight(380)
        self._apply_done.connect(self._on_apply_done)
        self._progress_msg.connect(self._set_status)
        self._build_ui()

    def _headline(self) -> str:
        r = self._result
        if r.latest_version and r.latest_version != r.current_version:
            return (f"<b>Aether BBS {r.latest_version} is available.</b>"
                    f"&nbsp; You have {r.current_version}.")
        return (f"<b>{len(r.commits)} new change(s) are available</b> "
                f"({r.current_hash} → {r.remote_hash}).")

    def _build_ui(self):
        layout = QVBoxLayout(self)
        r = self._result

        head = QLabel(self._headline())
        head.setStyleSheet("font-size: 11pt;")
        layout.addWidget(head)

        if r.release and r.release.url:
            link = QLabel(f'<a href="{r.release.url}">Release page on GitHub</a>')
            link.setOpenExternalLinks(True)
            layout.addWidget(link)

        layout.addSpacing(6)

        notes = QTextEdit()
        notes.setReadOnly(True)
        if r.commits:
            layout.addWidget(QLabel("<b>What's new:</b>"))
            notes.setPlainText("\n".join(
                f"  {c.hash}  {c.subject}  ({c.author})" for c in r.commits))
        elif r.release and r.release.notes.strip():
            layout.addWidget(QLabel("<b>What's new:</b>"))
            notes.setMarkdown(r.release.notes)
        else:
            notes.setPlainText("No release notes were published.")
        layout.addWidget(notes, 1)

        how = {
            "git": "Your git copy will be moved to the new release.",
            "installer": "The new version's installer is downloaded from GitHub and "
                         "run.  Your settings, messages and files are not touched.",
        }.get(r.install_kind,
              "The new version is downloaded from GitHub and installed over this "
              "folder.  Replaced files are kept in backups\\.  Your settings, "
              "messages and files are not touched.")
        info = QLabel(how + "  Aether BBS restarts when it is done.")
        info.setWordWrap(True)
        info.setStyleSheet("color:#b5bac1;")
        layout.addWidget(info)

        if self._active_sessions:
            warn = QLabel(
                f"{self._active_sessions} station(s) are connected now.  "
                "Updating restarts the BBS and drops them.")
            warn.setWordWrap(True)
            warn.setStyleSheet("color:#e2a84b; font-weight:bold;")
            layout.addWidget(warn)

        # Progress bar (hidden until update starts)
        self._progress = QProgressBar()
        self._progress.setRange(0, 0)  # indeterminate
        self._progress.setTextVisible(False)
        self._progress.hide()
        layout.addWidget(self._progress)

        self._status = QLabel("")
        self._status.setWordWrap(True)
        self._status.setStyleSheet("color:#e2a84b;")
        layout.addWidget(self._status)

        btn_row = QHBoxLayout()
        self._skip_btn = QPushButton("Skip this version")
        self._skip_btn.setToolTip("Don't ask again until a newer version comes out")
        self._skip_btn.clicked.connect(self._on_skip_clicked)
        self._skip_btn.setVisible(bool(r.latest_version and r.release and self._on_skip))
        btn_row.addWidget(self._skip_btn)
        btn_row.addStretch()

        self._later_btn = QPushButton("Remind me later")
        self._later_btn.clicked.connect(self.reject)
        btn_row.addWidget(self._later_btn)

        self._update_btn = QPushButton("Update now")
        self._update_btn.setDefault(True)
        self._update_btn.setStyleSheet(
            "background-color:#5865f2; color:white; padding:6px 18px;"
            "border-radius:4px; font-weight:bold;"
        )
        self._update_btn.clicked.connect(self._on_update_clicked)
        btn_row.addWidget(self._update_btn)

        layout.addLayout(btn_row)

    # ── actions ──────────────────────────────────────────────────────────

    def _set_status(self, text: str):
        self._status.setText(text)

    def _on_skip_clicked(self):
        if self._on_skip:
            self._on_skip(self._result.latest_version)
        self.reject()

    def _on_update_clicked(self):
        for b in (self._update_btn, self._later_btn, self._skip_btn):
            b.setEnabled(False)
        self._progress.show()
        self._status.setStyleSheet("color:#e2a84b;")
        self._set_status("Starting update ...")

        threading.Thread(
            target=self._do_apply,
            daemon=True,
            name="UpdateApply",
        ).start()

    def _do_apply(self):
        result = self._updater.apply_update(self._result, progress=self._progress_msg.emit)
        self._apply_done.emit(result)

    def _on_apply_done(self, result):
        self._progress.hide()
        if result.success and result.manual_step:
            # macOS: the sysop drags the new app into Applications
            self._status.setStyleSheet("color:#57f287;")
            self._set_status(result.manual_step)
            self._later_btn.setText("Close")
            self._later_btn.setEnabled(True)
            return
        if result.success:
            msg = f"Updated {result.old_hash} → {result.new_hash}."
            if result.warning:
                self._status.setStyleSheet("color:#e2a84b;")
                self._set_status(msg + "  " + result.warning +
                                 "\n\nRestarting in a few seconds ...")
                delay = 8000
            else:
                self._status.setStyleSheet("color:#57f287;")
                self._set_status(msg + "  Restarting ...")
                delay = 1500
            if not result.relaunch:
                self._set_status(f"Downloaded {result.new_hash}.  The installer finishes "
                                 "the update and starts Aether BBS again.  Closing ...")
            relaunch = result.relaunch
            QTimer.singleShot(delay, lambda: self._restart(relaunch=relaunch))
        else:
            self._status.setStyleSheet("color:#ed4245;")
            self._set_status(f"Update failed: {result.error}")
            for b in (self._update_btn, self._later_btn, self._skip_btn):
                b.setEnabled(True)
