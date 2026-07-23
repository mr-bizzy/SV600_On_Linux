#!/usr/bin/env python3
"""
sv600_watch.py — hold the SV600 open and scan when its button is pressed.

THE BUTTON
----------
The SV600 has no interrupt endpoint — only bulk OUT 0x02 and bulk IN 0x81 — so
it cannot notify the host of anything. A button press is only visible by ASKING:
GET DATA BUFFER STATUS (op 0xc2) returns a 32-byte payload whose byte[4] goes
0x00 -> 0x01 while the button is down. That is why the official software polls
0xc2 every 200 ms for the entire session; it is not an idle heartbeat.

Confirmed on hardware (capture sv600-button-test.pcapng, empty mat, one press):
    t=23.29  byte[4] 0x00->0x01     button pressed
    t=24.44  byte[4] 0x01->0x00     released, ~1.15 s later
    t=25.18  SET WINDOW + SCAN      official software starts the scan
One press, one pulse; placing a document on the mat produces nothing.

WHY A DAEMON, NOT A CRON JOB
----------------------------
Two reasons, both load-bearing:

 1. Only one process can claim a USB interface. Polling five times a second
    means this process owns the scanner, so nothing else — sv600_scan.py
    included — can open it while this runs. That is a constraint, not a bug:
    it is why this does the scanning itself rather than shelling out.

 2. The per-scan SETUP + TEARDOWN cycle is what causes the cold-scanner
    CHECK CONDITION on CHANGE IRLED (the lamp subsystem answers TEST UNIT
    READY before it is actually ready) and the flashing-amber wedges. Holding
    the device open and warm removes that failure class rather than retrying
    around it.

USAGE
-----
    sudo python3 sv600_watch.py --outdir ~/Scans
    sudo python3 sv600_watch.py --outdir ~/Scans -f pdf-ocr -c gray
    sudo python3 sv600_watch.py --outdir ~/Scans --batch 20 -f pdf-ocr
    sudo python3 sv600_watch.py --outdir ~/Scans --pages 2    # two sheets
    sudo python3 sv600_watch.py --dry-run                     # log presses only

--batch turns successive presses into ONE document: turn the page, press,
repeat; the document closes N seconds after the last press.

Files are chowned back to SUDO_UID, so they are yours, not root's.
Ctrl-C tears the scanner down cleanly.
"""
import argparse
import datetime as _dt
import os
import shutil
import subprocess
import tempfile
import sys
import time

import usb.core
import usb.util

import sv600_scan as S
import sv600_output

# GET DATA BUFFER STATUS, byte-exact from the capture: 10-byte CDB, transfer
# length 0x20 at CDB bytes 6-8.
BUFSTAT_CMD = S.wrap(bytes([0xc2, 0, 0, 0, 0, 0, 0, 0, 0x20, 0]))
BUFSTAT_LEN = 32
BUTTON_BYTE = 4          # byte[4] of the payload
# byte[4] is a LATCHED ONE-SHOT EVENT, not the live button level. Measured on
# hardware: at a 50 ms poll every press reports exactly ONE poll however long
# the button is held — a level signal held 3 s would have reported ~60. The flag
# therefore persists until something reads it, so a press cannot be missed at
# any poll rate. The rate only sets how soon after a press the scan starts;
# 50 ms keeps that imperceptible for a 32-byte transfer 20x a second.
POLL_S = 0.05


