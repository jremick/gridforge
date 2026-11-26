"""GridForge v1.2.0 Pan-Australia State Fire-Trail, Hut & Trig Overlay E2E Integration Tests.

Covers:
  1. Offline CI tests (default, zero network):
     - `AutoStateTrailsSource` cross-border merging across 2 mocked adjacent states (NSW + ACT)
       plus an excluded non-contributing state (TAS): verifies merged `au_state_trails` +
       `au_state_pois` GeoPackage, dynamic `resolved_attributions` containing ONLY contributing
       states, and end-to-end offline Color & B&W PDF rendering with `au-state-trails` via both
       Python API and air-gapped CLI (`gridforge render --au-overlay au-state-trails --pack ...`).
  2. Live Pan-Australia E2E Suite (`@pytest.mark.live` + `GRIDFORGE_RUN_LIVE=1`):
     - Tasmania — Cradle Mountain (`-41.684, 145.951`, `55G`, `EPSG:32755`, `au-tas-trails`, slope shading)
     - Queensland — Lamington / Binna Burra (`-28.200, 153.190`, `56J`, `EPSG:32756`, `au-qld-trails`, slope shading)
     - ACT — Namadgi / Orroral Valley (`-35.640, 148.980`, `55H`, `EPSG:32755`, `au-act-trails`)
     - Western Australia — Stirling Range / Bluff Knoll (`-34.375, 118.255`, `50H`, `EPSG:32750`, `au-wa-trails`, slope shading)
     - South Australia — Flinders Ranges / Wilpena Pound (`-31.530, 138.600`, `54J`, `EPSG:32754`, `au-sa-trails`, `/vsizip/` RAM check)
     - Northern Territory — West MacDonnell / Larapinta (`-23.730, 133.240`, `53K`, `EPSG:32753`, `au-nt-trails` + `au-hydro`)
     - Auto Cross-Border Check (`--au-overlay au-state-trails` via CLI on Cradle Mountain TAS)
"""

from __future__ import annotations

import os
import resource
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from gridforge.config import (
    AUOverlayKind,
    CartoOptions,
    ColorMode,
    DemSourceKind,
    Orientation,
    PaperSize,
    SheetLayout,
    SheetSpec,
    Units,
)
from gridforge.geo.crs import compute_sheet_layout
from gridforge.pipeline.runner import run_render_job
from gridforge.sources.dem_local import LocalDEMSource
from gridforge.sources.osm_pbf import LocalPBFSource

_INTEGRATION_DIR = Path(__file__).resolve().parent
if str(_INTEGRATION_DIR) not in sys.path:
    sys.path.insert(0, str(_INTEGRATION_DIR))

from verify_pdf import assert_pdf_valid  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures"

BLUE_MOUNTAINS_LAT = -33.732
BLUE_MOUNTAINS_LON = 150.312
BLUE_MOUNTAINS_EPSG = 32756
BLUE_MOUNTAINS_GZD = "56H"

RUN_LIVE = os.environ.get("GRIDFORGE_RUN_LIVE", "").strip().lower() in {"1", "true", "yes"}

