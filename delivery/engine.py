"""
delivery/engine.py

DeliveryEngine — deterministic failover state machine for outbound
message delivery across all three tiers.

Responsibilities:
- Route messages to the appropriate backend based on recipient type
- Execute delivery attempts with per-provider exponential backoff
- Persist failover state across application restarts (via SQLite)
- Emit backend-abstracted events for GUI and logging
- Enforce message retention until delivery or expiration

The engine runs on a background thread, processing the delivery queue
at configurable intervals.  All state transitions are logged.
"""

import logging
import random
import threading
from datetime import datetime, timezone, timedelta
from typing import Optional

from delivery.models import (
    DeliveryBackendType,
    DeliveryStatus,
    DeliveryResult,
    FailoverEvent,
    FailoverEventType,
    MessageDeliveryState,
)
from delivery.backends import (
    OutboundDeliveryBackend,
    WinlinkBackend,
    SMTPBackend,
)
from bbs.store import MessageStore
from core.events import EventBus, EventType

log = logging.getLogger(__name__)

# Backoff parameters
BACKOFF_BASE_SEC    = 5.0
BACKOFF_MAX_SEC     = 3600.0   # 1 hour max backoff
BACKOFF_JITTER      = 0.25     # ±25%

# Queue processing
DEFAULT_POLL_SEC    = 30       # how often the engine checks the queue

# Message expiration
DEFAULT_EXPIRY_DAYS = 7


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _compute_backoff(attempt: int) -> float:
    """Exponential backoff with jitter, capped at BACKOFF_MAX_SEC."""
    delay = min(BACKOFF_BASE_SEC * (2 ** attempt), BACKOFF_MAX_SEC)
    jitter = random.uniform(-delay * BACKOFF_JITTER, delay * BACKOFF_JITTER)
    return max(1.0, delay + jitter)


