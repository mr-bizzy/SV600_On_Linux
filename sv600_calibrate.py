#!/usr/bin/env python3
"""
sv600_calibrate.py — solve the SV600's image <-> millimetre mapping.

The camera is fixed relative to the document plane, so one mapping serves every
flat scan. Two findings drive the design:

  * The mapping is NOT projective. Fitted against 488 correctly-matched chart
    dots, a homography leaves 2.517 mm of error while a cubic leaves 0.206 mm
    (0.223 mm held-out) — an 11x difference. A homography-based rectification
    therefore cannot help distorting the page, worst where its four corners
    constrain it least. So we fit a POLYNOMIAL, in both directions.

  * Dots must be matched by LATTICE TOPOLOGY, not by predicted position. With a
    10 mm pitch and ~7 mm of initial model error, a nearest-truth search snaps
    many dots into the wrong cell and then converges confidently on nonsense.
    Walking the lattice needs no model at all.

Usage:
    python3 sv600_calibrate.py cal-raw.bin [-o sv600-calibration.json]

Input: a raw scan of sv600-calchart-a4.pdf (make_calchart.py), lying flat,
captured with sv600_scan.py --no-dewarp --no-crop --no-color --keep-raw.
"""
import argparse, json, os, sys
from collections import deque
import numpy as np

W, H = 5572, 4429
HERE = os.path.dirname(os.path.abspath(__file__))
DEGREE = 3          # cubic: 0.223mm held-out. deg4 fits better inside the dot
                    # field but extrapolates less safely to the page edges.


def otsu(v):
    h, _ = np.histogram(v, bins=256, range=(0, 256))
    h = h.astype(float)
    w0 = np.cumsum(h); w1 = h.sum() - w0
    mu = np.cumsum(h * np.arange(256))
    with np.errstate(invalid="ignore", divide="ignore"):
        b = (mu[-1] * w0 / h.sum() - mu) ** 2 / (w0 * w1)
    b[~np.isfinite(b)] = 0
    return int(np.argmax(b))


def homography(src, dst):
    A = []
    for (X, Y), (x, y) in zip(src, dst):
        A.append([X, Y, 1, 0, 0, 0, -x * X, -x * Y, -x])
        A.append([0, 0, 0, X, Y, 1, -y * X, -y * Y, -y])
    _, _, V = np.linalg.svd(np.array(A, float))
    return (V[-1] / V[-1][-1]).reshape(3, 3)


def poly_terms(x, y, deg):
    return np.stack([x ** i * y ** j
                     for i in range(deg + 1) for j in range(deg + 1 - i)], 1)


def fit_poly(src, dst, deg):
    A = poly_terms(src[:, 0], src[:, 1], deg)
    cx, *_ = np.linalg.lstsq(A, dst[:, 0], rcond=None)
    cy, *_ = np.linalg.lstsq(A, dst[:, 1], rcond=None)
    pred = np.stack([A @ cx, A @ cy], 1)
    return cx, cy, np.hypot(*(pred - dst).T)


