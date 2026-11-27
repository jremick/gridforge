"""Comprehensive unit tests for GridForge MCP server, visuals, transports, and CLI additions."""

from __future__ import annotations

import asyncio
import base64
import io
import json
import sys
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import Response
from fastapi.testclient import TestClient
from PIL import Image
import pytest
from typer.testing import CliRunner

from gridforge import __version__
from gridforge.cli import app as cli_app
from gridforge.config import (
    ColorMode,
    PaperSize,
    RenderResult,
)
from gridforge.mcp import (
    APP_MIME_TYPE,
    APP_ONLY,
    APP_URI,
    DEFAULT_PROTOCOL_VERSION,
    INLINE_UI,
    STUDIO_UI,
    GridForgeMCPServer,
    create_mcp_router,
    run_stdio_server,
)
from gridforge.mcp.server import CAPABILITIES_URI, GUIDE_URI, build_studio_url
from gridforge.mcp.visuals import (
    render_footprint_card_png,
    render_rf_link_chart_png,
    render_route_card_png,
)
from gridforge.web.app import create_app
from gridforge.web.schemas import (
    GeocodeResult,
    RFLinkProfileResponse,
    RFLinkSample,
    ViewshedPreviewResponse,
)


def _sample_rf_profile(status: str = "CLEAR") -> dict[str, Any]:
    samples: list[dict[str, Any]] = []
    for i in range(21):
        d = i * 0.5
        los = 900.0 + i * 5.0
        f1 = 22.0 * (1.0 - ((i - 10) / 10.0) ** 2)
        if status == "OBSTRUCTED" and 8 <= i <= 12:
            terr = los + 12.0 - abs(i - 10) * 4.0
        elif status == "MARGINAL" and 8 <= i <= 12:
            terr = los - 4.0
        else:
            terr = los - 45.0
        samples.append(
            RFLinkSample(
                dist_km=round(d, 2),
                terrain_m=round(terr, 1),
                los_m=round(los, 1),
                fresnel_lo_m=round(los - f1, 1),
                fresnel_hi_m=round(los + f1, 1),
                fresnel60_lo_m=round(los - 0.6 * f1, 1),
                clearance_m=round(los - terr, 1),
            ).model_dump()
        )
    min_clr = min(s["clearance_m"] for s in samples)
    return RFLinkProfileResponse(
        status=status,  # type: ignore[arg-type]
        distance_km=10.0,
        freq_mhz=915.0,
        fspl_db=111.7,
        min_clearance_m=min_clr,
        min_fresnel_pct=180.0 if status == "CLEAR" else (25.0 if status == "MARGINAL" else -40.0),
        los_clear=(status != "OBSTRUCTED"),
        fresnel_60pct_clear=(status == "CLEAR"),
        bearing_true_deg=54.2,
        bearing_grid_deg=53.1,
        bearing_mag_deg=41.5,
        bearing_mils=738.0,
        from_elev_m=898.0,
        to_elev_m=998.0,
        from_mgrs="56H KH 5100 6400",
        to_mgrs="56H KH 5910 6980",
        from_label="Katoomba Ridge",
        to_label="Blackheath Peak",
        from_height_m=3.0,
        to_height_m=5.0,
        samples=[RFLinkSample(**s) for s in samples],
    ).model_dump()


def _sample_viewshed_response() -> dict[str, Any]:
    return ViewshedPreviewResponse(
        observer_mgrs_10m="56H KH 5100 6400",
        observer_elev_m=1012.0,
        utm_epsg=32756,
        max_distance_m=5000.0,
        visible_area_km2=28.4,
        viewshed_geojson={
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "properties": {"visible": True},
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [
                            [
                                [150.29, -33.75],
                                [150.34, -33.75],
                                [150.34, -33.71],
                                [150.29, -33.71],
                                [150.29, -33.75],
                            ]
                        ],
                    },
                }
            ],
        },
        range_ring_geojson={
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "properties": {"radius_m": 5000.0},
                    "geometry": {
                        "type": "LineString",
                        "coordinates": [
                            [150.26, -33.732],
                            [150.312, -33.687],
                            [150.364, -33.732],
                            [150.312, -33.777],
                            [150.26, -33.732],
                        ],
                    },
                }
            ],
        },
    ).model_dump()


def _make_tiny_png(color: tuple[int, int, int] = (24, 48, 72), size: tuple[int, int] = (320, 440)) -> bytes:
    im = Image.new("RGB", size, color)
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture()
def web_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    monkeypatch.delenv("GRIDFORGE_AUTH_MODE", raising=False)
    monkeypatch.delenv("GRIDFORGE_API_KEY", raising=False)
    monkeypatch.delenv("GRIDFORGE_PUBLIC_URL", raising=False)
    return create_app(
        data_dir=tmp_path / "jobs",
        packs_dir=tmp_path / "packs",
        web_dist_dir=tmp_path / "no_dist",
    )


# ---------------------------------------------------------------------------
# 1. Zero QGIS / PyQt5 Guard & Protocol Negotiation
# ---------------------------------------------------------------------------


