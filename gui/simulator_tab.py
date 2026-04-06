"""
gui/simulator_tab.py

VARA FM Simulator Tab — lets sysops test the BBS without radio hardware.

Each configured instance gets its own simulator panel. The panel:
  - Starts/stops a VARAFMSimulator bound to the instance's ports
  - Shows real-time BBS ↔ simulator traffic in a two-column console
  - Provides buttons for all simulatable VARA FM events
  - Allows typing arbitrary data as a remote station
  - Displays the simulator's internal state as a status bar

Design aesthetic: industrial operator console — dark background, monospace
traffic display, amber/green status indicators, military-style groupboxes.
"""

import sys
import time
import queue
import logging
import threading
from datetime import datetime
from typing import Optional

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QSplitter,
    QGroupBox, QLabel, QPushButton, QLineEdit,
    QTextEdit, QComboBox, QTabWidget, QFrame,
    QSizePolicy, QSpinBox, QCheckBox, QScrollArea,
)
from PyQt6.QtCore import Qt, QTimer, QMetaObject, Q_ARG, pyqtSignal, QObject, pyqtSlot
from PyQt6.QtGui import QFont, QColor, QTextCharFormat, QTextCursor

from core.config import AppConfig, InstanceConfig
from core.events import EventBus, EventType
from vara.instance_manager import VARAFMInstanceManager
from gui.theme import (
    BG_DEEPEST, BG_DARK, BG_MEDIUM, BG_ELEVATED, BG_HOVER, BORDER_SUBTLE, BORDER_NORMAL,
    TEXT_PRIMARY, TEXT_SECONDARY, TEXT_MUTED, TEXT_LINK,
    ACCENT, ACCENT_HOVER, GREEN, GREEN_DIM, RED, RED_DIM, BLUE, YELLOW,
    BTN_SUCCESS, BTN_DANGER, BTN_WARNING, BTN_ACCENT, BTN_NORMAL,
    IND_GREEN, IND_RED, IND_AMBER, IND_OFF,
)

log = logging.getLogger(__name__)

# ── Local aliases for backward compatibility within this module ───────────
BTN_GREEN  = BTN_SUCCESS
BTN_RED    = BTN_DANGER
BTN_AMBER  = BTN_WARNING
BTN_BLUE   = BTN_ACCENT
IND_ON     = IND_GREEN


def _ts() -> str:
    return datetime.now().strftime("%H:%M:%S.%f")[:12]


# ── Signal bridge for thread-safe GUI updates ─────────────────────────────────

class SimBridge(QObject):
    log_line   = pyqtSignal(str, str)   # text, colour hex
    state_changed = pyqtSignal(str)     # state name
    cmd_received  = pyqtSignal(str)     # command received from BBS app
    data_received = pyqtSignal(bytes)   # data received from BBS app


# ── Per-instance simulator panel ──────────────────────────────────────────────

