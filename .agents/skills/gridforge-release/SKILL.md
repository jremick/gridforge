---
name: gridforge-release
description: Prepares, verifies, tags, and publishes a GridForge vX.Y.Z release — covering CI mount parity, live multi-region open-data E2E checks, deployment doc audits, sample PDF and Studio visual refreshes, release preflight, multi-arch GHCR publishing, GitHub Release assets, and metadata sync. Use whenever cutting a release, preparing release notes, or checking release readiness.
---

# GridForge Release Runbook

Follow this ordered checklist whenever preparing or cutting a `vX.Y.Z` release of GridForge. Never skip a gate without recording the reason in the release notes.

## 1. Pre-Tag Verification Gates

1. **Confirm `main` CI & Mount Parity:**
   - Run `gh run list --branch main --limit 3` and verify the latest `CI` run is green.
   - Verify `.github/workflows/ci.yml` mounts the full `tests/` tree (including `tests/unit`, `tests/integration`, and `tests/e2e`) and any new runtime config/data directories read by tests.
2. **Live Multi-Region Open-Data E2E Check:**
   - Run live integration tests inside the freshly built container so upstream open-data feeds (USGS 3DEP, NRCan HRDEM, GA ELVIS/QLD, LINZ, Overpass) are verified against real network responses:
     ```bash
     docker run --rm -e GRIDFORGE_RUN_LIVE=1 -v "$(pwd):/app" gridforge:dev \
       pytest -q tests/integration -m live
     ```
3. **Deployment & Configuration Docs Audit:**
   - Check `git diff <prev-tag>..HEAD` for new `GRIDFORGE_*` environment variables, runtime modes, CLI flags, or API endpoints.
   - Update `deploy/hosted-mode.md`, `README.md`, and `docs/STYLING_GUIDE.md` (if themes/QML changed) so operators never have to read source code to configure new features.
4. **Showcase Sample PDFs & Visual Refresh:**
   - If major new scales, DPI tiers, themes, regions, or supplements shipped, add or update a showcase render in `scripts/render_release_samples.sh` and matching row in `README.md`.
   - If Studio UI changed noticeably, capture a fresh `1440×900` desktop screenshot (`<= 300 KB` JPEG, green worker status, clean telemetry) and visually inspect it before embedding in `README.md`.
5. **Repository Privacy & Hygiene Scan:**
   - Confirm zero workstation paths, private emails, secrets, cloud account/project IDs, or machine-specific SSH hostnames in tracked files, scripts, workflows, or comments.
   - Confirm commit author uses the repository's public/noreply identity (`git config user.email`).

## 2. Version Bump, Changelog & Mechanical Preflight

1. Bump SemVer identically across:
   - `pyproject.toml` (`[project].version`)
   - `src/gridforge/__init__.py` (`__version__`)
   - `web/package.json` and `web/package-lock.json`
   - `.github/workflows/release.yml` (default `workflow_dispatch` tag)
2. Add a dated `## [X.Y.Z] - YYYY-MM-DD` section to `CHANGELOG.md`, update `README.md` sample PDF download links to `vX.Y.Z`, and prune/refresh `README.md`'s **Planned Roadmap** section for shipped and upcoming high-value work.
3. Run all local gates:
   ```bash
   python3 scripts/release_preflight.py --tag vX.Y.Z
   npm --prefix web test && npm --prefix web run build
   ```
   (`release_preflight.py` mechanically checks version parity, `CHANGELOG.md`, `README.md` badges/images/links, sample PDF tag & script parity, `deploy/hosted-mode.md`, CI `tests/e2e` coverage, and privacy path hygiene.)

## 3. Tag, Publish GHCR & Attach Sample PDFs

1. Commit release prep with a clean Conventional Commit and create an annotated SemVer tag:
   ```bash
   git tag -a vX.Y.Z -m "GridForge vX.Y.Z — <concise headline>"
   git push origin main --follow-tags
   ```
2. Wait for `.github/workflows/release.yml` to finish pushing multi-arch `linux/amd64` + `linux/arm64` images (`ghcr.io/jremick/gridforge:vX.Y.Z`, `:vX.Y`, `:latest`).
3. Create the GitHub Release matching `CHANGELOG.md`:
   ```bash
   gh release create vX.Y.Z --title "GridForge vX.Y.Z — <headline>" --notes-file <notes.md>
   ```
4. Render and attach all verified sample PDFs directly from the published `vX.Y.Z` image:
   ```bash
   ./scripts/render_release_samples.sh --image ghcr.io/jremick/gridforge:vX.Y.Z --upload vX.Y.Z
   ```
5. Sync GitHub repository description/topics and confirm anonymous `HTTP 200` pull access on GHCR:
   ```bash
   ./scripts/sync_github_metadata.sh
   ```
