#!/usr/bin/env python3
"""
sv600_probe.py — READ-ONLY diagnostic for the SV600 scan-ready handshake.

Why this exists: sv600_scan.py's wait_ready() tests byte 8 of what it calls the
"status" for 0x08, but the bytes coming back (00 00 00 80 ...) don't start with
0x53, so it is not the 'S' status block — it's the GET DATA BUFFER STATUS
payload, and byte 8 is the wrong field. Readiness therefore never fires, the
12 s timeout expires, and the scanner gets a READ(10) with nothing staged, which
drops it into the flashing-amber error state.

This probe issues SETUP + SCAN and then polls GET DATA BUFFER STATUS, dumping
BOTH USB reads in full so we can see which field actually reports buffered
bytes. It NEVER issues READ(10), so it cannot wedge the device that way.

    sudo python3 sv600_probe.py [--seconds 90] [--no-scan]

--no-scan polls without starting a scan, to get the idle baseline first.
"""
import sys, time, argparse
import usb.core, usb.util

VID, PID = 0x04c5, 0x128e
EP_OUT, EP_IN = 0x02, 0x81
MPS = 512

# (wrapper, data_out, data_in_len) — data_in_len MUST be read or the bulk-IN
# stream desyncs by one transfer for every command that follows. RECEIVE DIAG
# (0x1c) declares 2 bytes of data-in in its length field; leaving it unread is
# the bug that made every later "status" read return the previous response.
SETUP = [
    ("43000000000000000000000000000000000000151000000c00000000000000", "000000002c06000000000000", 0),
    ("430000000000000000000000000000000000001d0000001000000000000000", "47455420504f574f46462054494d4520", 0),
    ("430000000000000000000000000000000000001c0000000200000000000000", None, 2),
    ("430000000000000000000000000000000000001d0000001100000000000000", "534554205343414e202020202020202000", 0),
    ("430000000000000000000000000000000000001d0000001300000000000000", "534554205343414e204d4f4445202020000000", 0),
    ("430000000000000000000000000000000000001d0000001100000000000000", "534554205343414e202020202020202001", 0),
    ("43000000000000000000000000000000000000151000000c00000000000000", "000000003406000000000000", 0),
    ("430000000000000000000000000000000000001d0000001100000000000000", "4348414e47452049524c45442020202001", 0),
    ("430000000000000000000000000000000000001d0000001100000000000000", "4348414e47452049524c45442020202000", 0),
    ("43000000000000000000000000000000000000151000000c00000000000000", "000000003c06000000c00000", 0),
    ("43000000000000000000000000000000000000240000000000000048000000",
     "00000000000000400000012c012c00000000000000000000571000004534000000050800008000000000000000000000c1800100000000000000000000c000005710000045340000", 0),
]
SCAN_CMD, SCAN_DATA = "430000000000000000000000000000000000001b0000000100000000000000", "00"
POLL_CMD = "43000000000000000000000000000000000000c20000000000000020000000"


def hx(s): return bytes.fromhex(s)


# The 31-byte wrapper embeds a standard SCSI CDB starting at byte 19. That model
# predicts every length field in the captured commands: the 6-byte CDBs carry
# their length at CDB byte 4, the 10-byte ones at CDB bytes 6-8. So we can build
# commands rather than only replay captured ones.
def wrap(cdb):
    b = bytearray(31)
    b[0] = 0x43
    b[19:19 + len(cdb)] = cdb
    return bytes(b).hex()


REQUEST_SENSE = wrap(bytes([0x03, 0, 0, 0, 18, 0]))   # alloc 18 bytes

SENSE_KEYS = {
    0x0: "NO SENSE", 0x1: "RECOVERED ERROR", 0x2: "NOT READY",
    0x3: "MEDIUM ERROR", 0x4: "HARDWARE ERROR", 0x5: "ILLEGAL REQUEST",
    0x6: "UNIT ATTENTION", 0x7: "DATA PROTECT", 0xb: "ABORTED COMMAND",
}


def decode_sense(s):
    if len(s) < 14:
        return f"(short sense, {len(s)} bytes: {s.hex()})"
    key = s[2] & 0x0f
    asc, ascq = s[12], s[13]
    return (f"response=0x{s[0]:02x} key=0x{key:x} ({SENSE_KEYS.get(key, '?')}) "
            f"ASC=0x{asc:02x} ASCQ=0x{ascq:02x}")


