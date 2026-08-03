"""USGS 3DEP inland-bathymetry basemap — unit tests.

Two layers of coverage for feat/usgs-inland-bathymetry-basemap:

1. Behavioural — a Node harness (usgs_bathymetry_harness.mjs) loads the REAL
   map-core.js and offline.js in a vm sandbox and reports the URLs/values the
   shipped code actually produces. We assert the WMS bbox maths, the
   layer/style wiring, the one-cache-key invariant, and the offline template
   resolution against that report.
2. Static — node --check on the two modified files + content assertions that
   the exports and the layer/style bug fix are present (mirrors the repo's
   existing test_t5_map_daylight.py convention).
"""
import json
import math
import subprocess
import pathlib
from urllib.parse import urlparse, parse_qs

import pytest

ROOT = pathlib.Path(__file__).parent.parent
STATIC = ROOT / "src/vanchor/ui/static"
MAP_CORE = STATIC / "map-core.js"
OFFLINE = STATIC / "offline.js"
HARNESS = pathlib.Path(__file__).parent / "usgs_bathymetry_harness.mjs"

MERC_R = 20037508.342789244


def _txt(p):
    return p.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def report():
    """Run the Node harness once and hand every test the parsed report."""
    r = subprocess.run(
        ["node", str(HARNESS), str(STATIC)],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, f"harness failed:\n{r.stderr}"
    return json.loads(r.stdout)


# ---------------------------------------------------------------- static checks
def test_node_check_map_core():
    r = subprocess.run(["node", "--check", str(MAP_CORE)], capture_output=True)
    assert r.returncode == 0, r.stderr.decode()


def test_node_check_offline():
    r = subprocess.run(["node", "--check", str(OFFLINE)], capture_output=True)
    assert r.returncode == 0, r.stderr.decode()


def test_basemap_registered():
    txt = _txt(MAP_CORE)
    assert '"USGS Topobathy (US)"' in txt


def test_layer_style_not_swapped():
    """Regression guard: the render name goes in LAYERS, STYLES stays 'default'.

    The earlier draft had these backwards (LAYERS='3DEPElevation:None',
    STYLES=<render>), which the live WMS rejects with a ServiceException.
    """
    txt = _txt(MAP_CORE)
    assert 'USGS_3DEP_LAYER = "3DEPElevation:Hillshade Elevation Tinted"' in txt
    assert 'USGS_3DEP_STYLE = "default"' in txt
    # the broken combo must be gone
    assert "3DEPElevation:None" not in txt


def test_offline_tileurl_accepts_function():
    txt = _txt(OFFLINE)
    assert 'if (typeof template === "function") return template(z, x, y);' in txt


def test_offline_exposes_tile_maths():
    txt = _txt(OFFLINE)
    assert "VA._offline = { enumerateTiles, countTiles, tileUrl };" in txt


# ------------------------------------------------------------- behavioural (harness)
def test_basemap_present(report):
    assert report["hasBasemap"] is True


def test_template_is_function(report):
    """WMS is bbox-based, so its offline template must be a (z,x,y)=>url fn."""
    assert report["templateIsFunction"] is True


def test_native_max_zoom(report):
    """3DEP DEM is ~1 m/px; upscale past z16 rather than fetch empty tiles."""
    assert report["baseNativeMax"] == 16


def test_usgs_endpoint_config(report):
    cfg = report["usgs3dep"]
    assert cfg["endpoint"].endswith("/3DEPElevation/ImageServer/WMSServer")
    assert cfg["layer"] == "3DEPElevation:Hillshade Elevation Tinted"
    assert cfg["style"] == "default"


def test_one_cache_key_invariant(report):
    """The live layer's getTileUrl, the offline template, the offline tileUrl()
    resolver, and the raw wmsTileUrl builder must all emit the SAME url for a
    given tile — otherwise a pre-cached tile and a live-panned tile miss each
    other in the IndexedDB cache."""
    urls = {report["wmsUrl"], report["liveUrl"], report["offlineTmplUrl"], report["fnResolved"]}
    assert len(urls) == 1, f"cache-key divergence: {urls}"


def test_wms_query_params(report):
    q = parse_qs(urlparse(report["wmsUrl"]).query)
    assert q["SERVICE"] == ["WMS"]
    assert q["REQUEST"] == ["GetMap"]
    assert q["VERSION"] == ["1.3.0"]
    assert q["CRS"] == ["EPSG:3857"]
    assert q["WIDTH"] == ["256"] and q["HEIGHT"] == ["256"]
    assert q["FORMAT"] == ["image/png"]
    # the fix: render name in LAYERS, default style in STYLES
    assert q["LAYERS"] == ["3DEPElevation:Hillshade Elevation Tinted"]
    assert q["STYLES"] == ["default"]


def test_wms_bbox_matches_web_mercator_tile(report):
    """Independently recompute the EPSG:3857 bbox for tile z12/x1160/y1512 and
    assert the code's BBOX matches (easting,northing axis order for 1.3.0)."""
    z, x, y = 12, 1160, 1512
    span = (2 * MERC_R) / (2 ** z)
    exp_min_x = -MERC_R + x * span
    exp_max_x = exp_min_x + span
    exp_max_y = MERC_R - y * span
    exp_min_y = exp_max_y - span

    q = parse_qs(urlparse(report["wmsUrl"]).query)
    got = [float(v) for v in q["BBOX"][0].split(",")]
    expected = [exp_min_x, exp_min_y, exp_max_x, exp_max_y]
    for g, e in zip(got, expected):
        assert math.isclose(g, e, rel_tol=0, abs_tol=1e-6), f"bbox {got} != {expected}"


def test_string_template_still_works(report):
    """The function-template branch must not regress the plain XYZ string path."""
    assert report["stringResolved"] == "https://a.basemaps.cartocdn.com/dark_all/5/3/7.png"


def test_enumerate_matches_count(report):
    """enumerateTiles() length and countTiles() must agree for the same bbox."""
    assert report["enumerated"] == report["counted"]
    assert report["counted"] > 0
