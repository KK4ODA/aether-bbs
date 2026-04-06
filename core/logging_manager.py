"""
core/logging_manager.py

Configures Python's logging framework:
  - Rotating file handler (from config)
  - Optional packet-frame log (separate file)
  - In-memory queue handler so the GUI can consume log records
"""

import logging
import logging.handlers
import queue
from pathlib import Path
from typing import Optional

from core.config import LoggingConfig


# Shared queue that the GUI log viewer reads from
gui_log_queue: queue.Queue = queue.Queue(maxsize=2000)


class GUIQueueHandler(logging.handlers.QueueHandler):
    """Pushes records into the shared GUI queue, dropping oldest if full."""

    def enqueue(self, record):
        try:
            self.queue.put_nowait(record)
        except queue.Full:
            try:
                self.queue.get_nowait()   # discard oldest
                self.queue.put_nowait(record)
            except Exception:
                pass


PACKET_LOGGER_NAME   = "packet"
VARA_CMD_LOGGER_NAME = "vara_cmd"
CHAT_LOGGER_NAME     = "chat"


def setup_logging(cfg: LoggingConfig) -> None:
    """Call once at startup to configure all handlers."""

    log_dir = Path(cfg.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    level = getattr(logging, cfg.level.upper(), logging.INFO)

    fmt = logging.Formatter(
        "%(asctime)s  %(levelname)-8s  %(threadName)-20s  %(name)s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # ── Root logger ─────────────────────────────────────────────────────────
    root = logging.getLogger()
    root.setLevel(level)

    # Console handler (for development)
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    console.setLevel(level)
    root.addHandler(console)

    # Rotating main log file
    main_file = logging.handlers.RotatingFileHandler(
        log_dir / "vara_fm_bbs.log",
        maxBytes=cfg.max_bytes,
        backupCount=cfg.backup_count,
        encoding="utf-8",
    )
    main_file.setFormatter(fmt)
    main_file.setLevel(level)
    root.addHandler(main_file)

    # GUI queue handler
    gui_handler = GUIQueueHandler(gui_log_queue)
    gui_handler.setFormatter(fmt)
    gui_handler.setLevel(level)
    root.addHandler(gui_handler)

    # ── Packet frame logger ─────────────────────────────────────────────────
    if cfg.packet_log:
        pkt_fmt = logging.Formatter(
            "%(asctime)s  %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        pkt_handler = logging.handlers.RotatingFileHandler(
            log_dir / "packet_frames.log",
            maxBytes=cfg.max_bytes,
            backupCount=cfg.backup_count,
            encoding="utf-8",
        )
        pkt_handler.setFormatter(pkt_fmt)

        pkt_logger = logging.getLogger(PACKET_LOGGER_NAME)
        pkt_logger.setLevel(logging.DEBUG)
        pkt_logger.addHandler(pkt_handler)
        pkt_logger.propagate = False   # don't also write to main log

    # ── VARA FM raw command/event logger ────────────────────────────────────
    # When enabled, every raw line exchanged on the VARA FM TCP command port
    # is written to vara_cmd.log.  Useful for diagnosing session-timeout issues
    # or any unexpected VARA FM behaviour without needing to run a packet sniffer.
    update_vara_cmd_logging(cfg)

    # ── Chat session logger ──────────────────────────────────────────────────
    update_chat_logging(cfg)

    logging.getLogger(__name__).info(
        f"Logging initialised: level={cfg.level}, dir={log_dir}"
    )


def update_vara_cmd_logging(cfg: LoggingConfig) -> None:
    """Enable or disable the VARA FM command log at runtime without restart.

    Called whenever settings are saved so the checkbox takes effect immediately.
    Safe to call multiple times — idempotent.
    """
    vara_logger = logging.getLogger(VARA_CMD_LOGGER_NAME)

    # Remove any existing file handlers so we don't accumulate duplicates
    for h in vara_logger.handlers[:]:
        h.close()
        vara_logger.removeHandler(h)

    if not cfg.vara_cmd_log:
        return  # disabled — logger has no handlers, all calls are silent no-ops

    log_dir = Path(cfg.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    vara_fmt = logging.Formatter(
        "%(asctime)s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    vara_handler = logging.handlers.RotatingFileHandler(
        log_dir / "vara_cmd.log",
        maxBytes=cfg.max_bytes,
        backupCount=cfg.backup_count,
        encoding="utf-8",
    )
    vara_handler.setFormatter(vara_fmt)
    vara_logger.setLevel(logging.DEBUG)
    vara_logger.addHandler(vara_handler)
    vara_logger.propagate = False

def update_chat_logging(cfg: LoggingConfig) -> None:
    """Enable or disable chat session logging at runtime without restart.

    Each chat session's lines are appended to chat.log in the log directory.
    Safe to call multiple times — idempotent.
    """
    chat_logger = logging.getLogger(CHAT_LOGGER_NAME)

    for h in chat_logger.handlers[:]:
        h.close()
        chat_logger.removeHandler(h)

    if not cfg.chat_log:
        return

    log_dir = Path(cfg.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    fmt = logging.Formatter(
        "%(asctime)s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    handler = logging.handlers.RotatingFileHandler(
        log_dir / "chat.log",
        maxBytes=cfg.max_bytes,
        backupCount=cfg.backup_count,
        encoding="utf-8",
    )
    handler.setFormatter(fmt)
    chat_logger.setLevel(logging.DEBUG)
    chat_logger.addHandler(handler)
    chat_logger.propagate = False
