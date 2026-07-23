# Scanning a book on the SV600 (Linux)

The SV600 was built for books — it photographs a spread laid flat on the mat.
The scan itself is easy; **flattening** the page curvature is the work, and the
tool for that is **ScanTailor Advanced**, not this project. This project's job is
to capture clean spreads and hand them off.

The pipeline:

```
press the scan button   →   raw spread (PNG)
        ↓  sv600_prep.py  (deskew + trim the mat)
      ScanTailor Advanced  (split · dewarp · threshold · export)
        ↓
      flat, readable pages
```

`./sv600_book.sh IN_DIR` does the middle two steps for you: preps every raw
spread in `IN_DIR` and opens the result in ScanTailor.

---

## 1. Capture

Run the button watcher in **Book, raw spread** mode, PNG output, one file per
press:

```
systemctl --user start sv600-watch      # if not already running
```

In the on-screen prompt: **Change mode → Book, raw spread (for ScanTailor)**,
and make sure the output format is PNG. Then, per spread:

* lay the open book flat on the mat, spine roughly vertical, centred;
* press the scan button;
* turn the page, repeat.

Don't use the daemon's in-daemon "flatten" modes for real books — they are
experimental and lose near-spine text. Raw spread → ScanTailor is the path.

## 2. Prep + open (`sv600_book.sh`)

```
./sv600_book.sh ~/Scans                  # preps ~/Scans/*.png, opens ScanTailor
./sv600_book.sh ~/Scans ~/book-batch     # explicit output folder
```

Prep deskews each spread and trims the black mat/wedge border, so ScanTailor's
content detection locks onto the page instead of the mat — this is what removes
the black edge bands you get otherwise.

## 3. ScanTailor — only these stages matter

ScanTailor has 8 stages; for SV600 book scans you only tune two. Set them on the
first page, then **click the ▶▶ (all pages) button** on each stage so they apply
to the whole book, and review.

| Stage | What to do |
|---|---|
| Fix Orientation | leave as-is |
| **Split Pages** | **the important one.** Choose two-page layout; drag the split line onto the gutter. Auto-detect is usually close. |
| Deskew | Auto (already roughly done by the prep) |
| **Select Content** | drag the content box to **exclude any black mat/spine band** at the edges — this fixes the black artefacts |
| Margins | set uniform margins so all pages match |
| **Output** | tick **Dewarping** (the curvature fix). Choose **Black & White** for text; **Color/Grayscale** for pages with photos. 300–600 dpi. |

The two that earn their keep: **Split Pages** and **Output → Dewarping**. The
rest are defaults.

## 4. Export

ScanTailor writes one image per page into an `out/` folder. Bundle them into a
searchable PDF with this project's OCR wrapper:

```
./scanocr ~/book-batch/out/*.tif -o book.pdf
```

---

## Notes / honesty

* **This is the manual path.** ScanTailor has no batch API, so each book needs a
  pass — but settings propagate across pages, so it is mostly *reviewing*, not
  configuring, after the first page.
* **Learning curve is real but shallow.** Once Split Pages and Output→Dewarping
  are set, the rest is clicking through.
* **Alternatives that did NOT win** (see the project notes): `sv600_dewarp.py`
  (this project's own dewarper) fails on thick books; the UVDoc Docker unwarper
  (`sv600_unwarp.sh`) is automatic and decent but leaves mat/facing-page/warp
  blemishes. ScanTailor gives the cleanest result for the effort.
* For **flat single sheets** (documents, letters, receipts) you do NOT need any
  of this — the calibrated `sv600_scan.py` / daemon path handles them at ~0.3 mm
  accuracy with one button press.