class DeliveryEngine:
    """
    Deterministic failover engine for outbound message delivery.

    Routing logic:
      - Callsign recipients → prefer Winlink backend
      - Email addresses (@) → prefer SMTP backend
      - Sysop override rules take precedence

    Failover order:
      SMTP Provider #1 → #2 → #3 → … → Local MTA → Winlink (for email)
      Winlink (for callsigns) → (no SMTP fallback for RF-only addresses)

    State persistence:
      Each message's delivery state is stored as JSON in the
      delivery_state column of the messages table.  This survives
      application restarts with full failover position recovery.
    """

    def __init__(
        self,
        store:     MessageStore,
        event_bus: EventBus,
    ):
        self._store = store
        self._bus = event_bus

        # Backends (initialised but not configured)
        self._winlink = WinlinkBackend()
        self._smtp = SMTPBackend()

        # Backend registry — order matters for fallback
        self._backends: dict[DeliveryBackendType, OutboundDeliveryBackend] = {
            DeliveryBackendType.WINLINK:   self._winlink,
            DeliveryBackendType.SMTP:      self._smtp,
        }

        # Sysop routing overrides: {recipient_pattern: backend_type}
        self._routing_overrides: dict[str, DeliveryBackendType] = {}

        # Engine state
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._poll_interval = DEFAULT_POLL_SEC
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._failover_log: list[FailoverEvent] = []

        # Per-message expiration
        self._expiry_days = DEFAULT_EXPIRY_DAYS

    # ── Properties ───────────────────────────────────────────────────────────

    # ── Configuration ────────────────────────────────────────────────────────

    def update_config(self, config) -> None:
        """
        Apply configuration from AppConfig.
        Called on startup and on CONFIG_CHANGED events.
        """
        # Winlink backend config
        wl = getattr(config, "winlink", None)
        if wl:
            self._winlink.update_config(
                enabled=wl.enabled,
                callsign=wl.gateway_callsign,
                pat_http_addr=wl.pat_http_addr,
            )

        # Outbound delivery config
        od = getattr(config, "outbound_delivery", None)
        if od:
            self._poll_interval = getattr(od, "engine_poll_seconds", DEFAULT_POLL_SEC)
            self._expiry_days = getattr(od, "message_expiry_days", DEFAULT_EXPIRY_DAYS)

            # SMTP providers
            smtp_cfg = getattr(od, "smtp", None)
            if smtp_cfg:
                providers_list = []
                for p in getattr(smtp_cfg, "providers", []):
                    providers_list.append({
                        "provider_id":      p.provider_id,
                        "display_name":     p.display_name,
                        "host":             p.host,
                        "port":             p.port,
                        "tls_mode":         p.tls_mode,
                        "username":         p.username,
                        "password":         p.password,
                        "sender_email":     p.sender_email,
                        "sender_name":      p.sender_name,
                        "enabled":          p.enabled,
                        "priority":         p.priority,
                        "failure_threshold": p.failure_threshold,
                        "max_attempts":     p.max_attempts,
                    })
                self._smtp.update_config(
                    enabled=smtp_cfg.enabled,
                    providers=providers_list,
                )

            # Routing overrides
            overrides = getattr(od, "routing_overrides", [])
            self._routing_overrides = {}
            for ov in overrides:
                pattern = getattr(ov, "pattern", "")
                backend = getattr(ov, "backend", "")
                if pattern and backend:
                    try:
                        self._routing_overrides[pattern] = DeliveryBackendType(backend)
                    except ValueError:
                        pass

    def set_cms_poller(self, poller) -> None:
        """Inject the PatClient for Winlink backend delivery."""
        self._winlink.set_poller(poller)

    # ── Engine lifecycle ─────────────────────────────────────────────────────

    def start(self) -> None:
        """Start the delivery engine background thread."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._run_loop,
            daemon=True,
            name="DeliveryEngine",
        )
        self._thread.start()
        log.info("Delivery engine started")

    def stop(self) -> None:
        """Stop the delivery engine."""
        self._running = False
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=10)
            self._thread = None
        log.info("Delivery engine stopped")

    @property
    def is_running(self) -> bool:
        return self._running

    def _run_loop(self) -> None:
        """Main engine loop — processes the delivery queue periodically."""
        while self._running:
            try:
                self._process_queue()
            except Exception as e:
                log.error(f"Delivery engine error: {e}", exc_info=True)

            # Wait for next cycle — wakes immediately on stop()
            if self._stop_event.wait(timeout=self._poll_interval):
                break

    # ── Queue processing ─────────────────────────────────────────────────────

    def _process_queue(self) -> None:
        """Process all pending delivery messages."""
        pending = self._get_pending_messages()
        if not pending:
            return

        log.debug(f"Delivery engine processing {len(pending)} pending message(s)")

        for msg in pending:
            if not self._running:
                break
            self._attempt_delivery(msg)

        # Also check for expired messages
        self._expire_stale_messages()

    def _get_pending_messages(self) -> list[dict]:
        """Get messages that need delivery attempts."""
        try:
            with self._store._lock:
                cur = self._store._conn.execute(
                    """SELECT * FROM messages
                       WHERE delivery_status IN (?, ?, ?)
                         AND is_sent = 0
                       ORDER BY priority DESC, date ASC
                       LIMIT 50""",
                    (
                        DeliveryStatus.PENDING.value,
                        DeliveryStatus.BACKEND_FAILED.value,
                        DeliveryStatus.FAILOVER.value,
                    ),
                )
                return [dict(r) for r in cur.fetchall()]
        except Exception as e:
            log.error(f"Failed to query pending deliveries: {e}")
            return []

    def _attempt_delivery(self, message: dict) -> None:
        """
        Execute the delivery state machine for one message.
        """
        mid = message.get("id", "")

        # Load or create delivery state
        state = self._load_delivery_state(mid)

        # Check backoff — is this message in a cooldown period?
        if self._is_in_backoff(state):
            return

        # Route: determine which backend to use
        backend_type = state.selected_backend or self._route_message(message)
        if backend_type is None:
            log.debug(f"MID={mid}: no suitable backend found")
            return

        state.selected_backend = backend_type
        state.delivery_status = DeliveryStatus.ATTEMPTING
        self._save_delivery_state(mid, state)

        # Update message table status
        self._update_message_delivery_status(mid, DeliveryStatus.ATTEMPTING, backend_type)

        # Get the backend and attempt delivery
        backend = self._backends.get(backend_type)
        if not backend or not backend.is_enabled():
            # Backend disabled — try failover
            self._handle_failover(mid, message, state, backend_type,
                                  f"{backend_type.value} backend is disabled")
            return

        # Determine which provider to try within the backend
        provider_id = self._select_provider(backend, state)

        # Attempt delivery
        result = backend.deliver(message, provider_id=provider_id)

        if result.success:
            self._handle_success(mid, message, state, result)
        else:
            self._handle_failure(mid, message, state, result)

    # ── Routing ──────────────────────────────────────────────────────────────

    @staticmethod
    def _check_internet(timeout: float = 2.0) -> bool:
        """Quick TCP probe to check internet availability."""
        import socket
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(timeout)
            s.connect(("8.8.8.8", 53))
            s.close()
            return True
        except Exception:
            return False

    def _route_message(self, message: dict) -> Optional[DeliveryBackendType]:
        """
        Determine the preferred delivery backend for a message.

        Routing rules:
        1. Sysop override rules (highest priority)
        2. All recipients → Winlink (primary)
        3. Email addresses → SMTP fallback (if Winlink unavailable)
        """
        to_addr = message.get("to_call", "").upper()

        # Check sysop overrides
        for pattern, backend in self._routing_overrides.items():
            if pattern.upper() in to_addr or to_addr.endswith(pattern.upper()):
                log.info(f"Routing MID={message.get('id', '?')} → {backend.value} "
                         f"(override rule: {pattern})")
                return backend

        # Primary: Winlink (handles both callsigns and email addresses)
        if self._winlink.is_enabled():
            return DeliveryBackendType.WINLINK

        # Fallback: SMTP for email addresses when Winlink is unavailable
        if "@" in to_addr and self._smtp.is_enabled() and self._check_internet():
            log.info(f"Winlink unavailable — routing email to SMTP fallback")
            return DeliveryBackendType.SMTP

        return None

    def _select_provider(self, backend: OutboundDeliveryBackend,
                         state: MessageDeliveryState) -> str:
        """Select the next provider to attempt within a multi-provider backend."""
        providers = backend.get_provider_ids()
        if not providers:
            return ""

        now = datetime.now(timezone.utc)

        # Find the first provider not in backoff
        for pid in providers:
            pa = state.provider_attempts.get(pid)
            if pa and pa.backoff_until:
                try:
                    backoff_time = datetime.fromisoformat(pa.backoff_until)
                    if now < backoff_time:
                        continue  # still in backoff
                except ValueError:
                    pass
            return pid

        # All providers in backoff — return the one with the earliest backoff expiry
        return providers[0]

    # ── Success / Failure handlers ───────────────────────────────────────────

    def _handle_success(self, mid: str, message: dict,
                        state: MessageDeliveryState,
                        result: DeliveryResult) -> None:
        """Handle successful delivery."""
        state.delivery_status = DeliveryStatus.DELIVERED
        state.record_attempt(result.provider_id,
                             result.backend or state.selected_backend)
        state.record_failover(FailoverEvent(
            event_type=FailoverEventType.DELIVERY_CONFIRMED,
            backend=result.backend or state.selected_backend,
            provider_id=result.provider_id,
            message_id=mid,
        ))

        self._save_delivery_state(mid, state)
        self._update_message_delivery_status(mid, DeliveryStatus.DELIVERED,
                                             state.selected_backend)

        # Mark the message as sent in the existing store
        self._store.mark_sent(mid)

        log.info(
            f"MID={mid} delivered via {state.selected_backend.value}"
            f"/{result.provider_id} "
            f"in {result.duration:.1f}s "
            f"(total attempts: {state.total_attempts()})"
        )

        self._bus.emit(
            EventType.MESSAGE_SENT,
            source="delivery",
            data={
                "mid": mid,
                "backend": state.selected_backend.value,
                "provider": result.provider_id,
                "duration": result.duration,
                "total_attempts": state.total_attempts(),
            },
        )

    def _handle_failure(self, mid: str, message: dict,
                        state: MessageDeliveryState,
                        result: DeliveryResult) -> None:
        """Handle a failed delivery attempt — decide retry or failover."""
        backend_type = state.selected_backend
        provider_id = result.provider_id or "unknown"

        # Record the attempt
        state.record_attempt(provider_id, backend_type, error=result.error)
        pa = state.provider_attempts[provider_id]

        # Compute backoff for this provider
        backoff_secs = _compute_backoff(pa.consecutive_failures)
        pa.backoff_until = (
            datetime.now(timezone.utc) + timedelta(seconds=backoff_secs)
        ).isoformat()

        # Determine the appropriate failover event type
        if backend_type == DeliveryBackendType.SMTP:
            event_type = FailoverEventType.SMTP_PROVIDER_FAILURE
        else:
            event_type = FailoverEventType.WINLINK_TRANSPORT_FAILURE

        failover_event = FailoverEvent(
            event_type=event_type,
            backend=backend_type,
            provider_id=provider_id,
            message_id=mid,
            error=result.error,
        )
        state.record_failover(failover_event)
        with self._lock:
            self._failover_log.append(failover_event)

        log.warning(
            f"MID={mid} delivery failed via {backend_type.value}/{provider_id}: "
            f"{result.error} (attempt {pa.attempt_count}, "
            f"backoff {backoff_secs:.0f}s)"
        )

        # Check if we should failover to the next backend
        backend = self._backends.get(backend_type)
        if backend:
            all_providers_exhausted = True
            for pid in backend.get_provider_ids():
                p_att = state.provider_attempts.get(pid)
                if p_att is None or p_att.consecutive_failures < 3:
                    all_providers_exhausted = False
                    break

            if all_providers_exhausted:
                self._handle_failover(mid, message, state, backend_type, result.error)
                return

        # Stay on current backend, retry after backoff
        state.delivery_status = DeliveryStatus.BACKEND_FAILED
        self._save_delivery_state(mid, state)
        self._update_message_delivery_status(mid, DeliveryStatus.BACKEND_FAILED,
                                             backend_type)

    def _handle_failover(self, mid: str, message: dict,
                         state: MessageDeliveryState,
                         failed_backend: DeliveryBackendType,
                         error: str) -> None:
        """Escalate to the next backend in the failover chain."""
        next_backend = self._get_next_backend(failed_backend, message)

        if next_backend is None:
            # All backends exhausted
            state.delivery_status = DeliveryStatus.BACKEND_FAILED
            state.record_failover(FailoverEvent(
                event_type=FailoverEventType.BACKEND_EXHAUSTED,
                backend=failed_backend,
                message_id=mid,
                error="All delivery backends exhausted",
            ))
            self._save_delivery_state(mid, state)
            self._update_message_delivery_status(mid, DeliveryStatus.BACKEND_FAILED,
                                                 failed_backend)
            log.error(f"MID={mid} all delivery backends exhausted")
            return

        # Transition to next backend
        log.info(
            f"MID={mid} failing over: {failed_backend.value} → {next_backend.value}"
        )
        state.selected_backend = next_backend
        state.delivery_status = DeliveryStatus.FAILOVER
        state.record_failover(FailoverEvent(
            event_type=FailoverEventType.SMTP_PROVIDER_FAILURE
            if failed_backend == DeliveryBackendType.SMTP
            else FailoverEventType.WINLINK_TRANSPORT_FAILURE,
            backend=failed_backend,
            message_id=mid,
            error=error,
        ))

        self._save_delivery_state(mid, state)
        self._update_message_delivery_status(mid, DeliveryStatus.FAILOVER,
                                             next_backend)

    def _get_next_backend(self, failed: DeliveryBackendType,
                          message: dict) -> Optional[DeliveryBackendType]:
        """
        Determine the next backend in the failover chain.

        For email recipients:
          Winlink → SMTP fallback → (exhausted)
        For callsign recipients:
          Winlink → (exhausted)
        """
        to_addr = message.get("to_call", "")
        is_email = "@" in to_addr

        if is_email:
            chain = [
                DeliveryBackendType.WINLINK,
                DeliveryBackendType.SMTP,
            ]
        else:
            chain = [DeliveryBackendType.WINLINK]

        try:
            idx = chain.index(failed)
        except ValueError:
            return None

        # Find the next enabled backend in the chain
        for next_type in chain[idx + 1:]:
            backend = self._backends.get(next_type)
            if backend and backend.is_enabled():
                return next_type

        return None

    # ── Backoff check ────────────────────────────────────────────────────────

    def _is_in_backoff(self, state: MessageDeliveryState) -> bool:
        """Check if all providers for the selected backend are in backoff."""
        if not state.selected_backend:
            return False

        backend = self._backends.get(state.selected_backend)
        if not backend:
            return False

        now = datetime.now(timezone.utc)
        for pid in backend.get_provider_ids():
            pa = state.provider_attempts.get(pid)
            if pa is None:
                return False  # untried provider available
            if not pa.backoff_until:
                return False
            try:
                backoff_time = datetime.fromisoformat(pa.backoff_until)
                if now >= backoff_time:
                    return False  # backoff expired
            except ValueError:
                return False

        return True  # all providers in backoff

    # ── State persistence ────────────────────────────────────────────────────

    def _load_delivery_state(self, mid: str) -> MessageDeliveryState:
        """Load delivery state from the database, or create a new one."""
        try:
            with self._store._lock:
                cur = self._store._conn.execute(
                    "SELECT delivery_state FROM messages WHERE id=?",
                    (mid,),
                )
                row = cur.fetchone()
                if row and row[0]:
                    return MessageDeliveryState.from_json(row[0])
        except Exception as e:
            log.warning(f"Failed to load delivery state for MID={mid}: {e}")

        return MessageDeliveryState(message_id=mid)

    def _save_delivery_state(self, mid: str, state: MessageDeliveryState) -> None:
        """Persist delivery state to the database."""
        state.last_updated = _utcnow()
        try:
            with self._store._lock:
                self._store._conn.execute(
                    "UPDATE messages SET delivery_state=? WHERE id=?",
                    (state.to_json(), mid),
                )
                self._store._conn.commit()
        except Exception as e:
            log.error(f"Failed to save delivery state for MID={mid}: {e}")

    def _update_message_delivery_status(self, mid: str,
                                        status: DeliveryStatus,
                                        backend: DeliveryBackendType) -> None:
        """Update the delivery status and backend columns in the messages table."""
        try:
            with self._store._lock:
                self._store._conn.execute(
                    """UPDATE messages
                       SET delivery_status=?,
                           delivery_backend_selected=?
                       WHERE id=?""",
                    (status.value, backend.value, mid),
                )
                self._store._conn.commit()
        except Exception as e:
            log.error(f"Failed to update delivery status for MID={mid}: {e}")

    # ── Expiration ───────────────────────────────────────────────────────────

    def _expire_stale_messages(self) -> None:
        """Mark messages as expired if they've exceeded the retention period."""
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=self._expiry_days)
        ).isoformat()

        try:
            with self._store._lock:
                cur = self._store._conn.execute(
                    """UPDATE messages
                       SET delivery_status=?
                       WHERE delivery_status IN (?, ?, ?)
                         AND is_sent=0
                         AND created_at < ?""",
                    (
                        DeliveryStatus.EXPIRED.value,
                        DeliveryStatus.PENDING.value,
                        DeliveryStatus.BACKEND_FAILED.value,
                        DeliveryStatus.FAILOVER.value,
                        cutoff,
                    ),
                )
                self._store._conn.commit()
                if cur.rowcount:
                    log.info(f"Expired {cur.rowcount} stale delivery message(s)")
        except Exception as e:
            log.error(f"Failed to expire stale messages: {e}")

    # ── Manual controls ──────────────────────────────────────────────────────

    def deliver_now(self, mid: str) -> Optional[DeliveryResult]:
        """
        Trigger immediate delivery of a specific message.
        Bypasses backoff and queue scheduling.
        """
        msg = self._store.get_message(mid)
        if not msg:
            return DeliveryResult(
                success=False,
                error=f"Message {mid} not found",
            )

        state = self._load_delivery_state(mid)
        backend_type = state.selected_backend or self._route_message(msg)
        if backend_type is None:
            return DeliveryResult(
                success=False,
                error="No suitable backend available",
            )

        backend = self._backends.get(backend_type)
        if not backend or not backend.is_enabled():
            return DeliveryResult(
                success=False,
                error=f"{backend_type.value} backend is not available",
            )

        state.selected_backend = backend_type
        result = backend.deliver(msg)

        if result.success:
            self._handle_success(mid, msg, state, result)
        else:
            self._handle_failure(mid, msg, state, result)

        return result

    def retry_message(self, mid: str) -> None:
        """Reset a failed message for retry."""
        state = self._load_delivery_state(mid)
        state.delivery_status = DeliveryStatus.PENDING
        state.selected_backend = None
        # Clear backoff on all providers
        for pa in state.provider_attempts.values():
            pa.backoff_until = ""
            pa.consecutive_failures = 0
        self._save_delivery_state(mid, state)
        self._update_message_delivery_status(
            mid, DeliveryStatus.PENDING,
            DeliveryBackendType.SMTP,  # placeholder
        )
        log.info(f"MID={mid} reset for delivery retry")

    def set_message_backend(self, mid: str, backend: DeliveryBackendType) -> None:
        """Override the delivery backend for a specific message."""
        state = self._load_delivery_state(mid)
        state.selected_backend = backend
        state.delivery_status = DeliveryStatus.PENDING
        self._save_delivery_state(mid, state)
        log.info(f"MID={mid} backend override → {backend.value}")

    # ── Diagnostics ──────────────────────────────────────────────────────────

    def get_backend_health_summary(self) -> dict[str, str]:
        """Return health status of all backends."""
        return {
            bt.value: backend.health().value
            for bt, backend in self._backends.items()
        }

    def get_queue_depth(self) -> dict[str, int]:
        """Return counts of messages in each delivery status."""
        summary = {}
        try:
            with self._store._lock:
                for status in DeliveryStatus:
                    cur = self._store._conn.execute(
                        "SELECT COUNT(*) FROM messages WHERE delivery_status=?",
                        (status.value,),
                    )
                    count = cur.fetchone()[0]
                    if count:
                        summary[status.value] = count
        except Exception as e:
            log.error(f"Failed to get queue depth: {e}")
        return summary

    def get_failover_log(self, limit: int = 100) -> list[dict]:
        """Return recent failover events."""
        with self._lock:
            snapshot = list(self._failover_log[-limit:])
        return [e.to_dict() for e in snapshot]

    def get_message_delivery_state(self, mid: str) -> Optional[MessageDeliveryState]:
        """Return the full delivery state for a message."""
        return self._load_delivery_state(mid)

    def export_diagnostics(self) -> dict:
        """Export full diagnostic state for troubleshooting."""
        return {
            "engine_running": self._running,
            "poll_interval": self._poll_interval,
            "expiry_days": self._expiry_days,
            "backend_health": self.get_backend_health_summary(),
            "queue_depth": self.get_queue_depth(),
            "failover_log": self.get_failover_log(50),
            "routing_overrides": {
                k: v.value for k, v in self._routing_overrides.items()
            },
            "timestamp": _utcnow(),
        }