def page_quad(gm):
    thr = 0.8 * float(np.median(gm[gm > otsu(gm)]))
    m = gm > thr
    k = 9
    c = np.cumsum(np.cumsum(m.astype(np.float32), 0), 1)
    c = np.pad(c, ((1, 0), (1, 0)))
    box = (c[k:, k:] - c[:-k, k:] - c[k:, :-k] + c[:-k, :-k]) / (k * k)
    m2 = np.zeros_like(m)
    m2[k // 2:k // 2 + box.shape[0], k // 2:k // 2 + box.shape[1]] = box > 0.6
    ys, xs = np.nonzero(m2)
    if len(xs) < 5000:
        return None
    cor = lambda fn: (lambda i: (float(xs[i]), float(ys[i])))(int(np.argmax(fn(xs, ys))))
    return [cor(lambda x, y: -(x + y)), cor(lambda x, y: x - y),
            cor(lambda x, y: x + y), cor(lambda x, y: y - x)]


def find_dots(G, quad):
    """Blob-scan the page for dots.

    Uses the GREEN channel: chromatic aberration displaces R and B by up to 12px
    at the page bottom, comparable to a dot's diameter there, so a grey average
    smears the far blobs badly (it lost 100+ dots). Threshold is LOCAL because
    paper brightness falls off toward the bottom of the frame."""
    x0, x1 = int(min(p[0] for p in quad)), int(max(p[0] for p in quad))
    y0, y1 = int(min(p[1] for p in quad)), int(max(p[1] for p in quad))
    sub = G[y0:y1, x0:x1]
    b = 151
    pad = np.pad(sub, b // 2, mode="edge")
    cs = np.cumsum(np.cumsum(pad.astype(np.float64), 0), 1)
    cs = np.pad(cs, ((1, 0), (1, 0)))
    loc = (cs[b:, b:] - cs[:-b, b:] - cs[b:, :-b] + cs[:-b, :-b]) / (b * b)
    loc = loc[:sub.shape[0], :sub.shape[1]]
    mask = sub < loc - 45

    seen = np.zeros_like(mask, bool)
    hh, ww = mask.shape
    dots = []
    for sy in range(hh):
        row = mask[sy]
        if not row.any():
            continue
        for sx in np.nonzero(row)[0]:
            if seen[sy, sx]:
                continue
            q = deque([(sy, sx)]); seen[sy, sx] = True; pts = []
            while q:
                y, x = q.popleft(); pts.append((y, x))
                for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    ny, nx = y + dy, x + dx
                    if 0 <= ny < hh and 0 <= nx < ww and mask[ny, nx] and not seen[ny, nx]:
                        seen[ny, nx] = True; q.append((ny, nx))
            p = np.array(pts); n = len(p)
            h_ = p[:, 0].max() - p[:, 0].min() + 1
            w_ = p[:, 1].max() - p[:, 1].min() + 1
            if (90 < n < 900 and 0.6 < w_ / h_ < 1.6 and 9 < w_ < 38
                    and 9 < h_ < 38 and n / (h_ * w_) > 0.55):
                dots.append((x0 + p[:, 1].mean(), y0 + p[:, 0].mean()))
    D = np.array(dots)
    if len(D) < 20:
        return D
    # Isolation filter: grid dots sit ~1 pitch apart while text glyphs are only
    # a few px apart, so this rejects the sheet's label without needing to know
    # where it is. Shape alone does not separate them — round glyphs pass.
    d2 = ((D[:, None, :] - D[None, :, :]) ** 2).sum(-1)
    np.fill_diagonal(d2, np.inf)
    nn = np.sqrt(d2.min(1))
    keep = nn > 0.5 * np.median(nn)
    if keep.sum() >= 20:
        D = D[keep]
    return D


def index_lattice(D, quad):
    """Assign integer (col,row) to each dot by walking the lattice."""
    TL, TR, BR, BL = [np.array(q, float) for q in quad]
    u = ((TR - TL) + (BR - BL)) / 2; u /= np.linalg.norm(u)
    v = ((BL - TL) + (BR - TR)) / 2; v /= np.linalg.norm(v)
    n = len(D)
    d2 = ((D[:, None, :] - D[None, :, :]) ** 2).sum(-1)
    np.fill_diagonal(d2, np.inf)
    # Robust local pitch: median of the 4 nearest, so a missing neighbour does
    # not inflate it. Pitch shrinks down-page (112px -> 84px) with perspective.
    pitch = np.median(np.sqrt(np.sort(d2, 1))[:, :4], 1)
    neigh = [[] for _ in range(n)]
    for i in range(n):
        r = 1.30 * pitch[i]
        for j in np.where(d2[i] < r * r)[0]:
            vec = D[j] - D[i]
            du, dv = float(vec @ u), float(vec @ v)
            maj, mnr = (du, dv) if abs(du) > abs(dv) else (dv, du)
            # Reject diagonals: they sit at 1.41x pitch, so a looser radius
            # reads them as axis steps and the walk self-conflicts (it produced
            # 438 duplicate indices before this check).
            if abs(mnr) > 0.45 * abs(maj):
                continue
            step = ((1 if du > 0 else -1, 0) if abs(du) > abs(dv)
                    else (0, 1 if dv > 0 else -1))
            neigh[i].append((j, step))
    seed = int(np.argmin(((D - D.mean(0)) ** 2).sum(1)))
    idx = {seed: (0, 0)}
    q = deque([seed])
    while q:
        i = q.popleft()
        ci, ri = idx[i]
        for j, (dc, dr) in neigh[i]:
            if j not in idx:
                idx[j] = (ci + dc, ri + dr)
                q.append(j)
    return idx, seed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("raw")
    ap.add_argument("-o", "--output", default=os.path.join(HERE, "sv600-calibration.json"))
    ap.add_argument("--truth", default=os.path.join(HERE, "sv600-calchart-a4.json"))
    ap.add_argument("--dpi", type=float, default=300.0)
    ap.add_argument("--degree", type=int, default=DEGREE)
    a = ap.parse_args()

    truth = json.load(open(a.truth))
    buf = open(a.raw, "rb").read()
    if len(buf) != W * H * 3:
        sys.exit(f"{a.raw}: expected {W*H*3:,} bytes, got {len(buf):,}")
    img = np.frombuffer(buf, np.uint8).reshape(H, W, 3)
    G = img[:, :, 1].astype(np.float32)
    gm = img.mean(2).astype(np.float32)

    quad = page_quad(gm)
    if quad is None:
        sys.exit("no page found")
    print("page corners:", [tuple(int(v) for v in q) for q in quad])

    D = find_dots(G, quad)
    print(f"detected {len(D)} dots (truth has {len(truth['dots_mm'])})")
    if len(D) < 50:
        sys.exit("too few dots — is this a scan of the dense cal chart?")

    idx, seed = index_lattice(D, quad)
    if len({v for v in idx.values()}) != len(idx):
        sys.exit("lattice walk produced duplicate indices — detection unreliable")
    print(f"indexed {len(idx)}/{len(D)} dots by lattice walk, no duplicates")

    # anchor indices onto true mm by trying small offsets
    T = np.array(truth["dots_mm"], float)
    pitch = truth["pitch_mm"]
    Hc = homography([(0, 0), (210, 0), (210, 297), (0, 297)], quad)
    Hi = np.linalg.inv(Hc)
    p = np.hstack([D, np.ones((len(D), 1))]) @ Hi.T
    approx = p[:, :2] / p[:, 2:3]
    tx, ty = np.unique(T[:, 0]), np.unique(T[:, 1])
    sx = tx[np.argmin(abs(tx - approx[seed][0]))]
    sy = ty[np.argmin(abs(ty - approx[seed][1]))]
    best = None
    for oc in range(-3, 4):
        for orr in range(-3, 4):
            mm, px = [], []
            for i, (ci, ri) in idx.items():
                mx, my = sx + (ci + oc) * pitch, sy + (ri + orr) * pitch
                if np.any((abs(T[:, 0] - mx) < 0.01) & (abs(T[:, 1] - my) < 0.01)):
                    mm.append([mx, my]); px.append(D[i].tolist())
            if best is None or len(mm) > len(best[0]):
                best = (np.array(mm), np.array(px))
    mm_a, px_a = best
    print(f"anchored: {len(mm_a)} dots matched to true positions")
    if len(mm_a) < 50:
        sys.exit("anchoring failed")

    S = a.dpi / 25.4
    Hh = homography(mm_a.tolist(), px_a.tolist())
    ph = np.hstack([mm_a, np.ones((len(mm_a), 1))]) @ Hh.T
    eh = np.hypot(*((ph[:, :2] / ph[:, 2:3]) - px_a).T)
    cx, cy, e = fit_poly(mm_a, px_a, a.degree)
    ix, iy, ei = fit_poly(px_a, mm_a, a.degree)       # reverse direction
    print(f"\nhomography     : {eh.mean()/S:.3f} mm mean, {eh.max()/S:.3f} max")
    print(f"poly deg {a.degree} mm->px: {e.mean()/S:.3f} mm mean, {e.max()/S:.3f} max")
    print(f"poly deg {a.degree} px->mm: {ei.mean():.3f} mm mean, {ei.max():.3f} max")

    calib = {
        "model": "poly", "degree": a.degree,
        "mm_to_px": [cx.tolist(), cy.tolist()],
        "px_to_mm": [ix.tolist(), iy.tolist()],
        "H_mm_to_px": Hh.tolist(),        # kept for fallback / size estimates
        "source_size": [W, H], "dpi": a.dpi,
        "fit_bounds_mm": [float(mm_a[:,0].min()), float(mm_a[:,1].min()),
                          float(mm_a[:,0].max()), float(mm_a[:,1].max())],
        "fit_bounds_px": [float(px_a[:,0].min()), float(px_a[:,1].min()),
                          float(px_a[:,0].max()), float(px_a[:,1].max())],
        "n_points": int(len(mm_a)),
        "residual_mm_mean": float(e.mean() / S),
        "residual_mm_max": float(e.max() / S),
        "homography_residual_mm_mean": float(eh.mean() / S),
        "source": os.path.basename(a.raw),
    }
    with open(a.output, "w") as f:
        json.dump(calib, f, indent=1)
    print(f"wrote {a.output}")


if __name__ == "__main__":
    main()
