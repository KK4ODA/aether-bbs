"""
delivery/backends.py

Outbound delivery backend abstraction and implementations.

Architecture:
  OutboundDeliveryBackend (abstract)
   ├── WinlinkBackend  (via Pat client — HTTP API + CLI)
   └── SMTPBackend     (multi-provider failover)

No protocol specifics leak above the backend interface.
"""

import logging
import smtplib
import ssl
import time
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.utils import formatdate, make_msgid
from typing import Optional

from delivery.models import (
    DeliveryBackendType,
    DeliveryResult,
    BackendHealth,
    WinlinkTransportType,
)

log = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
# Abstract Backend
# ═══════════════════════════════════════════════════════════════════════════════

class OutboundDeliveryBackend(ABC):
    """
    Interface for all outbound delivery backends.

    Implementations must be:
    - Thread-safe (called from the delivery engine thread)
    - Idempotent on retry (same message can be attempted again safely)
    - Self-contained (no cross-backend dependencies)
    """

    @abstractmethod
    def backend_type(self) -> DeliveryBackendType:
        """Return the backend identifier."""
        ...

    @abstractmethod
    def is_enabled(self) -> bool:
        """Whether this backend is currently enabled by configuration."""
        ...

    @abstractmethod
    def health(self) -> BackendHealth:
        """Current observable health state."""
        ...

    @abstractmethod
    def deliver(self, message: dict, provider_id: str = "") -> DeliveryResult:
        """
        Attempt to deliver a message.

        Args:
            message:     Dict from MessageStore (id, from_call, to_call,
                         subject, body, etc.)
            provider_id: For multi-provider backends, which provider to use.
                         Empty string = use default/next in priority.

        Returns:
            DeliveryResult with success/failure details.
        """
        ...

    @abstractmethod
    def test_connection(self, provider_id: str = "") -> DeliveryResult:
        """
        Test connectivity without sending a real message.
        Used by the GUI 'Test' buttons.
        """
        ...

    @abstractmethod
    def get_provider_ids(self) -> list[str]:
        """
        Return ordered list of provider identifiers.
        Single-provider backends return a single-element list.
        Multi-provider backends (SMTP) return one ID per configured provider.
        """
        ...

    @abstractmethod
    def update_config(self, **kwargs) -> None:
        """Apply configuration changes at runtime."""
        ...


# ═══════════════════════════════════════════════════════════════════════════════
# Tier 1 — Winlink Backend (API-Upgradeable)
# ═══════════════════════════════════════════════════════════════════════════════