def test_mcp_package_has_zero_qgis_or_pyqt_imports() -> None:
    assert "qgis" not in sys.modules
    assert "PyQt5" not in sys.modules


def test_protocol_negotiation_and_initialize_capabilities(web_app: FastAPI) -> None:
    server = GridForgeMCPServer(app=web_app)

    for ver in ("2025-06-18", "2025-03-26", "2024-11-05"):
        resp = asyncio.run(
            server.handle_message(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {"protocolVersion": ver, "clientInfo": {"name": "pytest"}},
                }
            )
        )
        assert resp is not None
        res = resp["result"]
        assert res["protocolVersion"] == ver
        assert res["serverInfo"] == {"name": "gridforge", "version": __version__}
        assert res["capabilities"]["tools"] == {}
        assert res["capabilities"]["resources"] == {}
        assert res["capabilities"]["prompts"] == {}
        assert res["capabilities"]["extensions"]["io.modelcontextprotocol/ui"]["mimeTypes"] == [
            APP_MIME_TYPE
        ]

    # Unsupported version negotiates cleanly to default
    fallback = asyncio.run(
        server.handle_message(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "initialize",
                "params": {"protocolVersion": "1999-01-01"},
            }
        )
    )
    assert fallback is not None
    assert fallback["result"]["protocolVersion"] == DEFAULT_PROTOCOL_VERSION

    # Ping & notifications & unknown method
    ping = asyncio.run(server.handle_message({"jsonrpc": "2.0", "id": 3, "method": "ping"}))
    assert ping == {"jsonrpc": "2.0", "id": 3, "result": {}}

    notif = asyncio.run(
        server.handle_message({"jsonrpc": "2.0", "method": "notifications/initialized"})
    )
    assert notif is None

    err = asyncio.run(
        server.handle_message({"jsonrpc": "2.0", "id": 4, "method": "no/such/method"})
    )
    assert err is not None
    assert err["error"]["code"] == -32601


# ---------------------------------------------------------------------------
# 2. Tools, Resources, and Prompts Metadata Contracts
# ---------------------------------------------------------------------------


def test_tools_resources_prompts_metadata(web_app: FastAPI, tmp_path: Path) -> None:
    custom_html = tmp_path / "custom_app.html"
    custom_html.write_text("<!doctype html><html><body>Custom MCP App</body></html>", encoding="utf-8")
    server = GridForgeMCPServer(app=web_app, app_html_path=custom_html)

    # tools/list
    tools_resp = asyncio.run(
        server.handle_message({"jsonrpc": "2.0", "id": 10, "method": "tools/list"})
    )
    assert tools_resp is not None
    tools = {t["name"]: t for t in tools_resp["result"]["tools"]}
    expected_model_tools = {
        "open_gridforge_studio",
        "answer_location_briefing",
        "geocode_place",
        "preview_footprint",
        "analyze_rf_link",
        "compute_viewshed",
        "measure_route",
        "plan_strip_atlas",
        "render_map",
        "get_job_status",
        "get_job_page_preview",
        "create_share_link",
        "list_packs",
        "get_capabilities",
    }
    expected_app_only_tools = {
        "app_get_job_status",
        "app_get_job_page",
        "app_get_capabilities",
    }
    assert set(tools.keys()) == (expected_model_tools | expected_app_only_tools)

    studio_tool = tools["open_gridforge_studio"]
    assert studio_tool["inputSchema"]["required"] == []
    assert studio_tool["_meta"] == STUDIO_UI
    assert studio_tool["_meta"]["com.google.antigravity/ui"]["entrypoints"] == [
        {"type": "aux_pane", "title": "GridForge Studio"},
        {"type": "global", "title": "GridForge Maps"},
    ]

    for inline_name in (
        "answer_location_briefing",
        "geocode_place",
        "preview_footprint",
        "analyze_rf_link",
        "compute_viewshed",
        "measure_route",
        "plan_strip_atlas",
        "render_map",
        "get_job_status",
        "list_packs",
    ):
        assert tools[inline_name]["_meta"] == INLINE_UI

    for app_name in expected_app_only_tools:
        assert tools[app_name]["_meta"] == APP_ONLY
        assert tools[app_name]["_meta"]["ui"]["visibility"] == ["app"]

    # resources/list & resources/read
    res_list = asyncio.run(
        server.handle_message({"jsonrpc": "2.0", "id": 11, "method": "resources/list"})
    )
    assert res_list is not None
    resources = {r["uri"]: r for r in res_list["result"]["resources"]}
    assert APP_URI in resources
    assert resources[APP_URI]["mimeType"] == APP_MIME_TYPE
    assert resources[APP_URI]["_meta"] == {"ui": {"prefersBorder": False}}
    assert CAPABILITIES_URI in resources
    assert GUIDE_URI in resources

    app_read = asyncio.run(
        server.handle_message(
            {"jsonrpc": "2.0", "id": 12, "method": "resources/read", "params": {"uri": APP_URI}}
        )
    )
    assert app_read is not None
    app_item = app_read["result"]["contents"][0]
    assert app_item["uri"] == APP_URI
    assert app_item["mimeType"] == APP_MIME_TYPE
    assert app_item["_meta"] == {"ui": {"prefersBorder": False}}
    assert "Custom MCP App" in app_item["text"]

    cap_read = asyncio.run(
        server.handle_message(
            {"jsonrpc": "2.0", "id": 13, "method": "resources/read", "params": {"uri": CAPABILITIES_URI}}
        )
    )
    assert cap_read is not None
    caps_json = json.loads(cap_read["result"]["contents"][0]["text"])
    assert caps_json["version"] == __version__
    assert 25000 in caps_json["scales"]

    guide_read = asyncio.run(
        server.handle_message(
            {"jsonrpc": "2.0", "id": 14, "method": "resources/read", "params": {"uri": GUIDE_URI}}
        )
    )
    assert guide_read is not None
    assert "WMM2025" in guide_read["result"]["contents"][0]["text"]

    # prompts/list & prompts/get
    pr_list = asyncio.run(
        server.handle_message({"jsonrpc": "2.0", "id": 15, "method": "prompts/list"})
    )
    assert pr_list is not None
    prompt_names = {p["name"] for p in pr_list["result"]["prompts"]}
    assert prompt_names == {
        "plan_backcountry_map",
        "analyze_lora_link",
        "navigation_briefing",
        "plan_strip_atlas",
    }

    for pname, pargs in (
        ("plan_backcountry_map", {"location": "Yosemite Valley", "activity": "ski-touring"}),
        ("analyze_lora_link", {"from_location": "Peak A", "to_location": "Peak B", "freq_mhz": "915"}),
        ("navigation_briefing", {"location": "Mt Kosciuszko"}),
        ("plan_strip_atlas", {"route_description": "Overland Track", "scale": "50000"}),
    ):
        pr_get = asyncio.run(
            server.handle_message(
                {
                    "jsonrpc": "2.0",
                    "id": 16,
                    "method": "prompts/get",
                    "params": {"name": pname, "arguments": pargs},
                }
            )
        )
        assert pr_get is not None
        msgs = pr_get["result"]["messages"]
        assert len(msgs) == 1
        assert msgs[0]["role"] == "user"
        assert msgs[0]["content"]["type"] == "text"
        assert len(msgs[0]["content"]["text"]) > 40


