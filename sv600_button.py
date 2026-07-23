#!/usr/bin/env python3
"""
sv600_button.py — probe the SV600's scan button over USB.

WHY THIS EXISTS
---------------
The official software polls GET DATA BUFFER STATUS (op 0xc2) roughly every
200 ms for the whole session. That was previously written off as a constant
idle heartbeat, but it is not constant. Across the three usbmon captures in
this directory its 32-byte payload takes exactly two values:

    00000080 00 000000...      idle
    00000080 01 000000...      byte[4] = 0x01

and the second one appears only in the two captures that contain a SECOND
scan, 2-3 s before that scan starts, for about 400 ms (2 polls at the 200 ms
rate). It never appears in the single-scan capture, whose scan was started
from the GUI:

    capture 171412 : flag t=141.92, scan t=144.00, 2 scans
    capture 170252 : flag t=140.39, scan t=143.74, 2 scans
    capture 160920 : flag never,    scan t=  7.17, 1 scan

That is consistent with byte[4] being the scan button. It is NOT yet proven —
"a document was placed on the mat" would fit the same evidence. This script
settles it: poll 0xc2 and watch what the byte does while you press the button.

USAGE
-----
    sudo python3 sv600_button.py                 # 60 s, full setup first
    sudo python3 sv600_button.py --seconds 120
    sudo python3 sv600_button.py --no-setup      # does 0xc2 answer un-set-up?

The --no-setup question matters for the daemon design: if 0xc2 answers after
nothing more than TEST UNIT READY, a button watcher can idle cheaply and only
run the 12-command SETUP once a press actually arrives.

WHAT TO DO
----------
Run it, then: press the scan button, wait, press it again, wait, and place a
sheet on the mat WITHOUT pressing. If byte[4] tracks the presses and not the
sheet, the button is confirmed. This script never sends SCAN, so it cannot
leave the scanner in the flashing-amber state that a rejected SCAN causes.
"""
import argparse
import sys
import time

import usb.core
import usb.util

from sv600_scan import (SETUP, TEARDOWN, open_dev, cmd, wrap, wake, run_seq,
                        scsi_status, STATUS_GOOD)

# GET DATA BUFFER STATUS, exactly as the official software issues it:
# 10-byte CDB, transfer length 0x20 at CDB bytes 6-8 -> a 32-byte payload.
BUFSTAT_CMD = wrap(bytes([0xc2, 0, 0, 0, 0, 0, 0, 0, 0x20, 0]))
BUFSTAT_LEN = 32

IDLE = bytes.fromhex("0000008000000000000000000000000000000000000000000000000000000000")


def poll(dev):
    """One 0xc2 poll. Returns (payload, scsi_status) or (None, None)."""
    try:
        data, st = cmd(dev, BUFSTAT_CMD, read_len=BUFSTAT_LEN, timeout=2000)
    except usb.core.USBError as e:
        print(f"    [!] USB error on poll: {e}")
        for ep in (0x81, 0x02):
            try:
                dev.clear_halt(ep)
            except Exception:
                pass
        return None, None
    return data[:BUFSTAT_LEN], scsi_status(st)


def diff(a, b):
    """Indices where two payloads differ, as 'byte[i] xx->yy' strings."""
    return [f"byte[{i}] 0x{x:02x}->0x{y:02x}"
            for i, (x, y) in enumerate(zip(a, b)) if x != y]


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seconds", type=float, default=60.0,
                    help="how long to poll (default 60)")
    ap.add_argument("--interval", type=float, default=0.2,
                    help="poll interval in seconds (default 0.2, as the official software)")
    ap.add_argument("--no-setup", action="store_true",
                    help="skip the 12-command SETUP — tests whether 0xc2 answers "
                         "on a bare, woken scanner (matters for the daemon design)")
    args = ap.parse_args()

    dev = open_dev()
    did_setup = False
    try:
        print("[*] waking ...")
        if not wake(dev):
            sys.exit("Scanner did not answer TEST UNIT READY.")

        if not args.no_setup:
            print("[*] running SETUP ...")
            run_seq(dev, SETUP, "setup")
            did_setup = True
        else:
            print("[*] SETUP skipped (--no-setup).")

        print(f"\n[*] Polling 0xc2 every {args.interval:.2f}s for {args.seconds:.0f}s.")
        print("    PRESS THE SCAN BUTTON a few times, with pauses. Then place a")
        print("    sheet on the mat WITHOUT pressing, to tell the two apart.\n")

        t0 = time.time()
        prev = None
        n = 0
        events = []
        bad = 0
        while time.time() - t0 < args.seconds:
            data, st = poll(dev)
            t = time.time() - t0
            if data is None:
                bad += 1
                if bad > 10:
                    print("[!] too many USB errors — giving up.")
                    break
                time.sleep(args.interval)
                continue
            n += 1
            if st != STATUS_GOOD:
                print(f"  t={t:6.2f}  SCSI status 0x{st:02x}")
            if prev is not None and data != prev:
                where = diff(prev, data)
                print(f"  t={t:6.2f}  CHANGE  {', '.join(where)}")
                print(f"            {data.hex()}")
                events.append((t, where, data))
            elif prev is None:
                tag = "  (matches the captures' idle value)" if data == IDLE else ""
                print(f"  t={t:6.2f}  initial {data.hex()}{tag}")
            prev = data
            time.sleep(args.interval)

        print(f"\n[*] {n} polls, {len(events)} change(s), {bad} USB error(s).")
        if not events:
            print("    Payload never changed. Either the button is not reported")
            print("    here, or nothing was pressed. Try --no-setup / with setup,")
            print("    whichever you have not run yet.")
        else:
            byte4 = [e for e in events if any("byte[4]" in w for w in e[1])]
            print(f"    byte[4] changed in {len(byte4)} of {len(events)} events.")
            if byte4 and len(byte4) == len(events):
                print("    -> consistent with byte[4] being the button flag.")
            elif events:
                other = sorted({w.split()[0] for e in events for w in e[1]})
                print(f"    -> bytes involved: {', '.join(other)}")
    finally:
        try:
            if did_setup:
                print("[*] teardown ...")
                run_seq(dev, TEARDOWN, "teardown", verbose=False)
        except Exception as e:
            print(f"[!] teardown failed: {e}")
        try:
            usb.util.release_interface(dev, 0)
        except Exception:
            pass


if __name__ == "__main__":
    main()
