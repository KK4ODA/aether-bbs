"""
gui/modem_panel.py

Modem Status Panel (VARA FM/HF, Soundmodem and Aether HF instances).
Displays one status card per configured instance showing:
  process state, TCP state, PTT, BUSY, current session, traffic, link
  quality (SN / BITRATE), and for Aether HF the modem's own status.
"""

import json
import logging
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGroupBox,
    QLabel, QPushButton, QScrollArea, QTableWidget, QTableWidgetItem, QHeaderView,
    QInputDialog
)
from PyQt6.QtCore import Qt, QTimer, pyqtSlot

from core.config import AppConfig
from core.events import EventBus, EventType
from vara.instance_manager import ModemInstanceManager
from gui.theme import STATE_COLORS
from gui.aether_heard import AetherHeardPanel

log = logging.getLogger(__name__)


def _indicator(color: str, size: int = 14) -> QLabel:
    lbl = QLabel()
    lbl.setFixedSize(size, size)
    lbl.setStyleSheet(
        f"background-color:{color};border-radius:{size//2}px;"
    )
    return lbl


class InstanceCard(QGroupBox):
    """One status card for one modem instance."""

    def __init__(self, instance_name: str, im: ModemInstanceManager,
                 inst_cfg=None, parent=None):
        super().__init__(instance_name, parent)
        self._name = instance_name
        self._im   = im
        self._cfg  = inst_cfg  # use fresh config if provided
        self._bytes_tx = 0
        self._bytes_rx = 0
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(2)
        layout.setContentsMargins(4, 4, 4, 4)

        # Prefer fresh config passed at construction; fall back to instance manager
        if self._cfg:
            freq = self._cfg.frequency_label
            transport = getattr(self._cfg, "transport", "VARA_FM")
        else:
            status = self._im.get_status(self._name)
            freq      = status.cfg.frequency_label if status else ""
            transport  = getattr(status.cfg, "transport", "VARA_FM") if status else "VARA_FM"

        # Short badge label from transport
        badge_map = {"VARA_FM": "FM", "VARA_HF": "HF",
                     "SOUNDMODEM": "AGWPE", "AETHER_HF": "AETHER"}
        mode = badge_map.get(transport, transport)

        # Header row: frequency label + transport badge
        header_row = QHBoxLayout()
        self._freq_lbl = QLabel(freq or "—")
        self._freq_lbl.setStyleSheet("font-weight:bold;font-size:10pt;")
        self._static_freq = freq or "—"  # config value to restore when scan stops
        mode_badge = QLabel(f" {mode} ")
        badge_color = {"FM": "#1a6b3a", "HF": "#1a3f6b",
                       "AGWPE": "#6b3a1a", "AETHER": "#3a1a6b"}.get(mode, "#444")
        mode_badge.setStyleSheet(
            f"background:{badge_color};color:white;border-radius:3px;"
            "font-weight:bold;font-size:9pt;padding:1px 4px;"
        )
        header_row.addWidget(self._freq_lbl)
        header_row.addStretch()
        header_row.addWidget(mode_badge)
        layout.addLayout(header_row)

        # Indicator row
        ind_layout = QHBoxLayout()

        self._proc_ind   = _indicator(STATE_COLORS["stopped"])
        self._tcp_ind    = _indicator(STATE_COLORS["disconnected"])
        self._ptt_ind    = _indicator(STATE_COLORS["stopped"])
        self._ptt_ind.setToolTip(
            "PTT — lights red when the modem keys the transmitter.\n"
            "VARA FM and Aether HF key the radio themselves (set up in\n"
            "the modem); VARA HF is keyed by the PTT method set here."
        )
        self._busy_ind   = _indicator(STATE_COLORS["stopped"])

        for lbl_text, ind in [
            ("Process", self._proc_ind),
            ("TCP",     self._tcp_ind),
            ("PTT",     self._ptt_ind),
            ("BUSY",    self._busy_ind),
        ]:
            col = QVBoxLayout()
            col.setSpacing(2)
            col.addWidget(ind, alignment=Qt.AlignmentFlag.AlignHCenter)
            col.addWidget(QLabel(lbl_text), alignment=Qt.AlignmentFlag.AlignHCenter)
            ind_layout.addLayout(col)

        layout.addLayout(ind_layout)

        # Session info
        self._session_lbl = QLabel("No active session")
        self._session_lbl.setWordWrap(True)
        layout.addWidget(self._session_lbl)

        # Traffic
        self._traffic_lbl = QLabel("TX: 0 B   RX: 0 B")
        self._traffic_lbl.setStyleSheet("color:#888;font-size:9pt;")
        layout.addWidget(self._traffic_lbl)

        # Link quality (SN / BITRATE lines from the modem)
        self._snr = None
        self._bps = None
        self._link_lbl = QLabel("")
        self._link_lbl.setStyleSheet("color:#888;font-size:9pt;")
        self._link_lbl.setVisible(False)
        layout.addWidget(self._link_lbl)

        # Aether HF: the modem's own status, from its control API
        self._aether_lbl = QLabel("")
        self._aether_lbl.setWordWrap(True)
        self._aether_lbl.setStyleSheet("color:#b9a6ff;font-size:9pt;")
        self._aether_problems = QLabel("")
        self._aether_problems.setWordWrap(True)
        self._aether_problems.setStyleSheet("color:#f0b232;font-size:9pt;")
        self._aether_lbl.setVisible(False)
        self._aether_problems.setVisible(False)
        layout.addWidget(self._aether_lbl)
        layout.addWidget(self._aether_problems)

        if transport == "AETHER_HF":
            ae_row = QHBoxLayout()
            probe_btn = QPushButton("Probe…")
            probe_btn.setToolTip(
                "Ask another Aether station whether it hears this one, and\n"
                "how well — one short transmission each way, no session.")
            probe_btn.clicked.connect(self._on_probe)
            beacon_btn = QPushButton("Beacon")
            beacon_btn.setToolTip(
                "Send one beacon with the BBS callsign on Aether's tone floor.\n"
                "Refused under automatic control (§97.203(d)).")
            beacon_btn.clicked.connect(self._on_beacon)
            for b in (probe_btn, beacon_btn):
                b.setFixedHeight(22)
                ae_row.addWidget(b)
            ae_row.addStretch()
            layout.addLayout(ae_row)
            self._aether_action_lbl = QLabel("")
            self._aether_action_lbl.setWordWrap(True)
            self._aether_action_lbl.setStyleSheet("color:#b9a6ff;font-size:9pt;")
            layout.addWidget(self._aether_action_lbl)

        # Control buttons
        btn_layout = QHBoxLayout()
        self._btn_start   = QPushButton("Start")
        self._btn_stop    = QPushButton("Stop")
        self._btn_restart = QPushButton("Restart")
        self._btn_abort   = QPushButton("ABORT")
        self._btn_abort.setStyleSheet("color:red;font-weight:bold;")
        self._btn_abort.setToolTip(
            "Send ABORT to the modem: ends the current RF session or\n"
            "call at once, without an orderly disconnect.\n"
            "A normal Stop is preferred."
        )

        self._btn_start.clicked.connect(lambda: self._im.start_instance(self._name))
        self._btn_stop.clicked.connect(lambda: self._im.stop_instance(self._name))
        self._btn_restart.clicked.connect(lambda: self._im.restart_instance(self._name))
        self._btn_abort.clicked.connect(self._on_abort)

        for btn in (self._btn_start, self._btn_stop, self._btn_restart, self._btn_abort):
            btn.setFixedHeight(24)
            btn_layout.addWidget(btn)
        layout.addLayout(btn_layout)


        self.setMinimumWidth(280)

    @pyqtSlot(str)
    def on_process_state(self, state_name: str):
        color = {
            "RUNNING":    STATE_COLORS["running"],
            "CRASHED":    STATE_COLORS["crashed"],
            "STOPPED":    STATE_COLORS["stopped"],
            "STARTING":   STATE_COLORS["connecting"],
        }.get(state_name, STATE_COLORS["stopped"])
        self._proc_ind.setStyleSheet(
            f"background-color:{color};border-radius:7px;"
        )

    @pyqtSlot(bool)
    def on_tcp_connected(self, connected: bool):
        color = STATE_COLORS["connected"] if connected else STATE_COLORS["disconnected"]
        self._tcp_ind.setStyleSheet(
            f"background-color:{color};border-radius:7px;"
        )

    @pyqtSlot(bool)
    def on_ptt(self, active: bool):
        color = STATE_COLORS["ptt"] if active else STATE_COLORS["stopped"]
        self._ptt_ind.setStyleSheet(
            f"background-color:{color};border-radius:7px;"
        )

    @pyqtSlot(bool)
    def on_busy(self, busy: bool):
        color = STATE_COLORS["busy"] if busy else STATE_COLORS["stopped"]
        self._busy_ind.setStyleSheet(
            f"background-color:{color};border-radius:7px;"
        )

    @pyqtSlot(str)
    def on_session_connected(self, call: str):
        self._session_lbl.setText(f"Session: {call}")
        self._session_lbl.setStyleSheet("color:#2ECC71;font-weight:bold;")

    @pyqtSlot()
    def on_session_disconnected(self):
        self._session_lbl.setText("No active session")
        self._session_lbl.setStyleSheet("")
        self._bps = None

    @pyqtSlot(int)
    def on_buffer(self, n: int):
        self._traffic_lbl.setText(
            f"TX: {self._bytes_tx} B   RX: {self._bytes_rx} B   BUF: {n}"
        )

    @pyqtSlot(float, int, int)
    def on_link_quality(self, snr: float, level: int, bps: int):
        """SN / BITRATE from the modem (NaN / -1 for 'not in this update')."""
        if snr == snr:          # not NaN
            self._snr = snr
        if bps >= 0:
            self._bps = (level, bps)
        parts = []
        if self._snr is not None:
            parts.append(f"SNR {self._snr:+.0f} dB")
        if self._bps:
            parts.append(f"level {self._bps[0]} · {self._bps[1]} bps")
        self._link_lbl.setText("Link: " + "   ".join(parts))
        self._link_lbl.setVisible(bool(parts))

    @pyqtSlot(str)
    def on_aether_status(self, payload: str):
        try:
            st = json.loads(payload)
        except ValueError:
            return
        self._aether_lbl.setVisible(True)
        if not st.get("reachable"):
            self._aether_lbl.setText("Aether HF: control API not reachable")
            self._aether_lbl.setToolTip(st.get("error", ""))
            self._aether_problems.setVisible(False)
            return
        bits = [f"Aether HF {st.get('version') or ''}".strip()]
        if st.get("callsign"):
            bits.append(st["callsign"])
        if st.get("bandwidth_hz"):
            bits.append(f"{st['bandwidth_hz']} Hz")
        if st.get("frequency_hz"):
            bits.append(f"{st['frequency_hz'] / 1e6:.3f} MHz")
        state = st.get("state") or "?"
        if st.get("remote"):
            state += f" {st['remote']}"
        bits.append(state)
        verdict = st.get("verdict")
        if st.get("policy") == "none":
            # No regulatory profile: Aether checks nothing, the operator does
            bits.append("no rules set")
        elif verdict:
            bits.append({"legal": "TX legal", "warning": "TX warning",
                         "blocked": "TX blocked"}.get(verdict, verdict))
        self._aether_lbl.setText(" · ".join(bits))
        self._aether_lbl.setToolTip(st.get("verdict_text", ""))
        probs = st.get("problems") or []
        self._aether_problems.setText("\n".join(f"⚠ {p}" for p in probs))
        self._aether_problems.setVisible(bool(probs))

    # ── Aether HF actions ────────────────────────────────────────────────

    def _aether_client(self):
        status = self._im.get_status(self._name) if self._im else None
        client = getattr(status, "aether_client", None)
        if client is None:
            self._aether_action_lbl.setText(
                "Aether HF is not running for this instance.")
        return client

    def _run_aether_action(self, label: str, fn):
        import threading
        from aether.control import AetherControlError
        from PyQt6.QtCore import QMetaObject, Q_ARG
        self._aether_action_lbl.setText(f"{label}…")

        def work():
            try:
                text = fn()
            except AetherControlError as e:
                text = f"{label}: {e}"
            QMetaObject.invokeMethod(
                self, "_show_aether_action", Qt.ConnectionType.QueuedConnection,
                Q_ARG(str, text))
        threading.Thread(target=work, daemon=True, name=f"Aether-{label}").start()

    @pyqtSlot(str)
    def _show_aether_action(self, text: str):
        self._aether_action_lbl.setText(text)
        log.info(f"[{self._name}] {text}")

    def _on_probe(self):
        client = self._aether_client()
        if client is None:
            return
        call, ok = QInputDialog.getText(
            self, "Probe a station", "Callsign to probe:")
        if ok:
            self.probe(call)

    def probe(self, call: str):
        """Ask *call* whether it hears us; the answer shows on this card."""
        call = call.strip().upper()
        client = self._aether_client() if call else None
        if client is not None:
            self._run_aether_action(f"Probe {call}", lambda: client.probe(call))

    def _on_beacon(self):
        client = self._aether_client()
        if client is not None:
            self._run_aether_action(
                "Beacon", lambda: (client.beacon(), "Beacon queued — it goes "
                                   "out when the channel is clear.")[1])

    def _on_abort(self):
        mux = self._im.get_mux(self._name)
        if mux:
            mux.send_cmd("ABORT")
            log.info(f"[{self._name}] ABORT issued by sysop")

    @pyqtSlot(float, int, int)
    def on_scan_freq(self, freq_khz: float, ch_index: int, total: int):
        """Update frequency label with the current scan channel."""
        if freq_khz == int(freq_khz):
            f = str(int(freq_khz))
        else:
            f = f"{freq_khz:.1f}"
        self._freq_lbl.setText(f"📡 {f} kHz ({ch_index + 1}/{total})")
        self._freq_lbl.setStyleSheet("font-weight:bold;font-size:10pt;color:#58a6ff;")

    @pyqtSlot()
    def on_scan_paused(self):
        """Show that scanning is paused (session connected)."""
        current = self._freq_lbl.text()
        # Only modify if we're showing a scan frequency (starts with 📡)
        if current.startswith("📡"):
            # Strip the channel counter to show just the held frequency
            freq_part = current.replace("📡 ", "").split("(")[0].strip()
            self._freq_lbl.setText(f"📡 ⏸ {freq_part}")
            self._freq_lbl.setStyleSheet(
                "font-weight:bold;font-size:10pt;color:#f39c12;"
            )

    @pyqtSlot()
    def on_scan_resumed(self):
        """Next on_scan_freq call will update — just reset colour."""
        self._freq_lbl.setStyleSheet("font-weight:bold;font-size:10pt;color:#58a6ff;")



