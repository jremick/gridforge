#!/usr/bin/env bash
# GridForge CLI Cookbook — Reproducible true-scale UTM topographic sheet & offline export examples.
# Uses the prebuilt multi-arch GHCR image by default (zero local build required).
# Override with GRIDFORGE_IMAGE=gridforge:dev to test a local development build.
#
# Usage:
#   ./examples/render-examples.sh                 # Run Blue Mountains + Cradle Mountain defaults
#   ./examples/render-examples.sh all             # Run all AU, US, Canada, NZ, RF & offline pack examples
#   ./examples/render-examples.sh blue-mountains|sydney|uluru|cradle|boulder|yosemite|banff|kosciuszko-rf|tongariro|pack

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT_DIR="${REPO_ROOT}/out"
PACKS_DIR="${REPO_ROOT}/packs"
IMAGE="${GRIDFORGE_IMAGE:-ghcr.io/jremick/gridforge:latest}"

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
  echo "==> [1/10] Blue Mountains NSW — Color + B&W + Custom GeoJSON Route Overlay"
  run_gridforge render \
    --center "-33.732,150.312" \
    --scale 25000 \
    --paper a4 \
    --mode color \
    --units metric \
    --dem-source au-elvis \
    --au-overlay au-nsw-trails \
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
    --au-overlay au-nsw-trails \
    --overlay /examples/katoomba_route.geojson \
    --title "BLUE MOUNTAINS — KATOOMBA LOOP (B&W)" \
    --output /out/katoomba_25k_a4_route_bw.pdf
}

render_sydney() {
  echo "==> [2/10] Sydney Harbour NSW — Native GA 5 m LiDAR (AHD)"
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
  echo "==> [3/10] Uluru / Kata Tjuta NT — Outback Hydrology, CAPAD Parks & Airfields"
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
  echo "==> [4/10] Cradle Mountain TAS — Laser-Print B&W + LIST Trails + ATAK .mbtiles + Garmin .kmz"
  run_gridforge render \
    --center "-41.684,145.951" \
    --scale 25000 \
    --paper a4 \
    --mode bw \
    --dem-source au-elvis \
    --au-overlay au-state-trails \
    --au-overlay au-hydro \
    --au-overlay au-parks \
    --title "CRADLE MOUNTAIN — TASMANIA" \
    --output /out/cradle_mountain_25k_a4_bw.pdf \
    --mbtiles /out/cradle_mountain_25k.mbtiles \
    --kmz /out/cradle_mountain_25k.kmz
}

render_boulder() {
  echo "==> [5/10] Boulder / Flatirons CO — USGS 3DEP 1 m LiDAR (NAVD88) + FEMA/NHD/FAA"
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

render_yosemite() {
  echo "==> [6/10] Yosemite Valley CA — Alpine Winter Theme + USGS 3DEP + USFS/NPS Trails + PAD-US"
  run_gridforge render \
    --center "37.745,-119.593" \
    --scale 24000 \
    --paper letter \
    --units imperial \
    --mode alpine \
    --dem-source usgs3dep \
    --us-overlay us-trails \
    --us-overlay us-public-lands \
    --slope-shading \
    --title "YOSEMITE VALLEY — ALPINE CA" \
    --output /out/yosemite_24k_letter_alpine.pdf
}

render_banff() {
  echo "==> [7/10] Banff / Sulphur Mtn AB — Night-Red Theme + Canada NRCan HRDEM (CGVD2013) + Parks Canada"
  run_gridforge render \
    --center "51.178,-115.571" \
    --scale 25000 \
    --paper a4 \
    --mode night-red \
    --dem-source ca-hrdem \
    --ca-overlay ca-canvec-trails \
    --title "BANFF — SULPHUR MTN AB" \
    --output /out/banff_25k_a4_night_red.pdf
}

render_kosciuszko_rf() {
  echo "==> [8/10] Mt Kosciuszko NSW — RF Viewshed + 915 MHz Fresnel Link Card + Tactical Rings + Field Bundle"
  run_gridforge render \
    --center "-36.456,148.263" \
    --scale 25000 \
    --paper a4 \
    --mode color \
    --dem-source au-elvis \
    --au-overlay au-nsw-trails \
    --viewshed "-36.4559,148.2636,10,1.5,8,Kosciuszko Relay" \
    --rf-link "-36.4559,148.2636,-36.4535,148.2877,915,10,5,Kosciuszko Relay,Seamans Hut" \
    --rf-link-card \
    --range-ring "-36.4559,148.2636,1:2,Summit Rings" \
    --waypoint "Kosciuszko Relay,-36.4559,148.2636,waypoint" \
    --waypoint "Seamans Hut,-36.4535,148.2877,camp" \
    --calibration-page \
    --title "MT KOSCIUSZKO — RF LINK & TACTICAL" \
    --output /out/kosciuszko_rf_tactical_25k_a4.pdf \
    --gpkg /out/kosciuszko_25k.gpkg \
    --osmand /out/kosciuszko_25k.sqlitedb \
    --field-bundle /out/kosciuszko_field_bundle.zip
}

render_tongariro() {
  echo "==> [9/10] Tongariro Alpine Crossing NZ — Live LINZ 1 m LiDAR (NZVD2016) + DOC Great Walks"
  run_gridforge render \
    --center "-39.133,175.642" \
    --scale 25000 \
    --paper a4 \
    --mode color \
    --dem-source nz-linz \
    --nz-overlay nz-doc-trails \
    --slope-shading \
    --title "TONGARIRO ALPINE CROSSING — NZ" \
    --output /out/tongariro_nz_25k_a4.pdf
}

build_offline_pack() {
  echo "==> [10/10] Air-Gapped Regional Pack (with Offline Terrain Basemap) — Blue Mountains"
  run_gridforge pack list-regions
  run_gridforge pack build \
    --region blue-mountains \
    --output /data/packs/blue-mountains
  run_gridforge pack inspect /data/packs/blue-mountains
  run_gridforge render \
    --center "-33.732,150.312" \
    --scale 25000 \
    --paper a4 \
    --pack-id blue-mountains \
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
    render_yosemite
    render_banff
    render_kosciuszko_rf
    render_tongariro
    build_offline_pack
    ;;
  blue-mountains) render_blue_mountains ;;
  sydney)         render_sydney ;;
  uluru)          render_uluru ;;
  cradle)         render_cradle ;;
  boulder)        render_boulder ;;
  yosemite)       render_yosemite ;;
  banff)          render_banff ;;
  kosciuszko-rf)  render_kosciuszko_rf ;;
  tongariro)      render_tongariro ;;
  pack)           build_offline_pack ;;
  *)
    echo "Usage: $0 [default|all|blue-mountains|sydney|uluru|cradle|boulder|yosemite|banff|kosciuszko-rf|tongariro|pack]" >&2
    exit 2
    ;;
esac

echo "Done! Outputs written to ${OUT_DIR}"
