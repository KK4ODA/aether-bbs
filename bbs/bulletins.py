"""
bbs/bulletins.py

Bulletin board support for the Aether BBS.

Bulletins are messages addressed to a group name (ALL, WX, DX, LOCAL, etc.)
rather than a specific callsign. Any station connecting to the BBS can read
bulletins regardless of their own callsign.

This module provides:
  - BulletinArea  — config object for a single area
  - BulletinStore — thin layer over MessageStore with bulletin-specific queries
  - BulletinForwarder — connects to neighbour BBSs and exchanges new bulletins
"""

import time
import queue
import logging
import threading
import socket
from typing import Optional

from bbs.store import MessageStore
from core.config import AppConfig, ForwardingNeighbourConfig
from core.events import EventBus, EventType

log = logging.getLogger(__name__)

# Well-known bulletin group names accepted without explicit config
DEFAULT_AREAS = {
    "ALL", "WX", "DX", "NTS", "LOCAL", "INFO", "SALE", "WANTED",
    "ARES", "RACES", "EMCOMM", "NETS",
}


def is_bulletin_address(to_call: str) -> bool:
    """
    Return True if to_call is a bulletin group name rather than a callsign.
    Heuristic: no digit after the first two characters, OR in the well-known set.
    """
    uc = to_call.upper().strip()
    if uc in DEFAULT_AREAS:
        return True
    # Bulletins typically have no SSID and no digit structure: ALL, WX, DX, etc.
    # A proper callsign always has at least one digit (W1ABC, VK3XYZ, etc.)
    import re
    return bool(re.match(r'^[A-Z]{2,8}$', uc)) and not re.search(r'\d', uc)


# ── BulletinStore — bulletin-specific query methods ───────────────────────────

