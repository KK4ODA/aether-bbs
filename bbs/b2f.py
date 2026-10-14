"""
bbs/b2f.py

B2F (Burst-2-Forward) protocol engine.
Handles the full Winlink message exchange session:
  - Banner / handshake
  - Challenge/response authentication
  - Proposal exchange
  - Message send/receive with optional LZHUF compression
"""

import zlib
import random
import logging
import binascii
from typing import Optional, Callable

from bbs.store import MessageStore
from bbs.bulletins import BulletinStore, is_bulletin_address

log = logging.getLogger(__name__)

B2F_VERSION          = "B2F-2.4.1.0"


class B2FSession:
    """
    Handles one complete B2F session with a remote Winlink client.

    send_fn:  callable(str) — sends text over the data channel
    recv_fn:  callable(timeout) → str|None — receives a line from the channel
    recv_raw: callable(n_bytes, timeout) → bytes — receives raw bytes
    """

    def __init__(
        self,
        bbs_callsign: str,
        store: MessageStore,
        send_fn:       Callable[[str], None],
        recv_fn:       Callable[[float], Optional[str]],
        recv_raw:      Callable[[int, float], bytes],
        send_raw_fn:   Callable[[bytes], None]  = None,
        set_raw_mode:  Callable[[bool], None]   = None,
        bulletin_store: "BulletinStore | None"  = None,
    ):
        self.bbs_callsign    = bbs_callsign
        self.store           = store
        self.bulletin_store  = bulletin_store
        self._send         = send_fn
        self._recv         = recv_fn
        self._recv_raw     = recv_raw
        self._send_raw_fn  = send_raw_fn
        self._set_raw_mode = set_raw_mode or (lambda enabled: None)

        self.remote_callsign: Optional[str] = None
        self.msgs_sent = 0
        self.msgs_recv = 0

    def run(self, first_line: Optional[str] = None) -> bool:
        """
        Execute a full B2F session.
        first_line: the first line already read by the dispatcher.
        Returns True on clean completion.
        """
        try:
            # ── Handshake ────────────────────────────────────────────────
            self._send(f"[WL2K-2.0-{B2F_VERSION}-T1FM]\r")

            # If the dispatcher already consumed the first line, use it
            client_banner = first_line or self._recv(30)
            if not client_banner:
                log.warning("B2F: no client banner received")
                return False

            self.remote_callsign = self._parse_callsign(client_banner)
            log.info(f"B2F session with {self.remote_callsign}")

            # Challenge / response
            challenge = random.randint(10_000_000, 99_999_999)
            self._send(f";PQ: {challenge}\r")
            pr_line = self._recv(5)
            if pr_line and pr_line.startswith(";PR:"):
                expected = self._compute_response(challenge)
                received = pr_line.split(":", 1)[1].strip()
                if received != expected:
                    log.warning(
                        f"B2F: challenge/response mismatch "
                        f"(expected {expected}, got {received})"
                    )
                    # Accept anyway — open BBS policy

            # Send BBS ident
            self._send(f"[{self.bbs_callsign}]\r")

            # ── Proposal exchange ─────────────────────────────────────────
            self._proposal_exchange()

            # ── Finish ────────────────────────────────────────────────────
            self._send("FF\r")
            self._recv(3)   # brief wait for FF echo; not required to proceed
            log.info(
                f"B2F session complete: {self.msgs_sent} sent, "
                f"{self.msgs_recv} received"
            )
            return True

        except Exception as e:
            log.error(f"B2F session error: {e}", exc_info=True)
            return False

    # ── Proposal exchange ─────────────────────────────────────────────────

    def _proposal_exchange(self):
        """Send outbound proposals, receive inbound proposals."""
        if not self.remote_callsign:
            return

        # Outbound: messages destined for this client
        outbound = self.store.get_messages_for(self.remote_callsign)
        for msg in outbound:
            body = self._format_message(msg)
            size = len(body)
            self._send(f"FC EM {msg['id']} {size} {size} 0\r")
        self._send("F>\r")

        # Parse FS response
        fs_line = self._recv(30) or ""
        accepted_flags = self._parse_fs(fs_line, len(outbound))

        for i, msg in enumerate(outbound):
            if i < len(accepted_flags) and accepted_flags[i] == "+":
                body = self._format_message(msg)
                self._send_raw(body)
                self.store.mark_sent(msg["id"])
                self.msgs_sent += 1
                log.info(f"B2F: sent MID {msg['id']} to {self.remote_callsign}")

        # Inbound: collect all FC proposals, send one FS response, then receive bodies.
        # First response timeout is short — a client with nothing to send
        # replies FF immediately. Subsequent FC lines use longer timeout.
        proposals = []   # list of (mid, size, compress, accept)
        first = True
        while True:
            line = self._recv(8 if first else 30)
            first = False
            if line is None:
                break
            line = line.strip()

            if line.startswith("FC"):
                result = self._handle_inbound_proposal(line)
                if result:
                    proposals.append(result)
            elif line.startswith("F>"):
                # Client done sending proposals — send single FS line
                if proposals:
                    flags = "".join("+" if p[3] else "=" for p in proposals)
                    self._send(f"FS {flags}\r")
                    # Now receive bodies for accepted proposals
                    for mid, size, compress, accept in proposals:
                        if accept:
                            self._receive_message(mid, size, compress)
                else:
                    self._send("FS\r")
                self._send("F>\r")
                break
            elif line.startswith("FF"):
                break

    def _handle_inbound_proposal(self, line: str):
        """Accept one inbound FC proposal line. Called per FC line.
        Returns (mid, size, compress, accepted) for batch FS sending.
        """
        parts = line.split()
        if len(parts) < 4:
            return None

        mid      = parts[2]
        size     = int(parts[3]) if parts[3].isdigit() else 0
        compress = len(parts) >= 6 and parts[5] == "1"
        accept   = not self.store.has_message(mid)

        if not accept:
            log.debug(f"B2F: already have MID {mid}, rejecting")

        return mid, size, compress, accept

    def _receive_message(self, mid: str, size: int, compress: bool):
        """Receive raw bytes for one accepted message and store it."""
        self._set_raw_mode(True)
        try:
            raw = self._recv_raw(size, 120)
        finally:
            self._set_raw_mode(False)

        if not raw:
            log.warning(f"B2F: no data received for MID {mid}")
            return

        if compress:
            try:
                raw = zlib.decompress(raw)
            except zlib.error as e:
                log.error(f"B2F: decompression failed for MID {mid}: {e}")
                return

        msg = self._parse_message(raw)
        if msg:
            from_call = msg.get("from", self.remote_callsign)
            to_call   = msg.get("to",   self.bbs_callsign)
            subject   = msg.get("subject", "")
            body      = msg.get("body", b"")
            if self.bulletin_store and is_bulletin_address(to_call):
                self.bulletin_store.save_bulletin(
                    from_call=from_call,
                    to_group=to_call.upper(),
                    subject=subject,
                    body=body,
                    mid=mid,
                )
                log.info(
                    f"B2F: bulletin MID {mid} → area {to_call.upper()} "
                    f"from {self.remote_callsign}"
                )
            else:
                self.store.save_message(
                    from_call=from_call,
                    to_call=to_call,
                    subject=subject,
                    body=body,
                    mid=mid,
                )
                log.info(f"B2F: received MID {mid} from {self.remote_callsign}")
            self.msgs_recv += 1

    # ── Message formatting ────────────────────────────────────────────────

    def _format_message(self, msg) -> bytes:
        """Encode a message row as a B2F message block."""
        body = msg["body"]
        if isinstance(body, str):
            body = body.encode("utf-8", errors="replace")

        headers = (
            f"MID: {msg['id']}\r\n"
            f"Date: {msg['date']}\r\n"
            f"From: {msg['from_call']}@winlink.org\r\n"
            f"To: {msg['to_call']}@winlink.org\r\n"
            f"Subject: {msg['subject']}\r\n"
            f"Body: {len(body)}\r\n"
            f"\r\n"
        )
        return headers.encode("ascii", errors="replace") + body

    def _parse_message(self, raw: bytes) -> Optional[dict]:
        """Parse a B2F message block into a dict."""
        try:
            header_end = raw.find(b"\r\n\r\n")
            if header_end == -1:
                header_end = raw.find(b"\n\n")
            if header_end == -1:
                return None

            header_bytes = raw[:header_end]
            body_start   = header_end + 4 if b"\r\n\r\n" in raw else header_end + 2

            headers: dict[str, str] = {}
            for line in header_bytes.decode("ascii", errors="replace").splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    headers[k.strip().lower()] = v.strip()

            body_len = int(headers.get("body", "0"))
            body = raw[body_start: body_start + body_len]

            return {
                "mid":     headers.get("mid", ""),
                "from":    headers.get("from", "").split("@")[0],
                "to":      headers.get("to", "").split("@")[0],
                "subject": headers.get("subject", ""),
                "date":    headers.get("date", ""),
                "body":    body,
            }
        except Exception as e:
            log.error(f"B2F: message parse error: {e}")
            return None

    # ── Helpers ───────────────────────────────────────────────────────────

    @staticmethod
    def _compute_response(challenge: int) -> str:
        challenge_bytes = challenge.to_bytes(4, byteorder="little")
        response = binascii.crc32(challenge_bytes) & 0xFFFFFFFF
        return str(response)

    @staticmethod
    def _parse_callsign(banner: str) -> str:
        try:
            inner = banner.strip().strip("[]")
            return inner.split("-")[0].upper()
        except Exception:
            return "UNKNOWN"

    @staticmethod
    def _parse_fs(fs_line: str, count: int) -> list[str]:
        """Parse an FS response line into a list of '+', '=', or '-' chars."""
        if " " in fs_line:
            chars = fs_line.split(" ", 1)[1].strip()
        else:
            chars = ""
        return list(chars.ljust(count, "="))

    def _send_raw(self, data: bytes):
        """Send raw bytes over the transport."""
        if self._send_raw_fn is not None:
            self._send_raw_fn(data)
        else:
            # Fallback for transports where send_fn accepts bytes
            try:
                self._send(data)  # type: ignore
            except TypeError:
                self._send(data.decode("latin-1"))
