## Summary
Briefly explain what this PR adds or fixes and link any related issues.

## Changes
-

## Verification
- [ ] Offline test suite passes inside Docker (`pytest -q tests/unit tests/integration`)
- [ ] Frontend builds cleanly with zero TypeScript/Vite errors (`cd web && npm run build`) *(if `web/` touched)*
- [ ] Native QGIS 3.40 `.qml` files regenerated via `python3 -m gridforge.render.styles` *(if `styles.py` touched)*
- [ ] All new geospatial data sources use commercial-friendly open licenses (Public Domain / ODbL / CC BY) with collar attribution emitted *(if `sources/` touched)*
- [ ] Visually inspected rendered PDF / UI output *(attach screenshots below for cartography or UI changes)*

## Screenshots (if applicable)
<!-- Drag and drop rasterized PDF previews or Print Studio screenshots here -->
