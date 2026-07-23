#!/usr/bin/env python3
"""
unwarp.py — batch document-unwarping with PaddleOCR's UVDoc, inside the
container. Runs over every image in an input directory and writes a flattened
copy to the output directory.

UVDoc flattens the WHOLE spread well in one pass — feed it the raw, untrimmed
--full spread so it has margin to work with (trimming tight first pushes the
near-spine page numbers out of frame). Then --split cuts the FLATTENED result
into two pages: after flattening the gutter is a straight vertical line, so the
cut is clean. Splitting before unwarping instead chops the near-spine text off
each page. --split also trims the dark mat border from each page (--no-trim to
keep it).

    unwarp.py IN_DIR OUT_DIR [--split] [--no-trim] [--ext .png]
"""
import argparse
import os
import sys

import numpy as np
from PIL import Image

Image.MAX_IMAGE_PIXELS = None


def split_flat(arr, overlap_frac=0.04):
    """Cut a FLATTENED spread into (left, right) at the gutter, with overlap.

    Splitting happens AFTER unwarping, not before: once UVDoc has flattened the
    spread the gutter is a straight vertical line, so a straight cut is exactly
    right. Cutting first, on the curved image, chopped the near-spine text off
    each page.

    Each half extends OVERLAP past the cut, because the darkest column is the
    spine SHADOW, which sits just inside the true page boundary — a bare cut
    there clips the last letters of the near-spine lines. The overlap keeps each
    page's own inner text whole; the small duplicated gutter strip is harmless
    (it is mostly margin, and trim_dark removes most of it anyway)."""
    g = arr.max(2).astype(np.float32) if arr.ndim == 3 else arr.astype(np.float32)
    h, w = g.shape
    lo, hi = w // 3, 2 * w // 3
    col = g[:, lo:hi].mean(0)
    x = lo + int(np.argmin(col))
    if col.max() - col.min() < 0.12 * max(col.mean(), 1.0):
        bright = g > max(g.mean(), 60)
        xs = np.nonzero(bright.any(0))[0]
        x = (int(xs.min()) + int(xs.max())) // 2 if len(xs) else w // 2
    ov = int(w * overlap_frac)
    return arr[:, :min(x + ov, w)], arr[:, max(x - ov, 0):]


def trim_dark(arr, margin=40):
    """Crop the dark mat / wedge border off a flattened page.

    UVDoc keeps the black scanner mat around the book; a page for OCR should not.
    Crop to the largest bright region's bbox plus a small margin."""
    g = arr.max(2) if arr.ndim == 3 else arr
    ds = 8
    small = g[::ds, ::ds]
    thr = max(int(small.mean()), 55)
    m = _largest_cc(small > thr)
    ys, xs = np.nonzero(m)
    if len(xs) < 30:
        return arr
    x0 = max(int(xs.min()) * ds - margin, 0)
    y0 = max(int(ys.min()) * ds - margin, 0)
    x1 = min((int(xs.max()) + 1) * ds + margin, arr.shape[1])
    y1 = min((int(ys.max()) + 1) * ds + margin, arr.shape[0])
    return arr[y0:y1, x0:x1]


def _largest_cc(mask):
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


def unwarp_batch(inputs, outdir, model, split, trim, ext):
    written = 0
    for path in inputs:
        name = os.path.splitext(os.path.basename(path))[0]
        try:
            im = Image.open(path).convert("RGB")
        except Exception as e:
            print(f"[!] {path}: cannot read ({e})", flush=True)
            continue
        try:
            results = model.predict(np.asarray(im), batch_size=1)
        except Exception as e:
            print(f"[!] {name}: unwarp failed ({e})", flush=True)
            continue
        flat = _result_image(results)
        if flat is None:
            print(f"[!] {name}: no image in result", flush=True)
            continue
        # Flatten first, THEN split (clean straight gutter), THEN trim the mat.
        pages = list(zip(("-1", "-2"), split_flat(flat))) if split \
            else [("", flat)]
        for suffix, page in pages:
            if trim:
                page = trim_dark(page)
            dst = os.path.join(outdir, f"{name}{suffix}{ext}")
            Image.fromarray(page).save(dst)
            print(f"[*] {os.path.basename(path)}{suffix} -> {dst} "
                  f"({page.shape[1]}x{page.shape[0]})", flush=True)
            written += 1
    return written


def _result_image(results):
    """Extract the rectified RGB array from a PaddleOCR predict() result."""
    for res in results:
        # dict-like access used by TextImageUnwarping results
        for key in ("doctr_img", "rectified_img", "output_img", "img"):
            try:
                v = res[key]
            except Exception:
                v = getattr(res, key, None)
            if v is not None:
                a = np.asarray(v)
                if a.ndim == 3:
                    return a.astype(np.uint8)
    return None


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("indir")
    ap.add_argument("outdir")
    ap.add_argument("--split", action="store_true",
                    help="split each flattened spread into two pages (done AFTER "
                         "unwarping, so the gutter is a clean vertical line)")
    ap.add_argument("--no-trim", action="store_true",
                    help="keep the dark mat border (default: crop it off)")
    ap.add_argument("--ext", default=".png", help="output extension (default .png)")
    ap.add_argument("--model", default="UVDoc")
    args = ap.parse_args()

    os.makedirs(args.outdir, exist_ok=True)
    exts = (".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp")
    inputs = sorted(os.path.join(args.indir, f) for f in os.listdir(args.indir)
                    if f.lower().endswith(exts))
    if not inputs:
        sys.exit(f"No images in {args.indir}")
    print(f"[*] {len(inputs)} image(s); loading {args.model} ...", flush=True)

    from paddleocr import TextImageUnwarping
    model = TextImageUnwarping(model_name=args.model)

    n = unwarp_batch(inputs, args.outdir, model, args.split,
                     not args.no_trim, args.ext)
    print(f"\n[*] {n} page(s) written to {args.outdir}", flush=True)


if __name__ == "__main__":
    main()
