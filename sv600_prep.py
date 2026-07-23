#!/usr/bin/env python3
"""
sv600_prep.py — prepare raw SV600 book spreads for a ScanTailor batch.

Book flattening on Linux is done by ScanTailor Advanced (github.com/4lex4/
scantailor-advanced — actively maintained, Dec 2025 release), NOT by this
project: automatic dewarping of a real thick book failed here, and ScanTailor's
per-page manual-correctable mesh is what actually works. This script produces
clean INPUT for it.

Take the raw whole-mat spreads from `sv600_scan.py --full` (or the daemon's
"Book, raw spread" mode) and, for each:

  * DESKEW to upright — a book laid a few degrees off square is rotated back, so
    ScanTailor's split and deskew stages start from straight pages;
  * TRIM the surround — the black scanner-base wedges that --full leaves in the
    frame corners, and the excess dark mat, are cropped away, because a big
    black border confuses ScanTailor's content detection;
  * write sequential lossless TIFFs — spread-0001.tif, spread-0002.tif ... into
    one folder, at the right DPI, which ScanTailor batch-opens in order.

USAGE
-----
    ./sv600_prep.py ~/Scans/scan-*.png ~/book-batch/        # last arg is OUTDIR
    ./sv600_prep.py --start 5 spread.png ~/book-batch/       # continue numbering
    ./sv600_prep.py --no-deskew --margin 8 in/*.png out/

Then open the OUTDIR in ScanTailor Advanced as a project.
"""
import argparse
import os
import sys

import numpy as np


def _otsu(g):
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


def _largest_cc(mask):
    """Largest 4-connected True component of a small boolean mask.

    The book is the biggest bright blob. This drops disconnected bright patches
    the threshold also catches — the desk visible at the frame edges of a --full
    scan (~100 vs the page's ~200), which would otherwise blow the bounding box
    out to the whole frame."""
    h, w = mask.shape
    seen = np.zeros((h, w), bool)
    best = None
    ys, xs = np.nonzero(mask)
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
                if 0 <= ny < h and 0 <= nx < w and mask[ny, nx] and not seen[ny, nx]:
                    seen[ny, nx] = True
                    stack.append((ny, nx))
        if best is None or len(pts) > len(best):
            best = pts
    out = np.zeros((h, w), bool)
    if best:
        p = np.array(best)
        out[p[:, 0], p[:, 1]] = True
    return out


def book_mask(img, ds=8):
    """Downscaled boolean mask of the book: the largest bright region.

    Both the black scanner-base wedge (~0) and the dark mat (~15-23) fall below
    the page (~200). The desk seen at the very edges of a --full frame (~100) is
    above the threshold too, so the largest-connected-region step is what
    actually isolates the book from it."""
    g = img.max(2) if img.ndim == 3 else img
    small = g[::ds, ::ds]
    thr = max(_otsu(small) * 0.6, 45)
    return _largest_cc(small > thr)


def estimate_skew(mask, limit=10.0, step=0.5):
    """Angle (deg) that best squares the book up.

    A rectangle's axis-aligned bounding box is smallest when the rectangle is
    axis-aligned, so the angle minimising the bright mask's bbox area is the
    book's rotation. Cheap on the already-downscaled mask, and robust to the
    curved page edges because it is an area minimum, not an edge fit."""
    from PIL import Image
    m = Image.fromarray((mask * 255).astype(np.uint8))
    best_a, best_area = 0.0, None
    a = -limit
    while a <= limit + 1e-9:
        r = np.asarray(m.rotate(a, resample=Image.NEAREST, expand=True)) > 127
        ys, xs = np.nonzero(r)
        if len(xs):
            area = (xs.max() - xs.min() + 1) * (ys.max() - ys.min() + 1)
            if best_area is None or area < best_area:
                best_area, best_a = area, a
        a += step
    return best_a


