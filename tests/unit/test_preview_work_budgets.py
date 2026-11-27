"""Offline checks for GridForge preview and queued atlas work limits."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfReader, PdfWriter

from gridforge.config import ColorMode, Orientation, PaperSize
from gridforge.geo.crs import compute_sheet_layout
from gridforge.web import db, viewshed, worker
from gridforge.web.app import create_app
from gridforge.web.schemas import JobStatus


def test_rf_preview_refuses_long_link_before_selecting_source(tmp_path, monkeypatch):
    def unexpected_source(*args, **kwargs):
        pytest.fail("An over-budget preview selected an elevation source")

    monkeypatch.setattr(viewshed, "_resolve_preview_elevation_source", unexpected_source)
    app = create_app(
        data_dir=tmp_path / "jobs",
        packs_dir=tmp_path / "packs",
        web_dist_dir=tmp_path / "dist",
    )
    client = TestClient(app, base_url="http://localhost:8080")
    response = client.post(
        "/api/rf-link-profile",
        json={"from_lat": -33.73, "from_lon": 150.31, "to_lat": -35.28, "to_lon": 149.13},
    )
    assert response.status_code == 422
    assert "100 km" in response.json()["detail"]


def test_wide_ruler_keeps_geodesic_results_without_terrain_fetch(tmp_path, monkeypatch):
    def unexpected_source(*args, **kwargs):
        pytest.fail("An over-budget ruler selected an elevation source")

    monkeypatch.setattr(viewshed, "_resolve_preview_elevation_source", unexpected_source)
    app = create_app(
        data_dir=tmp_path / "jobs",
        packs_dir=tmp_path / "packs",
        web_dist_dir=tmp_path / "dist",
    )
    client = TestClient(app, base_url="http://localhost:8080")
    response = client.post(
        "/api/ruler-metrics",
        json={
            "points": [{"lat": -33.73, "lon": 150.31}, {"lat": -35.28, "lon": 149.13}],
            "dem_source": "auto",
        },
    )
    assert response.status_code == 200
    assert response.json()["total_distance_m"] > 100_000
    assert response.json()["segments"][0]["grade_pct"] is None


def test_observer_data_margin_cannot_wrap_across_antimeridian(tmp_path, monkeypatch):
    def unexpected_source(*args, **kwargs):
        pytest.fail("A wrapped DEM footprint selected an elevation source")

    monkeypatch.setattr(viewshed, "_resolve_preview_elevation_source", unexpected_source)
    app = create_app(
        data_dir=tmp_path / "jobs",
        packs_dir=tmp_path / "packs",
        web_dist_dir=tmp_path / "dist",
    )
    client = TestClient(app, base_url="http://localhost:8080")
    response = client.post(
        "/api/viewshed-preview",
        json={"observer": {"lat": -16.8, "lon": 179.98, "max_distance_m": 25_000}},
    )
    assert response.status_code == 422
    assert "antimeridian" in response.json()["detail"]


def test_queued_strip_sheet_uses_worker_ordinals_for_paths(tmp_path, monkeypatch):
    data_dir = tmp_path / "jobs"
    data_dir.mkdir()
    db_path = data_dir / "jobs.sqlite3"
    db.init_db(db_path)
    labels = ["North/West", "East sector"]
    centers = [(-33.732, 150.312), (-33.721, 150.324)]
    job_id = "02f0933f10b34419"
    db.create_job(
        db_path,
        job_id=job_id,
        title="Fixture atlas",
        center="-33.732, 150.312",
        center_mgrs=None,
        scale=25000,
        paper=PaperSize.A4,
        orientation=Orientation.PORTRAIT,
        color_mode=ColorMode.COLOR,
        request_json=json.dumps(
            {
                "title": "Fixture atlas",
                "strip_sheets": [
                    {"sheet_id": label, "center_lat": lat, "center_lon": lon}
                    for label, (lat, lon) in zip(labels, centers)
                ],
            }
        ),
    )
    claimed = db.claim_next_job(db_path)
    assert claimed is not None
    scratch_paths: list[Path] = []
    page_paths: list[Path] = []
    titles: list[str] = []

    def prepare(spec, work_dir, **kwargs):
        scratch_paths.append(work_dir)
        return compute_sheet_layout(spec)

    def render(self, layout, output_pdf):
        page_paths.append(output_pdf)
        titles.append(layout.spec.title)
        with PdfWriter() as writer:
            writer.add_blank_page(width=595, height=842)
            writer.write(output_pdf)

    monkeypatch.setattr(worker.runner, "prepare_job_workspace", prepare)
    monkeypatch.setattr(worker.PyQGISRenderer, "render_pdf", render)
    result = worker.process_single_job(
        claimed, db_path=db_path, data_dir=data_dir, packs_dir=tmp_path / "packs",
    )

    assert result is not None
    assert result.status == JobStatus.SUCCEEDED, result.error
    assert [path.name for path in scratch_paths] == ["sheet_01", "sheet_02"]
    assert [path.name for path in page_paths] == ["sheet_01.pdf", "sheet_02.pdf"]
    assert scratch_paths[0].parent == scratch_paths[1].parent
    assert all(not path.exists() for path in scratch_paths)
    assert all(label in title for label, title in zip(labels, titles))
    output = data_dir / job_id / "artifacts" / result.artifacts[0].filename
    assert len(PdfReader(output).pages) == 2
