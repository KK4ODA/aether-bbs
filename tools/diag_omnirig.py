"""
diag_omnirig.py -- OmniRig diagnostic tool

Run from a command prompt (close VARA BBS first):
    python diag_omnirig.py

Reports OmniRig status, waits for the radio to come online,
then tests frequency set and PTT.
"""

import sys
import time

STATUS_NAMES = {
    0: "ST_NOTCONFIGURED  (no rig profile selected)",
    1: "ST_DISABLED        (rig disabled in OmniRig)",
    2: "ST_PORTBUSY        (serial port in use by another app)",
    3: "ST_NOTRESPONDING   (port open, radio not answering CAT)",
    4: "ST_ONLINE          (radio connected, read-only)",
    5: "ST_TRANSCEIVE      (radio connected, read-write)",
}

TX_NAMES = {
    0x00000001: "PM_UNKNOWN  (state unknown)",
    0x00200000: "PM_RX       (receiving)",
    0x00400000: "PM_TX       (transmitting)",
}

PM_RX = 0x00200000
PM_TX = 0x00400000


def main():
    rig_num = 1
    if len(sys.argv) > 1:
        rig_num = int(sys.argv[1])

    print(f"{'='*60}")
    print(f"  OmniRig Diagnostic -- Rig{rig_num}")
    print(f"{'='*60}\n")

    # -- Step 1: Import pywin32 ---
    print("[1] Importing pywin32...")
    try:
        import win32com.client
        import pythoncom
        print("    OK\n")
    except ImportError:
        print("    FAIL -- pip install pywin32")
        return

    # -- Step 2: Connect to OmniRig ---
    print("[2] Connecting to OmniRig (STA + Dispatch)...")
    try:
        pythoncom.CoInitialize()
        omnirig = win32com.client.Dispatch("OmniRig.OmniRigX")
        print("    OK -- connected to OmniRig.OmniRigX\n")
    except Exception as e:
        print(f"    FAIL -- {e}")
        print("    Is OmniRig installed and running?")
        return

    rig = omnirig.Rig1 if rig_num == 1 else omnirig.Rig2

    # -- Step 3: Immediate status read ---
    print("[3] Immediate status read (before any waiting):")
    try:
        status = rig.Status
        freq = rig.FreqA
        tx = rig.Tx
        print(f"    Status = {status}  {STATUS_NAMES.get(status, '???')}")
        print(f"    FreqA  = {freq} Hz")
        print(f"    Tx     = 0x{tx:08X}  {TX_NAMES.get(tx, '')}")
    except Exception as e:
        print(f"    ERROR: {e}")
    print()

    # -- Step 4: Wait for online with message pumping ---
    print("[4] Waiting up to 10 seconds for radio to come online...")
    print("    (pumping Windows messages each cycle)")
    online = False
    last_status = -1
    for i in range(100):
        try:
            pythoncom.PumpWaitingMessages()
        except Exception:
            pass
        try:
            status = rig.Status
            if status != last_status:
                sname = STATUS_NAMES.get(status, f"UNKNOWN({status})")
                print(f"    [{i*0.1:.1f}s] Status changed: {status} = {sname}")
                last_status = status
            if status >= 4:  # ST_ONLINE or ST_TRANSCEIVE
                online = True
                break
        except Exception as e:
            print(f"    [{i*0.1:.1f}s] Error reading status: {e}")
        time.sleep(0.1)

    if online:
        print(f"\n    RADIO IS ONLINE (status={last_status})")
        try:
            freq = rig.FreqA
            mode = rig.Mode
            print(f"    FreqA = {freq} Hz ({freq/1000:.1f} kHz)")
            print(f"    Mode  = 0x{mode:08X}")
        except Exception as e:
            print(f"    Read error: {e}")
    else:
        print(f"\n    RADIO NOT ONLINE after 10s (status={last_status})")
        print()
        print("    This is the problem. OmniRig cannot communicate")
        print("    with the radio. Possible causes:")
        print()
        print("    a) OmniRig rig type doesn't match your radio")
        print("       Open OmniRig Setup and check Rig1 settings")
        print()
        print("    b) Another program is holding the COM port")
        print("       Close VARA HF, JS8Call, WSJT-X, etc.")
        print()
        print("    c) FTDX10: make sure you're using the 'Enhanced'")
        print("       COM port, not the 'Standard' one")
        print()
        print("    d) Try: close everything, unplug/replug USB,")
        print("       restart OmniRig, then run this script again")

    print()

    # -- Step 5: Test OmniRig interface version ---
    print("[5] OmniRig interface info:")
    for prop in ["InterfaceVersion", "SoftwareVersion"]:
        try:
            val = getattr(omnirig, prop)
            print(f"    {prop} = {val}")
        except Exception:
            print(f"    {prop} = (not available)")
    try:
        rig_type = rig.RigType
        print(f"    Rig{rig_num}.RigType = '{rig_type}'")
    except Exception:
        print(f"    Rig{rig_num}.RigType = (not available)")
    try:
        port_bits = rig.PortBits
        print(f"    Rig{rig_num}.PortBits = {port_bits}")
    except Exception:
        pass
    print()

    if not online:
        print("    Skipping frequency/PTT tests (radio not online)")
        input("\n    Press Enter to exit...")
        try:
            pythoncom.CoUninitialize()
        except Exception:
            pass
        return

    # -- Step 6: Test frequency set ---
    print("[6] Testing frequency set...")
    try:
        old_freq = rig.FreqA
        test_freq = 7074000
        print(f"    Current:  {old_freq} Hz")
        print(f"    Setting:  {test_freq} Hz...")
        rig.FreqA = test_freq
        # Wait and pump
        for _ in range(10):
            pythoncom.PumpWaitingMessages()
            time.sleep(0.1)
        new_freq = rig.FreqA
        print(f"    Readback: {new_freq} Hz")
        if abs(new_freq - test_freq) < 100:
            print("    OK -- frequency set works!")
        elif new_freq == old_freq:
            print("    FAIL -- frequency did not change")
        else:
            print(f"    PARTIAL -- changed but readback differs")

        # Restore
        if old_freq > 0:
            print(f"    Restoring {old_freq} Hz...")
            rig.FreqA = old_freq
            time.sleep(0.5)
            pythoncom.PumpWaitingMessages()
    except Exception as e:
        print(f"    ERROR: {e}")
    print()

    # -- Step 7: Test PTT ---
    print("[7] Testing PTT (2-second key)...")
    print("    >>> RADIO WILL TRANSMIT <<<")
    resp = input("    Continue? (y/n): ").strip().lower()
    if resp != 'y':
        print("    Skipped")
    else:
        try:
            print(f"    Setting Tx = PM_TX...")
            rig.Tx = PM_TX
            for _ in range(5):
                pythoncom.PumpWaitingMessages()
                time.sleep(0.1)
            rb = rig.Tx
            print(f"    Readback: 0x{rb:08X}  {TX_NAMES.get(rb, '')}")
            if rb == PM_TX:
                print("    PTT is ON")
            else:
                print("    PTT may not have engaged")

            time.sleep(1.5)
            print(f"    Setting Tx = PM_RX...")
            rig.Tx = PM_RX
            for _ in range(5):
                pythoncom.PumpWaitingMessages()
                time.sleep(0.1)
            rb = rig.Tx
            print(f"    Readback: 0x{rb:08X}  {TX_NAMES.get(rb, '')}")
        except Exception as e:
            print(f"    ERROR: {e}")
            try:
                rig.Tx = PM_RX
            except Exception:
                pass
    print()

    # -- Summary ---
    print(f"{'='*60}")
    print("  Done")
    print(f"{'='*60}")

    try:
        pythoncom.CoUninitialize()
    except Exception:
        pass

    input("\nPress Enter to exit...")


if __name__ == "__main__":
    main()
