"""Tests for live sonar/fishfinder ingest merged with the chart (#45)."""

from __future__ import annotations

from vanchor.core.models import GeoPoint, GpsFix
from vanchor.core.state import NavigationState
from vanchor.nav import nmea, sonar


# --------------------------------------------------------------------------- #
# A tiny fake DepthMap: only the attributes charted_depth_at duck-types on.
# --------------------------------------------------------------------------- #
class FakeChart:
    def __init__(self, points=None, contours=None):
        # points: list of (lat, lon, depth); contours: list of {d, pts}
        self.points = points or []
        self.contours = contours or []

    def contours_in(self, bbox=None, limit=200):
        return list(self.contours)


# --------------------------------------------------------------------------- #
# Parsing: raw feed -> Sounding
# --------------------------------------------------------------------------- #
def test_dbt_sentence_parses_to_sounding():
    s = sonar.sounding_from_sentence(nmea._wrap("SDDBT,26.6,f,8.1,M,4.4,F"))
    assert s is not None
    assert s.depth_m == 8.1
    assert s.source == "DBT"
    assert s.below == "transducer"


def test_dbs_sentence_parses_to_sounding():
    # DBS is the newly-added below-surface sentence.
    s = sonar.sounding_from_sentence(nmea._wrap("SDDBS,26.6,f,8.1,M,4.4,F"))
    assert s is not None
    assert s.depth_m == 8.1
    assert s.below == "surface"


def test_dpt_sentence_parses_to_sounding():
    s = sonar.sounding_from_sentence(nmea.encode_dpt(4.2))
    assert s is not None
    assert s.depth_m == 4.2
    assert s.source == "DPT"


def test_non_depth_sentence_yields_none():
    assert sonar.sounding_from_sentence(nmea.encode_hdm(90.0)) is None


def test_garbage_sentence_yields_none():
    assert sonar.sounding_from_sentence("not a sentence") is None


def test_nmea2000_dict_payload_parses():
    s = sonar.sounding_from_payload({"depth": 3.4})
    assert s is not None
    assert s.depth_m == 3.4
    assert s.source == "n2k"


def test_nmea2000_pgn128267_payload_with_offset():
    # PGN 128267 "Water Depth": Depth below transducer + Offset to the surface.
    s = sonar.sounding_from_payload(
        {"pgn": 128267, "fields": {"Depth": 3.2, "Offset": 0.3}}
    )
    assert s is not None
    assert s.depth_m == 3.5


def test_deeper_style_json_string_payload_parses():
    s = sonar.sounding_from_payload('{"depth_m": 5.5}')
    assert s is not None
    assert s.depth_m == 5.5


def test_payload_without_depth_yields_none():
    assert sonar.sounding_from_payload({"battery": 90}) is None
    assert sonar.sounding_from_payload("not json") is None


# --------------------------------------------------------------------------- #
# Chart lookup
# --------------------------------------------------------------------------- #
def test_charted_depth_from_nearest_sounding():
    chart = FakeChart(points=[(59.0, 18.0, 10.0), (59.001, 18.001, 12.0)])
    d = sonar.charted_depth_at(chart, GeoPoint(59.0, 18.0), radius_m=25.0)
    assert d == 10.0


def test_charted_depth_none_when_nothing_nearby():
    chart = FakeChart(points=[(60.0, 19.0, 10.0)])
    assert sonar.charted_depth_at(chart, GeoPoint(59.0, 18.0), radius_m=25.0) is None


def test_charted_depth_from_contour_vertex():
    chart = FakeChart(contours=[{"d": 8.0, "pts": [[59.0, 18.0], [59.0, 18.0005]]}])
    d = sonar.charted_depth_at(chart, GeoPoint(59.0, 18.0), radius_m=25.0)
    assert d == 8.0


# --------------------------------------------------------------------------- #
# Divergence comparison
# --------------------------------------------------------------------------- #
def test_measured_equals_chart_no_alert():
    div = sonar.divergence(10.0, 10.0)
    assert div.alert is False
    assert div.delta_m == 0.0


def test_measured_shallower_beyond_tolerance_alerts():
    div = sonar.divergence(4.0, 10.0)  # 6 m shallower than the chart
    assert div.alert is True
    assert div.delta_m < 0.0


def test_measured_within_tolerance_no_alert():
    # 10.5 vs 10.0: within max(1.0, 0.15*10) = 1.5 m tolerance.
    div = sonar.divergence(9.2, 10.0)
    assert div.alert is False


