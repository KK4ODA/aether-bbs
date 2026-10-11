"""
aether/control.py

Minimal client for the Aether HF control API (docs/spec/control-api.md in
the aether-hf repository).

Uses the one-shot HTTP form of the API — ``POST /v1/<method>`` with a JSON
body — so it needs nothing beyond the standard library.  Responses have the
shape ``{"ok": true, "result": {...}}`` or
``{"ok": false, "error": {"code", "message", "retryable"}}``.

A loopback daemon needs no token.  A daemon bound to another interface
requires ``Authorization: Bearer <token>`` from any non-loopback client.
"""

import json
import logging
import urllib.error
import urllib.request
from typing import Any, Optional

log = logging.getLogger(__name__)

DEFAULT_CONTROL_PORT = 8515
API_VERSION = "v1"


class AetherControlError(Exception):
    """Raised when the control API cannot be reached or refuses a request."""

    def __init__(self, message: str, code: str = "", retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class AetherControlClient:
    """Calls methods on one aetherd control API endpoint."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = DEFAULT_CONTROL_PORT,
        token: str = "",
        timeout: float = 3.0,
    ):
        self.host = host
        self.port = port
        self._token = token
        self.timeout = timeout

    @property
    def base_url(self) -> str:
        host = self.host
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"   # bare IPv6 literal
        return f"http://{host}:{self.port}/{API_VERSION}"

    def call(self, method: str, params: Optional[dict] = None) -> Any:
        """Invoke ``method`` and return its ``result``.

        Raises AetherControlError for transport failures and refusals.
        """
        body = json.dumps(params or {}).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}/{method}",
            data=body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        if self._token:
            req.add_header("Authorization", f"Bearer {self._token}")

        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                payload = resp.read()
        except urllib.error.HTTPError as e:
            # 400 carries the same error object as a WebSocket refusal
            try:
                payload = e.read()
            except Exception:
                payload = b""
            if e.code == 401:
                raise AetherControlError(
                    "Aether HF refused the control API request: a token is "
                    "required. Set the control token in the instance's "
                    "Aether HF settings.",
                    code="unauthorised",
                ) from None
            if not payload:
                raise AetherControlError(
                    f"Aether HF control API answered HTTP {e.code}",
                    code=f"http_{e.code}",
                ) from None
        except (urllib.error.URLError, OSError) as e:
            reason = getattr(e, "reason", e)
            raise AetherControlError(
                f"Aether HF control API not reachable at "
                f"{self.host}:{self.port} ({reason})",
                code="unreachable",
                retryable=True,
            ) from None

        try:
            msg = json.loads(payload.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise AetherControlError(
                "Aether HF control API sent a reply that is not JSON",
                code="bad_reply",
            ) from None

        if msg.get("ok"):
            return msg.get("result")
        err = msg.get("error") or {}
        raise AetherControlError(
            err.get("message") or f"Aether HF refused '{method}'",
            code=err.get("code", ""),
            retryable=bool(err.get("retryable", False)),
        )

    # ── Convenience wrappers ──────────────────────────────────────────────

    def status(self) -> dict:
        return self.call("status") or {}

    def capabilities(self) -> dict:
        return self.call("capabilities") or {}

    def heard(self) -> list[dict]:
        return (self.call("heard.list") or {}).get("stations", [])

    def sessions(self, remote: str = "") -> list[dict]:
        params = {"remote": remote} if remote else None
        return (self.call("sessions.list", params) or {}).get("sessions", [])

    def probe(self, remote: str) -> Any:
        return self.call("probe", {"remote": remote})

    def ping(self) -> bool:
        """True when the control API answers at all."""
        try:
            self.status()
            return True
        except AetherControlError:
            return False