class ModemStatusPanel(QScrollArea):
    """Scrollable panel containing one InstanceCard per configured instance."""

    def __init__(
        self,
        config: AppConfig,
        im: ModemInstanceManager,
        bus: EventBus,
        store=None,
        bulletin_store=None,
        parent=None,
    ):
        super().__init__(parent)
        self._config = config
        self._im     = im
        self._bus    = bus
        self._store  = store
        self._bbs    = bulletin_store
        self._cards: dict[str, InstanceCard] = {}
        self._build_ui()
        self._wire_events()

        # Refresh heard stations every 5 seconds
        if self._store:
            self._heard_timer = QTimer(self)
            self._heard_timer.timeout.connect(self._refresh_heard)
            self._heard_timer.start(5000)
            self._refresh_heard()

    def _build_ui(self):
        container = QWidget()
        layout    = QVBoxLayout(container)
        layout.setSpacing(4)
        layout.setContentsMargins(4, 4, 4, 4)

        # Compact GroupBox styling
        container.setStyleSheet("""
            QGroupBox {
                margin-top: 8px;
                padding-top: 6px;
                font-weight: bold;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                subcontrol-position: top left;
                padding: 0 3px;
            }
            QLabel { padding: 0px; }
        """)

        title = QLabel("Modem Instances")
        title.setStyleSheet("font-size:12pt;font-weight:bold;padding:0px;")
        layout.addWidget(title)

        for inst_cfg in self._config.instances:
            if inst_cfg.enabled:
                card = InstanceCard(inst_cfg.name, self._im,
                                    inst_cfg=inst_cfg, parent=container)
                self._cards[inst_cfg.name] = card
                layout.addWidget(card)

        # ── Aether HF stations heard (with SNR) ──────────────────────
        self._aether_heard = AetherHeardPanel(container)
        self._aether_heard.probe_requested.connect(self._on_probe_requested)
        self._aether_heard.setVisible(self._has_aether_monitor())
        layout.addWidget(self._aether_heard)

        # ── Recently Heard Stations ──────────────────────────────────
        if self._store:
            heard_group = QGroupBox("Recently Heard Stations")
            hg_layout = QVBoxLayout(heard_group)
            hg_layout.setContentsMargins(4, 2, 4, 2)
            hg_layout.setSpacing(2)
            self._heard_table = QTableWidget(0, 4)
            self._heard_table.setHorizontalHeaderLabels(
                ["Callsign", "Instance", "Last Heard", "Count"]
            )
            self._heard_table.horizontalHeader().setSectionResizeMode(
                QHeaderView.ResizeMode.Stretch
            )
            self._heard_table.verticalHeader().setDefaultSectionSize(22)
            self._heard_table.verticalHeader().setVisible(False)
            hg_layout.addWidget(self._heard_table)

            heard_btn_row = QHBoxLayout()
            clear_heard_btn = QPushButton("🗑  Clear")
            clear_heard_btn.setFixedHeight(24)
            clear_heard_btn.setToolTip("Clear all recently heard stations.")
            clear_heard_btn.clicked.connect(self._on_clear_heard)
            clear_heard_btn.setStyleSheet(
                "QPushButton { background:#5865f2; color:#fff; border:none; "
                "border-radius:4px; padding:4px 12px; font-weight:600; }"
                "QPushButton:hover { background:#4752c4; }"
            )
            heard_btn_row.addStretch()
            heard_btn_row.addWidget(clear_heard_btn)
            hg_layout.addLayout(heard_btn_row)
            layout.addWidget(heard_group)

        layout.addStretch()
        self.setWidget(container)
        self.setWidgetResizable(True)
        self.setMinimumWidth(300)

    def _wire_events(self):
        self._bus.subscribe(EventType.SESSION_CONNECTED,    self._on_connected)
        self._bus.subscribe(EventType.SESSION_DISCONNECTED, self._on_disconnected)
        self._bus.subscribe(EventType.CHANNEL_BUSY,         self._on_busy)
        self._bus.subscribe(EventType.PTT_CHANGE,           self._on_ptt)
        self._bus.subscribe(EventType.BUFFER_UPDATE,        self._on_buffer)
        self._bus.subscribe(EventType.VARA_CONNECTED,       self._on_vara_tcp_up)
        self._bus.subscribe(EventType.VARA_DISCONNECTED,    self._on_vara_tcp_down)
        self._bus.subscribe(EventType.VARA_PROCESS_UP,      self._on_proc_up)
        self._bus.subscribe(EventType.VARA_PROCESS_DOWN,    self._on_proc_down)
        self._bus.subscribe(EventType.CONFIG_CHANGED,       self._on_config_changed)
        self._bus.subscribe(EventType.SCAN_FREQ_CHANGED,    self._on_scan_freq)
        self._bus.subscribe(EventType.LINK_QUALITY,         self._on_link_quality)
        self._bus.subscribe(EventType.AETHER_STATUS,        self._on_aether_status)
        self._bus.subscribe(EventType.AETHER_HEARD,         self._on_aether_heard)

    def _on_config_changed(self, event):
        new_cfg = (event.data or {}).get("config")
        if new_cfg:
            self._config = new_cfg
        from PyQt6.QtCore import QMetaObject
        QMetaObject.invokeMethod(
            self, "_rebuild_cards",
            Qt.ConnectionType.QueuedConnection,
        )

    @pyqtSlot()
    def _rebuild_cards(self):
        """Tear down existing cards and recreate from current config."""
        container = self.widget()
        layout = container.layout()

        # Remove old instance cards from the layout (keep the title and stretch)
        for card in self._cards.values():
            layout.removeWidget(card)
            card.deleteLater()
        self._cards.clear()

        # Re-add cards right after the title, above the heard tables
        pos = 1
        for inst_cfg in self._config.instances:
            if inst_cfg.enabled:
                card = InstanceCard(inst_cfg.name, self._im,
                                    inst_cfg=inst_cfg, parent=container)
                self._cards[inst_cfg.name] = card
                layout.insertWidget(pos, card)
                pos += 1

                # Restore live state from the instance manager
                status = self._im.get_status(inst_cfg.name)
                if status:
                    if status.process_state.name in ("RUNNING",):
                        card.on_process_state("RUNNING")
                    if status.mux is not None:
                        from vara.interface import VARAFMState
                        if status.mux.state != VARAFMState.DISCONNECTED:
                            card.on_tcp_connected(True)
                    if status.current_call:
                        card.on_session_connected(status.current_call)

        self._aether_heard.keep_instances(
            [i.name for i in self._config.instances if i.enabled and i.is_aether])
        self._aether_heard.setVisible(self._has_aether_monitor())
        log.info(f"Modem panel rebuilt — {len(self._cards)} instance card(s)")

    def _on_connected(self, event):
        card = self._cards.get(event.source)
        if card:
            call = (event.data or {}).get("remote_call", "")
            from PyQt6.QtCore import QMetaObject, Q_ARG
            QMetaObject.invokeMethod(card, "on_session_connected",
                Qt.ConnectionType.QueuedConnection, Q_ARG(str, call))
            # Pause scan indicator on the card
            QMetaObject.invokeMethod(card, "on_scan_paused",
                Qt.ConnectionType.QueuedConnection)

    def _on_disconnected(self, event):
        card = self._cards.get(event.source)
        if card:
            from PyQt6.QtCore import QMetaObject
            QMetaObject.invokeMethod(card, "on_session_disconnected",
                Qt.ConnectionType.QueuedConnection)
            # Resume scan indicator on the card
            QMetaObject.invokeMethod(card, "on_scan_resumed",
                Qt.ConnectionType.QueuedConnection)

    def _on_busy(self, event):
        card = self._cards.get(event.source)
        if card:
            from PyQt6.QtCore import QMetaObject, Q_ARG
            QMetaObject.invokeMethod(card, "on_busy",
                Qt.ConnectionType.QueuedConnection,
                Q_ARG(bool, (event.data or {}).get("busy", False)))

    def _on_ptt(self, event):
        card = self._cards.get(event.source)
        if card:
            from PyQt6.QtCore import QMetaObject, Q_ARG
            QMetaObject.invokeMethod(card, "on_ptt",
                Qt.ConnectionType.QueuedConnection,
                Q_ARG(bool, (event.data or {}).get("ptt", False)))

    def _on_buffer(self, event):
        card = self._cards.get(event.source)
        if card:
            from PyQt6.QtCore import QMetaObject, Q_ARG
            QMetaObject.invokeMethod(card, "on_buffer",
                Qt.ConnectionType.QueuedConnection,
                Q_ARG(int, (event.data or {}).get("bytes", 0)))

    def _on_scan_freq(self, event):
        card = self._cards.get(event.source)
        if card:
            d = event.data or {}
            from PyQt6.QtCore import QMetaObject, Q_ARG
            QMetaObject.invokeMethod(card, "on_scan_freq",
                Qt.ConnectionType.QueuedConnection,
                Q_ARG(float, d.get("freq_khz", 0.0)),
                Q_ARG(int, d.get("channel_index", 0)),
                Q_ARG(int, d.get("total_channels", 0)))

    def _on_link_quality(self, event):
        card = self._cards.get(event.source)
        if card:
            d = event.data or {}
            from PyQt6.QtCore import QMetaObject, Q_ARG
            snr = d.get("snr_db")
            QMetaObject.invokeMethod(card, "on_link_quality",
                Qt.ConnectionType.QueuedConnection,
                Q_ARG(float, float("nan") if snr is None else float(snr)),
                Q_ARG(int, int(d.get("level", -1))),
                Q_ARG(int, int(d.get("bps", -1))))

    def _on_aether_status(self, event):
        card = self._cards.get(event.source)
        if card:
            from PyQt6.QtCore import QMetaObject, Q_ARG
            QMetaObject.invokeMethod(card, "on_aether_status",
                Qt.ConnectionType.QueuedConnection,
                Q_ARG(str, json.dumps(event.data or {}, default=str)))

    def _on_vara_tcp_up(self, event):
        card = self._cards.get(event.source)
        if card:
            from PyQt6.QtCore import QMetaObject, Q_ARG
            QMetaObject.invokeMethod(card, "on_tcp_connected",
                Qt.ConnectionType.QueuedConnection, Q_ARG(bool, True))

    def _on_vara_tcp_down(self, event):
        card = self._cards.get(event.source)
        if card:
            from PyQt6.QtCore import QMetaObject, Q_ARG
            QMetaObject.invokeMethod(card, "on_tcp_connected",
                Qt.ConnectionType.QueuedConnection, Q_ARG(bool, False))

    def _on_proc_up(self, event):
        card = self._cards.get(event.source)
        if card:
            from PyQt6.QtCore import QMetaObject, Q_ARG
            QMetaObject.invokeMethod(card, "on_process_state",
                Qt.ConnectionType.QueuedConnection, Q_ARG(str, "RUNNING"))

    def _on_proc_down(self, event):
        card = self._cards.get(event.source)
        if card:
            from PyQt6.QtCore import QMetaObject, Q_ARG
            QMetaObject.invokeMethod(card, "on_process_state",
                Qt.ConnectionType.QueuedConnection, Q_ARG(str, "CRASHED"))

    # ── Aether HF stations heard ──────────────────────────────────────

    def _has_aether_monitor(self) -> bool:
        return any(i.enabled and i.is_aether and i.aether.monitor
                   for i in self._config.instances)

    def _on_aether_heard(self, event):
        from PyQt6.QtCore import QMetaObject, Q_ARG
        QMetaObject.invokeMethod(
            self._aether_heard, "set_stations", Qt.ConnectionType.QueuedConnection,
            Q_ARG(str, event.source or ""),
            Q_ARG(str, json.dumps(event.data or {}, default=str)))

    def _on_probe_requested(self, instance: str, call: str):
        card = self._cards.get(instance)
        if card:
            card.probe(call)

    # ── Heard stations ────────────────────────────────────────────────

    def _refresh_heard(self):
        if not self._store:
            return
        stations = self._store.get_heard_stations(limit=20)
        self._heard_table.setRowCount(0)
        for station in stations:
            row = self._heard_table.rowCount()
            self._heard_table.insertRow(row)
            call = station["callsign"]
            if station["ssid"]:
                call += f"-{station['ssid']}"
            self._heard_table.setItem(row, 0, QTableWidgetItem(call))
            self._heard_table.setItem(row, 1, QTableWidgetItem(station["instance"]))
            self._heard_table.setItem(row, 2, QTableWidgetItem(
                str(station["last_heard"])[:19]))
            self._heard_table.setItem(row, 3, QTableWidgetItem(
                str(station["hear_count"])))

    def _on_clear_heard(self):
        if not self._store:
            return
        self._store.clear_heard_stations()
        self._heard_table.setRowCount(0)

    # ── Bulletin posting removed — consolidated in Bulletins tab ────