# ---------------------------------------------------------------------------
# 3. Pure-Pillow Visual PNG Asset Generators
# ---------------------------------------------------------------------------


def test_visual_png_generators_valid_compact_and_fast(web_app: FastAPI) -> None:
    client = TestClient(web_app, base_url="http://127.0.0.1")

    # 1. RF Link Fresnel charts (CLEAR, MARGINAL, OBSTRUCTED)
    for st in ("CLEAR", "MARGINAL", "OBSTRUCTED"):
        t0 = time.perf_counter()
        png = render_rf_link_chart_png(_sample_rf_profile(st))
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        assert png.startswith(b"\x89PNG\r\n\x1a\n")
        assert len(png) <= 120_000
        with Image.open(io.BytesIO(png)) as im:
            assert im.size == (760, 360)
        assert elapsed_ms < 500.0

    # 2. Footprint card (multi-sheet 2x2 atlas + waypoints + viewshed)
    fp_resp = client.post(
        "/api/footprint",
        json={"center": "-33.732,150.312", "scale": 25000, "grid_cols": 2, "grid_rows": 2},
    )
    assert fp_resp.status_code == 200
    fp_data = fp_resp.json()
    waypoints = [
        {"label": "Trailhead", "lat": -33.735, "lon": 150.305, "category": "waypoint"},
        {"label": "Camp 1", "lat": -33.725, "lon": 150.318, "category": "camp"},
    ]
    fp_png = render_footprint_card_png(
        fp_data,
        title="Blue Mountains 2x2 Atlas",
        waypoints=waypoints,
        viewshed={
            **_sample_viewshed_response(),
            "observer": {"label": "Echo Point", "lat": -33.732, "lon": 150.312},
        },
    )
    assert fp_png.startswith(b"\x89PNG\r\n\x1a\n")
    assert len(fp_png) <= 120_000
    with Image.open(io.BytesIO(fp_png)) as im:
        assert im.size == (720, 460)

    # 3. Route card (RouteMetrics + Ruler with elevations + StripAtlasPlan)
    rm_resp = client.post("/api/route-metrics", json={"waypoints": waypoints})
    assert rm_resp.status_code == 200
    rm_png = render_route_card_png(rm_resp.json(), title="Blue Mountains Traverse")
    assert rm_png.startswith(b"\x89PNG\r\n\x1a\n")
    assert len(rm_png) <= 120_000
    with Image.open(io.BytesIO(rm_png)) as im:
        assert im.size == (720, 400)

    ruler_png = render_route_card_png(
        {
            "total_distance_m": 4250.0,
            "points": [{"lat": -33.735, "lon": 150.305}, {"lat": -33.72, "lon": 150.325}],
            "segments": [
                {
                    "from_idx": 0,
                    "to_idx": 1,
                    "distance_m": 4250.0,
                    "bearing_true_deg": 42.1,
                    "bearing_grid_deg": 41.3,
                    "bearing_mag_deg": 29.0,
                    "bearing_mils": 516.0,
                    "elev_start_m": 820.0,
                    "elev_end_m": 1040.0,
                    "grade_pct": 5.18,
                }
            ],
        },
        title="Ruler With Elevation",
    )
    assert ruler_png.startswith(b"\x89PNG\r\n\x1a\n")
    with Image.open(io.BytesIO(ruler_png)) as im:
        assert im.size == (720, 400)

    strip_resp = client.post(
        "/api/strip-plan",
        json={
            "route_geojson": {
                "type": "LineString",
                "coordinates": [[150.28, -33.75], [150.32, -33.72], [150.38, -33.68]],
            },
            "scale": 25000,
        },
    )
    assert strip_resp.status_code == 200
    strip_png = render_route_card_png(strip_resp.json(), title="Strip Atlas")
    assert strip_png.startswith(b"\x89PNG\r\n\x1a\n")
    with Image.open(io.BytesIO(strip_png)) as im:
        assert im.size == (720, 400)


