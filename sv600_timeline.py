#!/usr/bin/env python3
"""
sv600_timeline.py — chronological log of every SV600 response that CHANGES.

Point it at a usbmon pcapng and it walks the bulk stream, pairs each 31-byte
command wrapper with the data-in that follows it, and prints a timeline of the
moments a response differed from the previous response to the SAME opcode.

This is how the scan button was found: op 0xc2 (GET DATA BUFFER STATUS) had been
written off as a constant idle heartbeat, but its byte[4] flips 0x00 -> 0x01 a
couple of seconds before a button-started scan and never does so in a session
whose scan was started from the GUI. It deliberately reports EVERY opcode rather
than just 0xc2, because if the button is not in 0xc2 it is most likely in 0x1c
(RECEIVE DIAG), which had 11 distinct responses in the existing captures.

Usage:
    ./sv600_timeline.py capture.pcapng
    ./sv600_timeline.py capture.pcapng --op c2        # one opcode only
    ./sv600_timeline.py capture.pcapng --all          # include first-sightings
    ./sv600_timeline.py capture.pcapng --max-len 64   # ignore bulk image reads

Requires tshark (apt install tshark).
"""
import argparse
import collections
import shutil
import subprocess
import sys

# Opcodes seen in the captures, for readability.
OPS = {
    0x00: "TEST UNIT READY", 0x03: "REQUEST SENSE", 0x12: "INQUIRY",
    0x15: "MODE SELECT", 0x1a: "MODE SENSE", 0x1b: "SCAN",
    0x1c: "RECEIVE DIAG", 0x1d: "SEND DIAG (ASCII cmd)", 0x24: "SET WINDOW",
    0x28: "READ(10)", 0x2a: "WRITE(10)", 0xc2: "GET DATA BUFFER STATUS",
    0xf1: "vendor: scan-complete poll",
}


def bulk_stream(path):
    """Yield (time, endpoint, hexdata) for every bulk transfer with data."""
    if not shutil.which("tshark"):
        sys.exit("tshark not found. Install it:  sudo apt install tshark")
    proc = subprocess.run(
        ["tshark", "-r", path,
         "-Y", "usb.transfer_type==0x03 && "
               "(usb.endpoint_address==0x02 || usb.endpoint_address==0x81)",
         "-T", "fields", "-e", "frame.time_relative",
         "-e", "usb.endpoint_address", "-e", "usb.capdata"],
        capture_output=True, text=True)
    if proc.returncode != 0:
        sys.exit(f"tshark failed: {proc.stderr.strip()}")
    for line in proc.stdout.splitlines():
        f = line.split("\t")
        if len(f) < 3 or not f[2]:
            continue
        yield float(f[0]), f[1], f[2].replace(":", "")


def diff(a, b):
    """Byte-level differences between two hex payloads."""
    A, B = bytes.fromhex(a), bytes.fromhex(b)
    if len(A) != len(B):
        return [f"length {len(A)}->{len(B)}"]
    return [f"byte[{i}] 0x{x:02x}->0x{y:02x}"
            for i, (x, y) in enumerate(zip(A, B)) if x != y]


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("capture", help="usbmon pcapng from usbcap.sh")
    ap.add_argument("--op", help="only this opcode, e.g. c2")
    ap.add_argument("--all", action="store_true",
                    help="also print the first response seen for each opcode")
    ap.add_argument("--max-len", type=int, default=256,
                    help="ignore responses longer than this many bytes "
                         "(default 256: drops bulk image reads)")
    args = ap.parse_args()

    only = int(args.op, 16) if args.op else None

    last_cmd = None
    prev = {}                      # opcode -> previous response hex
    counts = collections.Counter()
    events = []

    for t, ep, d in bulk_stream(args.capture):
        if ep == "0x02":
            # A command wrapper is 31 bytes starting 0x43, CDB at byte 19.
            if len(d) == 62 and d[:2] == "43":
                last_cmd = int(d[38:40], 16)
            continue
        if last_cmd is None:
            continue
        if only is not None and last_cmd != only:
            continue
        if len(d) // 2 > args.max_len:
            continue
        # Skip the bare 13-byte 0x53 status block; it is reported by scsi status
        # elsewhere and would drown the interesting payloads.
        if d[:2] == "53" and len(d) // 2 == 13:
            continue
        counts[last_cmd] += 1
        p = prev.get(last_cmd)
        if p is None:
            if args.all:
                events.append((t, last_cmd, "first", d, []))
        elif p != d:
            events.append((t, last_cmd, "change", d, diff(p, d)))
        prev[last_cmd] = d

    if not events:
        print("No response changes found. "
              "(Try --all, or raise --max-len, or check the capture is not empty.)")
        return

    print(f"{'time':>9}  {'op':>4}  {'':<26} what")
    print("-" * 78)
    for t, op, kind, d, where in events:
        name = OPS.get(op, "?")
        print(f"{t:9.2f}  0x{op:02x}  {name:<26} {kind}")
        if where:
            print(f"{'':>9}  {'':>4}  {'':<26} {', '.join(where)}")
        print(f"{'':>9}  {'':>4}  {'':<26} {d}")

    print("\nresponses seen per opcode:")
    for op, n in sorted(counts.items()):
        print(f"   0x{op:02x} {OPS.get(op, '?'):<28} {n}")


if __name__ == "__main__":
    main()