class InstanceSimPanel(QWidget):
    """
    Full simulator panel for one VARA FM instance.
    Imports VARAFMSimulator lazily to avoid circular imports.
    """

    # Simulator states
    STATE_STOPPED    = "STOPPED"
    STATE_LISTENING  = "LISTENING"     # server bound, waiting for app to connect
    STATE_CONNECTED  = "CONNECTED"     # app TCP-connected, no RF session
    STATE_RF_SESSION = "RF SESSION"    # simulated RF connection active

    def __init__(self, inst_cfg: InstanceConfig, app_cfg: AppConfig,
                 im: "VARAFMInstanceManager", parent=None):
        super().__init__(parent)
        self.inst_cfg = inst_cfg
        self.app_cfg  = app_cfg
        self._im      = im

        self._sim     = None   # VARAFMSimulator instance
        self._state   = self.STATE_STOPPED
        self._bridge  = SimBridge(self)
        self._poll_timer = QTimer(self)

        # Listen for session disconnects so we can reset state when the BBS
        # ends a session naturally (e.g. user types BYE or session times out)
        self._im._bus.subscribe(EventType.SESSION_DISCONNECTED, self._on_session_disconnected_event)
        self._im._bus.subscribe(EventType.SESSION_CONNECTED,    self._on_session_connected_event)

        self._build_ui()
        self._wire_signals()

    # ── UI construction ───────────────────────────────────────────────────

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(6)
        root.setContentsMargins(8, 8, 8, 8)

        # ── Top bar: state + controls ─────────────────────────────────────
        top = QHBoxLayout()

        # State indicator cluster
        state_box = QGroupBox("Simulator State")
        state_layout = QHBoxLayout(state_box)
        state_layout.setSpacing(12)
        state_layout.setContentsMargins(8, 16, 8, 8)

        self._ind_server  = QLabel(); self._ind_server.setStyleSheet(IND_OFF)
        self._ind_client  = QLabel(); self._ind_client.setStyleSheet(IND_OFF)
        self._ind_session = QLabel(); self._ind_session.setStyleSheet(IND_OFF)
        self._ind_busy    = QLabel(); self._ind_busy.setStyleSheet(IND_OFF)
        self._ind_ptt     = QLabel(); self._ind_ptt.setStyleSheet(IND_OFF)

        for ind, label in [
            (self._ind_server,  "Server"),
            (self._ind_client,  "App Connected"),
            (self._ind_session, "RF Session"),
            (self._ind_busy,    "BUSY"),
            (self._ind_ptt,     "PTT"),
        ]:
            col = QVBoxLayout()
            col.setSpacing(2)
            col.addWidget(ind, alignment=Qt.AlignmentFlag.AlignHCenter)
            lbl = QLabel(label)
            lbl.setAlignment(Qt.AlignmentFlag.AlignHCenter)
            col.addWidget(lbl)
            state_layout.addLayout(col)

        self._state_label = QLabel("● STOPPED")
        self._state_label.setStyleSheet(f"color:{RED};font-weight:bold;font-size:10pt;")
        state_layout.addStretch()
        state_layout.addWidget(self._state_label)
        top.addWidget(state_box, stretch=3)

        # Start / Stop
        ctrl_box = QGroupBox("Simulator Control")
        ctrl_layout = QVBoxLayout(ctrl_box)
        ctrl_layout.setContentsMargins(8, 16, 8, 8)
        self._start_btn = QPushButton("▶  Start Simulator")
        self._stop_btn  = QPushButton("■  Stop Simulator")
        self._start_btn.setStyleSheet(BTN_GREEN)
        self._stop_btn.setStyleSheet(BTN_RED)
        self._stop_btn.setEnabled(False)
        self._start_btn.clicked.connect(self._start_sim)
        self._stop_btn.clicked.connect(self._stop_sim)
        ctrl_layout.addWidget(self._start_btn)
        ctrl_layout.addWidget(self._stop_btn)

        port_lbl = QLabel(
            f"Ports: CMD {self.inst_cfg.cmd_port} / DATA {self.inst_cfg.data_port}"
        )
        port_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        ctrl_layout.addWidget(port_lbl)
        top.addWidget(ctrl_box, stretch=1)
        root.addLayout(top)

        # ── Vertical splitter: walkthrough (top) + main area (bottom) ─────
        self._v_splitter = QSplitter(Qt.Orientation.Vertical)
        self._v_splitter.setChildrenCollapsible(False)

        # Top pane: collapsible loopback walkthrough
        wt = self._build_walkthrough()
        wt.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Preferred,
        )
        self._v_splitter.addWidget(wt)

        # Bottom pane: traffic console + RF controls side by side
        bottom_widget = QWidget()
        bottom_layout = QVBoxLayout(bottom_widget)
        bottom_layout.setContentsMargins(0, 0, 0, 0)
        bottom_layout.setSpacing(0)

        # ── Main area: traffic console + controls ─────────────────────────
        splitter = QSplitter(Qt.Orientation.Horizontal)

        # Left: traffic console
        console_box = QGroupBox("Traffic Console  (BBS App ↔ Simulator)")
        console_layout = QVBoxLayout(console_box)
        console_layout.setContentsMargins(8, 16, 8, 8)

        self._console = QTextEdit()
        self._console.setReadOnly(True)
        self._console.setFont(QFont("Consolas", 9))
        self._console.setMinimumHeight(100)
        console_layout.addWidget(self._console)

        clear_btn = QPushButton("Clear Console")
        clear_btn.clicked.connect(self._console.clear)
        clear_btn.setFixedWidth(120)
        console_layout.addWidget(clear_btn, alignment=Qt.AlignmentFlag.AlignRight)
        splitter.addWidget(console_box)

        # Right: RF event controls
        ctrl_scroll = QScrollArea()
        ctrl_scroll.setWidgetResizable(True)
        ctrl_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        ctrl_widget = QWidget()
        ctrl_col = QVBoxLayout(ctrl_widget)
        ctrl_col.setSpacing(8)

        # RF Connection group
        rf_box = QGroupBox("RF Connection")
        rf_layout = QVBoxLayout(rf_box)
        rf_layout.setContentsMargins(8, 16, 8, 8)

        call_row = QHBoxLayout()
        call_row.addWidget(QLabel("Remote call:"))
        self._remote_call_edit = QLineEdit("W9XYZ")
        self._remote_call_edit.setPlaceholderText("e.g. W9XYZ")
        call_row.addWidget(self._remote_call_edit)
        rf_layout.addLayout(call_row)

        self._connect_btn    = QPushButton("📡  Simulate CONNECT")
        self._disconnect_btn = QPushButton("✗   Simulate DISCONNECT")
        self._connect_btn.setStyleSheet(BTN_GREEN)
        self._disconnect_btn.setStyleSheet(BTN_RED)
        self._connect_btn.setEnabled(False)
        self._disconnect_btn.setEnabled(False)
        self._connect_btn.clicked.connect(self._sim_connect)
        self._disconnect_btn.clicked.connect(self._sim_disconnect)
        rf_layout.addWidget(self._connect_btn)
        rf_layout.addWidget(self._disconnect_btn)
        ctrl_col.addWidget(rf_box)

        # Channel events group
        ch_box = QGroupBox("Channel Events")
        ch_layout = QVBoxLayout(ch_box)
        ch_layout.setContentsMargins(8, 16, 8, 8)

        busy_row = QHBoxLayout()
        self._busy_btn = QPushButton("BUSY  OFF")
        self._busy_btn.setCheckable(True)
        self._busy_btn.setChecked(False)
        self._busy_btn.setStyleSheet(BTN_NORMAL)
        self._busy_btn.setEnabled(False)
        self._busy_btn.toggled.connect(self._on_busy_toggled)
        busy_row.addWidget(self._busy_btn)
        ch_layout.addLayout(busy_row)

        ptt_row = QHBoxLayout()
        self._ptt_btn = QPushButton("PTT  OFF")
        self._ptt_btn.setCheckable(True)
        self._ptt_btn.setChecked(False)
        self._ptt_btn.setStyleSheet(BTN_NORMAL)
        self._ptt_btn.setEnabled(False)
        self._ptt_btn.toggled.connect(self._on_ptt_toggled)
        ptt_row.addWidget(self._ptt_btn)
        ch_layout.addLayout(ptt_row)

        buf_spin_row = QHBoxLayout()
        buf_spin_row.addWidget(QLabel("Buffer bytes:"))
        self._buffer_spin = QSpinBox()
        self._buffer_spin.setRange(0, 65535)
        self._buffer_spin.setValue(0)
        self._buffer_spin.setEnabled(False)
        buf_spin_row.addWidget(self._buffer_spin)
        ch_layout.addLayout(buf_spin_row)
        buf_send = QPushButton("Send BUFFER")
        buf_send.setEnabled(False)
        buf_send.clicked.connect(
            lambda: self._send_event(f"BUFFER {self._buffer_spin.value()}")
        )
        self._buf_send_btn = buf_send
        ch_layout.addWidget(buf_send)
        ctrl_col.addWidget(ch_box)

        # Inject data group
        inject_box = QGroupBox("Inject Data  (simulates remote station typing)")
        inject_layout = QVBoxLayout(inject_box)
        inject_layout.setContentsMargins(8, 16, 8, 8)

        self._inject_edit = QLineEdit()
        self._inject_edit.setPlaceholderText("Type a BBS command, e.g.  H  or  LB")
        self._inject_edit.returnPressed.connect(self._inject_text)
        inject_layout.addWidget(self._inject_edit)

        inject_row = QHBoxLayout()
        inject_btn = QPushButton("⏎  Send Line")
        inject_btn.setStyleSheet(BTN_BLUE)
        inject_btn.setEnabled(False)
        inject_btn.clicked.connect(self._inject_text)
        self._inject_btn = inject_btn
        inject_row.addWidget(inject_btn)

        inject_b2f_btn = QPushButton("Send B2F Banner")
        inject_b2f_btn.setEnabled(False)
        inject_b2f_btn.setToolTip(
            "Injects a Winlink client banner to trigger B2F session"
        )
        inject_b2f_btn.clicked.connect(self._inject_b2f_banner)
        self._inject_b2f_btn = inject_b2f_btn
        inject_row.addWidget(inject_b2f_btn)
        inject_layout.addLayout(inject_row)

        inject_raw_row = QHBoxLayout()
        inject_raw_row.addWidget(QLabel("Raw hex:"))
        self._inject_hex = QLineEdit()
        self._inject_hex.setPlaceholderText("e.g. 48656c6c6f")
        inject_raw_row.addWidget(self._inject_hex)
        inject_hex_btn = QPushButton("Send")
        inject_hex_btn.setEnabled(False)
        inject_hex_btn.clicked.connect(self._inject_hex_data)
        self._inject_hex_btn = inject_hex_btn
        inject_raw_row.addWidget(inject_hex_btn)
        inject_layout.addLayout(inject_raw_row)

        # Command cheat-sheet
        cheat = QLabel(
            "<b>Personal:</b>  H  L  R&lt;n&gt;  S&lt;call&gt;  K&lt;n&gt;  U  N  I  B<br>"
            "<b>Bulletins:</b>  LB [area]  RB&lt;n&gt;  SB&lt;group&gt;  KB&lt;n&gt;  BA<br>"
            "<b>Groups:</b>  ALL  WX  DX  LOCAL  ARES  NTS  EMCOMM"
        )
        cheat.setTextFormat(Qt.TextFormat.RichText)
        cheat.setStyleSheet(
            f"color:{TEXT_MUTED}; font-size:8pt; padding-top:4px;"
        )
        cheat.setWordWrap(True)
        inject_layout.addWidget(cheat)
        ctrl_col.addWidget(inject_box)

        # Custom event group
        custom_box = QGroupBox("Custom VARA FM Event")
        custom_layout = QHBoxLayout(custom_box)
        custom_layout.setContentsMargins(8, 16, 8, 8)
        self._custom_event_edit = QLineEdit()
        self._custom_event_edit.setPlaceholderText("e.g. REGISTERED")
        custom_layout.addWidget(self._custom_event_edit)
        custom_send = QPushButton("Send")
        custom_send.setEnabled(False)
        custom_send.clicked.connect(self._send_custom_event)
        self._custom_send_btn = custom_send
        custom_layout.addWidget(custom_send)
        ctrl_col.addWidget(custom_box)

        # Scenario shortcuts
        scenario_box = QGroupBox("Quick Scenarios")
        scenario_layout = QVBoxLayout(scenario_box)
        scenario_layout.setContentsMargins(8, 16, 8, 8)
        scenarios = [
            ("Interactive BBS session",    self._scenario_interactive),
            ("Bulletin board session",      self._scenario_bulletins),
            ("Winlink B2F session",         self._scenario_b2f),
            ("Busy channel → connect",      self._scenario_busy_then_connect),
        ]
        self._scenario_btns_list = []
        for label, slot in scenarios:
            btn = QPushButton(label)
            btn.setEnabled(False)
            btn.clicked.connect(slot)
            setattr(self, f"_scenario_btn_{label[:8]}", btn)
            scenario_layout.addWidget(btn)
            self._scenario_btns_list.append(btn)
        ctrl_col.addWidget(scenario_box)

        ctrl_col.addStretch()
        ctrl_scroll.setWidget(ctrl_widget)
        ctrl_scroll.setMinimumWidth(300)
        ctrl_scroll.setMaximumWidth(400)
        splitter.addWidget(ctrl_scroll)

        splitter.setSizes([600, 340])
        splitter.setStretchFactor(0, 1)   # console: take all extra space
        splitter.setStretchFactor(1, 0)   # controls: fixed width, don't shrink
        bottom_layout.addWidget(splitter)
        self._v_splitter.addWidget(bottom_widget)

        # Walkthrough collapsed by default — give it minimal space, console gets the rest
        self._v_splitter.setSizes([30, 800])
        self._v_splitter.setStretchFactor(0, 0)   # walkthrough: don't stretch
        self._v_splitter.setStretchFactor(1, 1)   # console: take all extra space
        root.addWidget(self._v_splitter)

    # ── Loopback walkthrough panel ────────────────────────────────────────

    def _build_walkthrough(self) -> QWidget:
        """
        Collapsible step-by-step guide for running a loopback BBS test.
        Collapsed by default; expands when the user clicks the header.
        Each step highlights itself automatically as the simulator state advances.
        """
        outer = QFrame()
        outer.setStyleSheet(
            f"QFrame {{ background:{BG_DEEPEST}; border:1px solid {BORDER_SUBTLE};"
            " border-radius:4px; }"
        )
        outer_layout = QVBoxLayout(outer)
        outer_layout.setContentsMargins(0, 0, 0, 0)
        outer_layout.setSpacing(0)

        # ── Clickable header bar ──────────────────────────────────────────
        self._wt_toggle_btn = QPushButton(
            "▶   Loopback Test Walkthrough  —  click to expand"
        )
        self._wt_toggle_btn.setStyleSheet(
            "QPushButton {"
            f"  background:{BG_DEEPEST}; border:none; border-radius:0;"
            f"  color:{BLUE}; font-weight:bold; font-size:9pt;"
            "  padding:6px 12px; text-align:left;"
            "}"
            f"QPushButton:hover {{ background:{BG_HOVER}; }}"
        )
        self._wt_toggle_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._wt_toggle_btn.clicked.connect(self._toggle_walkthrough)
        outer_layout.addWidget(self._wt_toggle_btn)

        # ── Collapsible body ──────────────────────────────────────────────
        self._wt_body = QWidget()
        self._wt_body.setVisible(False)
        body_layout = QVBoxLayout(self._wt_body)
        body_layout.setContentsMargins(12, 8, 12, 10)
        body_layout.setSpacing(4)

        # Intro line
        intro = QLabel(
            "Follow these steps to run a complete loopback BBS test "
            "without any radio hardware. Steps highlight automatically as you progress."
        )
        intro.setWordWrap(True)
        intro.setStyleSheet(f"color:{TEXT_SECONDARY}; font-size:9pt; padding-bottom:6px;")
        body_layout.addWidget(intro)

        # Steps definition: (attr_name, number, title, detail, trigger_state)
        # trigger_state is the simulator STATE that causes this step to go green
        STEPS = [
            (
                "_wt_step1",
                "1",
                "Start the Simulator",
                "Click  ▶ Start Simulator  above. The Server LED turns amber — "
                "the simulator is now listening on the configured TCP ports.",
                self.STATE_LISTENING,
            ),
            (
                "_wt_step2",
                "2",
                "Start the VARA FM Instance",
                "Switch to the  📡 Dashboard  tab and click  Start  on this instance's "
                "card. The BBS app will connect to the simulator's TCP ports. "
                "The App Connected LED turns green when the handshake completes.",
                self.STATE_CONNECTED,
            ),
            (
                "_wt_step3",
                "3",
                "Simulate an Incoming RF Connection",
                "Return to this tab. Enter a test callsign (e.g. W9XYZ) in the "
                "Remote Call field, then click  📡 Simulate CONNECT. "
                "The BBS will send its welcome message — watch it appear in the "
                "Traffic Console on the left.",
                self.STATE_RF_SESSION,
            ),
            (
                "_wt_step4",
                "4",
                "Interact with the BBS",
                "Type BBS commands in the  Inject Data  field and press Enter. "
                "Personal messages: H (help), L (list), S W1ABC (send), I (info), N (new). "
                "Bulletins: LB (list bulletins), SB ALL (post to ALL group), "
                "BA (list areas), RB 1 (read bulletin 1).",
                None,   # manual step — no auto-trigger
            ),
            (
                "_wt_step5",
                "5",
                "Send a Test Message",
                "Type  S W1ABC  to send a message to W1ABC. "
                "Enter a subject and body when prompted, then type  .  on a line "
                "by itself to save it. Switch to  ✉️  Messages  to confirm it was stored.",
                None,
            ),
            (
                "_wt_step6",
                "6",
                "Test Winlink B2F (optional)",
                "First disconnect the current session (click ✗ Simulate DISCONNECT). "
                "Then click  Send B2F Banner  to simulate a Winlink Express connection, "
                "or use the  Winlink B2F session  Quick Scenario for a one-click version. "
                "The BBS will switch to B2F mode and exchange message proposals.",
                None,
            ),
            (
                "_wt_step7",
                "7",
                "Stop Simulator — Ready for RF",
                "Click  ✗ Stop Simulator  above. "
                "The BBS will reconnect to VARA FM and resume listening on the real radio. "
                "Check the  📋 Log  tab to confirm no errors were logged. "
                "Your instance is now verified and ready for real RF operation.",
                None,
            ),
        ]

        self._wt_step_widgets: list[tuple] = []   # (frame, number_lbl, title_lbl, detail_lbl, trigger_state)

        for attr, num, title, detail, trigger in STEPS:
            step_frame = QFrame()
            step_frame.setStyleSheet(
                f"QFrame {{ background:{BG_DEEPEST}; border:1px solid {BG_ELEVATED};"
                " border-radius:3px; margin:1px 0; }"
            )
            sf_layout = QHBoxLayout(step_frame)
            sf_layout.setContentsMargins(10, 6, 10, 6)
            sf_layout.setSpacing(10)

            # Step number badge
            num_lbl = QLabel(num)
            num_lbl.setFixedSize(22, 22)
            num_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            num_lbl.setStyleSheet(
                f"background:{BG_ELEVATED}; color:{TEXT_SECONDARY}; font-weight:bold;"
                " border-radius:11px; font-size:9pt;"
            )
            sf_layout.addWidget(num_lbl)

            # Text block
            text_col = QVBoxLayout()
            text_col.setSpacing(1)
            title_lbl = QLabel(title)
            title_lbl.setStyleSheet(
                f"color:{TEXT_PRIMARY}; font-weight:bold; font-size:9pt;"
            )
            detail_lbl = QLabel(detail)
            detail_lbl.setWordWrap(True)
            detail_lbl.setStyleSheet(f"color:{TEXT_SECONDARY}; font-size:8pt;")
            text_col.addWidget(title_lbl)
            text_col.addWidget(detail_lbl)
            sf_layout.addLayout(text_col, stretch=1)

            # Status tick
            tick_lbl = QLabel("○")
            tick_lbl.setFixedWidth(18)
            tick_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            tick_lbl.setStyleSheet(f"color:{TEXT_MUTED}; font-size:12pt;")
            sf_layout.addWidget(tick_lbl)

            body_layout.addWidget(step_frame)
            setattr(self, attr, step_frame)
            self._wt_step_widgets.append(
                (step_frame, num_lbl, title_lbl, tick_lbl, trigger)
            )

        # Tip footer
        tip = QLabel(
            "💡  Tip:  Use  Interactive BBS session  or  Bulletin board session  "
            "in Quick Scenarios to run steps 3–4 automatically."
        )
        tip.setWordWrap(True)
        tip.setStyleSheet(
            f"color:{BLUE}; font-size:8pt; padding-top:6px;"
        )
        body_layout.addWidget(tip)

        outer_layout.addWidget(self._wt_body)
        return outer

    def _toggle_walkthrough(self):
        visible = self._wt_body.isVisible()
        self._wt_body.setVisible(not visible)
        arrow = "▼" if not visible else "▶"
        label = "collapse" if not visible else "expand"
        self._wt_toggle_btn.setText(
            f"{arrow}   Loopback Test Walkthrough  —  click to {label}"
        )
        # Resize splitter: expanded ~280px for walkthrough, collapsed ~30px
        total = self._v_splitter.height()
        if not visible:
            # Expanding — give walkthrough 280px, rest to console
            self._v_splitter.setSizes([280, max(100, total - 280)])
        else:
            # Collapsing — shrink walkthrough to header only
            self._v_splitter.setSizes([30, max(100, total - 30)])

    def _mark_step_complete(self, step_index: int):
        """Mark a specific step (0-based) as complete regardless of trigger."""
        if step_index >= len(self._wt_step_widgets):
            return
        frame, num_lbl, title_lbl, tick_lbl, _ = self._wt_step_widgets[step_index]
        frame.setStyleSheet(
            f"QFrame {{ background:{BG_DEEPEST}; border:1px solid {GREEN};"
            " border-radius:3px; margin:1px 0; }"
        )
        num_lbl.setStyleSheet(
            f"background:{GREEN}; color:white; font-weight:bold;"
            " border-radius:11px; font-size:9pt;"
        )
        tick_lbl.setText("✔")
        tick_lbl.setStyleSheet(f"color:{GREEN}; font-size:12pt;")

    def _update_walkthrough_steps(self, state: str):
        """
        Called whenever the simulator state changes.
        Steps whose trigger_state matches or has been passed are marked complete.
        """
        state_order = {
            self.STATE_STOPPED:    0,
            self.STATE_LISTENING:  1,
            self.STATE_CONNECTED:  2,
            self.STATE_RF_SESSION: 3,
        }
        current_order = state_order.get(state, 0)

        for frame, num_lbl, title_lbl, tick_lbl, trigger in self._wt_step_widgets:
            if trigger is None:
                continue   # manual steps keep their current style
            trigger_order = state_order.get(trigger, 99)
            if current_order >= trigger_order:
                # Completed
                frame.setStyleSheet(
                    f"QFrame {{ background:{BG_DEEPEST}; border:1px solid {GREEN};"
                    " border-radius:3px; margin:1px 0; }"
                )
                num_lbl.setStyleSheet(
                    f"background:{GREEN}; color:white; font-weight:bold;"
                    " border-radius:11px; font-size:9pt;"
                )
                tick_lbl.setText("✔")
                tick_lbl.setStyleSheet(f"color:{GREEN}; font-size:12pt;")
            else:
                # Pending
                frame.setStyleSheet(
                    f"QFrame {{ background:{BG_DEEPEST}; border:1px solid {BG_ELEVATED};"
                    " border-radius:3px; margin:1px 0; }"
                )
                num_lbl.setStyleSheet(
                    f"background:{BG_ELEVATED}; color:{TEXT_SECONDARY}; font-weight:bold;"
                    " border-radius:11px; font-size:9pt;"
                )
                tick_lbl.setText("○")
                tick_lbl.setStyleSheet(f"color:{TEXT_MUTED}; font-size:12pt;")

    # ── Signal wiring ─────────────────────────────────────────────────────

    def _wire_signals(self):
        self._bridge.log_line.connect(self._append_console)
        self._bridge.state_changed.connect(self._on_state_changed)
        self._bridge.cmd_received.connect(self._on_cmd_received)
        self._bridge.data_received.connect(self._on_data_received)

        # Poll for received commands/data every 250ms
        self._poll_timer.timeout.connect(self._poll_sim)
        self._poll_timer.start(250)

    # ── Simulator lifecycle ───────────────────────────────────────────────

    def _start_sim(self):
        from tests.vara_simulator import VARAFMSimulator

        if self._sim:
            return

        # Stop the real VARA FM instance (if running) so the simulator can
        # bind the same ports. This is the expected workflow for loopback testing.
        mux = self._im.get_mux(self.inst_cfg.name)
        if mux is not None:
            from vara.interface import VARAFMState
            if mux.state != VARAFMState.DISCONNECTED:
                self._log("[SIM] Stopping VARA FM instance to free ports...", TEXT_SECONDARY)
                self._im.stop_instance(self.inst_cfg.name)
                # Brief pause for ports to be released
                import time; time.sleep(1.0)

        try:
            self._sim = VARAFMSimulator(
                cmd_port=self.inst_cfg.cmd_port,
                data_port=self.inst_cfg.data_port,
            )
            self._sim.start()
        except OSError as e:
            self._log(f"[ERROR] Could not start simulator: {e}", RED)
            self._sim = None
            return

        self._set_state(self.STATE_LISTENING)
        self._log(
            f"[SIM] Simulator started on CMD:{self.inst_cfg.cmd_port} "
            f"DATA:{self.inst_cfg.data_port}",
            f"{GREEN}"
        )
        self._log(
            "[SIM] Waiting for BBS app to connect "
            "(start the instance from the Dashboard tab)...",
            f"{TEXT_SECONDARY}"
        )
        # Tell the dashboard to show a blue "SIMULATOR" indicator
        from core.events import EventType
        self.app_cfg  # access to get event_bus — pass through inst_cfg name
        # We need the event bus — get it via the instance manager
        try:
            self._im._bus.emit(EventType.SIMULATOR_ACTIVE, source=self.inst_cfg.name)
        except Exception:
            pass

        self._start_btn.setEnabled(False)
        self._stop_btn.setEnabled(True)

        # Watch for app connection
        threading.Thread(
            target=self._wait_for_app_connect,
            daemon=True,
        ).start()

    def _stop_sim(self):
        if self._sim:
            self._sim.stop()
            self._sim = None
        self._set_state(self.STATE_STOPPED)
        self._log("[SIM] Simulator stopped.", TEXT_SECONDARY)
        self._mark_step_complete(6)  # step 7: simulator stopped = walkthrough done
        self._start_btn.setEnabled(True)
        self._stop_btn.setEnabled(False)
        self._set_rf_buttons_enabled(False)
        self._last_cmd_count    = 0
        self._last_data_len     = 0
        self._last_tx_len       = 0
        self._last_bbs_resp_len = 0
        self._wt_step4_done        = False
        self._wt_step5_done        = False
        self._wt_b2f_session_active = False
        # Reset toggle buttons
        self._busy_btn.blockSignals(True)
        self._busy_btn.setChecked(False)
        self._busy_btn.setText("BUSY  OFF")
        self._busy_btn.setStyleSheet(BTN_NORMAL)
        self._busy_btn.blockSignals(False)
        self._ptt_btn.blockSignals(True)
        self._ptt_btn.setChecked(False)
        self._ptt_btn.setText("PTT  OFF")
        self._ptt_btn.setStyleSheet(BTN_NORMAL)
        self._ptt_btn.blockSignals(False)
        try:
            from core.events import EventType
            self._im._bus.emit(EventType.SIMULATOR_INACTIVE, source=self.inst_cfg.name)
        except Exception:
            pass

    def _wait_for_app_connect(self):
        """Background: block until the BBS app connects, then update state."""
        if self._sim and self._sim.wait_for_client(timeout=60.0):
            self._bridge.state_changed.emit(self.STATE_CONNECTED)
            self._bridge.log_line.emit(
                "[SIM] BBS app connected to simulator TCP ports.",
                f"{GREEN}"
            )
        else:
            self._bridge.log_line.emit(
                "[SIM] Timed out waiting for BBS app to connect.",
                f"{YELLOW}"
            )

    # ── RF simulation actions ─────────────────────────────────────────────

    def _on_session_disconnected_event(self, event):
        """Called from event bus (background thread) when BBS session ends naturally."""
        if event.source != self.inst_cfg.name:
            return
        if self._state == self.STATE_RF_SESSION:
            # Update self._state here (background thread write is safe for this
            # simple assignment) so subsequent checks in this method are correct,
            # then drive the full UI update via _set_state on the main thread.
            self._state = self.STATE_CONNECTED
            QMetaObject.invokeMethod(self, "_set_state",
                Qt.ConnectionType.QueuedConnection,
                Q_ARG(str, self.STATE_CONNECTED))
            self._bridge.log_line.emit("[RF]  ──► DISCONNECTED (session ended)", RED)
            # Mark step 6 complete if this was a B2F session
            if getattr(self, "_wt_b2f_session_active", False):
                self._wt_b2f_session_active = False
                QMetaObject.invokeMethod(self, "_mark_step6_complete",
                    Qt.ConnectionType.QueuedConnection)

    @pyqtSlot()
    def _mark_step6_complete(self):
        self._mark_step_complete(5)  # step 6 is index 5

    def _on_session_connected_event(self, event):
        """Called from event bus (background thread) when BBS accepts a session."""
        if event.source != self.inst_cfg.name:
            return
        if self._state != self.STATE_RF_SESSION:
            self._state = self.STATE_RF_SESSION
            QMetaObject.invokeMethod(self, "_set_state",
                Qt.ConnectionType.QueuedConnection,
                Q_ARG(str, self.STATE_RF_SESSION))

    @pyqtSlot()
    def _sim_connect(self):
        call = self._remote_call_edit.text().strip().upper() or "W9XYZ"
        mycall = self.app_cfg.station.bbs_callsign
        if self._sim:
            self._sim.simulate_connect(call, mycall)
            self._log(f"[RF]  ──► CONNECTED {mycall} {call}", BLUE)
            # Don't set STATE_RF_SESSION here — wait for SESSION_CONNECTED event
            # from the bus, which confirms the BBS actually acquired the session.
            # If the session lock is still held the connect will be ignored and
            # we never get stuck in STATE_RF_SESSION with no way back.

    def _sim_disconnect(self):
        if self._sim:
            self._sim.simulate_disconnect()
            self._log("[RF]  ──► DISCONNECTED", RED)
            self._set_state(self.STATE_CONNECTED)

    def _on_busy_toggled(self, checked: bool):
        if checked:
            self._busy_btn.setText("BUSY  ON")
            self._busy_btn.setStyleSheet(BTN_AMBER)
            self._send_event("BUSY TRUE")
        else:
            self._busy_btn.setText("BUSY  OFF")
            self._busy_btn.setStyleSheet(BTN_NORMAL)
            self._send_event("BUSY FALSE")

    def _on_ptt_toggled(self, checked: bool):
        if checked:
            self._ptt_btn.setText("PTT  ON")
            self._ptt_btn.setStyleSheet(BTN_RED)
            self._send_event("PTT TRUE")
        else:
            self._ptt_btn.setText("PTT  OFF")
            self._ptt_btn.setStyleSheet(BTN_NORMAL)
            self._send_event("PTT FALSE")

    def _send_event(self, event: str):
        if self._sim:
            self._sim.send_event(event)
            self._log(f"[EVT] ──► {event}", YELLOW)
            # Update indicators
            if "BUSY TRUE"  in event: self._ind_busy.setStyleSheet(IND_AMBER)
            if "BUSY FALSE" in event: self._ind_busy.setStyleSheet(IND_OFF)
            if "PTT TRUE"   in event: self._ind_ptt.setStyleSheet(IND_RED)
            if "PTT FALSE"  in event: self._ind_ptt.setStyleSheet(IND_OFF)

    def _inject_text(self):
        text = self._inject_edit.text()
        if not text or not self._sim:
            return
        data = (text + "\r\n").encode("utf-8", errors="replace")
        self._sim.inject_data(data)
        self._log(f"[RX]  ◄── {text!r}", "#58A6FF")
        self._inject_edit.clear()

    def _inject_b2f_banner(self):
        call = self._remote_call_edit.text().strip().upper() or "W9XYZ"
        banner = f"[{call}-3.0-B2FWISP-2.4.1.0]\r\n"
        if self._sim:
            self._wt_b2f_session_active = True
            if self._state != self.STATE_RF_SESSION:
                # Must have an active RF session before injecting data.
                # Simulate a CONNECT first, then inject after a short delay.
                self._sim_connect()

                def _delayed_inject():
                    import time
                    time.sleep(0.8)
                    self._sim.inject_data(banner.encode("ascii"))
                    self._bridge.log_line.emit(f"[RX]  ◄── {banner.strip()}", "#58A6FF")

                import threading
                threading.Thread(target=_delayed_inject, daemon=True).start()
            else:
                self._sim.inject_data(banner.encode("ascii"))
                self._log(f"[RX]  ◄── {banner.strip()}", "#58A6FF")

    def _inject_hex_data(self):
        hex_str = self._inject_hex.text().strip().replace(" ", "")
        if not hex_str or not self._sim:
            return
        try:
            data = bytes.fromhex(hex_str)
            self._sim.inject_data(data)
            self._log(f"[RX]  ◄── hex:{hex_str}", "#58A6FF")
            self._inject_hex.clear()
        except ValueError:
            self._log("[ERROR] Invalid hex string", RED)

    def _send_custom_event(self):
        event = self._custom_event_edit.text().strip()
        if event:
            self._send_event(event)
            self._custom_event_edit.clear()

    # ── Quick scenario runners ─────────────────────────────────────────────

    def _scenario_interactive(self):
        """Simulate a complete interactive BBS session."""
        call = self._remote_call_edit.text().strip().upper() or "W9XYZ"
        self._log(f"[SCENARIO] Starting interactive BBS session as {call}", YELLOW)

        def _run():
            time.sleep(0.5)
            self._bridge.log_line.emit("[SCENARIO] Simulating RF connect...", TEXT_SECONDARY)
            QMetaObject.invokeMethod(self, "_sim_connect",
                                     Qt.ConnectionType.QueuedConnection)
            time.sleep(2.0)   # wait for BBS welcome

            for cmd in ["H", "L", "I", "N", "B"]:
                time.sleep(1.5)
                self._bridge.log_line.emit(
                    f"[SCENARIO] Injecting command: {cmd}", f"{TEXT_SECONDARY}"
                )
                if self._sim:
                    self._sim.inject_data((cmd + "\r\n").encode())

        threading.Thread(target=_run, daemon=True).start()

    def _scenario_bulletins(self):
        """
        Simulate a bulletin board session:
          connect → BA (list areas) → LB (list bulletins) → SB ALL (post bulletin)
          → subject → body → . → LB (verify) → B (bye)
        """
        call = self._remote_call_edit.text().strip().upper() or "W9XYZ"
        self._log(f"[SCENARIO] Starting bulletin board session as {call}", YELLOW)

        def _run():
            time.sleep(0.5)
            self._bridge.log_line.emit(
                "[SCENARIO] Connecting and testing bulletin commands...", f"{TEXT_SECONDARY}"
            )
            QMetaObject.invokeMethod(self, "_sim_connect",
                                     Qt.ConnectionType.QueuedConnection)
            time.sleep(2.0)

            steps = [
                ("BA",                    "List bulletin areas"),
                ("LB",                    "List all bulletins"),
                ("LB ALL",                "List ALL area bulletins"),
                ("SB ALL",                "Start sending bulletin to ALL"),
                ("Simulator Test Bulletin","Enter subject"),
                ("This is a test bulletin posted via the simulator loopback test.\r\n"
                 "73 de BBS Simulator.",  "Enter body"),
                (".",                     "End body (save bulletin)"),
                ("LB",                    "Verify bulletin appears in list"),
                ("B",                     "Disconnect"),
            ]

            for cmd, hint in steps:
                time.sleep(1.8)
                self._bridge.log_line.emit(
                    f"[SCENARIO] {hint}: {cmd!r}", f"{TEXT_SECONDARY}"
                )
                if self._sim:
                    self._sim.inject_data((cmd + "\r\n").encode())

        threading.Thread(target=_run, daemon=True).start()

    def _scenario_b2f(self):
        """Simulate a Winlink B2F session."""
        call = self._remote_call_edit.text().strip().upper() or "W9XYZ"
        self._log(f"[SCENARIO] Starting B2F session as {call}", YELLOW)

        def _run():
            time.sleep(0.5)
            QMetaObject.invokeMethod(self, "_sim_connect",
                                     Qt.ConnectionType.QueuedConnection)
            time.sleep(1.0)
            banner = f"[{call}-3.0-B2FWISP-2.4.1.0]\r\n"
            if self._sim:
                self._wt_b2f_session_active = True
                self._sim.inject_data(banner.encode("ascii"))
                self._bridge.log_line.emit(
                    f"[SCENARIO] Sent B2F banner: {banner.strip()}", f"{TEXT_SECONDARY}"
                )

        threading.Thread(target=_run, daemon=True).start()

    def _scenario_busy_then_connect(self):
        """Simulate channel busy, then clear and connect."""
        self._log("[SCENARIO] Channel busy for 3s, then clears and connects", YELLOW)

        def _run():
            self._send_event("BUSY TRUE")
            time.sleep(3.0)
            self._send_event("BUSY FALSE")
            time.sleep(1.0)
            QMetaObject.invokeMethod(self, "_sim_connect",
                                     Qt.ConnectionType.QueuedConnection)

        threading.Thread(target=_run, daemon=True).start()

    # ── Polling — show BBS→simulator traffic ──────────────────────────────

    def _poll_sim(self):
        """Called by QTimer to drain received command/data logs."""
        if not self._sim:
            return

        # Drain new commands (BBS app → VARA FM cmd channel)
        cmds = self._sim.get_received_commands()
        if len(cmds) > getattr(self, "_last_cmd_count", 0):
            new_cmds = cmds[getattr(self, "_last_cmd_count", 0):]
            for cmd in new_cmds:
                self._bridge.cmd_received.emit(cmd)
            self._last_cmd_count = len(cmds)

        # Drain data FROM remote station → BBS (injected via inject_data)
        all_rx = self._sim.get_received_data()
        if len(all_rx) > getattr(self, "_last_data_len", 0):
            new_data = all_rx[getattr(self, "_last_data_len", 0):]
            self._bridge.data_received.emit(new_data)
            self._last_data_len = len(all_rx)

        # Drain BBS responses → remote station (welcome msg, prompts, etc.)
        all_tx = self._sim.get_sent_data()
        if len(all_tx) > getattr(self, "_last_tx_len", 0):
            new_tx = all_tx[getattr(self, "_last_tx_len", 0):]
            self._bridge.log_line.emit(
                self._format_bbs_response(new_tx), "#E8D44D"
            )
            self._last_tx_len = len(all_tx)

            # Step 4: first BBS response after RF session = user is interacting
            if self._state == self.STATE_RF_SESSION:
                if not getattr(self, "_wt_step4_done", False):
                    self._wt_step4_done = True
                    self._mark_step_complete(3)  # step 4 is index 3

        # Step 5: "Message saved." in BBS→remote data = a message was stored
        # This travels on the data socket (get_received_data), not get_sent_data
        all_rx_data = self._sim.get_received_data()
        if len(all_rx_data) > getattr(self, "_last_bbs_resp_len", 0):
            new_bbs = all_rx_data[getattr(self, "_last_bbs_resp_len", 0):]
            self._last_bbs_resp_len = len(all_rx_data)
            if b"Message saved" in new_bbs or b"Bulletin saved" in new_bbs:
                if not getattr(self, "_wt_step5_done", False):
                    self._wt_step5_done = True
                    self._mark_step_complete(4)  # step 5 is index 4

        # Check if app has connected
        if (self._state == self.STATE_LISTENING and
                self._sim._cmd_client is not None):
            self._bridge.state_changed.emit(self.STATE_CONNECTED)

    # ── Console output ────────────────────────────────────────────────────

    def _format_bbs_response(self, data: bytes) -> str:
        """Format BBS response bytes for console display."""
        try:
            text = data.decode("utf-8", errors="replace")
        except Exception:
            text = repr(data)
        # Show each non-empty line prefixed with BBS arrow
        lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        out = []
        for line in lines:
            if line.strip():
                out.append(f"{_ts()}  [BBS] ──► {line}")
            elif out:
                # Preserve blank lines between content but not leading blanks
                out.append("")
        return "\n".join(out)

    def _log(self, text: str, color: str = TEXT_PRIMARY):
        self._bridge.log_line.emit(f"{_ts()}  {text}", color)

    def _append_console(self, text: str, color: str):
        cursor = self._console.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        fmt = QTextCharFormat()
        fmt.setForeground(QColor(color))
        cursor.insertText(text + "\n", fmt)
        # Auto-scroll
        self._console.setTextCursor(cursor)
        self._console.ensureCursorVisible()

    def _on_cmd_received(self, cmd: str):
        """BBS app sent a VARA FM command."""
        self._append_console(f"{_ts()}  [CMD] ◄── {cmd}", GREEN)

    def _on_data_received(self, data: bytes):
        """BBS app sent data on the data channel."""
        try:
            text = data.decode("utf-8", errors="replace")
        except Exception:
            text = repr(data)
        # Truncate very long payloads in the display
        display = text[:200] + ("…" if len(text) > 200 else "")
        display = display.replace("\r", "").replace("\n", "↵")
        self._append_console(f"{_ts()}  [DATA]◄── {display!r}", "#79C0FF")

    # ── State management ──────────────────────────────────────────────────

    @pyqtSlot(str)
    def _set_state(self, state: str):
        self._state = state
        self._bridge.state_changed.emit(state)

    def _on_state_changed(self, state: str):
        colors = {
            self.STATE_STOPPED:    (f"{RED}",  IND_OFF,   IND_OFF,   IND_OFF),
            self.STATE_LISTENING:  (f"{YELLOW}",  IND_AMBER, IND_OFF,   IND_OFF),
            self.STATE_CONNECTED:  (f"{GREEN}",  IND_ON,    IND_ON,    IND_OFF),
            self.STATE_RF_SESSION: (f"{BLUE}",  IND_ON,    IND_ON,    IND_ON),
        }
        color, srv, cli, sess = colors.get(
            state, (f"{TEXT_PRIMARY}", IND_OFF, IND_OFF, IND_OFF)
        )
        self._state_label.setText(f"● {state}")
        self._state_label.setStyleSheet(
            f"color:{color};font-weight:bold;font-size:10pt;"
        )
        self._ind_server.setStyleSheet(srv)
        self._ind_client.setStyleSheet(cli)
        self._ind_session.setStyleSheet(sess)

        connected = state in (self.STATE_CONNECTED, self.STATE_RF_SESSION)
        rf_active = state == self.STATE_RF_SESSION

        self._connect_btn.setEnabled(connected and not rf_active)
        self._disconnect_btn.setEnabled(rf_active)
        self._busy_btn.setEnabled(connected)
        self._ptt_btn.setEnabled(connected)
        self._buffer_spin.setEnabled(connected)
        self._buf_send_btn.setEnabled(connected)
        self._custom_send_btn.setEnabled(connected)
        self._inject_btn.setEnabled(rf_active)
        self._inject_b2f_btn.setEnabled(connected)   # can initiate B2F before or during session
        self._inject_hex_btn.setEnabled(rf_active)
        # Scenarios start a new connect — only valid when connected but no active session
        for btn in getattr(self, "_scenario_btns_list", []):
            btn.setEnabled(connected and not rf_active)

        # Advance walkthrough step highlights
        self._update_walkthrough_steps(state)

    def _set_rf_buttons_enabled(self, enabled: bool):
        for btn in [
            self._connect_btn, self._disconnect_btn,
            self._busy_btn, self._ptt_btn,
            self._inject_btn, self._inject_b2f_btn,
            self._inject_hex_btn, self._custom_send_btn,
        ]:
            btn.setEnabled(enabled)
        self._buffer_spin.setEnabled(enabled)
        self._buf_send_btn.setEnabled(enabled)


