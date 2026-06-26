"""Tests for offline chart prefetch + management (#52) and survey/cone API (#47).

The prefetch path is exercised offline: the network fetch is monkeypatched so no
test ever hits Overpass.
"""

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from shapely.geometry import Polygon

from vanchor.app import Runtime
from vanchor.core.config import AppConfig
from vanchor.nav import water
from vanchor.ui.server import create_app


@pytest.fixture()
def runtime(tmp_path):
    cfg = AppConfig()
    cfg.data_dir = str(tmp_path)
    return Runtime(cfg)


@pytest.fixture()
def client(runtime):
    with TestClient(create_app(runtime)) as c:
        yield c


# A simple square water polygon (lon/lat) used to stand in for an Overpass fetch.
_FAKE_ELEMENTS = [
    {
        "type": "way",
        "geometry": [
            {"lon": 13.30, "lat": 59.65},
            {"lon": 13.40, "lat": 59.65},
            {"lon": 13.40, "lat": 59.70},
            {"lon": 13.30, "lat": 59.70},
            {"lon": 13.30, "lat": 59.65},
        ],
    }
]

_BBOX = [59.64, 13.29, 59.71, 13.41]  # south, west, north, east


def test_prefetch_caches_offline(client):
    with patch.object(water, "fetch_overpass", return_value=_FAKE_ELEMENTS) as m:
        body = client.post("/api/route/prefetch", json={"bbox": _BBOX}).json()
    assert m.called
    assert body["ok"] is True
    assert body["cached"] is True
    assert body["vertices"] > 0

    # A second prefetch of the same area is served from cache (no fetch).
    with patch.object(water, "fetch_overpass") as m2:
        body2 = client.post("/api/route/prefetch", json={"bbox": _BBOX}).json()
    assert not m2.called
    assert body2["ok"] and body2["cached"]
    assert "already" in body2["message"].lower()


def test_prefetch_handles_network_failure(client):
    with patch.object(water, "fetch_overpass", side_effect=RuntimeError("no net")):
        body = client.post("/api/route/prefetch", json={"bbox": _BBOX}).json()
    assert body["ok"] is False
    assert body["cached"] is False
    assert body["vertices"] == 0
    assert "offline" in body["message"].lower() or "download" in body["message"].lower()


def test_prefetch_empty_water(client):
    with patch.object(water, "fetch_overpass", return_value=[]):
        body = client.post("/api/route/prefetch", json={"bbox": _BBOX}).json()
    assert body["ok"] is False
    assert "no mapped water" in body["message"].lower()


def test_prefetch_bad_bbox(client):
    body = client.post("/api/route/prefetch", json={"bbox": [1, 2, 3]}).json()
    assert body["ok"] is False
    assert "bbox" in body["message"].lower()


def test_charts_list_and_clear(client):
    # Empty to start.
    assert client.get("/api/route/charts").json()["charts"] == []

    with patch.object(water, "fetch_overpass", return_value=_FAKE_ELEMENTS):
        client.post("/api/route/prefetch", json={"bbox": _BBOX})

    charts = client.get("/api/route/charts").json()["charts"]
    assert len(charts) == 1
    chart = charts[0]
    assert chart["bbox"] == _BBOX
    assert chart["vertices"] > 0
    assert chart["size_bytes"] > 0

    cleared = client.post("/api/route/charts/clear").json()
    assert cleared["ok"] and cleared["removed"] == 1
    assert client.get("/api/route/charts").json()["charts"] == []


def test_survey_endpoint(client):
    # 100 m x 50 m rectangle near the sim start, 10 m spacing.
    proj = water.Projection.for_point(13.32, 59.66)
    x0, y0 = proj.point_to_metric(13.32, 59.66)
    corners = [(x0, y0), (x0 + 100, y0), (x0 + 100, y0 + 50), (x0, y0 + 50)]
    polygon = []
    for x, y in corners:
        lon, lat = proj.point_to_lonlat(x, y)
        polygon.append([lat, lon])

    body = client.post(
        "/api/route/survey", json={"polygon": polygon, "spacing_m": 10.0}
    ).json()
    assert body["ok"] is True
    assert len(body["waypoints"]) >= 2
    assert body["waypoints"][-1]["name"] == "DEST"
    poly_m = Polygon([proj.point_to_metric(lon, lat) for lat, lon in polygon])
    nav = poly_m.buffer(1.0)
    from shapely.geometry import Point

    for wp in body["waypoints"]:
        x, y = proj.point_to_metric(wp["lon"], wp["lat"])
        assert nav.covers(Point(x, y))


def test_survey_endpoint_bad_polygon(client):
    body = client.post(
        "/api/route/survey", json={"polygon": [[59.66, 13.32]], "spacing_m": 10.0}
    ).json()
    assert body["ok"] is False
    assert body["waypoints"] == []


def test_survey_endpoint_missing_spacing(client):
    body = client.post(
        "/api/route/survey",
        json={"polygon": [[59.66, 13.32], [59.67, 13.32], [59.67, 13.33]]},
    ).json()
    assert body["ok"] is False
    assert "spacing" in body["message"].lower()


def test_boat_profile_exposes_sonar_cone(client):
    body = client.get("/api/boat").json()
    assert body["sonar_cone_deg"] == 20.0


def test_boat_profile_accepts_sonar_cone(client):
    body = client.post("/api/boat", json={"sonar_cone_deg": 12.5}).json()
    assert body["sonar_cone_deg"] == 12.5
    # And it appears in telemetry's boat block.
    tele = client.get("/api/state").json()
    assert tele["boat"]["sonar_cone_deg"] == 12.5
