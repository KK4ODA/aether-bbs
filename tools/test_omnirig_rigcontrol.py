"""
tools/test_omnirig_rigcontrol.py

Standalone diagnostic for testing OmniRig frequency / mode control.
Run from the project root:

    python tools\\test_omnirig_rigcontrol.py [1|2]

  1 = OmniRig Rig1 (default)
  2 = OmniRig Rig2

Tests:
  1. Connects to OmniRig
  2. Reads current frequency and mode
  3. Sets a test frequency (7122.0 kHz)
  4. Reads back to verify
  5. Restores original frequency
"""

import sys
import time


def main():
    rig_num = 1
    if len(sys.argv) > 1 and sys.argv[1] in ("1", "2"):
        rig_num = int(sys.argv[1])

    print(f"=== OmniRig Rig Control Test — Rig {rig_num} ===\n")

    try:
        import pythoncom
        import win32com.client
    except ImportError:
        print("ERROR: pywin32 is not installed.")
        print("Install with:  pip install pywin32")
        return 1

    try:
        pythoncom.CoInitialize()
    except Exception:
        pass

    try:
        omnirig = win32com.client.Dispatch("OmniRig.OmniRigX")
    except Exception as e:
        print(f"ERROR: Cannot connect to OmniRig: {e}")
        print("Make sure OmniRig is running.")
        return 1

    rig = omnirig.Rig1 if rig_num == 1 else omnirig.Rig2

    try:
        status = rig.Status
        print(f"OmniRig status: 0x{status:04X}")
        if status == 0:
            print("WARNING: Status=0 — rig may not be connected in OmniRig")
    except Exception as e:
        print(f"WARNING: Could not read status: {e}")

    # Read current frequency
    try:
        orig_freq = rig.FreqA
        print(f"Current FreqA:  {orig_freq} Hz  ({orig_freq/1000:.1f} kHz)")
    except Exception as e:
        print(f"ERROR reading frequency: {e}")
        return 1

    # Read current mode
    try:
        mode_val = rig.Mode
        print(f"Current Mode:   0x{mode_val:08X}")
    except Exception as e:
        print(f"Could not read mode: {e}")

    # Set test frequency
    test_freq_khz = 7122.0
    test_freq_hz = int(test_freq_khz * 1000)
    print(f"\nSetting frequency to {test_freq_khz} kHz ({test_freq_hz} Hz)...")

    try:
        rig.FreqA = test_freq_hz
        time.sleep(0.5)
        readback = rig.FreqA
        print(f"Readback:       {readback} Hz  ({readback/1000:.1f} kHz)")
        if readback == test_freq_hz:
            print("✓ Frequency set successfully!")
        else:
            print(f"⚠ Readback mismatch: expected {test_freq_hz}, got {readback}")
    except Exception as e:
        print(f"ERROR setting frequency: {e}")
        return 1

    # Set USB mode
    USB_MODE = 0x02000000
    print(f"\nSetting mode to USB (0x{USB_MODE:08X})...")
    try:
        rig.Mode = USB_MODE
        time.sleep(0.5)
        mode_readback = rig.Mode
        print(f"Mode readback:  0x{mode_readback:08X}")
        if mode_readback == USB_MODE:
            print("✓ Mode set successfully!")
        else:
            print(f"⚠ Mode readback mismatch")
    except Exception as e:
        print(f"ERROR setting mode: {e}")

    # Restore original frequency
    print(f"\nRestoring original frequency: {orig_freq} Hz...")
    try:
        rig.FreqA = orig_freq
        time.sleep(0.5)
        final = rig.FreqA
        print(f"Restored to:    {final} Hz  ({final/1000:.1f} kHz)")
        if final == orig_freq:
            print("✓ Original frequency restored!")
        else:
            print(f"⚠ Restore readback mismatch")
    except Exception as e:
        print(f"ERROR restoring frequency: {e}")

    try:
        pythoncom.CoUninitialize()
    except Exception:
        pass

    print("\n=== Test complete ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
