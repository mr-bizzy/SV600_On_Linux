# SV600 on Linux

A native Linux driver for the **Fujitsu / Ricoh ScanSnap SV600** overhead
scanner (USB `04c5:128e`), reverse-engineered from a USB capture of the official
ScanSnap software. No VM, no SANE backend required — it talks to the scanner
directly and produces deskewed, colour-corrected, calibrated output.

There is no SANE backend and no official Linux support for the SV600. This fills
that gap. See [SANE backends issue #89](https://gitlab.com/sane-project/backends/-/work_items/89).

## Status

* **Flat documents (sheets, letters, receipts): excellent.** A per-device
  polynomial calibration rectifies the scanner's oblique geometry to ~0.3 mm and
  snaps output to true paper sizes.
* **Scan button: works.** A user daemon holds the device open and scans on a
  button press, with an on-screen "continue / finish / discard" prompt, batching
  to multi-page documents, and a save dialog.
* **Books: capture works; flattening is handed to [ScanTailor Advanced](https://github.com/4lex4/scantailor-advanced).**
  The SV600 was built for books, but the page-curvature correction is done best
  by ScanTailor — see [BOOK-SCANNING.md](BOOK-SCANNING.md). An automatic
  UVDoc-in-Docker unwarper is also included as an alternative.

## Requirements

```
sudo apt install python3-usb python3-numpy python3-pil libusb-1.0-0 \
                 ocrmypdf img2pdf tesseract-ocr        # OCR (optional)
sudo apt install yad                                   # daemon prompt (optional)
sudo apt install jbig2enc                              # smaller PDFs (optional)
```

USB access: the scanner node must be readable by your user. A udev rule such as

```
SUBSYSTEM=="usb", ATTR{idVendor}=="04c5", ATTR{idProduct}=="128e", \
  MODE="0660", GROUP="scanner", TAG+="uaccess"
```

(then add yourself to the `scanner` group) removes the need for `sudo`.

## Quick start

```bash
# One scan -> searchable PDF
sudo python3 sv600_scan.py out.png          # or ./scan2pdf out.pdf

# Formats and colour
python3 sv600_scan.py -f pdf-ocr -c gray out.pdf
python3 sv600_scan.py -f png -c bw out.png

# Two A4 sheets side by side, or an A3 sheet
python3 sv600_scan.py --pages 2 out.png     # -> out-1.png, out-2.png
python3 sv600_scan.py --paper a3 out.png

# Scan-button daemon (systemd --user)
./install-service.sh
systemctl --user enable --now sv600-watch
```

## Tools

| File | What it does |
|---|---|
| `sv600_scan.py` | one-shot scan → dewarped, colour-corrected, calibrated image/PDF |
| `sv600_watch.py` | scan-button daemon: press → scan, prompt, batch, save dialog |
| `sv600_output.py` | shared output: png/jpeg/tiff/pdf/pdf-ocr × colour/gray/bw |
| `sv600_calibrate.py` | build the per-device polynomial calibration from a chart |
| `sv600_prep.py` | deskew + trim raw book spreads for ScanTailor |
| `sv600_book.sh` | prep spreads and open ScanTailor (book workflow) |
| `sv600_unwarp.sh` + `docker/` | automatic book flattening via PaddleOCR/UVDoc in a GPU container |
| `sv600_dewarp.py` | experimental in-house book dewarper (superseded by the above) |
| `scan2pdf`, `scanocr` | scan → OCR'd, compressed PDF |
| `sv600_button.py`, `sv600_timeline.py`, `usbcap.sh` | protocol probing / USB capture analysis |
| `install-service.sh`, `sv600-watch.service.in` | systemd user service for the daemon |

## Protocol (summary)

The SV600 speaks a Fujitsu SCSI command set wrapped in a 31-byte `0x43` ('C')
transport header on bulk endpoints:

* **bulk OUT `0x02`**: `0x43`, 18×`0x00`, then a standard SCSI CDB at byte 19,
  a 4-byte field and a 4-byte big-endian transfer length. SET WINDOW / MODE
  SELECT / ASCII commands add a data-out phase.
* **bulk IN `0x81`**: the data-in phase, then a 13-byte `0x53` ('S') status
  block (`…00` = OK/done, `…08` = data ready).

Key findings, all verified against the capture:

* SCAN requires a 266-byte tone/gamma LUT uploaded via WRITE(10) first, or it
  fails with CHECK CONDITION — the single thing that stalled the driver longest.
* Image is plain interleaved RGB, 5572 px wide, contiguous rows, uncompressed.
* SET WINDOW carries dpi (bytes 10–13) and window extents (bytes 24–29, in
  1200ths inch); the official software does a full-platen pass then a cropped
  pass.
* **The scan button** is byte[4] of the `0xc2` GET DATA BUFFER STATUS reply — a
  latched one-shot flag (poll it; it is not a live level). This is what the
  daemon watches.

The full command sequence is transcribed in `sv600_scan.py` (`SETUP` / `TEARDOWN`).

## Calibration

`sv600-calibration.json` is a per-device geometry fit (this repo ships one as an
example). To build your own: print `sv600-calchart-a4.pdf` (regenerate with
`make_calchart.py`), scan it, and run `sv600_calibrate.py`. The scanner's optics
are fixed, so one calibration serves every flat scan.

## License

**GPL-2.0-or-later** — see [LICENSE](LICENSE). Chosen to match the SANE project,
so any of this can feed a future SANE backend.

## Credits

Reverse-engineered by capturing and analysing the official ScanSnap software's
USB traffic. Not affiliated with Fujitsu / PFU / Ricoh. "ScanSnap" is their
trademark.
