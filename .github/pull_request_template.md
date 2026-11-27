## Summary
Briefly explain what this PR adds or fixes and link any related issues.

## Changes
-

## Verification
- [ ] Offline test suite passes inside Docker (`pytest -q tests/unit tests/integration tests/e2e`)
- [ ] Frontend tests and builds pass with zero TypeScript/Vite errors (`npm --prefix web test && npm --prefix web run build && npm --prefix web run build:mcp-app`) *(if `web/` touched)*
- [ ] Release & README hygiene preflight passes (`python3 scripts/release_preflight.py`)
- [ ] Native QGIS 3.40 `.qml` files regenerated via `python3 -m gridforge.render.styles` *(if `src/gridforge/render/styles.py` touched)*
- [ ] All new geospatial data sources use commercial-friendly open licenses (Public Domain / ODbL / CC BY) with collar attribution emitted *(if `src/gridforge/sources/` touched)*
- [ ] Visually inspected rendered PDF / UI output *(attach screenshots below for cartography or UI changes)*

## Screenshots (if applicable)
<!-- Drag and drop rasterized PDF previews or Print Studio screenshots here -->
