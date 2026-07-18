"""Typed, nested, file-backed application configuration.

The whole controller is configured from a single :class:`AppConfig` tree of
small dataclasses. Each sub-config maps onto the constructors of an existing
component (the simulator, sensors, controller, control modes, helm and the
environment) so the integrator can wire things up by reading fields straight
off the config rather than threading loose keyword arguments around.

Configs can be loaded from a YAML (``.yaml``/``.yml``) or JSON (``.json``)
file. Unknown keys are ignored and any missing key falls back to its default,
so partial config files are always valid. With no path (or a missing file) the
built-in defaults are returned unchanged.
"""

from __future__ import annotations

import json
import logging
import math
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger("vanchor.config")


@dataclass
class SimConfig:
    """Simulator world + physics settings.

    Maps onto ``Simulator(physics_hz, time_scale)`` and seeds the boat's
    starting position.
    """

    start_lat: float = 59.66275  # 59°39'45.9"N (Lake Vänern, Karlstad)
    start_lon: float = 13.32247  # 13°19'20.9"E
    physics_hz: float = 20.0
    model: str = "fossen"  # "fossen" (3-DOF, bow-mount aware) or "simple"
    time_scale: float = 1.0


@dataclass
class SimMotorConfig:
    """Simulated-motor actuation shaping (roadmap #36).

    Maps onto :class:`vanchor.sim.devices.SimMotorController`'s opt-in shaping
    stages, which mirror the actuation holes present in the real firmware/ESC so
    sim-trained gains can be stress-tested against them. **All fields default to
    zero = OFF**, so the simulated motor is a transparent passthrough and every
    existing tuned gain / recorded scenario is bit-for-bit preserved until a
    field is set. See ``SimMotorController`` for the physical meaning of each
    stage; they compose in order (reverse-delay gate -> slew limit -> lag).

    These are surfaced through the same persisted device-config path as the
    hardware config (``devices.json``), so a bench setup survives a restart and
    is editable live from Settings alongside the real-hardware knobs.
    """

    # DEFAULTS MIRROR THE REAL FIRMWARE (sim-vs-real review 2026-07-15):
    # engine.ino applies THROTTLE_SLEW_PER_S = 1.0 and a 1000 ms reverse
    # dead-time unconditionally, so the sim boat now feels those too. Set a
    # field to 0 to restore the legacy transparent-passthrough motor (older
    # recorded scenarios / gains were captured with instant actuation).
    reverse_delay_s: float = 1.0     # hold output at zero this long after a thrust sign flip
    thrust_slew_per_s: float = 1.0   # max normalized thrust change per second (0 = unlimited)
    thrust_lag_tau_s: float = 0.0    # first-order prop spin-up lag (0 = instant; no firmware analog)


@dataclass
class SeaStateConfig:
    """Deterministic sea-state / wave model driving the simulated IMU (#38).

    Maps onto :class:`vanchor.sim.sea_state.SeaState`. A couple of superposed,
    seeded sinusoids parameterised by significant wave height + peak period add
    roll/pitch/heave motion (and the matching accelerometer/gyro signature) on
    top of the flat-water IMU. **Default-off**: ``significant_wave_height_m: 0``
    contributes exactly zero, so the IMU output is bit-for-bit unchanged from the
    flat-water model until waves are enabled. Fully deterministic (seeded, no
    wall-clock) so recorded/replayed sessions stay reproducible.
    """

    significant_wave_height_m: float = 0.0  # Hs; 0 = flat water (model OFF)
    peak_period_s: float = 4.0              # dominant wave period (s)
    heading_deg: float = 0.0                # wave-propagation heading (splits roll vs pitch)
    seed: int = 20240517                    # phase seed (deterministic, no wall-clock)


@dataclass
class BoatConfig:
    """Physical boat + trolling-motor geometry.

    Feeds the ``fossen`` 3-DOF physics model (which is bow-mount aware) and the
    ``simple`` model's speed/turn limits. ``thruster_mount`` captures *where* the
    steerable trolling motor sits: a bow mount pulls the bow around, a stern
    mount pushes it -- the sign of the resulting yaw is opposite, which the
    model accounts for via the longitudinal offset from the centre of gravity.
    """

    length_m: float = 4.1
    beam_m: float = 1.7
    mass_kg: float = 300.0
    max_speed_mps: float = 1.6
    max_thrust_n: float = 250.0  # ~55 lbf trolling motor
    reverse_efficiency: float = 0.6  # reverse prop thrust as a fraction of forward
    thruster_mount: str = "bow"  # "bow" | "stern" | "center"
    thruster_offset_m: float | None = None  # explicit CG->thruster (+fwd); overrides mount
    thruster_y_m: float = 0.0  # lateral CG->thruster offset (+ = starboard)
    # Longitudinal CG position, as a fraction of length AFT of the geometric
    # centre. Real boats carry their mass aft (battery, fuel, outboard/engine,
    # the helm seat), so the CG sits behind centre -- which LENGTHENS the lever
    # arm from the CG to a bow motor (sharper turns) and shortens it for a stern
    # motor. 0 = CG at centre (old behaviour); ~0.1 is typical. Folded into
    # thruster_x_m() below (and thus the Fossen yaw moment).
    cg_aft_frac: float = 0.10
    # Thrust-yaw feed-forward: a steering deflection that pre-cancels the yaw a
    # laterally-offset thruster induces under straight thrust. None = derive from
    # geometry (atan2(thruster_y_m, |thruster_x_m|)); a number overrides it. A
    # calibration-measured TRIM (radians) is added on top of whichever is used.
    thrust_yaw_ff: float | None = None
    thrust_yaw_ff_trim: float = 0.0  # calibration refinement (radians) on the FF angle
    max_steer_angle_deg: float = 180.0  # full mechanical swing (manual reaches this)
    autopilot_steer_deg: float = 35.0  # authority the autopilot actually uses
    # Head rotation speed: the gearmotor's 20 rpm is 120 deg/s peak, but with
    # ramp up/down the EFFECTIVE rate is 90-100 deg/s (owner-confirmed
    # 2026-07-09). 95 captures the ramp in a constant-rate model.
    max_steer_rate_dps: float = 95.0
    max_turn_rate_deg: float = 18.0  # used by the kinematic "simple" model
    # Hull character / tracking (directional stability) for the fossen model.
    # ~0.35 = jon boat (flat-bottom: loose, skittish, lots of leeway), 1.0 =
    # current skiff (default), ~2.5 = deep-V / keel (tracks straight, resists
    # turning). Clamped to ~0.25..3.0 on use. Scales the yaw + sway damping (see
    # FossenParams.__post_init__); at 1.0 with the default L/B it is a no-op.
    hull_tracking: float = 1.0
    # Steering gearbox (the closed-loop azimuth unit; see cad/steering.py).
    shaft_dia_mm: float = 25.4
    # +/- mechanical rotation limit of the steering head. The servo/gearbox
    # design allows at least +/-360 deg from centre (owner-confirmed 2026-07-09;
    # the old 185 value was a stale cable-wrap assumption). Set lower for a
    # build with restricted cable routing.
    steer_range_deg: float = 360.0
    steer_reduction: float = 4.0  # pinion->ring reduction
    # Sonar transducer beam (cone) angle in degrees. NMEA DPT/DBT carry only a
    # depth, never a beam angle, so this configurable default is what sizes the
    # depth-map footprint: footprint diameter at depth d = 2*d*tan(cone/2).
    sonar_cone_deg: float = 20.0

    def thruster_x_m(self) -> float:
        """Signed longitudinal distance from CG to the thruster (+ = forward).

        The mount fixes the thruster's position relative to the hull's geometric
        centre (bow ~+0.42L, stern ~-0.42L); the CG sits ``cg_aft_frac`` of a
        length BEHIND that centre, so the CG->thruster arm is the gap between
        them. An explicit ``thruster_offset_m`` already encodes the CG distance
        and is used verbatim.
        """
        if self.thruster_offset_m is not None:
            return self.thruster_offset_m
        frac = {"bow": 0.42, "stern": -0.42, "center": 0.0}.get(self.thruster_mount, 0.42)
        return frac * self.length_m + self.cg_aft_frac * self.length_m

    def thrust_yaw_ff_angle(self) -> float:
        """Feed-forward steering deflection (radians) that cancels the straight-
        thrust yaw of a laterally-offset thruster.

        A thruster at ``(x, y)`` making forward thrust ``F`` and steering-induced
        lateral force produces yaw ``N = x*F_lat - y*F_fwd``. Deflecting the motor
        by ``delta`` gives ``F_fwd = F*cos(delta)``, ``F_lat = F*sin(delta)`` so
        ``N = F*(x*sin(delta) - y*cos(delta))`` which is zero when
        ``x*sin(delta) = y*cos(delta)`` => ``delta = atan2(y, |x|)`` -- independent
        of thrust magnitude. ``thrust_yaw_ff`` overrides this geometric value; a
        measured ``thrust_yaw_ff_trim`` is then added on top.

        Note the lever arm uses ``|x|``: a stern mount (x < 0) needs the same
        *physical* deflection sign as a bow mount to oppose the same lateral
        offset; the bow/stern steering-authority flip is handled separately by the
        helm's ``steer_sign``.
        """
        if self.thrust_yaw_ff is not None:
            return self.thrust_yaw_ff + self.thrust_yaw_ff_trim
        x_mag = abs(self.thruster_x_m())
        return math.atan2(self.thruster_y_m, x_mag) + self.thrust_yaw_ff_trim


