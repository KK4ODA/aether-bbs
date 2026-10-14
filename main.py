"""
Aether BBS - Main Entry Point
"""

import sys
import signal
import atexit
import logging
import threading

# Qt must be imported before other app modules that use it
from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import Qt

from core.config import ConfigManager
from core.paths import install_dir
from core.logging_manager import setup_logging, update_vara_cmd_logging
from core.events import EventBus, EventType
from vara.instance_manager import ModemInstanceManager
from bbs.store import MessageStore
from bbs.filestore import FileStore
from bbs.bulletins import BulletinStore, BulletinForwarder, BulletinForwardingServer
from bbs.dispatcher import SessionDispatcher
from bbs.access import AccessController
from bbs.rate_limiter import RateLimiter
from bbs.mode_policy import ModePolicy
from aprs.gateway import APRSGateway
from gui.mainwindow import MainWindow
from winlink.pat_client import PatClient
from winlink.notify import CMSNotifier
from delivery.inbound import InboundEmailPoller, WinlinkForwardManager
from notifications.ntfy import NtfyNotifier
from notifications.pushover import PushoverNotifier
from notifications.sms import SMSNotifier
from notifications import NotificationRouter


log = logging.getLogger(__name__)


class BBSApplication:
    """Top-level application controller. Owns all major subsystems."""

    def __init__(self):
        self.config_manager    = ConfigManager()
        self.config            = None
        self.event_bus         = EventBus()
        self.instance_manager  = None
        self.message_store     = None
        self.bulletin_store    = None
        self.file_store        = None
        self.bulletin_forwarder = None
        self.bulletin_server   = None
        self.dispatcher        = None
        self.aprs_gateway      = None
        self.cms_poller        = None
        self.cms_notifier      = None
        self.inbound_email     = None
        self._inbound_applied  = None   # inbound email settings in use
        self.forward_manager   = None
        self.ntfy_notifier     = None
        self.pushover_notifier = None
        self.sms_notifier      = None
        self.notification_router = None
        self.access_controller = None
        self.rate_limiter      = None
        self.mode_policy       = None
        self.main_window       = None
        self.updater           = None
        self._shutdown_done    = False
        self._relaunch         = False   # restart() asks main() to start us again
        self._shutdown_lock    = threading.Lock()

    def start(self, app: QApplication) -> int:
        # --- Load configuration ---
        self.config = self.config_manager.load()
        setup_logging(self.config.logging)
        log.info("Aether BBS starting up")
        log.info(f"BBS callsign: {self.config.station.bbs_callsign}")

        # --- Initialise subsystems ---
        self.message_store = MessageStore(self.config, event_bus=self.event_bus)
        self.message_store.initialize()

        # Security: access control, rate limiting, operational mode policy
        self.access_controller = AccessController(
            conn=self.message_store.conn,
            lock=self.message_store.lock,
            config=self.config_manager,
            event_bus=self.event_bus,
        )
        # Migrate upload whitelist → role-based access control (one-time)
        self.access_controller.migrate_from_whitelist(self.config)

        self.mode_policy = ModePolicy(self.config)
        self.rate_limiter = RateLimiter(
            multiplier=self.mode_policy.rate_limit_multiplier
        )

        # Bulletin board — always initialised (zero cost if unused)
        self.file_store = FileStore(
            self.config_manager, event_bus=self.event_bus,
            access_controller=self.access_controller,
        )
        self.file_store.initialize()

        self.bulletin_store = BulletinStore(self.message_store)
        # given the ConfigManager, both follow Settings → Save
        self.bulletin_forwarder = BulletinForwarder(
            self.config_manager, self.message_store, self.bulletin_store,
            self.event_bus,
        )
        self.bulletin_server = BulletinForwardingServer(
            self.config_manager, self.message_store, self.bulletin_store,
            self.event_bus,
        )

        self.instance_manager = ModemInstanceManager(
            self.config, self.event_bus
        )
        # RF neighbours are called through the modem instances
        self.bulletin_forwarder.set_instance_manager(self.instance_manager)

        # Multi-user chat room manager
        from bbs.converse import ConversationManager
        self.converse_manager = ConversationManager(self.event_bus)

        self.dispatcher = SessionDispatcher(
            self.config_manager, self.instance_manager,
            self.message_store, self.event_bus,
            bulletin_store=self.bulletin_store,
            file_store=self.file_store,
            access_controller=self.access_controller,
            mode_policy=self.mode_policy,
            rate_limiter=self.rate_limiter,
            converse_manager=self.converse_manager,
        )

        # always built (it connects only once started) so that turning
        # APRS on in Settings needs no restart
        self.aprs_gateway = APRSGateway(self.config_manager, self.event_bus)

        # Pat Winlink client — always create, enable/disable via config
        self.cms_poller = PatClient(
            store=self.message_store,
            event_bus=self.event_bus,
        )
        self._apply_winlink_config(self.config)

        # Wire outbound queue and Pat client into dispatcher for Winlink routing
        if self.cms_poller and self.cms_poller.outbound_queue:
            self.dispatcher._outbound_queue = self.cms_poller.outbound_queue
        self.dispatcher._pat_client = self.cms_poller

        # CMS notification hooks
        self.cms_notifier = CMSNotifier(self.event_bus)

        # Winlink forward manager — dynamically builds ;FW list
        self.forward_manager = WinlinkForwardManager(event_bus=self.event_bus)
        self.forward_manager.set_access_controller(self.access_controller)
        self.forward_manager.set_message_store(self.message_store)
        self.cms_poller.set_forward_manager(self.forward_manager)

        # Inbound email poller — IMAP/POP3 polling for email→RF delivery
        self.inbound_email = InboundEmailPoller(
            store=self.message_store,
            event_bus=self.event_bus,
        )
        self.inbound_email.set_access_controller(self.access_controller)
        self._apply_internet_email(self.config)

        # Push notification backends — subscribe to PRIORITY_MESSAGE_RECEIVED
        self.ntfy_notifier = NtfyNotifier(self.config_manager, self.event_bus)
        self.pushover_notifier = PushoverNotifier(self.config_manager, self.event_bus)
        self.sms_notifier = SMSNotifier(self.config_manager, self.event_bus)

        # Notification router — translates ACCESS_REQUEST and BULLETIN_POSTED
        # into PRIORITY_MESSAGE_RECEIVED for the backends above
        self.notification_router = NotificationRouter(
            self.config_manager, self.event_bus
        )

        # --- Register shutdown handlers ---
        # Ctrl+C, SIGTERM and the window's Quit end the Qt event loop;
        # main() shuts the subsystems down after it returns, so no GUI timer
        # can run against a closed database.  atexit is the fallback.
        atexit.register(self._shutdown)
        signal.signal(signal.SIGINT,  lambda *_: self._quit_app())
        signal.signal(signal.SIGTERM, lambda *_: self._quit_app())
        self.event_bus.subscribe(EventType.SHUTDOWN, lambda _e: self._quit_app())

        # --- Build GUI ---
        self.main_window = MainWindow(
            self.config_manager,
            self.config,
            self.instance_manager,
            self.message_store,
            self.event_bus,
            bulletin_store=self.bulletin_store,
            file_store=self.file_store,
            gateway=self.aprs_gateway,
            cms_poller=self.cms_poller,
            access_controller=self.access_controller,
            notifiers={
                "ntfy": self.ntfy_notifier,
                "pushover": self.pushover_notifier,
                "sms": self.sms_notifier,
            },
        )
        self.main_window.show()

        # --- Start background services ---
        self.instance_manager.start_all()
        self.dispatcher.start()
        self.bulletin_forwarder.start()
        self.bulletin_server.start()
        if self.aprs_gateway and self.config.aprs.enabled:
            self.aprs_gateway.start()
        if self.cms_poller:
            self.cms_poller.start()
        if self.inbound_email:
            self.inbound_email.start()      # polls only while enabled

        # --- Subscribe to config changes for live-updatable settings ---
        # vara_cmd_log can be toggled without a restart
        self.event_bus.subscribe(
            EventType.CONFIG_CHANGED,
            lambda e: update_vara_cmd_logging((e.data or {}).get("config", self.config).logging)
        )
        # Update CMS poller config on changes
        self.event_bus.subscribe(
            EventType.CONFIG_CHANGED,
            self._on_config_changed_cms,
        )
        # Update security policy on config changes
        self.event_bus.subscribe(
            EventType.CONFIG_CHANGED,
            self._on_config_changed_security,
        )
        # Bulletin forwarding: the server listens again if its settings
        # changed (neighbours are read live)
        self.event_bus.subscribe(
            EventType.CONFIG_CHANGED,
            lambda e: (e.data or {}).get("config") and self.bulletin_server.apply_config(),
        )
        # APRS-IS: connect when turned on, disconnect when turned off
        self.event_bus.subscribe(
            EventType.CONFIG_CHANGED,
            self._on_config_changed_aprs,
        )
        # Inbound email and the Winlink forward list
        self.event_bus.subscribe(
            EventType.CONFIG_CHANGED,
            self._on_config_changed_email,
        )
        # Restart instances whose rig_control or transport changed
        self.event_bus.subscribe(
            EventType.CONFIG_CHANGED,
            self._on_config_changed_instances,
        )

        # --- Software updater ---
        from core.updater import Updater
        self.updater = Updater(
            repo_dir=install_dir(),
            event_bus=self.event_bus,
            config=self.config.updater,
        )
        self.main_window.set_updater(self.updater, restart_callback=self.restart)
        self.event_bus.subscribe(
            EventType.CONFIG_CHANGED,
            lambda e: (e.data or {}).get("config") and
                      self.updater.update_config((e.data or {})["config"].updater),
        )
        if self.config.updater.check_on_startup:
            self.updater.check_once_async()
        if self.config.updater.auto_check_interval_hours > 0:
            self.updater.start_periodic_check()

        log.info("All subsystems started")
        return app.exec()

    def _apply_winlink_config(self, cfg):
        """Push Winlink config into the Pat client."""
        if not self.cms_poller:
            return
        wl = cfg.winlink
        self.cms_poller.update_config(
            enabled=wl.enabled,
            callsign=wl.gateway_callsign,
            password=wl.decrypt_gateway_password(cfg.security.machine_secret),
            grid_square=wl.grid_square,
            pat_exe_path=wl.pat_exe_path,
            pat_http_addr=wl.pat_http_addr,
            auto_launch_pat=wl.auto_launch_pat,
            forward_calls=wl.forward_calls,
            cms_timeout=wl.cms_timeout,
        )

    def _apply_internet_email(self, cfg):
        """Push the inbound email and Winlink forward-list settings in
        (at startup, and live on every Save)."""
        wl, od = cfg.winlink, cfg.outbound_delivery
        self.forward_manager.update_config(
            gateway_callsign=wl.gateway_callsign,
            manual_calls=wl.forward_calls,
            mode=od.winlink_forward.mode,
            max_calls=od.winlink_forward.max_calls,
        )
        ie = od.inbound_email
        if ie == self._inbound_applied:
            return          # unchanged: keep the poll timer as it is
        self._inbound_applied = ie
        self.inbound_email.update_config(
            enabled=ie.enabled,
            protocol=ie.protocol,
            host=ie.host,
            port=ie.port,
            use_ssl=ie.use_ssl,
            username=ie.username,
            password=ie.decrypt_password(cfg.security.machine_secret),
            folder=ie.folder,
            bbs_domain=ie.bbs_domain,
            poll_interval=ie.poll_interval,
            delete_after_fetch=ie.delete_after_fetch,
            max_message_size=ie.max_message_size,
            allowed_senders=ie.allowed_senders,
        )

    def _on_config_changed_aprs(self, event):
        cfg = (event.data or {}).get("config")
        if not cfg or not self.aprs_gateway:
            return
        if cfg.aprs.enabled and not self.aprs_gateway.running:
            self.aprs_gateway.start()
        elif not cfg.aprs.enabled and self.aprs_gateway.running:
            self.aprs_gateway.stop()

    def _on_config_changed_email(self, event):
        cfg = (event.data or {}).get("config")
        if cfg and self.inbound_email:
            self._apply_internet_email(cfg)

    def _on_config_changed_cms(self, event):
        """Update Pat client when config changes."""
        cfg = (event.data or {}).get("config")
        if cfg:
            self._apply_winlink_config(cfg)

    def _on_config_changed_instances(self, event):
        """Restart instances whose rig_control or transport settings changed."""
        cfg = (event.data or {}).get("config")
        if cfg and self.instance_manager:
            self.instance_manager.update_config(cfg)

    def _on_config_changed_security(self, event):
        """Update mode policy and rate limiter when config changes."""
        cfg = (event.data or {}).get("config")
        if cfg:
            if self.mode_policy:
                self.mode_policy.update_config(cfg)
            if self.rate_limiter and self.mode_policy:
                self.rate_limiter.multiplier = self.mode_policy.rate_limit_multiplier

    @staticmethod
    def _quit_app():
        # exit(), not quit(): in Qt 6 quit() first asks each window to
        # close, and the main window's "Stop the BBS and quit?" box would
        # block a SIGTERM shutdown (the window's own Quit asked already)
        app = QApplication.instance()
        if app:
            app.exit(0)

    def restart(self, relaunch: bool = True):
        """Quit for an update; main() shuts down and, if *relaunch*, starts
        the BBS again.  relaunch=False: an installer the updater started
        brings the new version up itself."""
        log.info("Restart requested — shutting down for update")
        self._relaunch = relaunch
        self._quit_app()

    def relaunch_if_requested(self):
        if not self._relaunch:
            return
        import subprocess as _sp
        from core.paths import is_frozen
        # A built app's sys.argv[0] is the program itself
        args = [sys.executable] + (sys.argv[1:] if is_frozen() else sys.argv)
        _sp.Popen(args)

    def _shutdown(self):
        with self._shutdown_lock:
            if self._shutdown_done:
                return
            self._shutdown_done = True
        log.info("Shutdown initiated")
        try:
            if self.updater:
                self.updater.stop()
            if self.dispatcher:
                self.dispatcher.stop()
            if self.bulletin_forwarder:
                self.bulletin_forwarder.stop()
            if self.bulletin_server:
                self.bulletin_server.stop()
            if self.aprs_gateway:
                self.aprs_gateway.stop()
            if self.cms_poller:
                self.cms_poller.stop()
            if self.inbound_email:
                self.inbound_email.stop()
            if self.instance_manager:
                self.instance_manager.stop_all()
            if self.message_store:
                self.message_store.close()
            log.info("Shutdown complete")
        except Exception as e:
            log.error(f"Error during shutdown: {e}")


