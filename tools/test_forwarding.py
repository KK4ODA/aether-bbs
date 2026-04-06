#!/usr/bin/env python3
"""
tools/test_forwarding.py

Loopback forwarding test harness for VARA BBS.

Simulates a remote neighbour BBS so the sysop can exercise the full
bulletin forwarding protocol without deploying a second BBS instance.
Runs as a standalone CLI tool — no PyQt or VARA modem required.

Usage
─────
  1. Enable the forwarding server in your config:

       forwarding:
         server_enabled: true
         server_port: 7300
         server_bind: "127.0.0.1"   # loopback is fine for testing

  2. Start the BBS normally.

  3. In a separate terminal:

       python tools/test_forwarding.py --port 7300

     This connects to the BBS forwarding server and runs a menu-driven
     test session where you can:
       • Push test bulletins TO the BBS   (exercises server receive path)
       • Pull bulletins FROM the BBS      (exercises server send path)
       • Run full bidirectional exchange   (exercises complete protocol)
       • Inject urgent vs normal bulletins
       • Verify forwarding log entries in the GUI Forwarding tab

  4. Watch the BBS log output and the Bulletins → Forwarding tab for
     results.

Protocol Reference
──────────────────
  FWD HELLO <callsign>         Handshake
  FWD OFFER <mid> <area> <sz>  Offer a bulletin
  FWD GET *                    Request the peer's offer list
  FWD GET <mid>                Request a specific bulletin
  FWD DATA <mid> <from> <to> <flags> <subject>   Bulletin header
  FWD BODY                     Start of body
  FWD END                      End of body / end of offer list
  FWD ACK <mid>                Accepted
  FWD NAK <mid>                Rejected (duplicate / filtered)
  FWD BYE                      Session end

  Flags: "-" = normal, "U" = urgent

Transport
─────────
  This module operates over TCP sockets — it is network-layer forwarding
  between BBS nodes.  It is NOT tied to a specific RF mode (FM or HF).
  Neighbour BBS nodes communicate over IP, which may ride over the
  internet, a LAN, a VPN, or even an IP-over-RF tunnel.  The VARA FM/HF
  modems are not involved in forwarding; they serve RF terminal users.
"""

import argparse
import logging
import socket
import sys
import time
import uuid

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s %(levelname)-5s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("fwd-test")


# ── Protocol tokens ──────────────────────────────────────────────────────────

FWD_HELLO = "FWD HELLO"
FWD_OFFER = "FWD OFFER"
FWD_GET   = "FWD GET"
FWD_DATA  = "FWD DATA"
FWD_BODY  = "FWD BODY"
FWD_END   = "FWD END"
FWD_ACK   = "FWD ACK"
FWD_NAK   = "FWD NAK"
FWD_BYE   = "FWD BYE"


# ── Socket helpers ───────────────────────────────────────────────────────────

def send_line(sock: socket.socket, line: str):
    """Send a protocol line (auto-appends CRLF)."""
    data = (line + "\r\n").encode("ascii", errors="replace")
    sock.sendall(data)
    log.debug(f"  → {line}")


def recv_line(sock: socket.socket, timeout: float = 10.0) -> str | None:
    """Receive one CRLF-terminated line."""
    sock.settimeout(timeout)
    raw = b""
    try:
        while not raw.endswith(b"\n"):
            chunk = sock.recv(1)
            if not chunk:
                return None
            raw += chunk
    except socket.timeout:
        if raw:
            log.warning(f"  ← (partial, timed out): {raw!r}")
        return None
    decoded = raw.decode("ascii", errors="replace").strip()
    log.debug(f"  ← {decoded}")
    return decoded


def make_mid() -> str:
    """Generate a unique message ID."""
    return f"TEST{uuid.uuid4().hex[:8].upper()}"


# ── Handshake ────────────────────────────────────────────────────────────────

def do_handshake(sock: socket.socket, our_call: str) -> str | None:
    """Perform FWD HELLO exchange.  Returns the remote BBS callsign."""
    send_line(sock, f"{FWD_HELLO} {our_call}")
    reply = recv_line(sock)
    if not reply or not reply.startswith(FWD_HELLO):
        log.error(f"Handshake failed: {reply!r}")
        return None
    parts = reply.split()
    remote = parts[2] if len(parts) >= 3 else "UNKNOWN"
    log.info(f"Handshake OK — remote BBS is {remote}")
    return remote


# ── Test actions ─────────────────────────────────────────────────────────────

