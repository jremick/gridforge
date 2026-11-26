#!/usr/bin/env bash
# Idempotently sync GitHub repository description, topics, and verify
# OpenGraph social preview + public GHCR manifest availability.
#
# Usage:
#   ./scripts/sync_github_metadata.sh [owner/repo]
set -euo pipefail

REPO="${1:-jremick/gridforge}"

HOMEPAGE="https://gridmaps.net"
DESCRIPTION="Live at gridmaps.net (GridMaps) — Open-source tactical & backcountry MGRS topographic map generator: true-scale UTM PDFs, 4 themes (Night-Red/Alpine), RF viewsheds & Fresnel cards, ATAK/Garmin/OsmAnd/GeoPackage, air-gapped packs across US, Canada, AU & NZ."

TOPICS=(
  atak
  australia
  canada
  cartography
  garmin
  geopackage
  gis
  maplibre
  mbtiles
  meshtastic
  mgrs
  new-zealand
  offline-maps
  osmand
  qgis
  rf-viewshed
  search-and-rescue
  self-hosted
  topographic-maps
  utm
)

echo "Updating homepage (${HOMEPAGE}), description & ${#TOPICS[@]} topics on ${REPO}..."
TOPIC_ARGS=()
for t in "${TOPICS[@]}"; do
  TOPIC_ARGS+=(--add-topic "$t")
done

gh repo edit "${REPO}" \
  --homepage "${HOMEPAGE}" \
  --description "${DESCRIPTION}" \
  "${TOPIC_ARGS[@]}"

echo
echo "Checking repository metadata & Social Preview status..."
gh repo view "${REPO}" --json homepageUrl,description,repositoryTopics,usesCustomOpenGraphImage \
  --jq '{homepageUrl, description, topic_count: (.repositoryTopics | length), usesCustomOpenGraphImage}'

USES_CUSTOM_OG="$(gh repo view "${REPO}" --json usesCustomOpenGraphImage --jq '.usesCustomOpenGraphImage')"
if [ "${USES_CUSTOM_OG}" != "true" ]; then
  echo
  echo "WARNING: GitHub custom Social Preview image is not uploaded yet."
  echo "Upload docs/images/banner.png (1280x640) once at:"
  echo "  https://github.com/${REPO}/settings (Social preview -> Edit -> Upload an image)"
fi

echo
echo "Checking public GHCR pull access for ghcr.io/${REPO}:latest..."
TOKEN="$(curl -fsSL "https://ghcr.io/token?scope=repository:${REPO}:pull" | python3 -c "import sys,json; print(json.load(sys.stdin)['token'])")"
HTTP_CODE="$(curl -s -o /dev/null -w '%{http_code}' \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "Accept: application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json" \
  "https://ghcr.io/v2/${REPO}/manifests/latest")"

if [ "${HTTP_CODE}" = "200" ]; then
  echo "GHCR public pull OK: ghcr.io/${REPO}:latest (HTTP ${HTTP_CODE})"
else
  echo "ERROR: ghcr.io/${REPO}:latest returned HTTP ${HTTP_CODE} (check Package visibility -> Public)" >&2
  exit 1
fi
