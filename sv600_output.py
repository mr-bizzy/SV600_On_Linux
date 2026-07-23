#!/usr/bin/env python3
"""
sv600_output.py — colour mode and output format for SV600 scans.

Shared by sv600_scan.py (one-shot CLI) and sv600_watch.py (button daemon) so
the two cannot drift apart.

COLOUR IS APPLIED LAST, IN SOFTWARE, AND DELIBERATELY SO
--------------------------------------------------------
The sensor always delivers interleaved RGB and the whole pipeline depends on
that: chromatic-aberration correction resamples R and B onto G, white balance
needs three channels, and page detection thresholds on max(R,G,B) because
saturated colour has a poor mean but a strong max. Converting to grey or bilevel
at the scanner would break all three. So we always capture and process in
colour, then convert at the very end — which also gives a better bilevel result
than the scanner could, because it is thresholding an already white-balanced,
deskewed, sharpened page.

RESOLUTION
----------
`--dpi` is honest about what it is: the rectifier renders the output page at
that many pixels per inch, resampling from the 300 dpi capture through the
polynomial map. 600 dpi therefore means a genuine 600 dpi *raster*, interpolated
from a 300 dpi *capture*. That is exactly what the official software does — a
matched-pair test against ScanSnap on Windows measured its "600 dpi" output as
2x interpolation with no additional real detail. Changing the sensor's own
sampling rate is a separate, unverified job: it means editing the SET WINDOW
payload (dpi at bytes 10-13), and WIDTH_PX/HEIGHT_PX, the READ(10) chunking and
the ILI residual arithmetic are all hardcoded for 300 dpi, while the CA
coefficients and the calibration are fitted in 300 dpi pixel units.
"""
import os
import shutil
import subprocess

import numpy as np

FORMATS = ("png", "jpeg", "tiff", "pdf", "pdf-ocr")
COLOR_MODES = ("color", "gray", "bw")

# Multi-page formats hold every page in ONE file; the rest write one file each.
MULTIPAGE = ("tiff", "pdf", "pdf-ocr")

_EXT = {"png": ".png", "jpeg": ".jpg", "tiff": ".tif",
        "pdf": ".pdf", "pdf-ocr": ".pdf"}

# Reverse map, for letting a typed filename choose the format. ".pdf" is
# deliberately ambiguous between pdf and pdf-ocr; format_from_ext() resolves it
# in favour of whatever the caller already had, so someone running pdf-ocr who
# types "report.pdf" still gets OCR.
_FROM_EXT = {".png": "png", ".jpg": "jpeg", ".jpeg": "jpeg",
             ".tif": "tiff", ".tiff": "tiff", ".pdf": "pdf"}


def format_from_ext(path, current=None):
    """Format implied by a filename's extension, or None if unrecognised."""
    fmt = _FROM_EXT.get(os.path.splitext(path)[1].lower())
    if fmt == "pdf" and current == "pdf-ocr":
        return "pdf-ocr"
    return fmt


def to_gray(img8):
    """RGB -> luminance, ITU-R BT.601 weights."""
    w = np.array([0.299, 0.587, 0.114], np.float32)
    return (img8.astype(np.float32) @ w).clip(0, 255).astype(np.uint8)


