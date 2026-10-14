"""
core/config.py

TOML-backed configuration with pydantic v2 schema validation.
Handles default generation, corrupt-config recovery, and OS-appropriate paths.
"""

import sys
import shutil
import logging
from pathlib import Path
from typing import Any, Literal, Optional
from datetime import datetime

try:
    import tomllib          # stdlib Python 3.11+
except ImportError:
    import tomli as tomllib  # backport for 3.9/3.10

import tomli_w
from pydantic import BaseModel, Field, field_validator, model_validator

log = logging.getLogger(__name__)


# ── Pydantic schema ───────────────────────────────────────────────────────────

class TacticalAddress(BaseModel):
    """A tactical callsign that triggers a priority alert, with its own spoken phrase."""
    callsign:          str  = Field(default="", max_length=8)
    alert_phrase:      str  = Field(default="Priority message received", max_length=120)
    advertised:        bool = True   # show this address in the connect banner
    winlink_password:  str  = ""     # Winlink CMS password (encrypted at rest, ENC: prefix)


class StationConfig(BaseModel):
    bbs_callsign:        str   = "N0CALL-10"
    sysop_callsign:      str   = "N0CALL"
    grid_square:         str   = ""
    latitude:            float | None = None   # decimal degrees, positive = N
    longitude:           float | None = None   # decimal degrees, positive = E
    beacon_interval:     int   = Field(default=10, ge=0)   # minutes; 0 = disabled
    beacon_text:         str   = "Aether BBS"
    # Legacy single-callsign field — migrated to tactical_addresses on load
    home_tactical_callsign: str = Field(default="", max_length=8)
    tactical_addresses:  list[TacticalAddress] = Field(
        default_factory=lambda: [TacticalAddress(callsign="BRECKEN",
                                                 alert_phrase="Priority message received")]
    )

    @model_validator(mode="after")
    def _migrate_legacy_tactical(self) -> "StationConfig":
        """If only the old home_tactical_callsign field is set, migrate it."""
        legacy = self.home_tactical_callsign.upper().strip()
        if legacy and not any(t.callsign for t in self.tactical_addresses):
            self.tactical_addresses = [TacticalAddress(callsign=legacy,
                                                        alert_phrase="Priority message received")]
            self.home_tactical_callsign = ""
        return self


class AlertConfig(BaseModel):
    """Settings for the priority message alert system."""
    audio_enabled:  bool  = True
    alert_interval: int   = Field(default=10, ge=1, le=300)
    volume:         float = Field(default=0.85, ge=0.0, le=1.0)
    visual_only:    bool  = False
    high_contrast:  bool  = False
    large_font:     bool  = False
    session_sounds: bool  = True    # play tone on RF session connect/disconnect



class NotificationConfig(BaseModel):
    """Push notification settings for ntfy, Pushover, and SMS."""

    # ── ntfy ──────────────────────────────────────────────────────────────
    enabled:        bool = False          # master switch for ntfy
    ntfy_url:       str  = "http://127.0.0.1:8080"  # local ntfy server
    topic:          str  = "aether-bbs"
    priority:       str  = Field(default="urgent",
                                  pattern="^(min|low|default|high|urgent)$")
    cloud_relay:    bool = False          # also post to cloud (ntfy.sh)
    cloud_url:      str  = "https://ntfy.sh"

    # ── Pushover ──────────────────────────────────────────────────────────
    pushover_enabled:    bool = False
    pushover_api_token:  str  = ""
    pushover_user_key:   str  = ""
    pushover_priority:   int  = Field(default=1, ge=-2, le=2)
    pushover_sound:      str  = "siren"

    # ── SMS (USB GSM modem) ───────────────────────────────────────────────
    sms_enabled:         bool       = False
    sms_serial_port:     str        = ""        # COM port or /dev/ttyUSB0
    sms_baud_rate:       int        = Field(default=9600, ge=300, le=115200)
    sms_phone_numbers:   list[str]  = Field(default_factory=list)

    # ── Event triggers ────────────────────────────────────────────────────
    notify_on_registration:    bool       = True   # new user awaiting sysop approval
    notify_on_urgent_bulletin: bool       = True   # bulletin posted to an urgent area
    urgent_bulletin_areas:     list[str]  = Field(
        default_factory=lambda: ["EMCOMM", "WX", "ARES"]
    )