# TEARDOWN assumes a post-scan device state — its second command is a 24-byte
# dimension readback, which a scanner that was never set up rejects with
# ILLEGAL REQUEST / ASC 0x24 (invalid field in CDB). That is exactly what a
# --dry-run session hit on exit. So only tear down if SETUP actually ran.
# Scan modes, cycled by the prompt's Switch button in this order. Each is
# (key, label, full, dewarp, single, rtl, join):
#   full   -> --full: skip page detection, keep the whole 432x300mm area
#   dewarp -> flatten book curvature (sv600_dewarp) after capture
#   single -> a cover / first / last leaf: one page, no gutter split
#   rtl    -> right-to-left binding: order the two pages right then left
#   join   -> keep the flattened halves as ONE combined spread image
#
# The three flatten layouts mirror ScanSnap's Book Image Viewer output buttons:
#   join   == ScanSnap "1"    one spread
#   (2pg)  == ScanSnap "1|2"  two pages, left first
#   rtl    == ScanSnap "2|1"  two pages, right first
# "Book (raw)" keeps the un-flattened spread, because dewarping is unvalidated
# on real books; the flatten layouts and "cover" opt into it.
MODES = [
    ("document", "Document (detect page)",              False, False, False, False, False),
    # The recommended book path: capture the raw spread, then run sv600_prep.py
    # on the folder and flatten in ScanTailor Advanced. In-daemon flatten below
    # is EXPERIMENTAL — it fails on real thick books (fore-edge / gutter), which
    # is why ScanTailor does the flattening.
    ("book-raw", "Book, raw spread (for ScanTailor)",   True,  False, False, False, False),
    ("book-1",   "Book flatten, 1 spread (experimental)", True, True, False, False, True),
    ("book-2",   "Book flatten, 2 pages (experimental)", True, True, False, False, False),
    ("book-rtl", "Book flatten, 2 pages R-to-L (exp.)", True,  True,  False, True,  False),
    ("cover",    "Cover / single page (experimental)",  True,  True,  True,  False, False),
]

STATE = {"setup_done": False, "tmpdir": None,
         # Live scan mode, switchable from the prompt, as an index into MODES.
         # --full seeds it to "book". Lives here, not in the parsed arguments.
         "mode": 0, "last_raw": None, "last_count": 0}


def cur_mode():
    return MODES[STATE["mode"] % len(MODES)]


def mode_label(i=None):
    return MODES[(STATE["mode"] if i is None else i) % len(MODES)][1]


def log(msg):
    print(f"[{_dt.datetime.now():%H:%M:%S}] {msg}", flush=True)


def poll_button(dev):
    """True if the button is currently down, None if the poll failed."""
    try:
        data, _ = S.cmd(dev, BUFSTAT_CMD, read_len=BUFSTAT_LEN, timeout=2000)
    except usb.core.USBError:
        for ep in (S.EP_IN, S.EP_OUT):
            try:
                dev.clear_halt(ep)
            except Exception:
                pass
        return None
    if len(data) <= BUTTON_BYTE:
        return None
    return bool(data[BUTTON_BYTE] & 0x01)


def capture(dev, args):
    """Scan and return the CA-corrected full frame, before any page logic."""
    S.run_seq(dev, S.SETUP, "setup")
    STATE["setup_done"] = True
    S.wait_ready(dev, timeout=args.timeout)
    buf = S.read_image(dev)
    # No teardown here: teardown returns the scanner to idle and we want it warm
    # for the next press. It runs once, on shutdown.
    img = S.to_rgb(buf)
    if not args.no_ca:
        img = S.correct_ca(img)
    return img


def _flatten(pages, args, calib, single, rtl=False, join=False):
    """Dewarp each captured frame into flat page(s), in the chosen layout.

    A dewarp failure falls back to the un-flattened page rather than losing the
    scan — a curved-but-readable page beats no page. Colour and sharpening are
    left to render(), which runs them after this so the final resample is not
    itself sharpened."""
    import sv600_dewarp as D
    z_px = 350.0 * (float((calib or {}).get("dpi", 300.0)) / 25.4)
    out = []
    for p in pages:
        try:
            left, right = D.dewarp(p, verbose=False, z_px=z_px, single=single)
        except Exception as e:
            log(f"[!] dewarp failed ({e}) — keeping the un-flattened page")
            out.append(p)
            continue
        if single:
            got = [x for x in (left, right) if x is not None]
        elif join:
            spread = D.join(left, right)
            got = [spread] if spread is not None else []
        else:
            halves = [right, left] if rtl else [left, right]
            got = [x for x in halves if x is not None]
        out.extend(got or [p])
    return out


def render(img, args, calib, mode_idx):
    """Turn a captured frame into finished pages, in the given mode.

    Kept separate from capture() so the prompt's mode switch can re-render a
    page that has ALREADY been scanned, instead of making you rescan it. The
    frame is the expensive part (~17 s); rendering is seconds (dewarp adds a
    few)."""
    _key, _label, full, dewarp, single, rtl, join = MODES[mode_idx % len(MODES)]
    # process_pages() reads args.full; present the mode without mutating the
    # caller's parsed arguments.
    a = argparse.Namespace(**vars(args))
    a.full = full
    pages = S.process_pages(img, a, calib)
    if dewarp:
        pages = _flatten(pages, args, calib, single, rtl=rtl, join=join)
    if not args.no_color:
        pages = [S.color_correct(p) for p in pages]
    if not args.no_sharpen:
        pages = [S.sharpen(p, amount=args.sharpen) for p in pages]
    if len(pages) > 1 and args.join:
        pages = [S.join_pages(pages)]
    return pages