def to_bw(gray, block=151, offset=18):
    """Grey -> bilevel with a LOCAL threshold.

    Global thresholding (Otsu) is wrong for this scanner: the SV600 lights the
    page from above at an angle and brightness falls off measurably toward the
    bottom of the frame, so one threshold either fills the far end with black or
    washes the near end out. A local mean over a ~1/2 inch window at 300 dpi
    tracks that gradient. Same integral-image trick the dot detector uses."""
    g = gray.astype(np.float32)
    b = max(3, int(block) | 1)                 # odd
    pad = np.pad(g, b // 2, mode="edge")
    cs = np.cumsum(np.cumsum(pad, 0), 1)
    cs = np.pad(cs, ((1, 0), (1, 0)))
    loc = (cs[b:, b:] - cs[:-b, b:] - cs[b:, :-b] + cs[:-b, :-b]) / (b * b)
    loc = loc[:g.shape[0], :g.shape[1]]
    return g > (loc - offset)                  # True = white (paper)


def apply_color(img8, mode, bw_block=151, bw_offset=18):
    """Convert a processed RGB page to the requested colour mode.

    Returns a PIL image: RGB, L, or 1-bit."""
    from PIL import Image
    if mode == "color":
        return Image.fromarray(img8, "RGB")
    gray = to_gray(img8)
    if mode == "gray":
        return Image.fromarray(gray, "L")
    if mode == "bw":
        return Image.fromarray(to_bw(gray, bw_block, bw_offset)).convert("1")
    raise ValueError(f"unknown colour mode {mode!r}")


def _save_pdf(images, path, dpi):
    """Write a (possibly multi-page) PDF without OCR."""
    images[0].save(path, "PDF", resolution=float(dpi),
                   save_all=True, append_images=images[1:])


def _save_pdf_ocr(images, path, dpi, scanocr, lang="eng", log=print):
    """Write page images to a temp dir and hand them to scanocr."""
    import tempfile
    if not (scanocr and os.access(scanocr, os.X_OK)):
        log(f"[!] scanocr not available at {scanocr} — writing a plain PDF.")
        _save_pdf(images, path, dpi)
        return
    tmp = tempfile.mkdtemp(prefix="sv600-ocr-")
    try:
        pages = []
        for i, im in enumerate(images):
            p = os.path.join(tmp, f"page-{i + 1:04d}.png")
            im.save(p, dpi=(dpi, dpi))
            pages.append(p)
        subprocess.run([scanocr, "-l", lang, "-o", path, *pages], check=True)
    except subprocess.CalledProcessError as e:
        log(f"[!] scanocr failed ({e}) — writing a plain PDF instead.")
        _save_pdf(images, path, dpi)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def out_names(base, fmt, n):
    """Output path(s) for n pages. base is a path WITHOUT extension."""
    ext = _EXT[fmt]
    if fmt in MULTIPAGE or n <= 1:
        return [base + ext]
    return [f"{base}-{i + 1}{ext}" for i in range(n)]


def save(pages, base, fmt="png", color="color", dpi=300.0, jpeg_quality=90,
         bw_block=151, bw_offset=18, scanocr=None, lang="eng",
         chown_back=None, log=print):
    """Convert and write pages. Returns the paths written.

    pages: list of HxWx3 uint8 arrays (the processed RGB pages)
    base:  output path WITHOUT extension
    """
    if fmt not in FORMATS:
        raise ValueError(f"unknown format {fmt!r} (choose from {', '.join(FORMATS)})")
    if color not in COLOR_MODES:
        raise ValueError(f"unknown colour mode {color!r}")

    images = [apply_color(p, color, bw_block, bw_offset) for p in pages]
    paths = out_names(base, fmt, len(images))

    if fmt == "pdf":
        _save_pdf(images, paths[0], dpi)
    elif fmt == "pdf-ocr":
        _save_pdf_ocr(images, paths[0], dpi, scanocr, lang, log)
    elif fmt == "tiff":
        # G4 for bilevel is dramatically smaller and is what fax/document TIFF
        # readers expect; LZW is the safe lossless choice for grey and colour.
        comp = "group4" if color == "bw" else "tiff_lzw"
        images[0].save(paths[0], "TIFF", compression=comp,
                       resolution=float(dpi), resolution_unit=2,
                       save_all=True, append_images=images[1:])
    elif fmt == "jpeg":
        for im, p in zip(images, paths):
            # JPEG has no bilevel mode; 1-bit would be silently promoted and
            # then ruined by DCT ringing, so say so rather than produce mush.
            if im.mode == "1":
                log("[!] jpeg cannot store bilevel — saving grey instead.")
                im = im.convert("L")
            im.save(p, "JPEG", quality=int(jpeg_quality), dpi=(dpi, dpi))
    else:                                       # png
        for im, p in zip(images, paths):
            im.save(p, "PNG", dpi=(dpi, dpi))

    for p in paths:
        if chown_back:
            chown_back(p)
        try:
            log(f"[*] saved {p} ({os.path.getsize(p) / 1e6:.1f} MB)")
        except OSError:
            pass
    return paths


def add_args(ap, default_format="png"):
    """Register the shared output options on an ArgumentParser."""
    ap.add_argument("--format", "-f", default=default_format, choices=FORMATS,
                    help=f"output format (default {default_format}). tiff/pdf/"
                         "pdf-ocr hold all pages in one file; png/jpeg write one "
                         "file per page")
    ap.add_argument("--color", "-c", default="color", choices=COLOR_MODES,
                    help="colour mode (default color). Applied in software after "
                         "processing — the sensor is always RGB and the CA, white "
                         "balance and page detection stages all need it")
    ap.add_argument("--jpeg-quality", type=int, default=90, metavar="Q",
                    help="JPEG quality 1-95 (default 90)")
    ap.add_argument("--bw-block", type=int, default=151, metavar="PX",
                    help="bilevel local-threshold window (default 151 px, ~1/2in "
                         "at 300dpi)")
    ap.add_argument("--bw-offset", type=int, default=18, metavar="N",
                    help="bilevel threshold offset below the local mean "
                         "(default 18; raise to keep more ink, lower for cleaner "
                         "paper)")
    ap.add_argument("--ocr-lang", default=os.environ.get("SCANOCR_LANG", "eng"),
                    metavar="LANG", help="OCR language for pdf-ocr (default eng)")
    return ap
