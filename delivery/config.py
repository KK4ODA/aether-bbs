"""
delivery/config.py

Pydantic configuration models for internet email (inbound polling and the
Winlink ;FW list).  Imported by core/config.py and added to AppConfig.
"""

from pydantic import BaseModel, Field


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
    bbs_domain:          str        = ""       # e.g. "aetherbbs.example.com"
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
    Internet email settings, stored under ``[outbound_delivery]`` (the name
    of the SMTP fallback they used to share the section with).
    """
    inbound_email:    InboundEmailConfig    = Field(default_factory=InboundEmailConfig)
    winlink_forward:  WinlinkForwardConfig  = Field(default_factory=WinlinkForwardConfig)
