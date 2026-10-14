"""
bbs/interactive.py

Interactive BBS session handler.
Presents a menu-driven interface to connecting stations.
Implements packet-aware output pacing (waits for BUFFER 0 before prompts).
"""

import time
import queue
import logging
import threading
from typing import Optional

from bbs.store import MessageStore

# Sentinel pushed into _rx_queue by feed_line(None) to signal RF disconnect.
# Distinct from None (queue timeout) so the session loop can tell them apart.
_RF_DISCONNECTED = object()

# Sentinel pushed into _rx_queue when the sysop initiates a chat from the GUI.
_SYSOP_CHAT_INITIATE = object()

# Sentinel pushed into _rx_queue when the sysop ends a chat from the GUI.
_SYSOP_CHAT_END = object()
from bbs.bulletins import BulletinStore
from core.config import AppConfig
from core.events import EventBus, EventType

log = logging.getLogger(__name__)

HELP_TEXT = """\r
Commands:\r
  L [area]    - List personal messages\r
  R <n>       - Read message n\r
  S <call>    - Send local message (held for RF pickup)\r
  SW <call>   - Send via Winlink CMS\r
  K <n>       - Kill (delete) your message n\r
  KA          - Kill ALL messages (sysop only)\r
  A           - List message areas\r
  U           - Your personal messages\r
  N           - New messages since last login\r
  LB [area]   - List bulletins (all areas or one area)\r
  RB <n>      - Read bulletin n\r
  SB <group>  - Send bulletin to group (ALL, WX, LOCAL, DX ...)\r
  SUB <group> - Send URGENT bulletin (flagged for immediate attention)\r
  KB <n>      - Kill your bulletin n (sysop: any)\r
  BA          - List bulletin areas\r
  FA               - List file areas\r
  FL [area]        - List files\r
  FG <area> <file> - Download a file (BASE64 stream + CRC-32)\r
  FP <area> <file> - Upload a file  (BASE64 stream + CRC-32)\r
  FD <area> <file> - Delete a file  (own uploads; sysop: any)\r
  MH          - MHeard - recently heard stations\r
  I           - BBS info\r
  P <n>       - Set pager lines (default 20)\r
  CH          - Chat with sysop (if available)\r
  CHAT [room] - Join multi-user chat room (default: LOCAL)\r
  S user@addr - Send via Winlink (email addresses always route to CMS)\r
  WL          - Check Winlink CMS for your mail (on-demand)\r
  WLREG       - Register for Winlink email forwarding\r
  WLOFF       - Unregister from Winlink forwarding\r
  TACTICAL <addr> - Set tactical address for WL/send (TACTICAL OFF to clear)\r
  SETPASS <pw>- Set or change your login password\r
  REGISTER    - Request access (if not yet registered)\r
  B / 73      - Bye\r
  H / ?       - This help\r
"""

# ── RF connect banner ─────────────────────────────────────────────────────────
# Displayed to connecting stations when config.bbs.show_connect_banner is True.
# Pure 7-bit ASCII, 53 chars wide, ~500 bytes — fast even at 100 bps.

_CONNECT_BANNER = (
    "\r\n"
    " +--------------------------------------------------+\r\n"
    " |   ___   ___   ___                                |\r\n"
    " |  | _ ) | _ ) / __|                               |\r\n"
    " |  | _ \\ | _ \\ \\__ \\   A E T H E R  B B S          |\r\n"
    " |  |___/ |___/ |___/                               |\r\n"
    " |                                                  |\r\n"
    " |  Station : {bbs_call:<14}  Grid : {grid:<8}       |\r\n"
    " |  Sysop   : {sysop:<14}  QRG  : {freq:<8}       |\r\n"
    " +--------------------------------------------------+\r\n"
    "\r\n"
)


