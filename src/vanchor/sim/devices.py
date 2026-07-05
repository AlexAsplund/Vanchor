"""Simulated devices that implement the real hardware interfaces.

Because these subclass the same ABCs as future serial devices, the controller,
navigator and event wiring cannot tell the difference between simulated and
real hardware. The simulated GPS/compass derive noisy NMEA from the boat's
ground-truth state; the simulated motor records the latest command so the boat
physics can read it.
"""

from __future__ import annotations

import asyncio
import logging
import math
import random
import time
from typing import Callable

from ..core import events
from ..core.events import EventBus
from ..core.geo import angle_difference, mps_to_knots, offset_meters
from ..core.models import BoatState, GpsFix, ImuSample, MotorCommand
from ..hardware.interfaces import Actuator, MotorController, Sensor
from ..nav import nmea

logger = logging.getLogger("vanchor.sim.devices")

TruthFn = Callable[[], BoatState]

# A deliberately corrupt NMEA sentence used by the ``garbage`` fault (#37): it
# carries a ``*`` with a non-hex checksum field, which ``nmea.parse`` always
# rejects with an ``NmeaError`` regardless of the require_checksum flag.
_GARBAGE_NMEA = "$GPRMC,GARBAGE,DATA,####,,,*ZZ"


def _sign(x: float) -> int:
    """Return -1, 0, or 1 for the sign of *x*."""
    if x > 0:
        return 1
    if x < 0:
        return -1
    return 0


