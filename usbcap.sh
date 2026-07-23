#!/usr/bin/env bash
#
# usbcap.sh — capture Fujitsu SV600 USB traffic to a pcapng for protocol analysis.
#
# Usage:  sudo ./usbcap.sh [-s SNAPLEN] [output.pcapng]
#
# Loads usbmon, captures the bus the SV600 is on, and records until you press
# ENTER. Do ONE scan (in the VM, with the real ScanSnap software) during the
# capture window, then press ENTER to stop. Output is owned by your user so it
# can be analyzed without sudo.
#
#   -s N   truncate each packet to N bytes.
#
#          Use -s 256 for PROTOCOL work (commands, status, button polls). The
#          image transfer is 484764 bytes per READ(10) and dwarfs everything
#          else — full captures of one scan run 75-150 MB and are slow to grep,
#          while every command wrapper is 31 bytes and every status block 13,
#          so 256 keeps all of them and throws away only image payload.
#          Leave unset (full capture) when you need the image bytes themselves.
#
set -euo pipefail

SNAPLEN=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    -s|--snaplen) SNAPLEN="$2"; shift 2 ;;
    -h|--help)    grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    -*)           echo "Unknown option: $1" >&2; exit 1 ;;
    *)            break ;;
  esac
done

# NOTE: dumpcap drops privileges when run via sudo, so it must write somewhere
# an unprivileged user can create files (e.g. /tmp). We chown it back to you at
# the end. Do NOT default this into $HOME — dumpcap can't write there under sudo.
# Default to /var/tmp, NOT /tmp: dumpcap drops privileges so it needs a
# world-writable dir, but /tmp is routinely cleared and we lost the original
# SV600 capture that way. /var/tmp survives reboots, and we copy the result into
# KEEPDIR (alongside this script) at the end, once we're root again.
OUT="${1:-/var/tmp/sv600-capture-$(date +%Y%m%d-%H%M%S).pcapng}"
REALUSER="${SUDO_USER:-$(id -un)}"
KEEPDIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"

case "$OUT" in
  /tmp/*|/var/tmp/*) : ;;  # writable by the dropped-privilege user
  *) echo "WARNING: dumpcap under sudo may not be able to write '$OUT'." >&2
     echo "         If it fails with 'Permission denied', use a /tmp path." >&2 ;;
esac

[[ $EUID -eq 0 ]] || { echo "Run with sudo:  sudo $0 $*" >&2; exit 1; }
command -v dumpcap >/dev/null || { echo "Install capture tools first:  sudo apt install -y tshark" >&2; exit 1; }

modprobe usbmon 2>/dev/null || true

# The SV600 stays visible on the host bus even when passed through to a VM.
BUS="$(lsusb | awk '/04c5:(128e|13ba)/{print $2+0; exit}')"
[[ -n "$BUS" ]] || { echo "SV600 (04c5:128e) not found on USB. Plug it in." >&2; exit 1; }
IF="usbmon${BUS}"

echo "=================================================================="
echo " Capturing USB bus $BUS  ($IF)  ->  $OUT"
echo "=================================================================="
echo " 1. Make sure the SV600 is passed through to the VM."
echo " 2. In the VM, open ScanSnap and run ONE scan of a test page."
echo " 3. When the scanned image appears, come back here."
echo
echo " Recording now...  press ENTER to STOP."
echo

DC_ARGS=(-q -i "$IF" -w "$OUT")
[[ -n "$SNAPLEN" ]] && DC_ARGS+=(-s "$SNAPLEN") && echo " (snaplen $SNAPLEN — image payload truncated, protocol kept)"

dumpcap "${DC_ARGS[@]}" &
DC=$!
# stop cleanly on Ctrl-C too
trap 'kill "$DC" 2>/dev/null || true' INT
read -r _ || true
kill "$DC" 2>/dev/null || true
wait "$DC" 2>/dev/null || true

chown "$REALUSER":"$REALUSER" "$OUT" 2>/dev/null || true
SZ="$(du -h "$OUT" 2>/dev/null | cut -f1)"
echo
echo ">> Saved capture: $OUT ($SZ)"

# Keep a copy next to the script so a /tmp-style wipe can't lose it again.
if [[ -s "$OUT" ]]; then
  KEEP="$KEEPDIR/$(basename "$OUT")"
  if cp -n "$OUT" "$KEEP" 2>/dev/null; then
    chown "$REALUSER":"$REALUSER" "$KEEP" 2>/dev/null || true
    echo ">> Copy kept at:  $KEEP"
  else
    echo ">> WARNING: could not copy to $KEEPDIR — keep $OUT safe yourself." >&2
  fi
else
  echo ">> WARNING: capture is empty — did the scan actually run during the window?" >&2
fi
echo ">> Hand this file back to Claude for decoding."