class BulletinStore:
    """
    Thin wrapper around MessageStore that adds bulletin-specific queries.
    Bulletins are stored in the same messages table; they are identified by
    is_bulletin=1 (new column added via migration) and to_call being a group name.
    """

    def __init__(self, store: MessageStore):
        self._store = store
        self._apply_migration()

    def _apply_migration(self):
        """Add bulletin columns/tables if not already present."""
        conn = self._store._conn
        lock = self._store._lock
        with lock:
            # Check if is_bulletin column exists
            cur = conn.execute("PRAGMA table_info(messages)")
            cols = {row[1] for row in cur.fetchall()}
            stmts = []
            if "is_bulletin" not in cols:
                stmts.append(
                    "ALTER TABLE messages ADD COLUMN is_bulletin INTEGER NOT NULL DEFAULT 0"
                )
                stmts.append(
                    "CREATE INDEX IF NOT EXISTS idx_messages_bulletin "
                    "ON messages(is_bulletin, area)"
                )
            if "is_urgent" not in cols:
                stmts.append(
                    "ALTER TABLE messages ADD COLUMN is_urgent INTEGER NOT NULL DEFAULT 0"
                )
            stmts += [
                """CREATE TABLE IF NOT EXISTS bulletin_seen (
                    mid         TEXT NOT NULL,
                    bbs_call    TEXT NOT NULL,
                    seen_at     TEXT NOT NULL DEFAULT (datetime('now')),
                    PRIMARY KEY (mid, bbs_call)
                )""",
                """CREATE TABLE IF NOT EXISTS forwarding_log (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    neighbour   TEXT    NOT NULL,
                    direction   TEXT    NOT NULL,
                    mid         TEXT    NOT NULL,
                    timestamp   TEXT    NOT NULL DEFAULT (datetime('now')),
                    success     INTEGER NOT NULL DEFAULT 1
                )""",
            ]
            for stmt in stmts:
                try:
                    conn.execute(stmt)
                except Exception as e:
                    if "duplicate column" not in str(e).lower():
                        log.warning(f"Migration warning: {e}")
            conn.commit()
        log.info("BulletinStore migration applied")

    # ── Save a bulletin ───────────────────────────────────────────────────

    def save_bulletin(
        self,
        from_call: str,
        to_group: str,
        subject: str,
        body: bytes,
        area: str = "",
        instance: str = "",
        mid: Optional[str] = None,
        expires_days: Optional[int] = None,
        is_urgent: bool = False,
    ) -> str:
        """Save a bulletin. area defaults to to_group if not specified.

        When is_urgent is True, the base message priority is also set to 9
        so that generic queries sorted by priority will naturally elevate
        urgent bulletins.  priority (0-9) is the generic sort field on the
        messages table; is_urgent is the boolean trigger for alert behaviour.
        """
        effective_area = (area or to_group).upper()
        mid = self._store.save_message(
            from_call=from_call,
            to_call=to_group.upper(),
            subject=subject,
            body=body,
            area=effective_area,
            instance=instance,
            mid=mid,
            expires_days=expires_days,
            priority=9 if is_urgent else 0,
        )
        # Mark as bulletin (and optionally urgent)
        with self._store._lock:
            self._store._conn.execute(
                "UPDATE messages SET is_bulletin=1, is_urgent=? WHERE id=?",
                (1 if is_urgent else 0, mid),
            )
            self._store._conn.commit()
        urgency = " [URGENT]" if is_urgent else ""
        log.info(f"Bulletin saved{urgency}: MID={mid} group={to_group} "
                 f"area={effective_area}")
        return mid

    # ── List bulletins ────────────────────────────────────────────────────

    def list_bulletins(
        self,
        area: Optional[str] = None,
        limit: int = 100,
    ):
        with self._store._lock:
            if area:
                cur = self._store._conn.execute(
                    """SELECT id,from_call,to_call,subject,date,area,is_read,is_urgent
                       FROM messages
                       WHERE is_bulletin=1 AND area=?
                       ORDER BY date DESC LIMIT ?""",
                    (area.upper(), limit),
                )
            else:
                cur = self._store._conn.execute(
                    """SELECT id,from_call,to_call,subject,date,area,is_read,is_urgent
                       FROM messages
                       WHERE is_bulletin=1
                       ORDER BY date DESC LIMIT ?""",
                    (limit,),
                )
            return [dict(r) for r in cur.fetchall()]

    def list_bulletin_areas(self) -> list[str]:
        with self._store._lock:
            cur = self._store._conn.execute(
                "SELECT DISTINCT area FROM messages WHERE is_bulletin=1 ORDER BY area"
            )
            return [row[0] for row in cur.fetchall()]

    def get_new_bulletins(self, since: Optional[str] = None, limit: int = 100):
        """Return bulletins posted since `since` (ISO datetime string)."""
        with self._store._lock:
            if since:
                cur = self._store._conn.execute(
                    """SELECT * FROM messages
                       WHERE is_bulletin=1 AND created_at > ?
                       ORDER BY date ASC LIMIT ?""",
                    (since, limit),
                )
            else:
                cur = self._store._conn.execute(
                    """SELECT * FROM messages
                       WHERE is_bulletin=1 AND is_read=0
                       ORDER BY date ASC LIMIT ?""",
                    (limit,),
                )
            return [dict(r) for r in cur.fetchall()]

    def get_urgent_bulletins(self, since: Optional[str] = None,
                              limit: int = 50):
        """Return urgent bulletins (is_urgent=1), optionally since a timestamp."""
        with self._store._lock:
            if since:
                cur = self._store._conn.execute(
                    """SELECT * FROM messages
                       WHERE is_bulletin=1 AND is_urgent=1 AND created_at > ?
                       ORDER BY date DESC LIMIT ?""",
                    (since, limit),
                )
            else:
                cur = self._store._conn.execute(
                    """SELECT * FROM messages
                       WHERE is_bulletin=1 AND is_urgent=1 AND is_read=0
                       ORDER BY date DESC LIMIT ?""",
                    (limit,),
                )
            return [dict(r) for r in cur.fetchall()]

    def bulletin_count(self, area: Optional[str] = None) -> int:
        with self._store._lock:
            if area:
                cur = self._store._conn.execute(
                    "SELECT COUNT(*) FROM messages WHERE is_bulletin=1 AND area=?",
                    (area.upper(),),
                )
            else:
                cur = self._store._conn.execute(
                    "SELECT COUNT(*) FROM messages WHERE is_bulletin=1"
                )
            return cur.fetchone()[0]

    # ── Duplicate detection for forwarding ────────────────────────────────

    def mark_seen(self, mid: str, bbs_call: str):
        """Record that we have seen/forwarded this MID to/from bbs_call."""
        with self._store._lock:
            self._store._conn.execute(
                "INSERT OR IGNORE INTO bulletin_seen (mid, bbs_call) VALUES (?,?)",
                (mid, bbs_call.upper()),
            )
            self._store._conn.commit()

    def already_seen(self, mid: str, bbs_call: str) -> bool:
        with self._store._lock:
            cur = self._store._conn.execute(
                "SELECT 1 FROM bulletin_seen WHERE mid=? AND bbs_call=?",
                (mid, bbs_call.upper()),
            )
            return cur.fetchone() is not None

    def get_unsent_bulletins_for(
        self,
        neighbour_call: str,
        areas: Optional[list[str]] = None,
        limit: int = 20,
    ):
        """
        Return bulletins not yet sent to neighbour_call.
        Optionally filter to specific areas.
        """
        with self._store._lock:
            if areas:
                placeholders = ",".join("?" * len(areas))
                cur = self._store._conn.execute(
                    f"""SELECT m.* FROM messages m
                        WHERE m.is_bulletin=1
                        AND m.area IN ({placeholders})
                        AND NOT EXISTS (
                            SELECT 1 FROM bulletin_seen s
                            WHERE s.mid=m.id AND s.bbs_call=?
                        )
                        ORDER BY m.date ASC LIMIT ?""",
                    [a.upper() for a in areas] + [neighbour_call.upper(), limit],
                )
            else:
                cur = self._store._conn.execute(
                    """SELECT m.* FROM messages m
                       WHERE m.is_bulletin=1
                       AND NOT EXISTS (
                           SELECT 1 FROM bulletin_seen s
                           WHERE s.mid=m.id AND s.bbs_call=?
                       )
                       ORDER BY m.date ASC LIMIT ?""",
                    (neighbour_call.upper(), limit),
                )
            return [dict(r) for r in cur.fetchall()]

    # ── Forwarding log ────────────────────────────────────────────────────

    def log_forward(
        self,
        neighbour: str,
        direction: str,
        mid: str,
        success: bool = True,
    ):
        with self._store._lock:
            self._store._conn.execute(
                """INSERT INTO forwarding_log (neighbour,direction,mid,success)
                   VALUES (?,?,?,?)""",
                (neighbour.upper(), direction, mid, 1 if success else 0),
            )
            self._store._conn.commit()

    def get_forwarding_log(self, limit: int = 100):
        with self._store._lock:
            cur = self._store._conn.execute(
                """SELECT * FROM forwarding_log
                   ORDER BY timestamp DESC LIMIT ?""",
                (limit,),
            )
            return cur.fetchall()