class WinlinkBackend(OutboundDeliveryBackend):
    """
    Winlink delivery via Pat client (https://getpat.io).

    Pat handles all CMS protocol details (B2F, LZHUF, authentication).
    This backend delegates to the PatClient for message sending and
    connection testing.
    """

    def __init__(self):
        self._enabled = False
        self._health = BackendHealth.DISABLED
        self._callsign = ""
        self._pat_http_addr = "localhost:8080"
        self._lock = threading.Lock()
        # Reference to PatClient (injected by engine)
        self._poller = None

    def backend_type(self) -> DeliveryBackendType:
        return DeliveryBackendType.WINLINK

    def is_enabled(self) -> bool:
        return self._enabled

    def health(self) -> BackendHealth:
        if not self._enabled:
            return BackendHealth.DISABLED
        return self._health

    def set_poller(self, poller) -> None:
        """Inject the PatClient for Winlink delivery."""
        self._poller = poller

    def deliver(self, message: dict, provider_id: str = "") -> DeliveryResult:
        """Deliver via Winlink using Pat client."""
        start = time.monotonic()
        pat_provider = WinlinkTransportType.PAT_CLIENT.value

        if not self._enabled:
            return DeliveryResult(
                success=False,
                backend=DeliveryBackendType.WINLINK,
                provider_id=pat_provider,
                error="Winlink backend is disabled",
            )

        if not self._callsign:
            return DeliveryResult(
                success=False,
                backend=DeliveryBackendType.WINLINK,
                provider_id=pat_provider,
                error="No Winlink callsign configured",
            )

        if self._poller is None:
            return DeliveryResult(
                success=False,
                backend=DeliveryBackendType.WINLINK,
                provider_id=pat_provider,
                error="Pat client not initialised",
            )

        try:
            # Trigger a blocking poll which sends queued messages and syncs
            result = self._poller.poll_now_blocking()

            if result is None:
                return DeliveryResult(
                    success=False,
                    backend=DeliveryBackendType.WINLINK,
                    provider_id=pat_provider,
                    error="Pat session already in progress or disabled",
                    duration=time.monotonic() - start,
                )

            if result.error:
                self._health = BackendHealth.DEGRADED
                return DeliveryResult(
                    success=False,
                    backend=DeliveryBackendType.WINLINK,
                    provider_id=pat_provider,
                    error=result.error,
                    duration=result.duration,
                    details={"sent": result.sent, "received": result.received},
                )

            self._health = BackendHealth.HEALTHY
            return DeliveryResult(
                success=result.sent > 0,
                backend=DeliveryBackendType.WINLINK,
                provider_id=pat_provider,
                duration=result.duration,
                details={"sent": result.sent, "received": result.received},
            )

        except Exception as e:
            self._health = BackendHealth.UNAVAILABLE
            return DeliveryResult(
                success=False,
                backend=DeliveryBackendType.WINLINK,
                provider_id=pat_provider,
                error=str(e),
                duration=time.monotonic() - start,
            )

    def test_connection(self, provider_id: str = "") -> DeliveryResult:
        """Test Pat connectivity by probing its HTTP API."""
        start = time.monotonic()
        pat_provider = WinlinkTransportType.PAT_CLIENT.value

        if not self._callsign:
            return DeliveryResult(
                success=False,
                backend=DeliveryBackendType.WINLINK,
                error="No callsign configured",
            )

        try:
            from urllib.request import Request, urlopen
            url = f"http://{self._pat_http_addr}/api/mailbox/in"
            req = Request(url)
            resp = urlopen(req, timeout=10)
            resp.read()  # consume response

            self._health = BackendHealth.HEALTHY
            return DeliveryResult(
                success=True,
                backend=DeliveryBackendType.WINLINK,
                provider_id=pat_provider,
                duration=time.monotonic() - start,
                details={"pat_addr": self._pat_http_addr},
            )
        except Exception as e:
            self._health = BackendHealth.UNAVAILABLE
            return DeliveryResult(
                success=False,
                backend=DeliveryBackendType.WINLINK,
                provider_id=pat_provider,
                error=f"Pat not reachable: {e}",
                duration=time.monotonic() - start,
            )

    def get_provider_ids(self) -> list[str]:
        return [WinlinkTransportType.PAT_CLIENT.value]

    def update_config(self, **kwargs) -> None:
        self._enabled = kwargs.get("enabled", self._enabled)
        self._callsign = kwargs.get("callsign", self._callsign)
        self._pat_http_addr = kwargs.get("pat_http_addr", self._pat_http_addr)

        if self._enabled and self._health == BackendHealth.DISABLED:
            self._health = BackendHealth.HEALTHY
        elif not self._enabled:
            self._health = BackendHealth.DISABLED


# ═══════════════════════════════════════════════════════════════════════════════
# Tier 2 — Multi-Provider SMTP Backend
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class SMTPProviderConfig:
    """Configuration for one SMTP provider."""
    provider_id:   str   = ""
    display_name:  str   = ""
    host:          str   = ""
    port:          int   = 587
    tls_mode:      str   = "STARTTLS"   # STARTTLS, SSL, NONE
    username:      str   = ""
    password:      str   = ""
    sender_email:  str   = ""
    sender_name:   str   = ""
    enabled:       bool  = True
    priority:      int   = 0            # lower = higher priority
    failure_threshold: int = 3          # consecutive failures before skip
    max_attempts:  int   = 5


