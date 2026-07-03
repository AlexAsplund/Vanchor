"""Tests for the external hardware watchdog heartbeat (roadmap #44).

The watchdog toggles a GPIO line that the ~1 Hz supervisor must keep petting; if
the supervisor stalls the line stops toggling and an external relay drops the
motor supply. These tests drive it through the injectable :class:`FakeGpio` so
the whole thing is exercised with no real hardware. (The real RPi.GPIO backend
is lazy-imported + untested on hardware, as documented on the module.)
"""

from __future__ import annotations

from vanchor.app import Runtime
from vanchor.core.config import WatchdogConfig, load
from vanchor.hardware.watchdog import FakeGpio, HardwareWatchdog


class _Clock:
    """A manually-advanced monotonic clock seam."""

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def _wd(**kw) -> tuple[HardwareWatchdog, FakeGpio, _Clock]:
    clk = _Clock()
    gpio = FakeGpio()
    wd = HardwareWatchdog(
        kw.pop("pin", 17),
        interval_s=kw.pop("interval_s", 1.0),
        enabled=kw.pop("enabled", True),
        active_low=kw.pop("active_low", False),
        gpio=gpio,
        now_fn=clk,
    )
    return wd, gpio, clk


# --------------------------------------------------------------------------- #
# Heartbeat toggles while healthy
# --------------------------------------------------------------------------- #
def test_heartbeat_toggles_while_pumped():
    wd, gpio, clk = _wd(interval_s=1.0)
    wd.start()
    assert gpio.mode[17] == "out"  # pin configured as output
    assert wd.toggles == 0

    levels = []
    for _ in range(4):
        clk.advance(1.0)
        levels.append(wd.pump())
    # Four heartbeat edges, alternating logical level 1,0,1,0.
    assert wd.toggles == 4
    assert levels == [1, 0, 1, 0]
    # The fake GPIO saw the same alternating physical writes.
    assert [v for (_pin, v) in gpio.writes] == [1, 0, 1, 0]


def test_pump_respects_interval():
    wd, _gpio, clk = _wd(interval_s=1.0)
    wd.start()
    # A pump BEFORE the interval elapses must not toggle.
    clk.advance(0.4)
    assert wd.pump() == 0
    assert wd.toggles == 0
    # Crossing the interval toggles once.
    clk.advance(0.7)  # total 1.1s
    assert wd.pump() == 1
    assert wd.toggles == 1


# --------------------------------------------------------------------------- #
# Stops toggling when the supervisor is not pumped (the whole point)
# --------------------------------------------------------------------------- #
def test_heartbeat_freezes_when_not_pumped():
    wd, gpio, clk = _wd(interval_s=1.0)
    wd.start()
    for _ in range(2):
        clk.advance(1.0)
        wd.pump()
    assert wd.toggles == 2
    frozen_level = wd.level
    n_writes = len(gpio.writes)

    # Supervisor stalls: time marches on but pump() is never called again.
    clk.advance(60.0)
    # No further edges, line held -> the external relay would time out and drop.
    assert wd.toggles == 2
    assert wd.level == frozen_level
    assert len(gpio.writes) == n_writes


# --------------------------------------------------------------------------- #
# Disabled watchdog is a total no-op (safe on a dev box)
# --------------------------------------------------------------------------- #
def test_disabled_watchdog_is_noop():
    wd, gpio, clk = _wd(enabled=False)
    wd.start()
    clk.advance(5.0)
    assert wd.pump() == 0
    assert wd.toggles == 0
    assert gpio.writes == []
    assert gpio.mode == {}  # never configured a pin
    assert not wd.started


def test_disabled_watchdog_never_builds_real_backend():
    # No injected backend + disabled -> start() must not try to import RPi.GPIO.
    wd = HardwareWatchdog(17, enabled=False, gpio=None)
    wd.start()  # would raise if it tried to build the real backend
    assert wd.pump() == 0


# --------------------------------------------------------------------------- #
# active_low inverts the electrical level (logical heartbeat unchanged)
# --------------------------------------------------------------------------- #
def test_active_low_inverts_physical_level():
    wd, gpio, clk = _wd(interval_s=1.0, active_low=True)
    wd.start()
    # Initial physical level is the inverse of logical 0 -> HIGH.
    assert gpio.levels[17] == 1
    clk.advance(1.0)
    assert wd.pump() == 1  # logical level 1
    assert gpio.levels[17] == 0  # ...written as physical LOW (inverted)


# --------------------------------------------------------------------------- #
# stop() de-asserts + releases the pin, idempotently
# --------------------------------------------------------------------------- #
def test_stop_deasserts_and_cleans_up():
    wd, gpio, clk = _wd(interval_s=1.0)
    wd.start()
    clk.advance(1.0)
    wd.pump()  # level now 1
    wd.stop()
    assert wd.level == 0
    assert gpio.levels[17] == 0
    assert gpio.cleaned == [17]
    assert not wd.started
    # Idempotent: a second stop is harmless.
    wd.stop()
    assert gpio.cleaned == [17]


# --------------------------------------------------------------------------- #
# from_config wiring
# --------------------------------------------------------------------------- #
def test_from_config_builds_disabled_by_default():
    wd = HardwareWatchdog.from_config(WatchdogConfig(), gpio=FakeGpio())
    assert wd.enabled is False
    assert wd.pin == 17


def test_from_config_enabled():
    cfg = WatchdogConfig(enabled=True, gpio_pin=23, interval_s=0.5, active_low=True)
    wd = HardwareWatchdog.from_config(cfg, gpio=FakeGpio())
    assert wd.enabled is True
    assert wd.pin == 23
    assert wd.interval_s == 0.5
    assert wd.active_low is True


# --------------------------------------------------------------------------- #
# Supervisor wiring: the ~1 Hz supervisor pass pets the watchdog
# --------------------------------------------------------------------------- #
def test_supervisor_pumps_watchdog(tmp_path):
    cfg = load(None)
    cfg.data_dir = str(tmp_path)
    rt = Runtime(cfg)
    # Swap in an enabled, injectable watchdog on a manual clock, then run the
    # supervisor's one-shot pass a few times.
    clk = _Clock()
    gpio = FakeGpio()
    rt.watchdog = HardwareWatchdog(17, interval_s=1.0, enabled=True, gpio=gpio, now_fn=clk)
    rt.watchdog.start()
    for _ in range(3):
        clk.advance(1.0)
        rt._supervise_once()
    assert rt.watchdog.toggles == 3


def test_default_runtime_watchdog_disabled(tmp_path):
    cfg = load(None)
    cfg.data_dir = str(tmp_path)
    rt = Runtime(cfg)
    # Default config -> watchdog present but disabled; supervising is a no-op on it.
    assert rt.watchdog.enabled is False
    rt._supervise_once()
    assert rt.watchdog.toggles == 0
