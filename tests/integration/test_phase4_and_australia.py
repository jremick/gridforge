"""Phase 4 Self-Hosted Web App & Australia-Wide Mapping E2E Integration Tests.

Covers:
  1. Offline CI tests (default, zero network):
     - Offline Australia regional pack builder + 100% air-gapped `--pack` render
       with all 4 mock AU national overlays (`au-hydro`, `au-parks`, `au-infra`, `au-aviation`)
       and `AHD` vertical datum verification.
     - CLI `gridforge pack list-regions` & `gridforge pack inspect`.
     - In-process FastAPI `TestClient` sanity checks (`/api/health`, `/api/packs`, `/api/footprint`
       for Sydney, Uluru, Cradle Mountain, Perth).
  2. Live Australia Multi-Region verification (`@pytest.mark.live` + `GRIDFORGE_RUN_LIVE=1`):
     - Sydney Harbour / North Shore NSW (`-33.8568, 151.2153`, `56H`, `EPSG:32756`, GA 5m LiDAR `AHD`)
     - Uluru / Kata Tjuta NT (`-25.3444, 131.0369`, `52J`, `EPSG:32752`, GA SRTM 1s `AHD` + AU overlays)
     - Cradle Mountain TAS (`-41.684, 145.951`, `55G`, `EPSG:32755`, B&W + AU overlays + `.mbtiles` + `.kmz`)
     - Perth / Kings Park WA (`-31.950, 115.860`, `50J`, `EPSG:32750`, GA 5m LiDAR `AHD`)
  3. Live Phase 4 `docker compose up` Web App & Memory Guardrail tests (`GRIDFORGE_WEB_URL` set):
     - `GET /` HTML + built MapLibre GL JS static assets (`200 OK`)
     - `GET /api/health` (`status == "ok"`, `worker_alive == True`)
     - `POST /api/footprint` across Sydney, Uluru, Cradle Mountain, Perth
     - Async `POST /api/jobs` (PDF + `.kmz`) -> poll `GET /api/jobs/{id}` -> download artifacts
       -> `verify_pdf.py` + `verify_exports.py` + `peak_rss_mb < 2048 MB`
"""

from __future__ import annotations

import json
import os
import re
import resource
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest

from gridforge.config import (
    AUOverlayKind,
    BBox,
    CartoOptions,
    ColorMode,
    DemSourceKind,
    Orientation,
    PackManifest,
    PaperSize,
    SheetLayout,
    SheetSpec,
    Units,
)
from gridforge.geo.crs import compute_sheet_layout
from gridforge.pipeline.runner import run_render_job

_INTEGRATION_DIR = Path(__file__).resolve().parent
if str(_INTEGRATION_DIR) not in sys.path:
    sys.path.insert(0, str(_INTEGRATION_DIR))

from verify_exports import (  # noqa: E402
    assert_garmin_kmz_valid,
    assert_mbtiles_valid,
)
from verify_pdf import assert_pdf_valid  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures"

# Australia multi-region verification sites
AU_SITES: dict[str, dict[str, Any]] = {
    "sydney": {
        "lat": -33.8568,
        "lon": 151.2153,
        "epsg": 32756,
        "gzd": "56H",
        "title": "SYDNEY HARBOUR — NORTH SHORE",
    },
    "uluru": {
        "lat": -25.3444,
        "lon": 131.0369,
        "epsg": 32752,
        "gzd": "52J",
        "title": "ULURU — KATA TJUTA NT",
    },
    "cradle_mountain": {
        "lat": -41.684,
        "lon": 145.951,
        "epsg": 32755,
        "gzd": "55G",
        "title": "CRADLE MOUNTAIN — TASMANIA",
    },
    "perth": {
        "lat": -31.950,
        "lon": 115.860,
        "epsg": 32750,
        "gzd": "50J",
        "title": "PERTH — KINGS PARK WA",
    },
}

BLUE_MOUNTAINS_LAT = -33.732
BLUE_MOUNTAINS_LON = 150.312
BLUE_MOUNTAINS_EPSG = 32756
BLUE_MOUNTAINS_GZD = "56H"

RUN_LIVE = os.environ.get("GRIDFORGE_RUN_LIVE", "").strip().lower() in {"1", "true", "yes"}
WEB_URL = os.environ.get("GRIDFORGE_WEB_URL", "").strip().rstrip("/")