class FileUploadConfig(BaseModel):
    """Security policy for RF (remote) file uploads."""
    allow_remote_uploads:    bool      = True
    # Upload authorization mode:
    #   "whitelist" — only callsigns in upload_whitelist may upload (DEFAULT)
    #   "blocklist" — anyone except blocked_callsigns may upload (legacy)
    #   "open"      — any connected station may upload
    permission_mode:         str       = Field(default="whitelist",
                                               pattern="^(whitelist|blocklist|open|role_based)$")
    # Whitelist mode: only these callsigns may upload via RF.
    # Supports base callsigns (e.g. KK4ODA matches KK4ODA-5) and
    # specific SSIDs (e.g. KK4ODA-5 matches only KK4ODA-5).
    upload_whitelist:        list[str] = Field(default_factory=list)
    max_file_bytes:          int       = Field(default=5*1024*1024, ge=1024)
    max_files_per_callsign:  int       = Field(default=20, ge=1)
    # Extensions that remote stations are allowed to upload.
    # Sysop GUI uploads bypass this list.
    allowed_extensions:      list[str] = Field(default_factory=lambda: [
        ".txt", ".pdf", ".doc", ".docx",
        ".jpg", ".jpeg", ".png", ".gif",
        ".zip", ".gz", ".csv", ".xlsx",
        ".mp3", ".wav", ".ics", ".kml", ".gpx", ".rtf",
    ])
    # Callsigns that are blocked from uploading (uppercase).
    # In whitelist mode, acts as a belt-and-suspenders deny override.
    blocked_callsigns:       list[str] = Field(default_factory=list)
    # Virus scanning
    virus_scan_enabled:      bool      = False
    # Timeout in seconds for the system AV scan (Windows Defender only)
    virus_scan_timeout:      int       = Field(default=30, ge=5, le=300)
    quarantine_infected:     bool      = True
    # Directory where infected files are moved (empty = delete instead)
    quarantine_dir:          str       = "quarantine"


class PTTConfig(BaseModel):
    """PTT control for VARA HF instances.

    VARA FM handles PTT internally — leave method as 'VOX' for FM.
    VARA HF requires the application to key the radio.

    Methods: VOX, RTS, DTR, DTRRTS, HAMLIB, CM108, OMNIRIG, FLRIG
    """
    method:           str   = "VOX"
    port:             str   = ""           # serial port for RTS/DTR
    hamlib_host:      str   = "127.0.0.1"
    hamlib_port:      int   = Field(default=4532, ge=1, le=65535)
    cm108_device:     str   = ""
    omnirig_rig:      int   = Field(default=1, ge=1, le=2)  # Rig1 or Rig2
    flrig_host:       str   = "127.0.0.1"
    flrig_port:       int   = Field(default=12345, ge=1, le=65535)
    watchdog_timeout: float = Field(default=90.0, ge=10.0, le=600.0)


class ScanChannelConfig(BaseModel):
    """One entry in the HF frequency scan table."""
    freq_khz:       float = Field(default=7122.0, ge=1800.0, le=30000.0)
    dwell_seconds:  float = Field(default=5.0, ge=1.0, le=120.0)
    hours:          str   = "00-24"   # UTC hours active, e.g. "14-04" or "00-24"

    @model_validator(mode="after")
    def _validate_hours(self) -> "ScanChannelConfig":
        """Validate 'HH-HH' format."""
        parts = self.hours.split("-")
        if len(parts) != 2:
            raise ValueError(f"hours must be 'HH-HH', got '{self.hours}'")
        for p in parts:
            h = int(p)
            if not (0 <= h <= 24):
                raise ValueError(f"hour must be 0-24, got {h}")
        return self


class RigControlConfig(BaseModel):
    """OmniRig-based rig control and frequency scanning for HF instances.

    When method is 'OMNIRIG', the BBS can set frequency and mode on the
    radio and scan a list of channels with configurable dwell time and
    time-of-day filtering.  Scanning pauses when a VARA session connects
    and resumes when it disconnects.
    """
    method:        str   = "NONE"         # "NONE" or "OMNIRIG"
    omnirig_rig:   int   = Field(default=1, ge=1, le=2)
    mode:          str   = "USB"          # USB, DATA-USB, LSB, etc.
    channels:      list[ScanChannelConfig] = Field(default_factory=list)