def do_scan(dev, args, calib):
    """One full scan + post-processing in the current mode."""
    img = capture(dev, args)
    # Keep the frame so the mode switch can re-render it. One frame is 74 MB;
    # only the most recent is kept, which is all the switch needs.
    STATE["last_raw"] = img
    pages = render(img, args, calib, STATE["mode"])
    STATE["last_count"] = len(pages)
    return pages


# The dialog is yad, not zenity, for two reasons found by testing:
#   * zenity --question ALWAYS shows a Cancel button ("--no-cancel is not
#     supported for this dialog"), and there must be no Close option here;
#   * zenity can only add extra buttons, whose on-screen order it chooses; yad
#     renders --button in the given order with the exit code you assign, which
#     is what makes the ordering below exactly what was asked for.
# Labels must not contain "&": yad parses them as Pango markup and a bare
# ampersand aborts rendering ("Entity did not end with a semicolon").
PROMPT_BUTTONS = [
    ("Scan next page", 0, "next"),
    ("Finish and save", 1, "finish"),
    ("Discard last page", 2, "redo"),
    ("Discard document", 3, "discard"),
]
# A single "Change mode" button opens a RADIOLIST to pick any mode directly,
# rather than a "Next mode" button that cycles one step at a time (tedious with
# six modes). yad forms are static so an in-prompt combo cannot live-update the
# preview; a separate picker dialog sidesteps that — pick, then the prompt
# re-renders and reopens with the new preview.
MODE_BUTTON = 4


def pick_mode():
    """Radiolist of all modes, current one preselected. Returns an index or None."""
    rows = []
    for i, (_k, label, *_r) in enumerate(MODES):
        rows += ["TRUE" if i == STATE["mode"] else "FALSE", label]
    try:
        r = subprocess.run(
            ["yad", "--list", "--radiolist", "--title=Scan mode",
             "--width=460", "--height=320", "--text=Choose the scan mode:",
             "--column=:CHK", "--column=Mode", "--print-column=2",
             "--button=Cancel:1", "--button=Select:0", *rows],
            capture_output=True, text=True, timeout=300)
    except Exception as e:
        log(f"[!] mode picker failed ({e})")
        return None
    if r.returncode != 0:
        return None
    chosen = (r.stdout or "").strip().rstrip("|")
    for i, (_k, label, *_r) in enumerate(MODES):
        if label == chosen:
            return i
    return None
# yad returns 252 when the window is closed or Escape is pressed. There is no
# Close BUTTON, but the window manager can still close the dialog, and that must
# be harmless — the document stays open either way.
YAD_ESCAPE = 252


def prompt_result(dialog):
    """The button pressed: 'next', 'finish', 'redo', 'discard', 'mode' or
    'dismiss'."""
    rc = dialog.returncode
    err = b""
    try:
        if dialog.stderr is not None:
            err = dialog.stderr.read() or b""
    except Exception:
        pass
    # A dialog that fails to start (bad DISPLAY, missing XAUTHORITY) exits at
    # once and would otherwise be indistinguishable from a deliberate choice.
    # Filter the GTK noise yad emits on healthy runs.
    msg = err.decode(errors="replace").strip()
    if msg and "Gtk-WARNING" not in msg and "GLib" not in msg:
        log(f"[!] yad: {msg}")
    if rc == MODE_BUTTON:
        return "mode"
    for _label, code, name in PROMPT_BUTTONS:
        if rc == code:
            return name
    return "dismiss"


