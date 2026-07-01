"""HWT901B compass driver: emits HDM, auto-learns declination (+ mount offset)
from GPS course, exposes a device menu, and self-registers as a compass source.
Uses a fake sensor -- no hwt901b library and no serial port required.
"""

from __future__ import annotations

from vanchor.core.geo import angle_difference
from vanchor.hardware import registry
from vanchor.hardware.drivers import load_drivers
from vanchor.hardware.drivers.hwt901b import HeadingOffsetEstimator, HWT901BCompass
from vanchor.nav import nmea


class _FakeSensor:
    """Stands in for hwt901b.HWT901B: returns a fixed magnetic heading."""

    def __init__(self, magnetic_deg: float) -> None:
        self.magnetic_deg = magnetic_deg
        self.closed = False

    def read_true_heading(self, declination_deg: float = 0.0, timeout: float = 1.0) -> float:
        return self.magnetic_deg  # declination applied by the driver, not here

    def close(self) -> None:
        self.closed = True


# ---- offset estimator (auto-declination) --------------------------------- #
def test_offset_estimator_converges_from_course():
    est = HeadingOffsetEstimator(time_constant_s=1.0)
    # Magnetic heading reads 90 but the GPS course is 100 -> offset -> +10.
    for _ in range(300):
        est.update(90.0, 100.0, 2.0, 0.2)
    assert est.settled and abs(angle_difference(est.offset_deg, 10.0)) < 1.0


def test_offset_estimator_ignores_stationary_and_turns():
    est = HeadingOffsetEstimator(time_constant_s=1.0, min_sog_mps=0.8, max_turn_dps=8.0)
    est.update(90.0, 200.0, 0.1, 0.2)   # too slow -> ignored
    assert est.offset_deg == 0.0 and not est.settled
    est.update(90.0, 100.0, 2.0, 0.2)   # first straight sample (seeds prev_cog)
    est.update(90.0, 170.0, 2.0, 0.2)   # 70deg COG jump in 0.2s -> mid-turn, ignored
    assert abs(est.offset_deg) < 5.0    # the turn sample didn't yank it


# ---- driver: HDM emission + declination modes ---------------------------- #
async def test_sample_once_off_mode_is_raw_magnetic():
    d = HWT901BCompass(_FakeSensor(123.0), bus=None, declination_mode="off")
    assert await d.sample_once(0.2) == nmea.encode_hdm(123.0)


async def test_sample_once_manual_declination():
    d = HWT901BCompass(_FakeSensor(100.0), bus=None,
                       declination_mode="manual", manual_declination_deg=5.0)
    assert await d.sample_once(0.2) == nmea.encode_hdm(105.0)


async def test_sample_once_auto_declination_from_motion():
    d = HWT901BCompass(_FakeSensor(90.0), bus=None, declination_mode="auto",
                       motion_provider=lambda: (100.0, 2.0))
    d.estimator.time_constant_s = 1.0
    for _ in range(300):
        await d.sample_once(0.2)
    # corrected heading converges to the true course (~100).
    assert abs(angle_difference(d.last_heading_deg, 100.0)) < 1.5


# ---- device menu + actions ----------------------------------------------- #
def test_device_menu_shape_and_actions():
    d = HWT901BCompass(_FakeSensor(50.0), bus=None)
    menu = d.device_menu()
    assert menu["device"] == "compass"
    assert {a["name"] for a in menu["actions"]} >= {"profile", "calibrate_mag"}
    assert d.apply_setting("declination_mode", "manual")["ok"]
    assert d.declination_mode == "manual"
    assert d.apply_setting("bogus", 1)["ok"] is False
    prof = d.run_action("profile")
    assert prof["ok"] and "offset_deg" in prof["status"]
    assert d.run_action("nope")["ok"] is False


# ---- registry self-registration ------------------------------------------ #
def test_hwt901b_registers_as_a_compass_source():
    load_drivers()
    assert registry.has("compass", "hwt901b")
    assert "hwt901b" in registry.sources("compass")


# ---- runtime dispatch: device_menu collection + setting/action endpoints -- #
def test_runtime_dispatches_device_menu_settings_and_actions(tmp_path):
    from vanchor.app import Runtime
    from vanchor.core.config import load

    cfg = load(None)
    cfg.data_dir = str(tmp_path)      # isolate from the repo's vanchor_data/
    rt = Runtime(cfg)
    rt.compass = HWT901BCompass(_FakeSensor(90.0))   # a device that exposes a menu

    menus = rt._device_menus()
    assert any(m.get("device") == "compass" for m in menus)

    assert rt.apply_device_setting("compass", "declination_mode", "manual")["ok"]
    assert rt.compass.declination_mode == "manual"
    assert rt.run_device_action("compass", "profile")["ok"] is True
    # a device with no menu (the sim GPS) degrades gracefully, not a crash:
    assert rt.apply_device_setting("gps", "whatever", 1)["ok"] is False
    assert rt.run_device_action("depth", "nope")["ok"] is False
