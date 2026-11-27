# GridForge Release & Marketability Checklist

Follow this checklist for every `vX.Y.Z` release so code quality, visuals, documentation, container publishing, and GitHub discoverability stay consistently launch-ready.

---

## 1. Version Bump & Changelog

1. Bump the SemVer version identically across:
   - `pyproject.toml` (`[project].version`)
   - `src/gridforge/__init__.py` (`__version__`)
   - `web/package.json` and `web/package-lock.json`
2. Add a dated `## [X.Y.Z] - YYYY-MM-DD` section to `CHANGELOG.md` summarizing user-facing additions, fixes, and security/reliability hardening.

## 2. Visuals, Studio Demo & Product-Focused `README.md`

Keep `README.md` structured as a **product landing page** (not a reverse-chronological changelog — detailed release history lives in `CHANGELOG.md` and GitHub Releases):

- **Hero & Quickstart Above the Fold:** Keep `docs/images/banner.png`, live GitHub Actions badges (`CI` + `GHCR Multi-Arch`), dynamic `GitHub Release` badge, and the 30-second `docker compose -f docker-compose.ghcr.yml up -d` Quickstart at the top.
- **Fresh Showcase Visuals:**
  - If new cartographic themes, overlays, regions, or supplement pages shipped, generate a crisp 4-panel showcase banner under `docs/images/` (like `docs/images/v14-showcase.jpg`, `<= 600 KB`) from real 300–600 DPI renders, visually inspect it, and add detailed screenshots to `docs/GALLERY.md` (keeping `README.md` focused on 1–2 headline visuals).
  - If Studio UI changed meaningfully, update the Studio screenshot in `docs/GALLERY.md` (`<= 300 KB`) and/or regenerate the animated Studio walkthrough GIF (`docs/images/studio-demo.gif`, `<= 2.5 MB`).
- **Keep Capability Tables, Docs & Metrics Current:** Keep `README.md` concise and skimmable while updating the comparison table, key metrics, and downloadable sample PDF links to match the new release tag, and keep detailed references current in `docs/DATA_SOURCES.md`, `docs/CLI_COOKBOOK.md`, `docs/GALLERY.md`, and `deploy/hosted-mode.md`.
- **Keep Planned Roadmap Current:** Prune or check off shipped items in `README.md`'s **Planned Roadmap** section and refresh upcoming high-value priorities whenever features land or a release is cut.
- **Showcase Headline Features in Sample PDFs:** Update `scripts/render_release_samples.sh` whenever major new scales, DPI tiers, themes, or regions ship so visitors can inspect real vector outputs from the new release.
- **Use Published Images in User Examples:** Use `ghcr.io/jremick/gridforge:latest` in Quickstart and CLI Cookbook commands (reserve `GRIDFORGE_IMAGE=gridforge:dev` strictly for local development/testing sections). Update the pinned version and multi-arch digest in `examples/render-examples.sh` right after GHCR publication (Section 6).

## 3. Automated Verification, CI Mount Parity & Release Preflight

Run all five gates before tagging:

```bash
# 1. Confirm latest GitHub Actions CI run on main is 100% green and .github/workflows/ci.yml
#    mounts every repo path read by tests/unit, tests/integration, and tests/e2e
gh run list --branch main --limit 3

# 2. Release & marketing hygiene gate (version parity, README links/badges/images)
python3 scripts/release_preflight.py --tag vX.Y.Z

# 3. Frontend unit tests & production bundles (Studio SPA + MCP App)
npm --prefix web test && npm --prefix web run build && npm --prefix web run build:mcp-app

# 4. Full offline PyQGIS test suite inside the rebuilt test image
docker build --target test -t gridforge:dev .
docker run --rm -v "$(pwd):/app" gridforge:dev pytest -q tests/unit tests/integration tests/e2e

# 5. Live multi-region E2E renders & visual inspection (verifies live Overpass/DEM/overlay feeds)
#    Run locally/on your cloud VM OR trigger directly on GitHub Actions:
#      gh workflow run ci.yml -f run_live=true
docker run --rm -e GRIDFORGE_RUN_LIVE=1 -v "$(pwd):/app" gridforge:dev \
  pytest -q tests/integration -m live
```

Verify live Studio UI (`0` console errors, `0 px` topbar clipping at `1440×900`, idle `web + worker < 200 MiB` RAM) and run a repo-privacy scan over tracked files and comments (zero local paths, personal emails, secrets, cloud account/project IDs, or machine-specific hostnames; public noreply commit author).

## 4. Commit, Tag & Push

1. Confirm clean `git status`, zero secrets, and repo-local noreply author email (`git config user.email`).
2. Cut clean atomic Conventional Commits and an annotated SemVer tag:
   ```bash
   git tag -a vX.Y.Z -m "GridForge vX.Y.Z — <concise headline>"
   git push origin main --follow-tags
   ```
3. Pushing `vX.Y.Z` automatically triggers **both** GitHub Actions workflows:
   - `.github/workflows/ci.yml` (offline tests + preflight + web build)
   - `.github/workflows/release.yml` (release preflight + multi-arch `linux/amd64` & `linux/arm64` push to `ghcr.io/jremick/gridforge:vX.Y.Z`, `:vX.Y`, and `:latest`)

## 5. Publish GitHub Release & Attach Sample 300 DPI PDFs

1. Create the GitHub Release:
   ```bash
   gh release create vX.Y.Z \
     --title "GridForge vX.Y.Z — <headline>" \
     --notes-file <release_notes.md>
   ```
2. Render and attach the verified 300–600 DPI sample PDFs so visitors can zoom into real vector sheets without running Docker (automatically runs on GitHub Actions in `.github/workflows/release.yml`, or re-trigger in the cloud with `gh workflow run release.yml -f tag=vX.Y.Z -f render_samples=true`, or run from a cloud VM):
   ```bash
   ./scripts/render_release_samples.sh --image ghcr.io/jremick/gridforge:vX.Y.Z --upload vX.Y.Z
   ```

## 6. Pin Examples Digest, Sync GitHub Metadata & Verify Public GHCR Pull

1. Fetch the newly published multi-architecture OCI index digest (`linux/amd64` + `linux/arm64`) and update `examples/render-examples.sh` on `main`:
   ```bash
   docker buildx imagetools inspect ghcr.io/jremick/gridforge:vX.Y.Z
   ```
2. Run the idempotent metadata & registry check script:

```bash
./scripts/sync_github_metadata.sh
```

This updates the GitHub description and up to 20 high-discovery repository topics, checks whether the custom Social Preview image (`docs/images/banner.png`, `1280×640`) is uploaded under **Repo Settings → Social Preview**, and verifies anonymous public `HTTP 200` pull access on `ghcr.io/jremick/gridforge:latest`.