# ---------------------------------------------------------------------------
# 4. End-to-End Tool Execution Against In-Process FastAPI App
# ---------------------------------------------------------------------------


def test_core_mcp_tools_against_asgi_app(
    web_app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import gridforge.web.app as web_app_mod

    # Mock Nominatim geocode & DEM-dependent RF/viewshed previews
    monkeypatch.setattr(
        web_app_mod,
        "_search_nominatim",
        lambda q, limit=5, on_cache_miss=None: [
            GeocodeResult(
                display_name="Mount Kosciuszko, NSW, Australia",
                short_name="Mount Kosciuszko",
                lat=-36.456,
                lon=148.263,
                type="peak",
            )
        ],
    )
    monkeypatch.setattr(
        web_app_mod,
        "compute_rf_link_preview",
        lambda req, **kwargs: RFLinkProfileResponse(**_sample_rf_profile("CLEAR")),
    )
    monkeypatch.setattr(
        web_app_mod,
        "compute_viewshed_preview",
        lambda req, **kwargs: ViewshedPreviewResponse(**_sample_viewshed_response()),
    )

    server = GridForgeMCPServer(app=web_app, public_url="https://gridmaps.example")

    async def _call(name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        resp = await server.handle_message(
            {
                "jsonrpc": "2.0",
                "id": name,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments or {}},
            }
        )
        assert resp is not None
        assert "result" in resp, f"Unexpected error for {name}: {resp}"
        return resp["result"]

    # 1. open_gridforge_studio with {}
    studio = asyncio.run(_call("open_gridforge_studio", {}))
    assert not studio["isError"]
    sc = studio["structuredContent"]
    assert sc["view"] == "studio"
    assert len(sc["presets"]) >= 6
    assert sc["public_url"] == "https://gridmaps.example"

    # 2. answer_location_briefing with place name (Australia -> au-elvis)
    brief_au = asyncio.run(
        _call(
            "answer_location_briefing",
            {"location": "Mt Kosciuszko", "question": "What is the magnetic declination?"},
        )
    )
    assert not brief_au["isError"]
    assert any(c["type"] == "image" and c["mimeType"] == "image/png" for c in brief_au["content"])
    sc_au = brief_au["structuredContent"]
    assert sc_au["view"] == "briefing"
    assert sc_au["location"]["place_name"] == "Mount Kosciuszko"
    assert sc_au["recommendations"]["dem_source"] == "au-elvis"
    assert "au-state-trails" in sc_au["recommendations"]["overlays"]
    assert sc_au["studio_url"].startswith("https://gridmaps.example/?")
    assert "WMM2025" in sc_au["declination"]["model_name"]

    # Regional recommendations spot-checks (US, NZ, CA, Global)
    for loc, expected_dem in (
        ("37.7456,-119.5936", "usgs3dep"),
        ("-39.133,175.642", "nz-linz"),
        ("51.4167,-116.2167", "ca-hrdem"),
        ("45.9237,6.8694", "glo30"),
    ):
        res = asyncio.run(_call("answer_location_briefing", {"location": loc, "include_image": False}))
        assert res["structuredContent"]["recommendations"]["dem_source"] == expected_dem

    # 3. geocode_place
    geo = asyncio.run(_call("geocode_place", {"query": "Kosciuszko"}))
    assert geo["structuredContent"]["view"] == "geocode"
    assert len(geo["structuredContent"]["results"]) == 1

    # 4. preview_footprint (2x2 atlas)
    fp = asyncio.run(
        _call("preview_footprint", {"center": "-33.732,150.312", "grid_cols": 2, "grid_rows": 2})
    )
    assert fp["structuredContent"]["view"] == "briefing"
    assert fp["structuredContent"]["sheet"]["sheet_count"] == 4
    assert any(c["type"] == "image" for c in fp["content"])

    # 5. analyze_rf_link
    rf = asyncio.run(
        _call(
            "analyze_rf_link",
            {
                "from_lat": -33.732,
                "from_lon": 150.312,
                "to_lat": -33.68,
                "to_lon": 150.38,
                "freq_mhz": 915.0,
            },
        )
    )
    assert not rf["isError"]
    assert rf["structuredContent"]["view"] == "rf_link"
    assert rf["structuredContent"]["profile"]["status"] == "CLEAR"
    assert "rf=" in rf["structuredContent"]["studio_url"]
    img_block = next(c for c in rf["content"] if c["type"] == "image")
    assert base64.b64decode(img_block["data"]).startswith(b"\x89PNG")

    # 6. compute_viewshed
    vs = asyncio.run(
        _call("compute_viewshed", {"lat": -33.732, "lon": 150.312, "max_distance_m": 5000})
    )
    assert not vs["isError"]
    assert vs["structuredContent"]["view"] == "viewshed"
    assert vs["structuredContent"]["result"]["visible_area_km2"] == 28.4
    assert "los=" in vs["structuredContent"]["studio_url"]
    assert any(c["type"] == "image" for c in vs["content"])

    # 7. measure_route (waypoints & ruler points)
    rt = asyncio.run(
        _call(
            "measure_route",
            {
                "waypoints": [
                    {"label": "Start", "lat": -33.735, "lon": 150.305},
                    {"label": "Summit", "lat": -33.720, "lon": 150.320, "category": "camp"},
                ]
            },
        )
    )
    assert not rt["isError"]
    assert rt["structuredContent"]["view"] == "route"
    assert rt["structuredContent"]["route_metrics"]["total_distance_km"] > 1.0
    assert any(c["type"] == "image" for c in rt["content"])

    rl = asyncio.run(
        _call(
            "measure_route",
            {"points": [{"lat": -33.735, "lon": 150.305}, {"lat": -33.720, "lon": 150.320}]},
        )
    )
    assert not rl["isError"]
    assert rl["structuredContent"]["ruler_metrics"]["total_distance_m"] > 1000.0

    # 8. plan_strip_atlas
    strip = asyncio.run(
        _call(
            "plan_strip_atlas",
            {
                "route_geojson": {
                    "type": "LineString",
                    "coordinates": [[150.28, -33.75], [150.35, -33.70]],
                }
            },
        )
    )
    assert not strip["isError"]
    assert strip["structuredContent"]["view"] == "route"
    assert strip["structuredContent"]["strip_plan"]["sheet_count"] >= 1
    assert any(c["type"] == "image" for c in strip["content"])

    # 9. render_map + get_job_status + app_get_job_status
    rnd = asyncio.run(
        _call(
            "render_map",
            {"center": "-33.732,150.312", "scale": 25000, "color_mode": "alpine", "wait": False},
        )
    )
    assert not rnd["isError"]
    sc_rnd = rnd["structuredContent"]
    assert sc_rnd["view"] == "render"
    job_id = sc_rnd["job"]["job_id"]
    assert sc_rnd["job"]["status"] == "queued"
    assert sc_rnd["share_url"].startswith("https://gridmaps.example/?share=")

    st_res = asyncio.run(_call("get_job_status", {"job_id": job_id}))
    assert st_res["structuredContent"]["job"]["job_id"] == job_id

    app_st = asyncio.run(_call("app_get_job_status", {"job_id": job_id}))
    assert app_st["structuredContent"]["job"]["job_id"] == job_id

    # 10. create_share_link, list_packs, get_capabilities, app_get_capabilities
    sh = asyncio.run(
        _call("create_share_link", {"name": "Katoomba", "center": "-33.732,150.312", "scale": 25000})
    )
    assert sh["structuredContent"]["share_url"].startswith("https://gridmaps.example/?share=")

    pk = asyncio.run(_call("list_packs", {"group": "nz"}))
    assert pk["structuredContent"]["view"] == "packs"
    assert len(pk["structuredContent"]["regions"]) >= 5
    assert all(r["group"] == "nz" for r in pk["structuredContent"]["regions"])

    caps = asyncio.run(_call("get_capabilities", {}))
    assert caps["structuredContent"]["version"] == __version__

    app_caps = asyncio.run(_call("app_get_capabilities", {}))
    assert "capabilities" in app_caps["structuredContent"]
    assert "packs" in app_caps["structuredContent"]


def test_completed_render_page_previews_and_app_only_page_fetch() -> None:
    """Verify signed download enrichment, page 1 ImageContent, page 2 inspection, and app-only base64 isolation."""
    fake_app = FastAPI()
    page1_png = _make_tiny_png((20, 60, 40))
    page2_png = _make_tiny_png((80, 30, 30))

    completed_job = {
        "job_id": "job_done_1234",
        "status": "succeeded",
        "stage": "completed",
        "progress_pct": 100,
        "title": "Yosemite Alpine",
        "center": "37.7456,-119.5936",
        "center_mgrs": "11S KB 7150 8120",
        "scale": 24000,
        "paper": "letter",
        "orientation": "portrait",
        "color_mode": "alpine",
        "grid_cols": 1,
        "grid_rows": 1,
        "created_at": "2026-01-01T00:00:00Z",
        "artifacts": [
            {
                "kind": "pdf",
                "filename": "yosemite.pdf",
                "size_bytes": 420_000,
                "download_url": "/api/jobs/job_done_1234/artifacts/yosemite.pdf",
                "signed_download_url": "/api/jobs/job_done_1234/artifacts/yosemite.pdf?sig=tok123",
            }
        ],
    }

    @fake_app.post("/api/jobs")
    def _create_job() -> dict[str, Any]:
        return completed_job

    @fake_app.get("/api/jobs/{job_id}")
    def _get_job(job_id: str) -> dict[str, Any]:
        return completed_job

    @fake_app.get("/api/jobs/{job_id}/pages")
    def _get_pages(job_id: str) -> dict[str, Any]:
        return {
            "job_id": job_id,
            "page_count": 2,
            "pages": [
                {
                    "page": 1,
                    "preview_url": f"/api/jobs/{job_id}/pages/1.png",
                    "signed_preview_url": f"/api/jobs/{job_id}/pages/1.png?sig=p1",
                },
                {
                    "page": 2,
                    "preview_url": f"/api/jobs/{job_id}/pages/2.png",
                    "signed_preview_url": f"/api/jobs/{job_id}/pages/2.png?sig=p2",
                },
            ],
        }

    @fake_app.get("/api/jobs/{job_id}/pages/{page}.png")
    def _get_page_png(job_id: str, page: int) -> Response:
        return Response(content=page1_png if page == 1 else page2_png, media_type="image/png")

    server = GridForgeMCPServer(app=fake_app, public_url="https://gridmaps.net")

    # render_map returns page 1 ImageContent + signed download URLs + resource_link
    res = asyncio.run(
        server.handle_message(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "render_map",
                    "arguments": {"center": "37.7456,-119.5936", "wait": True},
                },
            }
        )
    )
    assert res is not None
    result = res["result"]
    assert not result["isError"]
    assert any(c["type"] == "image" for c in result["content"])
    assert any(
        c["type"] == "resource_link"
        and c["uri"] == "https://gridmaps.net/api/jobs/job_done_1234/artifacts/yosemite.pdf?sig=tok123"
        for c in result["content"]
    )
    sc = result["structuredContent"]
    assert sc["page_count"] == 2
    assert sc["job"]["artifacts"][0]["signed_download_url"] == (
        "https://gridmaps.net/api/jobs/job_done_1234/artifacts/yosemite.pdf?sig=tok123"
    )

    # get_job_page_preview fetches page 2 as ImageContent
    p2 = asyncio.run(
        server.handle_message(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "get_job_page_preview",
                    "arguments": {"job_id": "job_done_1234", "page": 2, "width": 800},
                },
            }
        )
    )
    assert p2 is not None
    p2_img = next(c for c in p2["result"]["content"] if c["type"] == "image")
    assert base64.b64decode(p2_img["data"]) == page2_png

    # app_get_job_page keeps base64 strictly inside structuredContent (NOT as ImageContent!)
    app_p2 = asyncio.run(
        server.handle_message(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "app_get_job_page",
                    "arguments": {"job_id": "job_done_1234", "page": 2, "width": 1100},
                },
            }
        )
    )
    assert app_p2 is not None
    assert not any(c["type"] == "image" for c in app_p2["result"]["content"])
    app_sc = app_p2["result"]["structuredContent"]
    assert app_sc["page"] == 2
    assert app_sc["page_count"] == 2
    assert base64.b64decode(app_sc["data_base64"]) == page2_png