class SimMotorController(MotorController):
    """Records the most recent command; the boat physics reads ``command``.

    Optional actuation shaping (all parameters default to zero = OFF) mirrors
    the delays present in the real firmware so sim-trained gains can be stress-
    tested against the actuation holes that cause real-water limit cycles.

    All three shaping stages are **opt-in** and composed in order:

    1. **reverse_delay_s** — when the commanded thrust direction flips (e.g.
       forward → reverse) the output is held at zero for this many seconds.
       Mirrors the applied-direction gate in firmware/engine.ino that prevents
       the prop from reversing before it has shed momentum (~0.9 s on hardware).

    2. **thrust_slew_per_s** — the applied thrust may not change faster than
       this normalized rate per second (0 = unlimited).  Models the soft-start
       ramp the ESC uses to limit inrush current.

    3. **thrust_lag_tau_s** — first-order (exponential) lag toward the slew-
       limited target, with time-constant tau (0 = instant).  Models prop spin-
       up inertia: the prop cannot instantly change speed even after the ESC
       has fully commanded it.

    **dt source**: the shaping state is advanced by calling ``step(dt)`` with
    the simulator's physics dt.  ``Simulator.step`` now calls it every physics
    tick, but ``step`` short-circuits to a no-op while every shaping parameter is
    zero (the default), so existing tuned gains and recorded scenarios are
    bit-for-bit unchanged until a parameter is set (via config / the device
    API).  Deterministic tests call ``step`` directly to control sim-time.
    """

    def __init__(
        self,
        *,
        reverse_delay_s: float = 0.0,
        thrust_slew_per_s: float = 0.0,
        thrust_lag_tau_s: float = 0.0,
    ) -> None:
        self._reverse_delay_s = reverse_delay_s
        self._thrust_slew_per_s = thrust_slew_per_s
        self._thrust_lag_tau_s = thrust_lag_tau_s
        self._requested = MotorCommand()
        self._applied_thrust: float = 0.0
        self._reverse_hold_remaining: float = 0.0
        # --- debug capture (Devices -> Debug live view) -------------------- #
        # How many commands have been applied, and when the last one landed.
        self._apply_count: int = 0
        self._last_apply_monotonic: float | None = None

    def configure(
        self,
        *,
        reverse_delay_s: float | None = None,
        thrust_slew_per_s: float | None = None,
        thrust_lag_tau_s: float | None = None,
    ) -> None:
        """Update the shaping parameters on a live controller (roadmap #36).

        Only the supplied (non-``None``) parameters change; the rest are left as
        they are. Setting every parameter back to zero returns the controller to
        the transparent-passthrough default. Used by the device-config API so a
        bench operator can dial actuation shaping in without a restart.
        """
        if reverse_delay_s is not None:
            self._reverse_delay_s = float(reverse_delay_s)
        if thrust_slew_per_s is not None:
            self._thrust_slew_per_s = float(thrust_slew_per_s)
        if thrust_lag_tau_s is not None:
            self._thrust_lag_tau_s = float(thrust_lag_tau_s)

    def _shaping_enabled(self) -> bool:
        return (
            self._reverse_delay_s != 0.0
            or self._thrust_slew_per_s != 0.0
            or self._thrust_lag_tau_s != 0.0
        )

    def apply(self, command: MotorCommand) -> None:
        """Record *command*; also arms the reverse-delay gate when the thrust
        direction flips (positive → negative or negative → positive)."""
        if self._reverse_delay_s > 0.0:
            prev_sign = _sign(self._requested.thrust)
            new_sign = _sign(command.thrust)
            if prev_sign != 0 and new_sign != 0 and prev_sign != new_sign:
                self._reverse_hold_remaining = self._reverse_delay_s
        self._requested = command
        self._apply_count += 1
        self._last_apply_monotonic = time.monotonic()

    def step(self, dt: float) -> None:
        """Advance actuation shaping by *dt* seconds of simulator time.

        No-op when all shaping parameters are zero (the default).  Tests that
        exercise the opt-in shaping should call this after each ``apply`` to
        move sim time forward before reading ``command``.
        """
        if dt <= 0.0 or not self._shaping_enabled():
            return

        # Stage 1 — reverse-delay gate: hold output at zero while the timer runs.
        if self._reverse_hold_remaining > 0.0:
            self._reverse_hold_remaining = max(0.0, self._reverse_hold_remaining - dt)
            target = 0.0
        else:
            target = self._requested.thrust

        # Stage 2 — slew-rate limit.
        if self._thrust_slew_per_s > 0.0:
            max_delta = self._thrust_slew_per_s * dt
            target = self._applied_thrust + max(
                -max_delta, min(max_delta, target - self._applied_thrust)
            )

        # Stage 3 — first-order lag (exponential approach).
        if self._thrust_lag_tau_s > 0.0:
            alpha = min(1.0, dt / self._thrust_lag_tau_s)
            self._applied_thrust += alpha * (target - self._applied_thrust)
        else:
            self._applied_thrust = target

    @property
    def command(self) -> MotorCommand:
        if not self._shaping_enabled():
            # Default path: pass through instantly with no state mutation.
            return self._requested
        return MotorCommand(
            thrust=self._applied_thrust,
            steering=self._requested.steering,
        )

    def debug(self) -> str:
        cls = type(self).__name__
        try:
            if self._apply_count == 0:
                return f"{cls}: waiting for data…"
            req = self._requested
            lines = [
                cls,
                f"  requested : thrust={req.thrust:+.3f}  steering={req.steering:+.3f}",
            ]
            if self._shaping_enabled():
                # Shaping is on: the applied thrust differs from the request.
                cmd = self.command
                lines.append(
                    f"  applied   : thrust={cmd.thrust:+.3f}  steering={cmd.steering:+.3f}"
                )
                lines.append(f"  rev hold  : {self._reverse_hold_remaining:.2f} s")
            lines.append(f"  count     : {self._apply_count}")
            return "\n".join(lines)
        except Exception as exc:  # noqa: BLE001 - debug must never raise
            return f"{cls}: debug error ({exc})"


class SimServo(Actuator):
    """A trivial simulated servo/stepper, demonstrating the generic actuator
    interface. Not required for the control loop, but shows how a steering
    actuator would be modelled and tested."""

    def __init__(self) -> None:
        self._position = 0.0

    def set_normalized(self, value: float) -> None:
        self._position = max(-1.0, min(1.0, value))

    @property
    def position(self) -> float:
        return self._position


# Fault names a SimGps understands (roadmap #37). All default OFF; each is an
# explicit, independently-toggled degradation used to stress-test the autopilot's
# staleness/spike/parse defences against realistic receiver + serial failures.
GPS_FAULTS = ("dropout", "eof", "garbage", "glitch", "latency")
# Fault names a SimCompass understands (roadmap #37).
COMPASS_FAULTS = ("freeze", "garbage")


