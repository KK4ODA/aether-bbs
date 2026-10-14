"""
gui/mainwindow.py

Main application window.
Houses all tabs and wires the EventBus to Qt signals for safe
cross-thread GUI updates.
"""

import logging
from PyQt6.QtWidgets import (
    QMainWindow, QTabWidget, QStatusBar, QLabel, QWidget,
    QHBoxLayout,
)
from PyQt6.QtCore import Qt, QTimer, pyqtSignal, QObject
from PyQt6.QtGui import QCloseEvent, QResizeEvent

from gui.theme import GREEN, RED, YELLOW, TEXT_MUTED

from core.config import AppConfig
from core.config import ConfigManager
from core.events import EventBus, EventType
from vara.instance_manager import ModemInstanceManager
from bbs.store import MessageStore

from gui.modem_panel import ModemStatusPanel
from gui.bbs_panel import BBSStatusPanel
from gui.settings import SettingsTab
from gui.log_viewer import LogViewerTab
from gui.packet_monitor import PacketMonitorTab
from gui.messages_tab import MessagesTab
from gui.bulletins_tab import BulletinsTab
from gui.files_tab import FilesTab
from gui.chat_window import ChatWindow
from gui.winlink_panel import WinlinkPanel
from gui.session_sounds import play_connect_sound, play_disconnect_sound
log = logging.getLogger(__name__)


class EventSignalBridge(QObject):
    """Bridges EventBus callbacks (any thread) → Qt signals (main thread)."""
    session_connected    = pyqtSignal(str, str)   # instance, call
    session_disconnected = pyqtSignal(str)         # instance
    vara_process_up      = pyqtSignal(str)
    vara_process_down    = pyqtSignal(str)
    vara_disconnected    = pyqtSignal(str)
    aprs_is_connected    = pyqtSignal()
    aprs_is_disconnected = pyqtSignal()
    message_received          = pyqtSignal(str)   # mid
    priority_message_received = pyqtSignal(str)   # mid
    chat_request              = pyqtSignal(str, str, object)  # instance, remote_call, sysop_queue
    chat_message              = pyqtSignal(str, str, str, str)  # instance, direction, remote_call, text
    chat_ended                = pyqtSignal(str, str)            # instance, reason
    scan_freq_changed         = pyqtSignal(str, float, int, int)  # instance, freq_khz, ch_index, total

    # Software update signals
    update_available = pyqtSignal(object)   # UpdateCheckResult
    update_failed    = pyqtSignal(str)      # error message