def prep(img, margin_px=60, deskew=True, verbose=True, fill=(255, 255, 255)):
    """Deskew and trim one raw spread. Returns the prepared RGB array."""
    from PIL import Image
    ds = 8
    mask = book_mask(img, ds)                 # small (downscaled) book mask
    if deskew and mask.any():
        angle = estimate_skew(mask)
        if abs(angle) > 0.25:
            # White fill, not black: ScanTailor treats large black areas as
            # content; white reads as page margin and is ignored.
            img = np.asarray(Image.fromarray(img).rotate(
                angle, resample=Image.BICUBIC, expand=True, fillcolor=fill))
            # Rotate the MASK the same way, filled with FALSE, so the white
            # image-fill above is NOT counted as book and the crop stays tight.
            mimg = Image.fromarray((mask * 255).astype(np.uint8))
            mask = np.asarray(mimg.rotate(angle, resample=Image.NEAREST,
                                          expand=True)) > 127
            if verbose:
                print(f"    deskew {angle:+.1f} deg")
    ys, xs = np.nonzero(mask)
    if len(xs) < 50:
        if verbose:
            print("    [!] no bright book region found — leaving uncropped")
        return img
    # Mask is downscaled by ds; scale its bbox back and clamp to the (rotated)
    # image, which the mask matches in aspect after the shared rotation.
    sy, sx = img.shape[0] / mask.shape[0], img.shape[1] / mask.shape[1]
    x0 = max(int(xs.min() * sx) - margin_px, 0)
    y0 = max(int(ys.min() * sy) - margin_px, 0)
    x1 = min(int((xs.max() + 1) * sx) + margin_px, img.shape[1])
    y1 = min(int((ys.max() + 1) * sy) + margin_px, img.shape[0])
    if verbose:
        print(f"    trim -> {x1-x0}x{y1-y0} (from {img.shape[1]}x{img.shape[0]})")
    return img[y0:y1, x0:x1]


def read_dpi(path, default=300.0):
    from PIL import Image
    try:
        with Image.open(path) as im:
            d = im.info.get("dpi")
            if d and d[0]:
                return float(d[0])
    except Exception:
        pass
    return default


def main():
    ap = argparse.ArgumentParser(
        description=__doc__.split("\n")[1],
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("inputs", nargs="+",
                    help="raw --full spread image(s), then the OUTPUT DIRECTORY "
                         "as the final argument")
    ap.add_argument("--start", type=int, default=1,
                    help="first sequence number (default 1; use to continue a "
                         "batch)")
    ap.add_argument("--prefix", default="spread-",
                    help="output filename prefix (default 'spread-')")
    ap.add_argument("--margin", type=float, default=5.0, metavar="MM",
                    help="margin kept around the book when trimming (default 5)")
    ap.add_argument("--no-deskew", action="store_true",
                    help="skip deskew (leave ScanTailor to do it)")
    ap.add_argument("--dpi", type=float, default=None,
                    help="override DPI (default: read from each file, else 300)")
    ap.add_argument("--format", choices=["tiff", "png"], default="tiff",
                    help="output format (default tiff, ScanTailor's preferred)")
    args = ap.parse_args()

    if len(args.inputs) < 2:
        ap.error("need at least one input image and an output directory")
    *images, outdir = args.inputs
    if os.path.isdir(images[-1]) if images else False:
        ap.error("the OUTPUT DIRECTORY must be the LAST argument")
    os.makedirs(outdir, exist_ok=True)

    from PIL import Image
    Image.MAX_IMAGE_PIXELS = None            # a --full scan is ~30 MP
    ext = ".tif" if args.format == "tiff" else ".png"
    n = args.start
    written = 0
    for path in images:
        try:
            img = np.asarray(Image.open(path).convert("RGB"))
        except Exception as e:
            print(f"[!] {path}: cannot read ({e}) — skipping")
            continue
        dpi = args.dpi or read_dpi(path)
        print(f"[*] {os.path.basename(path)}  ({img.shape[1]}x{img.shape[0]}, {dpi:.0f}dpi)")
        out = prep(img, margin_px=int(args.margin * dpi / 25.4),
                   deskew=not args.no_deskew)
        dst = os.path.join(outdir, f"{args.prefix}{n:04d}{ext}")
        im = Image.fromarray(out)
        if args.format == "tiff":
            im.save(dst, "TIFF", compression="tiff_lzw",
                    resolution=float(dpi), resolution_unit=2)
        else:
            im.save(dst, "PNG", dpi=(dpi, dpi))
        print(f"    -> {dst}")
        n += 1
        written += 1

    print(f"\n[*] {written} spread(s) -> {outdir}")
    print(f"[*] Open that folder in ScanTailor Advanced as a New Project.")


if __name__ == "__main__":
    main()
