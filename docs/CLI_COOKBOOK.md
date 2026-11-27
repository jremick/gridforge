# GridForge CLI Cookbook

Every Studio capability runs headlessly via `ghcr.io/jremick/gridforge:latest`. For a runnable end-to-end shell script covering 10 regional targets, see [`examples/render-examples.sh`](../examples/render-examples.sh).

```bash
mkdir -p out packs
```

## 1. US Yosemite Valley CA — Alpine Winter + USGS 3DEP + USFS/NPS/PAD-US

```bash
docker run --rm -v "$(pwd)/out:/out" ghcr.io/jremick/gridforge:latest \
  gridforge render --center "37.745,-119.593" --scale 24000 --paper letter --units imperial \
    --mode alpine --dem-source usgs3dep --us-overlay us-trails --us-overlay us-public-lands \
    --slope-shading --title "YOSEMITE VALLEY — ALPINE CA" \
    --output /out/yosemite_24k_letter_alpine.pdf
```

## 2. Canada Banff AB — Night-Red (Red-Headlamp Safe) + NRCan HRDEM + Parks Canada

```bash
docker run --rm -v "$(pwd)/out:/out" ghcr.io/jremick/gridforge:latest \
  gridforge render --center "51.178,-115.571" --scale 25000 --paper a4 --mode night-red \
    --dem-source ca-hrdem --ca-overlay ca-canvec-trails \
    --title "BANFF — SULPHUR MTN AB" \
    --output /out/banff_25k_a4_night_red.pdf
```

## 3. Australia Mt Kosciuszko NSW — RF Viewshed + 915 MHz Fresnel Link Card + Field Bundle

```bash
docker run --rm -v "$(pwd)/out:/out" ghcr.io/jremick/gridforge:latest \
  gridforge render --center "-36.456,148.263" --scale 25000 --paper a4 \
    --dem-source au-elvis --au-overlay au-nsw-trails \
    --viewshed "-36.4559,148.2636,10,1.5,8,Kosciuszko Relay" \
    --rf-link "-36.4559,148.2636,-36.4535,148.2877,915,10,5,Kosciuszko Relay,Seamans Hut" --rf-link-card \
    --range-ring "-36.4559,148.2636,1:2,Summit Rings" \
    --output /out/kosciuszko_rf_tactical.pdf --gpkg /out/kosciuszko.gpkg \
    --osmand /out/kosciuszko.sqlitedb --field-bundle /out/kosciuszko_field_bundle.zip
```

## 4. New Zealand Tongariro — Live LINZ 1 m LiDAR + DOC Great Walks & Huts

```bash
docker run --rm -v "$(pwd)/out:/out" ghcr.io/jremick/gridforge:latest \
  gridforge render --center "-39.133,175.642" --scale 25000 --paper a4 \
    --dem-source nz-linz --nz-overlay nz-doc-trails --slope-shading \
    --title "TONGARIRO ALPINE CROSSING — NZ" \
    --output /out/tongariro_nz_25k_a4.pdf
```

## 5. Auto-Fitted Strip Atlas + Index Cover + Elevation Profile + Calibration Page

```bash
docker run --rm -v "$(pwd)/out:/out" -v "$(pwd)/examples:/examples:ro" ghcr.io/jremick/gridforge:latest \
  gridforge render --strip-route /examples/katoomba_route.geojson --scale 25000 --paper a4 \
    --dem-source au-elvis --au-overlay au-state-trails \
    --atlas-index --elevation-profile --fold-marks --calibration-page \
    --title "KATOOMBA BUSHWALK STRIP ATLAS" \
    --output /out/katoomba_strip_atlas.pdf
```

## 6. Build a 100% Offline Regional Pack & Render Without Internet

```bash
docker run --rm ghcr.io/jremick/gridforge:latest gridforge pack list-regions

docker run --rm -v "$(pwd)/packs:/data/packs" ghcr.io/jremick/gridforge:latest \
  gridforge pack build --region blue-mountains --output /data/packs/blue-mountains

docker run --rm -v "$(pwd)/packs:/data/packs:ro" -v "$(pwd)/out:/out" ghcr.io/jremick/gridforge:latest \
  gridforge render --center "-33.732,150.312" --scale 25000 --paper a4 \
    --pack-id blue-mountains --output /out/offline_sheet.pdf
```

## 7. Standalone Offline Terrain Basemap & Device-Only Exports (ATAK + Garmin)

```bash
docker run --rm -v "$(pwd)/packs:/data/packs" ghcr.io/jremick/gridforge:latest \
  gridforge basemap-pack --region blue-mountains --output /data/packs/basemap.mbtiles

docker run --rm -v "$(pwd)/out:/out" ghcr.io/jremick/gridforge:latest \
  gridforge export --center "-33.732,150.312" --scale 25000 --paper a4 \
    --format both --mbtiles-format png --garmin-profile standard --no-grid \
    --output /out/katoomba_offline
```
