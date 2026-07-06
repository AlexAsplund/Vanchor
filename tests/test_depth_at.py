"""depth_at: best-known depth at a point, for the map long-press menu."""
from vanchor.core.models import GeoPoint
from vanchor.nav.depth import ColumnarFeatures, DepthMap


def _dm():
    dm = DepthMap()
    dm.record(GeoPoint(59.8780, 12.0300), 4.2)
    dm.record(GeoPoint(59.8790, 12.0310), 6.8)
    return dm


def test_depth_at_uses_sounding_where_no_contours():
    hit = _dm().depth_at(59.8781, 12.0301)      # store has no contours here
    assert hit is not None and hit["source"] == "sounding"
    assert hit["depth_m"] == 4.2 and hit["dist_m"] < 20


def test_depth_at_none_when_out_of_range():
    assert _dm().depth_at(59.90, 12.10) is None      # kilometres away


def test_depth_at_falls_back_to_contours():
    dm = DepthMap()                                   # no soundings at all
    dm.contours = ColumnarFeatures.from_arrays(
        coords=[[59.8780, 12.0300], [59.8785, 12.0305]],
        offsets=[0, 2], vals=[3.0], val_key="d", vtx_key="pts")
    hit = dm.depth_at(59.8781, 12.0301)
    assert hit is not None and hit["source"] == "contour" and hit["depth_m"] == 3.0


def test_depth_at_contour_always_beats_sounding():
    dm = _dm()                                   # sounding 4.2 only ~14 m away
    dm.contours = ColumnarFeatures.from_arrays(
        coords=[[59.8785, 12.0305]], offsets=[0, 1], vals=[9.9],
        val_key="d", vtx_key="pts")              # contour farther away than the sounding
    hit = dm.depth_at(59.8781, 12.0301)
    assert hit is not None and hit["source"] == "contour"   # contours are authoritative
    assert hit["depth_m"] == 9.9
