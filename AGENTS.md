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
Every `vX.Y.Z` release **must** follow [docs/RELEASE_CHECKLIST.md](docs/RELEASE_CHECKLIST.md) and the `.agents/skills/gridforge-release/SKILL.md` runbook, including:
1. Keeping [README.md](README.md) structured as a crisp product landing page (hero banner, live badges, 30-second GHCR quickstart above the fold, up-to-date comparison table, visuals, and downloadable sample PDF links).
2. Verifying green `main` CI (with full `tests/` mount parity), running live multi-region open-data E2E smoke tests (`GRIDFORGE_RUN_LIVE=1`), updating deployment/env-var docs (`deploy/hosted-mode.md`), refreshing showcase samples/visuals for headline features, and scanning tracked files for repo privacy.
3. Passing `python3 scripts/release_preflight.py --tag vX.Y.Z` alongside offline pytest (`unit` + `integration` + `e2e`) and `npm --prefix web test`.
4. Waiting for multi-arch GHCR publish, attaching verified 300–600 DPI sample PDFs via `./scripts/render_release_samples.sh --image ghcr.io/jremick/gridforge:vX.Y.Z --upload vX.Y.Z`, and verifying public GHCR pull + GitHub metadata via `./scripts/sync_github_metadata.sh`.

See [CONTRIBUTING.md](CONTRIBUTING.md) for local Docker/Vite development commands and PR guidelines.