@dataclass
class EnvironmentConfig:
    """Wind and current. Maps onto
    ``Environment(current_speed, current_dir, wind_speed, wind_dir)``.
    """

    current_speed: float = 0.0
    current_dir: float = 0.0
    wind_speed: float = 0.0
    wind_dir: float = 0.0
    gust_amplitude_mps: float = 0.0  # gust std on top of the base wind (0 = steady)
    gust_tau_s: float = 5.0
    # Slow, session-scale weather wander amount in [0, 1] (0 = steady). Wind
    # variability slowly shifts wind speed AND direction; current variability
    # slowly shifts the current. Gusts ride on top.
    wind_variability: float = 0.0
    current_variability: float = 0.0


@dataclass
class SensorConfig:
    """Simulated sensor rates and noise.

    ``gps_hz`` maps onto ``SimGps(update_hz)`` and ``compass_hz`` onto
    ``SimCompass(update_hz)``.
    """

    gps_hz: float = 10.0  # matches the M9N driver's marine config (cfg_marine_10hz)
    compass_hz: float = 5.0
    depth_hz: float = 2.0
    # Per-fix position jitter (m, 1-sigma). Real marine GPS/chart-plotters
    # smooth (Kalman/SBAS) the fix before emitting NMEA, so the track is steady
    # frame-to-frame (~0.2-0.4 m), NOT the ~1.5 m raw-receiver scatter. Modelling
    # the denoised plotter keeps the autopilot from chasing phantom cross-track
    # error and weaving down a leg in otherwise calm water.
    gps_noise_m: float = 0.35
    compass_noise_deg: float = 1.0
    # Local magnetic declination applied by the navigator to convert MAGNETIC
    # headings (HDM/HDG, reference="M") to true. Degrees East-positive.
    #   None  = AUTO: evaluate the full WMM2025 model at the current position
    #           (the DEFAULT). Only affects magnetic sources; HDT/true sources
    #           (e.g. the HWT901B, which self-corrects) pass through untouched.
    #   float = a fixed manual override (e.g. a survey/plotter-supplied variation).
    # The app forces 0.0 when the compass is the SIMULATOR (a zero-declination,
    # true-heading world) so sim behaviour is unchanged regardless of this default.
    magnetic_declination_deg: float | None = None
    # GNSS/INS fusion (M9N UBX velocity + HWT901B IMU). Additive: it only fills the
    # extra state.fusion_* fields (yaw rate, ground velocity, crab, dead-reckoning)
    # from whatever sensors are present; heading/position/control are unchanged.
    # On by default (cheap + useful); set false to skip the filter entirely.
    fusion_enabled: bool = True
    # Make the SIMULATED GPS emit a rich fix with a MEASURED velocity vector (like
    # a UBX receiver) instead of NMEA RMC -- exercises the capability-gated fusion
    # path end-to-end in the sim. Off by default (keeps the sim's NMEA behaviour).
    gps_velocity: bool = False
    # Sim-GPS multipath jitter profile: "off" (clean white noise) or "indoor" (a
    # slow-wandering random walk + phantom velocity + large hAcc, matching a real
    # stationary M9N by a window ~5.7 m RMS). For testing the autopilot + filtering.
    gps_jitter: str = "off"
    # Accuracy-weighted GPS position low-pass: smooths harder when the receiver
    # reports a large hAcc (bad fix), ~passthrough for a good fix. Off by default.
    gps_position_filter: bool = False
    # Sensor-anomaly protection (spike rejection).
    position_jump_max_m: float = 15.0
    heading_jump_max_deg: float = 30.0


@dataclass
class ControlConfig:
    """Controller loop rate and the gains for every guided behaviour.

    ``tick_hz`` maps onto ``Controller(tick_hz)``. The ``heading_*`` gains map
    onto the helm ``PID(kp, ki, kd)``. The ``anchor_*`` gains and
    ``anchor_radius_m`` map onto ``modes.AnchorConfig(kp, ki, kd, ...)``. The
    ``waypoint_*`` fields map onto
    ``modes.WaypointConfig(arrival_radius_m, throttle, xte_gain)``.
    """

    tick_hz: float = 5.0

    heading_kp: float = 0.035  # auto-tuned compromise (faster settle, anchor-safe)
    heading_ki: float = 0.0
    heading_kd: float = 0.012
    steer_tau: float = 0.6  # low-pass (s) on steering so the head isn't driven by noise

    # --- Adaptive helm gain scheduling (roadmap #31) --------------------- #
    # A single steerable trolling motor vectors the boat with its prop wash, so
    # its steering AUTHORITY scales with thrust / boat speed (see STEER_EPS in
    # controller.py: below a thrust floor it has no authority at all). One fixed
    # ``heading_kp`` is therefore brittle across the 0.2-2 m/s band: at low speed
    # authority is WEAK so the loop is sluggish (wants MORE gain); at high speed
    # authority is STRONG so the same gain oscillates (wants LESS gain). This
    # schedule scales the helm's proportional gain with SOG:
    #   kp_eff = heading_kp * mult(sog),  mult linearly interpolated between
    #   ``mult_lo`` (at/below ``sog_lo_kn``) and ``mult_hi`` (at/above
    #   ``sog_hi_kn``), then clamped to [mult_min, mult_max].
    # The physically-correct shape is ``mult_lo >= mult_hi`` (more gain when slow).
    # Defaults are NEUTRAL (both multipliers 1.0) => kp_eff == heading_kp at every
    # speed, so behaviour is UNCHANGED until a non-flat schedule is configured.
    steer_gain_sog_lo_kn: float = 0.3   # SOG (kn) at/below which mult_lo applies
    steer_gain_sog_hi_kn: float = 2.0   # SOG (kn) at/above which mult_hi applies
    steer_gain_mult_lo: float = 1.0     # gain multiplier at low SOG (weak authority)
    steer_gain_mult_hi: float = 1.0     # gain multiplier at high SOG (strong authority)
    steer_gain_mult_min: float = 0.1    # clamp on the multiplier (bounds kp_eff)
    steer_gain_mult_max: float = 5.0

    anchor_kp: float = 0.12  # thrust per metre of position error
    anchor_kd: float = 0.6  # braking thrust per (m/s) of closing speed (reverse)
    anchor_radius_m: float = 5.0
    anchor_idle_deadband_m: float = 0.8  # idle within this band of the mark (no hunting)

    # --- Vectored / azimuth station-keeping (roadmap #35) ----------------- #
    # OPT-IN: while holding a spot, let the motor's azimuth sweep up to
    # ``station_keep_azimuth_deg`` off the bow (instead of the autopilot's
    # ±autopilot_steer_deg band) so it can push directly against the set rather
    # than re-orienting the hull first. Applies ONLY to the anchor-hold
    # station-keeping path -- heading-hold/waypoint/etc. keep the normal
    # autopilot authority. Defaults (False + 35 deg) preserve today's behaviour
    # exactly. When enabling, ~110-120 deg is a sensible authority: it covers a
    # beam set directly and, combined with reverse thrust, most of the circle.
    station_keep_vectored: bool = False
    station_keep_azimuth_deg: float = 35.0

    waypoint_throttle: float = 0.6
    waypoint_arrival_m: float = 5.0
    waypoint_xte_gain: float = 2.0

    # Tier-1 features.
    jog_increment_m: float = 1.5  # anchor-jog step (~5 ft)
    cruise_kp: float = 0.64  # Cruise Control (constant SOG) PID (auto-tuned)
    cruise_ki: float = 0.25
    track_min_distance_m: float = 5.0  # record a breadcrumb every N metres
    # --- Trip log (#66): automatic per-outing recording. ----------------- #
    auto_trip: bool = True  # auto-start a trip when the boat makes way
    trip_min_distance_m: float = 5.0  # breadcrumb spacing for the trip track
    trip_start_speed_kn: float = 0.5  # SOG over this auto-starts a trip
    trip_idle_timeout_s: float = 120.0  # idle this long below the threshold -> auto-stop
    drift_kp: float = 0.5  # Drift mode (controlled drift speed) PID
    drift_ki: float = 0.25
    drift_default_knots: float = 0.5