# ---------------------------------------------------------------------------
# 5. Streamable HTTP /mcp Router & Auth Forwarding
# ---------------------------------------------------------------------------


def test_streamable_http_mcp_router_lifecycle_and_auth(
    web_app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen_auth_headers: list[str | None] = []
    orig_capabilities = next(
        r.endpoint for r in web_app.routes if getattr(r, "path", None) == "/api/capabilities"
    )
    assert orig_capabilities is not None

    @web_app.middleware("http")
    async def _capture_caps_auth(request: Request, call_next: Any) -> Any:
        if request.url.path == "/api/capabilities":
            seen_auth_headers.append(request.headers.get("authorization"))
        return await call_next(request)

    client = TestClient(web_app, base_url="http://127.0.0.1:8080")

    # GET /mcp discovery JSON vs SSE 405
    get_json = client.get("/mcp", headers={"Accept": "application/json"})
    assert get_json.status_code == 200
    assert get_json.json()["transport"] == "streamable-http"

    get_sse = client.get("/mcp", headers={"Accept": "text/event-stream"})
    assert get_sse.status_code == 405
    assert "POST" in get_sse.headers["allow"]

    # POST /mcp initialize mints Mcp-Session-Id
    init_resp = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-03-26"},
        },
    )
    assert init_resp.status_code == 200
    session_id = init_resp.headers["mcp-session-id"]
    assert session_id
    assert init_resp.headers["mcp-protocol-version"] == "2025-03-26"
    assert init_resp.json()["result"]["protocolVersion"] == "2025-03-26"

    # Notification -> 202 Accepted
    notif_resp = client.post(
        "/mcp",
        headers={"Mcp-Session-Id": session_id},
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
    )
    assert notif_resp.status_code == 202
    assert notif_resp.content == b""

    # Batch JSON-RPC + auth header forwarding + public_url inference from Host
    batch_resp = client.post(
        "/mcp",
        headers={
            "Mcp-Session-Id": session_id,
            "Authorization": "Bearer gf_test_secret_token",
        },
        json=[
            {"jsonrpc": "2.0", "id": "b1", "method": "ping"},
            {
                "jsonrpc": "2.0",
                "id": "b2",
                "method": "tools/call",
                "params": {"name": "get_capabilities", "arguments": {}},
            },
            {
                "jsonrpc": "2.0",
                "id": "b3",
                "method": "tools/call",
                "params": {
                    "name": "preview_footprint",
                    "arguments": {"center": "-33.732,150.312", "include_image": False},
                },
            },
        ],
    )
    assert batch_resp.status_code == 200
    items = batch_resp.json()
    assert len(items) == 3
    assert items[0]["result"] == {}
    assert seen_auth_headers[-1] == "Bearer gf_test_secret_token"
    assert items[2]["result"]["structuredContent"]["studio_url"].startswith("http://127.0.0.1:8080/?")

    # Invalid JSON -> 400 parse error
    bad_json = client.post("/mcp", content=b"{not-json", headers={"Content-Type": "application/json"})
    assert bad_json.status_code == 400
    assert bad_json.json()["error"]["code"] == -32700

    # DELETE /mcp terminates session; subsequent reuse returns 404
    assert client.delete("/mcp").status_code == 400
    assert client.delete("/mcp", headers={"Mcp-Session-Id": session_id}).status_code == 204
    assert client.delete("/mcp", headers={"Mcp-Session-Id": session_id}).status_code == 404
    reuse = client.post(
        "/mcp",
        headers={"Mcp-Session-Id": session_id},
        json={"jsonrpc": "2.0", "id": 9, "method": "ping"},
    )
    assert reuse.status_code == 404

    # Single-tenant GRIDFORGE_API_KEY protection in auth_mode="none"
    monkeypatch.setenv("GRIDFORGE_API_KEY", "super-secret-key")
    assert client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"}).status_code == 401
    assert (
        client.post(
            "/mcp",
            headers={"Authorization": "Bearer wrong-key"},
            json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
        ).status_code
        == 401
    )
    ok_key = client.post(
        "/mcp",
        headers={"X-API-Key": "super-secret-key"},
        json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
    )
    assert ok_key.status_code == 200


