#!/usr/bin/env bash
# GridForge CLI Cookbook — Reproducible true-scale UTM topographic sheet & offline export examples.
# Prerequisites: `docker build -t gridforge:dev .` (or `docker compose build`).
# Usage:
#   ./examples/render-examples.sh               # Run Blue Mountains + Cradle Mountain examples
#   ./examples/render-examples.sh all           # Run all Australia, US, and offline pack examples
#   ./examples/render-examples.sh blue-mountains|sydney|uluru|cradle|boulder|pack

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT_DIR="${REPO_ROOT}/out"
PACKS_DIR="${REPO_ROOT}/packs"
IMAGE="${GRIDFORGE_IMAGE:-gridforge:dev}"

mkdir -p "${OUT_DIR}" "${PACKS_DIR}"

run_gridforge() {
  docker run --rm \
    -v "${OUT_DIR}:/out" \
    -v "${PACKS_DIR}:/data/packs" \
    -v "${REPO_ROOT}/examples:/examples:ro" \
    -v gridforge_cache:/tmp/gridforge-cache \
    "${IMAGE}" \
    gridforge "$@"
}

render_blue_mountains() {
  echo "==> [1/6] Blue Mountains NSW — Color + B&W + Custom GeoJSON Route Overlay"
  run_gridforge render \
    --center "-33.732,150.312" \
    --scale 25000 \
    --paper a4 \
    --mode color \
    --units metric \
    --dem-source au-elvis \
    --overlay /examples/katoomba_route.geojson \
    --title "BLUE MOUNTAINS — KATOOMBA LOOP" \
    --output /out/katoomba_25k_a4_route_color.pdf

  run_gridforge render \
    --center "-33.732,150.312" \
    --scale 25000 \
    --paper a4 \
    --mode bw \
    --units metric \
    --dem-source au-elvis \
    --overlay /examples/katoomba_route.geojson \
    --title "BLUE MOUNTAINS — KATOOMBA LOOP (B&W)" \
    --output /out/katoomba_25k_a4_route_bw.pdf
}

render_sydney() {
  echo "==> [2/6] Sydney Harbour NSW — Native GA 5 m LiDAR (AHD)"
  run_gridforge render \
    --center "-33.8568,151.2153" \
    --scale 25000 \
    --paper a4 \
    --mode color \
    --dem-source au-elvis \
    --title "SYDNEY HARBOUR — NORTH SHORE" \
    --output /out/sydney_25k_a4_color.pdf
}

render_uluru() {
  echo "==> [3/6] Uluru / Kata Tjuta NT — Outback Hydrology, CAPAD Parks & Airfields"
  run_gridforge render \
    --center "-25.3444,131.0369" \
    --scale 25000 \
    --paper a4 \
    --mode color \
    --dem-source au-elvis \
    --au-overlay au-hydro \
    --au-overlay au-parks \
    --au-overlay au-aviation \
    --title "ULURU — KATA TJUTA NT" \
    --output /out/uluru_25k_a4_color.pdf
}

render_cradle() {
  echo "==> [4/6] Cradle Mountain TAS — Laser-Print B&W + ATAK .mbtiles + Garmin .kmz"
  run_gridforge render \
    --center "-41.684,145.951" \
    --scale 25000 \
    --paper a4 \
    --mode bw \
    --dem-source au-elvis \
    --au-overlay au-hydro \
    --au-overlay au-parks \
    --title "CRADLE MOUNTAIN — TASMANIA" \
    --output /out/cradle_mountain_25k_a4_bw.pdf \
    --mbtiles /out/cradle_mountain_25k.mbtiles \
    --kmz /out/cradle_mountain_25k.kmz
}

render_boulder() {
  echo "==> [5/6] Boulder / Flatirons CO — USGS 3DEP 1 m LiDAR (NAVD88) + FEMA/NHD/FAA"
  run_gridforge render \
    --center "40.005,-105.285" \
    --scale 24000 \
    --paper letter \
    --mode color \
    --units imperial \
    --dem-source usgs3dep \
    --us-overlay fema \
    --us-overlay nhd \
    --us-overlay towers \
    --title "BOULDER — FLATIRONS, CO" \
    --output /out/boulder_24k_letter_color.pdf \
    --mbtiles /out/boulder_24k.mbtiles \
    --kmz /out/boulder_24k.kmz
}

build_offline_pack() {
  echo "==> [6/6] Air-Gapped Australia Offline Pack — Blue Mountains Region"
  run_gridforge pack list-regions
  run_gridforge pack build \
    --region blue-mountains \
    --output /data/packs/blue-mountains
  run_gridforge pack inspect /data/packs/blue-mountains
  run_gridforge render \
    --center "-33.732,150.312" \
    --scale 25000 \
    --paper a4 \
    --pack /data/packs/blue-mountains \
    --title "BLUE MOUNTAINS — AIR-GAPPED PACK" \
    --output /out/katoomba_airgapped_pack.pdf
}

TARGET="${1:-default}"
case "${TARGET}" in
  default)
    render_blue_mountains
    render_cradle
    ;;
  all)
    render_blue_mountains
    render_sydney
    render_uluru
    render_cradle
    render_boulder
    build_offline_pack
    ;;
  blue-mountains) render_blue_mountains ;;
  sydney)         render_sydney ;;
  uluru)          render_uluru ;;
  cradle)         render_cradle ;;
  boulder)        render_boulder ;;
  pack)           build_offline_pack ;;
  *)
    echo "Usage: $0 [default|all|blue-mountains|sydney|uluru|cradle|boulder|pack]" >&2
    exit 2
    ;;
esac

echo "Done! Outputs written to ${OUT_DIR}"
