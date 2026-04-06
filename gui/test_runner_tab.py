"""
gui/test_runner_tab.py

Test Runner Tab — execute the unit test suite from inside the GUI.

Features:
  - Lists all discovered test classes and methods
  - Run all / run selected / run failed-only
  - Per-test pass/fail/error/skip indicators
  - Summary bar with counts, progress, and elapsed time
  - Failure detail panel shows output for the selected test
  - Full-width vertical layout for maximum readability
"""

import re
import sys
import time
import subprocess
import threading
from pathlib import Path
from datetime import datetime
from typing import Optional

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QSplitter,
    QTreeWidget, QTreeWidgetItem, QPlainTextEdit,
    QPushButton, QLabel, QProgressBar, QFrame,
    QSizePolicy, QHeaderView,
)
from PyQt6.QtCore import Qt, QTimer, pyqtSignal, QObject, QThread
from PyQt6.QtGui import QFont, QColor

from gui.theme import (
    BG_DEEPEST, BG_DARK, BG_MEDIUM, BG_ELEVATED, BORDER_SUBTLE,
    TEXT_PRIMARY, TEXT_SECONDARY, TEXT_MUTED,
    ACCENT, GREEN, GREEN_DIM, RED, RED_DIM, BLUE, YELLOW,
    BTN_SUCCESS, BTN_DANGER, BTN_WARNING, BTN_ACCENT,
)

log_import = None
try:
    import logging
    log_import = logging.getLogger(__name__)
except Exception:
    pass


# ── Local aliases ─────────────────────────────────────────────────────────
BTN_GREEN  = BTN_SUCCESS
BTN_RED    = BTN_DANGER
BTN_AMBER  = BTN_WARNING
BTN_BLUE   = BTN_ACCENT

ICON_DOT   = "\u25cf"
ICON_EMPTY = "\u25cb"

STATUS_COLORS = {
    "PASS":    GREEN,
    "FAIL":    RED,
    "ERROR":   YELLOW,
    "SKIP":    TEXT_SECONDARY,
    "RUNNING": BLUE,
    "WAITING": TEXT_MUTED,
}

# Regex patterns for parsing pytest output
RE_TEST_ID    = re.compile(r"^(tests/\S+)::([\w]+)::(test_\w+)")
RE_PASSED     = re.compile(r"^(tests/\S+)::([\w]+)::(test_\w+)\s+PASSED")
RE_FAILED     = re.compile(r"^(tests/\S+)::([\w]+)::(test_\w+)\s+FAILED")
RE_ERROR      = re.compile(r"^(tests/\S+)::([\w]+)::(test_\w+)\s+ERROR")
RE_SKIPPED    = re.compile(r"^(tests/\S+)::([\w]+)::(test_\w+)\s+SKIPPED")
RE_SUMMARY    = re.compile(
    r"=+ ([\d]+ passed)?[,\s]*([\d]+ failed)?[,\s]*([\d]+ error)?[,\s]*([\d]+ warning)? in ([\d.]+)s"
)
RE_COLLECT    = re.compile(r"<([\w]+) (tests/\S+)::([\w]+)::(test_\w+)>")


# ── Worker — runs pytest in a subprocess ─────────────────────────────────────

