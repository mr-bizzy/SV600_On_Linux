#!/usr/bin/env python3
"""
sv600_scan.py — native Linux scanner for the Fujitsu ScanSnap SV600.

Reverse-engineered from a USB capture of the official ScanSnap software. The
SV600 speaks a Fujitsu SCSI command set wrapped in a 0x43 ('C') transport header
on the scanner USB device (04c5:128e):

  * bulk OUT 0x02 : 31-byte command wrapper = 0x43, 18x 0x00, [SCSI opcode],
                    4-byte field, 4-byte big-endian transfer length, pad.
                    (SET WINDOW / MODE SELECT / ASCII commands add a data-out phase.)
  * bulk IN  0x81 : data-in phase, then a 13-byte 0x53 ('S') status block
                    ( ...00 = OK/done, ...08 = data ready ).

The image is PLAIN INTERLEAVED RGB, 5572 px wide, contiguous rows, uncompressed.

Deps:  python3-usb python3-numpy python3-pil  (apt), libusb-1.0
Run as root (USB claim), scanner attached to THIS machine (not a VM):
    sudo python3 sv600_scan.py out.png
"""
import sys, os, json, time, argparse
import usb.core, usb.util
import numpy as np

import sv600_output

VID, PID   = 0x04c5, 0x128e
EP_OUT     = 0x02
EP_IN      = 0x81
WIDTH_PX   = 5572          # SET WINDOW: 22288/1200in * 300dpi
HEIGHT_PX  = 4429          # SET WINDOW: 17716/1200in * 300dpi
CHANNELS   = 3             # interleaved RGB
READ_LEN   = 0x07659c      # 484764 bytes per READ(10), as the official app uses
IMG_BYTES  = WIDTH_PX * HEIGHT_PX * CHANNELS
STATUS_LEN = 13
MPS        = 512           # bulk-IN max packet size; refreshed from the endpoint

def hx(s): return bytes.fromhex(s)


def wrap(cdb):
    """Build the 31-byte transport wrapper. It embeds a STANDARD SCSI CDB at
    byte 19 — verified by reconstructing every command in the USB capture
    byte-for-byte from this rule."""
    b = bytearray(31)
    b[0] = 0x43
    b[19:19 + len(cdb)] = bytes(cdb)
    return bytes(b).hex()


# The 266-byte payload WRITE(10) uploads: a 10-byte header plus a 256-entry
# identity ramp — a tone/gamma LUT. Omitting it makes SCAN fail with CHECK
# CONDITION, which is what stalled this driver for a long time.
LUT_DATA = "00001000010001000000" + "".join(f"{i:02x}" for i in range(256))

# ---- command sequence, transcribed from a usbmon capture of the official
#      software (frames 893-985). Each entry is (cdb, data_out, data_in_len).
#      data_in_len MUST be correct: an unread data-in phase desyncs the bulk-IN
#      stream by one transfer for every command that follows it.
SETUP = [
    (bytes([0x15, 0x10, 0, 0, 0x0c, 0]), "000000002c06000000000000", 0),           # MODE SELECT
    (bytes([0x1d, 0, 0, 0, 0x10, 0]), "47455420504f574f46462054494d4520", 0),      # "GET POWOFF TIME "
    (bytes([0x1c, 0, 0, 0, 0x02, 0]), None, 2),                                    # RECEIVE DIAG -> 2 bytes
    (bytes([0x1d, 0, 0, 0, 0x11, 0]), "534554205343414e202020202020202000", 0),    # "SET SCAN" 00
    (bytes([0x1d, 0, 0, 0, 0x13, 0]), "534554205343414e204d4f4445202020000000", 0),# "SET SCAN MODE"
    (bytes([0x1d, 0, 0, 0, 0x11, 0]), "534554205343414e202020202020202001", 0),    # "SET SCAN" 01
    (bytes([0x15, 0x10, 0, 0, 0x0c, 0]), "000000003406000000000000", 0),           # MODE SELECT
    (bytes([0x1d, 0, 0, 0, 0x11, 0]), "4348414e47452049524c45442020202001", 0),    # "CHANGE IRLED" 01
    (bytes([0x1d, 0, 0, 0, 0x11, 0]), "4348414e47452049524c45442020202000", 0),    # "CHANGE IRLED" 00
    (bytes([0x2a, 0, 0x83, 0, 0, 0, 0, 0x01, 0x0a, 0]), LUT_DATA, 0),              # WRITE(10): 266-byte LUT
    (bytes([0x15, 0x10, 0, 0, 0x0c, 0]), "000000003c06000000c00000", 0),           # MODE SELECT
    (bytes([0x24, 0, 0, 0, 0, 0, 0, 0, 0x48, 0]),                                  # SET WINDOW 300dpi 5572x4429
     "00000000000000400000012c012c00000000000000000000571000004534000000050800008000000000000000000000c1800100000000000000000000c000005710000045340000", 0),
]

# Return the scanner to idle after a scan (capture frames 4399-5083). Skipping
# this appears to leave the device latched in its flashing-amber error state.
TEARDOWN = [
    (bytes([0x03, 0, 0, 0, 18, 0]), None, 18),                                     # REQUEST SENSE
    (bytes([0x28, 0, 0x80, 0, 0, 0, 0, 0, 0x18, 0]), None, 24),                    # dimension readback
    (bytes([0x1d, 0, 0, 0, 0x10, 0]), "474554204c414d50204f46462054494d", 0),      # "GET LAMP OFF TIM"
    (bytes([0x1c, 0, 0, 0, 0x06, 0]), None, 6),                                    # RECEIVE DIAG -> 6 bytes
    (bytes([0x1d, 0, 0, 0, 0x11, 0]), "4348414e47452049524c45442020202001", 0),    # "CHANGE IRLED" 01
    (bytes([0x1d, 0, 0, 0, 0x11, 0]), "534554205343414e202020202020202002", 0),    # "SET SCAN" 02
    (bytes([0x15, 0x10, 0, 0, 0x0c, 0]), "000000002c06010000000000", 0),           # MODE SELECT
    (bytes([0x1d, 0, 0, 0, 0x10, 0]), "454e442057414954494e47205343414e", 0),      # "END WAITING SCAN"
]

TUR_CMD   = wrap(bytes([0x00, 0, 0, 0, 0, 0]))          # TEST UNIT READY
REQ_SENSE_CMD = wrap(bytes([0x03, 0, 0, 0, 18, 0]))
SCAN_CMD  = wrap(bytes([0x1b, 0, 0, 0, 0x01, 0]))
SCAN_DATA = "00"
READ_CMD  = wrap(bytes([0x28, 0, 0, 0, 0, 0,
                        (READ_LEN >> 16) & 0xff, (READ_LEN >> 8) & 0xff, READ_LEN & 0xff, 0]))
# Scan-complete poll. NOT the 0xc2 buffer-status command the old code used —
# that payload never changes. This returns SCSI BUSY (0x08) while the scan runs
# and GOOD (0x00) when the data is ready.
BUSY_CMD  = wrap(bytes([0xf1, 0x10, 0, 0, 0, 0,
                        (READ_LEN >> 16) & 0xff, (READ_LEN >> 8) & 0xff, READ_LEN & 0xff, 0]))

STATUS_GOOD, STATUS_CHECK, STATUS_BUSY = 0x00, 0x02, 0x08

# ---- USB plumbing -----------------------------------------------------------
_BUSY_MSG = """SV600 is busy — another process still has the USB interface claimed.

Check who has it:
    sudo lsof /dev/bus/usb/$(lsusb -d 04c5:128e | awk '{print $2"/"substr($4,1,3)}')
Look for a leftover sv600_scan.py / sv600_button.py, or a SANE process
(saned, scanimage, simple-scan). Kill it, then retry.

If nothing holds it, the claim is stale — unplug the scanner's USB cable and
plug it back in. (`sudo usbreset 04c5:128e` does the same without reaching
behind the desk, if usbutils' usbreset is installed.)"""


