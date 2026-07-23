#!/usr/bin/env bash
#
# sv600_unwarp.sh — flatten book pages with PaddleOCR/UVDoc in a GPU container.
#
# Usage:
#   ./sv600_unwarp.sh IN_DIR OUT_DIR [--split]
#     IN_DIR   folder of raw --full spreads (or single pages) — png/jpg/tiff
#     OUT_DIR  where flattened pages are written
#     --split  cut each spread into two pages before unwarping (book spreads)
#
# First run builds the image (~10 GB base, a few minutes) and downloads the
# UVDoc model once into ./docker/models. After that it is just inference.
#
set -euo pipefail

HERE="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
IMAGE="sv600-unwarp:latest"
MODELS="$HERE/docker/models"

[[ $# -ge 2 ]] || { grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 1; }
IN="$(readlink -f "$1")"; OUT="$2"; shift 2
mkdir -p "$OUT" "$MODELS"; OUT="$(readlink -f "$OUT")"
[[ -d "$IN" ]] || { echo "Input dir not found: $IN" >&2; exit 1; }

# Build once. Rebuild only if the Dockerfile or entrypoint changed.
if ! docker image inspect "$IMAGE" >/dev/null 2>&1 || \
   [[ "$HERE/docker/Dockerfile" -nt "$(docker inspect -f '{{.Metadata.LastTagTime}}' "$IMAGE" 2>/dev/null || echo @0)" ]]; then
  echo ">> Building $IMAGE (first time is slow) ..."
  docker build -t "$IMAGE" "$HERE/docker"
fi

# --gpus all needs nvidia-container-toolkit (present on this box). Drop it to run
# on CPU (much slower, but no CUDA needed).
# --user maps the container process to the caller, so output files are owned by
# you, not root. HOME=/models points PaddleX's model cache (~/.paddlex) at the
# mounted, persistent volume, so the UVDoc weights download once and are reused.
GPU=(--gpus all)
docker run --rm "${GPU[@]}" \
  --user "$(id -u):$(id -g)" -e HOME=/models \
  -v "$IN":/in:ro -v "$OUT":/out -v "$MODELS":/models \
  "$IMAGE" /in /out "$@"

echo ">> Done. Flattened pages in: $OUT"