def self_test() -> int:
    """``--self-test``: load every part of the BBS and its GUI, then exit.

    The installer builds run this to catch modules PyInstaller left out."""
    import importlib
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from core.version import get_version
    packages = {
        "aether": "aprs control launcher monitor websocket",
        "aprs": "gateway",
        "ax25": "parser router",
        "bbs": "access bulletins converse dispatcher filestore interactive "
               "mode_policy rate_limiter store",
        "core": "config crypto events logging_manager paths reconnect updater util version",
        "delivery": "config inbound",
        "gui": "aether_heard bbs_panel bulletins_tab chat_window delivery_tabs "
               "files_tab log_viewer mainwindow message_viewer messages_tab "
               "modem_panel packet_monitor session_sounds settings theme "
               "update_dialog winlink_panel",
        "notifications": "ntfy pushover sms",
        "transport": "agwpe agwpe_broker protocol",
        "vara": "broker flrig instance_manager interface mux omnirig process ptt "
                "rigcontrol",
        "winlink": "notify pat_client queue",
    }
    for pkg, names in packages.items():
        for name in names.split():
            importlib.import_module(f"{pkg}.{name}")
    app = QApplication.instance() or QApplication(sys.argv[:1])
    from gui.theme import apply_theme
    apply_theme(app)
    version = get_version()
    if version == "0.0.0":
        raise SystemExit("VERSION file missing from the build")
    print(f"Aether BBS {version} self-test OK")
    return 0


