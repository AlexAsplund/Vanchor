"""External hardware watchdog heartbeat (roadmap #44).

A Raspberry Pi can hard-hang in a way the in-process ("firmware") watchdog
cannot catch: the event loop stops, no failsafe runs, and a trolling motor keeps
driving on its last command. This module drives an EXTERNAL hardware watchdog --
a GPIO line the ~1 Hz supervisor must keep TOGGLING. The line feeds a
retriggerable monostable / relay driver on the boat; each edge re-arms it. If
the supervisor stalls the toggling stops, the monostable times out, and the
relay drops -- cutting the motor supply independently of the (hung) software.

The design is deliberately I/O-light and injectable:

* :class:`HardwareWatchdog` holds the heartbeat state and toggles a pin through a
  :class:`GpioBackend`. It is OFF by default and a total no-op until enabled +
  started, so building it on a dev box (no GPIO) is free and safe.
* :class:`FakeGpio` is an in-memory backend the unit tests drive -- they assert
  the line toggles while healthy and freezes when the supervisor stops pumping.
* :class:`_RPiGpioBackend` is the real ``RPi.GPIO`` backend. It is lazy-imported
  (importing this module never needs ``RPi.GPIO``) and is **UNTESTED ON
  HARDWARE** -- bench validation only.

Wiring: build one via :meth:`HardwareWatchdog.from_config`, ``start()`` it on
boot, ``pump()`` it once per supervisor tick, and ``stop()`` it on shutdown.
"""

from __future__ import annotations

import logging
import time
from typing import Callable, Protocol, runtime_checkable

logger = logging.getLogger("vanchor.watchdog")


@runtime_checkable
class GpioBackend(Protocol):
    """The tiny GPIO surface the watchdog needs (one output pin)."""

    def setup_output(self, pin: int, initial: int) -> None: ...
    def output(self, pin: int, value: int) -> None: ...
    def cleanup(self, pin: int) -> None: ...


class FakeGpio:
    """In-memory :class:`GpioBackend` for tests.

    Records the pin mode, every write, and cleanups so a test can assert the
    heartbeat toggles (and stops when the supervisor stalls) without any real
    hardware. ``levels[pin]`` is the last physical level written."""

    def __init__(self) -> None:
        self.mode: dict[int, str] = {}
        self.levels: dict[int, int] = {}
        self.writes: list[tuple[int, int]] = []
        self.cleaned: list[int] = []

    def setup_output(self, pin: int, initial: int = 0) -> None:
        self.mode[pin] = "out"
        self.levels[pin] = int(bool(initial))

    def output(self, pin: int, value: int) -> None:
        self.levels[pin] = int(bool(value))
        self.writes.append((pin, self.levels[pin]))

    def cleanup(self, pin: int) -> None:
        self.cleaned.append(pin)


class _RPiGpioBackend:  # pragma: no cover - real hardware, untested on bench
    """Real ``RPi.GPIO`` backend (BCM numbering).

    Lazy-imports ``RPi.GPIO`` in ``__init__`` so importing this module never
    requires the library on a dev box. **UNTESTED ON HARDWARE** -- validate on the
    bench before trusting it to cut a real motor supply."""

    def __init__(self) -> None:
        import RPi.GPIO as GPIO  # noqa: N814 - vendor module name

        self._GPIO = GPIO
        GPIO.setmode(GPIO.BCM)
        GPIO.setwarnings(False)

    def setup_output(self, pin: int, initial: int = 0) -> None:
        self._GPIO.setup(
            pin, self._GPIO.OUT,
            initial=self._GPIO.HIGH if initial else self._GPIO.LOW,
        )

    def output(self, pin: int, value: int) -> None:
        self._GPIO.output(pin, self._GPIO.HIGH if value else self._GPIO.LOW)

    def cleanup(self, pin: int) -> None:
        self._GPIO.cleanup(pin)


def _default_backend() -> GpioBackend:  # pragma: no cover - hardware path
    """Build the real GPIO backend (only reached when the watchdog is enabled and
    no backend was injected)."""
    return _RPiGpioBackend()


