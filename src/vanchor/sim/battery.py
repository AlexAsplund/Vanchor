"""A simple simulated battery model (#60).

Trolling-motor autopilots run off a deep-cycle battery; how much charge is left
(and how far the boat can still get on it) is a first-class safety concern. This
model is intentionally lightweight -- it is *not* an electrochemical model -- but
it is believable enough to drive the UI gauges and the Return-to-Launch
auto-recommend logic (#61):

  * It draws a current that is the sum of a constant *idle* (hotel/standby) load
    and a *propulsion* load proportional to ``|thrust|``. The propulsion draw is
    modelled at full thrust as ``load_a`` amps, scaled linearly with the thrust
    magnitude (a coarse but monotonic stand-in for the real cubic-ish curve).
  * It integrates the drawn charge out of the pack over time, lowering the
    state-of-charge (SOC).
  * It estimates time-to-empty and remaining range from a *recent average* draw
    and the boat's speed-over-ground, so the figures track how the boat is
    actually being used rather than an instantaneous spike.

On real hardware the SOC / voltage / current come from a battery monitor (a
shunt + gauge) over the HAL instead of being integrated here; the telemetry
shape and the range/time estimates stay identical so the UI and the RTL logic
do not care which source is wired in. See ``BatteryConfig``.
"""

from __future__ import annotations

from dataclasses import dataclass

# SOC is held above this floor so a fully drained pack still reports a sane
# (non-negative) value rather than going negative.
_SOC_MIN = 0.0
_SOC_MAX = 100.0


@dataclass
class BatteryConfig:
    """Battery pack sizing + the load model.

    ``reserve_pct`` is the usable-charge reserve: range / time-to-empty are
    reported down to this reserve (not to a flat-dead pack), matching how a
    skipper plans "I must turn back while I still have my reserve".
    """

    capacity_ah: float = 100.0  # pack capacity (amp-hours)
    nominal_v: float = 12.0  # nominal terminal voltage
    reserve_pct: float = 15.0  # usable-charge reserve (%) kept in hand
    idle_a: float = 0.5  # constant hotel/standby draw (A)
    load_a: float = 40.0  # propulsion draw at full |thrust| (A)
    # Recent-draw smoothing time constant (s) for the range/time estimate, so a
    # momentary thrust spike doesn't make the range figure jump around.
    draw_tau_s: float = 20.0


class Battery:
    """Integrates state-of-charge from the applied thrust and reports estimates.

    Pure and synchronous (no I/O, no clock) so it can be stepped deterministically
    from the simulator and unit-tested. ``step(dt, thrust, sog_mps)`` advances it.
    """

    def __init__(self, config: BatteryConfig | None = None, soc_pct: float = 100.0) -> None:
        self.config = config or BatteryConfig()
        self.soc_pct = max(_SOC_MIN, min(_SOC_MAX, soc_pct))
        # Last instantaneous draw (A) and the smoothed recent average (A).
        self.current_a = self.config.idle_a
        self._avg_current_a = self.config.idle_a
        # Smoothed recent speed-over-ground (m/s) for the range estimate.
        self._avg_sog_mps = 0.0

    def set_soc(self, soc_pct: float) -> None:
        """Set/reset the state-of-charge (e.g. swapping a fresh battery in)."""
        self.soc_pct = max(_SOC_MIN, min(_SOC_MAX, float(soc_pct)))

    def _draw_for(self, thrust: float) -> float:
        cfg = self.config
        return cfg.idle_a + cfg.load_a * min(1.0, abs(thrust))

    def step(self, dt: float, thrust: float, sog_mps: float) -> None:
        """Advance the SOC by one step under the given thrust + speed.

        ``thrust`` is the normalized applied thrust (-1..1); ``sog_mps`` is the
        boat's speed over ground in m/s (used only for the range estimate).
        """
        if dt <= 0.0:
            return
        cfg = self.config
        self.current_a = self._draw_for(thrust)

        # Integrate charge out of the pack: Ah drawn = A * (dt / 3600).
        ah_drawn = self.current_a * (dt / 3600.0)
        if cfg.capacity_ah > 0.0:
            self.soc_pct = max(_SOC_MIN, self.soc_pct - 100.0 * ah_drawn / cfg.capacity_ah)

        # Smooth the recent draw + speed for stable range/time estimates.
        alpha = dt / (cfg.draw_tau_s + dt)
        self._avg_current_a += (self.current_a - self._avg_current_a) * alpha
        self._avg_sog_mps += (max(0.0, sog_mps) - self._avg_sog_mps) * alpha

    # -- Derived telemetry ---------------------------------------------- #
    @property
    def voltage_v(self) -> float:
        """A crude terminal voltage that sags as the pack drains.

        Lead-acid resting voltage runs roughly from ~12.7 V full to ~11.8 V
        empty; we linearly interpolate over that span around ``nominal_v`` so the
        UI voltage gauge moves. (Real hardware reports a measured voltage.)
        """
        full = self.config.nominal_v + 0.7
        empty = self.config.nominal_v - 0.2
        return empty + (full - empty) * (self.soc_pct / 100.0)

    @property
    def draw_w(self) -> float:
        """Instantaneous power draw in watts."""
        return self.current_a * self.voltage_v

    @property
    def _usable_ah(self) -> float:
        """Amp-hours left above the reserve floor."""
        cfg = self.config
        usable_pct = max(0.0, self.soc_pct - cfg.reserve_pct)
        return cfg.capacity_ah * usable_pct / 100.0

    @property
    def time_to_empty_s(self) -> float:
        """Estimated seconds until the usable reserve is hit, at the recent draw.

        Returns ``inf`` when there is effectively no draw (nothing to estimate
        against)."""
        if self._avg_current_a <= 1e-6:
            return float("inf")
        return self._usable_ah / self._avg_current_a * 3600.0

    @property
    def range_m(self) -> float:
        """Estimated metres of travel left at the recent average draw + speed.

        Returns 0 when the boat isn't making way (no usable distance estimate)."""
        tte = self.time_to_empty_s
        if self._avg_sog_mps <= 1e-3 or tte == float("inf"):
            return 0.0
        return self._avg_sog_mps * tte

    def to_dict(self) -> dict:
        tte = self.time_to_empty_s
        return {
            "soc_pct": round(self.soc_pct, 1),
            "voltage_v": round(self.voltage_v, 2),
            "current_a": round(self.current_a, 2),
            "draw_w": round(self.draw_w, 1),
            "range_m": round(self.range_m, 1),
            # JSON has no infinity; surface "unknown" (no meaningful draw) as null.
            "time_to_empty_s": None if tte == float("inf") else round(tte, 1),
        }