def test_measured_deeper_than_chart_no_alert_by_default():
    # Deeper than charted is not a grounding risk -> no alert (shallow_only).
    div = sonar.divergence(20.0, 10.0)
    assert div.alert is False


def test_no_charted_depth_no_alert():
    div = sonar.divergence(2.0, None)
    assert div.alert is False


def test_zero_measured_never_alerts():
    # A 0 reading = lost bottom lock, must not false-trip the shoal alarm.
    div = sonar.divergence(0.0, 10.0)
    assert div.alert is False


# --------------------------------------------------------------------------- #
# The pipeline: ingest writes the state field
# --------------------------------------------------------------------------- #
def _state_at(lat, lon):
    st = NavigationState()
    st.fix = GpsFix(point=GeoPoint(lat, lon))
    return st


def test_ingest_updates_state_measured_depth():
    st = _state_at(59.0, 18.0)
    chart = FakeChart(points=[(59.0, 18.0, 10.0)])
    sounding = sonar.sounding_from_payload({"depth": 9.8})
    sonar.ingest(st, sounding, chart)
    assert st.sonar_depth_m == 9.8
    assert st.charted_depth_m == 10.0
    assert st.depth_divergence_alert is False


def test_ingest_raises_divergence_alert_on_shoal():
    st = _state_at(59.0, 18.0)
    chart = FakeChart(points=[(59.0, 18.0, 12.0)])
    sounding = sonar.sounding_from_sentence(nmea.encode_dpt(3.0))
    div = sonar.ingest(st, sounding, chart)
    assert div.alert is True
    assert st.depth_divergence_alert is True
    assert st.sonar_depth_m == 3.0
    assert st.depth_divergence_m < 0.0
    # And it surfaces through telemetry.
    tele = st.to_dict()
    assert tele["sonar"]["divergence_alert"] is True
    assert tele["sonar"]["depth_m"] == 3.0


def test_ingest_none_sounding_is_noop():
    st = _state_at(59.0, 18.0)
    st.depth_divergence_alert = True  # pre-existing
    chart = FakeChart(points=[(59.0, 18.0, 10.0)])
    div = sonar.ingest(st, None, chart)
    assert div.alert is False
    assert st.depth_divergence_alert is True  # untouched


def test_ingest_uses_explicit_position_over_state():
    st = _state_at(0.0, 0.0)  # fix far from the chart
    chart = FakeChart(points=[(59.0, 18.0, 12.0)])
    sounding = sonar.sounding_from_payload({"depth": 3.0})
    div = sonar.ingest(st, sounding, chart, position=GeoPoint(59.0, 18.0))
    assert div.alert is True


# --------------------------------------------------------------------------- #
# Runtime wiring (#45): the dead-code path is now driven by the running app.
# --------------------------------------------------------------------------- #
def test_runtime_wires_grounding_divergence_alert(tmp_path):
    # The Runtime computes charted-vs-sounded divergence from its DepthMap and
    # sets the state fields so the grounding alert can fire in telemetry. This
    # exercises the previously-dead nav/sonar.py path through app.py.
    from vanchor.app import Runtime
    from vanchor.core.config import load

    cfg = load(None)
    cfg.data_dir = str(tmp_path)
    rt = Runtime(cfg)

    pos = GeoPoint(59.0, 18.0)
    # Chart says 20 m here but the sounder reads 2 m -> materially shallower than
    # the chart = grounding-risk alert.
    rt.depth_map.points = [(59.0, 18.0, 20.0)]
    rt.state.depth_m = 2.0
    rt._update_depth_divergence(pos)
    assert rt.state.depth_divergence_alert is True
    assert rt.state.sonar_depth_m == 2.0
    assert rt.state.charted_depth_m == 20.0
    assert rt.state.depth_divergence_m < 0.0

    # Sounder agrees with the chart -> no alert.
    rt.state.depth_m = 20.0
    rt._update_depth_divergence(pos)
    assert rt.state.depth_divergence_alert is False


def test_runtime_divergence_noop_without_depth(tmp_path):
    # No live depth (lost bottom lock -> 0.0) leaves any prior alert untouched.
    from vanchor.app import Runtime
    from vanchor.core.config import load

    cfg = load(None)
    cfg.data_dir = str(tmp_path)
    rt = Runtime(cfg)
    rt.depth_map.points = [(59.0, 18.0, 20.0)]
    rt.state.depth_divergence_alert = True  # pre-existing
    rt.state.depth_m = 0.0  # no bottom lock
    rt._update_depth_divergence(GeoPoint(59.0, 18.0))
    assert rt.state.depth_divergence_alert is True  # untouched