# ── Top-level simulator tab (one sub-tab per instance) ───────────────────────

class SimulatorTab(QWidget):
    """
    Top-level tab housing one InstanceSimPanel per configured VARA FM instance.
    If there is only one instance, shows the panel directly (no sub-tabs).
    Always shows the loopback walkthrough panel at the top.
    """

    def __init__(
        self,
        config: AppConfig,
        im: VARAFMInstanceManager,
        parent=None,
    ):
        super().__init__(parent)
        self._config  = config
        self._im      = im
        self._panels: dict[str, InstanceSimPanel] = {}
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(6)

        enabled = [c for c in self._config.instances if c.enabled]

        if not enabled:
            lbl = QLabel("No enabled VARA FM instances configured.")
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lbl.setStyleSheet(f"color:{TEXT_SECONDARY};font-size:11pt;")
            layout.addWidget(lbl)
            return

        if len(enabled) == 1:
            panel = InstanceSimPanel(enabled[0], self._config, self._im, parent=self)
            self._panels[enabled[0].name] = panel
            layout.addWidget(panel)
        else:
            tabs = QTabWidget()
            for inst_cfg in enabled:
                panel = InstanceSimPanel(inst_cfg, self._config, self._im, parent=self)
                self._panels[inst_cfg.name] = panel
                tabs.addTab(panel, inst_cfg.name)
            layout.addWidget(tabs)

    def get_panel(self, instance_name: str) -> Optional[InstanceSimPanel]:
        return self._panels.get(instance_name)
