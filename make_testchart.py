#!/usr/bin/env python3
"""
make_testchart.py — generate the SV600 calibration chart (A4, exact geometry).

Produces sv600-testchart-a4.pdf. Print at 100% / "Actual size" — NOT "fit to
page" — then scan it with sv600_scan.py --no-dewarp --no-color --keep-raw, and
the analysis can solve for:

  * per-channel registration   <- slanted-edge patches at 5 field positions
                                  (isolated + aperiodic, so edge localisation
                                  can't lock onto a repeating pattern the way
                                  it does on body text)
  * geometry / aspect ratio    <- corner fiducials + dot grid at known mm
  * resolving power            <- line-pair groups, 1..5 lp/mm
  * white balance / gamma      <- neutral step wedge
  * print scale sanity check   <- the 150 mm reference line

Every feature's true position in mm is emitted to sv600-testchart-a4.json so the
analysis has ground truth without re-deriving it from this file.
"""
import json
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm

W_MM, H_MM = 210.0, 297.0

# --- feature geometry (mm, top-left origin; converted on draw) ---------------
FIDUCIALS = [(18, 22), (192, 22), (18, 275), (192, 275)]   # TL is keyed (see draw)
FID_SIZE  = 10.0

SLANTED = [("TL", 48, 62), ("TR", 162, 62), ("C", 105, 150),
           ("BL", 48, 238), ("BR", 162, 238)]
SLANT_SIZE = 20.0
SLANT_ANGLE = 5.0          # degrees; off-axis so each edge is sampled subpixel

RULER_Y, RULER_X0, RULER_LEN = 34.0, 30.0, 150.0

LPMM = [1, 2, 3, 4, 5]     # line pairs per mm (300 dpi Nyquist ~= 5.9 lp/mm)
LP_Y_V, LP_Y_H = 92.0, 103.0
LP_W, LP_H = 22.0, 9.0
LP_X0 = 40.0

WEDGE_Y, WEDGE_H = 254.0, 14.0
WEDGE_X0, WEDGE_N, WEDGE_W = 25.0, 8, 20.0

GRID_SPACING, GRID_DOT = 20.0, 2.0
GRID_X = [25 + 20 * i for i in range(9)]      # 25..185
GRID_Y = [50 + 20 * i for i in range(11)]     # 50..250


def y(v):                       # top-left mm -> reportlab bottom-left points
    return (H_MM - v) * mm


def excluded(gx, gy):
    """Keep the dot grid clear of every measurement feature."""
    for _, sx, sy in SLANTED:
        if abs(gx - sx) < 25 and abs(gy - sy) < 25:
            return True
    for fx, fy in FIDUCIALS:
        if abs(gx - fx) < 16 and abs(gy - fy) < 16:
            return True
    if LP_Y_V - 8 < gy < LP_Y_H + LP_H + 8:
        return True
    if gy > WEDGE_Y - 8:
        return True
    if gy < RULER_Y + 10:
        return True
    return False