def open_dev():
    global MPS
    dev = usb.core.find(idVendor=VID, idProduct=PID)
    if dev is None:
        sys.exit("SV600 (04c5:128e) not found. Attach it to THIS machine (not the VM).")
    for cfg in dev:
        for intf in cfg:
            if dev.is_kernel_driver_active(intf.bInterfaceNumber):
                try: dev.detach_kernel_driver(intf.bInterfaceNumber)
                except usb.core.USBError: pass
    # Only configure if it is not ALREADY configured. set_configuration() forces
    # a re-configuration, which the kernel refuses with EBUSY if any interface
    # is claimed — including by a libusb process (those show as Driver=usbfs and
    # do NOT answer is_kernel_driver_active, so the detach loop above misses
    # them). The device powers up in configuration 1 and stays there, so the
    # call is normally redundant; skipping it makes re-opening safe.
    try:
        active = dev.get_active_configuration()
    except usb.core.USBError:
        active = None
    if active is None or active.bConfigurationValue != 1:
        try:
            dev.set_configuration()
        except usb.core.USBError as e:
            if e.errno == 16:
                sys.exit(_BUSY_MSG)
            raise
    try:
        usb.util.claim_interface(dev, 0)
    except usb.core.USBError as e:
        if e.errno == 16:
            sys.exit(_BUSY_MSG)
        raise
    intf = dev.get_active_configuration()[(0, 0)]
    ep = usb.util.find_descriptor(intf, custom_match=lambda e: e.bEndpointAddress == EP_IN)
    if ep is not None:
        MPS = ep.wMaxPacketSize
    print(f"[*] SV600 opened (bulk-IN max packet = {MPS}).")
    return dev