class SoundmodemConfig(BaseModel):
    """Soundmodem / AX.25 packet modem connection settings."""
    # AGWPE only.  "KISS" is still read (older configs) but not supported:
    # the BBS has no AX.25 connected-mode stack of its own, and Soundmodem
    # and Direwolf both offer AGWPE, which provides one.
    protocol:       str  = Field(default="AGWPE",
                                  pattern="^(AGWPE|KISS)$")
    host:           str  = "127.0.0.1"
    port:           int  = Field(default=8000, ge=1, le=65535)  # AGWPE: 8000
    my_callsign:    str  = ""     # station callsign for AX.25
    my_ssid:        int  = Field(default=0, ge=0, le=15)
    exe_path:       str  = ""     # path to Soundmodem / Direwolf executable
    auto_launch:    bool = False  # start the executable automatically


class AetherConfig(BaseModel):
    """Aether HF modem settings (transport = "AETHER_HF").

    Aether HF's daemon answers on a VARA-compatible host interface, so the
    instance's own cmd_port/data_port (default 8300/8301, the port Aether's
    Setup step 5 calls *Host programs*) carry BBS sessions.  These settings
    cover what is particular to Aether: its control API, which bandwidth to
    ask for, and how to start it.
    """
    # Control API (JSON over HTTP) on the same host as the instance.
    control_port:     int  = Field(default=8515, ge=1, le=65535)
    # Bearer token — only needed when aetherd runs on another machine with a
    # non-loopback [control] bind.  Encrypted at rest (ENC: prefix).
    control_token:    str  = ""
    # Poll the control API for status, faults and heard stations.
    monitor:          bool = True
    # Bandwidth to request with BW<n> while the BBS is attached.
    # 0 = leave Aether on its own [radio] bandwidth (Setup step 4).
    bandwidth:        int  = Field(default=0)
    # Optional --config file passed when auto-starting the standalone aetherd.
    config_file:      str  = ""
    # APRS position beacon on the air as an Aether datagram (heard by other
    # Aether stations' APRS programs).  Interval in minutes.
    aprs_beacon:      bool = False
    aprs_interval:    int  = Field(default=60, ge=10, le=1440)

    @model_validator(mode="after")
    def _check_bandwidth(self) -> "AetherConfig":
        if self.bandwidth not in (0, 500, 2300):
            raise ValueError(
                f"aether.bandwidth must be 0, 500 or 2300 (got {self.bandwidth})"
            )
        return self


class InstanceConfig(BaseModel):
    name:             str  = "Primary"
    # Transport type: VARA_FM, VARA_HF, SOUNDMODEM, or AETHER_HF
    transport:        str  = Field(default="VARA_FM",
                                    pattern="^(VARA_FM|VARA_HF|SOUNDMODEM|AETHER_HF)$")
    # Legacy field — migrated to transport on load
    mode:             str  = Field(default="", pattern="^(FM|HF|)$")
    enabled:          bool = True
    frequency_label:  str  = ""
    executable_path:  str  = ""
    cmd_port:         int  = Field(default=8300, ge=1024, le=65535)
    data_port:        int  = Field(default=8301, ge=1024, le=65535)
    auto_start:       bool = False
    auto_restart:     bool = True
    restart_delay:    int  = Field(default=10, ge=1)
    default_path:     str  = ""       # AX.25 digipeater path, blank = direct
    soundcard:        str  = ""
    host:             str  = "127.0.0.1"
    # VARA HF bandwidth setting — ignored for FM, Soundmodem and Aether HF
    # (Aether uses aether.bandwidth).
    # Valid values: 500, 2300, 2750 Hz.
    # None = auto-default: 500 for HF, 2300 for FM.
    bandwidth:        int | None = None
    # PTT control — required for VARA HF.  For Aether HF, only when Aether's
    # keying is set to "host program" (Aether otherwise keys the radio itself
    # and this stays at VOX).  No-op for FM and Soundmodem.
    ptt: PTTConfig = Field(default_factory=PTTConfig)
    # Rig control — frequency/mode setting and scanning for HF instances.
    rig_control: RigControlConfig = Field(default_factory=RigControlConfig)
    # Soundmodem-specific settings (only used when transport=SOUNDMODEM)
    soundmodem: SoundmodemConfig = Field(default_factory=SoundmodemConfig)
    # AETHER HF modem settings (only used when transport=AETHER_HF)
    aether: AetherConfig = Field(default_factory=AetherConfig)

    @property
    def is_hf(self) -> bool:
        return self.transport.upper() in ("VARA_HF", "AETHER_HF")

    @property
    def is_soundmodem(self) -> bool:
        return self.transport.upper() == "SOUNDMODEM"

    @property
    def is_aether(self) -> bool:
        return self.transport.upper() == "AETHER_HF"

    @model_validator(mode="after")
    def _resolve_defaults(self):
        # Migrate legacy mode field to transport
        if self.mode and not self.transport:
            self.transport = f"VARA_{self.mode.upper()}"
            self.mode = ""
        if self.transport.startswith("VARA") or self.transport == "AETHER_HF":
            if self.cmd_port == self.data_port:
                raise ValueError(
                    f"cmd_port and data_port must differ (both are {self.cmd_port})"
                )
        if self.bandwidth is None and not self.is_soundmodem:
            self.bandwidth = 500 if self.is_hf else 2300
        return self


