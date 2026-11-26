# Contributing to GridForge

Thank you for helping make **GridForge** better! GridForge is licensed under **GNU GPL v3.0 or later** and is committed to 100% open-source tooling and cleanly licensed open geospatial data.

---

## Development Setup

GridForge targets **Python 3.11+** (tested on **Debian Trixie / Python 3.13 + Headless PyQGIS 3.40 LTR + GDAL 3.10**) and a **TypeScript + Vite + MapLibre GL JS** web studio (`web/`). Running tests inside the Docker image guarantees exact PyQGIS, GDAL, PROJ, and font parity across macOS (`arm64`) and Linux (`amd64`/`arm64`).

### 1. Build the Development Image

```bash
docker build -t gridforge:dev .
```

### 2. Run the Offline CI Test Suite (No Network Required)

```bash
docker run --rm -v "$(pwd):/app" gridforge:dev pytest -q tests/unit tests/integration
```

### 3. Run Live Regional E2E Tests (Optional, Uses Public Open-Data APIs)

```bash
docker run --rm -e GRIDFORGE_RUN_LIVE=1 -v "$(pwd):/app" gridforge:dev \
  pytest -q tests/integration -m live
```

### 4. Develop the MapLibre Print Studio (`web/`)

```bash
cd web
npm ci
npm run typecheck
npm run build
```

For hot-reloading UI edits alongside a running API server:

```bash
# Terminal 1: backend + worker
docker compose up -d

# Terminal 2: Vite dev server (proxies /api -> localhost:8080 if configured)
cd web && npm run dev
```

---

## Architecture & Engineering Guardrails

- **True-Scale Local UTM Only:** Print sheets must always project into their native local UTM zone (`EPSG:326xx`/`327xx`). Never render print sheets in Web Mercator (`EPSG:3857`).
- **Dual-Stack PDF Crispness:** Raster layers (hillshade, slope shading, RF viewshed) belong on the bottom map stack so vector layers (contours, roads, labels, MGRS 1 km grid) remain 100% crisp vector primitives (`rasterizeWholeImage=False`).
- **Dedicated Cartography Themes:** Never generate monochrome or night-safe maps by post-processing Color outputs. Always author explicit rules across all 4 themes (`color`, `bw`, `night-red`, `alpine`) in `src/gridforge/render/styles.py` and regenerate `assets/styles/{color,bw,night-red,alpine}/*.qml` (see [docs/STYLING_GUIDE.md](docs/STYLING_GUIDE.md)).
- **16 GB Host Memory Budget:** Keep the FastAPI `web` container completely free of `qgis.core` imports (`< 150 MiB` RSS), keep PyQGIS isolated in ephemeral `spawn` subprocesses, and stream large remote files disk-to-disk (e.g., GDAL `/vsicurl/` and `/vsizip/` with OGR spatial filters) rather than loading multi-hundred-MB payloads into RAM.
- **Strict Open-Data Licensing:** Every new elevation or overlay source **must** use a commercial-friendly open license (**Public Domain**, **ODbL**, **CC BY 3.0 / 4.0**, or **Copernicus DEM**) with zero API keys required and explicit collar attribution emitted when features are used. Do **not** add `CC BY-NC` (non-commercial) datasets, scraped proprietary basemaps, or key-gated commercial tile servers.

---

## Pull Request Checklist

Before opening a pull request:

1. **Tests pass offline:** `pytest -q tests/unit tests/integration` passes cleanly inside `gridforge:dev`.
2. **Frontend tests & build pass:** If `web/` changed, `npm --prefix web test && npm --prefix web run build` completes with zero errors.
3. **Styles regenerated:** If `src/gridforge/render/styles.py` changed, regenerate `assets/styles/{color,bw,night-red,alpine}/*.qml` via `python3 -m gridforge.render.styles` (see [docs/STYLING_GUIDE.md](docs/STYLING_GUIDE.md)).
4. **Release & README hygiene:** `python3 scripts/release_preflight.py` passes cleanly. For tagging releases, follow [docs/RELEASE_CHECKLIST.md](docs/RELEASE_CHECKLIST.md).
5. **Physical scale verified:** Any change touching CRS, margins, layouts, or grid geometry must keep `tests/integration/verify_pdf.py` within `<= 0.15 mm` scale error and `< 0.05 mm` orthogonality skew.
6. **Visual check:** Attach before/after screenshots (e.g., rasterized with `pdftoppm -png -r 150`) for cartography, collar, or Studio UI changes.
7. **Conventional Commits:** Use clear prefixes (`feat(...)`, `fix(...)`, `docs(...)`, `test(...)`, `chore(...)`).
