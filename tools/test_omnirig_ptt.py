"""
Standalone OmniRig PTT diagnostic tool.

Run this script directly to test OmniRig PTT independently
from the BBS application.  It will:

  1. Connect to OmniRig
  2. Show rig status and type
  3. Key PTT for 3 seconds
  4. Unkey PTT
  5. Report read-back values at each step

Usage:
    python tools/test_omnirig_ptt.py          (uses Rig 1)
    python tools/test_omnirig_ptt.py 2        (uses Rig 2)

Requires: pip install pywin32
"""

import sys
import time


def main():
    rig_num = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    assert rig_num in (1, 2), "Rig number must be 1 or 2"

    # OmniRig constants (verified against omnipyrig 4Z1KD)
    PM_RX = 0x00200000   # receive (PTT off)
    PM_TX = 0x00400000   # transmit (PTT on)

    print(f"=== OmniRig PTT Diagnostic — Rig {rig_num} ===\n")

    # ── Step 1: COM init ──────────────────────────────────────────────
    print("[1] Initializing COM...")
    try:
        import pythoncom
        pythoncom.CoInitialize()
        print("    COM initialized OK")
    except Exception as e:
        print(f"    COM init: {e} (may already be initialized)")

    # ── Step 2: Connect to OmniRig ───────────────────────────────────
    print("[2] Connecting to OmniRig...")
    try:
        import win32com.client
        omnirig = win32com.client.Dispatch("OmniRig.OmniRigX")
        print(f"    OmniRig connected: {omnirig}")
    except Exception as e:
        print(f"    *** FAILED: {e}")
        print("    Is OmniRig installed and running?")
        return 1

    # ── Step 3: Get rig reference ────────────────────────────────────
    print(f"[3] Accessing Rig{rig_num}...")
    try:
        rig = omnirig.Rig1 if rig_num == 1 else omnirig.Rig2
        print(f"    Rig object: {rig}")
    except Exception as e:
        print(f"    *** FAILED: {e}")
        return 1

    # ── Step 4: Check rig status ─────────────────────────────────────
    print("[4] Checking rig status...")
    status_names = {
        0: "ST_NOTCONFIGURED",
        1: "ST_DISABLED",
        2: "ST_PORTBUSY",
        3: "ST_NOTRESPONDING",
        4: "ST_ONLINE",
        5: "ST_TRANSVERTER",
    }
    try:
        status = rig.Status
        name = status_names.get(status, f"UNKNOWN({status})")
        print(f"    Status: 0x{status:04X} = {name}")
        if status != 4:
            print(f"    *** WARNING: Rig is not ONLINE (expected 0x0004)")
    except Exception as e:
        print(f"    Status check failed: {e}")

    try:
        rig_type = rig.RigType
        print(f"    RigType: {rig_type}")
    except Exception as e:
        print(f"    RigType: (unavailable: {e})")

    # ── Step 5: Read current Tx state ────────────────────────────────
    print("[5] Reading current Tx state...")
    try:
        tx_state = rig.Tx
        print(f"    Current Tx: 0x{tx_state:08X}")
        if tx_state == PM_RX:
            print("    = PM_RX (receive mode) — OK")
        elif tx_state == PM_TX:
            print("    = PM_TX (transmit mode) — radio is already keyed!")
        else:
            print(f"    = UNKNOWN value (expected 0x{PM_RX:08X} or 0x{PM_TX:08X})")
    except Exception as e:
        print(f"    Tx read failed: {e}")

    # ── Step 6: KEY PTT ──────────────────────────────────────────────
    print(f"\n[6] *** KEYING PTT (Tx = 0x{PM_TX:08X}) ***")
    print("    >>> RADIO SHOULD TRANSMIT NOW <<<")
    try:
        rig.Tx = PM_TX
        print("    Property set completed (no exception)")
    except Exception as e:
        print(f"    *** SET FAILED: {e}")
        return 1

    # Read back
    time.sleep(0.5)
    try:
        tx_state = rig.Tx
        print(f"    Tx readback: 0x{tx_state:08X}", end="")
        if tx_state == PM_TX:
            print(" ✅ CONFIRMED TX")
        elif tx_state == PM_RX:
            print(" ❌ STILL IN RX — PTT did not engage!")
        else:
            print(f" ⚠ UNEXPECTED VALUE")
    except Exception as e:
        print(f"    Tx readback failed: {e}")

    # Hold for 3 seconds
    print("    Holding PTT for 3 seconds...")
    time.sleep(3.0)

    # ── Step 7: UNKEY PTT ────────────────────────────────────────────
    print(f"\n[7] Releasing PTT (Tx = 0x{PM_RX:08X})...")
    try:
        rig.Tx = PM_RX
        print("    Property set completed (no exception)")
    except Exception as e:
        print(f"    *** SET FAILED: {e}")

    # Read back
    time.sleep(0.5)
    try:
        tx_state = rig.Tx
        print(f"    Tx readback: 0x{tx_state:08X}", end="")
        if tx_state == PM_RX:
            print(" ✅ CONFIRMED RX")
        elif tx_state == PM_TX:
            print(" ❌ STILL IN TX — PTT did not release!")
        else:
            print(f" ⚠ UNEXPECTED VALUE")
    except Exception as e:
        print(f"    Tx readback failed: {e}")

    # ── Done ─────────────────────────────────────────────────────────
    print("\n=== Diagnostic complete ===")
    print("If the radio keyed for 3 seconds, OmniRig PTT is working.")
    print("If not, check OmniRig rig.ini for [pmTx] and [pmRx] commands.")

    try:
        pythoncom.CoUninitialize()
    except Exception:
        pass

    return 0


if __name__ == "__main__":
    sys.exit(main())
