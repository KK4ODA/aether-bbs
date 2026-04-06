"""
bbs/mode_policy.py

Operational mode policy.  Returns mode-specific values for all
configurable behaviors.  The rest of the codebase queries this
object instead of checking the mode string directly.

This eliminates scattered ``if mode == "emcomm"`` checks.
Mode switching is a config change + hot reload — no code changes.
"""

from core.config import AppConfig


class ModePolicy:
    """
    Encapsulates all operational-mode-specific behavior.

    Supports three modes:
      - restricted: whitelist + approval only (default)
      - open: anyone can use, auto-approved
      - emcomm: maximum lockdown
    """

    def __init__(self, config: AppConfig):
        self._config = config

    def update_config(self, config: AppConfig):
        """Hot-reload support: swap the underlying config."""
        self._config = config

    @property
    def mode(self) -> str:
        return self._config.security.operational_mode.lower()

    @property
    def require_registration(self) -> bool:
        if self.mode == "open":
            return False
        if self.mode == "emcomm":
            return True  # always enforced in EMCOMM regardless of config
        # Restricted mode: honour the config checkbox
        return self._config.security.require_registration

    @property
    def require_password(self) -> bool:
        if self.mode == "emcomm":
            return True
        return self._config.security.require_password

    @property
    def auto_approve_on_connect(self) -> bool:
        """True when unknown users should be auto-approved on connect."""
        return not self.require_registration

    @property
    def min_upload_role(self) -> str:
        if self.mode == "emcomm":
            return "SYSOP"
        if self.mode == "restricted":
            return "TRUSTED"
        return "APPROVED"

    @property
    def min_bulletin_post_role(self) -> str:
        if self.mode == "emcomm":
            return "SYSOP"
        if self.mode == "restricted":
            return "TRUSTED"
        return "APPROVED"

    @property
    def session_timeout(self) -> int:
        base = self._config.bbs.session_timeout
        if self.mode == "emcomm":
            return min(base, 120)
        return base

    @property
    def rate_limit_multiplier(self) -> float:
        if self.mode == "emcomm":
            return 0.25
        if self.mode == "open":
            return 2.0
        return 1.0

    @property
    def auto_block_threshold(self) -> int:
        if self.mode == "emcomm":
            return 5
        if self.mode == "open":
            return 50
        return self._config.security.auto_block_threshold

    @property
    def beacon_suffix(self) -> str:
        if self.mode == "emcomm":
            return " EMCOMM"
        return ""

    @property
    def show_sysop_in_banner(self) -> bool:
        return self.mode == "open"

    @property
    def restricted_connect_banner(self) -> bool:
        return self.mode != "open"