PAN_AU_SITES: dict[str, dict[str, Any]] = {
    "tas": {
        "lat": -41.684,
        "lon": 145.951,
        "epsg": 32755,
        "gzd": "55G",
        "title": "CRADLE MOUNTAIN — TASMANIA LIST",
        "overlay": AUOverlayKind.AU_TAS_TRAILS,
        "slope": True,
        "extra_overlays": [],
        "required_text": ["AHD", "Tasmania"],
        "filename": "v12_tas_cradle_mountain_25k_a4_color.pdf",
    },
    "qld": {
        "lat": -28.200,
        "lon": 153.190,
        "epsg": 32756,
        "gzd": "56J",
        "title": "LAMINGTON — BINNA BURRA QLD",
        "overlay": AUOverlayKind.AU_QLD_TRAILS,
        "slope": True,
        "extra_overlays": [],
        "required_text": ["AHD", "Queensland"],
        "filename": "v12_qld_lamington_25k_a4_color.pdf",
    },
    "act": {
        "lat": -35.640,
        "lon": 148.980,
        "epsg": 32755,
        "gzd": "55H",
        "title": "NAMADGI — ORRORAL VALLEY ACT",
        "overlay": AUOverlayKind.AU_ACT_TRAILS,
        "slope": False,
        "extra_overlays": [],
        "required_text": ["AHD", "Australian Capital Territory"],
        "filename": "v12_act_namadgi_25k_a4_color.pdf",
    },
    "wa": {
        "lat": -34.375,
        "lon": 118.255,
        "epsg": 32750,
        "gzd": "50H",
        "title": "STIRLING RANGE — BLUFF KNOLL WA",
        "overlay": AUOverlayKind.AU_WA_TRAILS,
        "slope": True,
        "extra_overlays": [],
        "required_text": ["AHD", "Western Australia"],
        "filename": "v12_wa_stirling_range_25k_a4_color.pdf",
    },
    "sa": {
        "lat": -31.530,
        "lon": 138.600,
        "epsg": 32754,
        "gzd": "54J",
        "title": "FLINDERS RANGES — WILPENA SA",
        "overlay": AUOverlayKind.AU_SA_TRAILS,
        "slope": False,
        "extra_overlays": [],
        "required_text": ["AHD", "South Australia"],
        "filename": "v12_sa_flinders_ranges_25k_a4_color.pdf",
    },
    "nt": {
        "lat": -23.715,
        "lon": 133.360,
        "epsg": 32753,
        "gzd": "53K",
        "title": "WEST MACDONNELL — LARAPINTA NT",
        "overlay": AUOverlayKind.AU_NT_TRAILS,
        "slope": False,
        "extra_overlays": [AUOverlayKind.AU_HYDRO],
        "required_text": ["AHD", "Northern Territory"],
        "filename": "v12_nt_west_macdonnell_25k_a4_color.pdf",
    },
}


def _assert_peak_rss_under_2gb(label: str) -> float:
    """Assert Linux process & child peak RSS stays strictly under 2048 MB."""
    div = (1024.0 * 1024.0) if sys.platform == "darwin" else 1024.0
    self_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / div
    child_mb = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss / div
    peak_mb = max(self_mb, child_mb)
    assert peak_mb < 2048.0, (
        f"[{label}] Peak RSS {peak_mb:.1f} MB (self={self_mb:.1f}, child={child_mb:.1f}) "
        f"exceeded 2048 MB guardrail!"
    )
    return peak_mb


def _create_mock_state_gpkg(out_path: Path, layout: SheetLayout, state: str, shift_m: float = 0.0) -> Path:
    """Create a valid UTM GeoPackage matching the unified 2-layer AU state trails schema."""
    from osgeo import ogr, osr

    ogr.UseExceptions()
    osr.UseExceptions()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.exists():
        out_path.unlink()

    srs = osr.SpatialReference()
    srs.ImportFromEPSG(layout.utm_epsg)
    srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    driver = ogr.GetDriverByName("GPKG")

    cx = layout.center_easting
    cy = layout.center_northing
    ds = driver.CreateDataSource(str(out_path))

    lyr_t = ds.CreateLayer("au_state_trails", srs=srs, geom_type=ogr.wkbMultiLineString)
    for fname in ("name", "trail_class", "surface", "state"):
        lyr_t.CreateField(ogr.FieldDefn(fname, ogr.OFTString))

    for name, tclass, surface, pts in (
        (
            f"{state} Border Fire Trail",
            "fire_trail",
            "unsealed",
            [(cx - 1100, cy + 400 + shift_m), (cx + 900, cy + 250 + shift_m)],
        ),
        (
            f"{state} Ridge Walk",
            "walking_track",
            "natural",
            [(cx - 500 + shift_m, cy - 800), (cx + 300 + shift_m, cy + 750)],
        ),
    ):
        line = ogr.Geometry(ogr.wkbLineString)
        for px, py in pts:
            line.AddPoint_2D(px, py)
        mline = ogr.Geometry(ogr.wkbMultiLineString)
        mline.AddGeometry(line)
        feat = ogr.Feature(lyr_t.GetLayerDefn())
        feat.SetGeometry(mline)
        feat.SetField("name", name)
        feat.SetField("trail_class", tclass)
        feat.SetField("surface", surface)
        feat.SetField("state", state)
        lyr_t.CreateFeature(feat)

    lyr_p = ds.CreateLayer("au_state_pois", srs=srs, geom_type=ogr.wkbPoint)
    lyr_p.CreateField(ogr.FieldDefn("name", ogr.OFTString))
    lyr_p.CreateField(ogr.FieldDefn("poi_type", ogr.OFTString))
    lyr_p.CreateField(ogr.FieldDefn("elev_m", ogr.OFTReal))
    lyr_p.CreateField(ogr.FieldDefn("state", ogr.OFTString))

    for name, ptype, elev_m, px, py in (
        (f"{state} Alpine Hut", "hut", 1240.0, cx - 350 + shift_m, cy + 210),
        (f"{state} Primary Trig", "trig", 1425.5, cx + 280 + shift_m, cy - 310),
    ):
        pt = ogr.Geometry(ogr.wkbPoint)
        pt.AddPoint_2D(px, py)
        feat = ogr.Feature(lyr_p.GetLayerDefn())
        feat.SetGeometry(pt)
        feat.SetField("name", name)
        feat.SetField("poi_type", ptype)
        feat.SetField("elev_m", elev_m)
        feat.SetField("state", state)
        lyr_p.CreateFeature(feat)

    ds = None
    return out_path


