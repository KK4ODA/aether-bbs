"""
gui/files_tab.py

File Library tab — browse, upload, and delete files in the BBS file store.
Sysop can upload from the GUI directly; connected stations upload via RF commands.
"""

import logging
from pathlib import Path

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QSplitter,
    QTableWidget, QTableWidgetItem, QHeaderView,
    QTextEdit, QPushButton, QLabel, QComboBox,
    QLineEdit, QMessageBox, QFileDialog, QGroupBox,
    QFormLayout, QDialog, QDialogButtonBox, QProgressBar,
)
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont

from bbs.filestore import FileStore

log = logging.getLogger(__name__)


class UploadDialog(QDialog):
    """Dialog for sysop to upload a file from disk."""

    def __init__(self, store: FileStore, parent=None):
        super().__init__(parent)
        self._store = store
        self._path: Path | None = None
        self.setWindowTitle("Upload File to Library")
        self.setMinimumWidth(480)
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)

        form = QFormLayout()

        # File picker
        file_row = QHBoxLayout()
        self._file_edit = QLineEdit()
        self._file_edit.setPlaceholderText("No file selected")
        self._file_edit.setReadOnly(True)
        browse_btn = QPushButton("Browse…")
        browse_btn.clicked.connect(self._browse)
        file_row.addWidget(self._file_edit, stretch=1)
        file_row.addWidget(browse_btn)
        form.addRow("File:", file_row)

        # Area selector
        self._area_combo = QComboBox()
        areas = self._store.list_areas()
        for a in areas:
            self._area_combo.addItem(a["name"])
        form.addRow("Area:", self._area_combo)

        # Description
        self._desc_edit = QLineEdit()
        self._desc_edit.setPlaceholderText("One-line description (optional)")
        form.addRow("Description:", self._desc_edit)

        layout.addLayout(form)

        # Buttons
        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok |
            QDialogButtonBox.StandardButton.Cancel
        )
        btns.accepted.connect(self._on_ok)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

    def _browse(self):
        path, _ = QFileDialog.getOpenFileName(self, "Select File")
        if path:
            self._path = Path(path)
            self._file_edit.setText(str(self._path))

    def _on_ok(self):
        if not self._path or not self._path.exists():
            QMessageBox.warning(self, "No File", "Please select a file first.")
            return
        self.accept()

    def get_values(self):
        return {
            "path":  self._path,
            "area":  self._area_combo.currentText(),
            "desc":  self._desc_edit.text().strip(),
        }