@dataclass
class SafetyConfig:
    """Limits and watchdogs that protect the boat and the motor."""

    max_thrust_slew_per_s: float = 2.0
    reverse_delay_s: float = 0.5
    fix_timeout_s: float = 5.0
    # Loss-of-fix failsafe. ON by default (a switch in Settings -> Safety): once
    # no fresh fix has arrived for fix_timeout_s thrust is forced to zero so the
    # boat coasts rather than steaming blind -- the conservative default the
    # review and roadmap call for on a trolling motor. Set False to keep holding
    # the last command through a fix dropout.
    fix_failsafe_enabled: bool = True
    # Sensor-staleness watchdogs (seconds). A stale compass heading forces a
    # coast while a guided mode steers; a stale depth is treated as unknown by
    # the shallow-water stop instead of trusting a frozen sounding.
    heading_stale_s: float = 3.0
    depth_stale_s: float = 10.0
    drag_alarm_factor: float = 2.0
    # Shallow-water / geofence auto-stop (#62).
    min_depth_m: float = 0.0  # cut thrust below this sounded depth (0 = disabled)
    nogo_lookahead_m: float = 5.0  # also stop within this distance of a no-go zone
    # Return-to-Launch (#61) auto-recommend / auto-engage.
    rtl_margin_m: float = 100.0  # warn when range-home gets within this of battery range
    auto_rtl: bool = False  # if true, auto-engage RTL (don't just recommend)
    # Lost-connection failsafe (#64): seconds with no UI client connected while
    # underway before auto-engaging anchor-hold (hold position).
    link_loss_timeout_s: float = 20.0
    # Lost-link behaviour for GUIDED modes (routes/cruise/drift/...): default
    # True = keep flying the mission unsupervised (the pocket-the-phone /
    # locked-screen workflow: an active route must NOT park just because the
    # phone stopped talking — field report 2026-07-15). Geofence / depth /
    # battery failsafes still apply while unsupervised. False = park-and-hold
    # (anchor) where the boat is (#64). MANUAL driving always STOPS on link
    # loss; that deadman is part of the safety floor and is NOT configurable.
    link_loss_continue_mission: bool = True
    # Auto Follow-APB: when an external autopilot's APB sentence appears on any
    # NMEA input, auto-engage Follow-APB — but ONLY from idle MANUAL (it never
    # hijacks an anchor hold / route / a hand on the throttle). Re-arms only
    # after the APB feed has been silent for a while, so leaving the mode
    # manually isn't instantly overridden. Default OFF; toggle in Settings →
    # Safety (persisted server-side in safety.json).
    auto_follow_apb: bool = False
    # --- Low-battery thrust-derating ladder (#49) ----------------------- #
    # As the battery state-of-charge falls through these rungs the maximum
    # applied thrust is capped in progressive steps (a SOFT derate) BEFORE the
    # lowest stage hands the boat off to the existing RTL/failsafe. Each rung is
    # ``[soc_pct, thrust_cap]`` (cap in 0..1). The ladder only ever LOWERS the
    # cap -- STOP and every failsafe still take precedence and are never blocked.
    # Defaults: full thrust above 40 %, then 70 % / 45 % / 25 % caps as the pack
    # drains, then RTL hand-off at 10 %. Set ``battery_ladder_enabled: false`` to
    # disable the derate entirely (the cap stays 1.0).
    battery_ladder_enabled: bool = True
    battery_ladder: list = field(
        default_factory=lambda: [[40.0, 0.7], [25.0, 0.45], [15.0, 0.25]]
    )
    battery_rtl_soc_pct: float = 10.0  # lowest stage: hand off to RTL at/below this SoC


# Keys of :class:`SafetyConfig` that constitute the non-negotiable safety floor
# (#50). A later hot-reload, per-boat profile, or backup-restore may make these
# SAFER but never WEAKER. Named here (rather than as a config field) so the set
# is discoverable + testable in one place without polluting the serialized config.
SAFETY_FLOOR_KEYS: tuple[str, ...] = ("fix_failsafe_enabled", "min_depth_m")