def _count_gpkg_features(gpkg_path: Path) -> tuple[dict[str, int], set[str]]:
    """Return ``({layer_name: count}, {state codes})`` for a state trails GeoPackage."""
    from osgeo import ogr

    ogr.UseExceptions()
    ds = ogr.Open(str(gpkg_path))
    assert ds is not None, f"Failed to open GeoPackage: {gpkg_path}"
    counts: dict[str, int] = {}
    states: set[str] = set()
    for idx in range(ds.GetLayerCount()):
        lyr = ds.GetLayerByIndex(idx)
        name = lyr.GetName()
        counts[name] = lyr.GetFeatureCount()
        for feat in lyr:
            st = feat.GetField("state")
            if st:
                states.add(str(st))
    ds = None
    return counts, states


class TestOfflineV12PanAustraliaTrails:
    """Fast zero-network CI verification for AutoStateTrailsSource cross-border merge & rendering."""

    def test_offline_auto_state_trails_cross_border_merge_and_render(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from gridforge.packs.builder import build_offline_pack
        from gridforge.sources import overlays_au_states
        from gridforge.sources.overlays_au_states import AutoStateTrailsSource

        dem_path = FIXTURES_DIR / "synthetic_dem.tif"
        osm_path = FIXTURES_DIR / "synthetic_osm.osm"
        assert dem_path.is_file() and osm_path.is_file()

        spec = SheetSpec(
            center_lat=BLUE_MOUNTAINS_LAT,
            center_lon=BLUE_MOUNTAINS_LON,
            scale=25000,
            paper=PaperSize.A4,
            orientation=Orientation.PORTRAIT,
            color_mode=ColorMode.COLOR,
            units=Units.METRIC,
            title="V12 CROSS-BORDER AUTO TRAILS",
            date_iso="2026-06-15",
            au_overlays=[AUOverlayKind.AU_STATE_TRAILS],
        )
        layout = compute_sheet_layout(spec)

        nsw_gpkg = _create_mock_state_gpkg(tmp_path / "src_nsw.gpkg", layout, "NSW", shift_m=0.0)
        act_gpkg = _create_mock_state_gpkg(tmp_path / "src_act.gpkg", layout, "ACT", shift_m=240.0)

        class _FakeStateSource:
            def __init__(self, name: str, attribution: str, gpkg: Path | None) -> None:
                self.name = name
                self.attribution = attribution
                self._gpkg = gpkg

            def fetch_overlay(self, _layout: SheetLayout, work_dir: Path) -> Path | None:
                if self._gpkg is None:
                    return None
                work_dir.mkdir(parents=True, exist_ok=True)
                dst = work_dir / f"{self.name}.gpkg"
                shutil.copy2(self._gpkg, dst)
                return dst

        nsw_attr = "NSW Trails, Facilities & Trig Stations: © State of New South Wales (CC BY 4.0)"
        act_attr = "ACT Trails, Huts & Survey Control: © Australian Capital Territory (CC BY 4.0)"
        tas_attr = "Tasmania Trails, Huts & Survey Control: © State of Tasmania (CC BY 3.0 AU)"

        fake_sources = {
            "NSW": _FakeStateSource("au-nsw-trails", nsw_attr, nsw_gpkg),
            "ACT": _FakeStateSource("au-act-trails", act_attr, act_gpkg),
            "TAS": _FakeStateSource("au-tas-trails", tas_attr, None),
        }
        monkeypatch.setattr(
            overlays_au_states,
            "resolve_intersecting_states",
            lambda _bbox: ["NSW", "ACT", "TAS"],
        )

        # 1. Exercise AutoStateTrailsSource cross-border merging & dynamic attribution filtering
        auto_src = AutoStateTrailsSource(state_sources=fake_sources)
        merge_dir = tmp_path / "merge_work"
        merge_dir.mkdir(parents=True, exist_ok=True)
        merged_gpkg = auto_src.fetch_overlay(layout, merge_dir)
        assert merged_gpkg is not None and merged_gpkg.is_file()

        counts, states = _count_gpkg_features(merged_gpkg)
        assert counts.get("au_state_trails", 0) >= 4
        assert counts.get("au_state_pois", 0) >= 4
        assert states == {"NSW", "ACT"}

        resolved_attrs = getattr(auto_src, "resolved_attributions", [])
        assert nsw_attr in resolved_attrs
        assert act_attr in resolved_attrs
        assert tas_attr not in resolved_attrs
        assert "New South Wales" in auto_src.attribution
        assert "Australian Capital Territory" in auto_src.attribution
        assert "Tasmania" not in auto_src.attribution

        # 2. End-to-end offline Color & B&W render with au-state-trails via Python API
        out_color = tmp_path / "v12_offline_auto_color.pdf"
        run_render_job(
            spec,
            out_color,
            elevation_source=LocalDEMSource(dem_path),
            vector_source=LocalPBFSource(osm_path),
            regional_overlay_overrides={AUOverlayKind.AU_STATE_TRAILS: merged_gpkg},
        )
        assert_pdf_valid(
            out_color,
            scale=25000,
            paper="a4",
            orientation="portrait",
            expected_epsg=BLUE_MOUNTAINS_EPSG,
            expected_gzd=BLUE_MOUNTAINS_GZD,
            expected_title="V12 CROSS-BORDER AUTO TRAILS",
            require_osm_attribution=True,
            require_copernicus_attribution=False,
        )

        out_bw = tmp_path / "v12_offline_auto_bw.pdf"
        bw_spec = spec.model_copy(
            update={"color_mode": ColorMode.BW, "title": "V12 CROSS-BORDER AUTO BW"}
        )
        run_render_job(
            bw_spec,
            out_bw,
            elevation_source=LocalDEMSource(dem_path),
            vector_source=LocalPBFSource(osm_path),
            regional_overlay_overrides={AUOverlayKind.AU_STATE_TRAILS: merged_gpkg},
        )
        assert_pdf_valid(
            out_bw,
            scale=25000,
            paper="a4",
            orientation="portrait",
            expected_epsg=BLUE_MOUNTAINS_EPSG,
            expected_gzd=BLUE_MOUNTAINS_GZD,
            expected_title="V12 CROSS-BORDER AUTO BW",
            require_osm_attribution=True,
            require_copernicus_attribution=False,
        )
        assert out_color.read_bytes() != out_bw.read_bytes()

        # 3. End-to-end air-gapped CLI `gridforge render --au-overlay au-state-trails --pack ...`
        pack_dir = tmp_path / "au_state_pack"
        manifest = build_offline_pack(
            "150.24,-33.79,150.39,-33.67",
            pack_dir,
            name="Synthetic AU State Trails Pack",
            include_osm=True,
            include_dem=False,
            include_overlays=False,
            source_pbf=osm_path,
        )
        shutil.copy2(dem_path, pack_dir / "dem.tif")
        (pack_dir / "overlays").mkdir(parents=True, exist_ok=True)
        shutil.copy2(merged_gpkg, pack_dir / "overlays" / "au-state-trails.gpkg")
        updated = manifest.model_copy(
            update={
                "dem_path": "dem.tif",
                "vertical_datum": "AHD",
                "dem_attribution": "Elevation: © Commonwealth of Australia (Geoscience Australia) CC BY 4.0",
                "overlays": {"au-state-trails": "overlays/au-state-trails.gpkg"},
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        (pack_dir / "pack.json").write_text(updated.model_dump_json(indent=2), encoding="utf-8")

        cli_pdf = tmp_path / "v12_offline_cli_auto.pdf"
        env = os.environ.copy()
        env["OVERPASS_URL"] = "http://127.0.0.1:9/dead"
        proc = subprocess.run(
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
                "CLI AUTO STATE TRAILS",
                "--pack",
                str(pack_dir),
                "--au-overlay",
                "au-state-trails",
                "--output",
                str(cli_pdf),
            ],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0, f"CLI --au-overlay au-state-trails failed:\n{proc.stderr}\n{proc.stdout}"
        assert_pdf_valid(
            cli_pdf,
            scale=25000,
            paper="a4",
            orientation="portrait",
            expected_epsg=BLUE_MOUNTAINS_EPSG,
            expected_gzd=BLUE_MOUNTAINS_GZD,
            expected_title="CLI AUTO STATE TRAILS",
            extra_required_text=["AHD"],
            require_osm_attribution=True,
            require_copernicus_attribution=False,
        )


@pytest.mark.live
@pytest.mark.skipif(
    not RUN_LIVE,
    reason="Live v1.2.0 Pan-Australia tests skipped by default; set GRIDFORGE_RUN_LIVE=1 to run.",
)
class TestLiveV12PanAustraliaTrails:
    """Live Pan-Australia E2E verification across TAS, QLD, ACT, WA, SA, NT + Auto cross-border."""

    def _out_dir(self, tmp_path: Path) -> Path:
        override = os.environ.get("GRIDFORGE_LIVE_OUT_DIR")
        if override:
            path = Path(override)
            path.mkdir(parents=True, exist_ok=True)
            return path
        return tmp_path

    def _run_state_site(self, state_key: str, tmp_path: Path) -> Path:
        site = PAN_AU_SITES[state_key]
        out_pdf = self._out_dir(tmp_path) / site["filename"]
        overlays = [site["overlay"], *site["extra_overlays"]]
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
            au_overlays=overlays,
            carto=CartoOptions(show_slope_shading=bool(site["slope"])),
        )
        res = run_render_job(spec, out_pdf)
        assert res.utm_epsg == site["epsg"]
        _assert_peak_rss_under_2gb(f"live-{state_key}")

        assert_pdf_valid(
            out_pdf,
            scale=25000,
            paper="a4",
            orientation="portrait",
            expected_epsg=site["epsg"],
            expected_gzd=site["gzd"],
            expected_title=site["title"].split("—")[0].strip(),
            extra_required_text=site["required_text"],
            require_osm_attribution=True,
            require_copernicus_attribution=False,
        )
        return out_pdf

    def test_live_tas_cradle_mountain_trails_slope(self, tmp_path: Path) -> None:
        self._run_state_site("tas", tmp_path)

    def test_live_qld_lamington_trails_slope(self, tmp_path: Path) -> None:
        self._run_state_site("qld", tmp_path)

    def test_live_act_namadgi_trails(self, tmp_path: Path) -> None:
        self._run_state_site("act", tmp_path)

    def test_live_wa_stirling_range_trails_slope(self, tmp_path: Path) -> None:
        self._run_state_site("wa", tmp_path)

    def test_live_sa_flinders_ranges_trails_low_ram(self, tmp_path: Path) -> None:
        self._run_state_site("sa", tmp_path)

    def test_live_nt_west_macdonnell_trails_and_hydro(self, tmp_path: Path) -> None:
        self._run_state_site("nt", tmp_path)

    def test_live_auto_state_trails_cli_tas_routing(self, tmp_path: Path) -> None:
        """Verify `--au-overlay au-state-trails` auto-routes Cradle Mountain TAS to LIST CC BY 3.0 AU."""
        site = PAN_AU_SITES["tas"]
        out_pdf = self._out_dir(tmp_path) / "v12_auto_tas_cradle_mountain_25k_a4.pdf"
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "gridforge.cli",
                "render",
                "--center",
                f"{site['lat']},{site['lon']}",
                "--scale",
                "25000",
                "--paper",
                "a4",
                "--mode",
                "color",
                "--dem-source",
                "au-elvis",
                "--au-overlay",
                "au-state-trails",
                "--slope-shading",
                "--title",
                "AUTO STATE TRAILS — CRADLE MTN",
                "--output",
                str(out_pdf),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0, f"CLI --au-overlay au-state-trails failed:\n{proc.stderr}\n{proc.stdout}"
        _assert_peak_rss_under_2gb("live-auto-tas")

        report = assert_pdf_valid(
            out_pdf,
            scale=25000,
            paper="a4",
            orientation="portrait",
            expected_epsg=site["epsg"],
            expected_gzd=site["gzd"],
            expected_title="AUTO STATE TRAILS",
            extra_required_text=["AHD", "Tasmania"],
            require_osm_attribution=True,
            require_copernicus_attribution=False,
        )
        # Ensure dynamic attribution emitted ONLY Tasmania, not unrelated mainland states
        assert "Queensland" not in report.extracted_text
        assert "Western Australia" not in report.extracted_text