# ---------------------------------------------------------------------------
# 6. Stdio Transport Loop
# ---------------------------------------------------------------------------


def test_stdio_transport_strict_json_lines(web_app: FastAPI) -> None:
    lines = [
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}}),
        json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
        "{malformed-json",
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "preview_footprint",
                    "arguments": {"center": "-33.732,150.312", "include_image": False},
                },
            }
        ),
        "",
    ]
    in_buf = io.StringIO("\n".join(lines) + "\n")
    out_buf = io.StringIO()

    run_stdio_server(app=web_app, input_stream=in_buf, output_stream=out_buf)

    out_lines = [ln for ln in out_buf.getvalue().splitlines() if ln.strip()]
    # Notification emits no output line, so exactly 3 JSON responses
    assert len(out_lines) == 3
    parsed = [json.loads(ln) for ln in out_lines]
    assert parsed[0]["id"] == 1
    assert parsed[0]["result"]["protocolVersion"] == "2025-06-18"
    assert parsed[1]["error"]["code"] == -32700
    assert parsed[2]["id"] == 2
    assert parsed[2]["result"]["structuredContent"]["view"] == "briefing"


# ---------------------------------------------------------------------------
# 7. CLI Additions: `gridforge mcp` & `gridforge render --spec-file`
# ---------------------------------------------------------------------------


