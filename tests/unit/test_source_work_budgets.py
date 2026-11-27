"""Offline provider contracts for bounded reads and explicit outbound destinations."""

from __future__ import annotations

import io
import json
import struct
import time
import zipfile
import zlib
from pathlib import Path

import httpx
import pytest

from gridforge.config import BBox
from gridforge.sources import _http, dem_ca_hrdem, overlays_au_states, overlays_au_states_west
from gridforge.sources.osm_overpass import OverpassOSMSource, PRIMARY_OVERPASS_URL


_BBOX = BBox(min_x=145.0, min_y=-38.0, max_x=145.1, max_y=-37.9)
_POINT = {"type": "Feature", "geometry": {"type": "Point", "coordinates": [145.0, -38.0]}}
_TRUSTED_COG = "https://canelevation-dem.s3.ca-central-1.amazonaws.com/hrdem-mosaic-2m/test.tif"
_OTHER_BUCKET = "https://unrelated-bucket.s3.ca-central-1.amazonaws.com/data.tif"


class FixtureStream(httpx.SyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks

    def __iter__(self):
        yield from self.chunks


@pytest.mark.parametrize("budget", ["features", "coordinates", "bytes"])
def test_wfs_rejects_over_budget_response_and_keeps_cache_empty(tmp_path, monkeypatch, budget):
    feature_limit = 4
    features = [_POINT]
    if budget == "features":
        features = [_POINT] * (feature_limit + 1)
    if budget == "coordinates":
        line = {"type": "LineString", "coordinates": [[145.0, -38.0]] * 20}
        features = [{"type": "Feature", "geometry": line}]
        monkeypatch.setattr(overlays_au_states, "_MAX_WFS_GEOMETRY_VALUES", 30, raising=False)
    raw = json.dumps({"type": "FeatureCollection", "features": features}).encode()
    if budget == "bytes":
        monkeypatch.setattr(overlays_au_states, "_MAX_WFS_BYTES", len(raw) - 1, raising=False)

    def handler(request):
        assert request.url.params["count"] == str(feature_limit)
        return httpx.Response(200, stream=FixtureStream([raw[:50], raw[50:]]))

    cache = tmp_path / "layer.json"
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        features = overlays_au_states.query_wfs_geojson(
            "https://wfs.example.invalid/query",
            "trails",
            _BBOX,
            cache_path=cache,
            max_features=feature_limit,
            retries=1,
            client=client,
        )
    assert features == []
    assert not cache.exists()


def test_wfs_revalidates_existing_cache_then_serves_bounded_response(tmp_path):
    cache = tmp_path / "layer.json"
    cache.write_text(json.dumps([_POINT] * 5))
    calls = []

    def handler(request):
        calls.append(request.url)
        return httpx.Response(200, json={"type": "FeatureCollection", "features": [_POINT]})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        for _ in range(2):
            assert overlays_au_states.query_wfs_geojson(
                "https://wfs.example.invalid/query",
                "trailheads",
                _BBOX,
                cache_path=cache,
                max_features=4,
                client=client,
            ) == [_POINT]
    assert len(calls) == 1


def test_wfs_streams_final_response_without_reading_redirect_body(tmp_path):
    class UnusedBody(httpx.SyncByteStream):
        def __iter__(self):
            pytest.fail("A provider redirect body was read")
            yield b""

    def handler(request):
        if request.url.path == "/query":
            return httpx.Response(307, headers={"Location": "/data"}, stream=UnusedBody())
        return httpx.Response(200, json={"type": "FeatureCollection", "features": [_POINT]})

    with httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True) as client:
        assert overlays_au_states.query_wfs_geojson(
            "https://wfs.example.invalid/query", "trailheads", _BBOX,
            cache_path=tmp_path / "layer.json", client=client,
        ) == [_POINT]


def test_sa_parks_rejects_excess_stream_bytes_then_recovers(tmp_path, monkeypatch):
    monkeypatch.setattr(overlays_au_states_west, "_MAX_SA_PARKS_BYTES", 140, raising=False)
    calls = []
    parks = [{"name": "Fixture park", "lat": -31.53, "lng": 138.60}]

    def handler(request):
        calls.append(request.url)
        if len(calls) == 1:
            return httpx.Response(200, stream=FixtureStream([b" " * 70, b" " * 71]))
        return httpx.Response(200, json=parks)

    area = BBox(min_x=138.5, min_y=-31.6, max_x=138.7, max_y=-31.4)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        source = overlays_au_states_west.SATrailsSource(cache_dir=tmp_path, client=client)
        source.cache_dir.mkdir(parents=True, exist_ok=True)
        cached_file = source.cache_dir / "sa_parks_data.json"
        cached_file.write_bytes(b" " * 141)
        assert source._load_sa_parks_in_bbox(area) == []
        assert not list(source.cache_dir.glob("*.part"))
        assert source._load_sa_parks_in_bbox(area)[0][1]["name"] == "Fixture park"
        assert source._load_sa_parks_in_bbox(area)[0][1]["name"] == "Fixture park"
        assert json.loads(cached_file.read_text()) == parks
    assert len(calls) == 2