class PytestWorker(QObject):
    """Runs pytest in a subprocess; emits signals line by line."""

    line_ready    = pyqtSignal(str)
    result_ready  = pyqtSignal(str, str)
    finished      = pyqtSignal(int, float)
    discovered    = pyqtSignal(list)

    def __init__(self, test_path: str, test_ids: Optional[list] = None):
        super().__init__()
        self._test_path = test_path
        self._test_ids  = test_ids
        self._proc: Optional[subprocess.Popen] = None
        self._cancelled = False

    def run(self):
        t0 = time.monotonic()
        cmd = [
            sys.executable, "-m", "pytest",
            "-v", "--tb=short", "--no-header", "--color=no",
        ]
        if self._test_ids:
            cmd += self._test_ids
        else:
            cmd.append(self._test_path)

        try:
            self._proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1,
                cwd=str(Path(self._test_path).parent.parent),
            )
            for line in self._proc.stdout:
                if self._cancelled:
                    self._proc.terminate()
                    break
                line = line.rstrip()
                self.line_ready.emit(line)
                for pattern, status in [
                    (RE_PASSED, "PASS"), (RE_FAILED, "FAIL"),
                    (RE_ERROR, "ERROR"), (RE_SKIPPED, "SKIP"),
                ]:
                    m = pattern.match(line)
                    if m:
                        self.result_ready.emit(f"{m.group(2)}::{m.group(3)}", status)
                        break
            rc = self._proc.wait()
        except FileNotFoundError:
            self.line_ready.emit("[ERROR] pytest not found. Install: pip install pytest")
            rc = -1
        self.finished.emit(rc, time.monotonic() - t0)

    def collect(self):
        cmd = [
            sys.executable, "-m", "pytest",
            "--collect-only", "-q", "--no-header", "--color=no",
            self._test_path,
        ]
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True,
                cwd=str(Path(self._test_path).parent.parent),
            )
            test_ids = [
                line.strip() for line in result.stdout.splitlines()
                if "::" in line and line.strip().startswith("tests/")
            ]
            self.discovered.emit(test_ids)
        except FileNotFoundError:
            self.discovered.emit([])

    def cancel(self):
        self._cancelled = True
        if self._proc:
            try: self._proc.terminate()
            except Exception: pass


# ── Test tree item ─────────────────────────────────────────────────────────────

class TestItem(QTreeWidgetItem):
    def __init__(self, class_name: str, test_name: str):
        super().__init__()
        self.class_name = class_name
        self.test_name  = test_name
        self.test_id    = f"{class_name}::{test_name}"
        self.status     = "WAITING"
        self._set_display()

    def set_status(self, status: str):
        self.status = status
        self._set_display()

    def _set_display(self):
        color = STATUS_COLORS.get(self.status, TEXT_PRIMARY)
        icon = ICON_DOT if self.status != "WAITING" else ICON_EMPTY
        display_name = self.test_name
        if display_name.startswith("test_"):
            display_name = display_name[5:]
        self.setText(0, f" {icon}  {display_name}")
        self.setText(1, f"  {self.status}  ")
        self.setForeground(0, QColor(color))
        self.setForeground(1, QColor(color))


# ── Main tab widget ───────────────────────────────────────────────────────────

