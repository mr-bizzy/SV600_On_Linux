#!/usr/bin/env python3
"""
make_calchart.py — dense calibration target for the SV600 (A4).

The general-purpose test chart carries only ~45 grid dots, with exclusion holes
around its other features, and they span just the middle of the page. That is
too thin and too unevenly spread to fit the scanner's non-projective distortion:
a cubic needs 10 coefficients per axis, and the page bottom — where the residual
distortion actually shows — ends up extrapolated rather than fitted.

This target is nothing but fiducials and a dense, even dot lattice covering the
full page, so the model is heavily over-determined everywhere it is used.

    python3 make_calchart.py          -> sv600-calchart-a4.pdf + .json
"""
import json
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm

W_MM, H_MM = 210.0, 297.0
PITCH = 10.0                 # dot spacing
DOT = 2.0                    # dot diameter (~24 px at 300 dpi)
MARGIN = 4.0                 # dots must cover the FULL sheet: a page
                             # edge outside the fitted field gets extrapolated,
                             # and a cubic diverges by 5-20mm out there
# No fiducials: the calibrator anchors via the page quad + lattice walk, so big
# black squares would only punch holes in the coverage where it is needed most.
FID = []
FID_SIZE = 10.0


def y(v):
    return (H_MM - v) * mm


def main():
    c = canvas.Canvas("sv600-calchart-a4.pdf", pagesize=A4)
    c.setTitle("ScanSnap SV600 dense calibration target")
    truth = {"page_mm": [W_MM, H_MM], "units": "mm, origin top-left",
             "pitch_mm": PITCH, "dot_diameter_mm": DOT}

    # Label sits in the extreme top-left only, to break 180-degree symmetry.
    # It is dense text, so the isolation filter in the calibrator rejects it.
    c.setFillGray(0)
    c.setFont("Helvetica", 5)
    c.drawString(4 * mm, y(2.5), "SV600 CAL TOP-LEFT - print 100%, lay flat")

    truth["fiducials_mm"] = []
    for i, (fx, fy) in enumerate(FID):
        c.setFillGray(0)
        c.rect((fx - FID_SIZE / 2) * mm, y(fy + FID_SIZE / 2),
               FID_SIZE * mm, FID_SIZE * mm, stroke=0, fill=1)
        if i == 0:                                  # orientation key
            c.setFillGray(1)
            c.rect((fx - 2) * mm, y(fy + 2), 4 * mm, 4 * mm, stroke=0, fill=1)
        truth["fiducials_mm"].append([fx, fy])

    # Explicit edge rows/columns: a plain pitch loop stops short of the far edge
    # (leaving 13mm uncovered at the bottom), and any page area outside the dot
    # field has to be extrapolated — which is where the distortion showed.
    def axis(lo, hi):
        vals = []
        v = lo
        while v <= hi + 1e-6:
            vals.append(round(v, 2)); v += PITCH
        if hi - vals[-1] > 2.0:
            vals.append(round(hi, 2))
        return vals

    c.setFillGray(0)
    dots = []
    for gx in axis(MARGIN, W_MM - MARGIN):
        for gy in axis(MARGIN, H_MM - MARGIN):
            if any(abs(gx - fx) < 12 and abs(gy - fy) < 12 for fx, fy in FID):
                continue
            c.circle(gx * mm, y(gy), DOT / 2 * mm, stroke=0, fill=1)
            dots.append([gx, gy])
    truth["dots_mm"] = dots

    c.showPage()
    c.save()
    with open("sv600-calchart-a4.json", "w") as f:
        json.dump(truth, f, indent=1)
    print(f"wrote sv600-calchart-a4.pdf and .json ({len(dots)} dots, "
          f"{PITCH:.0f}mm pitch, spanning the full page)")


if __name__ == "__main__":
    main()