class BulletinAreaConfig(BaseModel):
    name:        str  = "ALL"          # group name, e.g. ALL, WX, LOCAL
    description: str  = ""             # human-readable label
    enabled:     bool = True
    max_age:     int  = Field(default=14, ge=1)   # days before bulletins expire


class ForwardingNeighbourConfig(BaseModel):
    bbs_callsign:     str        = "N0CALL-1"
    # How to reach the neighbour: "TCP" (host/port, the network) or "RF"
    # (call it on the air through the modem instance named in `instance`:
    # VARA FM, VARA HF or Aether HF).
    via:              str        = Field(default="TCP", pattern="^(TCP|RF)$")
    instance:         str        = ""
    host:             str        = "127.0.0.1"
    port:             int        = Field(default=7300, ge=1024, le=65535)
    enabled:          bool       = True
    interval_minutes: int        = Field(default=60, ge=5)    # how often to call
    areas:            list[str]  = Field(default_factory=list) # empty = all areas

    def matches(self, callsign: str) -> bool:
        """True when ``callsign`` is this neighbour.  A neighbour entered
        without an SSID matches any SSID of that callsign."""
        mine = self.bbs_callsign.strip().upper()
        theirs = callsign.strip().upper()
        if mine == theirs:
            return True
        return "-" not in mine and theirs.split("-")[0] == mine


class ForwardingConfig(BaseModel):
    server_enabled: bool = False
    server_port:    int  = Field(default=7300, ge=1024, le=65535)
    server_bind:    str  = "0.0.0.0"   # bind address; 0.0.0.0 for remote, 127.0.0.1 for local-only


class BBSConfig(BaseModel):
    welcome_message:       str  = "Welcome to the Aether BBS. Type H for help."
    show_connect_banner:   bool = True  # ASCII art shown to RF users on connect
    session_timeout:       int  = Field(default=600, ge=30)   # seconds
    message_dir:           str  = "messages"
    max_message_age:       int  = Field(default=30, ge=1)     # days
    max_sessions:          int  = Field(default=1,  ge=1)
    session_watchdog_timeout: int = Field(default=1800, ge=60)  # seconds; force-disconnect stale sessions
    bulletin_areas:        list[BulletinAreaConfig] = Field(
        default_factory=lambda: [
            BulletinAreaConfig(name="ALL",   description="General bulletins"),
            BulletinAreaConfig(name="WX",    description="Weather"),
            BulletinAreaConfig(name="LOCAL", description="Local information"),
            BulletinAreaConfig(name="DX",    description="DX news"),
        ]
    )
    forwarding:            ForwardingConfig = Field(default_factory=ForwardingConfig)
    forwarding_neighbours: list[ForwardingNeighbourConfig] = Field(default_factory=list)


class APRSConfig(BaseModel):
    enabled:        bool = False
    is_server:      str  = "rotate.aprs2.net"
    is_port:        int  = Field(default=14580, ge=1, le=65535)
    is_passcode:    int  = -1
    is_filter:      str  = ""
    beacon_symbol:  str  = "/B"   # / = primary table, B = BBS symbol


