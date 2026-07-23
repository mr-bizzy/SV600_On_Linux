#!/usr/bin/env bash
#
# sv600_book.sh — prep raw SV600 book spreads and open them in ScanTailor.
#
# Usage:
#   ./sv600_book.sh IN_DIR [OUT_DIR]
#     IN_DIR   folder of raw --full spreads (Book, raw spread mode, PNG)
#     OUT_DIR  where prepped TIFFs go (default: IN_DIR/scantailor)
#
# Steps: sv600_prep.py (deskew + trim the mat) -> launch ScanTailor Advanced on
# the result. ScanTailor does the split / dewarp / threshold / export. See
# BOOK-SCANNING.md for which ScanTailor stages matter.
#
set -euo pipefail

HERE="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
ST_APP="com.github._4lex4.ScanTailor-Advanced"

[[ $# -ge 1 ]] || { grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 1; }
IN="$1"; OUT="${2:-$1/scantailor}"
[[ -d "$IN" ]] || { echo "Input dir not found: $IN" >&2; exit 1; }

# 1. Prep: deskew + trim every raw spread into OUT as numbered TIFFs.
shopt -s nullglob
imgs=("$IN"/*.png "$IN"/*.PNG "$IN"/*.jpg "$IN"/*.tif "$IN"/*.tiff)
shopt -u nullglob
[[ ${#imgs[@]} -gt 0 ]] || { echo "No images in $IN" >&2; exit 1; }

echo ">> Prepping ${#imgs[@]} spread(s) -> $OUT"
python3 "$HERE/sv600_prep.py" "${imgs[@]}" "$OUT"

# 2. Open ScanTailor on the prepped folder.
if flatpak info "$ST_APP" >/dev/null 2>&1; then
  # Flatpak is sandboxed; make sure it can read the folder.
  flatpak override --user --filesystem="$(readlink -f "$OUT")" "$ST_APP" 2>/dev/null || true
  echo ">> Opening ScanTailor Advanced on $OUT"
  echo "   (New Project -> add $OUT -> DPI 300; see BOOK-SCANNING.md for stages)"
  setsid flatpak run "$ST_APP" >/dev/null 2>&1 &
elif command -v scantailor-advanced >/dev/null; then
  setsid scantailor-advanced >/dev/null 2>&1 &
  echo ">> Opening ScanTailor. New Project -> add $OUT"
else
  echo ">> ScanTailor Advanced is not installed. Install it with:"
  echo "     flatpak install -y flathub $ST_APP"
  echo "   Then open the prepped folder as a New Project: $OUT"
fi
