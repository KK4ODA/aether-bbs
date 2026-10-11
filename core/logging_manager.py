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

# Formatter constants
_MAIN_FORMAT   = "%(asctime)s  %(levelname)-8s  %(threadName)-20s  %(name)s  %(message)s"
_SIMPLE_FORMAT = "%(asctime)s  %(message)s"
_DATE_FORMAT   = "%Y-%m-%d %H:%M:%S"


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


def _ensure_log_dir(cfg: LoggingConfig) -> Path:
    """Create and return the log directory."""
    log_dir = Path(cfg.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir


def _setup_rotating_logger(
    name: str,
    filename: str,
    cfg: LoggingConfig,
    fmt_str: str = _SIMPLE_FORMAT,
) -> None:
    """Configure a dedicated rotating logger (non-propagating).

    Idempotent — removes existing handlers before adding new ones.
    """
    logger = logging.getLogger(name)

    for h in logger.handlers[:]:
        h.close()
        logger.removeHandler(h)

    log_dir = _ensure_log_dir(cfg)
    fmt = logging.Formatter(fmt_str, datefmt=_DATE_FORMAT)
    handler = logging.handlers.RotatingFileHandler(
        log_dir / filename,
        maxBytes=cfg.max_bytes,
        backupCount=cfg.backup_count,
        encoding="utf-8",
    )
    handler.setFormatter(fmt)
    logger.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    logger.propagate = False


def setup_logging(cfg: LoggingConfig) -> None:
    """Call once at startup to configure all handlers."""

    log_dir = _ensure_log_dir(cfg)
    level = getattr(logging, cfg.level.upper(), logging.INFO)
    fmt = logging.Formatter(_MAIN_FORMAT, datefmt=_DATE_FORMAT)

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
        log_dir / "aether_bbs.log",
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
        _setup_rotating_logger(PACKET_LOGGER_NAME, "packet_frames.log", cfg)

    # ── VARA FM raw command/event logger ────────────────────────────────────
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

    # Remove any existing file handlers
    for h in vara_logger.handlers[:]:
        h.close()
        vara_logger.removeHandler(h)

    if not cfg.vara_cmd_log:
        return  # disabled — logger has no handlers, all calls are silent no-ops

    _setup_rotating_logger(VARA_CMD_LOGGER_NAME, "vara_cmd.log", cfg)


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

    _setup_rotating_logger(CHAT_LOGGER_NAME, "chat.log", cfg)