def test_cli_mcp_help_and_render_spec_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import gridforge.cli as cli_mod

    runner = CliRunner()

    # 1. gridforge mcp --help
    help_res = runner.invoke(cli_app, ["mcp", "--help"])
    assert help_res.exit_code == 0, help_res.output
    for flag in ("--url", "--api-key", "--public-url", "--data-dir", "--packs-dir", "--app-html"):
        assert flag in help_res.output

    captured: list[tuple[Any, Path, dict[str, Any]]] = []

    def _fake_render_job(spec: Any, output: Path, **kwargs: Any) -> RenderResult:
        captured.append((spec, output, kwargs))
        return RenderResult(
            output_pdf=output,
            page_width_mm=210.0,
            page_height_mm=297.0,
            scale=spec.scale,
            utm_epsg=32756,
            elapsed_seconds=0.42,
        )

    monkeypatch.setattr(cli_mod, "run_render_job", _fake_render_job)

    # 2. CreateJobRequest JSON spec file
    job_spec_path = tmp_path / "job_spec.json"
    job_spec_path.write_text(
        json.dumps(
            {
                "center": "-33.732, 150.312",
                "scale": 50000,
                "paper": "a3",
                "orientation": "landscape",
                "color_mode": "alpine",
                "title": "Blue Mtns Spec",
                "outputs": {"pdf": True, "geopdf": True, "mbtiles_format": "jpg"},
                "waypoints": [{"label": "Base", "lat": -33.732, "lon": 150.312, "category": "camp"}],
                "include_calibration_page": True,
            }
        ),
        encoding="utf-8",
    )
    out_pdf = tmp_path / "out_job.pdf"
    res_job = runner.invoke(
        cli_app,
        ["render", "--spec-file", str(job_spec_path), "-O", str(out_pdf), "--json"],
    )
    assert res_job.exit_code == 0, res_job.output
    assert len(captured) == 1
    spec0, out0, kw0 = captured[0]
    assert out0 == out_pdf
    assert spec0.scale == 50000
    assert spec0.paper == PaperSize.A3
    assert spec0.color_mode == ColorMode.ALPINE
    assert spec0.geopdf is True
    assert spec0.include_calibration_page is True
    assert len(spec0.waypoints) == 1
    assert kw0["mbtiles_format"].value == "jpg"
    assert json.loads(res_job.output)["scale"] == 50000

    # 3. SheetSpec JSON via stdin ("-")
    sheet_json = json.dumps(
        {
            "center_lat": 37.7456,
            "center_lon": -119.5936,
            "scale": 24000,
            "paper": "letter",
            "color_mode": "night-red",
            "title": "Yosemite Night",
        }
    )
    out_stdin = tmp_path / "out_stdin.pdf"
    res_stdin = runner.invoke(
        cli_app,
        ["render", "--spec-file", "-", "-O", str(out_stdin)],
        input=sheet_json,
    )
    assert res_stdin.exit_code == 0, res_stdin.output
    assert len(captured) == 2
    assert captured[1][0].scale == 24000
    assert captured[1][0].color_mode == ColorMode.NIGHT_RED

    # 4. Studio deep-link helper sanity
    url = build_studio_url(
        "https://gridmaps.net",
        center="-33.732,150.312",
        scale=25000,
        paper="a4",
        color_mode="alpine",
        grid_cols=2,
        grid_rows=2,
        include_calibration_page=True,
    )
    assert url is not None
    assert "center=-33.732,150.312" in url
    assert "mode=alpine" in url
    assert "grid=2x2" in url
    assert "cal=1" in url

    # 5. Error handling: mutually exclusive --spec-file and --strip-route, invalid JSON
    bad_json_path = tmp_path / "broken.json"
    bad_json_path.write_text("{broken", encoding="utf-8")
    res_bad = runner.invoke(
        cli_app,
        ["render", "--spec-file", str(bad_json_path), "-O", str(tmp_path / "x.pdf")],
    )
    assert res_bad.exit_code != 0

    res_conflict = runner.invoke(
        cli_app,
        [
            "render",
            "--spec-file",
            str(job_spec_path),
            "--strip-route",
            str(job_spec_path),
            "-O",
            str(tmp_path / "x.pdf"),
        ],
    )
    assert res_conflict.exit_code != 0