class HardwareWatchdog:
    """Toggles a GPIO heartbeat line while the supervisor keeps pumping it (#44).

    OFF by default and a no-op until ``enabled`` and ``start()``-ed, so it is safe
    to build unconditionally on a dev box. ``pump()`` is called once per
    supervisor tick and toggles the line no faster than ``interval_s``; if pump()
    stops being called (a Pi hard-hang), the line stops toggling and the external
    relay drops the motor supply after its own timeout.

    ``active_low`` inverts the *electrical* level written for a relay board whose
    input is active-low; the logical heartbeat still simply alternates 0/1.
    """

    def __init__(
        self,
        pin: int,
        *,
        interval_s: float = 1.0,
        enabled: bool = True,
        active_low: bool = False,
        gpio: GpioBackend | None = None,
        now_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        self.pin = int(pin)
        self.interval_s = max(0.0, float(interval_s))
        self.enabled = bool(enabled)
        self.active_low = bool(active_low)
        self._gpio = gpio
        self._now = now_fn
        self._level = 0  # logical heartbeat level (0/1)
        self._last_toggle: float | None = None
        self._toggles = 0
        self._started = False

    @classmethod
    def from_config(
        cls,
        cfg,
        *,
        gpio: GpioBackend | None = None,
        now_fn: Callable[[], float] = time.monotonic,
    ) -> "HardwareWatchdog":
        """Build from a :class:`~vanchor.core.config.WatchdogConfig`-like object.

        A ``gpio`` backend may be injected (the tests pass a :class:`FakeGpio`);
        when omitted and the watchdog is enabled, the real backend is built
        lazily on ``start()``."""
        return cls(
            int(getattr(cfg, "gpio_pin", 17)),
            interval_s=float(getattr(cfg, "interval_s", 1.0)),
            enabled=bool(getattr(cfg, "enabled", False)),
            active_low=bool(getattr(cfg, "active_low", False)),
            gpio=gpio,
            now_fn=now_fn,
        )

    @property
    def toggles(self) -> int:
        """Number of heartbeat edges emitted since ``start()`` (test/telemetry)."""
        return self._toggles

    @property
    def level(self) -> int:
        """Current logical heartbeat level (0/1)."""
        return self._level

    @property
    def started(self) -> bool:
        return self._started

    def _phys(self, level: int) -> int:
        """Physical level for a logical ``level`` (honours ``active_low``)."""
        return level ^ 1 if self.active_low else level

    def start(self) -> None:
        """Set the pin as an output and drive the initial level. A no-op when
        disabled or already started. Builds the real GPIO backend lazily if none
        was injected -- so a dev box without ``RPi.GPIO`` only touches it when the
        watchdog is actually enabled."""
        if not self.enabled or self._started:
            return
        if self._gpio is None:
            self._gpio = _default_backend()
        self._gpio.setup_output(self.pin, self._phys(self._level))
        self._started = True
        self._last_toggle = self._now()
        logger.info(
            "hardware watchdog armed on GPIO %d (interval %.2fs, active_low=%s)",
            self.pin, self.interval_s, self.active_low,
        )

    def pump(self) -> int:
        """Heartbeat: toggle the line if at least ``interval_s`` has elapsed since
        the last edge. Called every supervisor tick; returns the current logical
        level. A no-op (returns the current level) when disabled or not started,
        so it is safe to call unconditionally on a dev box."""
        if not self.enabled or not self._started or self._gpio is None:
            return self._level
        now = self._now()
        if self._last_toggle is not None and (now - self._last_toggle) < self.interval_s:
            return self._level
        self._level ^= 1
        self._gpio.output(self.pin, self._phys(self._level))
        self._last_toggle = now
        self._toggles += 1
        return self._level

    def stop(self) -> None:
        """De-assert the line and release the pin. Best-effort + idempotent so a
        shutdown is never blocked. Stopping the heartbeat is itself the SAFE state
        (the external relay drops); driving the logical level to 0 makes the
        intent explicit."""
        if not self._started or self._gpio is None:
            self._started = False
            return
        try:
            self._level = 0
            self._gpio.output(self.pin, self._phys(0))
            self._gpio.cleanup(self.pin)
        except Exception:  # noqa: BLE001 - shutdown must never be blocked
            logger.debug("watchdog stop failed (best-effort)")
        finally:
            self._started = False