class LoggingConfig(BaseModel):
    level:        str = "WARNING"
    log_dir:      str = "logs"
    max_bytes:    int = Field(default=5_242_880, ge=65536)
    backup_count: int = Field(default=5, ge=1)
    packet_log:   bool = True
    vara_cmd_log: bool = False   # raw VARA FM TCP command/event log for diagnostics
    chat_log:     bool = True    # log sysop↔remote chat conversations


class GUIConfig(BaseModel):
    theme:           str  = "dark"
    log_lines:       int  = Field(default=500, ge=50)
    confirm_restart: bool = True


class WinlinkConfig(BaseModel):
    """Winlink integration via Pat client (https://getpat.io)."""
    enabled:            bool       = False
    gateway_callsign:   str        = ""        # mycall for Pat e.g. "KK4ODA-5"
    gateway_password:   str        = ""        # encrypted at rest (ENC: prefix)
    grid_square:        str        = ""        # Maidenhead locator
    forward_calls:      list[str]  = Field(default_factory=list)  # extra callsigns
    # Pat-specific settings
    pat_exe_path:       str        = ""        # path to pat.exe
    pat_http_addr:      str        = "localhost:8080"  # Pat HTTP API address
    auto_launch_pat:    bool       = True      # auto-start Pat with BBS
    cms_timeout:        int        = Field(default=120, ge=30, le=600)  # seconds

    def decrypt_gateway_password(self, machine_secret: str) -> str:
        """Return the plaintext gateway password, decrypting if stored encrypted."""
        from core.crypto import decrypt_value
        return decrypt_value(self.gateway_password, machine_secret)


class SecurityConfig(BaseModel):
    """Access control and authentication settings."""
    # Operational mode: restricted (default), open, emcomm
    operational_mode:        str  = Field(default="restricted",
                                          pattern="^(restricted|open|emcomm)$")
    # When True, APPROVED+ users without a password are prompted to set one
    require_password:        bool = True
    # When True, unknown callsigns must REGISTER before accessing the BBS
    require_registration:    bool = True
    # Consecutive auth failures before temporary lockout
    auth_failure_threshold:  int  = Field(default=5, ge=1, le=100)
    # Lockout duration in minutes after threshold exceeded
    auth_lockout_minutes:    int  = Field(default=60, ge=1, le=1440)
    # Total cumulative auth failures before automatic BLOCKED
    auto_block_threshold:    int  = Field(default=20, ge=5, le=1000)
    # Machine secret for password encryption (auto-generated on first run)
    machine_secret:          str  = ""
    # Allow plaintext password fallback for dumb terminals (less secure)
    allow_plaintext_auth:    bool = True


class UpdaterConfig(BaseModel):
    """Software update settings."""
    check_on_startup:           bool = True
    auto_check_interval_hours:  int  = Field(default=0, ge=0, le=168)  # 0 = startup only
    # "release": offer published releases.  "latest": a git clone follows
    # every commit on `branch` (ZIP installs always use releases).
    channel:                    Literal["release", "latest"] = "release"
    branch:                     str  = "master"
    repo:                       str  = "KK4ODA/aether-bbs"
    skipped_version:            str  = ""    # "Skip this version" in the prompt


class AppConfig(BaseModel):
    station:   StationConfig   = Field(default_factory=StationConfig)
    bbs:       BBSConfig       = Field(default_factory=BBSConfig)
    aprs:      APRSConfig      = Field(default_factory=APRSConfig)
    alert:     AlertConfig     = Field(default_factory=AlertConfig)
    logging:   LoggingConfig   = Field(default_factory=LoggingConfig)
    gui:       GUIConfig       = Field(default_factory=GUIConfig)
    winlink:   WinlinkConfig      = Field(default_factory=WinlinkConfig)
    notifications: NotificationConfig = Field(default_factory=NotificationConfig)
    file_upload: FileUploadConfig = Field(default_factory=FileUploadConfig)
    security:  SecurityConfig     = Field(default_factory=SecurityConfig)
    updater:   UpdaterConfig      = Field(default_factory=UpdaterConfig)
    outbound_delivery: Any = Field(default_factory=lambda: _get_delivery_config_class()())
    instances: list[InstanceConfig] = Field(
        default_factory=lambda: [InstanceConfig()]
    )

    @field_validator("outbound_delivery", mode="before")
    @classmethod
    def _coerce_delivery_config(cls, v):
        """Coerce plain dict (from TOML) into OutboundDeliveryConfig."""
        if isinstance(v, dict):
            from delivery.config import OutboundDeliveryConfig
            return OutboundDeliveryConfig(**v)
        return v

    @model_validator(mode="after")
    def no_port_conflicts(self):
        seen: set[int] = set()
        for inst in self.instances:
            if not inst.enabled:
                continue
            for port in (inst.cmd_port, inst.data_port):
                if port in seen:
                    raise ValueError(
                        f"Port {port} is used by more than one enabled instance"
                    )
                seen.add(port)
        return self