def _create_mock_au_overlay_gpkgs(out_dir: Path, layout: SheetLayout) -> dict[str, Path]:
    """Create valid UTM GeoPackages matching all 4 Australia CC BY 4.0 national overlay schemas."""
    from osgeo import ogr, osr

    ogr.UseExceptions()
    osr.UseExceptions()
    out_dir.mkdir(parents=True, exist_ok=True)

    srs = osr.SpatialReference()
    srs.ImportFromEPSG(layout.utm_epsg)
    srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    driver = ogr.GetDriverByName("GPKG")

    cx = layout.center_easting
    cy = layout.center_northing
    created: dict[str, Path] = {}

    # 1. au-hydro (au_hydro_points, au_hydro_lines, au_hydro_polys)
    hydro_path = out_dir / "au-hydro.gpkg"
    ds = driver.CreateDataSource(str(hydro_path))
    lyr_p = ds.CreateLayer("au_hydro_points", srs=srs, geom_type=ogr.wkbPoint)
    for fname in ("name", "featuretype", "type", "perenniality"):
        lyr_p.CreateField(ogr.FieldDefn(fname, ogr.OFTString))
    pt = ogr.Geometry(ogr.wkbPoint)
    pt.AddPoint(cx - 450, cy + 320)
    feat = ogr.Feature(lyr_p.GetLayerDefn())
    feat.SetGeometry(pt)
    feat.SetField("name", "Jamison Bore No 2")
    feat.SetField("featuretype", "Bore")
    feat.SetField("type", "Water")
    feat.SetField("perenniality", "Perennial")
    lyr_p.CreateFeature(feat)

    lyr_l = ds.CreateLayer("au_hydro_lines", srs=srs, geom_type=ogr.wkbLineString)
    for fname in ("name", "featuretype", "perenniality", "hierarchy"):
        lyr_l.CreateField(ogr.FieldDefn(fname, ogr.OFTString))
    for name, peren, pts in (
        ("Mock Perennial River", "Perennial", [(cx - 1000, cy + 700), (cx - 100, cy + 100), (cx + 800, cy - 600)]),
        ("Mock Dry Creek", "Non Perennial", [(cx + 700, cy + 900), (cx + 200, cy + 350)]),
    ):
        line = ogr.Geometry(ogr.wkbLineString)
        for px, py in pts:
            line.AddPoint(px, py)
        f = ogr.Feature(lyr_l.GetLayerDefn())
        f.SetGeometry(line)
        f.SetField("name", name)
        f.SetField("featuretype", "Watercourse")
        f.SetField("perenniality", peren)
        f.SetField("hierarchy", "Minor")
        lyr_l.CreateFeature(f)

    lyr_poly = ds.CreateLayer("au_hydro_polys", srs=srs, geom_type=ogr.wkbPolygon)
    for fname in ("name", "featuretype", "perenniality"):
        lyr_poly.CreateField(ogr.FieldDefn(fname, ogr.OFTString))
    ring = ogr.Geometry(ogr.wkbLinearRing)
    for px, py in ((cx - 250, cy - 500), (cx - 50, cy - 500), (cx - 50, cy - 350), (cx - 250, cy - 350), (cx - 250, cy - 500)):
        ring.AddPoint(px, py)
    poly = ogr.Geometry(ogr.wkbPolygon)
    poly.AddGeometry(ring)
    f = ogr.Feature(lyr_poly.GetLayerDefn())
    f.SetGeometry(poly)
    f.SetField("name", "Mock Billabong")
    f.SetField("featuretype", "Lake")
    f.SetField("perenniality", "Perennial")
    lyr_poly.CreateFeature(f)
    ds = None
    created[AUOverlayKind.AU_HYDRO.value] = hydro_path

    # 2. au-parks (au_protected_areas)
    parks_path = out_dir / "au-parks.gpkg"
    ds = driver.CreateDataSource(str(parks_path))
    lyr = ds.CreateLayer("au_protected_areas", srs=srs, geom_type=ogr.wkbPolygon)
    for fname in ("name", "park_type", "type_abbr", "iucn", "state"):
        lyr.CreateField(ogr.FieldDefn(fname, ogr.OFTString))
    ring = ogr.Geometry(ogr.wkbLinearRing)
    for px, py in ((cx - 1200, cy - 1200), (cx + 1100, cy - 1200), (cx + 1100, cy + 100), (cx - 1200, cy + 100), (cx - 1200, cy - 1200)):
        ring.AddPoint(px, py)
    poly = ogr.Geometry(ogr.wkbPolygon)
    poly.AddGeometry(ring)
    f = ogr.Feature(lyr.GetLayerDefn())
    f.SetGeometry(poly)
    f.SetField("name", "Blue Mountains")
    f.SetField("park_type", "National Park")
    f.SetField("type_abbr", "NP")
    f.SetField("iucn", "II")
    f.SetField("state", "NSW")
    lyr.CreateFeature(f)
    ds = None
    created[AUOverlayKind.AU_PARKS.value] = parks_path

    # 3. au-infra (au_power_lines, au_substations, au_pipelines)
    infra_path = out_dir / "au-infra.gpkg"
    ds = driver.CreateDataSource(str(infra_path))
    lyr_pl = ds.CreateLayer("au_power_lines", srs=srs, geom_type=ogr.wkbLineString)
    lyr_pl.CreateField(ogr.FieldDefn("name", ogr.OFTString))
    lyr_pl.CreateField(ogr.FieldDefn("capacity_kv", ogr.OFTReal))
    lyr_pl.CreateField(ogr.FieldDefn("status", ogr.OFTString))
    lyr_pl.CreateField(ogr.FieldDefn("state", ogr.OFTString))
    line = ogr.Geometry(ogr.wkbLineString)
    line.AddPoint(cx - 1400, cy + 850)
    line.AddPoint(cx + 1400, cy + 750)
    f = ogr.Feature(lyr_pl.GetLayerDefn())
    f.SetGeometry(line)
    f.SetField("name", "Mock 330kV Feeder")
    f.SetField("capacity_kv", 330.0)
    f.SetField("status", "Operational")
    f.SetField("state", "NSW")
    lyr_pl.CreateFeature(f)

    lyr_sub = ds.CreateLayer("au_substations", srs=srs, geom_type=ogr.wkbPoint)
    lyr_sub.CreateField(ogr.FieldDefn("name", ogr.OFTString))
    lyr_sub.CreateField(ogr.FieldDefn("voltage_kv", ogr.OFTReal))
    lyr_sub.CreateField(ogr.FieldDefn("status", ogr.OFTString))
    lyr_sub.CreateField(ogr.FieldDefn("state", ogr.OFTString))
    pt = ogr.Geometry(ogr.wkbPoint)
    pt.AddPoint(cx + 500, cy + 800)
    f = ogr.Feature(lyr_sub.GetLayerDefn())
    f.SetGeometry(pt)
    f.SetField("name", "Katoomba Sub")
    f.SetField("voltage_kv", 132.0)
    f.SetField("status", "Operational")
    f.SetField("state", "NSW")
    lyr_sub.CreateFeature(f)

    lyr_pipe = ds.CreateLayer("au_pipelines", srs=srs, geom_type=ogr.wkbLineString)
    for fname in ("name", "pipeline_type", "status", "operator"):
        lyr_pipe.CreateField(ogr.FieldDefn(fname, ogr.OFTString))
    pline = ogr.Geometry(ogr.wkbLineString)
    pline.AddPoint(cx - 1200, cy - 750)
    pline.AddPoint(cx + 1200, cy - 650)
    f = ogr.Feature(lyr_pipe.GetLayerDefn())
    f.SetGeometry(pline)
    f.SetField("name", "Mock Gas Trunk")
    f.SetField("pipeline_type", "Gas")
    f.SetField("status", "Operational")
    f.SetField("operator", "APA")
    lyr_pipe.CreateFeature(f)
    ds = None
    created[AUOverlayKind.AU_INFRA.value] = infra_path

    # 4. au-aviation (au_airfields, au_runways, au_homesteads)
    av_path = out_dir / "au-aviation.gpkg"
    ds = driver.CreateDataSource(str(av_path))
    lyr_af = ds.CreateLayer("au_airfields", srs=srs, geom_type=ogr.wkbPoint)
    for fname in ("name", "featuretype", "type"):
        lyr_af.CreateField(ogr.FieldDefn(fname, ogr.OFTString))
    pt = ogr.Geometry(ogr.wkbPoint)
    pt.AddPoint(cx + 900, cy - 200)
    f = ogr.Feature(lyr_af.GetLayerDefn())
    f.SetGeometry(pt)
    f.SetField("name", "Katoomba ALA")
    f.SetField("featuretype", "Aircraft Facility Point")
    f.SetField("type", "Landing Ground")
    lyr_af.CreateFeature(f)

    lyr_rw = ds.CreateLayer("au_runways", srs=srs, geom_type=ogr.wkbLineString)
    for fname in ("featuretype", "type"):
        lyr_rw.CreateField(ogr.FieldDefn(fname, ogr.OFTString))
    rw = ogr.Geometry(ogr.wkbLineString)
    rw.AddPoint(cx + 750, cy - 240)
    rw.AddPoint(cx + 1050, cy - 160)
    f = ogr.Feature(lyr_rw.GetLayerDefn())
    f.SetGeometry(rw)
    f.SetField("featuretype", "Runway")
    f.SetField("type", "Unsealed")
    lyr_rw.CreateFeature(f)

    lyr_hs = ds.CreateLayer("au_homesteads", srs=srs, geom_type=ogr.wkbPoint)
    for fname in ("name", "featuretype"):
        lyr_hs.CreateField(ogr.FieldDefn(fname, ogr.OFTString))
    hpt = ogr.Geometry(ogr.wkbPoint)
    hpt.AddPoint(cx - 800, cy - 250)
    f = ogr.Feature(lyr_hs.GetLayerDefn())
    f.SetGeometry(hpt)
    f.SetField("name", "Megalong Station")
    f.SetField("featuretype", "Homestead")
    lyr_hs.CreateFeature(f)
    ds = None
    created[AUOverlayKind.AU_AVIATION.value] = av_path

    return created