@dataclass(frozen=True)
class SafetyFloor:
    """Non-negotiable safety-floor lockout (#50).

    Captures the LOCKED safety values from the BASE/startup config. Wherever
    config is later merged, reloaded, or applied -- a hot-reload, a per-boat
    profile, a backup-restore, or a runtime Settings edit -- a locked key may be
    ratcheted TIGHTER (safer) but can never be WEAKENED below its startup value:

    * ``fix_failsafe_enabled`` -- if the loss-of-fix failsafe was ON at startup it
      can never be disabled by a later partial update (only left on).
    * ``min_depth_m`` -- the shallow-water auto-stop limit can be RAISED but never
      lowered below the startup floor (lowering it toward 0 weakens the stop).

    :meth:`enforce_fix_failsafe` / :meth:`enforce_min_depth` return the *allowed*
    value (clamped to the floor) and log a warning when they refuse a weakening;
    :meth:`sanitize` applies both to a partial update mapping. This is a pure,
    I/O-free policy object so it is exhaustively unit-testable -- the callers
    (the config-apply / command / geometry paths) route their proposed values
    through it, so a failsafe can only ever tighten.
    """

    fix_failsafe_enabled: bool = True
    min_depth_m: float = 0.0

    @classmethod
    def from_config(cls, safety: Any) -> "SafetyFloor":
        """Capture the floor from a (startup/base) safety config.

        Duck-typed (reads via ``getattr`` with safe defaults) so it accepts
        either the app :class:`SafetyConfig` or the controller/governor's own
        ``SafetyConfig`` -- both expose ``fix_failsafe_enabled`` + ``min_depth_m``."""
        return cls(
            fix_failsafe_enabled=bool(getattr(safety, "fix_failsafe_enabled", True)),
            min_depth_m=float(getattr(safety, "min_depth_m", 0.0)),
        )

    def enforce_fix_failsafe(self, proposed: bool | None) -> bool:
        """Allowed loss-of-fix-failsafe value: a base-enabled failsafe can never
        be turned OFF. ``None`` means "not being changed" -> keep the floor."""
        if proposed is None:
            return self.fix_failsafe_enabled
        allowed = bool(proposed) or self.fix_failsafe_enabled
        if allowed != bool(proposed):
            log.warning(
                "safety-floor lockout (#50): refusing to DISABLE the loss-of-fix "
                "failsafe -- it was locked ON by the startup config"
            )
        return allowed

    def enforce_min_depth(self, proposed: float | None) -> float:
        """Allowed min-depth stop: can be RAISED but never lowered below the
        startup floor. ``None`` means "not being changed" -> keep the floor."""
        if proposed is None:
            return self.min_depth_m
        p = float(proposed)
        if p < self.min_depth_m:
            log.warning(
                "safety-floor lockout (#50): refusing min-depth %.2fm below the "
                "startup floor of %.2fm -- keeping the safer limit", p, self.min_depth_m
            )
            return self.min_depth_m
        return p

    def sanitize(self, updates: dict[str, Any]) -> dict[str, Any]:
        """Return a copy of a partial ``{key: value}`` update with every locked
        key clamped to the floor (weakening refused + logged). Keys not present
        are left untouched, so a non-safety partial update passes through
        unchanged and non-locked keys still hot-reload normally."""
        out = dict(updates)
        if "fix_failsafe_enabled" in out:
            out["fix_failsafe_enabled"] = self.enforce_fix_failsafe(out["fix_failsafe_enabled"])
        if "min_depth_m" in out:
            out["min_depth_m"] = self.enforce_min_depth(out["min_depth_m"])
        return out


@dataclass
class BatteryConfig:
    """Simulated/monitored battery pack (#60, #42).

    Maps onto ``sim.battery.BatteryConfig``. On real hardware the live
    SOC/voltage/current come from a battery monitor over the HAL
    (``hardware.battery_source: ina226``); these fields still size the pack for
    the range/time-to-empty estimates. The ``i2c_*`` / ``shunt_ohms`` fields are
    read by the INA226 battery driver (the reference 4th device kind, #42) — this
    dataclass is exactly the narrow config slice that driver's capability object
    exposes.
    """

    capacity_ah: float = 100.0  # pack capacity (amp-hours)
    nominal_v: float = 12.0  # nominal terminal voltage
    reserve_pct: float = 15.0  # usable-charge reserve (%) kept in hand
    # Recent-draw smoothing time constant (s) for the range/time estimate.
    draw_tau_s: float = 20.0
    # --- INA226 / shunt battery driver (battery_source: ina226) -------------- #
    i2c_bus: int = 1  # /dev/i2c-<n> the shunt is on
    i2c_addr: int = 0x40  # INA226 I2C address (0x40 default)
    shunt_ohms: float = 0.001  # shunt resistance (ohms); current = Vshunt / Rshunt
    max_current_a: float = 80.0  # gauge full-scale current (informational/UI)


@dataclass
class ServerConfig:
    """Web UI / HTTP server bind address."""

    host: str = "127.0.0.1"
    port: int = 8000
    # Advertise the UI over mDNS so a phone/PWA can auto-find it at vanchor.local
    # (no IP typing). Graceful no-op if zeroconf is unavailable.
    mdns: bool = True
    # Optional HTTPS listener on a SECOND port (same app). Secure-context browser
    # APIs (Screen Wake Lock, full PWA/service-worker installs) need HTTPS, which
    # plain-HTTP LAN serving can't give. 0 disables. If the port is busy or no
    # cert can be produced, HTTPS is skipped with a warning (HTTP unaffected).
    https_port: int = 8443
    # Bring-your-own cert paths; both empty -> a self-signed cert with
    # CN=vanchor.local is auto-generated once into <data_dir>/tls/ and reused.
    ssl_certfile: str = ""
    ssl_keyfile: str = ""


@dataclass
class HardwareConfig:
    """Real serial hardware. ``enabled`` is the master switch (False = full
    simulation, the default; True = all real serial devices).

    Per-device ``*_source`` overrides let you **mix** simulated and real devices
    — e.g. drive a real steering servo while the boat itself is simulated, to
    bench-test the servo against a realistic autopilot. Each is ``"sim"`` or
    ``"serial"`` (the motor also accepts ``"both"`` = drive the sim boat AND
    mirror commands to the real servo). ``None`` follows ``enabled``."""

    enabled: bool = False
    gps_port: str = "/dev/ttyUSB0"
    compass_port: str = "/dev/ttyUSB1"
    motor_port: str = "/dev/ttyUSB2"
    # Shared baud fallback (NMEA 0183 standard). Kept for backward compat — if
    # per-device keys are absent this value is used for compass and motor.
    baudrate: int = 4800
    # Per-device baud rates. ``gps_baud`` defaults to 38400 because 5 Hz GPS
    # (RMC + GGA) needs ~8200 bit/s — already 170 % of a 4800-baud link, so the
    # OS RX buffer fills and fixes arrive stale within seconds. 38400 gives 4.5×
    # headroom. Set compass_baud / motor_baud only if your device needs it;
    # otherwise the shared ``baudrate`` (4800) is the right NMEA 0183 default.
    gps_baud: int = 38400
    compass_baud: int = 4800
    motor_baud: int = 115200  # protocol v2 default (CRC lines; fw VANCHOR_BAUD)
    # Per-device serial framing. NMEA 0183 and the Arduino motor link are 8N1
    # (the defaults); override only for an unusual device. bytesize 5-8;
    # parity "N"/"E"/"O"/"M"/"S"; stopbits 1, 1.5 or 2.
    gps_bytesize: int = 8
    gps_parity: str = "N"
    gps_stopbits: float = 1.0
    compass_bytesize: int = 8
    compass_parity: str = "N"
    compass_stopbits: float = 1.0
    motor_bytesize: int = 8
    motor_parity: str = "N"
    motor_stopbits: float = 1.0
    # --- Split-motor per-channel overrides (motor-split, Task 1) ------------- #
    # The motor is really two channels (steering + thrust). By default BOTH follow
    # the legacy motor_* fields above, so the constructed object graph is IDENTICAL
    # to today (one combined controller on one link). Set a channel's *_source (or
    # *_port) ONLY to drive that channel on its own board/link. Resolution lives in
    # source()/channel_link() below; the physical-link decision in
    # hardware.link_plan.plan_motor_links.
    steering_source: str | None = None  # "sim" | "serial" | "both" | "none"
    thrust_source: str | None = None    # "sim" | "serial" | "both" | "none"
    steering_port: str = ""             # blank -> fall back to motor_port
    thrust_port: str = ""               # blank -> fall back to motor_port
    steering_baud: int = 115200
    thrust_baud: int = 115200
    steering_bytesize: int = 8
    steering_parity: str = "N"
    steering_stopbits: float = 1.0
    thrust_bytesize: int = 8
    thrust_parity: str = "N"
    thrust_stopbits: float = 1.0
    # Sensors also accept "nmea": build NO internal device and let the navigator
    # be fed by external NMEA over the TCP bridge (--nmea-tcp) or inject_nmea —
    # e.g. a phone or chart-plotter GPS. So "GPS from NMEA" is never blocked.
    gps_source: str | None = None      # "sim" | "serial" | "nmea"
    compass_source: str | None = None  # "sim" | "serial" | "nmea"
    depth_source: str | None = None    # "sim" | "nmea" (no serial depth yet)
    motor_source: str | None = None    # "sim" | "serial" | "both"
    # Battery monitor (#42): the reference registry-driven 4th device kind.
    # "sim" reads the simulated pack; "ina226" is a real shunt gauge over I2C;
    # "none" disables it. None => "sim" when a simulated boat exists, else "none"
    # (a real battery monitor isn't implied by enabling serial GPS/compass/motor).
    battery_source: str | None = None  # "sim" | "ina226" | "none"

    # Per-device settings from a driver's device_menu(), keyed by device kind:
    # e.g. {"compass": {"declination_mode": "manual", "manual_declination_deg": 3}}.
    # Persisted so menu choices survive a restart and are applied when the driver
    # is built. Free-form (each driver validates via apply_setting).
    device_settings: dict = field(default_factory=dict)

    def source(self, device: str) -> str:
        """Resolve the source for ``device``
        ("gps"/"compass"/"depth"/"motor"/"steering"/"thrust"), honouring its
        override else falling back.

        The two motor channels ("steering"/"thrust") fall back to the LEGACY motor
        resolution (``source("motor")``) rather than straight to ``enabled``, so an
        unset channel behaves EXACTLY like today's single motor (Constraint 3)."""
        override = getattr(self, f"{device}_source", None)
        if override:
            return override
        if device in ("steering", "thrust"):
            return self.source("motor")
        return "serial" if self.enabled else "sim"

    def channel_link(self, channel: str) -> dict:
        """Resolve the full link for a motor ``channel`` ("steering"/"thrust").

        Returns ``{source, port, baud, bytesize, parity, stopbits}``.

        Partial-override rule (crisp, so back-compat is exact): a channel is
        "configured" when its ``*_source`` is set OR its ``*_port`` is non-empty.

          * NOT configured -> EVERY field mirrors the legacy ``motor_*`` link, so an
            unset channel is bit-for-bit today's motor (Constraint 3).
          * configured -> the channel's OWN baud + framing apply (they do not keep
            blending with motor_*). The port still falls back to ``motor_port`` when
            the channel port is left blank, since a serial link needs a port.
        """
        chan_src = getattr(self, f"{channel}_source", None)
        chan_port = getattr(self, f"{channel}_port", "")
        configured = bool(chan_src) or bool(chan_port)
        if not configured:
            # Legacy: mirror the motor link field-for-field.
            return {
                "source": self.source("motor"),
                "port": self.motor_port,
                "baud": self.motor_baud,
                "bytesize": self.motor_bytesize,
                "parity": self.motor_parity,
                "stopbits": self.motor_stopbits,
            }
        return {
            "source": chan_src if chan_src else self.source("motor"),
            "port": chan_port if chan_port else self.motor_port,
            "baud": getattr(self, f"{channel}_baud"),
            "bytesize": getattr(self, f"{channel}_bytesize"),
            "parity": getattr(self, f"{channel}_parity"),
            "stopbits": getattr(self, f"{channel}_stopbits"),
        }


