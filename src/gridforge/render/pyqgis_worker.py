"""Isolated PyQGIS 3.40 subprocess renderer implementing ``MapRenderer``.

Every render runs in a fresh spawned Python subprocess (never ``fork()``) so
Qt/QGIS C++ memory is completely reclaimed on exit with zero leak accumulation.
"""

from __future__ import annotations

import argparse
import gc
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

from gridforge.config import JobWorkspace, RenderResult

logger = logging.getLogger(__name__)


def render_workspace_in_process(workspace: JobWorkspace, output_pdf: Path) -> RenderResult:
    """Execute PyQGIS rendering inside the current process (called by worker subprocess)."""
    from qgis.core import QgsApplication, QgsProject

    from gridforge.render.layout import build_print_layout, export_layout_to_pdf
    from gridforge.render.styles import populate_project_layers

    t0 = time.perf_counter()

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    runtime_dir = Path(os.environ.setdefault("XDG_RUNTIME_DIR", "/tmp/runtime-root"))
    runtime_dir.mkdir(parents=True, exist_ok=True)
    try:
        runtime_dir.chmod(0o700)
    except OSError:
        pass

    QgsApplication.setPrefixPath(os.environ.get("QGIS_PREFIX_PATH", "/usr"), True)
    qgs = QgsApplication([], False)
    qgs.initQgis()

    def _execute_render() -> Path:
        project = QgsProject.instance()
        project.clear()
        ordered_layers = populate_project_layers(project, workspace)
        print_layout, map_item = build_print_layout(project, workspace, ordered_layers)

        # Verify physical scale lock before exporting
        actual_scale = map_item.scale()
        expected_scale = float(workspace.layout.spec.scale)
        rel_err = abs(actual_scale - expected_scale) / expected_scale
        if rel_err > 1e-4:
            raise RuntimeError(
                f"Map scale drift detected before export: actual={actual_scale:.4f}, expected={expected_scale:.4f}"
            )

        exported = export_layout_to_pdf(
            print_layout,
            output_pdf,
            dpi=workspace.layout.spec.dpi,
            geopdf=workspace.layout.spec.geopdf,
        )

        # Explicitly release Qt/QGIS objects before project.clear() and exitQgis()
        del map_item
        del print_layout
        ordered_layers.clear()
        del ordered_layers
        project.clear()
        return exported

    try:
        out_path = _execute_render()
    finally:
        gc.collect()
        qgs.exitQgis()

    elapsed = round(time.perf_counter() - t0, 3)
    return RenderResult(
        output_pdf=out_path,
        page_width_mm=workspace.layout.page_width_mm,
        page_height_mm=workspace.layout.page_height_mm,
        scale=workspace.layout.spec.scale,
        utm_epsg=workspace.layout.utm_epsg,
        elapsed_seconds=elapsed,
        workspace_dir=workspace.job_dir,
    )


class PyQGISRenderer:
    """``MapRenderer`` implementation isolating PyQGIS inside a spawned subprocess."""

    def __init__(self, *, timeout_seconds: int = 300, use_xvfb: bool | None = None) -> None:
        self.timeout_seconds = timeout_seconds
        self.use_xvfb = use_xvfb

    def render_pdf(self, workspace: JobWorkspace, output_pdf: Path) -> RenderResult:
        """Spawn a fresh PyQGIS worker subprocess to render ``workspace`` to ``output_pdf``."""
        caller_output_pdf = Path(output_pdf)
        resolved_output_pdf = caller_output_pdf.resolve()
        resolved_output_pdf.parent.mkdir(parents=True, exist_ok=True)

        workspace.job_dir.mkdir(parents=True, exist_ok=True)
        manifest_path = workspace.job_dir / "render_manifest.json"
        result_path = workspace.job_dir / "render_result.json"
        manifest_path.write_text(workspace.model_dump_json(indent=2), encoding="utf-8")

        use_xvfb = (
            self.use_xvfb
            if self.use_xvfb is not None
            else os.environ.get("GRIDFORGE_USE_XVFB", "").strip().lower() in ("1", "true", "yes")
        )

        cmd: list[str] = []
        if use_xvfb:
            cmd.extend(["xvfb-run", "-a"])
        cmd.extend(
            [
                sys.executable,
                "-m",
                "gridforge.render.pyqgis_worker",
                str(manifest_path),
                str(resolved_output_pdf),
                "--result-json",
                str(result_path),
            ]
        )

        env = os.environ.copy()
        if use_xvfb:
            env["QT_QPA_PLATFORM"] = "xcb"
        else:
            env.setdefault("QT_QPA_PLATFORM", "offscreen")

        runtime_dir = Path(env.setdefault("XDG_RUNTIME_DIR", "/tmp/runtime-root"))
        runtime_dir.mkdir(parents=True, exist_ok=True)

        proc = subprocess.run(
            cmd,
            env=env,
            capture_output=True,
            text=True,
            timeout=self.timeout_seconds,
            check=False,
        )

        if proc.returncode != 0:
            raise RuntimeError(
                f"PyQGIS worker subprocess failed (exit={proc.returncode}).\n"
                f"Command: {' '.join(cmd)}\n"
                f"STDERR:\n{proc.stderr[-3000:]}\n"
                f"STDOUT:\n{proc.stdout[-1500:]}"
            )

        parsed: RenderResult | None = None
        if result_path.is_file():
            parsed = RenderResult.model_validate_json(result_path.read_text(encoding="utf-8"))
        else:
            for line in reversed(proc.stdout.splitlines()):
                line_s = line.strip()
                if line_s.startswith("{") and line_s.endswith("}"):
                    parsed = RenderResult.model_validate_json(line_s)
                    break

        if parsed is None:
            raise RuntimeError(
                f"PyQGIS worker exited 0 but did not produce RenderResult JSON.\nSTDOUT:\n{proc.stdout}"
            )

        return parsed.model_copy(update={"output_pdf": caller_output_pdf})


def _worker_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="GridForge isolated PyQGIS render worker")
    parser.add_argument("manifest_json", type=Path, help="Serialized JobWorkspace JSON")
    parser.add_argument("output_pdf", type=Path, help="Target PDF output path")
    parser.add_argument("--result-json", type=Path, default=None, help="Optional path to write RenderResult JSON")
    args = parser.parse_args(argv)

    workspace = JobWorkspace.model_validate_json(args.manifest_json.read_text(encoding="utf-8"))
    result = render_workspace_in_process(workspace, args.output_pdf)
    payload = result.model_dump_json()

    if args.result_json is not None:
        args.result_json.parent.mkdir(parents=True, exist_ok=True)
        args.result_json.write_text(payload, encoding="utf-8")

    print(json.dumps(json.loads(payload)))
    return 0


if __name__ == "__main__":
    raise SystemExit(_worker_main())