def _build_local_synthetic_pack(pack_dir: Path) -> Path:
    """Assemble a self-contained air-gapped Australia pack from local synthetic fixtures."""
    from gridforge.packs.builder import build_offline_pack

    dem_src = FIXTURES_DIR / "synthetic_dem.tif"
    osm_src = FIXTURES_DIR / "synthetic_osm.osm"
    assert dem_src.is_file() and osm_src.is_file()

    # Use pack builder's streaming OSM extract path from local synthetic OSM XML
    manifest = build_offline_pack(
        "150.24,-33.79,150.39,-33.67",
        pack_dir,
        name="Synthetic Blue Mountains Offline Pack",
        include_osm=True,
        include_dem=False,
        include_overlays=False,
        source_pbf=osm_src,
    )

    # Copy synthetic DEM and generate all 4 mock AU national overlay GeoPackages inside pack
    shutil.copy2(dem_src, pack_dir / "dem.tif")
    spec = SheetSpec(
        center_lat=BLUE_MOUNTAINS_LAT,
        center_lon=BLUE_MOUNTAINS_LON,
        scale=25000,
        paper=PaperSize.A4,
    )
    layout = compute_sheet_layout(spec)
    overlay_paths = _create_mock_au_overlay_gpkgs(pack_dir / "overlays", layout)

    updated = manifest.model_copy(
        update={
            "dem_path": "dem.tif",
            "vertical_datum": "AHD",
            "dem_attribution": "Elevation: © Commonwealth of Australia (Geoscience Australia) CC BY 4.0",
            "overlays": {k: f"overlays/{p.name}" for k, p in overlay_paths.items()},
            "attributions": [
                "Map data © OpenStreetMap contributors (ODbL) — openstreetmap.org/copyright",
                "Elevation: © Commonwealth of Australia (Geoscience Australia) CC BY 4.0",
                "Hydrology/Parks/Infra/Aviation: © Commonwealth of Australia (GA/CAPAD) CC BY 4.0",
            ],
            "created_at": datetime.now(timezone.utc).isoformat(),
            "size_bytes": sum(f.stat().st_size for f in pack_dir.rglob("*") if f.is_file()),
        }
    )
    (pack_dir / "pack.json").write_text(updated.model_dump_json(indent=2), encoding="utf-8")
    return pack_dir