def test_app_html_env_override_and_builtin_fallback(
    web_app: FastAPI,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_html = tmp_path / "live_override.html"
    env_html.write_text("<!doctype html><html><body>Live Env Override</body></html>", encoding="utf-8")
    monkeypatch.setenv("GRIDFORGE_MCP_APP_HTML", str(env_html))

    srv_env = GridForgeMCPServer(app=web_app)
    assert "Live Env Override" in srv_env.load_app_html()

    monkeypatch.delenv("GRIDFORGE_MCP_APP_HTML", raising=False)
    srv_default = GridForgeMCPServer(app=web_app)
    html = srv_default.load_app_html()
    assert "<!doctype html>" in html.lower()


def test_render_map_forwards_wait_and_timeout_params() -> None:
    seen_params: list[dict[str, str]] = []
    fake_app = FastAPI()

    @fake_app.post("/api/jobs")
    def _post_job(request: Request) -> dict[str, Any]:
        seen_params.append(dict(request.query_params))
        return {
            "job_id": "job_wait_test",
            "status": "queued",
            "stage": "queued",
            "progress_pct": 0,
            "center": "-33.732,150.312",
            "scale": 25000,
            "paper": "a4",
            "orientation": "portrait",
            "color_mode": "color",
            "grid_cols": 1,
            "grid_rows": 1,
            "created_at": "2026-01-01T00:00:00Z",
            "artifacts": [],
        }

    srv = GridForgeMCPServer(app=fake_app)
    res = asyncio.run(
        srv.handle_message(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "render_map",
                    "arguments": {"center": "56HKH51006400", "wait": True, "timeout_s": 90},
                },
            }
        )
    )
    assert res is not None
    assert not res["result"]["isError"]
    assert seen_params[-1]["wait"] == "true"
    assert float(seen_params[-1]["timeout_s"]) == 90.0