# ── RF link for forwarding calls ───────────────────────────────────────────────

RF_LINE_TIMEOUT    = 180.0   # an HF round trip at a slow rung can take minutes
RF_CONNECT_TIMEOUT = 180.0   # calling, including the modem's retries
_HANDSHAKE_SKIP    = 30      # lines (a banner) tolerated before the HELLO


class _RFLink:
    """Session handler for one outbound forwarding call.

    Plugs into a TNCBroker's begin_outbound(): receives CONNECTED /
    DISCONNECTED and the data stream, and offers line-based send/recv.
    """

    def __init__(self, broker):
        self._broker = broker
        self._lines: queue.Queue = queue.Queue()
        self._buf = b""
        self.connected = threading.Event()
        self.disconnected = threading.Event()

    # SessionHandler protocol
    def on_cmd_event(self, event: str):
        up = event.upper()
        if up.startswith("CONNECTED"):
            self.connected.set()
        elif up.startswith("DISCONNECTED"):
            self.disconnected.set()
            self._lines.put(None)

    def on_data(self, data: bytes):
        self._buf += data
        while True:
            cut = min((i for i in (self._buf.find(b"\r"), self._buf.find(b"\n"))
                       if i >= 0), default=-1)
            if cut < 0:
                break
            line, self._buf = self._buf[:cut], self._buf[cut + 1:]
            text = line.decode("utf-8", errors="replace").strip()
            if text:            # "\r\n" leaves an empty line: skipped
                self._lines.put(text)

    def send(self, line: str):
        self._broker.send_data((line + "\r").encode("utf-8", errors="replace"))

    def recv(self, timeout: float = RF_LINE_TIMEOUT) -> Optional[str]:
        if self.disconnected.is_set() and self._lines.empty():
            return None
        try:
            return self._lines.get(timeout=timeout)
        except queue.Empty:
            return None


# ── BulletinForwarder — connects to neighbours and exchanges bulletins ─────────