def main():
    if "--self-test" in sys.argv:
        sys.exit(self_test())

    # Installed copies keep messages, files and logs in the profile folder
    from core.paths import prepare_data_dir
    prepare_data_dir()

    # High-DPI support
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )

    app = QApplication(sys.argv)
    app.setApplicationName("Aether BBS")
    app.setOrganizationName("Amateur Radio")
    from core.version import get_version
    app.setApplicationVersion(get_version())

    from gui.theme import apply_theme
    apply_theme(app)

    from core.paths import resource_dir
    icon = resource_dir() / "packaging" / "aether-bbs.png"
    if icon.is_file():
        from PyQt6.QtGui import QIcon
        app.setWindowIcon(QIcon(str(icon)))

    bbs_app = BBSApplication()
    rc = bbs_app.start(app)
    bbs_app._shutdown()
    bbs_app.relaunch_if_requested()
    sys.exit(rc)


if __name__ == "__main__":
    try:
        main()
    except Exception as _crash:
        import traceback
        from core.paths import data_dir
        crash_path = data_dir() / "crash.log"
        with open(crash_path, "w") as _f:
            traceback.print_exc(file=_f)
        # Also try to show a message box if Qt is alive
        try:
            from PyQt6.QtWidgets import QMessageBox, QApplication
            _app = QApplication.instance()
            if _app:
                QMessageBox.critical(
                    None, "Aether BBS — Fatal Error",
                    f"The application crashed at startup:\n\n{_crash}\n\n"
                    f"See crash.log for details."
                )
        except Exception:
            pass
        raise
