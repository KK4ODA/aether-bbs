"""
winlink/pat_client.py

PatClient — Winlink integration via the Pat client application.

Pat (https://getpat.io) is a cross-platform Winlink client written in Go.
This module manages Pat's lifecycle and delegates all CMS protocol handling
(B2F, LZHUF compression, authentication) to Pat.

Communication model:
  - HTTP REST API  → inbox polling  (GET /api/mailbox/in)
  - CLI subprocess → compose + send (pat compose / pat connect)

Replaces the former CMSPoller + direct telnet/B2F approach.
"""

import json
import logging
import os
import random
import socket
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from urllib.error import URLError
from urllib.request import Request, urlopen

from bbs.store import MessageStore, MessageStatus
from core.events import EventBus, EventType

log = logging.getLogger(__name__)

# Retry parameters for failed CMS connections
MAX_RETRIES        = 3
BACKOFF_BASE_SEC   = 5.0
BACKOFF_JITTER     = 0.25

# Polling intervals
INBOX_CHECK_INTERVAL = 15  # seconds between local inbox checks

# Subprocess timeouts
COMPOSE_TIMEOUT   = 15    # seconds
TELNET_TIMEOUT    = 120   # seconds
RF_TIMEOUT        = 300   # seconds
API_TIMEOUT       = 10    # seconds


# ── Result types ────────────────────────────────────────────────────────────

@dataclass
class PollResult:
    """Outcome of a CMS poll cycle (replaces ExchangeResult)."""
    sent:     int            = 0
    received: int            = 0
    error:    Optional[str]  = None
    duration: float          = 0.0


@dataclass
class SendResult:
    """Outcome of a single message send."""
    success:  bool  = False
    error:    str   = ""
    duration: float = 0.0


# ── Pat Client ──────────────────────────────────────────────────────────────

