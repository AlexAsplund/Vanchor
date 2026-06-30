"""Along-contour route: click a contour -> chained same-depth track."""
from vanchor.nav.contour_route import contour_route_near


def _c(d, pts):
    return {"d": d, "pts": pts}


def test_chains_same_depth_pieces():
    c = [
        _c(5.0, [[59.0, 18.0], [59.0, 18.001]]),
        _c(5.0, [[59.0, 18.001], [59.0, 18.002]]),   # shares an endpoint -> chains
        _c(9.0, [[59.01, 18.0], [59.01, 18.001]]),   # different depth -> ignored
    ]
    r = contour_route_near(59.0, 18.0005, c)
    assert r["ok"] and r["depth_m"] == 5.0 and r["loop"] is False
    assert len(r["waypoints"]) >= 2
    lons = [w["lon"] for w in r["waypoints"]]
    assert min(lons) < 18.0005 < max(lons)   # spans the chained line


def test_closed_contour_is_a_loop():
    ring = [[59.0, 18.0], [59.001, 18.0], [59.001, 18.001], [59.0, 18.001], [59.0, 18.0]]
    r = contour_route_near(59.0005, 18.0, [_c(3.0, ring)])
    assert r["ok"] and r["loop"] is True


def test_no_contour_near_click():
    r = contour_route_near(59.0, 18.0, [_c(5.0, [[59.5, 18.0], [59.5, 18.001]])])
    assert r["ok"] is False