@dataclass
class NmeaTcpConfig:
    """Optional NMEA-over-TCP server (e.g. for OpenCPN)."""

    enabled: bool = False
    host: str = "0.0.0.0"
    port: int = 10110


@dataclass
class WatchdogConfig:
    """External hardware watchdog: a heartbeat GPIO the ~1 Hz supervisor must keep
    toggling or an external relay cuts the motor supply (#44).

    This covers a Raspberry Pi HARD-HANG that the in-process (firmware) watchdog
    cannot catch: if the event loop stops, the heartbeat stops toggling, an
    external retriggerable relay driver times out, and the motor supply drops --
    independently of the (hung) software. **OFF by default**; enable it and set
    the BCM ``gpio_pin`` wired to the relay driver. The real RPi.GPIO / gpiozero
    backend is lazy-imported and is **untested on hardware** (bench only); a fake
    injectable backend drives the unit tests.
    """

    enabled: bool = False
    gpio_pin: int = 17        # BCM pin driving the external retriggerable relay
    interval_s: float = 1.0   # minimum seconds between heartbeat edges (~supervisor rate)
    active_low: bool = False  # invert the electrical level for the relay board's polarity


@dataclass
class ObsConfig:
    """Observability: the always-on black-box flight recorder (roadmap #20).

    A lightweight, always-running ring buffer that samples a low-rate snapshot of
    the control loop (mode, position, heading, distance-to-anchor, the DESIRED vs
    APPLIED motor command, and the active alarms). On ANY alarm transition (drag
    alarm, controller fault, link/fix failsafe, shallow/no-go stop, ...) the ring
    is dumped -- pre-trigger history plus a short post-trigger tail -- to a
    timestamped gzip file off the event loop, so an incident is captured even
    when the opt-in debug recorder isn't running.

    Defaults keep it ON at a low rate: the ring append is O(1) and only samples
    at ``blackbox_sample_hz``, so the control tick is never slowed. Set
    ``blackbox_enabled: false`` to turn it off entirely (no ring, no wrapper).
    """

    blackbox_enabled: bool = True
    blackbox_sample_hz: float = 1.0        # low-rate ring sampling cadence
    blackbox_window_s: float = 180.0       # pre-trigger history retained (~3 min)
    blackbox_post_trigger_s: float = 10.0  # tail captured (at tick rate) after a trip


@dataclass
class DemoConfig:
    """One-flag demo mode (`vanchor --demo`, adoption pack).

    Forced simulation with a seeded, already-moving scenario and an ephemeral
    data dir. **Default-off**: every field's default keeps current behaviour;
    the flag (or VANCHOR_DEMO=1) is the only way in. `readonly` additionally
    pins every UI client to the observer role (hosted-demo hardening);
    stop still always works.
    """

    enabled: bool = False
    readonly: bool = False
    scenario: str = "route"     # "route" (small looping route) | "anchor" (hold on the spot)
    start_lat: float = 59.8779  # charted demo lake (same spot the README screenshots use)
    start_lon: float = 12.0293
    weather_preset: str = "lake"  # sim weather preset applied at boot ("" = leave calm)


@dataclass
class AppConfig:
    """The root configuration tree."""

    data_dir: str = "vanchor_data"  # persisted depth map + debug recordings
    sim: SimConfig = field(default_factory=SimConfig)
    sim_motor: SimMotorConfig = field(default_factory=SimMotorConfig)
    sea_state: SeaStateConfig = field(default_factory=SeaStateConfig)
    boat: BoatConfig = field(default_factory=BoatConfig)
    environment: EnvironmentConfig = field(default_factory=EnvironmentConfig)
    sensors: SensorConfig = field(default_factory=SensorConfig)
    control: ControlConfig = field(default_factory=ControlConfig)
    safety: SafetyConfig = field(default_factory=SafetyConfig)
    battery: BatteryConfig = field(default_factory=BatteryConfig)
    server: ServerConfig = field(default_factory=ServerConfig)
    hardware: HardwareConfig = field(default_factory=HardwareConfig)
    nmea_tcp: NmeaTcpConfig = field(default_factory=NmeaTcpConfig)
    watchdog: WatchdogConfig = field(default_factory=WatchdogConfig)
    obs: ObsConfig = field(default_factory=ObsConfig)
    demo: DemoConfig = field(default_factory=DemoConfig)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "AppConfig":
        """Build an :class:`AppConfig` from a (possibly partial) mapping.

        Performs a deep, defensive merge: unknown keys are ignored and any key
        not present keeps its default. ``None`` is treated as an empty mapping.
        """
        data = data or {}
        kwargs: dict[str, Any] = {}
        for f in fields(cls):
            if f.name not in _SUBCONFIGS:
                # plain scalar field (e.g. data_dir): take value or keep default.
                if f.name in data:
                    kwargs[f.name] = data[f.name]
                continue
            sub_cls = f.type if isinstance(f.type, type) else _SUBCONFIGS[f.name]
            kwargs[f.name] = _build_sub(sub_cls, data.get(f.name))
        return cls(**kwargs)

    def to_dict(self) -> dict[str, Any]:
        """Serialize the full tree to plain nested dicts."""
        return asdict(self)


