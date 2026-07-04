"""Full WMM2025 magnetic declination + AUTO-by-default behaviour."""
import math

from vanchor.app import Runtime
from vanchor.core.config import load
from vanchor.nav import wmm


def test_wmm_matches_known_declinations():
    # Reference values (deg East) from the WMM2025 model at 2026.5.
    refs = {(59.33, 18.07): 7.71, (0.0, 0.0): -3.83, (45.0, -100.0): 4.85,
            (37.77, -122.42): 12.86, (64.0, -21.0): -10.60}
    for (lat, lon), exp in refs.items():
        assert abs(wmm.declination_deg(lat, lon, year=2026.5) - exp) < 0.1


def test_year_is_clamped_to_model_validity():
    # A stale/absurd clock can't push the model out of range -> no raise.
    assert math.isfinite(wmm.declination_deg(59.0, 18.0, year=1900.0))
    assert math.isfinite(wmm.declination_deg(59.0, 18.0, year=2999.0))


def test_default_year_uses_today():
    # No explicit year -> uses today's decimal year, still finite + reasonable.
    d = wmm.declination_deg(59.33, 18.07)
    assert 5.0 < d < 10.0  # Stockholm is ~+7-8 deg E this decade


def test_fallback_is_used_when_pygeomag_missing(monkeypatch):
    # Force the pygeomag import to fail -> the low-degree fallback answers.
    import builtins
    real_import = builtins.__import__

    def _no_pygeomag(name, *a, **k):
        if name == "pygeomag":
            raise ImportError("simulated missing pygeomag")
        return real_import(name, *a, **k)

    monkeypatch.setattr(wmm, "_geomag", None)
    monkeypatch.setattr(builtins, "__import__", _no_pygeomag)
    d = wmm.declination_deg(59.33, 18.07)
    assert math.isfinite(d)  # fallback gives a coarse but finite value
    assert 2.0 < d < 12.0    # right ballpark for Stockholm


# --- AUTO-by-default wiring ------------------------------------------------- #
def test_config_default_declination_is_auto():
    assert load(None).sensors.magnetic_declination_deg is None  # None == AUTO


def test_sim_compass_is_forced_to_zero_declination():
    # The simulator is a zero-declination true-heading world.
    rt = Runtime(load(None))
    assert rt.navigator.declination_deg == 0.0


def test_real_compass_gets_auto_declination():
    cfg = load(None)
    cfg.hardware.compass_source = "serial"
    rt = Runtime(cfg)
    assert rt.navigator.declination_deg is None  # AUTO (full WMM at the fix)


def test_manual_override_still_wins():
    cfg = load(None)
    cfg.sensors.magnetic_declination_deg = 3.5
    rt = Runtime(cfg)  # even with a sim compass, an explicit value overrides
    assert rt.navigator.declination_deg == 3.5