def rd(dev, n, timeout=3000):
    """Read up to n bytes, rounded up to whole max-size packets. Returns b'' on
    timeout instead of raising, so a quiet endpoint doesn't abort the probe."""
    req = -(-n // MPS) * MPS
    try:
        return bytes(dev.read(EP_IN, req, timeout=timeout))
    except usb.core.USBError:
        return b""


def cmd(dev, wrapper, data_out=None, read_len=0, timeout=3000):
    dev.write(EP_OUT, hx(wrapper), timeout=5000)
    if data_out:
        dev.write(EP_OUT, hx(data_out), timeout=5000)
    first = rd(dev, read_len, timeout) if read_len else b""
    second = rd(dev, 13, timeout)
    return first, second


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=90.0)
    ap.add_argument("--interval", type=float, default=1.0)
    ap.add_argument("--no-scan", action="store_true",
                    help="poll without sending SCAN (idle baseline)")
    a = ap.parse_args()

    global MPS
    dev = usb.core.find(idVendor=VID, idProduct=PID)
    if dev is None:
        sys.exit("SV600 (04c5:128e) not found.")
    for cfg in dev:
        for intf in cfg:
            if dev.is_kernel_driver_active(intf.bInterfaceNumber):
                try: dev.detach_kernel_driver(intf.bInterfaceNumber)
                except usb.core.USBError: pass
    dev.set_configuration()
    usb.util.claim_interface(dev, 0)
    ep = usb.util.find_descriptor(dev.get_active_configuration()[(0, 0)],
                                  custom_match=lambda e: e.bEndpointAddress == EP_IN)
    if ep is not None:
        MPS = ep.wMaxPacketSize
    print(f"[*] opened, bulk-IN MPS={MPS}")

    try:
        print("[*] baseline poll BEFORE setup:")
        d1, d2 = cmd(dev, POLL_CMD, read_len=32)
        print(f"    read1[{len(d1):3d}] {d1.hex()}")
        print(f"    read2[{len(d2):3d}] {d2.hex()}")

        print("[*] running setup (checking each status block for 0x53) ...")
        desync = False
        for i, (w, d, rl) in enumerate(SETUP):
            din, st = cmd(dev, w, d, read_len=rl)
            op = w[38:40]
            ok = st[:1] == b'\x53'
            note = "" if ok else "   <-- NOT 0x53: STREAM DESYNCED HERE"
            if not ok:
                desync = True
            extra = f" data_in[{len(din)}]={din.hex()}" if rl else ""
            print(f"    [{i:2d}] op=0x{op} st[{len(st):2d}]={st.hex()[:26]}{extra}{note}")
        if desync:
            print("[!] setup desynced the bulk-IN stream — readings below are unreliable.")
        else:
            print("[*] setup completed with the stream in sync.")

        print("[*] poll AFTER setup, BEFORE scan:")
        d1, d2 = cmd(dev, POLL_CMD, read_len=32)
        print(f"    read1[{len(d1):3d}] {d1.hex()}")
        print(f"    read2[{len(d2):3d}] {d2.hex()}")

        if not a.no_scan:
            print("[*] sending SCAN ...")
            s1, s2 = cmd(dev, SCAN_CMD, SCAN_DATA)
            print(f"    scan read1[{len(s1):3d}] {s1.hex()}")
            print(f"    scan read2[{len(s2):3d}] {s2.hex()}")
            # byte 9 of the 'S' block is the SCSI status; 0x02 = CHECK CONDITION
            if len(s2) > 9 and s2[9] != 0x00:
                print(f"[!] SCAN returned SCSI status 0x{s2[9]:02x}"
                      f"{' (CHECK CONDITION)' if s2[9] == 0x02 else ''}"
                      " — asking why via REQUEST SENSE:")
                d1, d2 = cmd(dev, REQUEST_SENSE, read_len=18)
                print(f"    sense data[{len(d1):3d}] {d1.hex()}")
                print(f"    sense status[{len(d2):3d}] {d2.hex()}")
                print(f"    -> {decode_sense(d1)}")

        print(f"[*] polling for {a.seconds:.0f}s — NO READ(10) will be issued.")
        print("    t       read1 (buffer-status payload)                    read2")
        t0 = time.time()
        prev = None
        while time.time() - t0 < a.seconds:
            d1, d2 = cmd(dev, POLL_CMD, read_len=32)
            line = f"  {time.time()-t0:6.1f}  [{len(d1):3d}] {d1.hex()[:56]:<56} [{len(d2):3d}] {d2.hex()[:26]}"
            cur = (d1, d2)
            mark = "" if cur == prev else "   <-- CHANGED"
            print(line + mark)
            prev = cur
            time.sleep(a.interval)
    finally:
        try: usb.util.release_interface(dev, 0)
        except Exception: pass
        print("[*] interface released. No READ(10) was sent.")


if __name__ == "__main__":
    main()