# Mapping of AppConfig field name -> its sub-config dataclass. Used by
# ``from_dict`` because ``from __future__ import annotations`` turns field
# type annotations into strings.
_SUBCONFIGS: dict[str, type] = {
    "sim": SimConfig,
    "sim_motor": SimMotorConfig,
    "sea_state": SeaStateConfig,
    "boat": BoatConfig,
    "environment": EnvironmentConfig,
    "sensors": SensorConfig,
    "control": ControlConfig,
    "safety": SafetyConfig,
    "battery": BatteryConfig,
    "server": ServerConfig,
    "hardware": HardwareConfig,
    "nmea_tcp": NmeaTcpConfig,
    "watchdog": WatchdogConfig,
    "obs": ObsConfig,
    "demo": DemoConfig,
}


def _build_sub(sub_cls: type, data: Any) -> Any:
    """Instantiate a leaf sub-config, ignoring unknown keys and filling
    defaults for anything absent."""
    if not isinstance(data, dict):
        return sub_cls()
    known = {f.name for f in fields(sub_cls)}
    kwargs = {k: v for k, v in data.items() if k in known}
    return sub_cls(**kwargs)


# --- Persisted, editable device/hardware config (devices.json) ----------- #
# The device config is the only part of the config that the running UI can
# edit + persist (separately from the load-only YAML/defaults). It lives in a
# small ``<data_dir>/devices.json`` so a saved hardware setup survives restarts.
# Shape: ``{"hardware": {...HardwareConfig fields...},
#           "nmea_tcp": {...NmeaTcpConfig fields...},
#           "sim_motor": {...SimMotorConfig fields...}}`` -- the same defensive,
# field-merge tolerance as the rest of the config (unknown/missing keys OK). The
# ``sim_motor`` block is only written when a SimMotorConfig is supplied, so the
# on-disk shape stays backward-compatible with files/tests that predate it.
DEVICES_FILE = "devices.json"


def _merge_into(obj: Any, data: Any) -> None:
    """Overwrite the known fields of dataclass ``obj`` from mapping ``data``,
    coercing numeric/bool fields to the declared type. Unknown/absent keys and
    ``None`` mean "keep what's there". Mutates ``obj`` in place."""
    if not isinstance(data, dict):
        return
    for f in fields(obj):
        if f.name not in data or data[f.name] is None:
            continue
        val = data[f.name]
        cur = getattr(obj, f.name)
        if isinstance(cur, bool):
            val = bool(val)
        elif isinstance(cur, int) and not isinstance(cur, bool):
            val = int(val)
        elif isinstance(cur, float):
            val = float(val)
        setattr(obj, f.name, val)


def load_device_overrides(data_dir: str | Path) -> dict[str, Any] | None:
    """Read ``<data_dir>/devices.json`` if present, else ``None``.

    Returns the parsed ``{"hardware": {...}, "nmea_tcp": {...}}`` mapping. A
    missing file (the common case) or a corrupt/non-mapping file returns
    ``None`` so startup falls back to the base config untouched.
    """
    p = Path(data_dir) / DEVICES_FILE
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.warning("ignoring unreadable %s: %s", p, exc)
        return None
    if not isinstance(data, dict):
        log.warning("ignoring %s: not a mapping", p)
        return None
    return data


def apply_device_overrides(config: AppConfig, data_dir: str | Path | None = None) -> AppConfig:
    """Override ``config.hardware`` + ``config.nmea_tcp`` from a persisted
    ``devices.json`` (if one exists under ``data_dir``), then return ``config``.

    ``data_dir`` defaults to ``config.data_dir``. A field-level merge: any saved
    key overrides the loaded base, missing/extra keys are tolerated. Call this
    after :func:`load` and before building the runtime so a saved device config
    survives restarts.
    """
    overrides = load_device_overrides(data_dir if data_dir is not None else config.data_dir)
    if overrides is None:
        return config
    _merge_into(config.hardware, overrides.get("hardware"))
    _merge_into(config.nmea_tcp, overrides.get("nmea_tcp"))
    _merge_into(config.sim_motor, overrides.get("sim_motor"))
    log.info("applied device overrides from %s", DEVICES_FILE)
    return config


def save_device_overrides(
    data_dir: str | Path,
    hardware: HardwareConfig,
    nmea_tcp: NmeaTcpConfig,
    sim_motor: SimMotorConfig | None = None,
) -> dict[str, Any]:
    """Persist ``hardware`` + ``nmea_tcp`` (and optionally ``sim_motor``) to
    ``<data_dir>/devices.json``.

    Returns the written mapping. The ``sim_motor`` block is included only when a
    :class:`SimMotorConfig` is supplied, keeping the on-disk shape backward
    compatible with callers/tests that persist only hardware + nmea_tcp. The
    directory is created if needed.
    """
    payload: dict[str, Any] = {"hardware": asdict(hardware), "nmea_tcp": asdict(nmea_tcp)}
    if sim_motor is not None:
        payload["sim_motor"] = asdict(sim_motor)
    d = Path(data_dir)
    d.mkdir(parents=True, exist_ok=True)
    (d / DEVICES_FILE).write_text(json.dumps(payload, indent=2), encoding="utf-8")
    log.info("saved device overrides to %s", d / DEVICES_FILE)
    return payload


# --- Environment binding (.env + VANCHOR_* overrides) -------------------- #
# Deployment values (bind hosts/ports, data dir, hardware ports, the NMEA
# bridge, routing endpoints) are all *environment-bindable* so a release can be
# configured without editing files. A tiny no-dependency .env loader populates
# ``os.environ`` (never clobbering a real env var), then ``apply_env_overrides``
# folds the ``VANCHOR_*`` vars onto the loaded config so env always wins.


def _parse_bool(value: str) -> bool:
    """Lenient truthy parser for env-var booleans ("1/true/yes/on" => True)."""
    return value.strip().lower() in ("1", "true", "yes", "on")


def load_dotenv(path: str | Path | None = None) -> dict[str, str]:
    """Read a ``.env`` file and set any of its keys not already in ``os.environ``.

    No external dependency. Parses simple ``KEY=VALUE`` lines, ignoring blank
    lines and ``#`` comments, stripping surrounding single/double quotes and a
    leading ``export ``. A real environment variable always wins (existing keys
    are left untouched). The path defaults to ``$VANCHOR_ENV_FILE`` else
    ``./.env``; a missing file is a quiet no-op. Returns the parsed mapping.
    """
    if path is None:
        path = os.environ.get("VANCHOR_ENV_FILE", ".env")
    p = Path(path)
    if not p.exists():
        return {}
    parsed: dict[str, str] = {}
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        if not key:
            continue
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        parsed[key] = val
        os.environ.setdefault(key, val)  # real env wins
    log.info("Loaded %d var(s) from %s", len(parsed), p)
    return parsed


