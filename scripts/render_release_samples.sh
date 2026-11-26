#!/usr/bin/env bash
# Render verified 300 DPI sample PDFs across North America, Australia, and New Zealand
# and optionally attach them to a GitHub Release as downloadable showcase assets.
#
# Usage:
#   ./scripts/render_release_samples.sh [--image ghcr.io/jremick/gridforge:latest] [--out-dir /tmp/gridforge-samples] [--upload v1.4.0]
set -euo pipefail

IMAGE="ghcr.io/jremick/gridforge:latest"
OUT_DIR="/tmp/gridforge-release-samples"
UPLOAD_TAG=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --image) IMAGE="$2"; shift 2 ;;
    --out-dir) OUT_DIR="$2"; shift 2 ;;
    --upload) UPLOAD_TAG="$2"; shift 2 ;;
    *) echo "Unknown argument: $1" >&2; exit 1 ;;
  esac
done

mkdir -p "${OUT_DIR}"

echo "Rendering 4 verified 300 DPI sample PDFs using ${IMAGE} into ${OUT_DIR}..."
# Stream rendered PDFs out via stdout tar so this works identically on both local
# Docker daemons and remote Docker contexts (e.g. ssh://gcp-dev).
docker run --rm "${IMAGE}" bash -ceu '
  mkdir -p /tmp/samples

  echo "[1/4] US Yosemite Valley CA — 1:24,000 Letter Alpine Winter..." >&2
  gridforge render \
    --center "37.745,-119.593" --scale 24000 --paper letter --units imperial \
    --mode alpine --dem-source usgs3dep \
    --us-overlay us-trails --us-overlay us-public-lands --slope-shading \
    --title "YOSEMITE VALLEY — ALPINE CA" \
    --output /tmp/samples/sample-yosemite-alpine-24k-letter.pdf >&2

  echo "[2/4] Canada Banff AB — 1:25,000 A4 Night-Red (Red-Headlamp Safe)..." >&2
  gridforge render \
    --center "51.178,-115.571" --scale 25000 --paper a4 --mode night-red \
    --dem-source ca-hrdem --ca-overlay ca-canvec-trails \
    --title "BANFF — SULPHUR MTN AB" \
    --output /tmp/samples/sample-banff-night-red-25k-a4.pdf >&2

  echo "[3/4] Australia Mt Kosciuszko NSW — RF Viewshed + 915 MHz Fresnel Link Card + Tactical..." >&2
  gridforge render \
    --center "-36.456,148.263" --scale 25000 --paper a4 --mode color \
    --dem-source au-elvis --au-overlay au-nsw-trails \
    --viewshed "-36.4559,148.2636,10,1.5,8,Kosciuszko Relay" \
    --rf-link "-36.4559,148.2636,-36.4535,148.2877,915,10,5,Kosciuszko Relay,Seamans Hut" \
    --rf-link-card \
    --range-ring "-36.4559,148.2636,1:2,Summit Rings" \
    --waypoint "Kosciuszko Relay,-36.4559,148.2636,waypoint" \
    --waypoint "Seamans Hut,-36.4535,148.2877,camp" \
    --title "MT KOSCIUSZKO — RF LINK & TACTICAL" \
    --output /tmp/samples/sample-kosciuszko-rf-tactical-25k-a4.pdf >&2

  echo "[4/4] New Zealand Tongariro — 1:25,000 A4 LINZ 1m LiDAR + DOC Tracks..." >&2
  gridforge render \
    --center "-39.133,175.642" --scale 25000 --paper a4 --mode color \
    --dem-source nz-linz --nz-overlay nz-doc-trails --slope-shading \
    --title "TONGARIRO ALPINE CROSSING — NZ" \
    --output /tmp/samples/sample-tongariro-nz-1m-lidar-25k-a4.pdf >&2

  ls -lh /tmp/samples/*.pdf >&2
  tar -C /tmp/samples -cf - .
' | tar -C "${OUT_DIR}" -xf -

echo
echo "Rendered sample PDFs in ${OUT_DIR}:"
ls -lh "${OUT_DIR}"/sample-*.pdf

if [ -n "${UPLOAD_TAG}" ]; then
  echo
  echo "Uploading sample PDFs to GitHub Release ${UPLOAD_TAG}..."
  gh release upload "${UPLOAD_TAG}" \
    "${OUT_DIR}/sample-yosemite-alpine-24k-letter.pdf" \
    "${OUT_DIR}/sample-banff-night-red-25k-a4.pdf" \
    "${OUT_DIR}/sample-kosciuszko-rf-tactical-25k-a4.pdf" \
    "${OUT_DIR}/sample-tongariro-nz-1m-lidar-25k-a4.pdf" \
    --clobber
  echo "Sample PDFs attached cleanly to release ${UPLOAD_TAG}!"
fi
