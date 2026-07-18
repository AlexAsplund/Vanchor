"""Battery warn/alarm ladder + RTL-recommend fixes (UX Task 1, item 4 / A5).

Covers:
- ``evaluate_rtl_recommend`` zero-range matrix: a zero range estimate with a
  critically low pack (soc <= 10) must still recommend RTL ("unknown" range is
  not "infinite"); a healthy pack at zero range must not.
- ``auto_rtl`` gating is untouched: with ``auto_rtl`` off no
  ``_schedule_auto_rtl`` call ever happens (recommend-only).
- Server-side battery alert edges (warn <25%, crit <10%) in
  ``evaluate_push_alerts``: the alert log grows once per threshold crossing,
  not once per tick.

All runtimes are isolated to ``tmp_path`` (never the repo's vanchor_data/).
Runtime methods are called directly -- never via TestClient telemetry loops
(see project memory: TestClient(Runtime()) can spin on depth data).
"""
import pytest

from vanchor.app import Runtime
from vanchor.core.config import load
from vanchor.core.models import GeoPoint, GpsFix


def _runtime(tmp_path) -> Runtime:
    cfg = load(None)
    cfg.data_dir = str(tmp_path)
    return Runtime(cfg)


def _with_batt(rt: Runtime, soc, range_m=0.0) -> Runtime:
    rt.battery_snapshot = lambda: {"soc_pct": soc, "range_m": range_m}  # type: ignore[method-assign]
    return rt


# --------------------------------------------------------------------------- #
# evaluate_rtl_recommend: zero-range / low-soc matrix
# --------------------------------------------------------------------------- #

class TestRtlRecommendZeroRange:
    def _rt(self, tmp_path, soc, range_m=0.0):
        rt = _with_batt(_runtime(tmp_path), soc, range_m)
        rt.state.launch = GeoPoint(59.0, 18.0)
        # ~200 m north of the launch point.
        rt.state.fix = GpsFix(point=GeoPoint(59.0018, 18.0))
        return rt

    def test_zero_range_soc8_recommends(self, tmp_path):
        rt = self._rt(tmp_path, soc=8.0, range_m=0.0)
        assert rt.evaluate_rtl_recommend() is True
        assert rt.state.rtl_recommended is True

    def test_zero_range_soc80_does_not_recommend(self, tmp_path):
        rt = self._rt(tmp_path, soc=80.0, range_m=0.0)
        assert rt.evaluate_rtl_recommend() is False
        assert rt.state.rtl_recommended is False

    def test_zero_range_soc_exactly_10_recommends(self, tmp_path):
        rt = self._rt(tmp_path, soc=10.0, range_m=0.0)
        assert rt.evaluate_rtl_recommend() is True

    def test_zero_range_no_soc_does_not_recommend(self, tmp_path):
        rt = self._rt(tmp_path, soc=None, range_m=0.0)
        assert rt.evaluate_rtl_recommend() is False

    def test_positive_range_logic_unchanged(self, tmp_path):
        # Plenty of range vs 200 m home -> no recommendation.
        rt = self._rt(tmp_path, soc=80.0, range_m=5000.0)
        assert rt.evaluate_rtl_recommend() is False
        # Range barely covers the distance home -> recommend.
        rt2 = self._rt(tmp_path, soc=40.0, range_m=210.0)
        assert rt2.evaluate_rtl_recommend() is True

    def test_no_launch_point_never_recommends(self, tmp_path):
        rt = self._rt(tmp_path, soc=5.0, range_m=0.0)
        rt.state.launch = None
        assert rt.evaluate_rtl_recommend() is False