def confirm_discard(n):
    """Second chance before throwing away a document that is not yet written.

    Only asked for a real document: discarding a single page is as cheap to
    redo as the confirmation itself, and a prompt there would just train the
    reflex to dismiss prompts."""
    if n < 2:
        return True
    try:
        r = subprocess.run(
            ["yad", "--title=Discard document?", "--width=340",
             f"--text=Discard all <b>{n} scanned pages</b>?\n\n"
             "They have not been saved and cannot be recovered.",
             "--button=Keep scanning:1", "--button=Discard:0"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)
        return r.returncode == 0
    except Exception as e:
        # Anything unexpected — dialog missing, timeout, closed window — must
        # mean KEEP. Discarding is the only irreversible action here.
        log(f"[!] discard confirmation failed ({e}) — keeping the document.")
        return False


def make_thumb(page, path, box=(360, 480)):
    """Write a preview of a page for the dialog to display.

    Fits the page inside a BOX rather than to a fixed width. A fixed width made
    the landscape whole-mat preview half the height of the portrait one
    (260x180 against 260x367), so Book mode looked like it had lost most of the
    image when it had only been drawn small.

    Shown so 'Discard last page' is a judgement rather than a guess — you can
    see whether the page came out before deciding to keep it. Cheap: one
    downscale of an array already in memory."""
    try:
        from PIL import Image
        im = Image.fromarray(page, "RGB")
        bw, bh = box
        scale = min(bw / im.width, bh / im.height)
        size = (max(1, int(im.width * scale)), max(1, int(im.height * scale)))
        im.resize(size, Image.LANCZOS).save(path, "PNG")
        return path
    except Exception as e:
        log(f"[!] could not make a preview: {e}")
        return None


def ask_save_path(args, default_name):
    """Save dialog: filename + explicit format. Returns (base, fmt) or (None, None).

    A plain file chooser with --file-filter entries does NOT work here: GTK's
    filters only filter the VIEW, so picking "PNG image" leaves the filename
    ending .pdf and the format silently stays PDF unless the extension is
    retyped by hand. So the format is its own field in a --form, and it is
    AUTHORITATIVE: whatever extension ends up in the filename is stripped and
    the one matching the chosen format is appended. One control, no way for the
    two to disagree."""
    formats = [args.format] + [f for f in sv600_output.FORMATS if f != args.format]
    default = os.path.join(args.outdir, default_name)
    try:
        r = subprocess.run(
            ["yad", "--form", "--title=Save scanned document",
             "--width=480", "--center", "--on-top",
             "--text=Choose a name and a format."
             "\n<small>The file extension is added automatically.</small>",
             # Folder and name are SEPARATE fields, not one file-picker field.
             # yad's :SFL opens a GTK save chooser whose Name box comes up EMPTY
             # for a file that does not exist yet (GTK needs set_current_name(),
             # which yad does not call), so there was nothing to edit and no
             # default offered. A DIR field pre-fills correctly because the
             # folder exists, and a plain text field always shows its default.
             "--field=Folder:DIR", args.outdir,
             "--field=Name", default_name,
             "--field=Format:CB", "!".join(formats),
             "--button=Save:0", "--button=Cancel:1"],
            capture_output=True, text=True, timeout=300)
    except Exception as e:
        log(f"[!] save dialog failed ({e}) — using the default name.")
        return default, args.format
    if r.returncode != 0:
        return None, None
    # yad --form returns the fields separated by "|", with a trailing one.
    fields = [f.strip() for f in (r.stdout or "").strip().split("|")]
    folder = fields[0] if fields else ""
    name = fields[1] if len(fields) > 1 else ""
    fmt = fields[2] if len(fields) > 2 else ""
    if fmt not in sv600_output.FORMATS:
        fmt = args.format
    if not name:
        # Saving with the name cleared should still produce a file rather than
        # silently doing nothing.
        name = default_name
        log(f"no name given — using {name}")
    typed = sv600_output.format_from_ext(name, fmt)
    if typed and typed != fmt:
        log(f"name says {typed} but Format is {fmt} — using {fmt}")
    return os.path.join(folder or args.outdir, os.path.splitext(name)[0]), fmt


def prompt_open(n, args, thumb=None):
    """Show the 'continue or finish' dialog for a document of n pages so far.

    Non-blocking on purpose: the dialog runs as a child process while the main
    loop keeps polling the button, so pressing the scan button adds a page
    without the user having to touch the dialog at all. That is the behaviour
    the official software has, and it is the reason this is not a plain
    blocking zenity call."""
    if not args.prompt:
        return None
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        # Running headless (e.g. a systemd service that did not inherit the
        # session environment). Say so once rather than failing silently, and
        # let --batch's timeout close documents instead.
        if not STATE.get("warned_no_display"):
            STATE["warned_no_display"] = True
            log("[!] --prompt needs a desktop session but DISPLAY is unset; "
                "falling back to the --batch timeout. Fix with: "
                "systemctl --user import-environment DISPLAY XAUTHORITY")
        return None
    if not shutil.which("yad"):
        if not STATE.get("warned_no_yad"):
            STATE["warned_no_yad"] = True
            log("[!] --prompt needs yad (sudo apt install yad); "
                "falling back to the --batch timeout.")
        return None
    text = (f"<b>{n} page{'s' if n != 1 else ''} scanned.</b>\n\n"
            "Turn the page, then scan the next one -\n"
            "or press the scan button, which does the same.\n\n"
            f"Mode: <b>{mode_label()}</b>")
    try:
        return subprocess.Popen(
            ["yad", "--title=ScanSnap SV600", f"--text={text}",
             "--width=460", "--center", "--on-top",
             f"--image={thumb or 'scanner'}", "--window-icon=scanner",
             *[f"--button={label}:{code}"
               for label, code, _name in PROMPT_BUTTONS],
             f"--button=Change mode:{MODE_BUTTON}"],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    except OSError as e:
        log(f"[!] could not show the prompt: {e}")
        return None


def prompt_close(dialog):
    """Dismiss a dialog that is still open (a button press supersedes it)."""
    if dialog and dialog.poll() is None:
        try:
            dialog.terminate()
            dialog.wait(timeout=2)
        except Exception:
            try:
                dialog.kill()
            except Exception:
                pass


def wait_second_press(dev, args):
    """True if a second press arrives within --double-press seconds.

    The only gesture the hardware leaves us. byte[4] is latched and one-shot, so
    a hold is indistinguishable from a tap and we can only count events, not
    measure their duration. The cost is real and is why this defaults to off:
    every ordinary scan waits out this window before starting."""
    deadline = time.time() + args.double_press
    while time.time() < deadline:
        if poll_button(dev):
            while poll_button(dev):        # drain the second press
                time.sleep(args.poll)
            return True
        time.sleep(args.poll)
    return False


def run_one(dev, args, calib):
    """One scan. Returns the page images written, or [] if it failed.

    Never raises: a daemon that dies on one bad scan is worse than one that logs
    it and waits for the next press."""
    try:
        return do_scan(dev, args, calib)
    except Exception as e:
        log(f"[!] scan failed: {e}")
        # Re-wake rather than die: a failed scan can leave sense data latched,
        # and TEST UNIT READY clears it.
        S.wake(dev, verbose=False)
        return []


def add_page(dev, args, calib, batch):
    """Scan one page into the open document. True if a page was added.

    Shared by the scan button and the dialog's 'Scan next page', so the two
    routes cannot drift apart — they are the same action reached two ways."""
    got = run_one(dev, args, calib)
    if not got:
        return False
    if not batch["stamp"]:
        batch["stamp"] = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    batch["pages"].extend(got)
    return True


def write_out(pages, args, calib, stamp=None, base=None, fmt=None):
    """Write pages in the configured format/colour. Returns paths."""
    if not pages:
        return []
    stamp = stamp or _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    if base is None:
        base = os.path.join(args.outdir, f"{args.prefix}{stamp}")
    dpi = float(args.dpi or (calib or {}).get("dpi", 300.0))
    here = os.path.dirname(os.path.abspath(__file__))
    return sv600_output.save(
        pages, base, fmt=fmt or args.format, color=args.color, dpi=dpi,
        jpeg_quality=args.jpeg_quality, bw_block=args.bw_block,
        bw_offset=args.bw_offset, scanocr=os.path.join(here, "scanocr"),
        lang=args.ocr_lang, chown_back=S._chown_back, log=log)


def rerender_last(batch, args, calib):
    """Re-render the most recent capture in the current mode. True if replaced.

    Only the last frame is kept, so earlier pages in a document keep the mode
    they were scanned in. In practice the switch is made on page 1 and every
    later page is captured in the new mode anyway."""
    img = STATE.get("last_raw")
    n = STATE.get("last_count", 0)
    if img is None or not n or len(batch["pages"]) < n:
        log("nothing to re-render — the new mode applies to the next page")
        return False
    try:
        pages = render(img, args, calib, STATE["mode"])
    except Exception as e:
        log(f"[!] re-render failed: {e}")
        return False
    del batch["pages"][-n:]
    batch["pages"].extend(pages)
    STATE["last_count"] = len(pages)
    log(f"re-rendered the last page as {mode_label()}")
    return True


def show_prompt(batch, args):
    """Open the prompt showing a preview of the most recent page."""
    pages = batch["pages"]
    if not pages:
        return None
    thumb = None
    if not args.no_preview:
        thumb = make_thumb(pages[-1], os.path.join(STATE["tmpdir"], "preview.png"))
    return prompt_open(len(pages), args, thumb)


def finish_batch(batch, args, calib, ask_name=False):
    """Write the accumulated pages as one document.

    Pages are held in memory as arrays until the document is closed, so a
    multi-page format never has to round-trip through intermediate files that
    would then need cleaning up — and choosing a single-page format simply
    writes each page separately at the end instead."""
    if not batch["pages"]:
        return True
    n = len(batch["pages"])
    base, fmt = None, args.format
    if ask_name:
        base, fmt = ask_save_path(args, f"{args.prefix}{batch['stamp']}")
        if base is None:
            # Cancelling the save dialog must not destroy the document; hand
            # control back so the prompt can reappear.
            log("save cancelled — the document is still open")
            return False
    log(f"finishing document: {n} page(s), format {fmt}")
    try:
        write_out(batch["pages"], args, calib, stamp=batch["stamp"],
                  base=base, fmt=fmt)
    except Exception as e:
        log(f"[!] could not write the document: {e}")
    batch["pages"] = []
    batch["stamp"] = None
    # Release the retained frame: it is only useful while its page is in the
    # open document, and it is 74 MB.
    STATE["last_raw"] = None
    STATE["last_count"] = 0
    return True


def main():
    ap = argparse.ArgumentParser(
        description="Scan when the SV600's button is pressed.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--outdir", default=".", help="where to write scans (default .)")
    ap.add_argument("--dry-run", action="store_true",
                    help="log button presses but do not scan (safe first run)")
    ap.add_argument("--full", action="store_true",
                    help="scan the WHOLE area instead of detecting a page — for "
                         "a book spread, or anything that is not a single sheet. "
                         "Skips page detection, cropping and paper snapping")
    ap.add_argument("--area", type=S._area_arg, metavar="X0,Y0,X1,Y1",
                    help="with --full, the millimetre rectangle to output "
                         "(default: Ricoh's 432x300mm maximum, centred)")
    ap.add_argument("--pages", type=int, default=1, metavar="N",
                    help="sheets on the mat (default 1). Ricoh's rule for the "
                         "SV600 is at most 10 documents, at least 15mm apart — "
                         "note two A4 PORTRAIT sheets need 210+15+210 = 435mm, "
                         "which exceeds the 432mm scan width, so a full-width "
                         "A4 pair must be butted together and read as one A3 "
                         "(--paper a3) rather than split with --pages 2")
    ap.add_argument("--batch", type=float, default=0.0, metavar="SEC",
                    help="MULTI-PAGE MODE: successive presses become ONE "
                         "document. Turn the page, press, repeat; after SEC with "
                         "no press the pages are combined into a single "
                         "searchable PDF. Try 20. Default 0 = off, one file per "
                         "press")
    ap.add_argument("--prompt", action="store_true",
                    help="with --batch, show a 'Continue / Finish & Save' dialog "
                         "after each page (zenity). Pressing the scan button "
                         "adds the next page and replaces the dialog, so you "
                         "never have to click to continue — only to finish")
    ap.add_argument("--save-as", action="store_true",
                    help="with --prompt, 'Finish and save' opens a file-save "
                         "dialog to name the document instead of using the "
                         "timestamp. Cancelling it leaves the document open")
    ap.add_argument("--no-preview", action="store_true",
                    help="do not show a thumbnail of the last scanned page in "
                         "the prompt dialog")
    ap.add_argument("--prefix", default="scan-", metavar="STR",
                    help="output filename prefix (default 'scan-'); files are "
                         "<prefix><timestamp>.<ext>")
    ap.add_argument("--double-press", type=float, default=0.0, metavar="SEC",
                    help="with --batch, press twice within this many seconds to "
                         "finish the document immediately instead of waiting out "
                         "--batch (try 0.6). DEFAULT 0 = OFF, because enabling it "
                         "delays every page by this long while we wait to see "
                         "whether a second press follows. A hold gesture is not "
                         "possible: the button flag is a latched one-shot event, "
                         "so holding looks identical to tapping (measured: 1 poll "
                         "either way at 50ms)")
    ap.add_argument("--join", action="store_true", help="with --pages, one wide image")
    ap.add_argument("--paper", default="auto",
                    choices=["auto", "none"] + sorted(S.PAPER))
    ap.add_argument("--dpi", type=float, default=None)
    ap.add_argument("--calibration", metavar="FILE")
    ap.add_argument("--no-white-balance", "--no-color", dest="no_color",
                    action="store_true")
    ap.add_argument("--no-ca", action="store_true")
    ap.add_argument("--no-sharpen", action="store_true")
    ap.add_argument("--sharpen", type=int, default=140, metavar="PCT")
    sv600_output.add_args(ap)
    ap.add_argument("--timeout", type=float, default=30.0)
    ap.add_argument("--poll", type=float, default=POLL_S,
                    help=f"poll interval (default {POLL_S}, as the official software)")
    args = ap.parse_args()
    # process_pages() expects these; the watcher never uses the no-geometry paths.
    args.no_dewarp = False
    args.no_crop = False

    # Check writability UP FRONT. Otherwise the first failure comes after a
    # 17-second scan, at the write, and the scanned pages are discarded — which
    # is exactly what happened when ~/Scans had been created root-owned by an
    # earlier sudo run and the service then ran as an ordinary user.
    STATE["mode"] = 1 if args.full else 0    # --full seeds the mode to "book"
    try:
        os.makedirs(args.outdir, exist_ok=True)
    except OSError as e:
        sys.exit(f"Cannot create --outdir {args.outdir}: {e}")
    if not os.access(args.outdir, os.W_OK | os.X_OK):
        import pwd
        try:
            owner = pwd.getpwuid(os.stat(args.outdir).st_uid).pw_name
        except Exception:
            owner = "?"
        sys.exit(
            f"--outdir {args.outdir} is not writable (owned by {owner}, "
            f"running as {pwd.getpwuid(os.getuid()).pw_name}).\n"
            f"If an earlier run under sudo created it:\n"
            f"    sudo chown -R $USER:$USER {args.outdir}")
    calib = S.load_calibration(args.calibration)
    # Scratch space for dialog preview thumbnails. Not in --outdir: these are
    # transient UI artefacts, not scans, and must never be mistaken for output.
    STATE["tmpdir"] = tempfile.mkdtemp(prefix="sv600-watch-")

    dev = S.open_dev()
    scans = 0
    # Defined before the try so the finally can always flush a pending document,
    # including when the failure happens before the main loop is reached.
    batch = {"pages": [], "stamp": None}
    last_page = 0.0
    dialog = None
    try:
        log("waking scanner ...")
        if not S.wake(dev):
            sys.exit("Scanner did not answer TEST UNIT READY.")
        log(f"watching for button presses (poll {args.poll:.2f}s). Ctrl-C to stop.")
        log(f"mode: {mode_label()}")
        if cur_mode()[0].startswith("book") or cur_mode()[0] == "cover":
            log("books: scan in 'Book, raw spread', then sv600_prep.py the "
                "folder and flatten in ScanTailor Advanced. In-daemon flatten "
                "is experimental.")
        if args.dry_run:
            log("DRY RUN — presses are logged, nothing is scanned.")

        was_down = False
        down_at = 0.0
        up_polls = 0
        errors = 0
        while True:
            # The prompt is the primary way a document ends. Checked before the
            # button so a click that has already landed is honoured immediately.
            if dialog is not None:
                if dialog.poll() is not None:
                    choice = prompt_result(dialog)
                    dialog = None
                    if choice == "mode":
                        # Pick any mode from a radiolist, then RE-RENDER the page
                        # already captured and reopen so the new preview shows at
                        # once. The 17s scan is the expensive part; re-rendering
                        # (even with dewarp) is not, so this never costs a rescan.
                        picked = pick_mode()
                        if picked is not None and picked != STATE["mode"]:
                            STATE["mode"] = picked
                            log(f"mode -> {mode_label()}")
                            rerender_last(batch, args, calib)
                        dialog = show_prompt(batch, args)
                        continue
                    if choice == "finish":
                        log("prompt: finish and save")
                        if not finish_batch(batch, args, calib,
                                            ask_name=args.save_as):
                            dialog = show_prompt(batch, args)
                    elif choice == "next":
                        log(f"prompt: scan next page -> page "
                            f"{len(batch['pages']) + 1}")
                        # Re-offer either way: a failed scan must not silently
                        # abandon a document that already has pages.
                        if add_page(dev, args, calib, batch):
                            last_page = time.time()
                        dialog = show_prompt(batch, args)
                    elif choice == "redo":
                        if batch["pages"]:
                            batch["pages"].pop()
                            log(f"prompt: deleted last page — "
                                f"{len(batch['pages'])} left")
                        last_page = time.time()
                        if batch["pages"]:
                            dialog = show_prompt(batch, args)
                        else:
                            batch["stamp"] = None
                            log("document is now empty — press the scan button "
                                "to start again")
                    elif choice == "discard":
                        n = len(batch["pages"])
                        if confirm_discard(n):
                            batch["pages"] = []
                            batch["stamp"] = None
                            log(f"prompt: discarded {n} page(s)")
                        else:
                            log("prompt: discard cancelled")
                            dialog = show_prompt(batch, args)
                    else:
                        log("prompt: closed — the document is still open; press "
                            "the scan button to add to it, or wait for the "
                            "timeout to save")

            # Timeout is the safety net, not the mechanism: it closes a document
            # that was abandoned. Timing from the last PAGE (not the last poll)
            # is what makes "turn the page, press, repeat" work unattended.
            if (batch["pages"] and args.batch > 0
                    and time.time() - last_page >= args.batch):
                log(f"no page for {args.batch:.0f}s — closing the document")
                prompt_close(dialog)
                dialog = None
                finish_batch(batch, args, calib)
            down = poll_button(dev)
            if down is None:
                errors += 1
                if errors >= 25:
                    log("[!] too many consecutive USB errors — exiting.")
                    break
                time.sleep(args.poll)
                continue
            errors = 0

            # The flag stays set for as long as the button is held (~1.15 s for
            # an ordinary press in the capture), so the press DURATION is
            # readable. Ricoh use the same distinction: a normal press scans
            # once, holding the Scan button "for 2 seconds or longer" arms
            # page-turning detection. Hence: act on the FALLING edge, and use
            # how long the flag was up to pick which action to take. (Acting on
            # the rising edge would make a long press indistinguishable.)
            if down and not was_down:
                down_at = time.time()
                up_polls = 1
            elif down and was_down:
                up_polls += 1
            elif was_down and not down:
                held = time.time() - down_at
                scans += 1
                # up_polls stays as a diagnostic: it is what proved the flag is
                # one-shot (always 1, however long the button is held).
                double = (args.batch > 0 and args.double_press > 0
                          and wait_second_press(dev, args))
                page = (f"page {len(batch['pages']) + 1}"
                        if args.batch > 0 else "scan")
                log(f"BUTTON pressed ({up_polls} poll"
                    f"{'s' if up_polls != 1 else ''}, {held:.2f}s) -> {page}"
                    f"{' [DOUBLE: finish document]' if double else ''}")
                # A press supersedes an open prompt: pressing the button IS
                # "continue", so the user never has to click anything to add a
                # page — exactly as the official software behaves.
                prompt_close(dialog)
                dialog = None
                if not args.dry_run:
                    if args.batch > 0:
                        if add_page(dev, args, calib, batch):
                            last_page = time.time()
                        if double:
                            finish_batch(batch, args, calib)
                        elif batch["pages"]:
                            dialog = show_prompt(batch, args)
                    else:
                        got = run_one(dev, args, calib)
                        if got:
                            write_out(got, args, calib)
                # Drain, so the press that started this cannot retrigger it.
                while poll_button(dev):
                    time.sleep(args.poll)
                was_down = False
                time.sleep(args.poll)
                continue
            was_down = down
            time.sleep(args.poll)
    except KeyboardInterrupt:
        print()
        log("stopping.")
    finally:
        # Never lose a half-finished document to Ctrl-C.
        try:
            prompt_close(dialog)
            finish_batch(batch, args, calib)
        except Exception as e:
            log(f"[!] could not finish the pending document: {e}")
        try:
            if STATE["setup_done"]:
                log("teardown ...")
                S.teardown(dev, verbose=False)
            else:
                log("no scan ran — skipping teardown.")
        except Exception as e:
            log(f"[!] teardown failed: {e}")
        try:
            usb.util.release_interface(dev, 0)
        except Exception:
            pass
        if STATE.get("tmpdir"):
            shutil.rmtree(STATE["tmpdir"], ignore_errors=True)
        log(f"done, {scans} press(es) handled.")


if __name__ == "__main__":
    main()
