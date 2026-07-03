"""A small, deterministic sea-state / wave model for the simulated IMU (#38).

The sim IMU (see :meth:`vanchor.sim.devices.SimCompass.imu_sample`) is otherwise
a *flat-water* model: yaw rate from the heading change, ~1 g down, everything
else ~0 plus light noise. That is fine for exercising the data path but shows
none of the roll/pitch/heave a boat sees in a seaway -- exactly the signal an
IMU-based motion estimator or a sea-state classifier would train on.

This module adds that motion with a couple of superposed sinusoids
parameterised by significant wave height ``Hs`` and peak period ``Tp``. It is
intentionally **not** a spectral (JONSWAP/Pierson-Moskowitz) simulator: it is a
handful of deterministic components whose amplitudes scale with ``Hs`` and whose
frequencies bracket the peak frequency, which is plenty to produce a believable,
reproducible oscillation.

Determinism is a hard requirement: the phases are drawn from a seeded
:class:`random.Random` (NO wall-clock, NO global RNG), and the motion is a pure
function of the elapsed simulated time ``t``. The same config + the same ``t``
always yields the same sample, so recorded/replayed sessions stay bit-for-bit
reproducible.

**Off by default**: with ``significant_wave_height_m <= 0`` every component has
zero amplitude, so :meth:`SeaState.sample` returns an all-zero
:class:`WaveMotion` and the IMU is unchanged from the flat-water model.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass

# Standard gravity (matches the sim IMU's flat-water ``az`` baseline).
_G = 9.80665

# --- Empirical shaping constants ---------------------------------------- #
# How the significant wave height maps onto angular / heave amplitudes. These
# are deliberately mild, plausible values (a small boat in a moderate chop rolls
# a handful of degrees per metre of Hs), not a validated RAO -- the goal is a
# believable, deterministic signal, not naval-architecture accuracy.
_ROLL_DEG_PER_M = 6.0    # beam-on roll amplitude per metre of Hs
_PITCH_DEG_PER_M = 3.0   # head/following pitch amplitude per metre of Hs
_HEAVE_M_PER_M = 0.35    # vertical heave amplitude as a fraction of Hs


@dataclass(frozen=True)
class WaveMotion:
    """One instant of wave-induced motion.

    Angles are absolute attitude offsets (deg); rates are their time derivatives
    (deg/s); ``heave_accel`` is the vertical (world-up) acceleration (m/s^2) the
    heave imparts, to be summed onto the IMU's ``az`` on top of gravity. All zero
    when the model is off.
    """

    roll_deg: float = 0.0
    pitch_deg: float = 0.0
    roll_rate_dps: float = 0.0
    pitch_rate_dps: float = 0.0
    heave_accel_mps2: float = 0.0


@dataclass
class _Component:
    """A single sinusoidal wave component: ``x(t) = amp * sin(w*t + phase)``."""

    amp: float
    w: float      # angular frequency (rad/s)
    phase: float  # phase offset (rad)


class SeaState:
    """A deterministic multi-sinusoid wave model.

    Build one from a :class:`vanchor.core.config.SeaStateConfig` (see
    :meth:`from_config`) and call :meth:`sample` with the elapsed simulated time
    to get the :class:`WaveMotion` at that instant. ``enabled`` is ``False``
    (and every sample is zero) whenever ``significant_wave_height_m <= 0``.
    """

    # Frequency spread of the components relative to the peak frequency. Two
    # nearby components produce a slow beat (the wave train swells and eases),
    # which reads as far more natural than a single pure tone.
    _FREQ_RATIOS = (1.0, 0.72, 1.35)
    # Per-DOF amplitude weights across the components (they sum to ~1 so the
    # aggregate amplitude still tracks the Hs-scaled target).
    _COMPONENT_WEIGHTS = (0.6, 0.28, 0.12)

    def __init__(
        self,
        *,
        significant_wave_height_m: float = 0.0,
        peak_period_s: float = 4.0,
        heading_deg: float = 0.0,
        seed: int = 20240517,
    ) -> None:
        self.significant_wave_height_m = float(significant_wave_height_m)
        self.peak_period_s = max(1e-3, float(peak_period_s))
        self.heading_deg = float(heading_deg)
        self.seed = int(seed)
        self._build()

    @classmethod
    def from_config(cls, cfg) -> "SeaState":
        """Build from a :class:`SeaStateConfig` (duck-typed on its fields)."""
        return cls(
            significant_wave_height_m=cfg.significant_wave_height_m,
            peak_period_s=cfg.peak_period_s,
            heading_deg=cfg.heading_deg,
            seed=cfg.seed,
        )

    @property
    def enabled(self) -> bool:
        return self.significant_wave_height_m > 0.0

    def _build(self) -> None:
        """(Re)compute the per-DOF component lists from the current params.

        The wave heading splits energy between roll (beam seas) and pitch (head
        seas): a beam sea (90 deg off the bow) is pure roll, a head/following sea
        (0/180 deg) is pure pitch. Heave is present regardless.
        """
        rng = random.Random(self.seed)
        w_peak = 2.0 * math.pi / self.peak_period_s
        Hs = max(0.0, self.significant_wave_height_m)

        # Beam/head split from the wave heading (relative to the bow at 0 deg).
        beam = abs(math.sin(math.radians(self.heading_deg)))   # 1 at 90 deg (beam)
        head = abs(math.cos(math.radians(self.heading_deg)))   # 1 at 0/180 (head)
        roll_amp = _ROLL_DEG_PER_M * Hs * beam
        pitch_amp = _PITCH_DEG_PER_M * Hs * head
        heave_amp = _HEAVE_M_PER_M * Hs

        def _components(total_amp: float) -> list[_Component]:
            comps: list[_Component] = []
            for ratio, weight in zip(self._FREQ_RATIOS, self._COMPONENT_WEIGHTS):
                comps.append(
                    _Component(
                        amp=total_amp * weight,
                        w=w_peak * ratio,
                        phase=rng.uniform(0.0, 2.0 * math.pi),
                    )
                )
            return comps

        self._roll = _components(roll_amp)
        self._pitch = _components(pitch_amp)
        self._heave = _components(heave_amp)

    def sample(self, t: float) -> WaveMotion:
        """The wave motion at elapsed simulated time ``t`` (seconds).

        Pure and deterministic: identical ``t`` -> identical output. Returns an
        all-zero :class:`WaveMotion` when the model is off (Hs <= 0)."""
        if not self.enabled:
            return WaveMotion()

        roll = pitch = roll_rate = pitch_rate = heave_acc = 0.0
        for c in self._roll:
            roll += c.amp * math.sin(c.w * t + c.phase)
            roll_rate += c.amp * c.w * math.cos(c.w * t + c.phase)
        for c in self._pitch:
            pitch += c.amp * math.sin(c.w * t + c.phase)
            pitch_rate += c.amp * c.w * math.cos(c.w * t + c.phase)
        for c in self._heave:
            # Vertical acceleration = second derivative of the heave position:
            #   z(t) = amp * sin(w t + p)  ->  z''(t) = -amp * w^2 * sin(...).
            heave_acc += -c.amp * c.w * c.w * math.sin(c.w * t + c.phase)

        return WaveMotion(
            roll_deg=roll,
            pitch_deg=pitch,
            roll_rate_dps=roll_rate,
            pitch_rate_dps=pitch_rate,
            heave_accel_mps2=heave_acc,
        )

    def apply_to_imu(self, sample, motion: WaveMotion):
        """Return a copy of an :class:`ImuSample` with ``motion`` folded in.

        Adds the wave attitude to ``roll_deg``/``pitch_deg``, the wave rates to
        the roll/pitch gyro axes (``gx``/``gy``), and the gravity projection of
        the tilt plus the heave acceleration to the accelerometer. Yaw (``gz``)
        is left untouched -- waves don't drive the boat's heading. A no-op-ish
        pure function: it never mutates ``sample``.
        """
        import dataclasses

        roll_rad = math.radians(motion.roll_deg)
        pitch_rad = math.radians(motion.pitch_deg)
        # Specific force from tilting in a gravity field (small-angle exact):
        # pitching the nose up puts a -x component of g on the accelerometer,
        # rolling to starboard puts a +y component. az keeps ~g*cos(tilt) plus
        # the heave acceleration.
        ax = sample.ax - _G * math.sin(pitch_rad)
        ay = sample.ay + _G * math.sin(roll_rad) * math.cos(pitch_rad)
        az = (
            sample.az
            - _G  # remove the flat-water 1 g baseline...
            + _G * math.cos(roll_rad) * math.cos(pitch_rad)  # ...re-add the tilted projection
            + motion.heave_accel_mps2
        )
        return dataclasses.replace(
            sample,
            ax=ax,
            ay=ay,
            az=az,
            gx=sample.gx + motion.roll_rate_dps,
            gy=sample.gy + motion.pitch_rate_dps,
            roll_deg=sample.roll_deg + motion.roll_deg,
            pitch_deg=sample.pitch_deg + motion.pitch_deg,
        )