class TestRunnerTab(QWidget):

    def __init__(self, parent=None):
        super().__init__(parent)
        self._tests_path = str(
            Path(__file__).parent.parent / "tests" / "test_core.py"
        )
        self._worker:  Optional[PytestWorker] = None
        self._thread:  Optional[QThread]      = None
        self._running  = False
        self._t_start  = 0.0

        self._class_items: dict[str, QTreeWidgetItem] = {}
        self._test_items:  dict[str, TestItem]        = {}

        # Per-test output capture
        self._test_output: dict[str, list[str]] = {}
        self._current_test_id: str = ""
        self._all_output: list[str] = []

        self._build_ui()
        self._discover_tests()

    # ── UI construction ───────────────────────────────────────────────────

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(6)
        root.setContentsMargins(8, 8, 8, 8)

        # ── Toolbar ───────────────────────────────────────────────────
        toolbar = QHBoxLayout()
        toolbar.setSpacing(4)

        self._run_all_btn   = QPushButton("\u25b6  Run All")
        self._run_sel_btn   = QPushButton("\u25b6  Selected")
        self._run_fail_btn  = QPushButton("\u21ba  Failed")
        self._cancel_btn    = QPushButton("\u25a0  Cancel")

        self._run_all_btn.setStyleSheet(BTN_GREEN)
        self._run_sel_btn.setStyleSheet(BTN_BLUE)
        self._run_fail_btn.setStyleSheet(BTN_AMBER)
        self._cancel_btn.setStyleSheet(BTN_RED)
        self._cancel_btn.setEnabled(False)

        self._run_all_btn.clicked.connect(self._run_all)
        self._run_sel_btn.clicked.connect(self._run_selected)
        self._run_fail_btn.clicked.connect(self._run_failed)
        self._cancel_btn.clicked.connect(self._cancel)

        for btn in (self._run_all_btn, self._run_sel_btn,
                    self._run_fail_btn, self._cancel_btn):
            btn.setFixedHeight(30)
            btn.setMinimumWidth(80)
            toolbar.addWidget(btn)

        toolbar.addStretch()

        self._status_label = QLabel("Ready")
        self._status_label.setStyleSheet(f"color:{TEXT_SECONDARY};font-size:10pt;")
        toolbar.addWidget(self._status_label)
        root.addLayout(toolbar)

        # ── Summary card ──────────────────────────────────────────────
        summary_card = QWidget()
        summary_card.setObjectName("test_summary")
        summary_card.setStyleSheet(
            f"QWidget#test_summary {{"
            f"  background-color:{BG_ELEVATED};"
            f"  border:1px solid {BORDER_SUBTLE};"
            f"  border-radius:6px;"
            f"}}"
        )
        summary_outer = QVBoxLayout(summary_card)
        summary_outer.setContentsMargins(14, 10, 14, 10)
        summary_outer.setSpacing(8)

        stats_row = QHBoxLayout()
        stats_row.setSpacing(20)

        self._lbl_total   = self._stat_widget("0", "Total",   TEXT_PRIMARY)
        self._lbl_pass    = self._stat_widget("0", "Passed",  GREEN)
        self._lbl_fail    = self._stat_widget("0", "Failed",  RED)
        self._lbl_error   = self._stat_widget("0", "Errors",  YELLOW)
        self._lbl_skip    = self._stat_widget("0", "Skipped", TEXT_SECONDARY)
        self._lbl_elapsed = self._stat_widget("\u2014", "Time", TEXT_PRIMARY)

        for val_lbl, key_lbl in (
            self._lbl_total, self._lbl_pass, self._lbl_fail,
            self._lbl_error, self._lbl_skip, self._lbl_elapsed,
        ):
            col = QVBoxLayout()
            col.setSpacing(0)
            col.addWidget(val_lbl, alignment=Qt.AlignmentFlag.AlignHCenter)
            col.addWidget(key_lbl, alignment=Qt.AlignmentFlag.AlignHCenter)
            stats_row.addLayout(col)

        stats_row.addStretch()
        summary_outer.addLayout(stats_row)

        self._progress = QProgressBar()
        self._progress.setRange(0, 100)
        self._progress.setValue(0)
        self._progress.setFixedHeight(6)
        self._progress.setTextVisible(False)
        self._progress.setStyleSheet(
            f"QProgressBar {{ background:{BG_DARK}; border:none; border-radius:3px; }}"
            f"QProgressBar::chunk {{ background:{ACCENT}; border-radius:3px; }}"
        )
        summary_outer.addWidget(self._progress)

        root.addWidget(summary_card)

        # ── Vertical splitter: tree (top) + detail (bottom) ───────────
        self._splitter = QSplitter(Qt.Orientation.Vertical)
        self._splitter.setChildrenCollapsible(False)

        # -- Test tree --
        tree_panel = QWidget()
        tree_panel.setObjectName("tree_card")
        tree_panel.setStyleSheet(
            f"QWidget#tree_card {{"
            f"  background-color:{BG_MEDIUM};"
            f"  border:1px solid {BORDER_SUBTLE};"
            f"  border-radius:6px;"
            f"}}"
        )
        tree_vbox = QVBoxLayout(tree_panel)
        tree_vbox.setContentsMargins(6, 6, 6, 6)
        tree_vbox.setSpacing(4)

        self._tree = QTreeWidget()
        self._tree.setColumnCount(2)
        self._tree.setHeaderLabels(["Test", "Result"])
        self._tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self._tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self._tree.setAlternatingRowColors(False)
        self._tree.setSelectionMode(QTreeWidget.SelectionMode.ExtendedSelection)
        self._tree.setFont(QFont("Segoe UI", 9))
        self._tree.setStyleSheet(
            f"QTreeWidget {{ border:none; background:{BG_DARK}; border-radius:4px; }}"
            f"QTreeWidget::item {{ padding:3px 0; }}"
            f"QTreeWidget::item:selected {{ background-color:{ACCENT}; color:#ffffff; }}"
        )
        self._tree.itemDoubleClicked.connect(self._on_item_double_clicked)
        self._tree.currentItemChanged.connect(self._on_tree_selection)
        tree_vbox.addWidget(self._tree)

        footer = QHBoxLayout()
        footer.setSpacing(4)
        for label, slot in [
            ("Select All",    lambda: self._tree.selectAll()),
            ("Select None",   lambda: self._tree.clearSelection()),
            ("Select Failed", self._select_failed),
        ]:
            b = QPushButton(label)
            b.setFixedHeight(24)
            b.setStyleSheet("font-size:8pt; padding:2px 8px;")
            b.clicked.connect(slot)
            footer.addWidget(b)

        self._refresh_btn = QPushButton("\u27f3  Refresh")
        self._refresh_btn.setFixedHeight(24)
        self._refresh_btn.setStyleSheet("font-size:8pt; padding:2px 8px;")
        self._refresh_btn.clicked.connect(self._discover_tests)
        footer.addWidget(self._refresh_btn)
        footer.addStretch()
        tree_vbox.addLayout(footer)

        self._splitter.addWidget(tree_panel)

        # -- Detail panel --
        detail_panel = QWidget()
        detail_panel.setObjectName("detail_card")
        detail_panel.setStyleSheet(
            f"QWidget#detail_card {{"
            f"  background-color:{BG_MEDIUM};"
            f"  border:1px solid {BORDER_SUBTLE};"
            f"  border-radius:6px;"
            f"}}"
        )
        detail_vbox = QVBoxLayout(detail_panel)
        detail_vbox.setContentsMargins(6, 6, 6, 6)
        detail_vbox.setSpacing(4)

        detail_header = QHBoxLayout()
        self._detail_title = QLabel("OUTPUT")
        self._detail_title.setStyleSheet(
            f"color:{TEXT_MUTED}; font-size:9pt; font-weight:bold;"
            "letter-spacing:0.5px; background:transparent;"
        )
        detail_header.addWidget(self._detail_title)
        detail_header.addStretch()
        copy_btn = QPushButton("Copy")
        copy_btn.setFixedHeight(22)
        copy_btn.setStyleSheet("font-size:8pt; padding:2px 10px;")
        copy_btn.clicked.connect(self._copy_detail)
        detail_header.addWidget(copy_btn)
        detail_vbox.addLayout(detail_header)

        self._detail_text = QPlainTextEdit()
        self._detail_text.setReadOnly(True)
        self._detail_text.setFont(QFont("Consolas", 9))
        self._detail_text.setStyleSheet(
            f"QPlainTextEdit {{"
            f"  border:none; background:{BG_DEEPEST};"
            f"  color:{TEXT_PRIMARY}; padding:8px;"
            f"  border-radius:4px;"
            f"  selection-background-color:{ACCENT};"
            f"}}"
        )
        self._detail_text.setPlaceholderText(
            "Select a test to see its output, or run tests to see results."
        )
        detail_vbox.addWidget(self._detail_text)
        self._splitter.addWidget(detail_panel)

        self._splitter.setSizes([450, 250])
        root.addWidget(self._splitter)

    def _stat_widget(self, value: str, label: str, color: str):
        val_lbl = QLabel(value)
        val_lbl.setStyleSheet(
            f"color:{color}; font-size:18pt; font-weight:bold; background:transparent;"
        )
        key_lbl = QLabel(label)
        key_lbl.setStyleSheet(f"color:{TEXT_MUTED}; font-size:8pt; background:transparent;")
        return val_lbl, key_lbl

    # ── Tree selection -> detail panel ────────────────────────────────────

    def _on_tree_selection(self, current, _previous):
        if current is None:
            self._detail_text.setPlainText("")
            self._detail_title.setText("OUTPUT")
            return

        if isinstance(current, TestItem):
            tid = current.test_id
            lines = self._test_output.get(tid, [])
            if lines:
                self._detail_title.setText(f"OUTPUT \u2014 {current.test_name}  ({current.status})")
                self._detail_text.setPlainText("\n".join(lines))
            elif current.status == "WAITING":
                self._detail_title.setText(f"OUTPUT \u2014 {current.test_name}")
                self._detail_text.setPlainText("Test has not been run yet.")
            elif current.status == "PASS":
                self._detail_title.setText(f"OUTPUT \u2014 {current.test_name}  (PASS)")
                self._detail_text.setPlainText("Test passed \u2014 no output captured.")
            else:
                self._detail_title.setText(f"OUTPUT \u2014 {current.test_name}")
                self._detail_text.setPlainText("")
        else:
            # Class-level item
            class_name = current.data(0, Qt.ItemDataRole.UserRole) or current.text(0).strip()
            all_lines = []
            for tid, lines in self._test_output.items():
                if tid.startswith(class_name + "::"):
                    if lines:
                        all_lines.append(f"\u2500\u2500 {tid.split('::')[-1]} \u2500\u2500")
                        all_lines.extend(lines)
                        all_lines.append("")
            self._detail_title.setText(f"OUTPUT \u2014 {class_name}")
            self._detail_text.setPlainText("\n".join(all_lines) if all_lines else "")

    def _copy_detail(self):
        text = self._detail_text.toPlainText()
        if text:
            from PyQt6.QtWidgets import QApplication
            QApplication.clipboard().setText(text)

    # ── Test discovery ────────────────────────────────────────────────────

    def _discover_tests(self):
        self._tree.clear()
        self._class_items.clear()
        self._test_items.clear()
        self._test_output.clear()
        self._all_output.clear()
        self._set_status("Discovering tests\u2026")
        worker = PytestWorker(self._tests_path)
        worker.discovered.connect(self._on_discovered)
        threading.Thread(target=worker.collect, daemon=True).start()

    def _on_discovered(self, test_ids: list):
        self._tree.clear()
        self._class_items.clear()
        self._test_items.clear()

        if not test_ids:
            test_ids = self._parse_test_file()

        for tid in test_ids:
            parts = tid.split("::")
            if len(parts) == 3:
                class_name, test_name = parts[1], parts[2]
            elif len(parts) == 2:
                class_name, test_name = parts[0], parts[1]
            else:
                continue

            if class_name not in self._class_items:
                ci = QTreeWidgetItem(self._tree)
                display = class_name[4:] if class_name.startswith("Test") else class_name
                ci.setText(0, f"  {display}")
                ci.setText(1, "")
                ci.setForeground(0, QColor(TEXT_SECONDARY))
                f = ci.font(0)
                f.setBold(True)
                f.setPointSize(10)
                ci.setFont(0, f)
                ci.setExpanded(True)
                ci.setData(0, Qt.ItemDataRole.UserRole, class_name)
                self._class_items[class_name] = ci

            ti = TestItem(class_name, test_name)
            self._class_items[class_name].addChild(ti)
            self._test_items[f"{class_name}::{test_name}"] = ti

        total = len(self._test_items)
        self._lbl_total[0].setText(str(total))
        self._set_status(f"Found {total} test(s)")
        self._reset_counters()

    def _parse_test_file(self) -> list:
        import ast
        path = Path(self._tests_path)
        if not path.exists():
            return []
        try:
            tree = ast.parse(path.read_text())
            ids = []
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
                    for item in node.body:
                        if isinstance(item, ast.FunctionDef) and item.name.startswith("test_"):
                            ids.append(f"tests/test_core.py::{node.name}::{item.name}")
            return ids
        except Exception:
            return []

    # ── Run controls ──────────────────────────────────────────────────────

    def _run_all(self):
        self._reset_all_statuses()
        self._start_run(test_ids=None)

    def _run_selected(self):
        selected = self._get_selected_test_ids()
        if not selected:
            self._set_status("No tests selected.")
            return
        self._reset_statuses(selected)
        self._start_run(test_ids=self._ids_to_pytest_args(selected))

    def _run_failed(self):
        failed = [tid for tid, item in self._test_items.items() if item.status in ("FAIL", "ERROR")]
        if not failed:
            self._set_status("No failed tests to re-run.")
            return
        self._reset_statuses(failed)
        self._start_run(test_ids=self._ids_to_pytest_args(failed))

    def _cancel(self):
        if self._worker:
            self._worker.cancel()
        self._set_status("Cancelling\u2026")

    def _start_run(self, test_ids: Optional[list]):
        if self._running:
            return
        self._running = True
        self._t_start = time.monotonic()
        self._test_output.clear()
        self._all_output.clear()
        self._current_test_id = ""
        self._detail_text.setPlainText("")
        self._progress.setValue(0)
        self._progress.setStyleSheet(
            f"QProgressBar {{ background:{BG_DARK}; border:none; border-radius:3px; }}"
            f"QProgressBar::chunk {{ background:{ACCENT}; border-radius:3px; }}"
        )
        self._set_status("Running\u2026")
        self._run_all_btn.setEnabled(False)
        self._run_sel_btn.setEnabled(False)
        self._run_fail_btn.setEnabled(False)
        self._cancel_btn.setEnabled(True)

        self._worker = PytestWorker(self._tests_path, test_ids)
        self._thread = QThread(self)
        self._worker.moveToThread(self._thread)
        self._worker.line_ready.connect(self._on_line)
        self._worker.result_ready.connect(self._on_result)
        self._worker.finished.connect(self._on_finished)
        self._thread.started.connect(self._worker.run)
        self._thread.start()

        self._elapsed_timer = QTimer(self)
        self._elapsed_timer.timeout.connect(self._tick_elapsed)
        self._elapsed_timer.start(200)

    # ── Signals from worker ───────────────────────────────────────────────

    def _on_line(self, line: str):
        """Capture output, associating lines with the current test."""
        self._all_output.append(line)

        stripped = line.strip()
        if not stripped:
            return
        # Skip pytest separator noise
        if stripped.startswith("=====") or stripped.startswith("_____"):
            return
        if stripped in ("PASSED", "FAILED", "short test summary info"):
            return

        m = RE_TEST_ID.match(line)
        if m:
            self._current_test_id = f"{m.group(2)}::{m.group(3)}"
            return

        if self._current_test_id:
            self._test_output.setdefault(self._current_test_id, []).append(line)

    def _on_result(self, test_id: str, status: str):
        self._current_test_id = test_id
        item = self._test_items.get(test_id)
        if item:
            item.set_status(status)
            self._tree.scrollToItem(item)
            self._update_class_status(item.class_name)

        self._refresh_summary_counts()

        done = sum(1 for i in self._test_items.values() if i.status not in ("WAITING", "RUNNING"))
        total = len(self._test_items)
        if total:
            self._progress.setValue(int(done / total * 100))

        # Live-update detail if this test is selected
        cur = self._tree.currentItem()
        if isinstance(cur, TestItem) and cur.test_id == test_id:
            self._on_tree_selection(cur, None)

    def _update_class_status(self, class_name: str):
        ci = self._class_items.get(class_name)
        if not ci:
            return
        statuses = set()
        for i in range(ci.childCount()):
            child = ci.child(i)
            if isinstance(child, TestItem):
                statuses.add(child.status)

        if "FAIL" in statuses or "ERROR" in statuses:
            ci.setText(1, "  FAIL  ")
            ci.setForeground(1, QColor(RED))
        elif "RUNNING" in statuses:
            ci.setText(1, "  \u2026  ")
            ci.setForeground(1, QColor(BLUE))
        elif statuses <= {"PASS", "SKIP"}:
            ci.setText(1, "  PASS  ")
            ci.setForeground(1, QColor(GREEN))
        else:
            ci.setText(1, "")

    def _on_finished(self, rc: int, elapsed: float):
        self._running = False
        self._elapsed_timer.stop()
        self._thread.quit()
        self._thread.wait()

        self._run_all_btn.setEnabled(True)
        self._run_sel_btn.setEnabled(True)
        self._run_fail_btn.setEnabled(True)
        self._cancel_btn.setEnabled(False)
        self._progress.setValue(100)

        self._refresh_summary_counts()
        self._lbl_elapsed[0].setText(f"{elapsed:.1f}s")

        fails  = sum(1 for i in self._test_items.values() if i.status == "FAIL")
        errors = sum(1 for i in self._test_items.values() if i.status == "ERROR")
        passed = sum(1 for i in self._test_items.values() if i.status == "PASS")

        if rc == 0:
            self._set_status(f"\u2714  All {passed} tests passed in {elapsed:.1f}s", GREEN)
            chunk_color = GREEN
        else:
            self._set_status(f"\u2718  {fails} failed, {errors} error(s) in {elapsed:.1f}s", RED)
            chunk_color = RED
        self._progress.setStyleSheet(
            f"QProgressBar {{ background:{BG_DARK}; border:none; border-radius:3px; }}"
            f"QProgressBar::chunk {{ background:{chunk_color}; border-radius:3px; }}"
        )

        # Auto-select first failed test
        for item in self._test_items.values():
            if item.status in ("FAIL", "ERROR"):
                self._tree.setCurrentItem(item)
                break

    # ── Helpers ───────────────────────────────────────────────────────────

    def _tick_elapsed(self):
        self._lbl_elapsed[0].setText(f"{time.monotonic() - self._t_start:.1f}s")

    def _refresh_summary_counts(self):
        counts = {"PASS": 0, "FAIL": 0, "ERROR": 0, "SKIP": 0}
        for item in self._test_items.values():
            if item.status in counts:
                counts[item.status] += 1
        self._lbl_pass[0].setText(str(counts["PASS"]))
        self._lbl_fail[0].setText(str(counts["FAIL"]))
        self._lbl_error[0].setText(str(counts["ERROR"]))
        self._lbl_skip[0].setText(str(counts["SKIP"]))

    def _reset_counters(self):
        for lbl, _ in (self._lbl_pass, self._lbl_fail, self._lbl_error, self._lbl_skip):
            lbl.setText("0")
        self._lbl_elapsed[0].setText("\u2014")
        self._progress.setValue(0)
        self._progress.setStyleSheet(
            f"QProgressBar {{ background:{BG_DARK}; border:none; border-radius:3px; }}"
            f"QProgressBar::chunk {{ background:{ACCENT}; border-radius:3px; }}"
        )

    def _reset_all_statuses(self):
        for item in self._test_items.values():
            item.set_status("WAITING")
        for ci in self._class_items.values():
            ci.setText(1, "")
        self._reset_counters()
        self._test_output.clear()
        self._all_output.clear()

    def _reset_statuses(self, test_ids: list):
        for tid in test_ids:
            short = "::".join(tid.split("::")[-2:])
            item = self._test_items.get(short)
            if item:
                item.set_status("WAITING")
                self._test_output.pop(short, None)

    def _get_selected_test_ids(self) -> list:
        return [item.test_id for item in self._tree.selectedItems() if isinstance(item, TestItem)]

    def _ids_to_pytest_args(self, short_ids: list) -> list:
        return [f"tests/test_core.py::{tid}" for tid in short_ids]

    def _select_failed(self):
        self._tree.clearSelection()
        for item in self._test_items.values():
            if item.status in ("FAIL", "ERROR"):
                item.setSelected(True)

    def _on_item_double_clicked(self, item, col):
        if isinstance(item, TestItem):
            self._reset_statuses([item.test_id])
            self._start_run(test_ids=self._ids_to_pytest_args([item.test_id]))

    def _set_status(self, text: str, color: str = TEXT_SECONDARY):
        self._status_label.setText(text)
        self._status_label.setStyleSheet(f"color:{color};font-size:10pt;")