class MainWindow(QMainWindow):

    def __init__(
        self,
        config_manager: ConfigManager,
        config: AppConfig,
        instance_manager: ModemInstanceManager,
        store: MessageStore,
        event_bus: EventBus,
        bulletin_store=None,
        file_store=None,
        gateway=None,
        cms_poller=None,
        access_controller=None,
        notifiers=None,
    ):
        super().__init__()
        self._cm      = config_manager
        self._config  = config
        self._im      = instance_manager
        self._store   = store
        self._bbs     = bulletin_store
        self._files   = file_store
        self._gateway = gateway
        self._cms_poller = cms_poller
        self._access  = access_controller
        self._notifiers = notifiers or {}
        self._bus     = event_bus

        self._bridge  = EventSignalBridge(self)
        self._setup_bridge()

        self.setWindowTitle(
            f"Aether BBS — {config.station.bbs_callsign}"
        )
        self.setMinimumSize(1024, 700)
        self.resize(1280, 860)
        self._active_sessions: dict[str, str] = {}  # instance → callsign
        self._build_ui()
        self._connect_signals()

        # Start AlertManager — listens for messages to Home Tactical Callsign
        from gui.message_viewer import AlertManager
        self._alert_manager = AlertManager(self._store, self._cm, self._bus)
        self._alert_manager.trigger_signal.connect(self._on_priority_message)

        # Track open priority alert windows by MID (prevent duplicates)
        self._priority_alerts: dict = {}
        # Phrase from AlertManager bus event, keyed by MID
        self._pending_alert_phrases: dict[str, str] = {}

        # Track open chat windows by instance name
        self._chat_windows: dict[str, ChatWindow] = {}

        # Poll-based status refresh (1 Hz)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh_status)
        self._timer.start(1000)

        # Internet connectivity check (every 30s)
        self._inet_online = True
        self._inet_timer = QTimer(self)
        self._inet_timer.timeout.connect(self._check_internet)
        self._inet_timer.start(30_000)
        QTimer.singleShot(2000, self._check_internet)  # first check after 2s

        log.info("Main window created")

    # ── UI construction ───────────────────────────────────────────────────

    def _build_ui(self):
        self._tabs = QTabWidget()
        self.setCentralWidget(self._tabs)

        # ── Tab 1: Dashboard ──────────────────────────────────────────
        # Modem status + BBS status side by side
        self._modem_panel = ModemStatusPanel(
            self._config, self._im, self._bus,
            store=self._store, bulletin_store=self._bbs,
            parent=self
        )
        self._bbs_panel  = BBSStatusPanel(
            self._config, self._im, self._store, self._bus,
            gateway=self._gateway, access_controller=self._access,
            parent=self
        )

        from PyQt6.QtWidgets import QSplitter, QVBoxLayout
        self._dashboard = QSplitter(Qt.Orientation.Horizontal)

        # Left side: modem status + Winlink CMS stacked
        left_widget = QWidget()
        left_layout = QVBoxLayout(left_widget)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(4)
        left_layout.addWidget(self._modem_panel)

        self._winlink_panel = WinlinkPanel(self._bus, parent=self)
        if self._cms_poller:
            self._winlink_panel.set_poller(self._cms_poller)
        left_layout.addWidget(self._winlink_panel)

        self._dashboard.addWidget(left_widget)

        # Right side: BBS panel (full height)
        self._dashboard.addWidget(self._bbs_panel)

        self._dashboard.setSizes([600, 600])
        self._dashboard_horizontal = True  # track current orientation

        self._tabs.addTab(self._dashboard, "Dashboard")

        # ── Tab 2: Messages (compound — Messages, Bulletins, Files) ───
        self._messages_group = QTabWidget()

        self._messages_tab = MessagesTab(
            self._store, config=self._config, parent=self
        )
        self._messages_group.addTab(self._messages_tab, "Messages")

        self._messages_group.addTab(
            BulletinsTab(
                self._bbs, self._config,
                event_bus=self._bus, config_manager=self._cm, parent=self
            ),
            "Bulletins"
        )

        if self._files:
            self._files_tab = FilesTab(
                self._files,
                sysop_callsign=self._config.station.sysop_callsign,
                parent=self,
            )
            self._messages_group.addTab(self._files_tab, "Files")

        self._tabs.addTab(self._messages_group, "Messages")

        # ── Tab 3: Settings (unchanged) ───────────────────────────────
        self._tabs.addTab(
            SettingsTab(self._cm, self._config, self._bus,
                       notifiers=self._notifiers, parent=self),
            "Settings"
        )

        # ── Tab 4: Diagnostics ────────────────────────────────────────
        self._diag_group = QTabWidget()

        self._diag_group.addTab(
            PacketMonitorTab(
                self._im, self._store, self._bus, parent=self
            ),
            "Activity Monitor"
        )
        self._diag_group.addTab(LogViewerTab(parent=self), "Log")

        self._tabs.addTab(self._diag_group, "Diagnostics")

        # ── Status bar ────────────────────────────────────────────────
        self._status_bar = QStatusBar()
        self.setStatusBar(self._status_bar)
        self._status_label = QLabel("Starting...")
        self._status_bar.addWidget(self._status_label, stretch=1)

        # Instance state indicators — one colored dot + label per instance.
        # Built once here, updated every 1 Hz by _refresh_status().
        self._inst_indicators: dict[str, QLabel] = {}
        inst_container = QWidget()
        inst_container.setStyleSheet(f"background:transparent;")
        inst_layout = QHBoxLayout(inst_container)
        inst_layout.setContentsMargins(4, 0, 4, 0)
        inst_layout.setSpacing(10)

        for name in self._im.instance_names():
            ind = QLabel(f"● {name}")
            ind.setStyleSheet(f"color:{TEXT_MUTED}; font-size:9pt;")
            ind.setToolTip(f"{name}: starting…")
            inst_layout.addWidget(ind)
            self._inst_indicators[name] = ind

        self._status_bar.addPermanentWidget(inst_container)

        # Internet status indicator
        self._inet_label = QLabel("NET: ?")
        self._inet_label.setStyleSheet(f"color:{TEXT_MUTED}; font-size:9pt;")
        self._inet_label.setToolTip("Internet connectivity status")
        self._status_bar.addPermanentWidget(self._inet_label)

        # Update-available indicator (clickable)
        self._update_label = QLabel("")
        self._update_label.setStyleSheet(
            f"color:{YELLOW}; font-size:9pt; font-weight:bold;"
        )
        self._update_label.setCursor(Qt.CursorShape.PointingHandCursor)
        self._update_label.setToolTip("Click to view available update")
        self._update_label.hide()
        self._update_label.mousePressEvent = lambda _ev: self._show_update_dialog()
        self._status_bar.addPermanentWidget(self._update_label)
        self._updater = None          # set later via set_updater()
        self._pending_update = None   # latest UpdateCheckResult
        self._prompted_versions: set[str] = set()   # auto-prompted this run

        # Scan status indicator — permanent widget on the far right.
        self._scan_label = QLabel("")
        self._scan_label.setStyleSheet("color:#069; font-weight:bold;")
        self._scan_label.setToolTip("HF frequency scan status")
        self._scan_label.hide()
        self._status_bar.addPermanentWidget(self._scan_label)
        self._scan_last_freq = 0.0
        self._scan_instance = ""

    def _connect_signals(self):
        b = self._bridge
        b.session_connected.connect(self._on_session_connected)
        b.session_disconnected.connect(self._on_session_disconnected)
        b.vara_process_up.connect(lambda src: self._set_status(f"{src}: modem running"))
        b.vara_process_down.connect(lambda src: self._set_status(f"{src}: modem CRASHED"))
        b.aprs_is_connected.connect(lambda: self._set_status("APRS-IS connected"))
        b.aprs_is_disconnected.connect(lambda: self._set_status("APRS-IS disconnected"))

        # Auto-refresh Messages tab when it becomes visible —
        # either via outer tab switch or inner sub-tab switch.
        self._tabs.currentChanged.connect(self._on_tab_changed)
        self._messages_group.currentChanged.connect(self._on_inner_tab_changed)

        # Priority alert fires via EventBus → bridge → _on_priority_message
        # (AlertManager.trigger_signal is connected after construction, not here)
        b.priority_message_received.connect(self._on_priority_message)

        # Refresh messages tab when any message arrives
        b.message_received.connect(lambda mid: self._messages_tab.refresh())

        # Chat
        b.chat_request.connect(self._on_chat_request)
        b.chat_message.connect(self._on_chat_message)
        b.chat_ended.connect(self._on_chat_ended)

        # Scan status indicator
        b.scan_freq_changed.connect(self._on_scan_freq_changed)
        b.session_connected.connect(self._on_scan_paused)
        b.session_disconnected.connect(self._on_scan_resumed)
        b.vara_disconnected.connect(self._on_scan_idle)

        # Software updates
        b.update_available.connect(self._on_update_available)
        b.update_failed.connect(lambda msg: log.debug("Update check: %s", msg))

    def _on_tab_changed(self, index: int):
        """Outer tab switched — refresh Messages if the Messages group is now active."""
        if self._tabs.widget(index) is self._messages_group:
            if self._messages_group.currentWidget() is self._messages_tab:
                self._messages_tab.refresh()

    def _on_inner_tab_changed(self, index: int):
        """Inner sub-tab switched within the Messages group."""
        if self._messages_group.widget(index) is self._messages_tab:
            self._messages_tab.refresh()

    # ── EventBus → Qt bridge setup ────────────────────────────────────────

    def _setup_bridge(self):
        b = self._bridge
        bus = self._bus

        def _emit(signal, *args):
            """Emit a Qt signal from any thread safely."""
            try:
                signal.emit(*args)
            except Exception as e:
                log.error(f"Signal emit error: {e}")

        bus.subscribe(EventType.SESSION_CONNECTED,
            lambda e: _emit(b.session_connected,
                            e.source, e.data.get("remote_call", "")))
        bus.subscribe(EventType.SESSION_DISCONNECTED,
            lambda e: _emit(b.session_disconnected, e.source))
        bus.subscribe(EventType.VARA_PROCESS_UP,
            lambda e: _emit(b.vara_process_up, e.source))
        bus.subscribe(EventType.VARA_PROCESS_DOWN,
            lambda e: _emit(b.vara_process_down, e.source))
        bus.subscribe(EventType.VARA_DISCONNECTED,
            lambda e: _emit(b.vara_disconnected, e.source))
        bus.subscribe(EventType.APRS_IS_CONNECTED,
            lambda e: _emit(b.aprs_is_connected))
        bus.subscribe(EventType.APRS_IS_DISCONNECTED,
            lambda e: _emit(b.aprs_is_disconnected))
        bus.subscribe(EventType.MESSAGE_RECEIVED,
            lambda e: _emit(b.message_received, (e.data or {}).get("mid", "")))
        def _on_priority_bus(e):
            d = e.data or {}
            mid    = d.get("mid", "")
            phrase = d.get("alert_phrase", "Priority message received")
            if mid:
                self._pending_alert_phrases[mid] = phrase
            _emit(b.priority_message_received, mid)

        bus.subscribe(EventType.PRIORITY_MESSAGE_RECEIVED, _on_priority_bus)

        def _on_chat_request_bus(e):
            d = e.data or {}
            _emit(b.chat_request, e.source,
                  d.get("remote_call", "UNKNOWN"),
                  d.get("sysop_queue"))

        def _on_chat_message_bus(e):
            d = e.data or {}
            _emit(b.chat_message, e.source,
                  d.get("direction", ""),
                  d.get("remote_call", ""),
                  d.get("text", ""))

        def _on_chat_ended_bus(e):
            d = e.data or {}
            _emit(b.chat_ended, e.source, d.get("reason", ""))

        bus.subscribe(EventType.CHAT_REQUEST, _on_chat_request_bus)
        bus.subscribe(EventType.CHAT_MESSAGE, _on_chat_message_bus)
        bus.subscribe(EventType.CHAT_ENDED,   _on_chat_ended_bus)

        # Converse (multi-user chat room) events
        def _on_converse_join_bus(e):
            d = e.data or {}
            room = d.get("room", "?")
            call = d.get("callsign", "?")
            log.info(f"CONVERSE {call} joined room {room}")

        def _on_converse_part_bus(e):
            d = e.data or {}
            room = d.get("room", "?")
            call = d.get("callsign", "?")
            log.info(f"CONVERSE {call} left room {room}")

        bus.subscribe(EventType.CONVERSE_JOIN, _on_converse_join_bus)
        bus.subscribe(EventType.CONVERSE_PART, _on_converse_part_bus)

        bus.subscribe(EventType.SCAN_FREQ_CHANGED,
            lambda e: _emit(b.scan_freq_changed, e.source,
                            (e.data or {}).get("freq_khz", 0.0),
                            (e.data or {}).get("channel_index", 0),
                            (e.data or {}).get("total_channels", 0)))

        # Software update events
        bus.subscribe(EventType.UPDATE_AVAILABLE,
            lambda e: _emit(b.update_available, e.data))
        bus.subscribe(EventType.UPDATE_FAILED,
            lambda e: _emit(b.update_failed, str(e.data or "")))

    # ── Slot handlers ─────────────────────────────────────────────────────

    def _on_session_connected(self, instance: str, call: str):
        self._active_sessions[instance] = call
        self._set_status(f"[{instance}] Connected: {call}")
        self._update_title()
        cfg = self._cm.current() or self._config
        if cfg.alert.session_sounds:
            play_connect_sound(volume=cfg.alert.volume)

    def _on_session_disconnected(self, instance: str):
        self._active_sessions.pop(instance, None)
        self._set_status(f"[{instance}] Disconnected")
        self._update_title()
        cfg = self._cm.current() or self._config
        if cfg.alert.session_sounds:
            play_disconnect_sound(volume=cfg.alert.volume)

    def _on_priority_message(self, mid: str):
        """Show a priority alert window for a message to the Home Tactical Callsign."""
        log.info(f"MainWindow: _on_priority_message called mid={mid!r}")
        if not mid:
            return
        if mid in self._priority_alerts:
            log.info(f"MainWindow: alert window already open for MID={mid}, raising")
            w = self._priority_alerts[mid]
            w.raise_()
            w.activateWindow()
            return

        from gui.message_viewer import PriorityAlertWindow, AudioAlertThread
        cfg    = self._cm.current() or self._config   # always use live config
        phrase = self._pending_alert_phrases.pop(mid, "Priority message received")
        log.info(f"MainWindow: audio_enabled={cfg.alert.audio_enabled} phrase={phrase!r}")
        audio = AudioAlertThread(cfg.alert, phrase=phrase)
        audio.start()

        alert_win = PriorityAlertWindow(
            store=self._store,
            mid=mid,
            our_call=cfg.station.bbs_callsign,
            config=cfg,
            audio_manager=audio,
            parent=None,
        )
        self._priority_alerts[mid] = alert_win
        alert_win.acknowledged.connect(lambda m: self._on_alert_acknowledged(m))
        alert_win.show()
        alert_win.raise_()
        alert_win.activateWindow()
        self._set_status(f"⚠ PRIORITY MESSAGE received — MID {mid}")
        log.warning(f"Priority alert displayed for MID={mid}")

    def _on_alert_acknowledged(self, mid: str):
        self._priority_alerts.pop(mid, None)
        self._messages_tab.refresh()
        self._set_status(f"Priority message acknowledged — MID {mid}")

    # ── Chat slot handlers ────────────────────────────────────────────────

    def _on_chat_request(self, instance: str, remote_call: str, sysop_queue):
        """Create and show the chat window for an incoming CH request."""
        # If a window already exists for this instance, close it first
        existing = self._chat_windows.get(instance)
        if existing:
            existing.close()

        cfg = self._cm.current() or self._config
        win = ChatWindow(
            instance_name=instance,
            remote_call=remote_call,
            sysop_call=cfg.station.sysop_callsign,
            sysop_queue=sysop_queue,
            parent=None,
        )
        win.ended.connect(lambda: self._chat_windows.pop(instance, None))
        self._chat_windows[instance] = win
        win.show()
        win.raise_()
        win.activateWindow()
        self._set_status(f"💬 Chat request from {remote_call} on {instance}")
        log.info(f"Chat window opened for {remote_call} on {instance}")

    def _on_chat_message(
        self, instance: str, direction: str, remote_call: str, text: str
    ):
        """Forward a chat line to the appropriate open chat window."""
        win = self._chat_windows.get(instance)
        if win:
            win.append_message(direction, text)

    def _on_chat_ended(self, instance: str, reason: str):
        """Mark the chat window as ended; keep it open for review."""
        win = self._chat_windows.get(instance)
        if win:
            win.set_ended(reason)
        self._set_status(f"Chat session ended on {instance}")

    def _set_status(self, text: str):
        self._status_label.setText(text)

    # ── Scan status indicator ─────────────────────────────────────────────

    def _on_scan_freq_changed(
        self, instance: str, freq_khz: float, ch_index: int, total: int
    ):
        """Update scan indicator when the rig tunes to a new channel."""
        self._scan_instance = instance
        self._scan_last_freq = freq_khz
        # Format freq: drop trailing .0 for clean integers
        if freq_khz == int(freq_khz):
            f = str(int(freq_khz))
        else:
            f = f"{freq_khz:.1f}"
        self._scan_label.setText(
            f"📡 {f} kHz ({ch_index + 1}/{total})"
        )
        self._scan_label.show()

    def _on_scan_paused(self, instance: str, call: str = ""):
        """Show paused state when a session connects on the scanning instance."""
        if not self._scan_instance or instance != self._scan_instance:
            return  # not the scanning instance
        if not self._scan_label.isVisible():
            return  # no scan running
        freq = self._scan_last_freq
        if freq == int(freq):
            f = str(int(freq))
        else:
            f = f"{freq:.1f}"
        self._scan_label.setText(f"📡 paused — {f} kHz")

    def _on_scan_resumed(self, instance: str):
        """Next scan_freq_changed will update the label — nothing to do."""
        pass

    def _on_scan_idle(self, instance: str):
        """Hide scan indicator when VARA disconnects on the scanning instance."""
        if not self._scan_instance or instance != self._scan_instance:
            return
        self._scan_label.setText("")
        self._scan_label.hide()
        self._scan_instance = ""

    def _refresh_status(self):
        """Called by 1 Hz timer — update instance indicators and window title."""
        from vara.process import ProcessState

        try:
            statuses = self._im.status_all()
        except Exception:
            return  # instance manager may not be ready yet

        for name, ind in self._inst_indicators.items():
            st = statuses.get(name)
            if st is None:
                ind.setStyleSheet(f"color:{TEXT_MUTED}; font-size:9pt;")
                ind.setToolTip(f"{name}: unknown")
                ind.setText(f"● {name}")
                continue

            # Determine color from process state + session state
            proc = st.process_state
            call = st.current_call

            if proc == ProcessState.CRASHED:
                color = RED
                tip = f"{name}: CRASHED"
            elif proc == ProcessState.STOPPED:
                color = TEXT_MUTED
                tip = f"{name}: stopped"
            elif proc in (ProcessState.STARTING, ProcessState.STOPPING):
                color = YELLOW
                tip = f"{name}: {proc.name.lower()}"
            elif call:
                color = GREEN
                tip = f"{name}: connected — {call}"
            elif proc == ProcessState.RUNNING:
                color = GREEN
                tip = f"{name}: listening"
            else:
                color = TEXT_MUTED
                tip = f"{name}: idle"

            label_text = f"● {name}"
            if call:
                label_text += f"  {call}"
            ind.setText(label_text)
            ind.setStyleSheet(f"color:{color}; font-size:9pt;")
            ind.setToolTip(tip)

        self._update_title()

    def _check_internet(self):
        """Periodic internet connectivity check — updates status bar indicator."""
        import socket
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(2)
            s.connect(("8.8.8.8", 53))
            s.close()
            online = True
        except Exception:
            online = False

        if online != self._inet_online:
            self._inet_online = online
            if online:
                log.info("Internet connectivity restored")
            else:
                log.warning("Internet connectivity lost — Winlink/email messages will queue")

        if online:
            self._inet_label.setText("NET: OK")
            self._inet_label.setStyleSheet(f"color:{GREEN}; font-size:9pt;")
            self._inet_label.setToolTip("Internet: connected")
        else:
            self._inet_label.setText("NET: DOWN")
            self._inet_label.setStyleSheet(f"color:{RED}; font-size:9pt; font-weight:bold;")
            self._inet_label.setToolTip(
                "Internet: offline — Winlink and email messages will queue until restored")

    def _update_title(self):
        """Update window title with callsign and session count."""
        cfg = self._cm.current() or self._config
        callsign = cfg.station.bbs_callsign
        n_inst = len(self._im.instance_names())
        n_sess = len(self._active_sessions)
        if n_sess:
            sessions = ", ".join(
                f"{call}" for call in self._active_sessions.values()
            )
            self.setWindowTitle(
                f"Aether BBS — {callsign} · {n_sess} session(s): {sessions}"
            )
        else:
            self.setWindowTitle(
                f"Aether BBS — {callsign} · {n_inst} instance(s)"
            )

    # ── Responsive dashboard layout ───────────────────────────────────────

    def resizeEvent(self, event: QResizeEvent):
        """Switch dashboard splitter orientation based on window width."""
        super().resizeEvent(event)
        w = event.size().width()
        if w < 1000 and self._dashboard_horizontal:
            self._dashboard.setOrientation(Qt.Orientation.Vertical)
            self._dashboard_horizontal = False
        elif w >= 1000 and not self._dashboard_horizontal:
            self._dashboard.setOrientation(Qt.Orientation.Horizontal)
            self._dashboard_horizontal = True

    # ── Software update ────────────────────────────────────────────────────

    def set_updater(self, updater, restart_callback=None):
        """Called by BBSApplication after construction."""
        self._updater = updater
        self._restart_callback = restart_callback

    def _on_update_available(self, check_result):
        self._pending_update = check_result
        ver = check_result.latest_version
        if ver and ver != check_result.current_version:
            self._update_label.setText(f"UPDATE v{ver}")
        else:
            self._update_label.setText(f"UPDATE ({len(check_result.commits)})")
        self._update_label.show()

        # Ask the sysop right away (once per version per run), unless they
        # asked to skip this version or pressed "Check for Updates" in
        # Settings, which opens its own dialog.
        if check_result.manual:
            return
        key = ver or check_result.remote_hash
        if key in self._prompted_versions:
            return
        cfg = self._cm.current() or self._config
        if ver and check_result.release and ver == cfg.updater.skipped_version:
            log.info("Update %s available but skipped by the sysop", ver)
            return
        self._prompted_versions.add(key)
        QTimer.singleShot(0, self._show_update_dialog)

    def _skip_update_version(self, version: str):
        cfg = self._cm.current() or self._config
        cfg = cfg.model_copy(update={
            "updater": cfg.updater.model_copy(update={"skipped_version": version})})
        try:
            self._cm.save(cfg)
            log.info("Sysop skipped update %s", version)
        except Exception as e:
            log.error("Could not save skipped update version: %s", e)

    def _show_update_dialog(self, check_result=None):
        check_result = check_result or self._pending_update
        if not self._updater or not check_result:
            return
        from gui.update_dialog import UpdateDialog
        dlg = UpdateDialog(
            check_result,
            self._updater,
            self._restart_callback or (lambda **_: None),
            parent=self,
            on_skip=self._skip_update_version,
            active_sessions=len(self._active_sessions),
        )
        dlg.exec()

    # ── Close handling ────────────────────────────────────────────────────

    def closeEvent(self, event: QCloseEvent):
        from PyQt6.QtWidgets import QMessageBox
        reply = QMessageBox.question(
            self,
            "Quit",
            "Stop the BBS and quit?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self._bus.emit(EventType.SHUTDOWN)
            event.accept()
        else:
            event.ignore()