def rd(dev, n, timeout=10000):
    """Read >= n bytes, but ALWAYS request a whole number of max-size packets so
    the device can never overflow the buffer (libusb Errno 75)."""
    req = -(-n // MPS) * MPS
    return bytes(dev.read(EP_IN, req, timeout=timeout))

def cmd(dev, wrapper_hex, data_out_hex=None, read_len=0, timeout=10000):
    """OUT wrapper [+ data-out], IN [data-in], IN status.

    read_len must match the CDB's declared transfer length. If a data-in phase
    is left unread the bulk-IN stream desyncs by one transfer and every later
    'status' is actually the previous command's response."""
    dev.write(EP_OUT, hx(wrapper_hex), timeout=5000)
    if data_out_hex:
        dev.write(EP_OUT, hx(data_out_hex), timeout=5000)
    data = rd(dev, read_len, timeout) if read_len else b""
    status = rd(dev, STATUS_LEN, timeout)
    return data, status[:STATUS_LEN]


def wake(dev, attempts=10, verbose=True):
    """TEST UNIT READY until the scanner answers, clearing any latched sense.

    The official driver opens every session with TEST UNIT READY, and the
    protocol has both a power-off and a lamp-off timer ('GET POWOFF TIME',
    'GET LAMP OFF TIM'), so an idle scanner can be asleep when we arrive. Going
    straight into MODE SELECT (as this script used to) just times out."""
    for i in range(attempts):
        try:
            _, st = cmd(dev, TUR_CMD, timeout=1500)
        except usb.core.USBError:
            # A timed-out endpoint can be left halted; clear it and retry.
            for ep in (EP_IN, EP_OUT):
                try: dev.clear_halt(ep)
                except Exception: pass
            if verbose and i == 0:
                print("    scanner not answering — retrying (it may be asleep) ...")
            time.sleep(0.4)
            continue
        if st[:1] != b'\x53':
            time.sleep(0.2)
            continue
        s = scsi_status(st)
        if s == STATUS_GOOD:
            if verbose and i:
                print(f"    awake after {i+1} attempts")
            return True
        if s == STATUS_CHECK:
            # Latched condition (e.g. power-on UNIT ATTENTION): read it to clear.
            try:
                cmd(dev, REQ_SENSE_CMD, read_len=18, timeout=1500)
            except usb.core.USBError:
                pass
            continue
        time.sleep(0.2)
    return False


def scsi_status(st):
    """Byte 9 of the 13-byte 0x53 block is the SCSI status byte."""
    return st[9] if len(st) > 9 else None


def check(st, what):
    """Raise on a desynced stream or a non-GOOD SCSI status, instead of
    ploughing on — a READ(10) issued after a failed SCAN is what used to drop
    the scanner into its flashing-amber error state."""
    if st[:1] != b'\x53':
        raise RuntimeError(f"{what}: bulk-IN desynced (status={st.hex()})")
    s = scsi_status(st)
    if s != STATUS_GOOD:
        name = {STATUS_CHECK: "CHECK CONDITION", STATUS_BUSY: "BUSY"}.get(s, "?")
        raise RuntimeError(f"{what}: SCSI status 0x{s:02x} ({name})")


SENSE_KEYS = {0x0: "NO SENSE", 0x1: "RECOVERED", 0x2: "NOT READY",
              0x3: "MEDIUM ERROR", 0x4: "HARDWARE ERROR", 0x5: "ILLEGAL REQUEST",
              0x6: "UNIT ATTENTION", 0x7: "DATA PROTECT", 0xb: "ABORTED"}
# Keys worth retrying: the scanner is coming up, not refusing.
TRANSIENT = (0x0, 0x2, 0x6)


def request_sense(dev):
    try:
        data, _ = cmd(dev, REQ_SENSE_CMD, read_len=18, timeout=3000)
        return data
    except usb.core.USBError:
        return b""


def sense_str(s):
    if len(s) < 14:
        return f"(short sense: {s.hex()})"
    key = s[2] & 0x0f
    return (f"key=0x{key:x} ({SENSE_KEYS.get(key, '?')}) "
            f"ASC=0x{s[12]:02x} ASCQ=0x{s[13]:02x}")


def run_seq(dev, seq, label, verbose=True, retries=4):
    """Run a command sequence, recovering from transient CHECK CONDITIONs.

    A cold scanner can report GOOD to TEST UNIT READY while its lamp subsystem
    is still coming up, so e.g. CHANGE IRLED returns CHECK CONDITION on the
    first attempt and succeeds moments later. Treating that as fatal made the
    first scan after an idle period fail while an immediate retry worked.
    So: CHECK CONDITION -> REQUEST SENSE -> retry if the key says 'not ready
    yet', otherwise fail with the decoded sense."""
    for i, (cdb, dout, dinlen) in enumerate(seq):
        what = f"{label}[{i}] op=0x{cdb[0]:02x}"
        for attempt in range(retries):
            last = attempt == retries - 1
            try:
                _, st = cmd(dev, wrap(cdb), dout, read_len=dinlen)
            except usb.core.USBError as e:
                # A cold scanner can drop a transfer outright rather than
                # answering CHECK CONDITION. Clear any halted endpoint and
                # retry — one dropped transfer used to abort the whole scan.
                if last:
                    raise RuntimeError(f"{what}: USB error after {retries} "
                                       f"attempts ({e})")
                if verbose:
                    print(f"    {what}: USB {e.__class__.__name__} — retrying "
                          f"({attempt + 1}/{retries - 1})")
                for ep in (EP_IN, EP_OUT):
                    try: dev.clear_halt(ep)
                    except Exception: pass
                time.sleep(0.5)
                continue
            if st[:1] != b'\x53':
                raise RuntimeError(f"{what}: bulk-IN desynced (status={st.hex()})")
            s = scsi_status(st)
            if s == STATUS_GOOD:
                break
            if s == STATUS_CHECK:
                sense = request_sense(dev)
                key = (sense[2] & 0x0f) if len(sense) >= 14 else None
                if key in TRANSIENT and not last:
                    if verbose:
                        print(f"    {what}: {sense_str(sense)} — retrying "
                              f"({attempt + 1}/{retries - 1})")
                    time.sleep(0.5)
                    continue
                raise RuntimeError(f"{what}: CHECK CONDITION, {sense_str(sense)}")
            if s == STATUS_BUSY and not last:
                time.sleep(0.5)
                continue
            raise RuntimeError(f"{what}: SCSI status 0x{s:02x}")
        else:
            raise RuntimeError(f"{what}: still failing after {retries} attempts")


# ---- scan flow --------------------------------------------------------------
def run_setup(dev):
    print("[*] Waking scanner ...")
    if not wake(dev):
        raise RuntimeError(
            "scanner did not respond to TEST UNIT READY. It may be asleep or "
            "wedged — power it off, unplug USB, wait ~30s, and reconnect.")
    print("[*] Running setup + LUT + SET WINDOW ...")
    run_seq(dev, SETUP, "setup")


def wait_ready(dev, timeout=30.0, verbose=True):
    """Send SCAN, then poll the 0xf1 command until it stops reporting BUSY.

    Aborts rather than proceeding on timeout: reading from a scanner that never
    staged data is what wedged the hardware previously."""
    print("[*] SCAN sent — waiting for lamp + capture ...")
    _, st = cmd(dev, SCAN_CMD, SCAN_DATA)
    check(st, "SCAN")
    t0 = time.time()
    while time.time() - t0 < timeout:
        _, st = cmd(dev, BUSY_CMD)
        if st[:1] != b'\x53':
            raise RuntimeError(f"scan poll: bulk-IN desynced (status={st.hex()})")
        s = scsi_status(st)
        if s == STATUS_GOOD:
            print(f"    ready after {time.time()-t0:.1f}s")
            return True
        if s != STATUS_BUSY:
            raise RuntimeError(f"scan poll: unexpected SCSI status 0x{s:02x}")
        time.sleep(0.05)
    raise RuntimeError(f"scan did not complete within {timeout:.0f}s — aborting "
                       "rather than reading from an unready scanner")


def read_image(dev, verbose=True):
    """Read the image in fixed-size chunks.

    The device pads the final READ(10) to a full chunk; the official software
    learns the residual from REQUEST SENSE's ILI information field. We simply
    truncate to IMG_BYTES, which the capture confirms is exact:
    152*484764 + (484764-133728) == 5572*4429*3."""
    n_reads = -(-IMG_BYTES // READ_LEN)
    print(f"[*] Reading image ({IMG_BYTES:,} bytes in {n_reads} chunks) ...")
    buf = bytearray()
    for i in range(n_reads):
        data, st = cmd(dev, READ_CMD, read_len=READ_LEN)
        if st[:1] != b'\x53':
            raise RuntimeError(f"READ(10) #{i+1}: bulk-IN desynced (status={st.hex()})")
        s = scsi_status(st)
        if s == STATUS_CHECK:
            # Expected on the LAST read: the device pads the chunk to full size
            # and flags ILI. The official software does exactly this — asks for
            # the residual and keeps only the valid prefix.
            sense, _ = cmd(dev, REQ_SENSE_CMD, read_len=18)
            key, ili = sense[2] & 0x0f, bool(sense[2] & 0x20)
            residual = int.from_bytes(sense[3:7], "big")
            if not (ili and key == 0x00):
                raise RuntimeError(
                    f"READ(10) #{i+1}: CHECK CONDITION, sense key=0x{key:x} "
                    f"ASC=0x{sense[12]:02x} ASCQ=0x{sense[13]:02x}")
            valid = READ_LEN - residual
            buf += data[:valid]
            if verbose:
                print(f"\n    final chunk short by {residual:,} B "
                      f"(ILI) -> {valid:,} B valid")
            break
        if s != STATUS_GOOD:
            raise RuntimeError(f"READ(10) #{i+1}: SCSI status 0x{s:02x}")
        buf += data
        print(f"    {min(len(buf), IMG_BYTES):,}/{IMG_BYTES:,}", end="\r")
    print()
    if len(buf) != IMG_BYTES:
        print(f"[!] Got {len(buf):,} bytes, expected {IMG_BYTES:,}.")
    return buf[:IMG_BYTES]


def teardown(dev, verbose=True):
    """Return the scanner to idle. Best-effort: never mask the original error."""
    try:
        run_seq(dev, TEARDOWN, "teardown")
        if verbose:
            print("[*] Scanner returned to idle.")
    except Exception as e:
        print(f"[!] Teardown failed (scanner may need a power cycle): {e}")

# ---- image post-processing --------------------------------------------------
def to_rgb(buf):
    row  = WIDTH_PX * CHANNELS
    rows = min(HEIGHT_PX, len(buf) // row)
    if rows == 0:
        sys.exit("[!] No complete image rows captured — nothing to save.")
    if rows < HEIGHT_PX:
        print(f"[!] Short scan: {rows} of {HEIGHT_PX} rows.")
    return np.frombuffer(bytes(buf[:rows * row]), np.uint8).reshape(rows, WIDTH_PX, CHANNELS)

def color_correct(img):
    f = img.astype(np.float32)
    for c in range(3):                      # gray-world white balance
        m = f[..., c].mean()
        if m > 1: f[..., c] *= (f.mean() / m)
    f = np.clip(f, 0, 255) / 255.0
    return (np.power(f, 1/2.2) * 255).astype(np.uint8)   # gamma

def _close_gaps(mask, max_gap):
    """Fill runs of False shorter than max_gap that are flanked by True.

    Without this, any dark band spanning the page — a table, a photo, the test
    chart's tone wedge — splits the bright run in two and the page gets cropped
    at the band instead of at its edge."""
    m = mask.copy()
    n = len(m)
    i = 0
    while i < n:
        if m[i]:
            i += 1
            continue
        j = i
        while j < n and not m[j]:
            j += 1
        if i > 0 and j < n and (j - i) <= max_gap:
            m[i:j] = True
        i = j
    return m


def _largest_run(mask, max_gap=0):
    """Start/end (inclusive) of the longest contiguous True run in a 1-D mask,
    tolerating interior gaps up to max_gap samples."""
    if max_gap:
        mask = _close_gaps(mask, max_gap)
    best = (0, -1); start = None; best_len = 0
    for i, v in enumerate(mask):
        if v:
            if start is None: start = i
            if i - start + 1 > best_len:
                best_len = i - start + 1; best = (start, i)
        else:
            start = None
    return best

def autocrop(img8, margin=24, verbose=True):
    """Crop to the LARGEST contiguous bright block (the page). Using the largest
    contiguous run — not first-to-last bright — makes this robust to wrapped edge
    slivers or specks at the frame boundary that would otherwise inflate the box."""
    col = img8.mean(axis=(0, 2))
    row = img8.mean(axis=(1, 2))
    cthr = (col.min() + col.max()) / 2
    rthr = (row.min() + row.max()) / 2
    H, W = img8.shape[:2]
    c0, c1 = _largest_run(col > cthr, max_gap=int(0.12 * W))
    r0, r1 = _largest_run(row > rthr, max_gap=int(0.12 * H))
    if c1 - c0 < W * 0.1 or r1 - r0 < H * 0.1:
        if verbose: print("[*] autocrop: page not confidently found — skipping.")
        return img8
    r0, r1 = max(0, r0 - margin), min(H, r1 + margin)
    c0, c1 = max(0, c0 - margin), min(W, c1 + margin)
    if verbose: print(f"[*] autocrop: page rows {r0}-{r1}, cols {c0}-{c1}")
    return img8[r0:r1, c0:c1]

# Lateral chromatic aberration of the SV600's lens. Fitted from 445 vertical +
# 255 horizontal high-contrast edges spread across a chart scan: R and B are
# displaced in opposite directions proportionally to distance from an optical
# centre, with G as the reference (the fitted R/B slope ratio came out -1.02 and
# -0.93, i.e. near-perfectly antisymmetric, which is the signature of lateral CA).
# Verified visually, not just numerically: a grid dot goes from clearly
# orange/blue fringed to neutral, and |R-G| on edge pixels falls 29.0 -> 8.3.
# Quadratic in each axis (coefficients highest-order first), fitted over 1254
# vertical-edge and 781 horizontal-edge samples spanning the full frame.
# The vertical term MUST be quadratic: the offset runs +1.0 px at the top of the
# frame to +12.2 px at the bottom, and a linear fit (residual 1.17 vs 0.82)
# under-corrects the bottom badly — which is exactly where the fringing shows.
# An earlier linear fit was also biased because its samples clustered in the
# upper frame, and a +-9 px reject silently discarded the large-offset samples
# at the bottom, hiding the acceleration.
CA = {0: dict(px=[3.250266e-08, 1.262629e-03, -3.720155],
              py=[7.630474e-07, 1.198451e-04, +0.784629]),
      2: dict(px=[-2.480958e-08, -1.257459e-03, +3.655143],
              py=[-6.947463e-07, -2.924435e-04, -0.717101])}
CA_MAX = 20.0          # px; guard against extrapolation running away


def correct_ca(img8, verbose=True):
    """Resample R and B onto G to remove colour fringing. Operates on the RAW
    frame — CA is an optical effect in sensor coordinates, so it must be undone
    before any geometric rectification."""
    src = img8.astype(np.float32)
    out = src.copy()
    h, w, _ = src.shape
    X = np.arange(w, dtype=np.float32)
    Y = np.arange(h, dtype=np.float32)
    for c, p in CA.items():
        dx = np.clip(np.polyval(p["px"], X), -CA_MAX, CA_MAX)
        dy = np.clip(np.polyval(p["py"], Y), -CA_MAX, CA_MAX)
        xs = np.clip(X + dx, 0, w - 1.001)
        ys = np.clip(Y + dy, 0, h - 1.001)
        xi = xs.astype(np.int32); fx = (xs - xi)[None, :]
        yi = ys.astype(np.int32); fy = (ys - yi)[:, None]
        ch = src[:, :, c]
        x1 = np.minimum(xi + 1, w - 1); y1 = np.minimum(yi + 1, h - 1)
        a = ch[np.ix_(yi, xi)]; b = ch[np.ix_(yi, x1)]
        cc = ch[np.ix_(y1, xi)]; dd = ch[np.ix_(y1, x1)]
        out[:, :, c] = (a * (1 - fx) + b * fx) * (1 - fy) + (cc * (1 - fx) + dd * fx) * fy
    if verbose:
        print("[*] chromatic aberration corrected (R/B resampled onto G)")
    return np.clip(out, 0, 255).astype(np.uint8)


def sharpen(img8, amount=140, radius=1.6, threshold=3, verbose=True):
    """Unsharp mask.

    Needed because rectification upscales the far end of the page: the SV600's
    perspective samples the top edge at ~11.2 px/mm but the bottom at ~8.4
    (ratio 1.33), so resampling to a uniform scale stretches the bottom ~1.4x.
    The official software does the same thing — its output shows no sharpness
    falloff top-to-bottom despite working from identical raw data."""
    from PIL import Image, ImageFilter
    o = Image.fromarray(img8, "RGB").filter(
        ImageFilter.UnsharpMask(radius=radius, percent=int(amount), threshold=int(threshold)))
    if verbose:
        print(f"[*] sharpened (unsharp mask r={radius} amount={amount}%)")
    return np.asarray(o)


def _chown_back(path):
    """Hand a file written under sudo back to the invoking user."""
    uid, gid = os.environ.get("SUDO_UID"), os.environ.get("SUDO_GID")
    if uid and gid:
        try:
            os.chown(path, int(uid), int(gid))
        except OSError:
            pass


def load_calibration(path=None, verbose=True):
    """Load the fixed image->mm mapping produced by sv600_calibrate.py."""
    if path is None:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "sv600-calibration.json")
    if not os.path.exists(path):
        return None
    try:
        c = json.load(open(path))
        if list(c.get("source_size", [])) != [WIDTH_PX, HEIGHT_PX]:
            print(f"[!] {path}: calibrated for {c.get('source_size')}, "
                  f"this scan is {[WIDTH_PX, HEIGHT_PX]} — ignoring.")
            return None
        np.array(c["H_mm_to_px"], float).reshape(3, 3)
        return c
    except Exception as e:
        print(f"[!] {path}: unreadable ({e}) — ignoring.")
        return None


def _top_regions(mask, n=1, ds=8, min_frac=0.25):
    """The n largest 4-connected True regions, each as a full-resolution mask.

    Labels on a downscaled copy (a page is thousands of px across, so an 8x
    reduction costs nothing) and returns the original mask restricted to each
    region's neighbourhood, largest first.

    Regions smaller than min_frac of the largest are dropped. Two sheets on the
    mat are within a few percent of each other in area, while the specks this
    rejects (stray desk highlights, a pen) are an order of magnitude smaller —
    so the cut is nowhere near either population."""
    small = mask[::ds, ::ds]
    h, w = small.shape
    seen = np.zeros((h, w), np.bool_)
    regions = []
    ys, xs = np.nonzero(small)
    for sy, sx in zip(ys, xs):
        if seen[sy, sx]:
            continue
        stack = [(sy, sx)]
        seen[sy, sx] = True
        pts = []
        while stack:
            y, x = stack.pop()
            pts.append((y, x))
            for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                ny, nx = y + dy, x + dx
                if 0 <= ny < h and 0 <= nx < w and small[ny, nx] and not seen[ny, nx]:
                    seen[ny, nx] = True
                    stack.append((ny, nx))
        regions.append(pts)
    if not regions:
        return []
    regions.sort(key=len, reverse=True)
    cut = len(regions[0]) * min_frac
    regions = [r for r in regions[:n] if len(r) >= cut]

    out = []
    for pts in regions:
        keep = np.zeros((h, w), np.bool_)
        for y, x in pts:
            keep[y, x] = True
        # grow by one small-pixel so the upscaled edge doesn't clip the true border
        grown = keep.copy()
        for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            grown |= np.roll(keep, (dy, dx), (0, 1))
        full = np.repeat(np.repeat(grown, ds, 0), ds, 1)[:mask.shape[0], :mask.shape[1]]
        if full.shape != mask.shape:      # pad if the reduction left a remainder
            pad = np.zeros_like(mask)
            pad[:full.shape[0], :full.shape[1]] = full
            full = pad
        out.append(mask & full)
    return out


def _largest_region(mask, ds=8):
    """Largest 4-connected True region, as a full-resolution mask."""
    r = _top_regions(mask, n=1, ds=ds)
    return r[0] if r else None


def _otsu(g):
    """Otsu threshold — used only to identify which pixels are 'paper'."""
    h, _ = np.histogram(g, bins=256, range=(0, 256))
    h = h.astype(np.float64)
    tot = h.sum()
    w0 = np.cumsum(h)
    w1 = tot - w0
    mu = np.cumsum(h * np.arange(256))
    with np.errstate(invalid="ignore", divide="ignore"):
        between = (mu[-1] * w0 / tot - mu) ** 2 / (w0 * w1)
    between[~np.isfinite(between)] = 0
    return int(np.argmax(between))


# Standard paper sizes (mm, portrait). Used to snap the output to a true page.
PAPER = {"a3": (297.0, 420.0), "a4": (210.0, 297.0), "a5": (148.0, 210.0),
         "letter": (215.9, 279.4), "legal": (215.9, 355.6)}


def _page_mask(g):
    """Cleaned bright mask separating sheets from the mat, or None.

    Callers pass the per-pixel MAX of R,G,B rather than the mean. Saturated
    colour has a mediocre mean but a strong max — a dark red logo bleeding off a
    sheet corner measured mean 187 vs max 219, so on the mean it fell under the
    page threshold, the corner was clipped inward, and an A4 letter measured
    197.5mm wide instead of 209.1."""
    o = _otsu(g)
    bright = g[g > o]
    if bright.size < 5000:
        return None
    # Threshold PAGE-vs-BACKGROUND, not paper-white-vs-everything. Keying on
    # bright paper silently excludes dark content that runs to the sheet edge
    # (a logo bleeding off a corner, a photo, a coloured border), so the
    # detected "corners" are not the real ones and the whole warp comes out
    # skewed. The black mat sits at ~15 while even dark print is >25, and the
    # largest-region filter still rejects the desk.
    thr = float(np.clip(0.28 * float(np.median(bright)), 40.0, 90.0))
    m = g > thr
    k = 9
    c = np.cumsum(np.cumsum(m.astype(np.float32), 0), 1)
    c = np.pad(c, ((1, 0), (1, 0)))
    box = (c[k:, k:] - c[:-k, k:] - c[k:, :-k] + c[:-k, :-k]) / (k * k)
    m2 = np.zeros_like(m)
    m2[k // 2:k // 2 + box.shape[0], k // 2:k // 2 + box.shape[1]] = box > 0.6
    return m2


def _quad_of(region):
    """The four corners (TL,TR,BR,BL) of one page region, in image pixels."""
    ys, xs = np.nonzero(region)
    if len(xs) < 5000:
        return None

    def corner(fn):
        i = int(np.argmax(fn(xs, ys)))
        return (float(xs[i]), float(ys[i]))

    return [corner(lambda x, y: -(x + y)), corner(lambda x, y: x - y),
            corner(lambda x, y: x + y), corner(lambda x, y: y - x)]


def _page_quads_px(g, pages=1):
    """Corners of the `pages` largest sheets, ordered left-to-right.

    Two A4 sheets laid side by side on the mat are two separate bright regions,
    so each gets its own quad and its own warp. That is better than treating the
    pair as one A3: a gap between them, or one sheet sitting slightly askew of
    the other, then costs nothing."""
    m2 = _page_mask(g)
    if m2 is None:
        return []
    quads = []
    for region in _top_regions(m2, n=pages):
        q = _quad_of(region)
        if q is not None:
            quads.append(q)
    # Left-to-right by centroid, so page 1 is the left-hand sheet.
    quads.sort(key=lambda q: sum(p[0] for p in q))
    return quads


def _page_quad_px(g):
    """The single largest page's four corners (TL,TR,BR,BL), or None."""
    q = _page_quads_px(g, pages=1)
    return q[0] if q else None


def _snap_paper(w_mm, h_mm, tol=0.04):
    """Nearest standard paper size, if within tol; else None."""
    best, berr = None, tol
    for name, (pw, ph) in PAPER.items():
        for cand in ((pw, ph), (ph, pw)):          # portrait or landscape
            e = max(abs(w_mm - cand[0]) / cand[0], abs(h_mm - cand[1]) / cand[1])
            if e < berr:
                best, berr = (name, cand), e
    return best


def _page_bbox_mm(g, Hi, margin):
    """Bounding box (mm) of the page, via its four corners in image space.

    Deliberately maps only the FOUR CORNERS, not every bright pixel. The inverse
    homography sends pixels near the horizon to enormous mm values, so a
    percentile over millions of points is unstable — a handful of extra bright
    pixels flipped the result from 230 mm wide to 388 mm."""
    # Threshold at a fraction of the PAPER's own brightness, found via Otsu.
    # A fraction of p99 is too low and lets bright patches of desk through, and
    # Otsu itself is lower still; both put the "page corners" out on the desk.
    # This rule gives corners stable to 1-2 px across fractions 0.75-0.85.
    o = _otsu(g)
    bright = g[g > o]
    if bright.size < 5000:
        return None
    # Threshold PAGE-vs-BACKGROUND, not paper-white-vs-everything. Keying on
    # bright paper silently excludes dark content that runs to the sheet edge
    # (a logo bleeding off a corner, a photo, a coloured border), so the
    # detected "corners" are not the real ones and the whole warp comes out
    # skewed. The black mat sits at ~15 while even dark print is >25, and the
    # largest-region filter still rejects the desk.
    thr = float(np.clip(0.28 * float(np.median(bright)), 40.0, 90.0))
    m = g > thr
    k = 9                                   # drop specks (stray desk highlights)
    c = np.cumsum(np.cumsum(m.astype(np.float32), 0), 1)
    c = np.pad(c, ((1, 0), (1, 0)))
    box = (c[k:, k:] - c[:-k, k:] - c[k:, :-k] + c[:-k, :-k]) / (k * k)
    m2 = np.zeros_like(m)
    m2[k // 2:k // 2 + box.shape[0], k // 2:k // 2 + box.shape[1]] = box > 0.6
    # Keep only the LARGEST connected bright region. The scan area is wider than
    # the black background mat, so the desk is visible at the frame corners at
    # ~85-120 vs the page's ~207 — bright enough that a slightly low threshold
    # lets it through. Corners were picked with argmax over single pixels, so
    # ONE stray desk pixel could capture a corner outright. The page is one big
    # blob and the desk patches are separate ones, so this is immune to both
    # that and to specks.
    m3 = _largest_region(m2)
    if m3 is None:
        return None
    ys, xs = np.nonzero(m3)
    if len(xs) < 5000:
        return None

    def corner(fn):
        i = int(np.argmax(fn(xs, ys)))
        return (float(xs[i]), float(ys[i]))

    quad = [corner(lambda x, y: -(x + y)), corner(lambda x, y: x - y),
            corner(lambda x, y: x + y), corner(lambda x, y: y - x)]
    pts = np.array([[q[0], q[1], 1.0] for q in quad]) @ Hi.T
    mm = pts[:, :2] / pts[:, 2:3]
    if not np.all(np.isfinite(mm)):
        return None
    lo = mm.min(0) - margin
    hi = mm.max(0) + margin
    if not (20 < hi[0] - lo[0] < 500 and 20 < hi[1] - lo[1] < 700):
        return None
    return lo, hi


def _poly_order(deg):
    return [(i, j) for i in range(deg + 1) for j in range(deg + 1 - i)]


def _poly_map(coefs, deg, X, Y):
    """Evaluate the fitted 2-D polynomial. Accumulates term by term rather than
    building the full design matrix, which for a full page would be ~700 MB."""
    cx, cy = np.asarray(coefs[0], float), np.asarray(coefs[1], float)
    U = np.zeros_like(X, dtype=np.float32)
    V = np.zeros_like(X, dtype=np.float32)
    for k, (i, j) in enumerate(_poly_order(deg)):
        t = np.ones_like(X, dtype=np.float32)
        for _ in range(i):
            t *= X
        for _ in range(j):
            t *= Y
        U += np.float32(cx[k]) * t
        V += np.float32(cy[k]) * t
    return U, V


def _taper(X, Y, bounds, width=25.0):
    """1 inside `bounds`, falling smoothly to 0 `width` beyond it."""
    x0, y0, x1, y1 = bounds
    dx = np.maximum(np.maximum(x0 - X, X - x1), 0)
    dy = np.maximum(np.maximum(y0 - Y, Y - y1), 0)
    d = np.hypot(dx, dy)
    w = np.clip(1.0 - d / width, 0.0, 1.0)
    return (w * w * (3 - 2 * w)).astype(np.float32)      # smoothstep


def _blend_map(coefs, deg, Hmat, bounds, X, Y):
    """Polynomial where it was fitted, homography outside, smoothly blended.

    A cubic diverges fast beyond its fitted region — measured at up to 241 px
    (20 mm) past the dot field, versus 8-56 px of genuine correction inside it.
    Since a page's own corners sit outside the dot coverage, raw extrapolation
    mismeasured an A4 sheet as 202x279 mm and visibly twisted the output. Here
    the correction is tapered away instead, so the worst case degrades to plain
    homography error rather than diverging."""
    U, V = _poly_map(coefs, deg, X, Y)
    den = Hmat[2, 0] * X + Hmat[2, 1] * Y + Hmat[2, 2]
    HU = (Hmat[0, 0] * X + Hmat[0, 1] * Y + Hmat[0, 2]) / den
    HV = (Hmat[1, 0] * X + Hmat[1, 1] * Y + Hmat[1, 2]) / den
    w = _taper(X, Y, bounds)
    return HU + w * (U - HU), HV + w * (V - HV)


def _catmull(t):
    t2, t3 = t * t, t * t * t
    return (-0.5 * t3 + t2 - 0.5 * t,
            1.5 * t3 - 2.5 * t2 + 1.0,
            -1.5 * t3 + 2.0 * t2 + 0.5 * t,
            0.5 * t3 - 0.5 * t2)


def _sample_bicubic(src, U, V):
    """Catmull-Rom sample of src at float coords (U,V). PIL can only resample
    through a homography, and the calibration is a polynomial, so we need our
    own sampler to avoid falling back to bilinear."""
    h, w, ch = src.shape
    x0 = np.floor(U).astype(np.int32)
    y0 = np.floor(V).astype(np.int32)
    fx = (U - x0).astype(np.float32)
    fy = (V - y0).astype(np.float32)
    wx = _catmull(fx)
    wy = _catmull(fy)
    out = np.zeros(U.shape + (ch,), np.float32)
    for m in range(4):
        yy = np.clip(y0 + m - 1, 0, h - 1)
        row = np.zeros(U.shape + (ch,), np.float32)
        for n_ in range(4):
            xx = np.clip(x0 + n_ - 1, 0, w - 1)
            row += src[yy, xx] * wx[n_][..., None]
        out += row * wy[m][..., None]
    return out


def rectify_page_poly(img8, calib, dpi=None, paper="auto", verbose=True, quad=None):
    """Rectify using the polynomial calibration.

    A homography cannot represent this scanner's distortion — fitted against 488
    chart dots it leaves 2.5 mm against a cubic's 0.21 mm — so warping through
    one leaves visible distortion, worst where its four corners constrain it
    least (mid-page, toward the bottom). Here every output pixel is mapped
    through the polynomial instead."""
    deg = int(calib.get("degree", 3))
    fwd = calib.get("mm_to_px")
    inv = calib.get("px_to_mm")
    if not fwd or not inv:
        return img8, False
    Hmat = np.array(calib["H_mm_to_px"], float).reshape(3, 3)
    Hinv = np.linalg.inv(Hmat)
    bmm = calib.get("fit_bounds_mm")
    bpx = calib.get("fit_bounds_px")
    if not bmm or not bpx:
        return img8, False
    S = (dpi or calib.get("dpi", 300.0)) / 25.4

    if quad is None:
        quad = _page_quad_px(img8.max(2).astype(np.float32))
    if quad is None:
        if verbose:
            print("[*] rectify: no page found — skipping.")
        return img8, False

    qx = np.array([p[0] for p in quad], np.float32)
    qy = np.array([p[1] for p in quad], np.float32)
    # Refuse to run where the model was never fitted. A cubic diverges by
    # 5-20mm beyond its dot field, which mismeasured an A4 sheet as 202x279
    # and visibly twisted the output. Better to fall back to the homography
    # (~2mm, but stable) than to extrapolate confidently.
    hq = np.stack([qx, qy, np.ones(4, np.float32)], 1) @ Hinv.T
    hmm = hq[:, :2] / hq[:, 2:3]
    out_by = max(float(bmm[0] - hmm[:, 0].min()), float(hmm[:, 0].max() - bmm[2]),
                 float(bmm[1] - hmm[:, 1].min()), float(hmm[:, 1].max() - bmm[3]), 0.0)
    # Modest extrapolation is fine and in fact ACCURATE — the cubic puts the A4
    # page corners at 209.5x297.1mm (true 210x297) from ~4-23mm outside the dot
    # field, while blending toward the homography wrecked it (200.6x299.0). But
    # the old, much tighter chart let pages sit 31-41mm out and the output came
    # out visibly twisted, so refuse beyond that.
    if out_by > 55.0:
        if verbose:
            print(f"[*] rectify: page extends {out_by:.0f}mm beyond the calibrated "
                  f"area — falling back to homography (recalibrate to cover it).")
        return img8, False
    mx, my = _poly_map(inv, deg, qx, qy)
    mm = np.stack([mx, my], 1).astype(float)
    dist = lambda a, b: float(np.hypot(*(a - b)))
    w_mm = (dist(mm[0], mm[1]) + dist(mm[3], mm[2])) / 2
    h_mm = (dist(mm[0], mm[3]) + dist(mm[1], mm[2])) / 2
    if not (30 < w_mm < 500 and 30 < h_mm < 700):
        if verbose:
            print(f"[*] rectify: implausible page {w_mm:.0f}x{h_mm:.0f}mm — skipping.")
        return img8, False

    tgt, note = (w_mm, h_mm), "measured"
    if paper != "none":
        hit = (_snap_paper(w_mm, h_mm) if paper == "auto"
               else (paper, PAPER[paper] if w_mm < h_mm else PAPER[paper][::-1]))
        if hit:
            note, tgt = hit
    ow, oh = int(round(tgt[0] * S)), int(round(tgt[1] * S))

    # output pixel -> platen mm (4-corner homography; the page IS a rectangle in
    # mm space once the polynomial has removed the lens distortion)
    A = []
    for (X, Y), (x, y) in zip([(0, 0), (ow, 0), (ow, oh), (0, oh)], mm):
        A.append([X, Y, 1, 0, 0, 0, -x * X, -x * Y, -x])
        A.append([0, 0, 0, X, Y, 1, -y * X, -y * Y, -y])
    _, _, Vv = np.linalg.svd(np.array(A, float))
    M = (Vv[-1] / Vv[-1][-1]).reshape(3, 3)

    src = img8.astype(np.float32)
    out = np.empty((oh, ow, 3), np.uint8)
    xs = np.arange(ow, dtype=np.float32)
    for y0 in range(0, oh, 256):                    # strip-wise: keeps memory sane
        y1 = min(y0 + 256, oh)
        YY, XX = np.meshgrid(np.arange(y0, y1, dtype=np.float32), xs, indexing="ij")
        den = M[2, 0] * XX + M[2, 1] * YY + M[2, 2]
        MX = (M[0, 0] * XX + M[0, 1] * YY + M[0, 2]) / den
        MY = (M[1, 0] * XX + M[1, 1] * YY + M[1, 2]) / den
        U, V = _poly_map(fwd, deg, MX, MY)
        out[y0:y1] = np.clip(_sample_bicubic(src, U, V), 0, 255).astype(np.uint8)
    if verbose:
        ang = np.degrees(np.arctan2(mm[1][1] - mm[0][1], mm[1][0] - mm[0][0]))
        print(f"[*] rectify(poly deg{deg}): page {w_mm:.1f}x{h_mm:.1f}mm at "
              f"{ang:+.2f}deg -> {note} {tgt[0]:.1f}x{tgt[1]:.1f}mm = {ow}x{oh}px "
              f"at {S*25.4:.0f} dpi")
    return out, True


def rectify_page(img8, calib, dpi=None, paper="auto", verbose=True, quad=None):
    """Map the page's four corners onto an exact, axis-aligned page rectangle.

    Better than rectifying an axis-aligned mm box then cropping, which keeps
    three separate errors: the page sits at ~1 deg on the platen so its bounding
    box is several mm oversized, the crop retains margin, and the page's own
    measured size carries the calibration's residual error. Mapping the quad
    straight onto the target rectangle deskews, removes margin, and snaps to a
    true paper size in a single resample."""
    from PIL import Image
    Hm = np.array(calib["H_mm_to_px"], float).reshape(3, 3)
    Hi = np.linalg.inv(Hm)
    S = (dpi or calib.get("dpi", 300.0)) / 25.4

    if quad is None:
        quad = _page_quad_px(img8.max(2).astype(np.float32))
    if quad is None:
        if verbose:
            print("[*] rectify: no page found — skipping.")
        return img8, False

    p = np.array([[x, y, 1.0] for x, y in quad]) @ Hi.T
    mm = p[:, :2] / p[:, 2:3]
    if not np.all(np.isfinite(mm)):
        return img8, False
    dist = lambda a, b: float(np.hypot(*(a - b)))
    w_mm = (dist(mm[0], mm[1]) + dist(mm[3], mm[2])) / 2
    h_mm = (dist(mm[0], mm[3]) + dist(mm[1], mm[2])) / 2
    if not (30 < w_mm < 500 and 30 < h_mm < 700):
        if verbose:
            print(f"[*] rectify: implausible page {w_mm:.0f}x{h_mm:.0f}mm — skipping.")
        return img8, False

    tgt = (w_mm, h_mm)
    note = "measured"
    if paper != "none":
        hit = _snap_paper(w_mm, h_mm) if paper == "auto" else \
              (paper, PAPER[paper] if w_mm < h_mm else PAPER[paper][::-1])
        if hit:
            note, tgt = hit[0], hit[1]
    ow, oh = int(round(tgt[0] * S)), int(round(tgt[1] * S))

    # homography: output pixel -> source pixel (PIL's PERSPECTIVE convention)
    dst = [(0, 0), (ow, 0), (ow, oh), (0, oh)]
    A = []
    for (X, Y), (x, y) in zip(dst, quad):
        A.append([X, Y, 1, 0, 0, 0, -x * X, -x * Y, -x])
        A.append([0, 0, 0, X, Y, 1, -y * X, -y * Y, -y])
    _, _, V = np.linalg.svd(np.array(A, float))
    M = (V[-1] / V[-1][-1]).reshape(3, 3)
    out = Image.fromarray(img8, "RGB").transform(
        (ow, oh), Image.PERSPECTIVE, tuple(M.flatten()[:8]),
        resample=Image.BICUBIC, fillcolor=(0, 0, 0))
    if verbose:
        ang = np.degrees(np.arctan2(mm[1][1] - mm[0][1], mm[1][0] - mm[0][0]))
        print(f"[*] rectify: page {w_mm:.1f}x{h_mm:.1f}mm at {ang:+.2f}deg "
              f"-> {note} {tgt[0]:.1f}x{tgt[1]:.1f}mm = {ow}x{oh}px "
              f"at {S*25.4:.0f} dpi (bicubic)")
    return np.asarray(out), True


def default_area_mm(calib, w_mm=None, h_mm=None):
    """The mm rectangle to use when not detecting a page.

    Defaults to EVERYTHING THE SENSOR SEES: the mm bounding box of the frame's
    four corners, ~569 x 383 mm. "Raw" should not silently throw away image.

    It used to default to Ricoh's 432 x 300 mm maximum flat-document area,
    centred on the optical centre — but that left 73/64/34/49 mm of captured
    frame unused on the four sides, so anything placed off-centre on the
    (loose) mat came out clipped. Narrow it with --area when the extra margin
    is just desk.

    The mat cannot be detected and used instead: its dark region merges with
    the scanner base and the shadowed frame corners, so on all four reference
    scans the largest dark region spans the entire frame and its bounding box
    is meaningless."""
    Hm = np.array(calib["H_mm_to_px"], float).reshape(3, 3)
    Hi = np.linalg.inv(Hm)
    pts = []
    for x, y in ((0, 0), (WIDTH_PX - 1, 0),
                 (WIDTH_PX - 1, HEIGHT_PX - 1), (0, HEIGHT_PX - 1)):
        v = Hi @ np.array([float(x), float(y), 1.0])
        pts.append(v[:2] / v[2])
    pts = np.array(pts)
    x0, y0 = pts.min(0)
    x1, y1 = pts.max(0)
    if w_mm or h_mm:                       # explicit size, centred on the frame
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        w_mm = w_mm or (x1 - x0)
        h_mm = h_mm or (y1 - y0)
        return (cx - w_mm / 2, cy - h_mm / 2, cx + w_mm / 2, cy + h_mm / 2)
    return (float(x0), float(y0), float(x1), float(y1))


def rectify(img8, calib, dpi=None, margin_mm=4.0, verbose=True, area_mm=None):
    """Map the scan into true millimetre space using the fixed calibration.

    This replaces per-scan corner detection, which cannot recover the aspect
    ratio on this scanner: the page is tilted about one axis, so the horizontal
    vanishing point goes to infinity and the problem is ill-conditioned. The
    camera-to-platen geometry is fixed, so one calibration serves every flat
    scan and yields correct proportions AND a known output scale."""
    Hm = np.array(calib["H_mm_to_px"], float).reshape(3, 3)
    Hi = np.linalg.inv(Hm)
    S = (dpi or calib.get("dpi", 300.0)) / 25.4          # output px per mm

    if area_mm is not None:
        # Fixed area: no page detection at all. This is the whole-mat / book
        # case, where there is no single sheet to find.
        x0, y0, x1, y1 = area_mm
    else:
        g = img8.max(2).astype(np.float32)
        bb = _page_bbox_mm(g, Hi, margin_mm)
        if bb is None:
            if verbose:
                print("[*] rectify: no page found — skipping.")
            return img8, False
        (x0, y0), (x1, y1) = bb
    ow, oh = int(round((x1 - x0) * S)), int(round((y1 - y0) * S))
    if not (50 < ow < 20000 and 50 < oh < 20000):
        if verbose:
            print(f"[*] rectify: implausible output {ow}x{oh} — skipping.")
        return img8, False

    # Output-pixel -> mm is affine, mm -> source-px is the calibration
    # homography, so the composition is a single homography and PIL can do the
    # resampling with BICUBIC. That matters: the far end of the page is upscaled
    # ~1.4x here, where bilinear is visibly soft.
    from PIL import Image
    A = np.array([[1.0 / S, 0.0, x0],
                  [0.0, 1.0 / S, y0],
                  [0.0, 0.0, 1.0]])
    M = Hm @ A
    M = M / M[2, 2]
    coeffs = tuple(M.flatten()[:8])
    out = Image.fromarray(img8, "RGB").transform(
        (ow, oh), Image.PERSPECTIVE, coeffs, resample=Image.BICUBIC, fillcolor=(0, 0, 0))
    if verbose:
        print(f"[*] rectify: {x1-x0:.1f} x {y1-y0:.1f} mm -> {ow}x{oh} px "
              f"at {S*25.4:.0f} dpi (bicubic)")
    return np.asarray(out), True


def _rfit(x, y, iters=4):
    """Robust line fit y = m*x + c with iterative outlier trimming (survives a
    dog-eared corner or text touching an edge)."""
    x = np.asarray(x, float); y = np.asarray(y, float)
    m, c = np.polyfit(x, y, 1)
    keep = np.ones(len(x), bool)
    for _ in range(iters):
        r = np.abs(y - (m * x + c))
        nk = r < 2.0 * r[keep].std() + 1e-6
        if nk.sum() > max(10, 0.3 * len(x)):
            keep = nk
        m, c = np.polyfit(x[keep], y[keep], 1)
    return m, c

def dewarp(img8, verbose=True):
    """Correct the SV600's overhead-camera keystone. Fit lines to the page's four
    edges and intersect them for accurate corners, then perspective-warp the
    trapezoid to a true rectangle. Returns (image, applied?).

    NOTE: this removes the projective (keystone) distortion. A small residual
    vertical scale gradient can remain from the SV600's non-projective optics —
    that's the part ScanSnap's proprietary flattening handles. It's cosmetic and
    invisible to OCR."""
    # Crop to the page box first — edge detection on the full frame is fooled by
    # stray bright pixels (a second sheet, edge reflections, wrap slivers).
    img8 = autocrop(img8, verbose=verbose)
    ch, cw = img8.shape[:2]
    g = img8.mean(2)
    mask = g > (g.min() + g.max()) / 2
    for _ in range(3):   # erode: drop thin slivers/specks
        mask = (mask & np.roll(mask, 1, 0) & np.roll(mask, -1, 0)
                     & np.roll(mask, 1, 1) & np.roll(mask, -1, 1))
    rows_any = np.where(mask.any(1))[0]
    cols_any = np.where(mask.any(0))[0]
    if len(rows_any) < ch * 0.3 or len(cols_any) < cw * 0.3:
        if verbose: print("[*] dewarp: page not found — skipping.")
        return img8, False
    # Sample each edge over the middle 70% (avoids the corner regions).
    ys = rows_any[(rows_any > ch * 0.15) & (rows_any < ch * 0.85)]
    xs = cols_any[(cols_any > cw * 0.15) & (cols_any < cw * 0.85)]
    xl = np.array([np.argmax(mask[y]) for y in ys])            # left edge:  x(y)
    xr = np.array([cw - 1 - np.argmax(mask[y][::-1]) for y in ys])  # right edge
    yt = np.array([np.argmax(mask[:, x]) for x in xs])         # top edge:   y(x)
    yb = np.array([ch - 1 - np.argmax(mask[:, x][::-1]) for x in xs])  # bottom
    lm, lc = _rfit(ys, xl); rm, rc = _rfit(ys, xr)   # x = m*y + c
    tm, tc = _rfit(xs, yt); bm, bc = _rfit(xs, yb)   # y = m*x + c
    def isect(lm, lc, tm, tc):                        # (x = lm*y+lc) ∩ (y = tm*x+tc)
        y = (tm * lc + tc) / (1 - tm * lm); return (lm * y + lc, y)
    TL = isect(lm, lc, tm, tc); BL = isect(lm, lc, bm, bc)
    TR = isect(rm, rc, tm, tc); BR = isect(rm, rc, bm, bc)
    dist = lambda a, b: float(np.hypot(a[0] - b[0], a[1] - b[1]))
    outW = int(round((dist(TL, TR) + dist(BL, BR)) / 2))
    outH = int(round((dist(TL, BL) + dist(TR, BR)) / 2))
    if outW < cw * 0.3 or outH < ch * 0.3:
        if verbose: print("[*] dewarp: corners implausible — skipping.")
        return img8, False
    from PIL import Image
    dst = [(0, 0), (outW, 0), (outW, outH), (0, outH)]
    src = [TL, TR, BR, BL]
    M = []
    for (X, Y), (x, y) in zip(dst, src):
        M.append([X, Y, 1, 0, 0, 0, -x * X, -x * Y])
        M.append([0, 0, 0, X, Y, 1, -y * X, -y * Y])
    coeffs = np.linalg.solve(np.array(M, float), np.array(src, float).reshape(8))
    out = Image.fromarray(img8, "RGB").transform(
        (outW, outH), Image.PERSPECTIVE, tuple(coeffs), Image.BICUBIC)
    if verbose:
        print(f"[*] dewarp: keystone (top {dist(TL,TR):.0f}px / bottom {dist(BL,BR):.0f}px) "
              f"-> {outW}x{outH} rectangle")
    return np.asarray(out), True

# ---- page splitting ---------------------------------------------------------
def _out_names(output, n):
    """One name per page: 'out.png' -> out.png, or out-1.png, out-2.png ..."""
    if n <= 1:
        return [output]
    stem, ext = os.path.splitext(output)
    return [f"{stem}-{i + 1}{ext}" for i in range(n)]


def join_pages(pages, gap=0):
    """Lay rectified pages side by side into one image, top-aligned.

    Two A4 portrait sheets (2480x3508 each) give 4960x3508 — A3 landscape at
    300 dpi. Heights can differ by a pixel or two after independent snapping, so
    the canvas takes the tallest and the shortfall is filled with white rather
    than black: a black band would be read as content by OCR and would darken
    any later thresholding."""
    h = max(p.shape[0] for p in pages)
    w = sum(p.shape[1] for p in pages) + gap * (len(pages) - 1)
    out = np.full((h, w, 3), 255, np.uint8)
    x = 0
    for p in pages:
        out[:p.shape[0], x:x + p.shape[1]] = p
        x += p.shape[1] + gap
    print(f"[*] join: {len(pages)} pages -> {w}x{h}")
    return out


def _rectify_one(img, calib, args, quad=None, verbose=True):
    """Geometry for a single sheet. Returns (image, fixed)."""
    if calib is not None:
        # Preferred: map the page's four corners straight onto a true page
        # rectangle — deskews, drops the margin, snaps to a standard size.
        if calib.get("model") == "poly":
            out, fixed = rectify_page_poly(img, calib, dpi=args.dpi,
                                           paper=args.paper, verbose=verbose,
                                           quad=quad)
            if fixed:
                return out, True
        out, fixed = rectify_page(img, calib, dpi=args.dpi, paper=args.paper,
                                  verbose=verbose, quad=quad)
        if fixed:
            return out, True
    return img, False


def process_pages(img, args, calib):
    """Geometry for the whole frame, returning one image per sheet.

    Each sheet gets its own quad and its own warp. Rectifying the pair as a
    single A3 would instead force one warp through four corners that are not
    all on one sheet, so any gap between the sheets, or one lying a degree off
    the other, would skew both."""
    if args.no_dewarp:
        return [img if args.no_crop else autocrop(img)]

    if getattr(args, "full", False):
        # Whole scan area, no page detection — for a book spread, or anything
        # that is not a single sheet on a dark background.
        if calib is None:
            print("[*] --full needs a calibration; returning the raw frame.")
            return [img]
        area = args.area or default_area_mm(calib)
        page, ok = rectify(img, calib, dpi=args.dpi, area_mm=area)
        if not ok:
            print("[*] --full: rectify failed — returning the raw frame.")
            return [img]
        return [page]

    if args.pages > 1:
        if calib is None:
            sys.exit("--pages needs a calibration; run sv600_calibrate.py first.")
        quads = _page_quads_px(img.max(2).astype(np.float32), pages=args.pages)
        if len(quads) < args.pages:
            print(f"[!] --pages {args.pages} but only {len(quads)} sheet(s) found. "
                  f"Check both are inside the mat's L corner marks.")
        if quads:
            out = []
            for i, q in enumerate(quads, 1):
                print(f"[*] sheet {i} of {len(quads)}:")
                page, fixed = _rectify_one(img, calib, args, quad=q)
                if not fixed:
                    print(f"[!] sheet {i}: rectify failed — skipping.")
                    continue
                out.append(page)
            if out:
                return out
        print("[!] Multi-page detection failed — falling back to single page.")

    page, fixed = _rectify_one(img, calib, args)
    if not fixed and calib is not None:
        # Page corners not found: fall back to rectifying a box around it,
        # which still fixes the perspective, then crop.
        page, fixed = rectify(img, calib, dpi=args.dpi)
        if fixed and not args.no_crop:
            page = autocrop(page)
    if not fixed:
        if calib is None:
            print("[*] No calibration found — falling back to dewarp(). "
                  "Run sv600_calibrate.py for correct proportions.")
        page, fixed = dewarp(img)             # does its own crop
    if not fixed and not args.no_crop:
        page = autocrop(page)
    return [page]


def _area_arg(s):
    """Parse --area X0,Y0,X1,Y1 (mm)."""
    try:
        v = [float(x) for x in s.replace(" ", "").split(",")]
    except ValueError:
        raise argparse.ArgumentTypeError("--area needs four numbers: X0,Y0,X1,Y1")
    if len(v) != 4:
        raise argparse.ArgumentTypeError("--area needs four numbers: X0,Y0,X1,Y1")
    if v[2] <= v[0] or v[3] <= v[1]:
        raise argparse.ArgumentTypeError("--area must be X0,Y0,X1,Y1 with X1>X0 and Y1>Y0")
    return tuple(v)


# ---- main -------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Native Linux scan from a Fujitsu ScanSnap SV600.")
    ap.add_argument("output", nargs="?", default="sv600.png",
                    help="output path; the extension is set by --format "
                         "(default sv600.png)")
    ap.add_argument("--no-dewarp", action="store_true", help="skip all geometric correction")
    ap.add_argument("--calibration", metavar="FILE",
                    help="image->mm calibration (default: sv600-calibration.json beside this script)")
    ap.add_argument("--dpi", type=float, default=None,
                    help="output resolution when using a calibration (default: its own)")
    ap.add_argument("--paper", default="auto",
                    choices=["auto", "none"] + sorted(PAPER),
                    help="snap the output to a true paper size: 'auto' picks the "
                         "nearest standard within 4%%, 'none' keeps the measured "
                         "size (default: auto)")
    ap.add_argument("--pages", type=int, default=1, metavar="N",
                    help="number of sheets on the mat (default 1). With N>1 each "
                         "sheet is detected and rectified separately and written "
                         "as out-1.png, out-2.png ... left to right; --join then "
                         "combines them into one wide page instead.")
    ap.add_argument("--join", action="store_true",
                    help="with --pages N, write a single side-by-side image "
                         "(two A4 portrait sheets -> one A3 landscape) instead "
                         "of one file per sheet")
    ap.add_argument("--no-crop",  action="store_true", help="skip crop (only used when --no-dewarp)")
    # Renamed: alongside the new --color (colour MODE), "--no-color" read as if
    # it meant greyscale, which is the opposite of what it does. Old spelling
    # kept as an alias so existing commands keep working.
    ap.add_argument("--no-white-balance", "--no-color", dest="no_color",
                    action="store_true",
                    help="skip white-balance/gamma (save raw sensor colours). "
                         "Not a colour MODE — see --color for that")
    ap.add_argument("--no-ca", action="store_true", help="skip chromatic-aberration correction")
    ap.add_argument("--no-sharpen", action="store_true", help="skip unsharp mask")
    ap.add_argument("--sharpen", type=int, default=140, metavar="PCT",
                    help="unsharp mask strength %% (default 140; 0-300)")
    ap.add_argument("--full", action="store_true",
                    help="scan the WHOLE area instead of detecting a page — for "
                         "a book spread, or anything that is not a single sheet. "
                         "Skips page detection, cropping and paper snapping")
    ap.add_argument("--area", type=_area_arg, metavar="X0,Y0,X1,Y1",
                    help="with --full, the millimetre rectangle to output "
                         "(default: Ricoh's 432x300mm maximum, centred)")
    sv600_output.add_args(ap)
    ap.add_argument("--keep-raw", metavar="FILE", help="also write the raw RGB bytes to FILE")
    ap.add_argument("--timeout",  type=float, default=30.0, help="seconds to wait for the scan to complete")
    args = ap.parse_args()

    dev = open_dev()
    try:
        run_setup(dev)
        wait_ready(dev, timeout=args.timeout)
        buf = read_image(dev)
        teardown(dev)
    finally:
        try: usb.util.release_interface(dev, 0)
        except Exception: pass

    if args.keep_raw:
        open(args.keep_raw, "wb").write(bytes(buf))
        _chown_back(args.keep_raw)
        print(f"[*] Raw bytes -> {args.keep_raw}")

    img = to_rgb(buf)

    if not args.no_ca:
        img = correct_ca(img)

    # Geometry BEFORE colour, for two reasons: colour_correct's gamma lifts the
    # dark surround and breaks the page detection rectify() relies on; and doing
    # white balance after cropping means it sees the page instead of a frame
    # that is ~60% black background.
    # Geometry. Preferred path is the fixed calibration (correct proportions and
    # a known output scale); dewarp() is the fallback when none is present, but
    # it cannot recover the aspect ratio on this scanner and comes out ~17% off.
    calib = None if args.no_dewarp else load_calibration(args.calibration)
    pages = process_pages(img, args, calib)

    if not args.no_color:
        pages = [color_correct(p) for p in pages]

    if not args.no_sharpen:
        pages = [sharpen(p, amount=args.sharpen) for p in pages]

    # Write the resolution into the file. Without it img2pdf/ocrmypdf assume a
    # default (~96 dpi) and the PDF claims a 633x872mm page for what is actually
    # a correctly-sized A4 image — the pixels were right, the stated physical
    # size was 3x out.
    out_dpi = float(args.dpi or (calib or {}).get("dpi", 300.0)) if not args.no_dewarp else 300.0

    if len(pages) > 1 and args.join:
        pages = [join_pages(pages)]

    here = os.path.dirname(os.path.abspath(__file__))
    base = os.path.splitext(args.output)[0]
    sv600_output.save(pages, base, fmt=args.format, color=args.color,
                      dpi=out_dpi, jpeg_quality=args.jpeg_quality,
                      bw_block=args.bw_block, bw_offset=args.bw_offset,
                      scanocr=os.path.join(here, "scanocr"), lang=args.ocr_lang,
                      chown_back=_chown_back)

if __name__ == "__main__":
    main()