def apply_env_overrides(config: AppConfig) -> AppConfig:
    """Override config fields from ``VANCHOR_*`` env vars (in place) and return it.

    Only set variables are applied (unset ones leave the loaded/default value
    untouched); each is coerced to the field's type. Booleans use
    :func:`_parse_bool` ("1/true/yes/on").
    """
    env = os.environ

    def _apply(var: str, target: Any, attr: str, coerce: Callable[[str], Any]) -> None:
        val = env.get(var)
        if val is None:
            return
        try:
            setattr(target, attr, coerce(val))
        except (TypeError, ValueError) as exc:
            log.warning("ignoring %s=%r: %s", var, val, exc)

    # Server.
    _apply("VANCHOR_HOST", config.server, "host", str)
    _apply("VANCHOR_PORT", config.server, "port", int)
    # Data.
    _apply("VANCHOR_DATA_DIR", config, "data_dir", str)
    # Simulation.
    _apply("VANCHOR_MODEL", config.sim, "model", str)
    _apply("VANCHOR_TIME_SCALE", config.sim, "time_scale", float)
    _apply("VANCHOR_PHYSICS_HZ", config.sim, "physics_hz", float)
    _apply("VANCHOR_SIM_START_LAT", config.sim, "start_lat", float)
    _apply("VANCHOR_SIM_START_LON", config.sim, "start_lon", float)
    # Sensors & hardware.
    _apply("VANCHOR_HARDWARE", config.hardware, "enabled", _parse_bool)
    _apply("VANCHOR_GPS_PORT", config.hardware, "gps_port", str)
    _apply("VANCHOR_COMPASS_PORT", config.hardware, "compass_port", str)
    _apply("VANCHOR_MOTOR_PORT", config.hardware, "motor_port", str)
    _apply("VANCHOR_BAUDRATE", config.hardware, "baudrate", int)
    _apply("VANCHOR_GPS_BAUD", config.hardware, "gps_baud", int)
    _apply("VANCHOR_COMPASS_BAUD", config.hardware, "compass_baud", int)
    _apply("VANCHOR_MOTOR_BAUD", config.hardware, "motor_baud", int)
    _apply("VANCHOR_GPS_SOURCE", config.hardware, "gps_source", str)
    _apply("VANCHOR_COMPASS_SOURCE", config.hardware, "compass_source", str)
    _apply("VANCHOR_DEPTH_SOURCE", config.hardware, "depth_source", str)
    _apply("VANCHOR_MOTOR_SOURCE", config.hardware, "motor_source", str)
    _apply("VANCHOR_BATTERY_SOURCE", config.hardware, "battery_source", str)
    # NMEA bridge.
    _apply("VANCHOR_NMEA_TCP", config.nmea_tcp, "enabled", _parse_bool)
    _apply("VANCHOR_NMEA_TCP_HOST", config.nmea_tcp, "host", str)
    _apply("VANCHOR_NMEA_TCP_PORT", config.nmea_tcp, "port", int)
    # Demo mode.
    _apply("VANCHOR_DEMO", config.demo, "enabled", _parse_bool)
    _apply("VANCHOR_DEMO_READONLY", config.demo, "readonly", _parse_bool)
    return config


def load(path: str | Path | None) -> AppConfig:
    """Load an :class:`AppConfig` from a ``.yaml``/``.yml``/``.json`` file.

    Returns the built-in defaults when ``path`` is None or the file does not
    exist. Raises ``ValueError`` for an unsupported extension and propagates
    parse errors from the underlying loader.

    A ``.env`` file is read first (see :func:`load_dotenv`) and the resulting
    ``VANCHOR_*`` environment variables are applied *after* the file/defaults so
    the environment always wins over the YAML/JSON and the built-in defaults.
    """
    load_dotenv()

    if path is None:
        return apply_env_overrides(AppConfig())

    p = Path(path)
    if not p.exists():
        log.warning("Config file %s not found; using defaults", p)
        return apply_env_overrides(AppConfig())

    text = p.read_text(encoding="utf-8")
    suffix = p.suffix.lower()
    if suffix in (".yaml", ".yml"):
        import yaml  # PyYAML, only needed for YAML configs.

        data = yaml.safe_load(text)
    elif suffix == ".json":
        data = json.loads(text)
    else:
        raise ValueError(f"Unsupported config extension: {p.suffix!r}")

    if data is not None and not isinstance(data, dict):
        raise ValueError(f"Config file {p} must contain a mapping at the top level")

    log.info("Loaded config from %s", p)
    return apply_env_overrides(AppConfig.from_dict(data))