def push_bulletin(sock, from_call: str, area: str, subject: str,
                  body: str, urgent: bool = False):
    """
    Push a single test bulletin TO the BBS.

    Protocol: we send OFFER, if server responds GET we send DATA.
    """
    mid = make_mid()
    size = len(body.encode("utf-8"))
    flags = "U" if urgent else "-"

    log.info(f"Offering bulletin MID={mid} area={area} "
             f"urgent={urgent} size={size}")
    send_line(sock, f"{FWD_OFFER} {mid} {area} {size}")

    reply = recv_line(sock)
    if not reply:
        log.error("No reply to OFFER")
        return False

    if reply.startswith(FWD_GET):
        # Server wants it — send DATA
        send_line(sock, f"{FWD_DATA} {mid} {from_call} {area} {flags} {subject}")
        send_line(sock, FWD_BODY)
        for line in body.splitlines():
            send_line(sock, line)
        send_line(sock, FWD_END)

        ack = recv_line(sock)
        if ack and ack.startswith(FWD_ACK):
            log.info(f"✔ Bulletin {mid} accepted by server")
            return True
        else:
            log.warning(f"✘ Bulletin {mid} rejected: {ack!r}")
            return False

    elif reply.startswith(FWD_NAK):
        log.info(f"Server already has {mid} or filtered — NAK")
        return False
    else:
        log.error(f"Unexpected reply to OFFER: {reply!r}")
        return False


def pull_bulletins(sock) -> list[dict]:
    """
    Pull bulletins FROM the BBS.

    Protocol: we send GET *, receive OFFER list, GET each one we want,
    receive DATA for each.
    """
    received = []
    log.info("Requesting server's bulletin offer list...")
    send_line(sock, f"{FWD_GET} *")

    offers = []
    while True:
        line = recv_line(sock)
        if line is None or line.startswith(FWD_BYE):
            break
        if line.startswith(FWD_END):
            break
        if line.startswith(FWD_OFFER):
            parts = line.split()
            if len(parts) >= 3:
                offers.append({"mid": parts[1], "area": parts[2],
                               "size": parts[3] if len(parts) >= 4 else "?"})

    if not offers:
        log.info("Server has no bulletins to offer.")
        return received

    log.info(f"Server offered {len(offers)} bulletin(s)")

    for offer in offers:
        mid = offer["mid"]
        log.info(f"Requesting MID={mid} area={offer['area']}...")
        send_line(sock, f"{FWD_GET} {mid}")

        # Expect FWD DATA header
        header = recv_line(sock)
        if not header or not header.startswith(FWD_DATA):
            log.error(f"Expected FWD DATA, got: {header!r}")
            send_line(sock, f"{FWD_NAK} {mid}")
            continue

        # Parse header: FWD DATA <mid> <from> <to> <flags> <subject>
        # or legacy:   FWD DATA <mid> <from> <to> <subject>
        hparts = header.split(" ", 6)
        if len(hparts) >= 7 and hparts[5] in ("-", "U"):
            h_from, h_to, h_flags, h_subj = hparts[3], hparts[4], hparts[5], hparts[6]
        else:
            hparts6 = header.split(" ", 5)
            h_from = hparts6[3] if len(hparts6) > 3 else "?"
            h_to   = hparts6[4] if len(hparts6) > 4 else "?"
            h_flags = "-"
            h_subj = hparts6[5] if len(hparts6) > 5 else "?"

        # Expect FWD BODY
        body_marker = recv_line(sock)
        if not body_marker or not body_marker.startswith(FWD_BODY):
            log.error(f"Expected FWD BODY, got: {body_marker!r}")
            send_line(sock, f"{FWD_NAK} {mid}")
            continue

        # Read body lines until FWD END
        body_lines = []
        while True:
            bline = recv_line(sock)
            if bline is None or bline.startswith(FWD_END):
                break
            body_lines.append(bline)

        bulletin = {
            "mid": mid, "from": h_from, "to": h_to,
            "flags": h_flags, "subject": h_subj,
            "body": "\n".join(body_lines),
            "urgent": h_flags == "U",
        }
        received.append(bulletin)

        send_line(sock, f"{FWD_ACK} {mid}")
        urgent_tag = " [URGENT]" if bulletin["urgent"] else ""
        log.info(f"✔ Received MID={mid}{urgent_tag}: {h_subj}")

    return received


# ── Full bidirectional test ──────────────────────────────────────────────────