class SMTPBackend(OutboundDeliveryBackend):
    """
    Multi-provider SMTP delivery with deterministic failover.

    Providers are attempted in priority order.  Each provider has
    independent failure tracking with exponential backoff.
    If all providers fail, the delivery engine escalates to Tier 3.
    """

    def __init__(self):
        self._enabled = False
        self._providers: list[SMTPProviderConfig] = []
        self._provider_health: dict[str, BackendHealth] = {}
        self._lock = threading.Lock()

    def backend_type(self) -> DeliveryBackendType:
        return DeliveryBackendType.SMTP

    def is_enabled(self) -> bool:
        return self._enabled and len(self._providers) > 0

    def health(self) -> BackendHealth:
        if not self._enabled:
            return BackendHealth.DISABLED
        if not self._providers:
            return BackendHealth.UNAVAILABLE

        with self._lock:
            healths = [self._provider_health.get(p.provider_id, BackendHealth.HEALTHY)
                        for p in self._providers if p.enabled]

        if not healths:
            return BackendHealth.UNAVAILABLE
        if all(h == BackendHealth.UNAVAILABLE for h in healths):
            return BackendHealth.UNAVAILABLE
        if any(h == BackendHealth.UNAVAILABLE for h in healths):
            return BackendHealth.DEGRADED
        return BackendHealth.HEALTHY

    def deliver(self, message: dict, provider_id: str = "") -> DeliveryResult:
        """
        Deliver via a specific SMTP provider, or the next available one.
        """
        if not self._enabled:
            return DeliveryResult(
                success=False,
                backend=DeliveryBackendType.SMTP,
                error="SMTP backend is disabled",
            )

        # If a specific provider is requested, use that one
        if provider_id:
            provider = self._get_provider(provider_id)
            if provider:
                return self._send_via_provider(provider, message)
            return DeliveryResult(
                success=False,
                backend=DeliveryBackendType.SMTP,
                provider_id=provider_id,
                error=f"SMTP provider '{provider_id}' not found",
            )

        # Otherwise iterate providers in priority order
        for provider in self._get_ordered_providers():
            if not provider.enabled:
                continue
            ph = self._provider_health.get(provider.provider_id, BackendHealth.HEALTHY)
            if ph == BackendHealth.UNAVAILABLE:
                continue

            result = self._send_via_provider(provider, message)
            if result.success:
                return result

            log.warning(
                f"SMTP provider {provider.display_name} failed: {result.error}"
            )

        return DeliveryResult(
            success=False,
            backend=DeliveryBackendType.SMTP,
            error="All SMTP providers failed",
        )

    def _send_via_provider(self, provider: SMTPProviderConfig,
                           message: dict) -> DeliveryResult:
        """Send one message through one SMTP provider."""
        start = time.monotonic()
        pid = provider.provider_id

        to_addr = message.get("to_call", "")
        if "@" not in to_addr:
            # Callsign-only recipients can't be sent via SMTP
            return DeliveryResult(
                success=False,
                backend=DeliveryBackendType.SMTP,
                provider_id=pid,
                error=f"Recipient '{to_addr}' is not an email address",
                duration=time.monotonic() - start,
            )

        try:
            # Build the email
            msg = MIMEMultipart("alternative")
            msg["From"] = (
                f"{provider.sender_name} <{provider.sender_email}>"
                if provider.sender_name
                else provider.sender_email
            )
            msg["To"] = to_addr
            msg["Subject"] = message.get("subject", "(no subject)")
            msg["Date"] = formatdate(localtime=True)
            msg["Message-ID"] = make_msgid(
                domain=provider.sender_email.split("@")[-1]
                if "@" in provider.sender_email else "aetherbbs.local"
            )
            msg["X-Mailer"] = "Aether-BBS-Delivery/1.0"

            # Body
            body_raw = message.get("body", b"")
            if isinstance(body_raw, bytes):
                body_text = body_raw.decode("utf-8", errors="replace")
            else:
                body_text = str(body_raw)

            msg.attach(MIMEText(body_text, "plain", "utf-8"))

            # Connect and send
            if provider.tls_mode == "SSL":
                context = ssl.create_default_context()
                server = smtplib.SMTP_SSL(
                    provider.host, provider.port, context=context, timeout=30
                )
            else:
                server = smtplib.SMTP(provider.host, provider.port, timeout=30)

            try:
                server.ehlo()
                if provider.tls_mode == "STARTTLS":
                    context = ssl.create_default_context()
                    server.starttls(context=context)
                    server.ehlo()

                if provider.username and provider.password:
                    server.login(provider.username, provider.password)

                server.sendmail(provider.sender_email, [to_addr], msg.as_string())
                duration = time.monotonic() - start

                # Success — mark provider healthy
                self._provider_health[pid] = BackendHealth.HEALTHY
                log.info(
                    f"SMTP delivery via {provider.display_name}: "
                    f"to={to_addr} mid={message.get('id', '?')} "
                    f"duration={duration:.1f}s"
                )
                return DeliveryResult(
                    success=True,
                    backend=DeliveryBackendType.SMTP,
                    provider_id=pid,
                    duration=duration,
                )
            finally:
                try:
                    server.quit()
                except Exception:
                    pass

        except smtplib.SMTPAuthenticationError as e:
            self._provider_health[pid] = BackendHealth.UNAVAILABLE
            return DeliveryResult(
                success=False,
                backend=DeliveryBackendType.SMTP,
                provider_id=pid,
                error=f"SMTP auth failed: {e}",
                duration=time.monotonic() - start,
            )
        except smtplib.SMTPRecipientsRefused as e:
            # Don't mark provider unhealthy for recipient errors
            return DeliveryResult(
                success=False,
                backend=DeliveryBackendType.SMTP,
                provider_id=pid,
                error=f"Recipient refused: {e}",
                duration=time.monotonic() - start,
            )
        except (smtplib.SMTPException, OSError, TimeoutError) as e:
            self._provider_health[pid] = BackendHealth.DEGRADED
            return DeliveryResult(
                success=False,
                backend=DeliveryBackendType.SMTP,
                provider_id=pid,
                error=f"SMTP error: {e}",
                duration=time.monotonic() - start,
            )

    def test_connection(self, provider_id: str = "") -> DeliveryResult:
        """Test SMTP provider connectivity (EHLO only, no message sent)."""
        provider = self._get_provider(provider_id)
        if not provider:
            return DeliveryResult(
                success=False,
                backend=DeliveryBackendType.SMTP,
                error=f"Provider '{provider_id}' not found",
            )

        start = time.monotonic()
        try:
            if provider.tls_mode == "SSL":
                ctx = ssl.create_default_context()
                server = smtplib.SMTP_SSL(
                    provider.host, provider.port, context=ctx, timeout=15
                )
            else:
                server = smtplib.SMTP(provider.host, provider.port, timeout=15)

            try:
                server.ehlo()
                if provider.tls_mode == "STARTTLS":
                    ctx = ssl.create_default_context()
                    server.starttls(context=ctx)
                    server.ehlo()

                if provider.username and provider.password:
                    server.login(provider.username, provider.password)

                self._provider_health[provider_id] = BackendHealth.HEALTHY
                return DeliveryResult(
                    success=True,
                    backend=DeliveryBackendType.SMTP,
                    provider_id=provider_id,
                    duration=time.monotonic() - start,
                    details={"ehlo_response": "OK"},
                )
            finally:
                try:
                    server.quit()
                except Exception:
                    pass

        except Exception as e:
            self._provider_health[provider_id] = BackendHealth.UNAVAILABLE
            return DeliveryResult(
                success=False,
                backend=DeliveryBackendType.SMTP,
                provider_id=provider_id,
                error=str(e),
                duration=time.monotonic() - start,
            )

    def get_provider_ids(self) -> list[str]:
        return [p.provider_id for p in self._get_ordered_providers()]

    def _get_provider(self, provider_id: str) -> Optional[SMTPProviderConfig]:
        for p in self._providers:
            if p.provider_id == provider_id:
                return p
        return None

    def _get_ordered_providers(self) -> list[SMTPProviderConfig]:
        return sorted(self._providers, key=lambda p: p.priority)

    def update_config(self, **kwargs) -> None:
        self._enabled = kwargs.get("enabled", self._enabled)
        providers_data = kwargs.get("providers", None)
        if providers_data is not None:
            self._providers = []
            for i, pd in enumerate(providers_data):
                pid = pd.get("provider_id", f"smtp_provider_{i}")
                self._providers.append(SMTPProviderConfig(
                    provider_id=pid,
                    display_name=pd.get("display_name", f"Provider {i+1}"),
                    host=pd.get("host", ""),
                    port=pd.get("port", 587),
                    tls_mode=pd.get("tls_mode", "STARTTLS"),
                    username=pd.get("username", ""),
                    password=pd.get("password", ""),
                    sender_email=pd.get("sender_email", ""),
                    sender_name=pd.get("sender_name", ""),
                    enabled=pd.get("enabled", True),
                    priority=pd.get("priority", i),
                    failure_threshold=pd.get("failure_threshold", 3),
                    max_attempts=pd.get("max_attempts", 5),
                ))
                if pid not in self._provider_health:
                    self._provider_health[pid] = BackendHealth.HEALTHY