# A documented, copy-pasteable example covering every section and its defaults.
DEFAULT_CONFIG_YAML: str = """\
# Vanchor-NG configuration. Every key is optional; omitted keys use defaults.

sim:
  start_lat: 59.66275
  start_lon: 13.32247
  physics_hz: 20.0
  model: fossen          # "fossen" (3-DOF, bow-mount aware) or "simple"
  time_scale: 1.0

sim_motor:               # simulated-motor actuation shaping (#36); all 0 = OFF (passthrough)
  reverse_delay_s: 1.0     # mirrors firmware REVERSE_DEADTIME_MS (0 = legacy instant motor)
  thrust_slew_per_s: 1.0   # mirrors firmware THROTTLE_SLEW_PER_S (0 = unlimited)
  thrust_lag_tau_s: 0.0    # first-order prop spin-up lag (0 = instant; no firmware analog)

sea_state:               # deterministic wave model driving the sim IMU (#38)
  significant_wave_height_m: 0.0  # Hs; 0 = flat water (model OFF, IMU unchanged)
  peak_period_s: 4.0              # dominant wave period (s)
  heading_deg: 0.0                # wave-propagation heading (splits roll vs pitch)
  seed: 20240517                  # phase seed (deterministic; no wall-clock)

boat:
  length_m: 4.1
  beam_m: 1.7
  mass_kg: 300.0
  max_speed_mps: 1.6
  max_thrust_n: 250.0    # ~55 lbf trolling motor
  reverse_efficiency: 0.6   # reverse prop thrust as a fraction of forward
  thruster_mount: bow    # bow | stern | center
  thruster_offset_m: null  # explicit CG->thruster distance (+fwd); overrides mount
  thruster_y_m: 0.0      # lateral CG->thruster offset (+ = starboard); off-centre yaws under thrust
  thrust_yaw_ff: null    # FF steer angle (rad) to cancel that yaw; null = derive atan2(y,|x|)
  thrust_yaw_ff_trim: 0.0  # calibration-measured FF refinement (rad), added on top
  max_steer_angle_deg: 180.0   # full mechanical swing (manual reaches this)
  autopilot_steer_deg: 35.0    # authority the autopilot actually uses
  max_steer_rate_dps: 95.0  # effective head rotation speed incl. ramp (peak 120)
  max_turn_rate_deg: 18.0  # used by the "simple" model only
  hull_tracking: 1.0     # directional stability: ~0.35 jon boat (loose) .. 1.0 skiff .. ~2.5 keel (tracks)
  shaft_dia_mm: 25.4         # trolling-motor shaft (steering gearbox)
  steer_range_deg: 360.0     # +/- head rotation limit (servo design allows >=360)
  steer_reduction: 4.0       # pinion -> ring reduction
  sonar_cone_deg: 20.0       # sonar transducer beam angle; sizes depth-map footprint (2*d*tan(cone/2))

environment:
  current_speed: 0.0
  current_dir: 0.0
  wind_speed: 0.0
  wind_dir: 0.0
  gust_amplitude_mps: 0.0     # gust std on top of the base wind (0 = steady)
  gust_tau_s: 5.0             # how slowly gusts build and fade
  wind_variability: 0.0       # slow session-scale wander of wind speed+dir, [0,1] (0 = steady)
  current_variability: 0.0    # slow session-scale wander of current, [0,1] (0 = steady)

sensors:
  gps_hz: 10.0
  compass_hz: 5.0
  depth_hz: 2.0
  gps_noise_m: 0.35   # denoised plotter output (steady), not raw-receiver scatter
  compass_noise_deg: 1.0
  # Local magnetic declination, degrees East-positive; applied to MAGNETIC headings
  # (HDM/HDG) to produce true. Default AUTO (full WMM2025 at the current position);
  # set a number to force a fixed value. Only affects magnetic sources — HDT/true
  # sources (e.g. the self-correcting HWT901B) pass through; the simulator is
  # forced to 0. Omit the key (or use null) for AUTO.
  magnetic_declination_deg: null
  position_jump_max_m: 15.0    # reject GPS jumps bigger than this (unless confirmed)
  heading_jump_max_deg: 30.0   # reject heading spikes bigger than this per sample

control:
  tick_hz: 5.0
  heading_kp: 0.035
  heading_ki: 0.0
  heading_kd: 0.012
  steer_tau: 0.6              # low-pass (s) on steering so the head isn't noise-driven
  # Adaptive helm gain scheduling (#31): scale the helm's proportional gain with
  # SOG so steering stays consistent as prop-wash authority changes with speed.
  # Defaults below are NEUTRAL (both multipliers 1.0 -> kp_eff == heading_kp).
  steer_gain_sog_lo_kn: 0.3   # SOG (kn) at/below which mult_lo applies
  steer_gain_sog_hi_kn: 2.0   # SOG (kn) at/above which mult_hi applies
  steer_gain_mult_lo: 1.0     # gain multiplier at low SOG (weak authority -> more gain)
  steer_gain_mult_hi: 1.0     # gain multiplier at high SOG (strong authority -> less gain)
  steer_gain_mult_min: 0.1    # clamp on the multiplier (bounds kp_eff)
  steer_gain_mult_max: 5.0
  anchor_kp: 0.12             # thrust per metre of position error
  anchor_kd: 0.6              # braking thrust per (m/s) closing speed (enables reverse)
  anchor_radius_m: 5.0
  anchor_idle_deadband_m: 0.8 # idle within this band of the mark (avoids GPS-noise hunting)
  # Vectored / azimuth station-keeping (#35): opt-in. While holding at anchor, swing the
  # motor azimuth up to station_keep_azimuth_deg off the bow (beyond the autopilot's
  # band) to push straight against the set. Off by default; only affects anchor hold.
  station_keep_vectored: false
  station_keep_azimuth_deg: 35.0  # try 110-120 when enabling
  waypoint_throttle: 0.6
  waypoint_arrival_m: 5.0
  waypoint_xte_gain: 2.0
  jog_increment_m: 1.5        # anchor-jog step (~5 ft)
  cruise_kp: 0.64             # Cruise Control (constant speed-over-ground) PID
  cruise_ki: 0.25
  track_min_distance_m: 5.0   # record a breadcrumb every N metres
  drift_kp: 0.5               # Drift mode (controlled drift speed) PID
  drift_ki: 0.25
  drift_default_knots: 0.5

safety:
  max_thrust_slew_per_s: 2.0
  reverse_delay_s: 0.5
  fix_timeout_s: 5.0
  fix_failsafe_enabled: true   # loss-of-fix failsafe; ON by default: coast after the timeout (Settings -> Safety)
  heading_stale_s: 3.0         # stale compass -> coast while a guided mode steers
  depth_stale_s: 10.0          # stale depth -> shallow-water stop treats depth as unknown
  drag_alarm_factor: 2.0
  min_depth_m: 0.0           # cut thrust below this sounded depth (0 = disabled) (#62)
  nogo_lookahead_m: 5.0      # also stop within this distance of a no-go zone (#62)
  rtl_margin_m: 100.0        # warn when range-home nears battery range (#61)
  auto_rtl: false            # if true, auto-engage Return-to-Launch (not just recommend) (#61)
  link_loss_timeout_s: 20.0  # no-UI-client time before the link-loss failsafe (#64)
  link_loss_continue_mission: true  # guided modes keep flying unsupervised (false = anchor-hold); manual ALWAYS stops
  auto_follow_apb: false     # auto-engage Follow-APB when an APB sentence appears (from idle manual only)
                                     # (pocket-the-phone); MANUAL always stops
  # Low-battery thrust-derating ladder (#49): as SoC falls through each
  # [soc_pct, thrust_cap] rung the max applied thrust is capped in steps (a soft
  # derate) BEFORE the lowest stage hands off to RTL. Only ever LOWERS thrust;
  # STOP + all failsafes still take precedence. Set enabled false to disable.
  battery_ladder_enabled: true
  battery_ladder:            # [soc_pct, thrust_cap]; full thrust above the top rung
    - [40.0, 0.7]
    - [25.0, 0.45]
    - [15.0, 0.25]
  battery_rtl_soc_pct: 10.0  # lowest stage: hand off to the existing RTL/failsafe at/below this SoC

battery:
  capacity_ah: 100.0         # pack capacity (amp-hours) (#60)
  nominal_v: 12.0            # nominal terminal voltage
  reserve_pct: 15.0          # usable-charge reserve (%) kept in hand
  draw_tau_s: 20.0           # recent-draw smoothing (s) for range/time estimate
  # INA226 / shunt battery driver (used only when hardware.battery_source: ina226) (#42):
  i2c_bus: 1                 # /dev/i2c-<n> the shunt is on
  i2c_addr: 64              # INA226 I2C address (0x40 = 64 decimal)
  shunt_ohms: 0.001          # shunt resistance (ohms); current = Vshunt / Rshunt
  max_current_a: 80.0        # gauge full-scale current (informational)

server:
  host: 127.0.0.1
  port: 8000
  https_port: 8443           # HTTPS listener (wake-lock/PWA need it); 0 disables
  ssl_certfile: ""           # bring-your-own cert; both empty -> auto-generate a
  ssl_keyfile: ""            #   self-signed CN=vanchor.local into <data_dir>/tls/

hardware:
  enabled: false            # master switch: false = full sim, true = all serial
  gps_port: /dev/ttyUSB0
  compass_port: /dev/ttyUSB1
  motor_port: /dev/ttyUSB2    # serial device path, or i2c:<bus>:<addr> for the helm-Pico tunnel
  baudrate: 4800            # shared fallback; prefer per-device keys below
  # Per-device baud rates. gps_baud is 38400 by default: a 5 Hz GPS sending
  # RMC+GGA (~8200 bit/s) saturates a 4800-baud link and causes ever-growing
  # fix lag. compass_baud / motor_baud default to 4800 (NMEA 0183 standard).
  gps_baud: 38400
  compass_baud: 4800
  motor_baud: 115200          # protocol v2 (CRC lines; match fw VANCHOR_BAUD)
  # Per-device source overrides (null = follow `enabled`). Mix sim + real freely:
  #   gps_source: nmea       # GPS from external NMEA (phone/plotter via nmea_tcp)
  #   motor_source: both     # drive the sim boat AND a real servo (bench testing)
  gps_source: null           # sim | serial | nmea
  compass_source: null       # sim | serial | nmea
  depth_source: null         # sim | nmea
  motor_source: null         # sim | serial | both
  battery_source: null       # sim | ina226 | none (#42; null = sim when simulated, else none)

nmea_tcp:
  enabled: false
  host: 0.0.0.0
  port: 10110

watchdog:                    # external hardware watchdog heartbeat (#44)
  enabled: false             # OFF by default; enable to arm the relay heartbeat
  gpio_pin: 17               # BCM pin wired to the external retriggerable relay driver
  interval_s: 1.0            # min seconds between heartbeat edges (~supervisor rate)
  active_low: false          # invert the electrical level for the relay board's polarity

demo:                        # one-flag demo mode (`vanchor --demo`); default OFF
  enabled: false             # forced sim + seeded moving scenario + DEMO badge
  readonly: false            # pin every client to observer (hosted demo); stop still works
  scenario: route            # route | anchor
  start_lat: 59.8779         # charted demo lake
  start_lon: 12.0293
  weather_preset: lake       # sim weather applied at boot ("" = calm)
"""