def test_sa_cache_replaces_invalid_archive_only_after_validation(tmp_path):
    dest = tmp_path / "trails.zip"
    dest.write_bytes(b"incomplete fixture archive")
    inner = overlays_au_states_west.DEFAULT_SA_TRAILS_ZIP_INNER
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(inner, json.dumps({"type": "FeatureCollection", "features": [_POINT]}))
    fixture = buffer.getvalue()
    responses = iter([b"temporary non-zip provider response", fixture])

    def handler(request):
        return httpx.Response(200, content=next(responses))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        def lookup():
            return overlays_au_states_west._stream_download_cached(
                "https://trails.example.invalid/trails.zip", dest, max_bytes=1024,
                validate=overlays_au_states_west._sa_trails_zip_member, timeout_s=10, client=client,
            )

        assert lookup() is None
        assert dest.read_bytes() == b"incomplete fixture archive"
        assert lookup() == (dest, inner)
        assert lookup() == (dest, inner)
    assert dest.read_bytes() == fixture
    assert not list(tmp_path.glob("*.part"))


def test_canadian_stac_accepts_only_named_provider_buckets():
    payload = {
        "features": [
            {"assets": {"dtm": {"href": _OTHER_BUCKET}}},
            {"assets": {"dtm": {"href": _TRUSTED_COG}}},
        ]
    }
    assert dem_ca_hrdem.extract_stac_cog_hrefs(payload) == [_TRUSTED_COG]


def test_canadian_stac_declines_redirects_with_injected_client(tmp_path):
    calls = []

    def handler(request):
        calls.append(request.url)
        if request.url.host == "datacube.services.geo.ca":
            return httpx.Response(307, headers={"Location": "https://catalog.example.invalid/search"})
        return httpx.Response(200, json={"features": []})

    with httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True) as client:
        source = dem_ca_hrdem.CanadaHRDEMSource(cache_dir=tmp_path, client=client)
        assert source._stac_search("hrdem-mosaic-2m", _BBOX) == []
    assert [url.host for url in calls] == ["datacube.services.geo.ca"]


def test_overpass_mirrors_require_explicit_opt_in(monkeypatch):
    monkeypatch.delenv("OVERPASS_URL", raising=False)
    monkeypatch.delenv("OVERPASS_FALLBACK_URLS", raising=False)
    assert OverpassOSMSource().endpoints == [PRIMARY_OVERPASS_URL]

    primary = "https://primary.example.invalid/api/interpreter"
    secondary = "https://secondary.example.invalid/api/interpreter"
    monkeypatch.setenv("OVERPASS_URL", primary)
    assert OverpassOSMSource().endpoints == [primary]
    monkeypatch.setenv("OVERPASS_FALLBACK_URLS", f" {secondary}, {primary}, {secondary} ")
    assert OverpassOSMSource().endpoints == [primary, secondary]


def test_overpass_does_not_forward_queries_on_http_redirect(tmp_path):
    primary = "https://primary.example.invalid/api/interpreter"
    requested = []

    def handler(request):
        requested.append(str(request.url))
        return httpx.Response(307, headers={"Location": "https://other.example.invalid/api/interpreter"})

    with httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True) as client:
        source = OverpassOSMSource(endpoints=[primary], max_rounds=1, client=client)
        with pytest.raises(RuntimeError):
            source._download_with_failover_to_file("[out:xml];out;", tmp_path / "overpass.osm")
    assert requested == [primary]


