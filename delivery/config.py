"""
delivery/config.py

Pydantic configuration models for the outbound delivery system.
These are imported by core/config.py and added to AppConfig.

Design principle: No protocol-specific fields.  The schema must remain
valid when Winlink switches from Telnet/B2F to CMS API.
"""

from pydantic import BaseModel, Field


class SMTPProviderModel(BaseModel):
    """Configuration for one SMTP provider."""
    provider_id:       str  = ""
    display_name:      str  = ""
    host:              str  = ""
    port:              int  = Field(default=587, ge=1, le=65535)
    tls_mode:          str  = Field(default="STARTTLS",
                                     pattern="^(STARTTLS|SSL|NONE)$")
    username:          str  = ""
    password:          str  = ""
    sender_email:      str  = ""
    sender_name:       str  = ""
    enabled:           bool = True
    priority:          int  = Field(default=0, ge=0)
    failure_threshold: int  = Field(default=3, ge=1, le=50)
    max_attempts:      int  = Field(default=5, ge=1, le=100)


class SMTPDeliveryConfig(BaseModel):
    """SMTP backend configuration (Tier 2)."""
    enabled:    bool                 = False
    providers:  list[SMTPProviderModel] = Field(default_factory=list)


class RoutingOverride(BaseModel):
    """Sysop routing override rule."""
    pattern:  str = ""       # recipient pattern (e.g. "@gmail.com", "EMCOMM")
    backend:  str = "SMTP"   # DeliveryBackendType value


class InboundEmailConfig(BaseModel):
    """
    Inbound email polling configuration.

    The BBS polls a mailbox (IMAP or POP3) on a schedule.
    Incoming emails addressed to callsign@bbs_domain are parsed
    and stored for RF pickup.
    """
    enabled:             bool       = False
    protocol:            str        = Field(default="IMAP",
                                            pattern="^(IMAP|POP3)$")
    host:                str        = ""
    port:                int        = Field(default=993, ge=1, le=65535)
    use_ssl:             bool       = True
    username:            str        = ""
    password:            str        = ""
    folder:              str        = "INBOX"
    bbs_domain:          str        = ""       # e.g. "varabbs.example.com"
    poll_interval:       int        = Field(default=5, ge=1, le=1440)  # minutes
    delete_after_fetch:  bool       = False
    max_message_size:    int        = Field(default=50000, ge=1000, le=500000)
    allowed_senders:     list[str]  = Field(default_factory=list)


class WinlinkForwardConfig(BaseModel):
    """
    Winlink ;FW (forward-for) list management.

    Controls which callsigns the BBS requests mail for when polling
    the CMS.  AUTO modes dynamically build the list from registered users.
    """
    mode:       str = Field(default="MANUAL",
                            pattern="^(MANUAL|AUTO_APPROVED|AUTO_ALL)$")
    max_calls:  int = Field(default=50, ge=1, le=50)


class OutboundDeliveryConfig(BaseModel):
    """
    Top-level outbound delivery configuration.

    Groups all three tier configurations and the failover engine
    settings under one namespace.
    """
    # Engine settings
    engine_enabled:        bool = False
    engine_poll_seconds:   int  = Field(default=30, ge=5, le=3600)
    message_expiry_days:   int  = Field(default=7, ge=1, le=365)

    # Backend configurations
    smtp:       SMTPDeliveryConfig = Field(default_factory=SMTPDeliveryConfig)

    # Routing
    routing_overrides: list[RoutingOverride] = Field(default_factory=list)

    # Inbound
    inbound_email:    InboundEmailConfig    = Field(default_factory=InboundEmailConfig)
    winlink_forward:  WinlinkForwardConfig  = Field(default_factory=WinlinkForwardConfig)
