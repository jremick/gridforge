# GridForge Cartographic Styling & QML Guide

GridForge ships dedicated **Color** (NATO / USGS topographic) and **B&W** (high-contrast monochrome laser-print) cartographic themes as native **QGIS 3.40 LTR `.qml`** files in `assets/styles/{color,bw}/` paired with parameterized SVG map symbols in `assets/icons/`.

This guide explains how the rendering stack works, how to customize styles interactively in **QGIS Desktop**, and how to regenerate native `.qml` files programmatically.

---

## 1. Dual-Stack Vector/Raster Architecture

GridForge prints PDFs with `QgsLayoutExporter.PdfExportSettings.rasterizeWholeImage = False` using a **two-item map stack** in `src/gridforge/render/layout.py`:

| Stack | Layers (Bottom -> Top) | Purpose |
| :--- | :--- | :--- |
| **Lower Map Item** *(raster/blended)* | `landcover` -> `hillshade` (`Multiply`) -> `slope_shading` -> `viewshed` | Keeps heavy raster blending isolated on the base layer without forcing Qt to rasterize vector linework above it. |
| **Upper Map Item** *(pure crisp vector)* | `fema_flood`, `au_protected_areas`, `water_polygons`, `au_hydro_polys` -> `contours`, `enc_depth_contours` -> `nhd_flowlines`, `au_hydro_lines`, `waterways` -> `buildings` -> `transport`, `au_state_trails`, `au_power_lines`, `au_pipelines`, `au_runways` -> `building_icons`, `points`, `au_state_pois`, `au_hydro_points`, `au_airfields`, `us_towers`, `viewshed_observers` -> `mgrs_grid_1km`, `mgrs_grid_100km`, `mgrs_zone_boundary`, `mgrs_square_labels` -> user overlays & waypoints | Exports every contour, cased highway, fire trail, icon, label, and 1 km MGRS grid line as razor-sharp PDF vector geometry at 300+ DPI. |

> **Important:** Never add a layer-level `Multiply` blend mode or raster effect to layers on the upper vector stack — Qt will silently rasterize overlapping vector geometry on PDF export.

---

## 2. Directory Layout

```text
assets/
├── icons/                # Parameterized SVG markers (huts, campgrounds, trigs, towers, etc.)
└── styles/
    ├── color/            # Native QGIS 3.40 Color theme .qml files (one per layer role)
    └── bw/               # Native QGIS 3.40 Laser-Print B&W theme .qml files
```

Every `.qml` file corresponds to a canonical layer role defined in `STYLE_ROLES` inside [`src/gridforge/render/styles.py`](../src/gridforge/render/styles.py) (e.g., `contours.qml`, `transport.qml`, `au_state_trails.qml`, `au_state_pois.qml`, `mgrs_grid_1km.qml`).

---

## 3. Editing Styles Interactively in QGIS Desktop

To visually experiment with line weights, color ramps, label placement, or hatch patterns in **QGIS Desktop 3.40+**:

1. **Generate a workspace with `--keep-workdir`:**

   ```bash
   docker run --rm -v "$(pwd)/out:/out" gridforge:dev \
     gridforge render --center "-33.732,150.312" --scale 25000 --paper a4 \
       --dem-source au-elvis --au-overlay au-nsw-trails \
       --keep-workdir --output /out/style_test.pdf
   ```

   GridForge prints the kept workspace directory containing `osm.gpkg`, `terrain.gpkg`, `hillshade.tif`, `mgrs.gpkg`, and any regional overlay `.gpkg` files.

2. **Load layers into QGIS Desktop:**
   - Open QGIS Desktop (`3.40 LTR` recommended).
   - Set the Project CRS to the sheet's local UTM EPSG (e.g., `EPSG:32756`) and set canvas scale to `1:25,000`.
   - Drag in the GeoPackage layers (`osm.gpkg`, `terrain.gpkg`, `mgrs.gpkg`) and `hillshade.tif`.

3. **Apply an existing GridForge `.qml`:**
   - Right-click any layer -> **Properties** -> **Symbology** -> **Style** -> **Load Style...**
   - Pick the matching file from `assets/styles/color/<role>.qml` or `assets/styles/bw/<role>.qml`.

4. **Tweak & Save:**
   - Adjust colors, stroke widths (always in **Millimeters**, never Map Units, so symbology prints consistently across paper sizes), or label rules.
   - Click **Style** -> **Save Style...** -> **QGIS Layer Style File (`.qml`)**.

---

## 4. Programmatic Style Authority & Regenerating Native `.qml` Files

Because GridForge dynamically adapts line widths, contour labels, and icon sizes across `1:15,000`, `1:24,000`, `1:25,000`, `1:48,000`, and `1:50,000` (and honors `CartoOptions` toggles like `--contour-density`, `--hillshade-strength`, and `--label-density`), the canonical style builders live in [`src/gridforge/render/styles.py`](../src/gridforge/render/styles.py).

After editing any `_build_*_style()` function in `src/gridforge/render/styles.py`, regenerate **all** native QGIS 3.40 `.qml` files in `assets/styles/{color,bw}/` cleanly inside the Docker container:

```bash
docker run --rm -v "$(pwd):/app" gridforge:dev python3 -m gridforge.render.styles
```

Or export to a scratch directory for diffing before committing:

```bash
docker run --rm -v "$(pwd):/app" gridforge:dev python3 -m gridforge.render.styles /tmp/qml_preview
```

Every exported `.qml` is tagged with the `gridforge-native-qml` marker and validated against QGIS 3.40's `saveNamedStyle()` XML serializer.

---

## 5. Design Rules for Color & B&W Themes

- **Color Theme (`assets/styles/color/`):**
  - Keep forest/landcover tints pastel (`#D9EAD3`, `#E8F0DC`) so `Multiply` hillshade and brown contours (`#9E6B43`) remain legible.
  - Reserve high-saturation accents strictly for operational features: burnt-orange (`#C84B0F`) for state fire trails, magenta/crimson for hazards & aviation, cyan/emerald for water & RF viewshed coverage, and dark blue/black (`#1B365D` / `#111111`) for the MGRS grid.
- **Laser-Print B&W Theme (`assets/styles/bw/`):**
  - Never rely on hue differences or mid-gray fills that turn muddy on monochrome office laser printers.
  - Differentiate landcover using **geometric vector patterns** (diagonal forest hatches, stippled scrub/rock dots, white open ground), use crisp cased roads (black casing + pure white core), keep hillshade bright/subtle, and verify readability at 300 DPI grayscale!
- **SVG Icons (`assets/icons/`):**
  - Keep SVGs minimal, centered on a clean integer `viewBox` (e.g., `0 0 24 24`), with explicit stroke/fill parameters (`param(fill)`, `param(outline)`) where Color vs. B&W recoloring is desired.
  - Never reference external URLs or system fonts inside SVG files.