class TestAutoRtlGatingUntouched:
    """rtl_recommended stays a recommendation flag; self-driving stays opt-in."""

    def test_auto_rtl_off_never_schedules(self, tmp_path):
        rt = _with_batt(_runtime(tmp_path), soc=8.0, range_m=100.0)
        assert rt.config.safety.auto_rtl is False
        calls = []
        rt._schedule_auto_rtl = lambda: calls.append(True)  # type: ignore[method-assign]
        rt.state.launch = GeoPoint(59.0, 18.0)
        rt.state.fix = GpsFix(point=GeoPoint(59.0018, 18.0))  # 200 m out, range 100 m
        assert rt.evaluate_rtl_recommend() is True
        assert calls == []

    def test_zero_range_crit_soc_does_not_self_drive_even_with_auto_rtl(self, tmp_path):
        # The zero-range early return recommends WITHOUT engaging auto-RTL:
        # a zero estimate is not a plannable range.
        rt = _with_batt(_runtime(tmp_path), soc=8.0, range_m=0.0)
        rt.config.safety.auto_rtl = True
        calls = []
        rt._schedule_auto_rtl = lambda: calls.append(True)  # type: ignore[method-assign]
        rt.state.launch = GeoPoint(59.0, 18.0)
        rt.state.fix = GpsFix(point=GeoPoint(59.0018, 18.0))
        assert rt.evaluate_rtl_recommend() is True
        assert calls == []


# --------------------------------------------------------------------------- #
# evaluate_push_alerts: battery warn/crit edges -> alert log (once per crossing)
# --------------------------------------------------------------------------- #

class TestBatteryAlertEdges:
    def _batt_entries(self, rt):
        return [e for e in rt.alert_log.snapshot() if e.get("kind") == "battery"]

    def test_warn_edge_records_once(self, tmp_path):
        rt = _with_batt(_runtime(tmp_path), soc=30.0)
        rt.evaluate_push_alerts()
        assert self._batt_entries(rt) == []
        _with_batt(rt, soc=24.0)
        rt.evaluate_push_alerts()
        entries = self._batt_entries(rt)
        assert len(entries) == 1
        assert entries[0]["severity"] == "warn"
        assert "24" in entries[0]["message"]
        # Repeat ticks below the threshold: no growth.
        rt.evaluate_push_alerts()
        rt.evaluate_push_alerts()
        assert len(self._batt_entries(rt)) == 1

    def test_crit_edge_records_once(self, tmp_path):
        rt = _with_batt(_runtime(tmp_path), soc=24.0)
        rt.evaluate_push_alerts()
        assert len(self._batt_entries(rt)) == 1  # warn
        _with_batt(rt, soc=8.0)
        rt.evaluate_push_alerts()
        entries = self._batt_entries(rt)
        assert len(entries) == 2
        assert entries[1]["severity"] == "alarm"
        assert "8" in entries[1]["message"]
        rt.evaluate_push_alerts()
        assert len(self._batt_entries(rt)) == 2

    def test_recovery_rearms_edges(self, tmp_path):
        rt = _with_batt(_runtime(tmp_path), soc=8.0)
        rt.evaluate_push_alerts()
        assert len(self._batt_entries(rt)) == 1  # crit
        _with_batt(rt, soc=60.0)  # battery swap
        rt.evaluate_push_alerts()
        _with_batt(rt, soc=20.0)
        rt.evaluate_push_alerts()
        entries = self._batt_entries(rt)
        assert len(entries) == 2
        assert entries[1]["severity"] == "warn"

    def test_no_soc_reading_no_entries(self, tmp_path):
        rt = _with_batt(_runtime(tmp_path), soc=None)
        rt.evaluate_push_alerts()
        assert self._batt_entries(rt) == []


# --------------------------------------------------------------------------- #
# Battery ladder thresholds (Runtime hand-off; UI battLevel mirror is JS-side)
# --------------------------------------------------------------------------- #

class TestBatteryLadderHandoff:
    def test_at_5pct_recommend_rtl_when_auto_off(self, tmp_path):
        rt = _with_batt(_runtime(tmp_path), soc=5.0)
        rt.config.safety.auto_rtl = False
        rt.state.rtl_recommended = False
        rt._battery_rtl_handoff(5.0)
        assert rt.state.rtl_recommended is True

    def test_alert_log_attribute_present(self, tmp_path):
        rt = _runtime(tmp_path)
        assert hasattr(rt, "alert_log")
        rt.alert_log.record("warn", "low battery test")
        snap = rt.alert_log.snapshot()
        assert len(snap) == 1
        assert snap[0]["severity"] == "warn"
