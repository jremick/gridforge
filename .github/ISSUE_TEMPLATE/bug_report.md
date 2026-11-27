---
name: Bug Report
about: Report a rendering, scale accuracy, data source, CLI, or Studio UI bug
title: "[Bug]: "
labels: bug
assignees: ""
---

## Summary
A clear, concise description of what went wrong.

## Reproduction Steps
1. Command or Studio action (paste **Copy CLI** command or URL permalink if using Print Studio):
   ```bash
   docker run --rm -v "$(pwd)/out:/out" ghcr.io/jremick/gridforge:latest gridforge render ...
   ```
2. Coordinates / MGRS, scale, paper size, DEM source, and overlays enabled:
   - Center:
   - Scale / Paper:
   - DEM / Overlays:

## Expected Behavior
What you expected to happen.

## Actual Behavior & Logs
Paste error output, stack traces, or describe visual/cartographic artifacts (screenshots welcome).

```text
(paste logs here)
```

## Environment
- **GridForge version / image tag:** (e.g. `v1.7.0`, `ghcr.io/jremick/gridforge:latest`, or git SHA)
- **Host OS & Architecture:** (e.g. macOS Apple Silicon `arm64`, Ubuntu 24.04 `amd64`)
- **Mode:** Docker Compose Studio / Docker CLI / 100% offline `--pack-id`