@pytest.mark.parametrize("redirect", [False, True])
def test_gdal_can_only_use_bounded_direct_cog_ranges(tmp_path, redirect):
    data = (Path(__file__).resolve().parents[1] / "fixtures" / "synthetic_dem.tif").read_bytes()
    calls = []

    def handler(request):
        calls.append(request)
        if redirect:
            return httpx.Response(307, headers={"Location": "https://storage.example.invalid/data.tif"})
        headers = {"Content-Length": str(len(data)), "Accept-Ranges": "bytes"}
        if request.method == "HEAD":
            return httpx.Response(200, headers=headers)
        start, end = (int(value) for value in request.headers["Range"].removeprefix("bytes=").split("-"))
        end = min(end, len(data) - 1)
        headers.update({"Content-Length": str(end - start + 1), "Content-Range": f"bytes {start}-{end}/{len(data)}"})
        return httpx.Response(206, headers=headers, content=data[start:end + 1])

    output = tmp_path / "clip.tif"
    with httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True) as client:
        source = dem_ca_hrdem.CanadaHRDEMSource(cache_dir=tmp_path, client=client)
        if redirect:
            with pytest.raises(RuntimeError):
                source._warp_cogs([_TRUSTED_COG], ["-ts", "32", "32"], output, context="Offline fixture")
        else:
            source._warp_cogs([_TRUSTED_COG], ["-ts", "32", "32"], output, context="Offline fixture")
            assert output.is_file()
            assert any(request.method == "GET" for request in calls)
    assert calls and all(str(r.url) == _TRUSTED_COG for r in calls)


def test_sa_trails_zip_rejects_forged_file_size_deflate_bomb(tmp_path, monkeypatch):
    monkeypatch.setattr(overlays_au_states_west, "_MAX_SA_TRAILS_MEMBER_BYTES", 100, raising=False)
    inner = overlays_au_states_west.DEFAULT_SA_TRAILS_ZIP_INNER
    payload = b'{"type":"FeatureCollection","pad":"' + (b"A" * 2000) + b'"}'
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(inner, payload)
    raw = buf.getvalue()
    real_crc = zlib.crc32(payload) & 0xFFFFFFFF
    fake_size = 10
    fake_crc = zlib.crc32(payload[:fake_size]) & 0xFFFFFFFF
    forged = (
        raw.replace(struct.pack("<I", len(payload)), struct.pack("<I", fake_size))
        .replace(struct.pack("<I", real_crc), struct.pack("<I", fake_crc))
    )
    zip_path = tmp_path / "forged.zip"
    zip_path.write_bytes(forged)

    with pytest.raises((ValueError, zipfile.BadZipFile)):
        overlays_au_states_west._sa_trails_zip_member(zip_path)


@pytest.mark.parametrize(
    "redirect_target",
    [
        "http://wfs.example.invalid/downgrade",
        "https://127.0.0.1/secret",
        "https://169.254.169.254/latest/meta-data/",
        "https://evil.example.invalid/steal-key",
        "https://user:pass@wfs.example.invalid/data",
    ],
)
def test_stream_provider_get_blocks_unsafe_redirects(redirect_target):
    calls: list[str] = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(302, headers={"Location": redirect_target})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ValueError):
            with _http.stream_provider_get(
                client,
                "https://wfs.example.invalid/query",
                params=None,
                user_agent="gridforge-test",
                deadline=time.monotonic() + 5.0,
            ):
                pass
    assert calls == ["https://wfs.example.invalid/query"]


def test_stream_provider_get_allows_allowlisted_subdomain_redirect():
    calls: list[str] = []

    def handler(request):
        calls.append(str(request.url))
        if request.url.host == "www.waterconnect.sa.gov.au":
            return httpx.Response(
                301,
                headers={"Location": "https://apps.waterconnect.sa.gov.au/GISDownloads/trails.zip"},
            )
        return httpx.Response(200, headers={"Content-Encoding": "  "}, content=b"ok")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with _http.stream_provider_get(
            client,
            "https://www.waterconnect.sa.gov.au/Content/Downloads/DEWNR/trails.zip",
            params=None,
            user_agent="gridforge-test",
            deadline=time.monotonic() + 5.0,
            allowed_redirect_host_suffixes=("waterconnect.sa.gov.au",),
        ) as resp:
            body = b"".join(_http.bounded_response_chunks(resp, max_bytes=64, deadline=time.monotonic() + 5.0))
    assert body == b"ok"
    assert len(calls) == 2


def test_bounded_response_chunks_rejects_gzip_encoding():
    resp = httpx.Response(200, headers={"Content-Encoding": "gzip"}, stream=FixtureStream([b"compressed"]))
    with pytest.raises(ValueError, match="identity"):
        list(_http.bounded_response_chunks(resp, max_bytes=1024, deadline=time.monotonic() + 5.0))