class TestOfflineAustraliaPackAndWebAPI:
    """Fast zero-network CI verification for Australia offline packs, AU overlays, and FastAPI backend."""

    def test_offline_pack_cli_and_airgapped_render(self, tmp_path: Path) -> None:
        pack_dir = _build_local_synthetic_pack(tmp_path / "au_offline_pack")

        # 1. `gridforge pack list-regions`
        list_proc = subprocess.run(
            [sys.executable, "-m", "gridforge.cli", "pack", "list-regions"],
            capture_output=True,
            text=True,
            check=False,
        )
        assert list_proc.returncode == 0, f"pack list-regions failed:\n{list_proc.stderr}\n{list_proc.stdout}"
        for expected_id in ("nsw", "tas", "nt", "wa", "blue-mountains", "red-centre"):
            assert expected_id in list_proc.stdout.lower()

        # 2. `gridforge pack inspect <pack_dir>`
        inspect_proc = subprocess.run(
            [sys.executable, "-m", "gridforge.cli", "pack", "inspect", str(pack_dir)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert inspect_proc.returncode == 0, f"pack inspect failed:\n{inspect_proc.stderr}\n{inspect_proc.stdout}"
        assert "AHD" in inspect_proc.stdout

        # 3. 100% air-gapped `gridforge render --pack <pack_dir>` with dead Overpass URL
        out_color = tmp_path / "airgapped_au_pack_color.pdf"
        env = os.environ.copy()
        env["OVERPASS_URL"] = "http://127.0.0.1:9/dead"
        render_proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "gridforge.cli",
                "render",
                "--center",
                f"{BLUE_MOUNTAINS_LAT},{BLUE_MOUNTAINS_LON}",
                "--scale",
                "25000",
                "--paper",
                "a4",
                "--mode",
                "color",
                "--title",
                "AIRGAPPED AU PACK COLOR",
                "--pack",
                str(pack_dir),
                "--au-overlay",
                "au-hydro",
                "--au-overlay",
                "au-parks",
                "--au-overlay",
                "au-infra",
                "--au-overlay",
                "au-aviation",
                "--output",
                str(out_color),
            ],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert render_proc.returncode == 0, f"air-gapped --pack render failed:\n{render_proc.stderr}\n{render_proc.stdout}"
        assert_pdf_valid(
            out_color,
            scale=25000,
            paper="a4",
            orientation="portrait",
            expected_epsg=BLUE_MOUNTAINS_EPSG,
            expected_gzd=BLUE_MOUNTAINS_GZD,
            expected_title="AIRGAPPED AU PACK COLOR",
            extra_required_text=["AHD"],
            require_osm_attribution=True,
            require_copernicus_attribution=False,
        )

        # 4. B&W air-gapped render via Python API with pack_dir
        out_bw = tmp_path / "airgapped_au_pack_bw.pdf"
        bw_spec = SheetSpec(
            center_lat=BLUE_MOUNTAINS_LAT,
            center_lon=BLUE_MOUNTAINS_LON,
            scale=25000,
            paper=PaperSize.A4,
            color_mode=ColorMode.BW,
            title="AIRGAPPED AU PACK BW",
            au_overlays=[
                AUOverlayKind.AU_HYDRO,
                AUOverlayKind.AU_PARKS,
                AUOverlayKind.AU_INFRA,
                AUOverlayKind.AU_AVIATION,
            ],
        )
        run_render_job(bw_spec, out_bw, pack_dir=pack_dir)
        assert_pdf_valid(
            out_bw,
            scale=25000,
            paper="a4",
            orientation="portrait",
            expected_epsg=BLUE_MOUNTAINS_EPSG,
            expected_gzd=BLUE_MOUNTAINS_GZD,
            expected_title="AIRGAPPED AU PACK BW",
            extra_required_text=["AHD"],
            require_osm_attribution=True,
            require_copernicus_attribution=False,
        )

    def test_offline_web_api_footprints_and_packs(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        fastapi_testclient = pytest.importorskip("fastapi.testclient")
        from gridforge.web.app import create_app

        packs_root = tmp_path / "packs"
        _build_local_synthetic_pack(packs_root / "blue_mountains_synth")
        data_dir = tmp_path / "web_data"
        data_dir.mkdir(parents=True, exist_ok=True)

        monkeypatch.setenv("GRIDFORGE_DATA_DIR", str(data_dir))
        monkeypatch.setenv("GRIDFORGE_PACKS_DIR", str(packs_root))

        app = create_app()
        with fastapi_testclient.TestClient(app) as client:
            health = client.get("/api/health")
            assert health.status_code == 200
            hdata = health.json()
            assert hdata["status"] == "ok"
            assert hdata["packs_count"] >= 1

            packs = client.get("/api/packs")
            assert packs.status_code == 200
            assert len(packs.json()["packs"]) >= 1

            # Verify /api/footprint across all 4 Australian states/territories
            for key, site in AU_SITES.items():
                resp = client.post(
                    "/api/footprint",
                    json={
                        "center": f"{site['lat']},{site['lon']}",
                        "scale": 25000,
                        "paper": "a4",
                        "orientation": "portrait",
                    },
                )
                assert resp.status_code == 200, f"footprint failed for {key}: {resp.text}"
                body = resp.json()
                assert body["utm_epsg"] == site["epsg"]
                assert body["gzd"] == site["gzd"]
                assert body["footprint_geojson"]["type"] == "FeatureCollection"
                assert len(body["footprint_geojson"]["features"]) == 1
                assert body["footprint_geojson"]["features"][0]["geometry"]["type"] == "Polygon"
                assert body["grid_geojson"]["type"] == "FeatureCollection"
                assert len(body["grid_geojson"]["features"]) >= 8
                assert "gm_angle_deg" in body["declination"]


@pytest.mark.live
@pytest.mark.skipif(
    not RUN_LIVE,
    reason="Live Australia multi-region tests skipped by default; set GRIDFORGE_RUN_LIVE=1 to run.",
)
class TestLiveAustraliaMultiRegion:
    """Live Australia-wide verification across Sydney 5m LiDAR, Uluru Outback, Cradle Mtn TAS, Perth WA."""

    def _out_dir(self, tmp_path: Path) -> Path:
        override = os.environ.get("GRIDFORGE_LIVE_OUT_DIR")
        if override:
            path = Path(override)
            path.mkdir(parents=True, exist_ok=True)
            return path
        return tmp_path

    def test_live_sydney_harbour_5m_lidar_ahd(self, tmp_path: Path) -> None:
        site = AU_SITES["sydney"]
        out_pdf = self._out_dir(tmp_path) / "au_sydney_25k_a4_color.pdf"
        spec = SheetSpec(
            center_lat=site["lat"],
            center_lon=site["lon"],
            scale=25000,
            paper=PaperSize.A4,
            orientation=Orientation.PORTRAIT,
            color_mode=ColorMode.COLOR,
            units=Units.METRIC,
            title=site["title"],
            dem_source=DemSourceKind.AU_ELVIS,
        )
        res = run_render_job(spec, out_pdf)
        assert res.utm_epsg == site["epsg"]

        # Linux ru_maxrss is in KB; confirm dense urban Sydney stays well under 2 GB
        self_rss_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0
        child_rss_mb = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss / 1024.0
        peak_rss_mb = max(self_rss_mb, child_rss_mb)
        assert peak_rss_mb < 2048.0, (
            f"Peak RSS {peak_rss_mb:.1f} MB (self={self_rss_mb:.1f}, child={child_rss_mb:.1f}) "
            f"exceeded 2048 MB guardrail!"
        )

        assert_pdf_valid(
            out_pdf,
            scale=25000,
            paper="a4",
            orientation="portrait",
            expected_epsg=site["epsg"],
            expected_gzd=site["gzd"],
            expected_title="SYDNEY HARBOUR",
            extra_required_text=["AHD"],
            require_osm_attribution=True,
            require_copernicus_attribution=False,
        )

    def test_live_uluru_outback_nt_overlays(self, tmp_path: Path) -> None:
        site = AU_SITES["uluru"]
        out_pdf = self._out_dir(tmp_path) / "au_uluru_25k_a4_color.pdf"
        spec = SheetSpec(
            center_lat=site["lat"],
            center_lon=site["lon"],
            scale=25000,
            paper=PaperSize.A4,
            orientation=Orientation.PORTRAIT,
            color_mode=ColorMode.COLOR,
            units=Units.METRIC,
            title=site["title"],
            dem_source=DemSourceKind.AU_ELVIS,
            au_overlays=[
                AUOverlayKind.AU_HYDRO,
                AUOverlayKind.AU_PARKS,
                AUOverlayKind.AU_AVIATION,
            ],
        )
        res = run_render_job(spec, out_pdf)
        assert res.utm_epsg == site["epsg"]

        assert_pdf_valid(
            out_pdf,
            scale=25000,
            paper="a4",
            orientation="portrait",
            expected_epsg=site["epsg"],
            expected_gzd=site["gzd"],
            expected_title="ULURU",
            extra_required_text=["AHD"],
            require_osm_attribution=True,
            require_copernicus_attribution=False,
        )

    def test_live_cradle_mountain_tas_bw_and_exports(self, tmp_path: Path) -> None:
        site = AU_SITES["cradle_mountain"]
        out_dir = self._out_dir(tmp_path)
        out_pdf = out_dir / "au_cradle_mountain_25k_a4_bw.pdf"
        mbt_path = out_dir / "au_cradle_mountain_25k_bw.mbtiles"
        kmz_path = out_dir / "au_cradle_mountain_25k_bw.kmz"

        spec = SheetSpec(
            center_lat=site["lat"],
            center_lon=site["lon"],
            scale=25000,
            paper=PaperSize.A4,
            orientation=Orientation.PORTRAIT,
            color_mode=ColorMode.BW,
            units=Units.METRIC,
            title=site["title"],
            dem_source=DemSourceKind.AU_ELVIS,
            au_overlays=[
                AUOverlayKind.AU_HYDRO,
                AUOverlayKind.AU_PARKS,
            ],
        )
        layout = compute_sheet_layout(spec)
        expected_wgs84 = layout.wgs84_bbox.as_tuple()

        res = run_render_job(spec, out_pdf, mbtiles_out=mbt_path, kmz_out=kmz_path)
        assert res.utm_epsg == site["epsg"]

        assert_pdf_valid(
            out_pdf,
            scale=25000,
            paper="a4",
            orientation="portrait",
            expected_epsg=site["epsg"],
            expected_gzd=site["gzd"],
            expected_title="CRADLE MOUNTAIN",
            extra_required_text=["AHD"],
            require_osm_attribution=True,
            require_copernicus_attribution=False,
        )
        assert_mbtiles_valid(mbt_path, expected_format="png", expected_bounds=expected_wgs84)
        assert_garmin_kmz_valid(kmz_path, max_tiles=100, expected_bounds=expected_wgs84)

    def test_live_perth_kings_park_wa_5m_lidar(self, tmp_path: Path) -> None:
        site = AU_SITES["perth"]
        out_pdf = self._out_dir(tmp_path) / "au_perth_25k_a4_color.pdf"
        spec = SheetSpec(
            center_lat=site["lat"],
            center_lon=site["lon"],
            scale=25000,
            paper=PaperSize.A4,
            orientation=Orientation.PORTRAIT,
            color_mode=ColorMode.COLOR,
            units=Units.METRIC,
            title=site["title"],
            dem_source=DemSourceKind.AU_ELVIS,
        )
        res = run_render_job(spec, out_pdf)
        assert res.utm_epsg == site["epsg"]

        assert_pdf_valid(
            out_pdf,
            scale=25000,
            paper="a4",
            orientation="portrait",
            expected_epsg=site["epsg"],
            expected_gzd=site["gzd"],
            expected_title="PERTH",
            extra_required_text=["AHD"],
            require_osm_attribution=True,
            require_copernicus_attribution=False,
        )


@pytest.mark.live
@pytest.mark.skipif(
    not WEB_URL,
    reason="Live web compose stack test skipped; set GRIDFORGE_WEB_URL=http://127.0.0.1:8080 to run.",
)
class TestLiveWebComposeStack:
    """End-to-end verification against live `docker compose up` GridForge web + worker services."""

    def test_web_ui_health_footprints_and_async_job(self, tmp_path: Path) -> None:
        with httpx.Client(base_url=WEB_URL, timeout=60.0) as client:
            # 1. Root SPA HTML & built Vite/MapLibre assets
            root_resp = client.get("/")
            assert root_resp.status_code == 200
            assert "text/html" in root_resp.headers.get("content-type", "")
            html = root_resp.text
            assert "GridForge" in html

            asset_urls = re.findall(r'(?:src|href)="(/assets/[^"]+)"', html)
            assert len(asset_urls) >= 1, f"Expected built Vite assets in index.html, got:\n{html[:500]}"
            for asset_url in asset_urls:
                a_resp = client.get(asset_url)
                assert a_resp.status_code == 200, f"Static asset {asset_url} returned {a_resp.status_code}"
                assert len(a_resp.content) > 100

            # 2. Health & worker heartbeat
            health_resp = client.get("/api/health")
            assert health_resp.status_code == 200
            health = health_resp.json()
            assert health["status"] == "ok"
            assert health["worker_alive"] is True, f"Worker not alive: {health}"

            # 3. Footprints across Sydney, Uluru, Cradle Mountain, Perth
            for key, site in AU_SITES.items():
                fp_resp = client.post(
                    "/api/footprint",
                    json={
                        "center": f"{site['lat']},{site['lon']}",
                        "scale": 25000,
                        "paper": "a4",
                        "orientation": "portrait",
                    },
                )
                assert fp_resp.status_code == 200, f"/api/footprint failed for {key}: {fp_resp.text}"
                fp = fp_resp.json()
                assert fp["utm_epsg"] == site["epsg"]
                assert fp["gzd"] == site["gzd"]
                poly_coords = fp["footprint_geojson"]["features"][0]["geometry"]["coordinates"][0]
                assert len(poly_coords) >= 5
                assert len(fp["grid_geojson"]["features"]) >= 8
                assert "magnetic_declination_deg" in fp["declination"]

            # 4. Submit async render job (PDF + Garmin KMZ) via multipart/form-data
            job_payload = {
                "center": f"{BLUE_MOUNTAINS_LAT},{BLUE_MOUNTAINS_LON}",
                "scale": 25000,
                "paper": "a4",
                "orientation": "portrait",
                "color_mode": "color",
                "units": "metric",
                "title": "WEB APP E2E — KATOOMBA",
                "dem_source": "au-elvis",
                "us_overlays": [],
                "au_overlays": [],
                "carto": CartoOptions().model_dump(),
                "outputs": {
                    "pdf": True,
                    "geopdf": False,
                    "mbtiles": False,
                    "mbtiles_format": "png",
                    "kmz": True,
                    "garmin_profile": "standard",
                    "include_grid_on_digital": True,
                },
            }
            submit_resp = client.post(
                "/api/jobs",
                data={"payload": json.dumps(job_payload)},
            )
            assert submit_resp.status_code in (200, 201, 202), f"Submit failed: {submit_resp.text}"
            job = submit_resp.json()
            job_id = job["job_id"]

            # Poll until terminal state (max 5 minutes)
            deadline = time.monotonic() + 300.0
            final_job = job
            while time.monotonic() < deadline:
                poll_resp = client.get(f"/api/jobs/{job_id}")
                assert poll_resp.status_code == 200
                final_job = poll_resp.json()
                if final_job["status"] in ("succeeded", "failed", "cancelled"):
                    break
                time.sleep(2.0)

            assert final_job["status"] == "succeeded", f"Web worker job did not succeed: {final_job}"

            peak_rss_mb = final_job.get("peak_rss_mb")
            if peak_rss_mb is not None:
                assert float(peak_rss_mb) < 2048.0, (
                    f"Web worker peak_rss_mb={peak_rss_mb} exceeded 2048 MB guardrail!"
                )

            artifacts = final_job.get("artifacts", [])
            by_kind = {a["kind"]: a for a in artifacts}
            assert "pdf" in by_kind, f"Missing PDF artifact in job: {final_job}"
            assert "kmz" in by_kind, f"Missing KMZ artifact in job: {final_job}"

            # 5. Download & verify PDF and KMZ artifacts
            pdf_dl = client.get(by_kind["pdf"]["download_url"])
            assert pdf_dl.status_code == 200
            dl_pdf = tmp_path / by_kind["pdf"]["filename"]
            dl_pdf.write_bytes(pdf_dl.content)
            assert_pdf_valid(
                dl_pdf,
                scale=25000,
                paper="a4",
                orientation="portrait",
                expected_epsg=BLUE_MOUNTAINS_EPSG,
                expected_gzd=BLUE_MOUNTAINS_GZD,
                expected_title="WEB APP E2E",
                extra_required_text=["AHD"],
                require_osm_attribution=True,
                require_copernicus_attribution=False,
            )

            kmz_dl = client.get(by_kind["kmz"]["download_url"])
            assert kmz_dl.status_code == 200
            dl_kmz = tmp_path / by_kind["kmz"]["filename"]
            dl_kmz.write_bytes(kmz_dl.content)
            assert_garmin_kmz_valid(dl_kmz, max_tiles=100)