def run_full_test(sock, our_call: str):
    """Push test bulletins, then pull whatever the BBS has."""
    print("\n" + "=" * 60)
    print("  PHASE 1: Push test bulletins TO the BBS")
    print("=" * 60)

    push_bulletin(
        sock, our_call, "ALL",
        "Forwarding test — normal bulletin",
        "This is a normal-priority test bulletin\n"
        "sent by the loopback test harness.\n"
        f"Timestamp: {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}",
        urgent=False,
    )

    push_bulletin(
        sock, our_call, "WX",
        "Forwarding test — URGENT weather alert",
        "⚠ URGENT weather test bulletin\n"
        "This should appear with urgency flags in the BBS.\n"
        f"Timestamp: {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}",
        urgent=True,
    )

    push_bulletin(
        sock, our_call, "LOCAL",
        "Forwarding test — local info",
        "Local area information test.\n"
        f"From simulated neighbour {our_call}.",
        urgent=False,
    )

    print("\n" + "=" * 60)
    print("  PHASE 2: Pull bulletins FROM the BBS")
    print("=" * 60)

    received = pull_bulletins(sock)
    if received:
        print(f"\nReceived {len(received)} bulletin(s) from BBS:")
        for b in received:
            urgent_tag = " 🚨 URGENT" if b["urgent"] else ""
            print(f"  MID={b['mid']}  {b['to']}{urgent_tag}  {b['subject']}")
    else:
        print("\nNo bulletins available from BBS (all already seen, or none exist).")


# ── Interactive menu ─────────────────────────────────────────────────────────

def interactive_menu(sock, our_call: str):
    """Simple interactive menu for targeted testing."""
    while True:
        print("\n" + "─" * 50)
        print("  Forwarding Test Harness")
        print("─" * 50)
        print("  1. Push a normal test bulletin")
        print("  2. Push an URGENT test bulletin")
        print("  3. Pull all available bulletins from BBS")
        print("  4. Run full bidirectional test (push 3 + pull)")
        print("  5. Push custom bulletin (interactive)")
        print("  Q. Quit")
        print()

        choice = input("Choice: ").strip().upper()

        if choice == "1":
            push_bulletin(
                sock, our_call, "ALL",
                f"Test bulletin {time.strftime('%H:%M:%S')}",
                f"Normal test from {our_call} at {time.strftime('%Y-%m-%d %H:%M:%S')}",
                urgent=False,
            )
        elif choice == "2":
            push_bulletin(
                sock, our_call, "WX",
                f"URGENT test {time.strftime('%H:%M:%S')}",
                f"Urgent weather test from {our_call} at {time.strftime('%Y-%m-%d %H:%M:%S')}",
                urgent=True,
            )
        elif choice == "3":
            received = pull_bulletins(sock)
            if received:
                for b in received:
                    u = " 🚨" if b["urgent"] else ""
                    print(f"  {b['mid']}  {b['to']}{u}  {b['subject']}")
                    print(f"    Body: {b['body'][:80]}...")
            else:
                print("  No bulletins available.")
        elif choice == "4":
            run_full_test(sock, our_call)
        elif choice == "5":
            area = input("  Area [ALL]: ").strip().upper() or "ALL"
            subj = input("  Subject: ").strip() or f"Custom test {time.strftime('%H:%M:%S')}"
            body = input("  Body: ").strip() or "Custom test body"
            urg  = input("  Urgent? [y/N]: ").strip().upper() == "Y"
            push_bulletin(sock, our_call, area, subj, body, urgent=urg)
        elif choice == "Q":
            break
        else:
            print("  Invalid choice.")


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Loopback forwarding test harness for VARA BBS",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Usage")[0],
    )
    parser.add_argument(
        "--host", default="127.0.0.1",
        help="BBS forwarding server address (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port", type=int, default=7300,
        help="BBS forwarding server port (default: 7300)",
    )
    parser.add_argument(
        "--call", default="TEST0-1",
        help="Simulated neighbour BBS callsign (default: TEST0-1)",
    )
    parser.add_argument(
        "--auto", action="store_true",
        help="Run full automatic test then exit (no interactive menu)",
    )
    args = parser.parse_args()

    print(f"Connecting to BBS forwarding server at {args.host}:{args.port}...")
    print(f"Simulating neighbour BBS: {args.call}")
    print()

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(10)

    try:
        sock.connect((args.host, args.port))
    except (socket.error, OSError) as e:
        print(f"✘ Connection failed: {e}")
        print()
        print("Make sure your BBS is running and the forwarding server is enabled:")
        print("  forwarding:")
        print("    server_enabled: true")
        print(f"    server_port: {args.port}")
        sys.exit(1)

    remote = do_handshake(sock, args.call)
    if not remote:
        print("✘ Handshake failed — is the forwarding server running?")
        sock.close()
        sys.exit(1)

    print(f"✔ Connected to BBS: {remote}")

    try:
        if args.auto:
            run_full_test(sock, args.call)
        else:
            interactive_menu(sock, args.call)
    except KeyboardInterrupt:
        print("\nInterrupted.")
    finally:
        try:
            send_line(sock, FWD_BYE)
        except Exception:
            pass
        sock.close()
        print("Session ended.")


if __name__ == "__main__":
    main()