class PatClient:
    """
    Manages the Pat Winlink client lifecycle, inbox polling, and message
    sending.

    Public interface is compatible with the former CMSPoller so that
    WinlinkBackend, WinlinkPanel, main.py, and CONFIG_CHANGED handlers
    work without structural changes.
    """

    def __init__(
        self,
        store:     MessageStore,
        event_bus: EventBus,
    ):
        self._store   = store
        self._bus     = event_bus
        self._lock    = threading.Lock()   # one CMS session at a time
        self._running = False
        self._poll_thread: Optional[threading.Thread] = None
        self._last_result: Optional[PollResult] = None
        self._queue:  Optional['OutboundQueue'] = None
        self._forward_manager = None

        # Pat process management
        self._pat_proc: Optional[subprocess.Popen] = None
        self._managed  = False  # True if we launched Pat (vs external)

        # Deduplication for inbox messages
        self._seen_ids: set[str] = set()
        self._seen_file = ""  # set in update_config once we know data dir

        # Configurable settings
        self._enabled         = False
        self._callsign        = ""
        self._password         = ""
        self._grid_square     = ""
        self._pat_exe_path    = ""
        self._pat_http_addr   = "localhost:8080"
        self._auto_launch_pat = True
        self._poll_interval   = 15    # minutes
        self._auto_poll       = True
        self._forward_calls: list[str] = []
        self._rf_fallback     = False
        self._rf_gateway      = ""

    # ── Configuration ────────────────────────────────────────────────────

    def update_config(
        self,
        enabled:         bool   = False,
        callsign:        str    = "",
        password:        str    = "",
        grid_square:     str    = "",
        pat_exe_path:    str    = "",
        pat_http_addr:   str    = "localhost:8080",
        auto_launch_pat: bool   = True,
        poll_interval:   int    = 15,
        auto_poll:       bool   = True,
        forward_calls:   Optional[list[str]] = None,
        rf_fallback:     bool   = False,
        rf_gateway:      str    = "",
    ):
        """Update Pat configuration.  Can be called while running."""
        self._enabled         = enabled
        self._callsign        = callsign.upper()
        self._password        = password
        self._grid_square     = grid_square
        self._pat_exe_path    = pat_exe_path
        self._pat_http_addr   = pat_http_addr or "localhost:8080"
        self._auto_launch_pat = auto_launch_pat
        self._poll_interval   = max(1, poll_interval)
        self._auto_poll       = auto_poll
        self._forward_calls   = forward_calls or []
        self._rf_fallback     = rf_fallback
        self._rf_gateway      = rf_gateway.upper()

        # Outbound queue — routing logic is transport-agnostic
        from winlink.queue import OutboundQueue
        if self._queue is None:
            self._queue = OutboundQueue(self._store, self._callsign)
        else:
            self._queue.set_local_callsign(self._callsign)

        # Dedup file lives next to the BBS data
        data_dir = Path(self._store.db_path).parent if hasattr(self._store, 'db_path') else Path(".")
        self._seen_file = str(data_dir / ".pat_seen_ids")
        self._load_seen_ids()

    # ── Lifecycle ────────────────────────────────────────────────────────

    def start(self):
        """Start the Pat client and polling service."""
        self._running = True

        if self._enabled:
            # Validate Pat installation
            if not self._pat_exe_path:
                log.warning("Winlink enabled but Pat executable path is empty — "
                            "configure it in Settings > Winlink")
            elif not os.path.isfile(self._pat_exe_path):
                log.warning("Pat executable not found at: %s", self._pat_exe_path)
                self._bus.emit(
                    EventType.CMS_SESSION_ERROR, source="winlink",
                    data={"error": f"Pat not found at {self._pat_exe_path}"},
                )
            else:
                # Configure Pat's own config.json
                if self._auto_launch_pat:
                    self._configure_pat()

                # Launch Pat if configured
                self._launch_pat()

            # Start polling thread
            if self._auto_poll:
                self._poll_thread = threading.Thread(
                    target=self._poll_loop, daemon=True, name="Pat-Poller",
                )
                self._poll_thread.start()

        log.info(
            "Pat client started (enabled=%s, interval=%dmin, exe=%s)",
            self._enabled, self._poll_interval,
            self._pat_exe_path or "<not set>",
        )

    def stop(self):
        """Stop the polling service and managed Pat process."""
        self._running = False

        # Terminate Pat only if we launched it
        if self._managed and self._pat_proc and self._pat_proc.poll() is None:
            log.info("Stopping managed Pat process (PID %d)", self._pat_proc.pid)
            self._pat_proc.terminate()
            try:
                self._pat_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._pat_proc.kill()
            self._pat_proc = None
            self._managed = False

        log.info("Pat client stopped")

    # ── Public polling interface (compatible with CMSPoller) ─────────────

    def poll_now(self) -> None:
        """Trigger an immediate CMS sync.  Returns immediately (async)."""
        if not self._enabled:
            log.warning("Pat poll requested but Winlink is disabled")
            return
        t = threading.Thread(
            target=self._do_poll, daemon=True, name="Pat-Poll-Manual",
        )
        t.start()

    def poll_now_blocking(self) -> Optional[PollResult]:
        """Trigger an immediate CMS sync, blocking until complete."""
        if not self._enabled:
            return None
        return self._do_poll()

    def send_message(self, to_addr: str, subject: str, body: str,
                     from_addr: str = "") -> SendResult:
        """
        Send a single message via Pat compose + CMS sync.

        Args:
            to_addr:  Recipient (callsign or email)
            subject:  Message subject
            body:     Message body text
            from_addr: Optional sender override (tactical address)

        Returns SendResult with success/error.
        """
        start = time.monotonic()

        # Compose the message in Pat's outbox
        compose_result = self._compose_message(to_addr, subject, body, from_addr)
        if not compose_result.success:
            return compose_result

        # Sync with CMS to actually push it
        synced = self._sync_cms()
        duration = time.monotonic() - start

        if synced:
            return SendResult(success=True, duration=duration)
        else:
            # Message is queued in Pat even if sync failed — it'll go next time
            return SendResult(
                success=True,
                error="Message queued in Pat but CMS sync failed — will retry",
                duration=duration,
            )

    @property
    def is_enabled(self) -> bool:
        return self._enabled

    @property
    def is_polling(self) -> bool:
        return self._lock.locked()

    @property
    def last_result(self) -> Optional[PollResult]:
        return self._last_result

    @property
    def outbound_queue(self) -> Optional['OutboundQueue']:
        return self._queue

    @property
    def pat_running(self) -> bool:
        """True if Pat HTTP API is reachable."""
        return self._probe_pat_http()

    def set_forward_manager(self, mgr) -> None:
        """Inject the WinlinkForwardManager for dynamic ;FW list building."""
        self._forward_manager = mgr

    # ── Pat process management ───────────────────────────────────────────

    def _launch_pat(self):
        """Launch Pat if auto_launch is enabled and Pat isn't already running."""
        if not self._auto_launch_pat:
            log.info("Pat auto-launch disabled")
            return

        if not self._pat_exe_path or not os.path.isfile(self._pat_exe_path):
            return

        # Check if Pat is already running externally
        if self._probe_pat_http():
            log.info("Pat already running on %s (external instance)", self._pat_http_addr)
            self._managed = False
            return

        try:
            cmd = [self._pat_exe_path, "http", "--addr", self._pat_http_addr]
            log.info("Launching Pat: %s", " ".join(cmd))
            self._pat_proc = subprocess.Popen(
                cmd,
                cwd=os.path.dirname(self._pat_exe_path),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self._managed = True
            time.sleep(3)

            if self._pat_proc.poll() is not None:
                log.error(
                    "Pat exited immediately (code %d) — check Pat configuration",
                    self._pat_proc.returncode,
                )
                self._pat_proc = None
                self._managed = False
            else:
                log.info("Pat running on %s (PID %d)", self._pat_http_addr, self._pat_proc.pid)

        except Exception as e:
            log.error("Failed to launch Pat: %s", e)
            self._pat_proc = None
            self._managed = False

    def _probe_pat_http(self) -> bool:
        """Quick TCP probe to check if Pat HTTP server is reachable."""
        try:
            host, port_str = self._pat_http_addr.rsplit(":", 1)
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(2)
            s.connect(("127.0.0.1", int(port_str)))
            s.close()
            return True
        except (ConnectionRefusedError, OSError, socket.timeout, ValueError):
            return False

    def _restart_pat(self):
        """Stop and restart the managed Pat process."""
        if self._managed and self._pat_proc and self._pat_proc.poll() is None:
            log.info("Restarting Pat (PID %d)", self._pat_proc.pid)
            self._pat_proc.terminate()
            try:
                self._pat_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._pat_proc.kill()
            self._pat_proc = None
        self._launch_pat()

    # ── Pat config management ────────────────────────────────────────────

    def _pat_config_path(self) -> str:
        """Find Pat's config.json on the local system."""
        candidates = []
        localappdata = os.environ.get("LOCALAPPDATA", "")
        if localappdata:
            candidates.append(os.path.join(localappdata, "pat", "config.json"))
        home = os.path.expanduser("~")
        candidates.append(os.path.join(home, ".wl2k", "config.json"))
        if self._pat_exe_path:
            candidates.append(os.path.join(os.path.dirname(self._pat_exe_path), "config.json"))
        for p in candidates:
            if os.path.isfile(p):
                return p
        return candidates[0] if candidates else ""

    def _configure_pat(self):
        """
        Write BBS settings into Pat's own config.json.
        Only called when auto_launch_pat is True (we own the instance).
        """
        path = self._pat_config_path()
        if not path:
            return

        try:
            if os.path.isfile(path):
                with open(path, "r") as f:
                    pcfg = json.load(f)
            else:
                pcfg = {}

            # Set core fields
            if self._callsign:
                pcfg["mycall"] = self._callsign
            if self._password:
                pcfg["secure_login_password"] = self._password
            if self._grid_square:
                pcfg["locator"] = self._grid_square

            # Set HTTP address
            pcfg["http_addr"] = self._pat_http_addr

            # Set forward callsigns as auxiliary addresses
            fwd = (
                self._forward_manager.build_forward_list()
                if self._forward_manager
                else self._forward_calls
            )
            if fwd:
                pcfg["auxiliary_addresses"] = fwd

            # Ensure telnet connection alias exists
            aliases = pcfg.get("connect_aliases", {})
            if "telnet" not in aliases:
                call = self._callsign or "NOCALL"
                aliases["telnet"] = (
                    f"telnet:///{call}:CMSTelnet@cms.winlink.org:8772/wl2k"
                )
                pcfg["connect_aliases"] = aliases

            # Clear Pat's own schedule — we manage sync timing
            pcfg["schedule"] = {}

            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                json.dump(pcfg, f, indent=2)
            log.info("Pat config written to %s", path)

        except Exception as e:
            log.warning("Failed to configure Pat: %s", e)

    # ── Inbox polling via HTTP API ───────────────────────────────────────

    def _pat_api_url(self) -> str:
        return f"http://{self._pat_http_addr}/api"

    def _check_inbox(self) -> int:
        """
        Poll Pat's local mailbox for new messages.
        Returns the number of new messages found.
        """
        try:
            url = self._pat_api_url() + "/mailbox/in"
            req = Request(url)
            resp = urlopen(req, timeout=API_TIMEOUT)
            raw = resp.read().decode()
            data = json.loads(raw)

            if not data:
                return 0

            new_count = 0
            for msg in data:
                mid = (
                    msg.get("MID", "") or msg.get("mid", "")
                    or msg.get("Id", "") or msg.get("id", "")
                )
                if not mid:
                    mid = f"{msg.get('Subject', msg.get('subject', ''))}_{msg.get('Date', msg.get('date', ''))}"

                if mid in self._seen_ids:
                    continue

                self._seen_ids.add(mid)
                self._save_seen_ids()

                # Parse message fields (Pat uses inconsistent casing)
                subject = msg.get("Subject", "") or msg.get("subject", "")
                from_field = msg.get("From", msg.get("from", ""))
                if isinstance(from_field, dict):
                    from_addr = from_field.get("Addr", "") or from_field.get("addr", "")
                elif isinstance(from_field, str):
                    from_addr = from_field
                else:
                    from_addr = str(from_field) if from_field else ""

                body_text = msg.get("Body", "") or msg.get("body", "")
                date_str = msg.get("Date", "") or msg.get("date", "")

                to_field = msg.get("To", msg.get("to", ""))
                if isinstance(to_field, list) and to_field:
                    to_addr = to_field[0].get("Addr", "") if isinstance(to_field[0], dict) else str(to_field[0])
                elif isinstance(to_field, dict):
                    to_addr = to_field.get("Addr", "") or to_field.get("addr", "")
                elif isinstance(to_field, str):
                    to_addr = to_field
                else:
                    to_addr = self._callsign

                log.info(
                    "Pat inbox: MID=%s From=%s Subject=%s",
                    mid[:20], from_addr, subject[:40],
                )

                # Store in BBS message store as an inbound message
                try:
                    self._store.save_message(
                        msg_id=mid,
                        from_call=from_addr,
                        to_call=to_addr,
                        subject=subject,
                        body=body_text,
                        msg_type="P",  # Private
                        is_sent=False,
                    )
                except Exception as e:
                    log.warning("Failed to store inbound Winlink message %s: %s", mid, e)
                    continue

                new_count += 1

                # Emit event for GUI / notifications
                self._bus.emit(
                    EventType.CMS_MESSAGE_RECEIVED,
                    source="winlink",
                    data={
                        "mid": mid,
                        "from": from_addr,
                        "to": to_addr,
                        "subject": subject,
                    },
                )

            return new_count

        except Exception as e:
            err_str = str(e)
            if "10061" in err_str or "Connection refused" in err_str:
                log.debug("Pat not reachable — inbox check skipped")
            elif "timed out" in err_str.lower():
                log.debug("Pat not responding — inbox check skipped")
            else:
                log.warning("Pat inbox poll failed: %s", err_str[:200])
            return 0

    # ── Message sending via CLI ──────────────────────────────────────────

    def _compose_message(self, to_addr: str, subject: str, body: str,
                         from_addr: str = "") -> SendResult:
        """Queue a message in Pat's outbox via `pat compose`."""
        start = time.monotonic()

        if not self._pat_exe_path or not os.path.isfile(self._pat_exe_path):
            return SendResult(error="Pat executable not configured")

        if not from_addr:
            from_addr = self._callsign

        cmd = [self._pat_exe_path, "compose"]
        if from_addr:
            cmd.extend(["--from", from_addr])
        cmd.extend(["--subject", subject, to_addr])

        try:
            log.info("Pat compose: %s", " ".join(cmd))
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=os.path.dirname(self._pat_exe_path),
            )
            stdout, stderr = proc.communicate(
                input=body.encode("utf-8"), timeout=COMPOSE_TIMEOUT,
            )
            duration = time.monotonic() - start

            if proc.returncode != 0:
                err = (stderr.decode().strip()
                       or stdout.decode().strip()
                       or f"Exit code {proc.returncode}")
                log.error("Pat compose failed: %s", err)
                return SendResult(error=err, duration=duration)

            log.info("Message queued in Pat to %s: %s", to_addr, subject[:40])
            return SendResult(success=True, duration=duration)

        except subprocess.TimeoutExpired:
            proc.kill()
            return SendResult(error="Pat compose timed out", duration=time.monotonic() - start)
        except Exception as e:
            log.error("Pat compose failed: %s", e)
            return SendResult(error=str(e), duration=time.monotonic() - start)

    # ── CMS sync ─────────────────────────────────────────────────────────

    def _sync_cms(self) -> bool:
        """
        Trigger Pat to connect to Winlink CMS.
        Uses telnet by default; falls back to VARA FM RF gateway if
        no internet and rf_fallback is configured.
        """
        if not self._pat_exe_path or not os.path.isfile(self._pat_exe_path):
            return False

        use_rf = False
        if self._rf_fallback and self._rf_gateway and not self._check_internet():
            use_rf = True
            log.info("No internet — Pat connecting via VARA FM to %s", self._rf_gateway)

        if use_rf:
            connect_url = f"varafm:///{self._rf_gateway}"
            cmd = [self._pat_exe_path, "connect", connect_url]
            timeout = RF_TIMEOUT
        else:
            cmd = [self._pat_exe_path, "connect", "telnet"]
            timeout = TELNET_TIMEOUT

        try:
            log.info("Pat sync: %s", " ".join(cmd))
            proc = subprocess.run(
                cmd,
                cwd=os.path.dirname(self._pat_exe_path),
                capture_output=True,
                timeout=timeout,
            )
            if proc.returncode == 0:
                log.info("Pat session completed (%s)", "RF" if use_rf else "telnet")
                return True
            log.warning("Pat connect failed: %s", proc.stderr.decode()[:200])
            return False

        except subprocess.TimeoutExpired:
            log.warning("Pat session timed out (%ds)", timeout)
            return False
        except Exception as e:
            log.warning("Pat connect failed: %s", e)
            return False

    @staticmethod
    def _check_internet(timeout: float = 2.0) -> bool:
        """Quick TCP probe to Google DNS to check internet availability."""
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(timeout)
            s.connect(("8.8.8.8", 53))
            s.close()
            return True
        except Exception:
            return False

    # ── Poll loop ────────────────────────────────────────────────────────

    def _poll_loop(self):
        """
        Background thread: full CMS sync on interval, local inbox check
        every 15 seconds between syncs.
        """
        while self._running:
            if not self._enabled:
                time.sleep(5)
                continue

            # Full CMS sync
            self._do_poll()

            # Between syncs, keep checking local Pat inbox frequently
            sync_interval = max(60, self._poll_interval * 60)
            elapsed = 0
            while elapsed < sync_interval and self._running and self._enabled:
                time.sleep(INBOX_CHECK_INTERVAL)
                elapsed += INBOX_CHECK_INTERVAL
                self._check_inbox()

    def _do_poll(self) -> Optional[PollResult]:
        """Execute one full CMS sync + inbox check cycle with retry logic."""
        if not self._lock.acquire(blocking=False):
            log.debug("Pat poll skipped — session already in progress")
            return None

        try:
            start = time.monotonic()

            self._bus.emit(
                EventType.CMS_POLL_STARTED,
                source="winlink",
                data={"callsign": self._callsign, "host": "pat"},
            )

            # Check if Pat is alive; try restart if we own it
            if not self._probe_pat_http():
                if self._managed or self._auto_launch_pat:
                    log.info("Pat not responding — attempting restart")
                    self._launch_pat()
                    if not self._probe_pat_http():
                        result = PollResult(error="Pat not running")
                        self._last_result = result
                        self._bus.emit(
                            EventType.CMS_POLL_COMPLETED,
                            source="winlink",
                            data={"error": result.error, "sent": 0, "received": 0, "duration": 0},
                        )
                        return result

            result = self._exchange_with_retry()
            result.duration = time.monotonic() - start
            self._last_result = result

            self._bus.emit(
                EventType.CMS_POLL_COMPLETED,
                source="winlink",
                data={
                    "sent":     result.sent,
                    "received": result.received,
                    "error":    result.error,
                    "duration": result.duration,
                },
            )

            return result

        finally:
            self._lock.release()

    def _exchange_with_retry(self) -> PollResult:
        """Run CMS sync + inbox check with exponential backoff retry."""
        last_error = None

        for attempt in range(MAX_RETRIES):
            try:
                # Send queued outbound messages
                sent = self._send_queued_messages()

                # Sync with CMS
                synced = self._sync_cms()

                # Check inbox for new messages
                received = self._check_inbox()

                if synced:
                    return PollResult(sent=sent, received=received)
                else:
                    last_error = "CMS sync failed"
                    # Still return partial results if we got inbox messages
                    if received > 0 or sent > 0:
                        return PollResult(sent=sent, received=received, error=last_error)

            except Exception as e:
                last_error = str(e)

            # Retry with backoff
            if attempt < MAX_RETRIES - 1:
                delay = BACKOFF_BASE_SEC * (2 ** attempt)
                delay += random.uniform(0, delay * BACKOFF_JITTER)
                log.warning(
                    "Pat attempt %d/%d failed: %s. Retrying in %.1fs",
                    attempt + 1, MAX_RETRIES, last_error, delay,
                )
                time.sleep(delay)

        log.error("Pat exchange failed after %d attempts: %s", MAX_RETRIES, last_error)
        return PollResult(error=last_error)

    def _send_queued_messages(self) -> int:
        """Send all queued outbound messages through Pat."""
        if not self._queue:
            return 0

        messages = self._queue.get_cms_outbound(limit=25)
        if not messages:
            return 0

        sent = 0
        for msg in messages:
            mid = msg.get("id", "")
            to_call = msg.get("to_call", "")
            subject = msg.get("subject", "")
            body = msg.get("body", "")

            self._queue.mark_sending(mid)

            result = self._compose_message(to_call, subject, body)
            if result.success:
                self._queue.mark_sent(mid)
                sent += 1
                self._bus.emit(
                    EventType.CMS_MESSAGE_SENT,
                    source="winlink",
                    data={"mid": mid, "to": to_call, "subject": subject},
                )
            else:
                self._queue.mark_failed(mid, result.error)

        return sent

    # ── Seen-IDs persistence ─────────────────────────────────────────────

    def _load_seen_ids(self):
        """Load previously seen message IDs from disk."""
        if self._seen_file and os.path.isfile(self._seen_file):
            try:
                with open(self._seen_file, "r") as f:
                    self._seen_ids = set(
                        line.strip() for line in f if line.strip()
                    )
            except Exception:
                pass

    def _save_seen_ids(self):
        """Persist seen message IDs to disk."""
        if not self._seen_file:
            return
        try:
            with open(self._seen_file, "w") as f:
                for mid in self._seen_ids:
                    f.write(mid + "\n")
        except Exception:
            pass

    # ── Pat installation check ───────────────────────────────────────────

    @staticmethod
    def find_pat_exe() -> str:
        """
        Attempt to locate pat.exe on the system.
        Checks common installation paths and PATH.
        Returns the path if found, or empty string.
        """
        # Check PATH
        import shutil
        pat_in_path = shutil.which("pat")
        if pat_in_path:
            return pat_in_path

        # Common Windows installation paths
        candidates = []
        for base in [
            os.environ.get("PROGRAMFILES", r"C:\Program Files"),
            os.environ.get("LOCALAPPDATA", ""),
            os.path.expanduser("~"),
            r"C:\Pat",
        ]:
            if base:
                candidates.append(os.path.join(base, "pat", "pat.exe"))
                candidates.append(os.path.join(base, "Pat", "pat.exe"))
                candidates.append(os.path.join(base, "pat.exe"))

        for p in candidates:
            if os.path.isfile(p):
                return p

        return ""

    @staticmethod
    def get_pat_version(exe_path: str) -> str:
        """Get Pat version string, or empty string on failure."""
        if not exe_path or not os.path.isfile(exe_path):
            return ""
        try:
            result = subprocess.run(
                [exe_path, "version"],
                capture_output=True, timeout=5,
            )
            return result.stdout.decode().strip()
        except Exception:
            return ""

    def get_nearby_gateways(self, limit: int = 20) -> list[dict]:
        """
        Query Pat for nearby VARA FM RMS gateways.
        Returns list of dicts with callsign and distance info.
        """
        if not self._pat_exe_path or not os.path.isfile(self._pat_exe_path):
            return []
        try:
            result = subprocess.run(
                [self._pat_exe_path, "rmslist", "-s", "-m", "varafm"],
                capture_output=True, timeout=30,
                cwd=os.path.dirname(self._pat_exe_path),
            )
            if result.returncode != 0:
                return []

            gateways = []
            for line in result.stdout.decode().splitlines():
                parts = line.strip().split()
                if len(parts) >= 2:
                    gateways.append({
                        "callsign": parts[0],
                        "info": " ".join(parts[1:]),
                    })
                if len(gateways) >= limit:
                    break
            return gateways

        except Exception as e:
            log.warning("Failed to list Pat gateways: %s", e)
            return []