# ── Outbound Delivery Config (lazy import to avoid circular deps) ─────────────

def _get_delivery_config_class():
    from delivery.config import OutboundDeliveryConfig
    return OutboundDeliveryConfig


# OutboundDeliveryConfig is resolved at runtime via default_factory
# (no model_rebuild needed — avoids circular import)


# ── Config path resolution ────────────────────────────────────────────────────

APP_NAME          = "Aether BBS"
def overlay(base: BaseModel, update: BaseModel) -> BaseModel:
    """Return *base* with the fields *update* was explicitly given laid over
    it, recursing into nested sections.  Fields *update* never set keep
    *base*'s values, so a form that edits only some of a section's settings
    doesn't reset the rest (values kept only in the TOML file, say)."""
    data = base.model_dump()
    for name in update.model_fields_set:
        new_val = getattr(update, name)
        old_val = getattr(base, name, None)
        if (isinstance(new_val, BaseModel) and isinstance(old_val, BaseModel)
                and type(new_val) is type(old_val)):
            data[name] = overlay(old_val, new_val)
        else:
            data[name] = new_val
    return type(base).model_validate(data)


CONFIG_DIR_NAME   = "aether-bbs"
CONFIG_FILE_NAME  = "aether_bbs.toml"
# Names used before the rebrand from VARA BBS — read once and migrated.
LEGACY_CONFIG_DIR_NAME  = "vara-fm-bbs"
LEGACY_CONFIG_FILE_NAME = "vara_fm_bbs.toml"


def _config_base() -> Path:
    if sys.platform == "win32":
        return Path.home() / "AppData" / "Roaming"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support"
    return Path.home() / ".config"


def get_config_dir() -> Path:
    """Return OS-appropriate config directory."""
    return _config_base() / CONFIG_DIR_NAME


def get_config_path() -> Path:
    return get_config_dir() / CONFIG_FILE_NAME


def get_legacy_config_path() -> Path:
    return _config_base() / LEGACY_CONFIG_DIR_NAME / LEGACY_CONFIG_FILE_NAME


def migrate_legacy_config(new_path: Path, legacy_path: Path) -> bool:
    """Copy a VARA BBS config to the Aether BBS location on first run.

    The old file is left where it is, so going back to an older version
    still finds its settings.  Returns True when a copy was made.
    """
    if new_path.exists() or not legacy_path.exists():
        return False
    try:
        new_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(legacy_path, new_path)
        log.info(f"Migrated configuration from {legacy_path} to {new_path}")
        return True
    except OSError as e:
        log.warning(f"Could not migrate legacy config {legacy_path}: {e}")
        return False


# ── ConfigManager ─────────────────────────────────────────────────────────────