class NewAreaDialog(QDialog):
    """Dialog to create a new file area."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("New File Area")
        self.setMinimumWidth(340)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self._name_edit = QLineEdit()
        self._name_edit.setPlaceholderText("e.g. IMAGES")
        self._desc_edit = QLineEdit()
        self._desc_edit.setPlaceholderText("Short description")
        form.addRow("Area name:", self._name_edit)
        form.addRow("Description:", self._desc_edit)
        layout.addLayout(form)
        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok |
            QDialogButtonBox.StandardButton.Cancel
        )
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

    def get_values(self):
        return {
            "name": self._name_edit.text().strip().upper(),
            "desc": self._desc_edit.text().strip(),
        }


class FilesTab(QWidget):

    def __init__(self, store: FileStore, sysop_callsign: str = "", parent=None):
        super().__init__(parent)
        self._store   = store
        self._sysop   = sysop_callsign.upper()
        self._build_ui()
        self.refresh()

    def _build_ui(self):
        layout = QVBoxLayout(self)

        # ── Top controls ──────────────────────────────────────────────────
        ctrl = QHBoxLayout()

        ctrl.addWidget(QLabel("Area:"))
        self._area_combo = QComboBox()
        self._area_combo.addItem("All areas")
        self._area_combo.currentTextChanged.connect(self.refresh)
        ctrl.addWidget(self._area_combo)

        refresh_btn = QPushButton("🔄 Refresh")
        refresh_btn.clicked.connect(self.refresh)
        ctrl.addWidget(refresh_btn)

        ctrl.addStretch()

        new_area_btn = QPushButton("➕ New Area")
        new_area_btn.clicked.connect(self._new_area)
        ctrl.addWidget(new_area_btn)

        upload_btn = QPushButton("⬆ Upload File")
        upload_btn.setToolTip("Upload a file from disk (sysop GUI upload)")
        upload_btn.clicked.connect(self._upload_file)
        ctrl.addWidget(upload_btn)

        layout.addLayout(ctrl)

        # ── Stats bar ─────────────────────────────────────────────────────
        self._stats_lbl = QLabel("")
        self._stats_lbl.setStyleSheet("color: #8B949E; font-size: 9pt;")
        layout.addWidget(self._stats_lbl)

        # ── Area management table ────────────────────────────────────────
        area_box = QGroupBox("File Areas  (double-click RF Upload to toggle)")
        area_box_layout = QVBoxLayout(area_box)
        area_box_layout.setContentsMargins(4, 4, 4, 4)
        self._area_table = QTableWidget(0, 4)
        self._area_table.setHorizontalHeaderLabels(
            ["Area", "Files", "RF Upload", "Description"]
        )
        ah = self._area_table.horizontalHeader()
        ah.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        ah.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)  # Description fills
        ah.setStretchLastSection(False)
        for col, w in [(0, 80), (1, 50), (2, 75)]:
            ah.resizeSection(col, w)
        self._area_table.verticalHeader().setVisible(False)
        self._area_table.setEditTriggers(
            QTableWidget.EditTrigger.NoEditTriggers
        )
        self._area_table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows
        )
        self._area_table.setMaximumHeight(130)
        self._area_table.itemDoubleClicked.connect(self._toggle_area_rf_upload)
        area_box_layout.addWidget(self._area_table)
        layout.addWidget(area_box)

        # ── Splitter: file list (top) + file info (bottom) ─────────────────
        splitter = QSplitter(Qt.Orientation.Vertical)

        # File table
        self._file_table = QTableWidget(0, 7)
        self._file_table.setHorizontalHeaderLabels([
            "#", "Area", "Filename", "Size", "Downloads", "Uploaded by", "Description"
        ])
        hdr = self._file_table.horizontalHeader()
        hdr.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        hdr.setSectionResizeMode(6, QHeaderView.ResizeMode.Stretch)  # Description fills
        hdr.setStretchLastSection(False)
        # Initial widths: #, Area, Filename, Size, Downloads, Uploaded by
        for col, w in [(0, 36), (1, 70), (2, 160), (3, 65), (4, 75), (5, 90)]:
            hdr.resizeSection(col, w)
        self._file_table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows
        )
        self._file_table.setEditTriggers(
            QTableWidget.EditTrigger.NoEditTriggers
        )
        self._file_table.setAlternatingRowColors(True)
        self._file_table.itemSelectionChanged.connect(self._on_selection)
        self._file_table.itemDoubleClicked.connect(
            lambda _: self._open_selected()
        )
        splitter.addWidget(self._file_table)

        # Detail / actions pane
        detail_widget = QWidget()
        detail_layout = QHBoxLayout(detail_widget)

        # File details
        self._detail_box = QGroupBox("File Details")
        detail_form = QVBoxLayout(self._detail_box)
        self._detail_lbl = QLabel("Select a file to view details.")
        self._detail_lbl.setWordWrap(True)
        self._detail_lbl.setFont(QFont("Consolas", 9))
        detail_form.addWidget(self._detail_lbl)
        detail_layout.addWidget(self._detail_box, stretch=1)

        # Action buttons
        actions_box = QGroupBox("Actions")
        actions_layout = QVBoxLayout(actions_box)

        self._open_btn = QPushButton("📂 Open File")
        self._open_btn.setEnabled(False)
        self._open_btn.setToolTip(
            "Open with the system default app  (or double-click a row)"
        )
        self._open_btn.clicked.connect(self._open_selected)
        actions_layout.addWidget(self._open_btn)

        self._download_btn = QPushButton("⬇ Save to Disk")
        self._download_btn.setEnabled(False)
        self._download_btn.clicked.connect(self._download_selected)
        actions_layout.addWidget(self._download_btn)

        self._delete_btn = QPushButton("🗑 Delete File")
        self._delete_btn.setEnabled(False)
        self._delete_btn.clicked.connect(self._delete_selected)
        actions_layout.addWidget(self._delete_btn)

        actions_layout.addStretch()
        detail_layout.addWidget(actions_box)

        splitter.addWidget(detail_widget)
        splitter.setSizes([350, 150])
        layout.addWidget(splitter)

        # ── RF command reference ──────────────────────────────────────────
        ref = QGroupBox("RF Commands (for connecting stations)")
        ref_layout = QVBoxLayout(ref)
        ref_lbl = QLabel(
            "  FA                          — List file areas\n"
            "  FL [area]                   — List files\n"
            "  FG <area> <filename>        — Download a file\n"
            "  FP <area> <filename> <desc> — Upload a file\n"
            "  FD <area> <filename>        — Delete (own files only; sysop: any)"
        )
        ref_lbl.setFont(QFont("Consolas", 9))
        ref_layout.addWidget(ref_lbl)
        layout.addWidget(ref)

    # ── Refresh ───────────────────────────────────────────────────────────

    def refresh(self):
        area_text = self._area_combo.currentText()
        area = None if area_text == "All areas" else area_text

        files = self._store.list_files(area)
        self._file_table.setRowCount(0)

        for i, f in enumerate(files):
            row = self._file_table.rowCount()
            self._file_table.insertRow(row)
            self._file_table.setItem(row, 0, QTableWidgetItem(str(i + 1)))
            self._file_table.setItem(row, 1, QTableWidgetItem(f["area"]))
            self._file_table.setItem(row, 2, QTableWidgetItem(f["filename"]))
            self._file_table.setItem(row, 3, QTableWidgetItem(
                self._fmt_size(f["size_bytes"])
            ))
            self._file_table.setItem(row, 4, QTableWidgetItem(str(f["dl_count"])))
            self._file_table.setItem(row, 5, QTableWidgetItem(f["uploader"]))
            self._file_table.setItem(row, 6, QTableWidgetItem(f["description"]))
            # Store (area, filename) in row 0's UserRole
            self._file_table.item(row, 0).setData(
                Qt.ItemDataRole.UserRole, (f["area"], f["filename"])
            )

        # Update area combo and area management table
        areas = self._store.list_areas()
        self._area_combo.blockSignals(True)
        current = self._area_combo.currentText()
        self._area_combo.clear()
        self._area_combo.addItem("All areas")
        for a in areas:
            self._area_combo.addItem(a["name"])
        idx = self._area_combo.findText(current)
        if idx >= 0:
            self._area_combo.setCurrentIndex(idx)
        self._area_combo.blockSignals(False)

        # Populate area management table
        self._area_table.setRowCount(0)
        for a in areas:
            r = self._area_table.rowCount()
            self._area_table.insertRow(r)
            rf_allowed = bool(a.get("allow_remote_upload", 1))
            rf_text = "✔ Yes" if rf_allowed else "✘ No"
            rf_color = "#2EA043" if rf_allowed else "#E74C3C"
            for col, (val, tip) in enumerate([
                (a["name"],           "Area name"),
                (str(a["file_count"]), "Files in this area"),
                (rf_text,             "Double-click to toggle RF upload permission"),
                (a.get("description",""), ""),
            ]):
                item = QTableWidgetItem(val)
                item.setToolTip(tip)
                if col == 2:
                    from PyQt6.QtGui import QColor
                    item.setForeground(QColor(rf_color))
                self._area_table.setItem(r, col, item)

        # Stats
        total_files = self._store.file_count()
        total_bytes = self._store.total_bytes()
        self._stats_lbl.setText(
            f"{total_files} file(s)  •  {self._fmt_size(total_bytes)} total"
        )

        self._open_btn.setEnabled(False)
        self._download_btn.setEnabled(False)
        self._delete_btn.setEnabled(False)
        self._detail_lbl.setText("Select a file to view details.")

    # ── Selection ─────────────────────────────────────────────────────────

    def _on_selection(self):
        key = self._selected_key()
        if not key:
            self._open_btn.setEnabled(False)
            self._download_btn.setEnabled(False)
            self._delete_btn.setEnabled(False)
            self._detail_lbl.setText("Select a file to view details.")
            return

        area, filename = key
        meta = self._store.get_file_meta(area, filename)
        if not meta:
            return

        self._detail_lbl.setText(
            f"Filename:    {meta['filename']}\n"
            f"Area:        {meta['area']}\n"
            f"Size:        {self._fmt_size(meta['size_bytes'])} "
            f"({meta['size_bytes']:,} bytes)\n"
            f"Uploaded by: {meta['uploader']}\n"
            f"Uploaded at: {str(meta['uploaded_at'])[:19]}\n"
            f"Downloads:   {meta['dl_count']}\n"
            f"Last DL:     {str(meta['last_dl_at'] or 'Never')[:19]}\n"
            f"SHA-256:     {meta['sha256'][:32]}…\n"
            f"Description: {meta['description']}"
        )
        self._open_btn.setEnabled(True)
        self._download_btn.setEnabled(True)
        self._delete_btn.setEnabled(True)

    def _selected_key(self):
        row = self._file_table.currentRow()
        if row < 0:
            return None
        item = self._file_table.item(row, 0)
        if not item:
            return None
        return item.data(Qt.ItemDataRole.UserRole)

    # ── Actions ───────────────────────────────────────────────────────────

    def _open_selected(self):
        """Save file to a system temp file and open with the OS default app."""
        key = self._selected_key()
        if not key:
            return
        area, filename = key
        data = self._store.read_file(area, filename)
        if data is None:
            QMessageBox.critical(self, "Error", "Could not read file from store.")
            return

        import sys, subprocess, tempfile, os
        # Write to a named temp file that persists long enough for the app to open.
        # NamedTemporaryFile with delete=False gives us a path we can hand to the OS.
        suffix = Path(filename).suffix or ""
        tmp = tempfile.NamedTemporaryFile(
            prefix=Path(filename).stem + "_",
            suffix=suffix,
            delete=False,
        )
        try:
            tmp.write(data)
            tmp.flush()
            tmp_path = tmp.name
        finally:
            tmp.close()

        try:
            if sys.platform == "win32":
                os.startfile(tmp_path)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", tmp_path])
            else:
                subprocess.Popen(["xdg-open", tmp_path])
        except Exception as e:
            QMessageBox.warning(
                self, "Cannot Open",
                f"No application found for this file type.\n\n"
                f"Use Save to Disk instead.\n\nDetail: {e}"
            )

    def _download_selected(self):
        key = self._selected_key()
        if not key:
            return
        area, filename = key
        data = self._store.read_file(area, filename)
        if data is None:
            QMessageBox.critical(self, "Error", "Could not read file from store.")
            return
        dest, _ = QFileDialog.getSaveFileName(self, "Save File As", filename)
        if not dest:
            return
        Path(dest).write_bytes(data)
        QMessageBox.information(
            self, "Saved",
            f"Saved {self._fmt_size(len(data))} to:\n{dest}"
        )

    def _delete_selected(self):
        key = self._selected_key()
        if not key:
            return
        area, filename = key
        reply = QMessageBox.question(
            self, "Delete File",
            f"Delete {area}/{filename}?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        if self._store.delete_file(area, filename):
            self.refresh()
        else:
            QMessageBox.critical(self, "Error", "Delete failed.")

    def _upload_file(self):
        dlg = UploadDialog(self._store, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        vals = dlg.get_values()
        path: Path = vals["path"]
        try:
            data = path.read_bytes()
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Could not read file:\n{e}")
            return

        # Sysop GUI upload uses a generous hard cap (50 MB) not the RF limit
        SYSOP_MAX = 50 * 1024 * 1024
        if len(data) > SYSOP_MAX:
            QMessageBox.warning(
                self, "File Too Large",
                f"File exceeds 50 MB sysop upload limit."
            )
            return

        ok, reason = self._store.commit_upload(
            area=vals["area"],
            filename=path.name,
            uploader=self._sysop or "SYSOP",
            description=vals["desc"],
            data=data,
            is_remote=False,  # sysop GUI upload bypasses policy checks
        )
        if ok:
            QMessageBox.information(
                self, "Uploaded",
                f"{path.name} ({self._fmt_size(len(data))}) added to {vals['area']}."
            )
            self.refresh()
        else:
            QMessageBox.critical(self, "Error", f"Upload failed: {reason}")

    def _toggle_area_rf_upload(self, item):
        """Double-click any cell in RF Upload column to toggle the flag."""
        row = item.row()
        name_item = self._area_table.item(row, 0)
        rf_item   = self._area_table.item(row, 2)
        if not name_item or not rf_item:
            return
        area_name = name_item.text()
        current   = rf_item.text() == "✔ Yes"
        self._store.set_area_remote_upload(area_name, not current)
        self.refresh()

    def _new_area(self):
        dlg = NewAreaDialog(self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        vals = dlg.get_values()
        if not vals["name"]:
            QMessageBox.warning(self, "Invalid", "Area name cannot be empty.")
            return
        if self._store.create_area(vals["name"], vals["desc"]):
            self.refresh()
        else:
            QMessageBox.critical(self, "Error", "Could not create area.")

    # ── Helpers ───────────────────────────────────────────────────────────

    @staticmethod
    def _fmt_size(n: int) -> str:
        if n < 1024:
            return f"{n} B"
        elif n < 1024 * 1024:
            return f"{n / 1024:.1f} KB"
        else:
            return f"{n / (1024*1024):.1f} MB"
