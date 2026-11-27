#!/usr/bin/env python3
"""GridForge release & repository marketing hygiene preflight check.

Verifies before cutting or publishing a release (and continuously in CI):
1. Version parity across pyproject.toml, src/gridforge/__init__.py,
   web/package.json, web/package-lock.json, CHANGELOG.md, and optional --tag.
2. README.md marketing quality:
   - Custom hero banner (docs/images/banner.png) exists and is embedded.
   - Live GitHub Actions CI & GHCR Release badges + dynamic Release badge exist.
   - Every referenced docs/images/* asset exists and fits size budgets.
   - Every relative markdown file link in README.md resolves on disk.
   - User-facing Quickstart/Cookbook uses ghcr.io/jremick/gridforge (not gridforge:dev).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover - Python 3.9/3.10 fallback
    tomllib = None

ROOT = Path(__file__).resolve().parent.parent

MAX_BANNER_BYTES = 1_500_000
MAX_JPG_BYTES = 800_000
MAX_PNG_BYTES = 1_500_000
MAX_GIF_BYTES = 2_500_000


def _read_pyproject_version(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    if tomllib is not None:
        return str(tomllib.loads(text)["project"]["version"])
    in_project = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("["):
            in_project = stripped == "[project]"
            continue
        if in_project:
            m = re.match(r'^version\s*=\s*"([^"]+)"', stripped)
            if m:
                return m.group(1)
    raise RuntimeError(f"Could not find [project].version in {path}")


def _read_versions() -> dict[str, str]:
    py_ver = _read_pyproject_version(ROOT / "pyproject.toml")

    init_text = (ROOT / "src/gridforge/__init__.py").read_text(encoding="utf-8")
    m_init = re.search(r'^__version__\s*=\s*"([^"]+)"', init_text, re.MULTILINE)
    if not m_init:
        raise RuntimeError("Could not find __version__ in src/gridforge/__init__.py")

    web_pkg = json.loads((ROOT / "web/package.json").read_text(encoding="utf-8"))
    web_lock = json.loads((ROOT / "web/package-lock.json").read_text(encoding="utf-8"))
    lock_root = str(web_lock.get("packages", {}).get("", {}).get("version", ""))

    return {
        "pyproject.toml": py_ver,
        "src/gridforge/__init__.py": m_init.group(1),
        "web/package.json": str(web_pkg["version"]),
        "web/package-lock.json": str(web_lock["version"]),
        "web/package-lock.json[packages.'']": lock_root,
    }


def check_release(tag: str | None = None) -> list[str]:
    errors: list[str] = []

    versions = _read_versions()
    unique = set(versions.values())
    if len(unique) != 1:
        errors.append(f"Version mismatch across package manifests: {versions}")
    version = versions["pyproject.toml"]

    if tag:
        expected = tag.lstrip("v")
        if version != expected:
            errors.append(f"Tag {tag} does not match pyproject.toml version {version}")

    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    if f"## [{version}]" not in changelog:
        errors.append(f"CHANGELOG.md is missing release heading '## [{version}]'")

    readme_path = ROOT / "README.md"
    readme = readme_path.read_text(encoding="utf-8")

    # Required hero banner & live badges
    if "docs/images/banner.png" not in readme:
        errors.append("README.md must embed the hero banner 'docs/images/banner.png'")
    if "actions/workflows/ci.yml/badge.svg" not in readme:
        errors.append("README.md is missing the live GitHub Actions CI badge")
    if "actions/workflows/release.yml/badge.svg" not in readme:
        errors.append("README.md is missing the live GitHub Actions GHCR Release badge")
    if "img.shields.io/github/v/release/" not in readme:
        errors.append("README.md is missing the dynamic GitHub Release version badge")

    # Validate all relative markdown links & images in README.md
    md_links = re.findall(r"!?\[[^\]]*\]\(([^)]+)\)", readme)
    html_srcs = re.findall(r'src="([^"]+)"', readme)
    for target in [*md_links, *html_srcs]:
        clean = target.strip().split()[0]
        if clean.startswith(("http://", "https://", "mailto:", "#")):
            continue
        rel_path = clean.split("#", 1)[0]
        if not rel_path:
            continue
        full = (ROOT / rel_path).resolve()
        if not full.exists():
            errors.append(f"Broken relative link in README.md: {rel_path}")
            continue
        if rel_path.startswith("docs/images/") and full.is_file():
            size = full.stat().st_size
            suffix = full.suffix.lower()
            if full.name == "banner.png" and size > MAX_BANNER_BYTES:
                errors.append(f"{rel_path} is {size} bytes (> {MAX_BANNER_BYTES})")
            elif suffix in {".jpg", ".jpeg"} and size > MAX_JPG_BYTES:
                errors.append(f"{rel_path} is {size} bytes (> {MAX_JPG_BYTES})")
            elif suffix == ".png" and full.name != "banner.png" and size > MAX_PNG_BYTES:
                errors.append(f"{rel_path} is {size} bytes (> {MAX_PNG_BYTES})")
            elif suffix == ".gif" and size > MAX_GIF_BYTES:
                errors.append(f"{rel_path} is {size} bytes (> {MAX_GIF_BYTES})")

    # User-facing sections (before Architecture / Testing) should use ghcr.io, not gridforge:dev
    dev_section_idx = readme.find("## Architecture")
    user_facing = readme[:dev_section_idx] if dev_section_idx != -1 else readme
    if "gridforge:dev" in user_facing:
        errors.append(
            "User-facing README sections reference 'gridforge:dev'; "
            "use 'ghcr.io/jremick/gridforge:latest' outside development/testing sections."
        )

    # Ensure docs/RELEASE_CHECKLIST.md and deploy/hosted-mode.md exist
    if not (ROOT / "docs/RELEASE_CHECKLIST.md").exists():
        errors.append("Missing docs/RELEASE_CHECKLIST.md")
    if not (ROOT / "deploy/hosted-mode.md").exists():
        errors.append("Missing deploy/hosted-mode.md")

    # Verify README sample PDF links match the target release tag and render_release_samples.sh
    sample_links = re.findall(
        r"/releases/download/(v[0-9]+\.[0-9]+\.[0-9]+)/(sample-[A-Za-z0-9_.-]+\.pdf)",
        readme,
    )
    if not sample_links:
        errors.append("README.md has no downloadable sample PDF release links")
    samples_script_path = ROOT / "scripts/render_release_samples.sh"
    samples_script = samples_script_path.read_text(encoding="utf-8") if samples_script_path.exists() else ""
    for link_tag, filename in sample_links:
        if tag and link_tag != tag:
            errors.append(
                f"README.md sample PDF link '{filename}' points to {link_tag} instead of {tag}"
            )
        if filename not in samples_script:
            errors.append(
                f"README.md links to '{filename}', which is not rendered/uploaded by scripts/render_release_samples.sh"
            )

    # Verify CI mounts the entire tests/ tree (so tests/e2e/ stays in parity with local pytest)
    ci_path = ROOT / ".github/workflows/ci.yml"
    if ci_path.exists():
        ci_text = ci_path.read_text(encoding="utf-8")
        if "tests/unit" in ci_text and "tests/e2e" not in ci_text and ":/app/tests" not in ci_text:
            errors.append(
                ".github/workflows/ci.yml appears to omit tests/e2e; mount the full tests/ directory."
            )

    # Repository privacy hygiene check across docs, workflows, scripts, source, tests, and web UI
    privacy_patterns = [
        ("macOS home path", re.compile(r"/" + "Users/[A-Za-z0-9._-]+/")),
        ("Linux home path", re.compile(r"/" + "home/[A-Za-z0-9._-]+/")),
        ("Windows user path", re.compile(r"C:\\\\" + r"Users\\[A-Za-z0-9._-]+\\")),
    ]
    _text_suffixes = {
        ".py", ".ts", ".tsx", ".js", ".mjs", ".cjs", ".json", ".css", ".html",
        ".md", ".yml", ".yaml", ".sh", ".toml", ".txt", ".qml", ".ini", ".cfg",
    }
    scan_paths: list[Path] = [
        ROOT / "AGENTS.md",
        ROOT / "README.md",
        ROOT / "CHANGELOG.md",
        ROOT / "CONTRIBUTING.md",
        ROOT / "Dockerfile",
        ROOT / "docker-compose.yml",
        ROOT / "docker-compose.ghcr.yml",
        ROOT / "pyproject.toml",
        *(ROOT / "docs").glob("*.md"),
        *(ROOT / "deploy").glob("*.md"),
        *(ROOT / ".github/workflows").glob("*.yml"),
        *(ROOT / "scripts").glob("*.sh"),
        *(ROOT / "scripts").glob("*.py"),
    ]
    for tree in (ROOT / "src", ROOT / "tests", ROOT / "web" / "src"):
        if tree.is_dir():
            for p in tree.rglob("*"):
                if p.is_file() and p.suffix.lower() in _text_suffixes:
                    scan_paths.append(p)
    for path in scan_paths:
        if not path.is_file() or path.resolve() == Path(__file__).resolve():
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        rel = path.relative_to(ROOT)
        for label, pattern in privacy_patterns:
            if pattern.search(text):
                errors.append(f"Privacy check failed ({label}) in {rel}")

    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description="GridForge release & marketing preflight check")
    parser.add_argument("--tag", default=None, help="Optional release tag to verify (e.g. v1.4.0)")
    args = parser.parse_args()

    errors = check_release(tag=args.tag)
    if errors:
        print("RELEASE PREFLIGHT FAILED:", file=sys.stderr)
        for err in errors:
            print(f"  - {err}", file=sys.stderr)
        return 1

    versions = _read_versions()
    print(
        f"RELEASE_PREFLIGHT_OK: version={versions['pyproject.toml']}"
        + (f" tag={args.tag}" if args.tag else "")
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
