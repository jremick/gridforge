# GridForge — Repository & Agent Guide

Welcome to **GridForge** (`jremick/gridforge`, GPL-3.0). Keep these core invariants and workflows in mind whenever developing or shipping releases:

## Engineering Guardrails
- **True-Scale Local UTM Only:** Print sheets must project into their native local UTM zone (`EPSG:326xx`/`327xx`), verified within `<= 0.15 mm` scale error and `< 0.05 mm` skew. Never use Web Mercator (`EPSG:3857`) for print layouts.
- **Dual-Stack Vector Crispness:** Keep rasters (hillshade, slope shading, viewshed) on the bottom `QgsLayoutItemMap` and all vectors/contours/MGRS on the top transparent map (`rasterizeWholeImage=False`).
- **4 Dedicated Cartography Themes:** Maintain explicit palettes and regenerated QGIS 3.40 `.qml` files in `assets/styles/{color,bw,night-red,alpine}/` (`python3 -m gridforge.render.styles`) — see [docs/STYLING_GUIDE.md](docs/STYLING_GUIDE.md).
- **16 GB Host Memory Budget:** Keep `gridforge.web.app` free of `qgis`/`PyQt5` imports, isolate PyQGIS in ephemeral `spawn` subprocesses (`< 2 GB` peak RSS), and stream remote files disk-to-disk.
- **Strict Open-Data Licensing:** Only add commercial-friendly, zero-auth open data (**Public Domain**, **ODbL**, **CC BY 3.0/4.0**, **OGL-Canada**, **Copernicus DEM**) with accurate collar attribution. Never add `CC BY-NC` or key-gated commercial feeds.
- **Local-First Security:** Keep Compose/CLI/Vite defaults on `127.0.0.1` with `SameOriginWritesMiddleware` + `TrustedHostMiddleware` protecting all mutating endpoints.

## Release & Marketability Workflow
Every `vX.Y.Z` release **must** follow [docs/RELEASE_CHECKLIST.md](docs/RELEASE_CHECKLIST.md), including:
1. Keeping [README.md](README.md) structured as a crisp product landing page (hero banner, live badges, 30-second GHCR quickstart above the fold, up-to-date comparison table, visuals, and downloadable sample PDF links).
2. Passing `python3 scripts/release_preflight.py --tag vX.Y.Z` alongside offline pytest and `npm --prefix web test`.
3. Attaching verified 300 DPI sample PDFs via `./scripts/render_release_samples.sh --upload vX.Y.Z`.
4. Syncing GitHub description/topics and verifying public GHCR pull access via `./scripts/sync_github_metadata.sh`.

See [CONTRIBUTING.md](CONTRIBUTING.md) for local Docker/Vite development commands and PR guidelines.