class InteractiveSession:
    """
    Handles one interactive BBS session.
    Runs in its own thread; communicates via a line queue.
    """

    def __init__(
        self,
        remote_call: str,
        instance_name: str,
        config: AppConfig,
        store: MessageStore,
        event_bus: EventBus,
        send_text_fn,    # callable(str) — sends text, handles pacing
        disconnect_fn,   # callable() — disconnects the RF session
        bulletin_store: Optional[BulletinStore] = None,
        file_store=None,
        b2f_handoff_fn=None,  # callable(first_line) — hand off to B2F session
        access_controller=None,  # bbs.access.AccessController
        mode_policy=None,        # bbs.mode_policy.ModePolicy
        rate_limiter=None,       # bbs.rate_limiter.RateLimiter
        outbound_queue=None,     # winlink.queue.OutboundQueue
        pat_client=None,         # winlink.pat_client.PatClient for on-demand CMS sync
        transport_label: str = "VARA_FM",  # transport type for INFO command
        converse_manager=None,   # bbs.converse.ConversationManager
    ):
        self.remote_call   = remote_call.upper()
        self.instance_name = instance_name
        self._config       = config
        self._store        = store
        self._bbs          = bulletin_store   # may be None if bulletins disabled
        self._files        = file_store        # may be None if file store disabled
        self._bus          = event_bus
        self._send         = send_text_fn
        self._disconnect   = disconnect_fn
        self._access       = access_controller
        self._policy       = mode_policy
        self._rate_limiter = rate_limiter
        self._outbound_queue = outbound_queue
        self._pat_client   = pat_client
        self._transport_label = transport_label
        self._converse     = converse_manager

        self._rx_queue: queue.Queue[str] = queue.Queue()
        self._running  = True
        self._prefs    = store.get_user_prefs(remote_call)
        self._pager    = self._prefs.get("pager_lines", 20)
        self._session_id: Optional[int] = None
        self._msgs_sent = 0
        self._msgs_recv = 0

        # Cache sysop callsign for privilege checks
        self._is_sysop = (
            self.remote_call == config.station.sysop_callsign.upper()
        )
        self._b2f_handoff = b2f_handoff_fn
        # Set once the RF link is gone: every prompt then ends at once
        self._link_down = False
        # Area of the last L / LB, so R/K and RB/KB number the same list
        self._list_area: Optional[str] = None
        self._bulletin_area: Optional[str] = None
        self._role = None  # set during access gate in run()
        self._tactical_call: Optional[str] = None  # active tactical address for session

        # Set when a new message for this station arrives mid-session.
        # Checked at every _show_prompt so the notification appears between commands.
        self._pending_notify = threading.Event()
        # Event subscriptions are registered in run() and cleaned up in its finally block

    def _on_sysop_chat_initiate(self, event):
        """Inject the chat-initiate sentinel if this event targets our instance."""
        if (event.data or {}).get("instance") == self.instance_name:
            self._rx_queue.put(_SYSOP_CHAT_INITIATE)

    def _on_sysop_chat_end(self, event):
        """Inject the chat-end sentinel if this event targets our instance."""
        if (event.data or {}).get("instance") == self.instance_name:
            self._rx_queue.put(_SYSOP_CHAT_END)

    def feed_line(self, line):
        """Called by the dispatcher when a line arrives from the remote station.
        Pass None to signal an RF disconnect and unblock any waiting _get_line."""
        self._rx_queue.put(_RF_DISCONNECTED if line is None else line)

    def run(self):
        """Main session loop. Call in a dedicated thread."""
        self._session_id = self._store.log_session_start(
            self.remote_call, self.instance_name, "INTERACTIVE"
        )

        # Subscribe to events here (cleaned up in finally block below)
        self._bus.subscribe(EventType.MESSAGE_RECEIVED, self._on_message_received)
        self._bus.subscribe(EventType.SYSOP_CHAT_INITIATE, self._on_sysop_chat_initiate)
        self._bus.subscribe(EventType.SYSOP_CHAT_END, self._on_sysop_chat_end)

        try:
            # ── ACCESS CONTROL GATE ──────────────────────────────────
            if self._access:
                from bbs.access import Role
                self._role = self._access.get_role(self.remote_call)
                self._access.audit(self.remote_call, "CONNECT",
                                   f"role={self._role.value}",
                                   self.instance_name)

                # BLOCKED — terminate immediately
                if self._role == Role.BLOCKED:
                    self._send("Access denied. 73.\r\n")
                    time.sleep(0.3)
                    self._disconnect()
                    return

                # Open mode: auto-approve unknown users
                if (self._policy and self._policy.auto_approve_on_connect
                        and self._role == Role.UNKNOWN):
                    self._access.set_role(self.remote_call, Role.APPROVED,
                                          changed_by="AUTO",
                                          reason="open_mode_auto_approve")
                    self._role = Role.APPROVED

                # UNKNOWN — restricted session
                if self._role == Role.UNKNOWN:
                    self._run_unknown_session()
                    return

                # PENDING — waiting for approval
                if self._role == Role.PENDING:
                    self._run_pending_session()
                    return

                # APPROVED / TRUSTED / SYSOP — continue to full session
                self._is_sysop = (self._role == Role.SYSOP)
            else:
                self._role = None  # no access control — legacy behavior

            # ── Full session banner ──────────────────────────────────
            if self._config.bbs.show_connect_banner:
                cfg = self._config
                freq = ""
                for inst in cfg.instances:
                    if inst.enabled and inst.frequency_label.strip():
                        freq = inst.frequency_label.strip()
                        break
                self._send(_CONNECT_BANNER.format(
                    bbs_call=cfg.station.bbs_callsign,
                    sysop=cfg.station.sysop_callsign,
                    grid=cfg.station.grid_square or "N/A",
                    freq=freq or "N/A",
                ))
            self._send(f"\r\n{self._config.bbs.welcome_message}\r\n")
            self._send(f"Hello {self.remote_call}, you are connected to "
                       f"{self._config.station.bbs_callsign}\r\n")
            if self._role:
                self._send(f"Access level: {self._role.value}\r\n")
            # Show advertised tactical addresses
            adv = [ta for ta in self._config.station.tactical_addresses
                   if ta.callsign and ta.advertised]
            if adv:
                self._send("\r\nAlert addresses (use S <address> to send):\r\n")
                for ta in adv:
                    self._send(f"  {ta.callsign:<10}{ta.alert_phrase}\r\n")

            # ── Authentication (if password set) ─────────────────────
            # Sysop must also authenticate over RF — callsign SSIDs are
            # not cryptographically verified, so any station transmitting
            # as KK4ODA-N would otherwise get unauthenticated sysop access.
            if self._access:
                if not self._authenticate_if_required():
                    return

            # Notify connecting station of any waiting messages
            waiting = self._store.get_messages_for(self.remote_call)
            unread  = [m for m in waiting if not m.get("is_read")]
            if unread:
                plural = "s" if len(unread) != 1 else ""
                self._send(
                    f"\r\n*** You have {len(unread)} message{plural}"
                    f" waiting. Type L to list. ***\r\n"
                )

            # Notify of new bulletins since last visit
            if self._bbs:
                last_seen = self._prefs.get("last_seen")
                new_bulls = self._bbs.get_new_bulletins(since=last_seen)
                if new_bulls:
                    n = len(new_bulls)
                    plural = "s" if n != 1 else ""
                    self._send(
                        f"*** {n} new bulletin{plural} since your"
                        f" last visit. Type LB to list. ***\r\n"
                    )

                # Check for urgent bulletins the user may not have seen
                urgent_bulls = self._bbs.get_urgent_bulletins(
                    since=last_seen,
                )
                # Also check area-based urgency for non-flagged bulletins
                urgent_areas = set()
                try:
                    ncfg = self._config.notifications
                    urgent_areas = {a.upper() for a in ncfg.urgent_bulletin_areas}
                except AttributeError:
                    pass
                if urgent_areas and new_bulls:
                    area_urgent = [
                        b for b in new_bulls
                        if (b.get("area") or "").upper() in urgent_areas
                        and not b.get("is_urgent")
                    ]
                else:
                    area_urgent = []

                all_urgent = list(urgent_bulls) + area_urgent
                if all_urgent:
                    nu = len(all_urgent)
                    plural = "s" if nu != 1 else ""
                    areas_hit = sorted({
                        (b.get("area") or b.get("to_call") or "").upper()
                        for b in all_urgent
                    })
                    self._send(
                        f"\r\n"
                        f"!!! URGENT: {nu} bulletin{plural} in "
                        f"{', '.join(areas_hit)} !!!\r\n"
                        f"!!! Type LB <area> to read. !!!\r\n"
                    )
            # Internet status notice
            if not self._check_internet():
                self._send(
                    "\r\n"
                    "*** NOTICE: BBS internet connection is down. ***\r\n"
                    "*** Winlink and email messages will be queued ***\r\n"
                    "*** and delivered when connectivity returns.  ***\r\n"
                )

            self._show_prompt()

            # ── Main command loop ────────────────────────────────────
            timeout = (self._policy.session_timeout
                       if self._policy
                       else self._config.bbs.session_timeout)

            while self._running:
                line = self._get_line(timeout=timeout)
                if self._link_down:
                    log.info(f"[{self.instance_name}] RF disconnected: {self.remote_call}")
                    break  # link is gone — do not attempt to send
                if line is None:
                    log.info(f"[{self.instance_name}] Session timeout for {self.remote_call}")
                    self._send("\r\nSession timeout. 73 de BBS.\r\n")
                    break
                if line is _SYSOP_CHAT_INITIATE:
                    self._cmd_chat(sysop_initiated=True)
                    continue

                self._handle_command(line.strip())

        except Exception as e:
            log.error(f"Interactive session error for {self.remote_call}: {e}", exc_info=True)
        finally:
            self._bus.unsubscribe(EventType.MESSAGE_RECEIVED, self._on_message_received)
            self._bus.unsubscribe(EventType.SYSOP_CHAT_INITIATE, self._on_sysop_chat_initiate)
            self._bus.unsubscribe(EventType.SYSOP_CHAT_END, self._on_sysop_chat_end)
            self._store.log_session_end(
                self._session_id,
                msgs_sent=self._msgs_sent,
                msgs_recv=self._msgs_recv,
            )
            self._store.save_user_prefs(self.remote_call, pager_lines=self._pager)
            if self._access:
                self._access.audit(self.remote_call, "SESSION_END", "",
                                   self.instance_name)
            self._bus.emit(EventType.SESSION_ENDED, source=self.instance_name,
                           data={"remote_call": self.remote_call})
            log.info(f"Interactive session ended: {self.remote_call}")

    # ── Restricted session loops ─────────────────────────────────────────

    def _run_unknown_session(self):
        """Session loop for UNKNOWN users — REGISTER or BYE only."""
        self._send(
            "\r\n"
            " +--------------------------------------------------+\r\n"
            " |   Aether BBS -- Access Restricted                  |\r\n"
            " +--------------------------------------------------+\r\n"
            "\r\n"
            f"Hello {self.remote_call}.\r\n"
            "This BBS requires registration.\r\n"
            "Type REGISTER to request access, or BYE to disconnect.\r\n"
        )
        self._send("> ")
        while self._running:
            line = self._get_line(timeout=120)
            if line is _RF_DISCONNECTED or line is None:
                break
            cmd = line.strip().upper() if isinstance(line, str) else ""
            if cmd in ("B", "BYE", "73", "QUIT", "EXIT"):
                self._send("73 de BBS.\r\n")
                self._running = False
                time.sleep(0.3)
                self._disconnect()
                return
            elif cmd in ("REGISTER", "REG"):
                if self._rate_limited("register", prompt=False):
                    self._send("> ")
                    continue
                if self._access:
                    ref = self._prompt_community_ref()
                    if ref is None:
                        break  # RF disconnect during prompt
                    ok, msg = self._access.register(
                        self.remote_call, self.instance_name,
                        community_ref=ref,
                    )
                    self._send(msg)
                    if ok:
                        time.sleep(0.5)
                        self._disconnect()
                        return
                else:
                    self._send("Registration not available.\r\n")
            else:
                self._send("Type REGISTER or BYE.\r\n")
            self._send("> ")

    def _run_pending_session(self):
        """Session loop for PENDING users — inform and BYE only."""
        self._send(
            f"\r\nHello {self.remote_call}.\r\n"
            "Your registration is pending sysop review.\r\n"
            "Type BYE to disconnect. You will be notified when approved.\r\n"
        )
        self._send("> ")
        while self._running:
            line = self._get_line(timeout=120)
            if line is _RF_DISCONNECTED or line is None:
                break
            cmd = line.strip().upper() if isinstance(line, str) else ""
            if cmd in ("B", "BYE", "73", "QUIT", "EXIT"):
                self._send("73 de BBS.\r\n")
                self._running = False
                time.sleep(0.3)
                self._disconnect()
                return
            else:
                self._send("Your access is pending. Type BYE to disconnect.\r\n")
            self._send("> ")

    # ── Registration helpers ─────────────────────────────────────────

    def _prompt_community_ref(self) -> Optional[str]:
        """Prompt the connecting station for a community trust reference.

        Returns the answer string, or None if the RF link dropped.
        """
        base = self.remote_call.split("-")[0].upper()
        self._send(
            f"\r\nRegistering callsign: {base} (no SSID)\r\n"
            "\r\n"
            "We rely on existing trust networks to verify new users.\r\n"
            "Name a groups.io or Facebook group where you have been\r\n"
            "active (or type NONE if not applicable):\r\n> "
        )
        line = self._get_line(timeout=120)
        if line is _RF_DISCONNECTED or line is None:
            return None
        return line.strip() if isinstance(line, str) else ""

    # ── Authentication ────────────────────────────────────────────────

    def _authenticate_if_required(self) -> bool:
        """
        If this callsign has a password set, run challenge-response.
        Returns True if auth succeeded or no password required.
        Returns False if auth failed (session will be terminated).
        """
        if not self._access:
            return True
        if not self._access.has_password(self.remote_call):
            # If mode requires password but none set, inform and disconnect
            if self._policy and self._policy.require_password:
                base = self.remote_call.split("-")[0].upper()
                self._send(
                    "\r\n*** This BBS requires a password. ***\r\n"
                    "Your password has not yet been provisioned.\r\n"
                    f"It will be sent to you via Winlink ({base}@winlink.org)\r\n"
                    "once your account has been approved by the sysop.\r\n"
                    "Disconnecting now.\r\n"
                )
                time.sleep(0.5)
                self._disconnect()
                return False
            return True

        # Has password — HMAC-SHA256 challenge-response
        if self._rate_limiter and self._rate_limiter.exhausted(
                self.remote_call, "auth_failure"):
            self._send("Too many failed authentication attempts.\r\n"
                       "Try again later.\r\n")
            time.sleep(0.5)
            self._disconnect()
            return False
        nonce = self._access.generate_challenge(self.remote_call)
        self._send(f";AUTH HMAC-SHA256 {nonce}\r\n")
        self._send("Enter password (or HMAC client will respond automatically):\r\n")
        self._send("> ")

        line = self._get_line(timeout=120)
        # The TERM client may send ";TRANSPORT?" before the auth response.
        # Consume and reply to it so the real auth line is read next.
        while (line is not None and isinstance(line, str)
               and line is not _RF_DISCONNECTED
               and line.strip().upper().startswith(";TRANSPORT")):
            self._send(f";TRANSPORT {self._transport_label}\r\n")
            line = self._get_line(timeout=120)

        if self._link_down:
            return False   # the link dropped: not a failed password
        if line is None or not isinstance(line, str) or line is _RF_DISCONNECTED:
            self._send("Authentication timed out.\r\n")
            self._access.record_auth_failure(self.remote_call)
            self._count_auth_failure()
            self._disconnect()
            return False

        response = line.strip()

        # Smart client sends ";AUTH <64-char hex HMAC>"
        if response.startswith(";AUTH "):
            response = response[6:].strip()

        if not response:
            self._send(";AUTH FAIL\r\n")
            self._access.record_auth_failure(self.remote_call)
            self._count_auth_failure()
            self._disconnect()
            return False

        if self._access.verify_challenge(self.remote_call, response):
            self._send(";AUTH OK\r\n")
            return True
        else:
            self._send(";AUTH FAIL\r\n")
            self._count_auth_failure()
            time.sleep(0.5)
            self._disconnect()
            return False

    def _count_auth_failure(self):
        if self._rate_limiter:
            self._rate_limiter.check(self.remote_call, "auth_failure")

    _RATE_LIMITED_TEXT = {
        "message_send":  "You have sent too many messages",
        "bulletin_post": "You have posted too many bulletins",
        "file_upload":   "You have uploaded too many files",
        "register":      "Too many registration attempts",
    }

    def _rate_limited(self, event: str, prompt: bool = True) -> bool:
        """Count one *event* for this station; when it is over the limit
        for the operational mode, say so and return True."""
        if not self._rate_limiter:
            return False
        allowed, count = self._rate_limiter.check(self.remote_call, event)
        if allowed:
            return False
        self._send(f"{self._RATE_LIMITED_TEXT.get(event, 'Rate limit exceeded')}"
                   f". Try again later.\r\n")
        if self._access:
            self._access.audit(self.remote_call, "RATE_LIMITED",
                               f"{event} count={count}", self.instance_name)
        if prompt:
            self._show_prompt()
        return True

    # ── Tactical address ─────────────────────────────────────────────────

    def _set_tactical(self, requested: str, machine_readable: bool = False):
        """Set or clear the session tactical address."""
        if not requested or requested == "OFF":
            self._tactical_call = None
            if machine_readable:
                self._send(";TACTICAL OK cleared\r\n")
            else:
                self._send("Tactical address cleared.\r\n")
                self._show_prompt()
            return

        # Validate against configured tactical addresses
        valid = {ta.callsign.upper() for ta in self._config.station.tactical_addresses
                 if ta.callsign}
        if requested not in valid:
            if machine_readable:
                self._send(f";TACTICAL FAIL not configured\r\n")
            else:
                avail = ", ".join(sorted(valid)) or "none"
                self._send(
                    f"Tactical address '{requested}' is not configured.\r\n"
                    f"Available: {avail}\r\n"
                )
                self._show_prompt()
            return

        self._tactical_call = requested
        if machine_readable:
            self._send(f";TACTICAL OK {requested}\r\n")
        else:
            self._send(f"Tactical address set to {requested}\r\n")
            self._send(f"WL will poll for {requested}. Outbound messages will be sent from {requested}.\r\n")
            self._show_prompt()

    # ── Command dispatch ──────────────────────────────────────────────────

    def _handle_command(self, cmd: str):
        # Detect a Winlink B2F session request — remote sends "[WL2K-...]"
        # Hand off transparently so Winlink clients get a proper B2F exchange.
        if cmd.startswith("[") and self._b2f_handoff:
            log.info(f"[{self.instance_name}] B2F header detected from "
                     f"{self.remote_call} — handing off to B2F session")
            self._running = False
            self._b2f_handoff(cmd)
            return

        upper = cmd.upper()

        # ── Machine-readable transport query (for terminal auto-detect) ──
        if upper.startswith(";TRANSPORT"):
            self._send(f";TRANSPORT {self._transport_label}\r\n")
            self._show_prompt()
            return

        # ── Tactical address (machine-readable or user-typed) ──────────
        if upper.startswith(";TACTICAL ") or upper.startswith("TACTICAL "):
            machine_readable = upper.startswith(";")
            arg = cmd.split(None, 1)[1].strip().upper() if " " in cmd else ""
            self._set_tactical(arg, machine_readable)
            return
        if upper == "TACTICAL":
            self._send("Usage: TACTICAL <address> or TACTICAL OFF\r\n")
            self._show_prompt()
            return

        # ── Rate limiting ────────────────────────────────────────────
        if self._rate_limiter:
            allowed, _ = self._rate_limiter.check(self.remote_call, "command")
            if not allowed:
                self._send("Rate limit exceeded. Please slow down.\r\n")
                self._show_prompt()
                return

        # ── REGISTER / SETPASS — always available ────────────────────
        if upper in ("REGISTER", "REG"):
            if self._rate_limited("register"):
                return
            if self._access:
                ref = self._prompt_community_ref()
                if ref is None:
                    return  # RF disconnect during prompt
                ok, msg = self._access.register(self.remote_call,
                                                self.instance_name,
                                                community_ref=ref)
                self._send(msg)
                if ok:
                    time.sleep(0.5)
                    self._disconnect()
                    self._running = False
                    return
            else:
                self._send("Registration not available.\r\n")
            self._show_prompt()
            return

        if upper == "SETPASS" or upper.startswith("SETPASS "):
            if not self._access:
                self._send("Password management not available.\r\n")
                self._show_prompt()
                return
            # If password given inline, use it; otherwise prompt
            pw = cmd[8:].strip() if len(cmd) > 8 else ""
            if not pw:
                self._send("Enter new password (min 4 chars):\r\n> ")
                line = self._get_line(timeout=60)
                if line is None or line is _RF_DISCONNECTED:
                    return
                pw = line.strip() if isinstance(line, str) else ""
            if len(pw) < 4:
                self._send("Password must be at least 4 characters.\r\n")
            else:
                self._access.set_password(self.remote_call, pw)
                self._send("Password set. You will be prompted on future connections.\r\n")
            self._show_prompt()
            return

        # ── Winlink registration ─────────────────────────────────────
        if upper == "WLREG":
            self._cmd_wlreg()
            return

        if upper == "WLOFF":
            self._store.set_winlink_registered(self.remote_call, False)
            self._send("Winlink forwarding disabled for your callsign.\r\n")
            self._show_prompt()
            return

        if upper == "WL":
            self._cmd_wl_poll()
            return

        # ── Access control permission check ──────────────────────────
        if self._access and not self._access.has_permission(
                self.remote_call, cmd, policy=self._policy):
            self._send("Permission denied for this command.\r\n")
            if self._access:
                self._access.audit(self.remote_call, "COMMAND_DENIED",
                                   f"cmd={cmd[:40]}", self.instance_name)
            self._show_prompt()
            return

        if upper in ("H", "?", "HELP"):
            self._send(HELP_TEXT)
            self._show_prompt()

        elif upper.startswith("L") and not upper.startswith("LB"):
            parts = upper.split()
            area = parts[1] if len(parts) > 1 else None
            self._cmd_list(area)

        elif upper.startswith("R ") and not upper.startswith("RB"):
            self._cmd_read(upper[2:].strip())

        elif upper.startswith("SW "):
            dest = cmd[3:].strip()
            self._cmd_send(dest, force_winlink=True)

        elif upper.startswith("S ") and not upper.startswith("SB"):
            dest = cmd[2:].strip()
            self._cmd_send(dest)

        elif upper == "KA":
            self._cmd_kill_all()

        elif upper.startswith("K ") and not upper.startswith("KB"):
            self._cmd_kill(upper[2:].strip())

        elif upper == "A":
            self._cmd_areas()

        elif upper == "U":
            self._cmd_my_messages()

        elif upper == "N":
            self._cmd_new_messages()

        # ── Bulletin commands ─────────────────────────────────────────────

        elif upper.startswith("LB"):
            parts = upper.split()
            area = parts[1] if len(parts) > 1 else None
            self._cmd_list_bulletins(area)

        elif upper.startswith("RB "):
            self._cmd_read_bulletin(upper[3:].strip())

        elif upper.startswith("SB "):
            group = cmd[3:].strip()
            self._cmd_send_bulletin(group)

        elif upper.startswith("SUB "):
            group = cmd[4:].strip()
            self._cmd_send_bulletin(group, urgent=True)

        elif upper.startswith("KB "):
            self._cmd_kill_bulletin(upper[3:].strip())

        elif upper == "BA":
            self._cmd_bulletin_areas()

        # ── Other commands ────────────────────────────────────────────────

        elif upper == "MH":
            self._cmd_mheard()

        elif upper == "I":
            self._cmd_info()

        elif upper.startswith("P "):
            try:
                n = int(upper[2:].strip())
                self._pager = max(5, min(n, 100))
                self._send(f"Pager set to {self._pager} lines.\r\n")
            except ValueError:
                self._send("Usage: P <number>\r\n")
            self._show_prompt()

        elif upper == "FA":
            self._cmd_file_areas()

        elif upper.startswith("FL"):
            parts = upper.split()
            area = parts[1] if len(parts) > 1 else None
            self._cmd_file_list(area)

        elif upper.startswith("FG "):
            self._cmd_file_get(cmd[3:].strip())

        elif upper.startswith("FP "):
            self._cmd_file_put(cmd[3:].strip())

        elif upper.startswith("FD "):
            self._cmd_file_delete(cmd[3:].strip())

        elif upper in ("B", "73", "BYE", "QUIT", "EXIT"):
            self._send(f"73 de {self._config.station.bbs_callsign}. Good day!\r\n")
            self._running = False
            time.sleep(0.5)
            self._disconnect()

        elif upper == "CH":
            self._cmd_chat()

        elif upper.startswith("CHAT") or upper.startswith("CONVERSE"):
            parts = cmd.split(None, 1)
            room_name = parts[1].strip().upper() if len(parts) > 1 else ""
            self._cmd_converse(room_name)

        else:
            self._send(f"Unknown command: {cmd!r}. Type H for help.\r\n")
            self._show_prompt()

    # ── BBS commands ──────────────────────────────────────────────────────

    def _cmd_wlreg(self):
        """Register for Winlink email forwarding with encrypted password exchange.

        Protocol:
          1. BBS sends  ;WLREG NONCE <32hex>
          2. Client encrypts Winlink password:
             key = SHA256(bbs_password + nonce), then XOR
          3. Client sends  ;WLREG <hex_encrypted>
          4. BBS decrypts using same key (it knows the BBS password)
        """
        import hashlib as _hl

        if not self._access:
            self._send("Winlink registration not available.\r\n")
            self._show_prompt()
            return

        # Get the user's decrypted BBS password (needed as shared key)
        bbs_password = self._access.get_decrypted_password(self.remote_call)
        if not bbs_password:
            self._send(
                "You must have a BBS password set before registering for Winlink.\r\n"
                "Use SETPASS to set one first.\r\n"
            )
            self._show_prompt()
            return

        # Generate nonce and send challenge
        import secrets as _sec
        nonce = _sec.token_hex(16)
        self._send(f";WLREG NONCE {nonce}\r\n")
        self._send("Your terminal program (e.g. VARA Term) will prompt for "
                   "your Winlink password.\r\n")

        # Wait for encrypted response
        line = self._get_line(timeout=120)
        if line is None or line is _RF_DISCONNECTED:
            return

        response = line.strip()
        if response.startswith(";WLREG "):
            encrypted_hex = response[7:].strip()
        else:
            # User typed something else — maybe a raw password (insecure fallback)
            self._send("Registration cancelled.\r\n")
            self._show_prompt()
            return

        # Decrypt: key = SHA256(bbs_password + nonce), XOR with encrypted bytes
        try:
            enc_bytes = bytes.fromhex(encrypted_hex)
            key = _hl.sha256((bbs_password + nonce).encode("utf-8")).digest()
            # Extend key to match encrypted length
            full_key = key * ((len(enc_bytes) // len(key)) + 1)
            wl_password = bytes(
                eb ^ kb for eb, kb in zip(enc_bytes, full_key)
            ).decode("utf-8")
        except Exception as e:
            log.warning(f"WLREG decrypt failed for {self.remote_call}: {e}")
            self._send("Registration failed — decryption error.\r\n")
            self._show_prompt()
            return

        if not wl_password:
            self._send("Empty password received. Registration cancelled.\r\n")
            self._show_prompt()
            return

        self._store.set_winlink_registered(self.remote_call, True, wl_password)
        self._send(
            "Winlink forwarding enabled for your callsign.\r\n"
            "Use the WL command to check Winlink CMS for your mail.\r\n"
        )
        self._show_prompt()

    def _cmd_wl_poll(self):
        """On-demand Winlink CMS check for the connected RF user.

        Polls for the user's own callsign (if WLREG'd) AND the active
        tactical address (if set), in a single CMS sync.  Tactical
        addresses share the gateway's Winlink password since they are
        aliases for the BBS station, not separate Winlink accounts.
        """
        if not self._pat_client or not self._pat_client.is_enabled:
            self._send("Winlink is not enabled on this BBS.\r\n")
            self._show_prompt()
            return

        from core.crypto import decrypt_value
        machine = self._config.security.machine_secret or ""

        # ── User's own Winlink credentials ─────────────────────────────
        prefs = self._store.get_user_prefs(self.remote_call)
        user_registered = prefs.get("winlink_registered", False)
        user_pw = ""
        if user_registered:
            enc_pw = prefs.get("winlink_password", "")
            user_pw = decrypt_value(enc_pw, machine) if enc_pw else ""

        # ── Tactical address shares the user's Winlink credentials ─────
        # Tactical addresses are aliases registered under the same
        # Winlink account as the connecting user, so they use the same
        # password the user provided via WLREG.

        # Must have credentials to poll
        if not user_pw:
            if not user_registered:
                self._send(
                    "You are not registered for Winlink forwarding.\r\n"
                    "Use WLREG to register first.\r\n"
                )
            else:
                self._send(
                    "No Winlink password on file. Use WLREG to re-register.\r\n"
                )
            self._show_prompt()
            return

        # ── Build poll addresses ───────────────────────────────────────
        # Primary = user callsign
        # Extra = tactical address using the same password
        extra = []
        primary_call = self.remote_call
        primary_pw = user_pw
        if self._tactical_call:
            extra.append((self._tactical_call, user_pw))

        polling_for = [primary_call]
        if extra:
            polling_for.extend(c for c, _ in extra)

        self._send(
            f"Connecting to Winlink CMS for {', '.join(polling_for)}...\r\n"
            "This may take 30-120 seconds. Please wait.\r\n"
        )

        result = self._pat_client.poll_for_user(
            primary_call, primary_pw,
            extra_addresses=extra or None,
        )

        if result.error:
            self._send(f"Winlink sync error: {result.error}\r\n")
        else:
            self._send(
                f"Winlink sync complete: {result.sent} sent, "
                f"{result.received} new messages received.\r\n"
            )
            if result.received > 0:
                self._send("Type L to list your messages.\r\n")

        self._show_prompt()

    def _listed_messages(self) -> list[dict]:
        """The messages L shows, in its order; R n and K n index this."""
        return self._store.list_messages(
            area=self._list_area, limit=100,
            to_call=None if self._is_sysop else self.remote_call,
        )

    def _cmd_list(self, area: Optional[str] = None):
        self._list_area = area
        msgs = self._listed_messages()
        if not msgs:
            self._send("No messages.\r\n")
        else:
            header = f"{'#':<4} {'FROM':<10} {'TO':<10} {'DATE':<12} SUBJECT\r\n"
            self._send(header)
            self._send("-" * 60 + "\r\n")
            for i, m in enumerate(msgs):
                read_flag = " " if m["is_read"] else "*"
                cms = m.get("cms_status", "")
                wl_tag = "[WL] " if cms in ("RECEIVED", "QUEUED", "SENT") else ""
                subj = f"{wl_tag}{m['subject']}"[:30]
                line = (
                    f"{i+1:<4}{read_flag}"
                    f"{m['from_call']:<10} {m['to_call']:<10} "
                    f"{str(m['date'])[:10]:<12} "
                    f"{subj}\r\n"
                )
                self._send(line)
                if (i + 1) % self._pager == 0:
                    self._send("-- More -- (press Enter) \r\n")
                    self._get_line(timeout=self._config.bbs.session_timeout)

        self._show_prompt()

    def _cmd_read(self, idx_str: str):
        try:
            idx = int(idx_str) - 1
        except ValueError:
            self._send("Usage: R <message number>\r\n")
            self._show_prompt()
            return

        msgs = self._listed_messages()
        if idx < 0 or idx >= len(msgs):
            self._send("Message not found.\r\n")
            self._show_prompt()
            return

        row = self._store.get_message(msgs[idx]["id"])
        if not row:
            self._send("Message not found.\r\n")
            self._show_prompt()
            return

        self._send(f"MID:     {row['id']}\r\n")
        self._send(f"From:    {row['from_call']}\r\n")
        self._send(f"To:      {row['to_call']}\r\n")
        self._send(f"Date:    {row['date']}\r\n")
        self._send(f"Subject: {row['subject']}\r\n")
        self._send("\r\n")

        body = row["body"]
        if isinstance(body, bytes):
            body = body.decode("utf-8", errors="replace")
        self._send(body + "\r\n")
        self._store.mark_read(row["id"])
        self._show_prompt()

    def _cmd_send(self, dest_call: str, force_winlink: bool = False):
        if not dest_call:
            usage = "Usage: SW <callsign>\r\n" if force_winlink else "Usage: S <callsign>\r\n"
            self._send(usage)
            self._show_prompt()
            return
        if self._rate_limited("message_send"):
            return

        # Email addresses always go via Winlink regardless of S vs SW
        has_at = "@" in dest_call
        via_label = "Winlink" if (force_winlink or has_at) else "Local"
        self._send(f"To: {dest_call.upper()}  [{via_label}]\r\n")
        self._send("Subject: ")
        subject = self._get_line(timeout=self._config.bbs.session_timeout) or ""

        self._send("Message (end with '.' on a line by itself):\r\n")
        lines = []
        while True:
            line = self._get_line(timeout=self._config.bbs.session_timeout)
            if line is None or line.strip() == ".":
                break
            lines.append(line)
        if self._link_down:
            return   # link dropped before the end: don't post half a message

        body_text = "\r\n".join(lines)
        from_call = self._tactical_call or self.remote_call
        mid = self._store.save_message(
            from_call=from_call,
            to_call=dest_call.upper(),
            subject=subject.strip(),
            body=body_text.encode("utf-8"),
            instance=self.instance_name,
        )
        self._msgs_recv += 1

        # Routing: SW or email → Winlink CMS; plain S → local RF pickup
        from winlink.queue import route_new_message
        if route_new_message(self._store, mid, dest_call,
                             force_winlink) == "QUEUED":
            if self._outbound_queue:
                self._send("Message saved and queued for Winlink delivery.\r\n")
            else:
                self._send("Message saved; it goes out once Winlink is "
                           "running.\r\n")
        else:
            self._send("Message saved (local — held for RF pickup).\r\n")
        self._show_prompt()

    def _cmd_kill(self, idx_str: str):
        try:
            idx = int(idx_str) - 1
        except ValueError:
            self._send("Usage: K <message number>\r\n")
            self._show_prompt()
            return

        msgs = self._listed_messages()
        if idx < 0 or idx >= len(msgs):
            self._send("Message not found.\r\n")
            self._show_prompt()
            return

        msg = msgs[idx]
        mine = self.remote_call in (msg["from_call"].upper(), msg["to_call"].upper())
        if not (mine or self._is_sysop):
            self._send("You can only delete your own messages.\r\n")
            self._show_prompt()
            return

        self._store.delete_message(msg["id"])
        self._send(f"Message {idx+1} deleted.\r\n")
        self._show_prompt()

    def _cmd_kill_all(self):
        """Delete all messages. Sysop only."""
        if not self._is_sysop:
            self._send("Only the sysop can delete all messages.\r\n")
            self._show_prompt()
            return

        self._send("Delete ALL messages? This cannot be undone. [Y/N] ")
        answer = self._get_line(timeout=self._config.bbs.session_timeout) or ""
        if answer.strip().upper() != "Y":
            self._send("Cancelled.\r\n")
            self._show_prompt()
            return

        count = self._store.delete_all_messages()
        self._send(f"Deleted {count} message(s).\r\n")
        log.info(f"Sysop {self.remote_call} deleted all messages ({count})")
        self._show_prompt()

    def _cmd_areas(self):
        areas = self._store.list_areas()
        if areas:
            self._send("Message areas: " + ", ".join(areas) + "\r\n")
        else:
            self._send("No message areas.\r\n")
        self._show_prompt()

    # ── Bulletin commands ─────────────────────────────────────────────────

    def _no_bulletins(self) -> bool:
        """Return True and send an error if bulletins are not available."""
        if not self._bbs:
            self._send("Bulletin board not available on this BBS.\r\n")
            self._show_prompt()
            return True
        return False

    def _cmd_list_bulletins(self, area: Optional[str] = None):
        if self._no_bulletins():
            return
        self._bulletin_area = area
        bulletins = self._bbs.list_bulletins(area=area, limit=100)
        if not bulletins:
            msg = f"No bulletins" + (f" in area {area}." if area else ".") + "\r\n"
            self._send(msg)
        else:
            label = f" [{area}]" if area else ""
            self._send(f"Bulletins{label}:\r\n")
            header = f"{'#':<4}  {'FROM':<10} {'GROUP':<10} {'DATE':<12} SUBJECT\r\n"
            self._send(header)
            self._send("-" * 64 + "\r\n")
            for i, m in enumerate(bulletins):
                urgent_flag = "!" if m.get("is_urgent") else " "
                read_flag   = " " if m["is_read"] else "*"
                line = (
                    f"{i+1:<4}{urgent_flag}{read_flag}"
                    f"{m['from_call']:<10} {m['to_call']:<10} "
                    f"{str(m['date'])[:10]:<12} "
                    f"{m['subject'][:28]}\r\n"
                )
                self._send(line)
                if (i + 1) % self._pager == 0:
                    self._send("-- More -- (press Enter) \r\n")
                    self._get_line(timeout=self._config.bbs.session_timeout)
            # Legend if any urgent bulletins exist
            if any(m.get("is_urgent") for m in bulletins):
                self._send("  (! = URGENT  * = unread)\r\n")
        self._show_prompt()

    def _cmd_read_bulletin(self, idx_str: str):
        if self._no_bulletins():
            return
        try:
            idx = int(idx_str) - 1
        except ValueError:
            self._send("Usage: RB <bulletin number>\r\n")
            self._show_prompt()
            return

        bulletins = self._bbs.list_bulletins(area=self._bulletin_area, limit=100)
        if idx < 0 or idx >= len(bulletins):
            self._send("Bulletin not found.\r\n")
            self._show_prompt()
            return

        row = self._store.get_message(bulletins[idx]["id"])
        if not row:
            self._send("Bulletin not found.\r\n")
            self._show_prompt()
            return

        self._send(f"MID:     {row['id']}\r\n")
        self._send(f"From:    {row['from_call']}\r\n")
        self._send(f"To:      {row['to_call']}  [{row['area']}]\r\n")
        self._send(f"Date:    {row['date']}\r\n")
        self._send(f"Subject: {row['subject']}\r\n")
        self._send("\r\n")

        body = row["body"]
        if isinstance(body, bytes):
            body = body.decode("utf-8", errors="replace")

        # Paged output for long bulletins
        lines = body.splitlines()
        for i, line in enumerate(lines):
            self._send(line + "\r\n")
            if (i + 1) % self._pager == 0 and i + 1 < len(lines):
                self._send("-- More -- (press Enter) \r\n")
                self._get_line(timeout=self._config.bbs.session_timeout)

        self._store.mark_read(row["id"])
        self._show_prompt()

    def _cmd_send_bulletin(self, group: str, urgent: bool = False):
        if self._no_bulletins():
            return
        if not group:
            self._send("Usage: SB <group>  (e.g. SB ALL, SB WX, SB LOCAL)\r\n")
            self._send("       SUB <group> (send URGENT bulletin)\r\n")
            # Show available areas as a hint
            areas = self._bbs.list_bulletin_areas()
            all_areas = list({
                a.name for a in self._config.bbs.bulletin_areas if a.enabled
            } | set(areas))
            if all_areas:
                self._send("Available groups: " + ", ".join(sorted(all_areas)) + "\r\n")
            self._show_prompt()
            return
        if self._rate_limited("bulletin_post"):
            return

        group_upper = group.upper()

        # Validate group against configured areas (warn but don't block)
        configured = {
            a.name.upper()
            for a in self._config.bbs.bulletin_areas
            if a.enabled
        }
        if configured and group_upper not in configured:
            self._send(
                f"Warning: {group_upper!r} is not a configured area. "
                f"Available: {', '.join(sorted(configured))}\r\n"
                f"Continue anyway? [Y/N] "
            )
            answer = self._get_line(timeout=self._config.bbs.session_timeout) or ""
            if answer.strip().upper() != "Y":
                self._send("Cancelled.\r\n")
                self._show_prompt()
                return

        urgency_label = " [URGENT]" if urgent else ""
        self._send(f"Bulletin to: {group_upper}{urgency_label}\r\n")
        self._send("Subject: ")
        subject = self._get_line(timeout=self._config.bbs.session_timeout) or ""
        if not subject.strip():
            self._send("No subject entered. Cancelled.\r\n")
            self._show_prompt()
            return

        self._send("Message (end with '.' on a line by itself):\r\n")
        lines = []
        while True:
            line = self._get_line(timeout=self._config.bbs.session_timeout)
            if line is None or line.strip() == ".":
                break
            lines.append(line)
        if self._link_down:
            return   # link dropped before the end: don't post half a message

        if not lines:
            self._send("No body entered. Bulletin not saved.\r\n")
            self._show_prompt()
            return

        # Find expiry days for this area
        expire_days = None
        for area_cfg in self._config.bbs.bulletin_areas:
            if area_cfg.name.upper() == group_upper:
                expire_days = area_cfg.max_age
                break
        if expire_days is None:
            expire_days = self._config.bbs.max_message_age

        body_text = "\r\n".join(lines)
        mid = self._bbs.save_bulletin(
            from_call=self.remote_call,
            to_group=group_upper,
            subject=subject.strip(),
            body=body_text.encode("utf-8"),
            area=group_upper,
            instance=self.instance_name,
            expires_days=expire_days,
            is_urgent=urgent,
        )
        self._msgs_recv += 1
        self._bus.emit(EventType.BULLETIN_POSTED, source=self.instance_name,
                       data={
                           "mid":       mid,
                           "from":      self.remote_call,
                           "to_group":  group_upper,
                           "subject":   subject.strip(),
                           "area":      group_upper,
                           "is_urgent": urgent,
                       })
        self._send(f"Bulletin saved{urgency_label}. MID={mid}\r\n")
        self._show_prompt()

    def _cmd_kill_bulletin(self, idx_str: str):
        if self._no_bulletins():
            return
        try:
            idx = int(idx_str) - 1
        except ValueError:
            self._send("Usage: KB <bulletin number>\r\n")
            self._show_prompt()
            return

        bulletins = self._bbs.list_bulletins(area=self._bulletin_area, limit=100)
        if idx < 0 or idx >= len(bulletins):
            self._send("Bulletin not found.\r\n")
            self._show_prompt()
            return

        b = bulletins[idx]
        # Only the author or sysop can kill a bulletin
        if b["from_call"].upper() != self.remote_call and not self._is_sysop:
            self._send("You can only delete your own bulletins.\r\n")
            self._show_prompt()
            return

        self._store.delete_message(b["id"])
        self._send(f"Bulletin {idx+1} deleted.\r\n")
        self._show_prompt()

    def _cmd_bulletin_areas(self):
        if self._no_bulletins():
            return
        configured = [
            a for a in self._config.bbs.bulletin_areas if a.enabled
        ]
        active = self._bbs.list_bulletin_areas()

        self._send("Bulletin areas:\r\n")
        shown = set()

        # Show configured areas with description and count
        for area_cfg in configured:
            name = area_cfg.name.upper()
            count = self._bbs.bulletin_count(name)
            desc = f"  {area_cfg.description}" if area_cfg.description else ""
            self._send(f"  {name:<12} {count:>4} bulletin(s){desc}\r\n")
            shown.add(name)

        # Show any active areas not in config
        for area in active:
            if area.upper() not in shown:
                count = self._bbs.bulletin_count(area)
                self._send(f"  {area.upper():<12} {count:>4} bulletin(s)\r\n")

        if not configured and not active:
            self._send("  (none)\r\n")

        self._show_prompt()

    def _cmd_my_messages(self):
        msgs = self._store.list_messages(limit=100)
        mine = [m for m in msgs if m["to_call"].upper() == self.remote_call]
        if not mine:
            self._send("No messages for you.\r\n")
        else:
            for i, m in enumerate(mine):
                read = " " if m["is_read"] else "*"
                self._send(
                    f"{i+1:<4}{read}{m['from_call']:<10} "
                    f"{str(m['date'])[:10]:<12} {m['subject'][:30]}\r\n"
                )
        self._show_prompt()

    def _cmd_new_messages(self):
        prefs = self._store.get_user_prefs(self.remote_call)
        msgs = self._store.get_new_messages_for(
            self.remote_call, since=prefs.get("last_seen")
        )
        if not msgs:
            self._send("No new messages.\r\n")
        else:
            self._send(f"{len(msgs)} new message(s):\r\n")
            for i, m in enumerate(msgs):
                self._send(
                    f"{i+1}. From {m['from_call']}: {m['subject']}\r\n"
                )
        self._show_prompt()

    def _cmd_mheard(self):
        stations = self._store.get_heard_stations(
            instance=self.instance_name, limit=20
        )
        if not stations:
            self._send("No stations heard yet.\r\n")
        else:
            self._send(f"{'CALLSIGN':<12} {'LAST HEARD':<20} {'COUNT':<6} VIA\r\n")
            self._send("-" * 55 + "\r\n")
            for s in stations:
                self._send(
                    f"{s['callsign']}{'-' + str(s['ssid']) if s['ssid'] else '':<12} "
                    f"{str(s['last_heard'])[:19]:<20} "
                    f"{s['hear_count']:<6} "
                    f"{s['via_path'] or 'direct'}\r\n"
                )
        self._show_prompt()

    def _cmd_converse(self, room_name: str = ""):
        """Join a multi-user chat room (converse mode)."""
        if not self._converse:
            self._send("Chat rooms are not available.\r\n")
            self._show_prompt()
            return

        from bbs.converse import Member

        room = self._converse.get_or_create_room(room_name)
        member = Member(
            callsign=self.remote_call,
            send_fn=self._send,
            member_type="RF",
            instance=self.instance_name,
        )
        room.join(member)

        self._send(
            f"Joined room {room.name}. "
            f"{room.member_count} user(s) here.\r\n"
            f"Type /EXIT to leave, /WHO for member list.\r\n"
        )

        try:
            while self._running:
                try:
                    line = self._rx_queue.get(timeout=1.0)
                except queue.Empty:
                    continue

                if line is _RF_DISCONNECTED:
                    self._link_lost()
                    return

                if line is None:
                    continue

                text = line.strip()
                if not text:
                    continue

                upper = text.upper()

                if upper == "/EXIT" or upper == "/QUIT":
                    room.part(self.remote_call, "left")
                    self._send("Left chat room. Type H for help.\r\n")
                    self._show_prompt()
                    return

                if upper == "/WHO":
                    members = room.list_members()
                    self._send(f"Room {room.name} ({len(members)} users):\r\n")
                    for m in members:
                        self._send(f"  {m}\r\n")
                    continue

                if upper.startswith("/ROOM "):
                    new_name = text[6:].strip().upper()
                    if new_name:
                        room.part(self.remote_call, "switching rooms")
                        room = self._converse.get_or_create_room(new_name)
                        room.join(member)
                        self._send(
                            f"Switched to room {room.name}. "
                            f"{room.member_count} user(s) here.\r\n"
                        )
                    continue

                # Normal message — broadcast to room
                room.broadcast(self.remote_call, text)

        finally:
            # Ensure cleanup on any exit
            self._converse.remove_user(self.remote_call)

    def _cmd_info(self):
        self._send(f"BBS:       {self._config.station.bbs_callsign}\r\n")
        self._send(f"Sysop:     {self._config.station.sysop_callsign}\r\n")
        self._send(f"Grid:      {self._config.station.grid_square or 'N/A'}\r\n")
        self._send(f"Transport: {self._transport_label}\r\n")
        self._send(f"Port:      {self.instance_name}\r\n")
        if self._role:
            self._send(f"Access:    {self._role.value}\r\n")
        if self._policy:
            self._send(f"Mode:      {self._policy.mode.upper()}\r\n")
        self._send(f"Messages:  {self._store.message_count()} on file\r\n")
        if self._bbs:
            self._send(f"Bulletins: {self._bbs.bulletin_count()} on file\r\n")
            areas = self._bbs.list_bulletin_areas()
            if areas:
                self._send(f"Bull areas:{', '.join(areas)}\r\n")
        adv = [ta for ta in self._config.station.tactical_addresses
               if ta.callsign and ta.advertised]
        if adv:
            self._send("\r\nAlert addresses (S <address> to notify sysop):\r\n")
            for ta in adv:
                self._send(f"  {ta.callsign:<10}{ta.alert_phrase}\r\n")
        self._show_prompt()

    # ── Chat command ───────────────────────────────────────────────────────

    def _cmd_chat(self, sysop_initiated: bool = False):
        """Enter interactive sysop chat mode.

        Emits CHAT_REQUEST so the GUI can pop up the chat window.  Then polls
        both the RF queue (remote lines) and a sysop_queue (GUI injections)
        until one of the following exits:

          - Remote types /EXIT        → return to BBS prompt, message remote
          - Sysop clicks End / types  → return to BBS prompt, message remote
            /EXIT in the GUI window
          - Sysop timeout             → 60 s since the last remote line with
                                        no sysop reply → auto-close, message remote
          - RF disconnected           → session ends normally

        sysop_initiated: True when the sysop started the chat from the GUI
                         rather than the remote typing CH.
        """
        sysop_queue: queue.Queue = queue.Queue()

        bbs_call = self._config.station.bbs_callsign
        if sysop_initiated:
            self._send(
                f"\r\n*** Sysop {bbs_call} would like to chat with you. ***\r\n"
                f"Type /EXIT at any time to return to the BBS.\r\n"
            )
        else:
            self._send(
                f"\r\nChat request sent to sysop. Please wait...\r\n"
                f"Type /EXIT at any time to return to the BBS.\r\n"
            )

        self._bus.emit(
            EventType.CHAT_REQUEST,
            source=self.instance_name,
            data={
                "remote_call": self.remote_call,
                "sysop_queue": sysop_queue,
            },
        )

        CHAT_POLL        = 1.0   # seconds between queue polls
        SYSOP_TIMEOUT    = 60.0  # seconds before auto-close if sysop silent
        last_unanswered  = None  # monotonic time of last unanswered remote line

        while self._running:
            # ── Check sysop queue (non-blocking) ──────────────────────────
            while True:
                try:
                    msg = sysop_queue.get_nowait()
                except queue.Empty:
                    break

                if msg is None:
                    # Sysop ended the chat from the GUI
                    self._send(
                        f"\r\n<{self._config.station.bbs_callsign}> "
                        f"Sysop has ended the chat. Returning to BBS.\r\n"
                    )
                    self._bus.emit(
                        EventType.CHAT_ENDED,
                        source=self.instance_name,
                        data={"remote_call": self.remote_call, "reason": "sysop_exit"},
                    )
                    self._show_prompt()
                    return
                else:
                    self._send(
                        f"\r\n<{self._config.station.bbs_callsign}> {msg}\r\n"
                    )
                    self._bus.emit(
                        EventType.CHAT_MESSAGE,
                        source=self.instance_name,
                        data={
                            "direction":   "TX",
                            "text":        msg,
                            "remote_call": self.remote_call,
                        },
                    )
                    last_unanswered = None  # sysop replied — reset timer

            # ── Check remote queue ─────────────────────────────────────────
            try:
                line = self._rx_queue.get(timeout=CHAT_POLL)
            except queue.Empty:
                line = None

            if line is _RF_DISCONNECTED:
                self._link_lost()
                self._bus.emit(
                    EventType.CHAT_ENDED,
                    source=self.instance_name,
                    data={"remote_call": self.remote_call, "reason": "rf_disconnect"},
                )
                return

            if line is _SYSOP_CHAT_END:
                self._send(
                    f"\r\n<{self._config.station.bbs_callsign}> "
                    f"Sysop has ended the chat. Returning to BBS.\r\n"
                )
                self._bus.emit(
                    EventType.CHAT_ENDED,
                    source=self.instance_name,
                    data={"remote_call": self.remote_call, "reason": "sysop_exit"},
                )
                self._show_prompt()
                return

            if line is not None:
                if line.strip().upper() == "/EXIT":
                    self._send("\r\nChat ended. Type H for help.\r\n")
                    self._bus.emit(
                        EventType.CHAT_MESSAGE,
                        source=self.instance_name,
                        data={
                            "direction":   "RX",
                            "text":        "/EXIT",
                            "remote_call": self.remote_call,
                        },
                    )
                    self._bus.emit(
                        EventType.CHAT_ENDED,
                        source=self.instance_name,
                        data={"remote_call": self.remote_call, "reason": "remote_exit"},
                    )
                    self._show_prompt()
                    return

                # Normal chat line from remote
                self._bus.emit(
                    EventType.CHAT_MESSAGE,
                    source=self.instance_name,
                    data={
                        "direction":   "RX",
                        "text":        line,
                        "remote_call": self.remote_call,
                    },
                )
                last_unanswered = time.monotonic()

            # ── Sysop timeout check ────────────────────────────────────────
            if (
                last_unanswered is not None
                and (time.monotonic() - last_unanswered) >= SYSOP_TIMEOUT
            ):
                self._send(
                    "\r\nSysop did not respond. Returning to BBS. Type H for help.\r\n"
                )
                self._bus.emit(
                    EventType.CHAT_ENDED,
                    source=self.instance_name,
                    data={"remote_call": self.remote_call, "reason": "timeout"},
                )
                self._show_prompt()
                return

    # ── Internal helpers ──────────────────────────────────────────────────

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

    def _show_prompt(self):
        if self._pending_notify.is_set():
            self._pending_notify.clear()
            waiting = self._store.get_messages_for(self.remote_call)
            unread  = [m for m in waiting if not m.get("is_read")]
            if unread:
                plural = "s" if len(unread) != 1 else ""
                self._send(
                    f"\r\n*** New message{plural} arrived - "
                    f"{len(unread)} waiting. Type L to list. ***"
                )
        self._send("\r\n> ")

    def _on_message_received(self, event):
        """EventBus callback — set notify flag if the new message is for us."""
        to_call = (event.data or {}).get("to", "").upper()
        if to_call == self.remote_call:
            self._pending_notify.set()

    def _link_lost(self):
        """The RF link dropped: end the session wherever it is."""
        self._link_down = True
        self._running = False

    def _get_line(self, timeout: float = 60):
        """Return the next line, or None on timeout or once the RF link is
        down (then self._link_down is set).  Any prompt reading through here
        ends as soon as the link drops instead of waiting out its timeout.
        Polls every 2 seconds so mid-session notifications can be pushed
        immediately rather than waiting for the next command.

        Keepalive design
        ----------------
        VARA FM supports a SESSIONTIMELIMIT command (sent during initialisation
        in interface.py) that sets a hard cap on session length in minutes.
        We send SESSIONTIMELIMIT 0 at startup to disable this cap so the BBS
        session_timeout setting is the sole arbiter.

        As a belt-and-suspenders measure, this loop also re-sends the prompt
        every VARA_KEEPALIVE_INTERVAL seconds.  This ensures some application
        data is flowing over the VARA data port and protects against any
        undocumented inactivity watchdog in VARA FM's ARQ layer.

        We also use time.monotonic() instead of an accumulated POLL counter so
        the deadline is tracked in real wall-clock seconds, avoiding drift from
        _show_prompt() I/O or GIL contention.
        """
        POLL = 2.0   # seconds between queue polls / notification checks
        # 60s matches VARA FM's own IAMALIVE cadence and gives ~20s margin
        # before VARA FM's confirmed 80s idle threshold (proven via vara_cmd.log:
        # DISCONNECTED fires exactly 80s after BUFFER 0; 90s was 6s too late).
        VARA_KEEPALIVE_INTERVAL = 60.0

        deadline       = (time.monotonic() + timeout) if timeout is not None else None
        last_keepalive = time.monotonic()

        if self._link_down:
            return None
        while deadline is None or time.monotonic() < deadline:
            try:
                val = self._rx_queue.get(timeout=POLL)
                if val is _RF_DISCONNECTED:
                    self._link_lost()
                    return None
                return val
            except queue.Empty:
                now = time.monotonic()

                # Push any pending mid-session notification without waiting
                # for the next command — remote may be sitting at the prompt.
                if self._pending_notify.is_set():
                    self._show_prompt()
                    last_keepalive = now  # prompt counts as keepalive traffic

                # Send a keepalive prompt to keep data flowing over the VARA
                # data port and prevent any ARQ-layer inactivity watchdog.
                elif (now - last_keepalive) >= VARA_KEEPALIVE_INTERVAL:
                    self._send("\r\n> ")
                    last_keepalive = now

        return None  # timeout expired

    # ── File commands ─────────────────────────────────────────────────────
    #
    # Protocol design: VARA's ARQ layer handles retransmission and error
    # correction at the link layer, so we stream entire files in one pass and
    # use a final CRC-32 for end-to-end integrity.  No per-chunk ACKs means
    # no half-duplex turnaround overhead per chunk.
    #
    # Download (GET):
    #   BBS → client:
    #     FILE <filename> <size_bytes> <crc32hex>\r\n
    #     <base64 data, line-wrapped at 60 chars>\r\n per line
    #     EOF <filename>\r\n
    #
    # Upload (PUT):
    #   client → BBS:
    #     PUTFILE <area> <filename> <size_bytes> <crc32hex> [description]\r\n
    #     <base64 data, one line per transmission>\r\n
    #     EOF\r\n
    #   BBS → client:
    #     OK <filename> or ERROR <reason>
    #
    # CRC-32 is computed over the raw (pre-base64) bytes and expressed as
    # an 8-character lowercase hex string.

    def _cmd_file_areas(self):
        if not self._files:
            self._send("File library not available.\r\n")
            self._show_prompt()
            return
        areas = self._files.list_areas()
        if not areas:
            self._send("No file areas defined.\r\n")
        else:
            self._send(f"\r\n{'AREA':<12} {'FILES':>6}  DESCRIPTION\r\n")
            self._send("-" * 50 + "\r\n")
            for a in areas:
                self._send(
                    f"{a['name']:<12} {a['file_count']:>6}  {a['description']}\r\n"
                )
        self._show_prompt()

    def _cmd_file_list(self, area=None):
        if not self._files:
            self._send("File library not available.\r\n")
            self._show_prompt()
            return
        if area and not self._files.area_exists(area):
            self._send(f"Unknown area: {area}. Type FA to list areas.\r\n")
            self._show_prompt()
            return
        files = self._files.list_files(area)
        if not files:
            label = area or "all areas"
            self._send(f"No files in {label}.\r\n")
        else:
            self._send(
                f"\r\n{'#':<4} {'AREA':<10} {'FILENAME':<24} {'SIZE':>8}  "
                f"{'DLs':>4}  DESCRIPTION\r\n"
            )
            self._send("-" * 74 + "\r\n")
            lines = []
            for i, f in enumerate(files):
                lines.append(
                    f"{i+1:<4} {f['area']:<10} {f['filename']:<24} "
                    f"{self._fmt_size(f['size_bytes']):>8}  {f['dl_count']:>4}  "
                    f"{f['description'][:24]}\r\n"
                )
            self._paged_send(lines)
        self._show_prompt()

    def _cmd_file_get(self, args: str):
        """FG <area> <filename> — stream entire file then CRC-32 for verification."""
        import base64, binascii
        if not self._files:
            self._send("File library not available.\r\n")
            self._show_prompt()
            return
        parts = args.split(None, 1)
        if len(parts) < 2:
            self._send("Usage: FG <area> <filename>\r\n")
            self._show_prompt()
            return
        area, filename = parts[0].upper(), parts[1].strip()

        meta = self._files.get_file_meta(area, filename)
        if not meta:
            self._send(f"File not found: {area}/{filename}\r\n")
            self._show_prompt()
            return

        data = self._files.read_file(area, filename)
        if data is None:
            self._send("File read error. Contact sysop.\r\n")
            self._show_prompt()
            return

        size    = len(data)
        crc     = f"{binascii.crc32(data) & 0xFFFFFFFF:08x}"
        encoded = base64.b64encode(data).decode("ascii")

        # Announce the transfer — client can abort before data starts
        self._send(
            f"\r\nFILE {filename} {size} {crc}\r\n"
            f"(Type CANCEL within 10s to abort)\r\n"
        )
        reply = self._get_line(timeout=self._config.bbs.session_timeout)
        if isinstance(reply, str) and reply.strip().upper() == "CANCEL":
            self._send("Transfer cancelled.\r\n")
            self._show_prompt()
            return

        # Stream base64 in 60-char lines — VARA handles flow at link layer
        LINE = 60
        for i in range(0, len(encoded), LINE):
            self._send(encoded[i:i + LINE] + "\r\n")

        self._send(f"EOF {filename}\r\n")
        log.info(f"File GET: {area}/{filename} ({size}B) → {self.remote_call}")
        self._show_prompt()

    def _cmd_file_put(self, args: str):
        """FP <area> <filename> [description]
        Client initiates with: PUTFILE <area> <file> <size> <crc32hex> [desc]
        then streams base64 lines, then EOF.
        """
        import base64, binascii, io
        if not self._files:
            self._send("File library not available.\r\n")
            self._show_prompt()
            return

        # args from the FP command line may carry area+filename+desc, but the
        # actual transfer metadata arrives in the PUTFILE handshake line.
        parts = args.split(None, 2)
        if len(parts) < 2:
            self._send(
                "Usage: FP <area> <filename> [description]\r\n"
                "Then send: PUTFILE <area> <filename> <size_bytes> <crc32hex> [desc]\r\n"
                "Followed by base64 data lines, then EOF\r\n"
            )
            self._show_prompt()
            return

        area_hint     = parts[0].upper()
        filename_hint = parts[1].strip()
        desc_hint     = parts[2].strip() if len(parts) > 2 else ""

        if not self._files.area_exists(area_hint):
            self._send(f"Unknown area: {area_hint}. Type FA to list areas.\r\n")
            self._show_prompt()
            return

        # ── Pre-flight permission check — reject BEFORE data transfer ────
        fu = self._files._config.file_upload
        if not fu.allow_remote_uploads:
            self._send("Remote uploads are disabled by sysop.\r\n")
            self._show_prompt()
            return

        allowed, perm_reason = self._files._check_upload_permission(
            self.remote_call
        )
        if not allowed:
            self._send(
                "Permission denied. You are not authorized to upload files.\r\n"
                "Contact the sysop to request upload access.\r\n"
            )
            self._files._emit_upload_denied(
                self.remote_call, filename_hint, area_hint, perm_reason
            )
            log.warning(
                f"Upload pre-flight denied: {self.remote_call} "
                f"file={filename_hint} area={area_hint}"
            )
            self._show_prompt()
            return

        if self._rate_limited("file_upload"):
            return

        mb = self._files._config.file_upload.max_file_bytes // (1024 * 1024)
        self._send(
            f"\r\nReady to receive {filename_hint} in {area_hint}.\r\n"
            f"Max size: {mb} MB.  Encoding: BASE64.\r\n"
            f"Send: PUTFILE {area_hint} {filename_hint} <size> <crc32hex> [desc]\r\n"
            f"Then base64 data lines, then EOF\r\n"
        )

        # Wait for PUTFILE header
        header = self._get_line(timeout=self._config.bbs.session_timeout)
        if header is None or not isinstance(header, str):
            self._send("Upload timed out.\r\n")
            self._show_prompt()
            return
        header = header.strip()
        if header.upper() == "CANCEL":
            self._send("Upload cancelled.\r\n")
            self._show_prompt()
            return
        if not header.upper().startswith("PUTFILE "):
            self._send(f"Expected PUTFILE header, got: {header[:40]}\r\n")
            self._show_prompt()
            return

        # Parse: PUTFILE <area> <filename> <size> <crc32hex> [description]
        hparts = header.split(None, 5)
        if len(hparts) < 5:
            self._send("Malformed PUTFILE header. Need area, filename, size, crc32.\r\n")
            self._show_prompt()
            return
        _, h_area, h_filename, h_size_str, h_crc = hparts[:5]
        h_desc = hparts[5] if len(hparts) > 5 else desc_hint
        if h_area.upper() != area_hint:
            # the area was checked above; the header must not name another
            self._send(f"PUTFILE area {h_area.upper()} does not match {area_hint}.\r\n")
            self._show_prompt()
            return
        try:
            h_size = int(h_size_str)
        except ValueError:
            self._send("Invalid size in PUTFILE header.\r\n")
            self._show_prompt()
            return
        max_bytes = self._files._config.file_upload.max_file_bytes
        mb = max_bytes // (1024 * 1024)
        if h_size <= 0 or h_size > max_bytes:
            self._send(f"File too large. Max {mb} MB.\r\n")
            self._show_prompt()
            return

        # Receive base64 lines until EOF
        self._send("GO\r\n")   # signal client to start streaming
        buf = io.BytesIO()
        b64_chars = 0
        # base64 of h_size bytes, plus room for line padding
        b64_limit = 4 * ((h_size + 2) // 3) + 1024

        while True:
            line = self._get_line(timeout=self._config.bbs.session_timeout)
            if line is None:
                self._send("\r\nUpload timed out. Aborting.\r\n")
                self._show_prompt()
                return
            if not isinstance(line, str):
                self._send("\r\nTransfer aborted (RF disconnect).\r\n")
                self._show_prompt()
                return
            line = line.strip()
            if line.upper() == "EOF":
                break
            if line.upper() == "CANCEL":
                self._send("Upload cancelled.\r\n")
                self._show_prompt()
                return
            b64_chars += len(line)
            if b64_chars > b64_limit:
                # more than the declared size: stop before it fills memory
                self._send(f"ERROR More data than the declared {h_size} bytes. Aborting.\r\n")
                self._show_prompt()
                return
            try:
                buf.write(base64.b64decode(line))
            except Exception:
                self._send(f"Bad base64 data. Aborting.\r\n")
                self._show_prompt()
                return

        data = buf.getvalue()

        # Verify size
        if len(data) != h_size:
            self._send(
                f"ERROR Size mismatch: expected {h_size}, got {len(data)}.\r\n"
            )
            self._show_prompt()
            return

        # Verify CRC-32
        actual_crc = f"{binascii.crc32(data) & 0xFFFFFFFF:08x}"
        if actual_crc != h_crc.lower():
            self._send(
                f"ERROR CRC mismatch: expected {h_crc}, got {actual_crc}.\r\n"
            )
            self._show_prompt()
            return

        # Save (is_remote=True applies all policy checks)
        ok, reason = self._files.commit_upload(
            h_area.upper(), h_filename, self.remote_call, h_desc, data,
            is_remote=True,
        )
        if ok:
            self._send(
                f"OK {h_filename} ({self._fmt_size(len(data))}) stored in {h_area.upper()}.\r\n"
            )
            log.info(
                f"File PUT: {h_area}/{h_filename} ({len(data)}B) "
                f"from {self.remote_call}"
            )
        else:
            self._send(f"ERROR {reason}\r\n")
            log.warning(
                f"File PUT rejected: {h_area}/{h_filename} "
                f"from {self.remote_call}: {reason}"
            )
        self._show_prompt()

    def _cmd_file_delete(self, args: str):
        """FD <area> <filename> — sysop or original uploader only"""
        if not self._files:
            self._send("File library not available.\r\n")
            self._show_prompt()
            return
        parts = args.split(None, 1)
        if len(parts) < 2:
            self._send("Usage: FD <area> <filename>\r\n")
            self._show_prompt()
            return
        area, filename = parts[0].upper(), parts[1].strip()

        meta = self._files.get_file_meta(area, filename)
        if not meta:
            self._send(f"File not found: {area}/{filename}\r\n")
            self._show_prompt()
            return

        if not self._is_sysop and meta["uploader"] != self.remote_call:
            self._send("Permission denied. You may only delete files you uploaded.\r\n")
            self._show_prompt()
            return

        if self._files.delete_file(area, filename):
            self._send(f"Deleted: {area}/{filename}\r\n")
        else:
            self._send("Delete failed. Contact sysop.\r\n")
        self._show_prompt()

    # ── File helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _fmt_size(n: int) -> str:
        if n < 1024:
            return f"{n}B"
        elif n < 1024 * 1024:
            return f"{n // 1024}K"
        else:
            return f"{n // (1024 * 1024)}M"

    def _paged_send(self, lines: list):
        """Send lines with paging (pauses every _pager lines)."""
        for i, line in enumerate(lines):
            self._send(line)
            if self._pager and (i + 1) % self._pager == 0:
                self._send("-- MORE -- (press ENTER or Q to quit)\r\n")
                resp = self._get_line(timeout=self._config.bbs.session_timeout)
                if resp is None or (isinstance(resp, str) and
                                    resp.strip().upper() == "Q"):
                    break