class BulletinForwarder:
    """
    Periodically connects to configured neighbour BBSs via TCP (direct Telnet/BBS
    protocol) and exchanges new bulletins bidirectionally.

    Protocol used: simple line-based BBS forwarding similar to FBB SP/SB protocol.
    Each side offers MIDs it has, the other side requests what it doesn't have.

    A neighbour is reached over TCP (``via = "TCP"``: host/port) or on the
    air (``via = "RF"``: a call through a VARA FM, VARA HF or Aether HF
    instance).  The protocol is the same either way.
    """

    # Simple forwarding protocol tokens
    _FWD_HELLO  = "FWD HELLO"
    _FWD_OFFER  = "FWD OFFER"     # FWD OFFER <mid> <area> <size>
    _FWD_GET    = "FWD GET"       # FWD GET <mid>
    _FWD_DATA   = "FWD DATA"      # FWD DATA <mid> <from> <to> <subject>
    _FWD_BODY   = "FWD BODY"      # followed by body lines, terminated by "FWD END"
    _FWD_END    = "FWD END"
    _FWD_ACK    = "FWD ACK"
    _FWD_NAK    = "FWD NAK"
    _FWD_BYE    = "FWD BYE"

    def __init__(
        self,
        config: AppConfig,
        store: MessageStore,
        bbs: "BulletinStore",
        event_bus: EventBus,
    ):
        self._config_src = config     # AppConfig, or ConfigManager (live)
        self._store   = store
        self._bbs     = bbs
        self._bus     = event_bus
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._im = None   # instance manager, for RF neighbours
        # when each neighbour was last called, kept here: a Save replaces
        # the neighbour objects, and must not make every neighbour due
        self._last_called: dict[tuple, float] = {}

    @property
    def _config(self) -> AppConfig:
        return _live(self._config_src)

    def set_instance_manager(self, im):
        """Give the forwarder the modem instances RF neighbours are called on."""
        self._im = im

    def start(self):
        # runs with or without neighbours: it reads the list on every pass,
        # so neighbours added in Settings are called without a restart
        if self._running:
            return
        self._running = True
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="BulletinForwarder",
            daemon=True,
        )
        self._thread.start()
        log.info("BulletinForwarder started")

    def stop(self):
        self._stop_event.set()
        self._running = False
        if self._thread:
            self._thread.join(timeout=10)

    # ── Main forwarding loop ──────────────────────────────────────────────

    def _run_loop(self):
        """
        Main loop: iterate over neighbours on their schedules,
        connect, exchange, disconnect, sleep.
        """
        while not self._stop_event.is_set():
            neighbours = getattr(self._config.bbs, "forwarding_neighbours", [])
            for neighbour in neighbours:
                if self._stop_event.is_set():
                    break
                if not neighbour.enabled:
                    continue
                if not self._is_due(neighbour):
                    continue

                where = (f"on the air via {neighbour.instance}"
                         if neighbour.via == "RF"
                         else f"at {neighbour.host}:{neighbour.port}")
                log.info(f"Forwarding: connecting to {neighbour.bbs_callsign} "
                         f"{where}")
                try:
                    self._forward_with(neighbour)
                except Exception as e:
                    log.error(
                        f"Forwarding error with {neighbour.bbs_callsign}: {e}",
                        exc_info=True,
                    )

            # Poll every 60 seconds to check schedules
            self._stop_event.wait(timeout=60)

    def _is_due(self, neighbour: "ForwardingNeighbourConfig") -> bool:
        """Check if it's time to forward to this neighbour."""
        interval_secs = neighbour.interval_minutes * 60
        last_fwd = self._last_called.get(_neighbour_key(neighbour), 0)
        return time.time() - last_fwd >= interval_secs

    def _mark_called(self, neighbour) -> None:
        now = time.time()
        self._last_called[_neighbour_key(neighbour)] = now
        try:
            neighbour._last_forwarded_at = now
        except (AttributeError, ValueError):
            pass

    # ── Per-neighbour session ─────────────────────────────────────────────

    def _forward_with(self, neighbour: "ForwardingNeighbourConfig"):
        if neighbour.via == "RF":
            self._forward_over_rf(neighbour)
        else:
            self._forward_over_tcp(neighbour)

    def _forward_over_tcp(self, neighbour: "ForwardingNeighbourConfig"):
        """Open a TCP connection to the neighbour and exchange bulletins."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(30)

        try:
            sock.connect((neighbour.host, neighbour.port))

            def send(line: str):
                sock.sendall((line + "\r\n").encode("ascii", errors="replace"))
                log.debug(f"FWD→{neighbour.bbs_callsign}: {line}")

            def recv() -> Optional[str]:
                try:
                    raw = b""
                    while not raw.endswith(b"\n"):
                        chunk = sock.recv(1)
                        if not chunk:
                            return None
                        raw += chunk
                    return raw.decode("ascii", errors="replace").strip()
                except socket.timeout:
                    return None

            self._exchange(send, recv, neighbour)

        except (socket.error, OSError) as e:
            log.warning(f"Forwarding TCP error with {neighbour.bbs_callsign}: {e}")
        finally:
            try:
                sock.close()
            except Exception:
                pass

    def _forward_over_rf(self, neighbour: "ForwardingNeighbourConfig"):
        """Call the neighbour on the air and exchange bulletins.

        The call is placed through the named modem instance (VARA FM, VARA
        HF or Aether HF).  An attempt counts against the interval whatever
        its outcome, so an unreachable neighbour is not called every minute.
        """
        self._mark_called(neighbour)
        call = neighbour.bbs_callsign
        mux = self._im.get_mux(neighbour.instance) if self._im else None
        if mux is None or not hasattr(mux, "begin_outbound"):
            log.error(
                f"Forwarding to {call}: instance '{neighbour.instance}' is not "
                f"running or cannot place calls (RF forwarding needs a VARA FM, "
                f"VARA HF or Aether HF instance)"
            )
            return
        if getattr(mux.state, "name", "") == "CONNECTED":
            log.info(f"Forwarding to {call}: {neighbour.instance} is busy "
                     f"with a session; will try at the next interval")
            return

        link = _RFLink(mux)
        if not mux.begin_outbound(link, timeout=15.0):
            log.info(f"Forwarding to {call}: {neighbour.instance} is busy; "
                     f"will try at the next interval")
            return
        try:
            if not mux.connect_station(call, busy_timeout=60.0):
                log.warning(f"Forwarding to {call}: channel stayed busy")
                return
            # Aether HF (and VARA) say DISCONNECTED for a call that ends
            # without a session: refused by the modem, or unanswered.
            deadline = time.time() + RF_CONNECT_TIMEOUT
            while (time.time() < deadline and not link.connected.is_set()
                   and not link.disconnected.is_set()):
                link.connected.wait(0.5)
            if not link.connected.is_set():
                if link.disconnected.is_set():
                    log.warning(f"Forwarding to {call}: the call ended without "
                                f"a session (refused or unanswered — see the "
                                f"modem's log)")
                else:
                    log.warning(f"Forwarding to {call}: no answer")
                    mux.send_cmd("ABORT")
                return
            log.info(f"Forwarding: on the air with {call}")
            self._exchange(link.send, link.recv, neighbour)
        finally:
            if link.connected.is_set() and not link.disconnected.is_set():
                time.sleep(2.0)   # let FWD BYE go out first
                mux.send_cmd("DISCONNECT")
                link.disconnected.wait(60.0)
            mux.end_outbound(link)

    def _exchange(self, send, recv, neighbour: "ForwardingNeighbourConfig") -> bool:
        """Run the client side of the forwarding protocol.  True when the
        session reached its end."""
        # Handshake.  A neighbour that greets with a banner first (an older
        # BBS answering on RF) is tolerated: skip lines until its HELLO.
        send(f"{self._FWD_HELLO} {self._config.station.bbs_callsign}")
        reply = None
        for _ in range(_HANDSHAKE_SKIP):
            reply = recv()
            if reply is None or reply.startswith(self._FWD_HELLO):
                break
        if not reply or not reply.startswith(self._FWD_HELLO):
            log.warning(
                f"Unexpected handshake reply from {neighbour.bbs_callsign}: {reply!r}"
            )
            return False

        remote_call = reply.split()[2] if len(reply.split()) >= 3 else neighbour.bbs_callsign
        log.info(f"Forwarding session open with {remote_call}")

        # Phase 1: offer our unsent bulletins to them
        areas = neighbour.areas or None
        outbound = self._bbs.get_unsent_bulletins_for(
            neighbour.bbs_callsign,
            areas=areas,
            limit=20,
        )
        # Offer urgent bulletins first so they arrive with priority
        outbound.sort(key=lambda r: (not r.get("is_urgent", 0), r.get("date", "")))
        for row in outbound:
            body = row["body"]
            if isinstance(body, bytes):
                size = len(body)
            else:
                size = len(body.encode("utf-8"))
            send(
                f"{self._FWD_OFFER} {row['id']} {row['area']} {size}"
            )
            ack = recv()
            if ack and ack.startswith(self._FWD_GET):
                # They want it — send it; it counts as delivered only once
                # they acknowledge it, otherwise it is offered again next time
                if self._send_bulletin(send, recv, row):
                    self._bbs.mark_seen(row["id"], neighbour.bbs_callsign)
                    self._bbs.log_forward(neighbour.bbs_callsign, "sent", row["id"])
                    log.info(f"Forwarded MID={row['id']} → {neighbour.bbs_callsign}")
                else:
                    log.warning(f"{neighbour.bbs_callsign} did not acknowledge "
                                f"MID={row['id']}; will offer it again")
            elif ack is None:
                log.warning(f"Forwarding to {neighbour.bbs_callsign}: no reply "
                            f"to offer of {row['id']}")
                return False
            else:
                # They already have it
                self._bbs.mark_seen(row["id"], neighbour.bbs_callsign)

        # Phase 2: ask them what they have for us
        # The answering side sends its whole list, then FWD END, before it
        # reads anything more, so collect the list before asking for any.
        send(f"{self._FWD_GET} *")  # request their offer list
        offers: list[tuple[str, str]] = []
        while True:
            line = recv()
            if line is None or line.startswith(self._FWD_BYE):
                break
            if line.startswith(self._FWD_OFFER):
                # "FWD OFFER <mid> <area> <size>": arguments follow the
                # two-word command
                parts = line.split()[2:]
                if len(parts) >= 2:
                    offers.append((parts[0], parts[1]))
            elif line.startswith(self._FWD_END):
                break

        for their_mid, their_area in offers:
            if self._should_accept(their_mid, their_area, neighbour):
                send(f"{self._FWD_GET} {their_mid}")
                self._receive_bulletin(send, recv, their_mid, neighbour)

        send(self._FWD_BYE)
        self._mark_called(neighbour)

        self._bus.emit(
            EventType.MESSAGE_RECEIVED,
            data={"source": "forwarding", "neighbour": neighbour.bbs_callsign},
        )
        return True

    def _send_bulletin(self, send, recv, row):
        """Send a single bulletin to the neighbour.

        Wire format (v2 with flags):
          FWD DATA <mid> <from> <to> <flags> <subject>
        Where flags is "-" for none or "U" for urgent.
        Old receivers split(" ", 5) and treat everything after the 4th
        positional token as subject, so "<flags> <subject>" becomes a
        slightly prefixed subject — harmless degradation.
        """
        body = row["body"]
        if isinstance(body, bytes):
            body_text = body.decode("utf-8", errors="replace")
        else:
            body_text = body

        is_urgent = bool(row.get("is_urgent", 0))
        flags = "U" if is_urgent else "-"

        send(
            f"{self._FWD_DATA} {row['id']} "
            f"{row['from_call']} {row['to_call']} {flags} {row['subject']}"
        )
        send(self._FWD_BODY)
        for line in body_text.splitlines():
            send(line)
        send(self._FWD_END)

        ack = recv()
        return bool(ack and ack.startswith(self._FWD_ACK))

    def _receive_bulletin(self, send, recv, mid: str, neighbour):
        """Receive a bulletin from the neighbour and store it.

        Supports two wire formats:
          Legacy:  FWD DATA <mid> <from> <to> <subject>          (6 parts at split 5)
          v2:      FWD DATA <mid> <from> <to> <flags> <subject>  (7 parts at split 6)

        The flags field is a single token: "-" for none, "U" for urgent.
        If only 6 parts are present (legacy sender), flags defaults to "-".
        """
        header_line = recv()
        if not header_line or not header_line.startswith(self._FWD_DATA):
            send(f"{self._FWD_NAK} {mid}")
            return

        # Try v2 format first (split into 7 parts)
        # Wire: "FWD DATA <mid> <from> <to> <flags> <subject>"
        #  idx:   0    1     2      3     4     5       6
        parts7 = header_line.split(" ", 6)
        if len(parts7) >= 7 and parts7[5] in ("-", "U"):
            # v2 format with flags
            from_call = parts7[3]
            to_call   = parts7[4]
            flags     = parts7[5]
            subject   = parts7[6]
        else:
            # Legacy format: "FWD DATA <mid> <from> <to> <subject>"
            #  idx:            0    1     2      3     4     5
            parts = header_line.split(" ", 5)
            if len(parts) < 6:
                send(f"{self._FWD_NAK} {mid}")
                return
            from_call = parts[3]
            to_call   = parts[4]
            subject   = parts[5]
            flags     = "-"

        is_urgent = ("U" in flags)

        # Read body
        body_marker = recv()
        if not body_marker or not body_marker.startswith(self._FWD_BODY):
            send(f"{self._FWD_NAK} {mid}")
            return

        lines = []
        while True:
            line = recv()
            if line is None:
                # the link dropped mid-body: storing the partial copy would
                # block the full one forever (duplicate MID), so drop it
                log.warning(f"Bulletin MID={mid} from {neighbour.bbs_callsign} "
                            f"cut off before FWD END; not stored")
                return
            if line.startswith(self._FWD_END):
                break
            lines.append(line)

        body_text = "\n".join(lines)

        # Determine area from the to_call group or neighbour area list
        area = to_call.upper() if is_bulletin_address(to_call) else "ALL"

        # Save (duplicate detection via INSERT OR IGNORE on mid)
        if not self._store.has_message(mid):
            self._bbs.save_bulletin(
                from_call=from_call,
                to_group=to_call,
                subject=subject,
                body=body_text.encode("utf-8"),
                area=area,
                mid=mid,
                is_urgent=is_urgent,
            )
            self._bbs.mark_seen(mid, neighbour.bbs_callsign)
            self._bbs.mark_seen(mid, self._config.station.bbs_callsign)
            self._bbs.log_forward(neighbour.bbs_callsign, "received", mid)
            urgency = " [URGENT]" if is_urgent else ""
            log.info(f"Received bulletin{urgency} MID={mid} from {neighbour.bbs_callsign}")
            self._bus.emit(EventType.BULLETIN_POSTED, data={
                "mid":      mid,
                "from":     from_call,
                "to_group": to_call,
                "subject":  subject,
                "area":     area,
                "is_urgent": is_urgent,
            })
            send(f"{self._FWD_ACK} {mid}")
        else:
            log.debug(f"Skipped duplicate MID={mid} from {neighbour.bbs_callsign}")
            send(f"{self._FWD_NAK} {mid}")

    def _should_accept(self, mid: str, area: str, neighbour) -> bool:
        """Decide whether to accept an incoming bulletin."""
        # Reject duplicates
        if self._store.has_message(mid):
            return False
        if self._bbs.already_seen(mid, neighbour.bbs_callsign):
            return False
        # Area filter: if neighbour has areas configured, only accept those
        if neighbour.areas and area.upper() not in [a.upper() for a in neighbour.areas]:
            return False
        return True


# ── Server side: receive inbound forwarding connections ───────────────────────

def _live(src) -> AppConfig:
    """*src*'s settings as saved now: a ConfigManager gives the latest
    (Settings → Save replaces the config object), an AppConfig itself."""
    current = getattr(src, "current", None)
    return (current() if callable(current) else None) or src


def _neighbour_key(n) -> tuple:
    via = getattr(n, "via", "TCP")
    where = (getattr(n, "instance", "") if via == "RF"
             else f"{getattr(n, 'host', '')}:{getattr(n, 'port', '')}")
    return (n.bbs_callsign.strip().upper(), via, where)


class BulletinForwardingServer:
    """
    Listens on a TCP port for inbound forwarding connections from neighbour BBSs.
    Runs the same simple protocol as BulletinForwarder but as the accepting side.
    """

    def __init__(
        self,
        config: AppConfig,
        store: MessageStore,
        bbs: BulletinStore,
        event_bus: EventBus,
    ):
        self._config_src = config     # AppConfig, or ConfigManager (live)
        self._store   = store
        self._bbs     = bbs
        self._bus     = event_bus
        self._server: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._serving = None          # the forwarding settings listened with

    @property
    def _config(self) -> AppConfig:
        return _live(self._config_src)

    def start(self):
        fwd_cfg = getattr(self._config.bbs, "forwarding", None)
        self._serving = fwd_cfg
        if not fwd_cfg or not getattr(fwd_cfg, "server_enabled", False):
            log.info("BulletinForwardingServer: server disabled in config")
            return
        port = getattr(fwd_cfg, "server_port", 7300)
        bind = getattr(fwd_cfg, "server_bind", "0.0.0.0")
        try:
            server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server.bind((bind, port))
            server.listen(5)
            server.settimeout(1.0)
            self._server = server
            # each listener has its own socket and stop event, so a
            # restarted server cannot leave its old accept loop running
            self._stop_event = threading.Event()
            self._thread = threading.Thread(
                target=self._accept_loop,
                args=(server, self._stop_event),
                name="FwdServer",
                daemon=True,
            )
            self._thread.start()
            log.info(f"BulletinForwardingServer listening on {bind}:{port}")
        except OSError as e:
            log.error(f"Could not start forwarding server: {e}")

    def stop(self):
        self._stop_event.set()
        server, self._server = self._server, None
        if server:
            try:
                server.close()
            except Exception:
                pass
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=2)
        self._thread = None

    def apply_config(self):
        """After a Save: listen again if the server settings changed."""
        if self._config.bbs.forwarding == self._serving:
            return
        log.info("Forwarding server settings changed; restarting it")
        self.stop()
        self.start()

    def _accept_loop(self, server: socket.socket, stop: threading.Event):
        while not stop.is_set():
            try:
                conn, addr = server.accept()
                log.info(f"Inbound forwarding connection from {addr}")
                threading.Thread(
                    target=self._handle_inbound,
                    args=(conn,),
                    daemon=True,
                ).start()
            except socket.timeout:
                continue
            except OSError:
                break

    def _handle_inbound(self, conn: socket.socket):
        """Handle one inbound forwarding session (server side).

        Protocol state machine (server perspective):
          1. Receive HELLO, reply HELLO
          2. Loop:
             - FWD OFFER <mid> <area> <size>  → decide GET or NAK, then receive DATA
             - FWD GET *                      → send our offer list (OFFERs + END)
             - FWD GET <mid>                  → look up and send that bulletin via DATA
             - FWD BYE                        → end session
        """
        conn.settimeout(30)

        def send(line: str):
            conn.sendall((line + "\r\n").encode("ascii", errors="replace"))
            log.debug(f"FWD-SRV→: {line}")

        def recv() -> Optional[str]:
            try:
                raw = b""
                while not raw.endswith(b"\n"):
                    chunk = conn.recv(1)
                    if not chunk:
                        return None
                    raw += chunk
                decoded = raw.decode("ascii", errors="replace").strip()
                log.debug(f"FWD-SRV←: {decoded}")
                return decoded
            except socket.timeout:
                return None

        try:
            self.serve(send, recv)
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def serve(self, send, recv, expected_call: str = "") -> bool:
        """Run the answering side of the forwarding protocol over any line
        transport (a TCP connection, or an RF session from a neighbour).

        ``expected_call``: on RF, the neighbour the caller was identified
        as; a HELLO naming another BBS is refused.  Only enabled neighbours
        in Settings → Forwarding are served.  Returns True when the session
        ran to its end.
        """
        fwd = BulletinForwarder(self._config, self._store, self._bbs, self._bus)
        try:
            # Receive their hello, reply with ours
            hello = recv()
            if not hello or not hello.startswith(BulletinForwarder._FWD_HELLO):
                return False
            parts = hello.split()
            remote_call = parts[2] if len(parts) >= 3 else "UNKNOWN"
            if expected_call and remote_call.upper().split("-")[0] != \
                    expected_call.upper().split("-")[0]:
                log.warning(f"Forwarding: HELLO from {remote_call} on a call "
                            f"from {expected_call}; refused")
                return False
            if not any(n.enabled and n.matches(remote_call) for n in
                       getattr(self._config.bbs, "forwarding_neighbours", [])):
                log.warning(f"Forwarding: HELLO from {remote_call}, which is "
                            f"not a configured neighbour; refused")
                return False

            # Build a transient neighbour handle for this session
            class _SessionNeighbour:
                bbs_callsign = remote_call
                areas = []
                _last_forwarded_at = 0

            neighbour = _SessionNeighbour()
            send(
                f"{BulletinForwarder._FWD_HELLO} "
                f"{self._config.station.bbs_callsign}"
            )
            log.info(f"Inbound forwarding session with {remote_call}")

            while True:
                line = recv()
                if line is None or line.startswith(BulletinForwarder._FWD_BYE):
                    break

                if line.startswith(BulletinForwarder._FWD_OFFER):
                    # ── They are offering a bulletin to us ────────────────
                    oparts = line.split()[2:]   # after "FWD OFFER"
                    if len(oparts) >= 2:
                        their_mid, their_area = oparts[0], oparts[1]
                        if fwd._should_accept(their_mid, their_area, neighbour):
                            send(f"{BulletinForwarder._FWD_GET} {their_mid}")
                            fwd._receive_bulletin(send, recv, their_mid, neighbour)
                        else:
                            send(f"{BulletinForwarder._FWD_NAK} {their_mid}")

                elif line.startswith(BulletinForwarder._FWD_GET):
                    # ── They are requesting bulletin(s) from us ───────────
                    gparts = line.split()[2:]   # after "FWD GET"
                    requested_mid = gparts[0] if gparts else "*"

                    if requested_mid == "*":
                        # Send our offer list — all unsent bulletins for them
                        outbound = self._bbs.get_unsent_bulletins_for(
                            remote_call, limit=20
                        )
                        outbound.sort(key=lambda r: (
                            not r.get("is_urgent", 0), r.get("date", "")
                        ))
                        for row in outbound:
                            body = row["body"]
                            size = len(body) if isinstance(body, bytes) \
                                else len(body.encode("utf-8"))
                            send(
                                f"{BulletinForwarder._FWD_OFFER} "
                                f"{row['id']} {row.get('area', 'ALL')} {size}"
                            )
                        send(BulletinForwarder._FWD_END)
                    else:
                        # Send a specific bulletin by MID
                        msg = self._store.get_message(requested_mid)
                        # Bulletins only: a private message is never
                        # forwarded, whatever MID a peer asks for
                        if msg and msg.get("is_bulletin"):
                            row = dict(msg)
                            if fwd._send_bulletin(send, recv, row):
                                self._bbs.mark_seen(requested_mid, remote_call)
                                self._bbs.log_forward(remote_call, "sent", requested_mid)
                                log.info(
                                    f"Forwarded MID={requested_mid} → "
                                    f"{remote_call} (inbound session)"
                                )
                        else:
                            send(
                                f"{BulletinForwarder._FWD_NAK} "
                                f"{requested_mid}"
                            )

                elif line.startswith(BulletinForwarder._FWD_END):
                    # End of their offer/request batch — continue listening
                    continue

            send(BulletinForwarder._FWD_BYE)
            return True

        except Exception as e:
            log.error(f"Inbound forwarding session error: {e}", exc_info=True)
            return False