class ConfigManager:
    """Loads, validates, and saves AppConfig from/to TOML."""

    def __init__(self, path: Optional[Path] = None):
        self._path = path or get_config_path()
        self._config: Optional[AppConfig] = None
        if path is None:
            migrate_legacy_config(self._path, get_legacy_config_path())

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> AppConfig:
        """Load config, creating defaults if missing, recovering if corrupt."""
        if not self._path.exists():
            log.info(f"No config found at {self._path}; writing defaults")
            # Run the migrations so the first file already carries a
            # machine_secret — secrets saved before it exists could not be
            # decrypted once one is generated.
            cfg, _ = self._migrate(AppConfig())
            self.save(cfg)
            self._config = cfg
            return cfg

        raw = None
        try:
            raw = tomllib.loads(self._path.read_text(encoding="utf-8"))
            raw = self._strip_defunct_fields(raw)
            cfg = AppConfig.model_validate(raw)
            cfg, migrated = self._migrate(cfg)
            if migrated:
                self.save(cfg)
                log.info(f"Config migrated and saved to {self._path}")
            else:
                log.info(f"Config loaded from {self._path}")
            self._config = cfg
            return cfg
        except Exception as e:
            log.error(f"Config load failed: {e} — backing up and salvaging what is valid")
            self._backup_bad_config()
            cfg = self._salvage(raw)
            self.save(cfg)
            self._config = cfg
            return cfg

    def _salvage(self, raw: Optional[dict]) -> AppConfig:
        """Rebuild a config from a file that failed validation: keep each
        top-level section that is valid on its own, reset only the others,
        and always keep the machine secret (without it every stored password
        and encrypted secret is lost)."""
        if not isinstance(raw, dict):           # not even valid TOML
            cfg, _ = self._migrate(AppConfig())
            return cfg
        kept: dict = {}
        reset: list[str] = []
        for key, value in raw.items():
            try:
                AppConfig.model_validate({**kept, key: value})
                kept[key] = value
            except Exception:
                reset.append(key)
        secret = (raw.get("security") or {}).get("machine_secret") \
            if isinstance(raw.get("security"), dict) else None
        if "security" in reset and isinstance(secret, str) and secret:
            kept["security"] = {"machine_secret": secret}
        cfg, _ = self._migrate(AppConfig.model_validate(kept))
        log.error(
            "Settings reset to defaults for: %s (the rest was kept; the "
            "original file was saved alongside as .bad)", ", ".join(reset) or "nothing")
        return cfg

    @staticmethod
    def _strip_defunct_fields(raw: dict) -> dict:
        """Remove/migrate fields before schema validation.

        Must run before model_validate so pydantic doesn't reject them.
        """
        wl = raw.get("winlink")
        if isinstance(wl, dict):
            wl.pop("poll_interval", None)
            wl.pop("auto_poll", None)

        # Migrate instance mode → transport
        for inst in raw.get("instances", []):
            if isinstance(inst, dict) and "mode" in inst and "transport" not in inst:
                mode = inst.pop("mode", "FM").upper()
                inst["transport"] = f"VARA_{mode}" if mode in ("FM", "HF") else "VARA_FM"

        # Aether HF: the prototype-era settings are gone.  Its host ports
        # now live on the instance (cmd_port/data_port), like VARA's.
        for inst in raw.get("instances", []):
            if not isinstance(inst, dict):
                continue
            ae = inst.get("aether")
            if not isinstance(ae, dict):
                continue
            old_cmd = ae.pop("cmd_port", None)
            ae.pop("data_port", None)
            for k in ("mode", "my_callsign", "audio_device_in",
                      "audio_device_out", "auto_launch"):
                ae.pop(k, None)
            if (inst.get("transport") == "AETHER_HF" and old_cmd
                    and "cmd_port" not in inst):
                inst["cmd_port"] = old_cmd
                inst["data_port"] = old_cmd + 1

        upd = raw.get("updater")
        if isinstance(upd, dict):
            upd.pop("include_aether", None)

        return raw

    def _migrate(self, cfg: AppConfig) -> tuple[AppConfig, bool]:
        """Apply known migrations to loaded config. Returns (cfg, changed)."""
        changed = False
        data = cfg.model_dump()

        # Fix invalid APRS beacon_symbol values (legacy default was "&#").
        # Symbol table char must be "/" or "\\".
        sym = data.get("aprs", {}).get("beacon_symbol", "")
        if len(sym) < 2 or sym[0] not in ("/", "\\"):
            log.info(
                f'Migrating beacon_symbol {sym!r} → "/B" (BBS symbol)'
            )
            data.setdefault("aprs", {})["beacon_symbol"] = "/B"
            cfg = AppConfig.model_validate(data)
            changed = True

        # Fix beacon_text accidentally set to "BBS OFFLINE" (offline beacon text).
        bt = data.get("station", {}).get("beacon_text", "")
        if bt.strip().upper() == "BBS OFFLINE":
            log.info("Migrating beacon_text \"BBS OFFLINE\" → \"Aether BBS\"")
            data.setdefault("station", {})["beacon_text"] = "Aether BBS"
            cfg = AppConfig.model_validate(data)
            changed = True

        # Rebrand: settings still at the VARA BBS defaults take the new name.
        # Anything the sysop wrote themselves is left alone, and so is the
        # ntfy topic (phones are subscribed to it by name).
        for section, key, old, new in (
            ("station", "beacon_text", "VARA BBS", "Aether BBS"),
            ("bbs", "welcome_message",
             "Welcome to the VARA BBS. Type H for help.",
             "Welcome to the Aether BBS. Type H for help."),
        ):
            if data.get(section, {}).get(key) == old:
                log.info(f'Migrating {section}.{key} "{old}" → "{new}"')
                data[section][key] = new
                cfg = AppConfig.model_validate(data)
                changed = True

        # Migrate file_upload: if blocked_callsigns exist but permission_mode
        # is absent (pre-whitelist config), default to "blocklist" to preserve
        # existing sysop behavior instead of the new "whitelist" default.
        fu = data.get("file_upload", {})
        if "permission_mode" not in fu and fu.get("blocked_callsigns"):
            log.info(
                'Migrating file_upload: setting permission_mode="blocklist" '
                '(blocked_callsigns present)'
            )
            data.setdefault("file_upload", {})["permission_mode"] = "blocklist"
            cfg = AppConfig.model_validate(data)
            changed = True

        # Auto-generate machine_secret if absent (used for password storage)
        sec = data.get("security", {})
        if not sec.get("machine_secret"):
            import secrets as _secrets
            secret = _secrets.token_hex(32)
            data.setdefault("security", {})["machine_secret"] = secret
            cfg = AppConfig.model_validate(data)
            changed = True
            log.info("Generated machine_secret for password encryption")

        # Encrypt Winlink gateway_password at rest if still plaintext
        from core.crypto import ENC_PREFIX, encrypt_value
        machine = data.get("security", {}).get("machine_secret", "")
        gw_pw = data.get("winlink", {}).get("gateway_password", "")
        if gw_pw and not gw_pw.startswith(ENC_PREFIX) and machine:
            enc = encrypt_value(gw_pw, machine)
            data.setdefault("winlink", {})["gateway_password"] = enc
            cfg = AppConfig.model_validate(data)
            changed = True
            log.info("Encrypted Winlink gateway_password at rest")

        # Encrypt Aether HF control tokens at rest
        if machine:
            for inst in data.get("instances", []):
                ae = inst.get("aether") or {}
                tok = ae.get("control_token", "")
                if tok and not tok.startswith(ENC_PREFIX):
                    ae["control_token"] = encrypt_value(tok, machine)
                    cfg = AppConfig.model_validate(data)
                    changed = True
                    log.info(f"Encrypted Aether HF control token for {inst.get('name', '?')}")

        # Encrypt tactical address Winlink passwords at rest
        if machine:
            for ta in data.get("station", {}).get("tactical_addresses", []):
                ta_pw = ta.get("winlink_password", "")
                if ta_pw and not ta_pw.startswith(ENC_PREFIX):
                    ta["winlink_password"] = encrypt_value(ta_pw, machine)
                    changed = True
                    log.info(f"Encrypted tactical Winlink password for {ta.get('callsign', '?')}")
            if changed:
                cfg = AppConfig.model_validate(data)

        return cfg, changed

    def save(self, cfg: AppConfig):
        """Atomically write config to TOML."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".toml.tmp")
        # TOML has no null: unset optional fields (e.g. latitude) are omitted
        data = cfg.model_dump(exclude_none=True)
        # pydantic nested models → plain dicts; tomli_w handles the rest
        tmp.write_bytes(tomli_w.dumps(data).encode("utf-8"))
        tmp.replace(self._path)
        self._config = cfg
        log.debug(f"Config saved to {self._path}")

    def reload(self) -> AppConfig:
        return self.load()

    def current(self) -> Optional[AppConfig]:
        return self._config

    def _backup_bad_config(self):
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup = self._path.with_suffix(f".toml.bad.{ts}")
        try:
            shutil.copy2(self._path, backup)
            log.info(f"Bad config backed up to {backup}")
        except Exception as e:
            log.warning(f"Could not back up bad config: {e}")