def main():
    out = "sv600-testchart-a4.pdf"
    c = canvas.Canvas(out, pagesize=A4)
    c.setTitle("ScanSnap SV600 calibration chart")
    truth = {"page_mm": [W_MM, H_MM], "units": "mm, origin top-left"}

    # --- instructions -------------------------------------------------------
    c.setFillGray(0)
    c.setFont("Helvetica-Bold", 10)
    c.drawCentredString(W_MM / 2 * mm, y(9), "ScanSnap SV600 calibration chart — A4")
    c.setFont("Helvetica", 7.5)
    c.drawCentredString(W_MM / 2 * mm, y(14),
                        "PRINT AT 100% / ACTUAL SIZE — do not use 'Fit to page'. "
                        "Print in grayscale/black-only if your printer offers it.")

    # --- corner fiducials; top-left is keyed with a white inset for orientation
    truth["fiducials_mm"] = []
    for i, (fx, fy) in enumerate(FIDUCIALS):
        c.setFillGray(0)
        c.rect((fx - FID_SIZE / 2) * mm, y(fy + FID_SIZE / 2),
               FID_SIZE * mm, FID_SIZE * mm, stroke=0, fill=1)
        if i == 0:                                  # orientation key
            c.setFillGray(1)
            c.rect((fx - 2) * mm, y(fy + 2), 4 * mm, 4 * mm, stroke=0, fill=1)
        truth["fiducials_mm"].append([fx, fy])

    # --- 150 mm reference line ---------------------------------------------
    c.setFillGray(0)
    c.setLineWidth(0.4 * mm)
    c.line(RULER_X0 * mm, y(RULER_Y), (RULER_X0 + RULER_LEN) * mm, y(RULER_Y))
    c.setLineWidth(0.15 * mm)
    for t in range(0, int(RULER_LEN) + 1, 10):
        h = 3.0 if t % 50 else 5.0
        c.line((RULER_X0 + t) * mm, y(RULER_Y),
               (RULER_X0 + t) * mm, y(RULER_Y - h))
    c.setFont("Helvetica", 6.5)
    c.drawCentredString((RULER_X0 + RULER_LEN / 2) * mm, y(RULER_Y + 4),
                        "this line is exactly 150 mm — measure it after printing")
    truth["ruler"] = {"y_mm": RULER_Y, "x0_mm": RULER_X0, "length_mm": RULER_LEN}

    # --- slanted-edge patches ----------------------------------------------
    truth["slanted_mm"] = []
    for name, sx, sy in SLANTED:
        c.saveState()
        c.translate(sx * mm, y(sy))
        c.rotate(SLANT_ANGLE)
        c.setFillGray(0)
        c.rect(-SLANT_SIZE / 2 * mm, -SLANT_SIZE / 2 * mm,
               SLANT_SIZE * mm, SLANT_SIZE * mm, stroke=0, fill=1)
        c.restoreState()
        c.setFillGray(0)
        c.setFont("Helvetica", 6)
        # label above the patch — below would collide with the wedge on BL/BR
        c.drawCentredString(sx * mm, y(sy - SLANT_SIZE / 2 - 3), name)
        truth["slanted_mm"].append(
            {"name": name, "cx": sx, "cy": sy,
             "size": SLANT_SIZE, "angle_deg": SLANT_ANGLE})

    # --- line-pair resolution groups ---------------------------------------
    truth["linepairs"] = []
    for i, lp in enumerate(LPMM):
        x0 = LP_X0 + i * (LP_W + 5)
        period = 1.0 / lp
        c.setFillGray(0)
        # vertical bars -> measures horizontal resolution
        n = int(LP_W / period)
        for k in range(n):
            c.rect((x0 + k * period) * mm, y(LP_Y_V + LP_H),
                   period / 2 * mm, LP_H * mm, stroke=0, fill=1)
        # horizontal bars -> measures vertical resolution
        n = int(LP_H / period)
        for k in range(n):
            c.rect(x0 * mm, y(LP_Y_H + k * period + period / 2),
                   LP_W * mm, period / 2 * mm, stroke=0, fill=1)
        c.setFont("Helvetica", 5.5)
        c.drawCentredString((x0 + LP_W / 2) * mm, y(LP_Y_V - 2), f"{lp} lp/mm")
        truth["linepairs"].append({"lp_per_mm": lp, "x0": x0, "w": LP_W,
                                   "y_vertical_bars": LP_Y_V,
                                   "y_horizontal_bars": LP_Y_H, "h": LP_H})

    # --- neutral step wedge -------------------------------------------------
    truth["wedge"] = []
    for i in range(WEDGE_N):
        g = 1.0 - i / (WEDGE_N - 1)          # 1.0 (white) -> 0.0 (black)
        x0 = WEDGE_X0 + i * WEDGE_W
        c.setFillGray(g)
        c.rect(x0 * mm, y(WEDGE_Y + WEDGE_H), WEDGE_W * mm, WEDGE_H * mm,
               stroke=0, fill=1)
        truth["wedge"].append({"x0": x0, "y": WEDGE_Y, "w": WEDGE_W,
                               "h": WEDGE_H, "gray": round(g, 4)})
    c.setStrokeGray(0)
    c.setLineWidth(0.2 * mm)
    c.rect(WEDGE_X0 * mm, y(WEDGE_Y + WEDGE_H),
           WEDGE_N * WEDGE_W * mm, WEDGE_H * mm, stroke=1, fill=0)

    # --- dot grid -----------------------------------------------------------
    c.setFillGray(0)
    truth["dots_mm"] = []
    for gx in GRID_X:
        for gy in GRID_Y:
            if excluded(gx, gy):
                continue
            c.circle(gx * mm, y(gy), GRID_DOT / 2 * mm, stroke=0, fill=1)
            truth["dots_mm"].append([gx, gy])
    truth["dot_diameter_mm"] = GRID_DOT

    c.showPage()
    c.save()

    with open("sv600-testchart-a4.json", "w") as f:
        json.dump(truth, f, indent=1)
    print(f"wrote {out}")
    print(f"wrote sv600-testchart-a4.json  "
          f"({len(truth['dots_mm'])} dots, {len(SLANTED)} slanted patches)")


if __name__ == "__main__":
    main()
