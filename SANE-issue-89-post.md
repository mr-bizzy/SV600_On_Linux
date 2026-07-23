For anyone still waiting on SV600 support: I've written a working **native Linux
driver** for the ScanSnap SV600 by reverse-engineering the official ScanSnap
software's USB traffic. It isn't a SANE backend — it drives the device directly
over libusb — but it's a complete, tested protocol description that a backend
could be built from, so I'm putting it here in case it helps (and to save
whoever picks up the backend the weeks of USB-capture archaeology it took).

**Repo:** https://github.com/mr-bizzy/SV600_On_Linux (GPL-2.0-or-later)

### What works today
- Full scan pipeline: `SET WINDOW` → `SCAN` → `READ(10)` loop → 5572-px
  interleaved RGB, with a per-device polynomial calibration that rectifies the
  scanner's oblique geometry to ~0.3 mm and snaps output to true paper sizes.
- The **scan button**, via a systemd `--user` daemon (press → scan → prompt →
  multi-page PDF).
- Book capture (curvature-flattening handed off to ScanTailor).

### Protocol notes for a backend author (all verified against the capture)
- **Transport:** a 31-byte `0x43` ('C') wrapper on bulk OUT `0x02` carrying a
  standard SCSI CDB at byte 19, a 4-byte field, and a 4-byte big-endian transfer
  length. Bulk IN `0x81` returns the data-in phase then a 13-byte `0x53` ('S')
  status block (`…00` = OK/done, `…08` = data ready).
- **The main gotcha:** `SCAN` fails with CHECK CONDITION unless a 266-byte
  tone/gamma LUT is first uploaded via `WRITE(10)`. This is what stalled the
  work longest.
- `SET WINDOW` carries dpi at payload bytes 10–13 and the window extents at
  bytes 24–29 (units of 1/1200 inch). The official app does a full-platen pass
  then a cropped second pass.
- **No interrupt endpoint.** The **scan button** is byte[4] of the `0xc2`
  GET DATA BUFFER STATUS reply — a latched one-shot flag you poll for (the
  official software polls it ~5×/s for the whole session).
- Image data is uncompressed, contiguous rows, plain interleaved RGB.

The full `SETUP` / `TEARDOWN` command sequence is transcribed in the repo, along
with the USB-capture analysis tooling I used to derive it. Happy to help whoever
takes on the backend, and to share the raw `usbmon` captures privately if useful.