class SimGps(Sensor):
    def __init__(
        self,
        get_truth: TruthFn,
        bus: EventBus | None = None,
        *,
        update_hz: float = 1.0,
        # Steady, denoised plotter output (not ~1.5 m raw-receiver scatter); see
        # SensorConfig.gps_noise_m. Keeps the autopilot from chasing phantom XTE.
        position_noise_m: float = 0.35,
        seed: int | None = 1234,
        emit_velocity: bool = False,
        # Multipath jitter model (e.g. indoor/urban). All 0 => the plain white-
        # noise GPS above, unchanged. When set, an Ornstein-Uhlenbeck random walk
        # adds a SLOW-wandering position error (walk_sigma_m steady-state, walk_tau_s
        # correlation) + a phantom velocity bias, and the fix reports reported_hacc_m
        # -- matching a real stationary M9N by a window (~5.7 m RMS, ~0.4 m/s phantom,
        # ~15 m hAcc). See scripts/measured jitter.
        walk_sigma_m: float = 0.0,
        walk_tau_s: float = 40.0,
        vel_bias_sigma_mps: float = 0.0,
        vel_tau_s: float = 8.0,
        reported_hacc_m: float = 0.0,
    ) -> None:
        self.get_truth = get_truth
        self.bus = bus
        self.update_hz = update_hz
        self.position_noise_m = position_noise_m
        self.walk_sigma_m = walk_sigma_m
        self.walk_tau_s = walk_tau_s
        self.vel_bias_sigma_mps = vel_bias_sigma_mps
        self.vel_tau_s = vel_tau_s
        self.reported_hacc_m = reported_hacc_m
        self._walk_e = self._walk_n = 0.0      # OU position random-walk state (m)
        self._vbias_e = self._vbias_n = 0.0    # OU phantom-velocity state (m/s)
        # When True, publish a rich GpsFix carrying the NED ground-velocity vector
        # (like a UBX receiver) instead of an NMEA RMC -- so the sim exercises the
        # SAME capability-gated fusion path as real UBX hardware, proving the
        # activation is driven by the fix's contents, not the driver.
        self.emit_velocity = emit_velocity
        self._rng = random.Random(seed)
        self._task: asyncio.Task | None = None
        # --- fault-injection knobs (#37); all OFF by default ---------------- #
        # dropout / EOF: emit nothing so the fix goes stale (the loss-of-fix
        # failsafe should latch). glitch: a big position jump (spike-guard bait).
        # garbage: an unparseable sentence (parser must reject it). latency: a
        # baud-saturation model -- each fix is buffered and only published this
        # many seconds late, so fresh fixes arrive stale.
        self.fault_dropout = False
        self.fault_glitch = False
        self.fault_glitch_m = 50.0
        self.fault_garbage = False
        self.fault_latency_s = 0.0
        self._latency_buf: list[tuple[float, str]] = []
        # --- debug capture (Devices -> Debug live view) -------------------- #
        # The last thing _loop actually emitted: an RMC sentence (NMEA path) or a
        # rich GpsFix (emit_velocity path). Kept purely for the live debug view.
        self._last_sentence: str | None = None
        self._last_fix: GpsFix | None = None
        self._rx_count: int = 0
        self._last_emit_monotonic: float | None = None

    def set_fault(self, name: str, enabled: bool = True, **params) -> bool:
        """Toggle a named GPS fault (see :data:`GPS_FAULTS`). Returns ``True`` if
        the fault name was recognised. ``dropout`` and ``eof`` are aliases (both
        silence the stream). ``glitch`` accepts ``glitch_m`` (jump size);
        ``latency`` accepts ``latency_s`` (delay). Unknown names are a no-op."""
        if name in ("dropout", "eof"):
            self.fault_dropout = bool(enabled)
        elif name == "glitch":
            self.fault_glitch = bool(enabled)
            if "glitch_m" in params:
                self.fault_glitch_m = float(params["glitch_m"])
        elif name == "garbage":
            self.fault_garbage = bool(enabled)
        elif name == "latency":
            self.fault_latency_s = float(params.get("latency_s", 2.0)) if enabled else 0.0
            if not enabled:
                self._latency_buf.clear()
        else:
            return False
        return True

    def _advance_jitter(self) -> tuple[float, float]:
        """Advance the multipath OU random-walk one tick; return the (east, north)
        position error in metres. No-op (0, 0) unless a jitter profile is set."""
        if self.walk_sigma_m <= 0.0:
            return 0.0, 0.0
        dt = 1.0 / self.update_hz
        rho = math.exp(-dt / max(self.walk_tau_s, 1e-3))
        q = math.sqrt(max(0.0, 1.0 - rho * rho)) * self.walk_sigma_m
        self._walk_e = self._walk_e * rho + self._rng.gauss(0.0, q)
        self._walk_n = self._walk_n * rho + self._rng.gauss(0.0, q)
        if self.vel_bias_sigma_mps > 0.0:
            rv = math.exp(-dt / max(self.vel_tau_s, 1e-3))
            qv = math.sqrt(max(0.0, 1.0 - rv * rv)) * self.vel_bias_sigma_mps
            self._vbias_e = self._vbias_e * rv + self._rng.gauss(0.0, qv)
            self._vbias_n = self._vbias_n * rv + self._rng.gauss(0.0, qv)
        return self._walk_e, self._walk_n

    def sample(self, truth: BoatState | None = None) -> str:
        """Build one RMC sentence from ground truth (pure, for tests).

        Course/speed-over-ground are derived from the *ground* velocity (hull
        motion plus drift), exactly as a real GPS reports them -- so the
        controller can observe the wind/current drift in COG/SOG.

        Honours the ``garbage`` and ``glitch`` faults (#37): garbage returns an
        intentionally malformed sentence the parser rejects; glitch offsets the
        reported position by a large jump to exercise the spike guard."""
        truth = truth or self.get_truth()
        if self.fault_garbage:
            self._advance_jitter()  # keep the walk clock ticking even under garbage
            return _GARBAGE_NMEA
        de, dn = self._advance_jitter()
        noisy = offset_meters(
            truth.point,
            self._rng.gauss(0.0, self.position_noise_m) + de,
            self._rng.gauss(0.0, self.position_noise_m) + dn,
        )
        if self.fault_glitch:
            noisy = offset_meters(noisy, self.fault_glitch_m, self.fault_glitch_m)
        sog_mps = math.hypot(truth.ground_ve, truth.ground_vn)
        # When essentially stationary COG is undefined; report the heading.
        if sog_mps > 0.05:
            cog = math.degrees(math.atan2(truth.ground_ve, truth.ground_vn)) % 360.0
        else:
            cog = truth.heading_deg
        return nmea.encode_rmc(noisy, sog_knots=mps_to_knots(sog_mps), cog_deg=cog)

    def sample_fix(self, truth: BoatState | None = None) -> GpsFix:
        """Build one rich GpsFix from ground truth, carrying the MEASURED NED
        ground-velocity vector (pure, for the ``emit_velocity`` path and tests).

        This is the sim standing in for a velocity-capable receiver: the fix
        includes ``vel_n``/``vel_e`` (and ``vel_d=0`` -- the sim is 2D) plus small
        accuracy estimates, so the fusion's capability-gated features light up
        exactly as they would for a real UBX M9N. Honours the ``glitch`` fault."""
        truth = truth or self.get_truth()
        de, dn = self._advance_jitter()
        noisy = offset_meters(
            truth.point,
            self._rng.gauss(0.0, self.position_noise_m) + de,
            self._rng.gauss(0.0, self.position_noise_m) + dn,
        )
        if self.fault_glitch:
            noisy = offset_meters(noisy, self.fault_glitch_m, self.fault_glitch_m)
        # Phantom velocity: the multipath drift leaks into the receiver's velocity,
        # so a "stationary" fix still reports ~0.4 m/s (matching the real M9N).
        vel_n = truth.ground_vn + self._vbias_n
        vel_e = truth.ground_ve + self._vbias_e
        sog_mps = math.hypot(vel_e, vel_n)
        cog = (math.degrees(math.atan2(vel_e, vel_n)) % 360.0
               if sog_mps > 0.05 else truth.heading_deg)
        return GpsFix(
            point=noisy, sog_knots=mps_to_knots(sog_mps), cog_deg=cog, valid=True,
            vel_n_mps=vel_n, vel_e_mps=vel_e, vel_d_mps=0.0,
            h_acc_m=self.reported_hacc_m or self.position_noise_m, s_acc_mps=0.05,
        )

    def _note_emit(self, *, sentence: str | None = None, fix: GpsFix | None = None) -> None:
        """Record the last item _loop emitted, for the live debug view."""
        self._last_sentence = sentence
        self._last_fix = fix
        self._rx_count += 1
        self._last_emit_monotonic = time.monotonic()

    def _active_faults(self) -> list[str]:
        """Human-readable list of the faults currently injected (for debug())."""
        active: list[str] = []
        if self.fault_dropout:
            active.append("dropout/eof")
        if self.fault_glitch:
            active.append(f"glitch {self.fault_glitch_m:g} m")
        if self.fault_garbage:
            active.append("garbage")
        if self.fault_latency_s > 0.0:
            active.append(f"latency {self.fault_latency_s:g} s")
        return active

    def debug(self) -> str:
        cls = type(self).__name__
        try:
            faults = self._active_faults()
            fault_line = f"\n  fault   : {', '.join(faults)}" if faults else ""
            if self._last_fix is not None:
                f = self._last_fix
                vn, ve, vd = f.vel_n_mps or 0.0, f.vel_e_mps or 0.0, f.vel_d_mps or 0.0
                return (
                    f"{cls}\n"
                    f"  lat/lon : {f.point.lat:.6f}, {f.point.lon:.6f} °\n"
                    f"  sog/cog : {f.sog_knots:.2f} kn / {f.cog_deg:.1f} °\n"
                    f"  vel NED : n={vn:+.2f} e={ve:+.2f} d={vd:+.2f} m/s\n"
                    f"  count   : {self._rx_count}"
                    f"{fault_line}"
                )
            if self._last_sentence is not None:
                try:
                    parsed = nmea.parse(self._last_sentence)
                except Exception:  # noqa: BLE001 - garbage sentence, show it raw
                    parsed = None
                if isinstance(parsed, nmea.RMC):
                    return (
                        f"{cls}\n"
                        f"  lat/lon : {parsed.point.lat:.6f}, {parsed.point.lon:.6f} °\n"
                        f"  sog/cog : {parsed.sog_knots:.2f} kn / {parsed.cog_deg:.1f} °\n"
                        f"  raw     : {self._last_sentence}\n"
                        f"  count   : {self._rx_count}"
                        f"{fault_line}"
                    )
                return (
                    f"{cls}\n"
                    f"  raw     : {self._last_sentence}\n"
                    f"  count   : {self._rx_count}"
                    f"{fault_line}"
                )
            return f"{cls}: waiting for data…"
        except Exception as exc:  # noqa: BLE001 - debug must never raise
            return f"{cls}: debug error ({exc})"

    async def start(self) -> None:
        self._task = asyncio.ensure_future(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _loop(self) -> None:
        period = 1.0 / self.update_hz
        loop = asyncio.get_running_loop()
        next_deadline = loop.time() + period
        while True:
            try:
                # dropout / EOF: publish nothing so the navigator's fix ages out.
                if not self.fault_dropout and self.bus is not None:
                    now = loop.time()
                    if self.emit_velocity:
                        # Rich-fix path (velocity-capable receiver stand-in).
                        fix = self.sample_fix()
                        self._note_emit(fix=fix)
                        await self.bus.publish(events.GPS_FIX_IN, fix)
                    elif self.fault_latency_s > 0.0:
                        # Baud-saturation model: buffer this fix and only release
                        # ones that are at least fault_latency_s old, so fresh
                        # fixes always arrive stale.
                        self._latency_buf.append((now, self.sample()))
                        cutoff = now - self.fault_latency_s
                        while self._latency_buf and self._latency_buf[0][0] <= cutoff:
                            _, due = self._latency_buf.pop(0)
                            self._note_emit(sentence=due)
                            await self.bus.publish(events.NMEA_IN, due)
                    else:
                        sentence = self.sample()
                        self._note_emit(sentence=sentence)
                        await self.bus.publish(events.NMEA_IN, sentence)
            except Exception:
                logger.exception("SimGps publish error; continuing")
            delay = next_deadline - loop.time()
            next_deadline += period
            if delay > 0:
                await asyncio.sleep(delay)


class SimDepthSounder(Sensor):
    """Simulated depth sounder: samples the synthetic bathymetry under the boat
    and emits DPT NMEA, exactly like a real transducer."""

    def __init__(
        self,
        get_truth: TruthFn,
        bathymetry,
        bus: EventBus | None = None,
        *,
        update_hz: float = 2.0,
        noise_m: float = 0.1,
        seed: int | None = 777,
    ) -> None:
        self.get_truth = get_truth
        self.bathymetry = bathymetry
        self.bus = bus
        self.update_hz = update_hz
        self.noise_m = noise_m
        self._rng = random.Random(seed)
        self._task: asyncio.Task | None = None
        # --- debug capture (Devices -> Debug live view) -------------------- #
        # The last depth emitted (m) and the boat position it was sampled at.
        self._last_depth_m: float | None = None
        self._last_point = None
        self._rx_count: int = 0
        self._last_emit_monotonic: float | None = None

    def sample(self, truth: BoatState | None = None) -> str:
        truth = truth or self.get_truth()
        depth = self.bathymetry.depth_at(truth.point) + self._rng.gauss(0.0, self.noise_m)
        depth = max(0.0, depth)
        self._last_depth_m = depth
        self._last_point = truth.point
        self._rx_count += 1
        self._last_emit_monotonic = time.monotonic()
        return nmea.encode_dpt(depth)

    def debug(self) -> str:
        cls = type(self).__name__
        try:
            if self._last_depth_m is None:
                return f"{cls}: waiting for data…"
            p = self._last_point
            pos = f"{p.lat:.6f}, {p.lon:.6f} °" if p is not None else "unknown"
            return (
                f"{cls}\n"
                f"  depth   : {self._last_depth_m:.2f} m\n"
                f"  at      : {pos}\n"
                f"  count   : {self._rx_count}"
            )
        except Exception as exc:  # noqa: BLE001 - debug must never raise
            return f"{cls}: debug error ({exc})"

    async def start(self) -> None:
        self._task = asyncio.ensure_future(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _loop(self) -> None:
        period = 1.0 / self.update_hz
        loop = asyncio.get_running_loop()
        next_deadline = loop.time() + period
        while True:
            try:
                if self.bus is not None:
                    await self.bus.publish(events.NMEA_IN, self.sample())
            except Exception:
                logger.exception("SimDepthSounder publish error; continuing")
            delay = next_deadline - loop.time()
            next_deadline += period
            if delay > 0:
                await asyncio.sleep(delay)


class SimCompass(Sensor):
    def __init__(
        self,
        get_truth: TruthFn,
        bus: EventBus | None = None,
        *,
        update_hz: float = 5.0,
        heading_noise_deg: float = 1.0,
        seed: int | None = 4321,
        sea_state=None,
    ) -> None:
        self.get_truth = get_truth
        self.bus = bus
        self.update_hz = update_hz
        self.heading_noise_deg = heading_noise_deg
        self._rng = random.Random(seed)
        self._task: asyncio.Task | None = None
        self._prev_heading: float | None = None  # for the simulated yaw rate
        # Optional deterministic sea-state model (#38) driving the IMU. ``None``
        # (or a model with Hs<=0) leaves the flat-water IMU bit-for-bit unchanged.
        self.sea_state = sea_state
        self._sea_t = 0.0  # elapsed sim-time fed to the wave model
        # --- fault-injection knobs (#37); all OFF by default ---------------- #
        # freeze: heading stuck at the value captured when the fault engaged (a
        # hung magnetometer). garbage: an unparseable sentence the parser rejects.
        self.fault_freeze = False
        self.fault_garbage = False
        self._frozen_heading: float | None = None
        # --- debug capture (Devices -> Debug live view) -------------------- #
        # The last heading emitted (numeric, or None + raw string when garbage)
        # and the last IMU sample produced.
        self._last_heading_deg: float | None = None
        self._last_heading_raw: str | None = None
        self._last_imu: ImuSample | None = None
        self._last_emit_monotonic: float | None = None

    def set_fault(self, name: str, enabled: bool = True, **params) -> bool:
        """Toggle a named compass fault (see :data:`COMPASS_FAULTS`). Returns
        ``True`` if the fault name was recognised, else a no-op ``False``."""
        if name == "freeze":
            self.fault_freeze = bool(enabled)
            if not enabled:
                self._frozen_heading = None
        elif name == "garbage":
            self.fault_garbage = bool(enabled)
        else:
            return False
        return True

    def sample(self, truth: BoatState | None = None) -> str:
        truth = truth or self.get_truth()
        self._last_emit_monotonic = time.monotonic()
        if self.fault_garbage:
            self._last_heading_deg = None
            self._last_heading_raw = _GARBAGE_NMEA
            return _GARBAGE_NMEA
        if self.fault_freeze:
            # Latch the first heading seen under the fault and hold it forever.
            if self._frozen_heading is None:
                self._frozen_heading = truth.heading_deg
            sentence = nmea.encode_hdm(self._frozen_heading)
            self._last_heading_deg = self._frozen_heading
            self._last_heading_raw = sentence
            return sentence
        heading = truth.heading_deg + self._rng.gauss(0.0, self.heading_noise_deg)
        sentence = nmea.encode_hdm(heading)
        self._last_heading_deg = heading
        self._last_heading_raw = sentence
        return sentence

    def debug(self) -> str:
        cls = type(self).__name__
        try:
            if self._last_heading_raw is None and self._last_imu is None:
                return f"{cls}: waiting for data…"
            lines = [cls]
            if self._last_heading_deg is not None:
                lines.append(f"  heading : {self._last_heading_deg:.1f} °")
            elif self._last_heading_raw is not None:
                lines.append(f"  heading : (raw) {self._last_heading_raw}")
            imu = self._last_imu
            if imu is not None:
                lines.append(
                    f"  accel   : ax={imu.ax:+.3f} ay={imu.ay:+.3f} az={imu.az:+.3f} m/s²"
                )
                lines.append(
                    f"  gyro    : gx={imu.gx:+.3f} gy={imu.gy:+.3f} gz={imu.gz:+.3f} °/s"
                )
                lines.append(
                    f"  att     : roll={imu.roll_deg:+.2f} pitch={imu.pitch_deg:+.2f} °"
                )
            faults = []
            if self.fault_freeze:
                faults.append("freeze")
            if self.fault_garbage:
                faults.append("garbage")
            if faults:
                lines.append(f"  fault   : {', '.join(faults)}")
            return "\n".join(lines)
        except Exception as exc:  # noqa: BLE001 - debug must never raise
            return f"{cls}: debug error ({exc})"

    def imu_sample(self, truth: BoatState, dt: float) -> ImuSample:
        """A flat-water simulated IMU: yaw rate from the heading change, ~1 g
        down, everything else ~0 plus light noise. Enough to exercise the IMU
        pipeline / data-collection path.

        When a :class:`~vanchor.sim.sea_state.SeaState` is attached and enabled
        (#38), its roll/pitch/heave motion (attitude, roll/pitch rates, and the
        gravity + heave accelerometer signature) is folded on top. With no sea
        state (or Hs<=0) the output is identical to the flat-water model, so the
        default behaviour is bit-for-bit preserved."""
        yaw_rate = 0.0
        if self._prev_heading is not None and dt > 0:
            yaw_rate = angle_difference(self._prev_heading, truth.heading_deg) / dt
        self._prev_heading = truth.heading_deg
        n = lambda s: self._rng.gauss(0.0, s)  # noqa: E731
        sample = ImuSample(
            ax=n(0.05), ay=n(0.05), az=9.80665 + n(0.05),
            gx=n(0.2), gy=n(0.2), gz=yaw_rate + n(0.3),
            roll_deg=n(0.3), pitch_deg=n(0.3), source="sim",
        )
        if self.sea_state is not None and self.sea_state.enabled:
            self._sea_t += max(0.0, dt)
            motion = self.sea_state.sample(self._sea_t)
            sample = self.sea_state.apply_to_imu(sample, motion)
        self._last_imu = sample
        return sample

    async def start(self) -> None:
        self._task = asyncio.ensure_future(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _loop(self) -> None:
        period = 1.0 / self.update_hz
        loop = asyncio.get_running_loop()
        next_deadline = loop.time() + period
        while True:
            try:
                truth = self.get_truth()
                if self.bus is not None:
                    await self.bus.publish(events.NMEA_IN, self.sample(truth))
                    await self.bus.publish(events.IMU_IN, self.imu_sample(truth, period))
            except Exception:
                logger.exception("SimCompass publish error; continuing")
            delay = next_deadline - loop.time()
            next_deadline += period
            if delay > 0:
                await asyncio.sleep(delay)
