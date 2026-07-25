"""Boat-profile / specs / gains cluster extracted from Runtime (issue #72).

All 18 methods that handle boat specs, named boat profiles, and per-boat
controller gains live here.  ``BoatSetup`` holds a back-reference to
``Runtime`` via ``self._rt`` for shared state that remains on Runtime
(config, state, bus, boats, controller, simulator, navigator, etc.).

Private state owned by this cluster and moved here:
  ``_boat_gains``      — per-profile gains dict (keyed by profile id)
  ``_boat_gains_path`` — on-disk path for ``boat_gains.json``
"""

from __future__ import annotations

import json
import logging
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

logger = logging.getLogger("vanchor.app")


class BoatSetup:
    """Boat profile specs, named profiles, and per-boat gains -- split out of Runtime."""

    def __init__(self, rt) -> None:
        self._rt = rt   # back-reference to Runtime for shared state
        # Per-boat saved gain profiles (#31): private state that is only accessed
        # by the 18 methods in this cluster.
        self._boat_gains_path: str = os.path.join(rt.config.data_dir, "boat_gains.json")
        self._boat_gains: dict = self._load_boat_gains()

    # ------------------------------------------------------------------ #
    # Boat profile (specs snapshot)
    # ------------------------------------------------------------------ #

    def boat_profile(self) -> dict:
        rt = self._rt
        b = rt.config.boat
        return {
            "length_m": b.length_m,
            "beam_m": b.beam_m,
            "mass_kg": b.mass_kg,
            "max_speed_mps": b.max_speed_mps,
            "max_thrust_n": b.max_thrust_n,
            "thruster_mount": b.thruster_mount,
            "thruster_offset_m": b.thruster_offset_m,
            "thruster_y_m": b.thruster_y_m,
            "thrust_yaw_ff": b.thrust_yaw_ff,
            "thrust_yaw_ff_trim": b.thrust_yaw_ff_trim,
            "max_steer_angle_deg": b.max_steer_angle_deg,
            "max_turn_rate_deg": b.max_turn_rate_deg,
            "hull_tracking": b.hull_tracking,
            "shaft_dia_mm": b.shaft_dia_mm,
            "steer_range_deg": b.steer_range_deg,
            "steer_reduction": b.steer_reduction,
            "sonar_cone_deg": b.sonar_cone_deg,
            # The currently-active named profile (#75) so the UI can highlight it.
            "active_boat_id": getattr(rt, "boats", None) and rt.boats.active_id,
        }

    def _apply_boat_specs(self, specs: dict) -> None:
        """Write a spec dict onto ``config.boat`` and make every live-applicable
        field take effect on the running sim/controller.

        Numeric/string fields are coerced to ``BoatConfig``'s declared types.
        After writing the config we *rebuild* the simulator's physics params via
        :func:`_build_boat_params` (not just poke ``max_speed_mps``) so changing
        mass, thrust, geometry etc. actually changes the boat's behaviour -- the
        Fossen model derives its damping + mass matrices from these at build
        time, so an in-place tweak alone would be ignored.
        """
        from ..core.models import ControlModeName
        from ..runtime.builders import _thrust_yaw_ff_norm

        rt = self._rt
        b = rt.config.boat
        for key, val in specs.items():
            if hasattr(b, key) and val is not None:
                cur = getattr(b, key)
                setattr(b, key, type(cur)(val) if isinstance(cur, (int, float)) else val)

        # Steering authority / slew limits + hull-character control tuning.
        rt.state.max_steer_angle_deg = b.max_steer_angle_deg
        # The hull character (directional stability) biases the AUTOPILOT TUNING,
        # so a boat starts sensibly tuned even on real hardware (where it can't
        # change the physics): a stiff, tracking hull (high hull_tracking) resists
        # turning -> use MORE steering authority and less command smoothing; a
        # loose, skittish hull -> LESS authority and more smoothing to avoid
        # hunting. This is a PRIOR -- the auto-calibration drive then measures the
        # real boat and refines from here. At hull_tracking == 1.0 it is a no-op.
        ht = min(3.0, max(0.25, b.hull_tracking))
        if b.max_steer_angle_deg > 0:
            rt.controller.safety.config.max_steer_slew_per_s = (
                b.max_steer_rate_dps / b.max_steer_angle_deg
            )
            auth_deg = min(b.autopilot_steer_deg * ht, b.max_steer_angle_deg)
            rt.controller.helm.autopilot_steer_scale = auth_deg / b.max_steer_angle_deg
        rt.controller.helm.steer_tau = (
            rt.config.control.steer_tau * min(1.8, max(0.6, ht ** -0.5))
        )
        # A bow vs stern mount flips which way a steering deflection turns the
        # boat -- keep the helm's sign in step so switching profiles never leaves
        # the autopilot steering backwards.
        rt.controller.helm.steer_sign = 1.0 if b.thruster_x_m() >= 0 else -1.0
        # The learned anchor mode mirrors the mount sign too (the Helm still owns
        # the actual command flip; the mode uses this for mount awareness +
        # telemetry) -- keep it in step on every profile change.
        ml = rt.controller.modes.get(ControlModeName.ANCHOR_ML)
        if ml is not None and hasattr(ml, "steer_sign"):
            ml.steer_sign = rt.controller.helm.steer_sign
        # "Leif" (pure full-azimuth learned mode) mirrors the mount sign too. Both
        # learned modes rescale their wide-azimuth steering to the boat's mechanical
        # range live from state.max_steer_angle_deg, so no azimuth sync is needed.
        leif = rt.controller.modes.get(ControlModeName.ANCHOR_LEIF)
        if leif is not None and hasattr(leif, "steer_sign"):
            leif.steer_sign = rt.controller.helm.steer_sign
        # Lateral-offset thrust-yaw feed-forward follows the geometry/trim live so
        # changing the offset (or the calibrated trim) updates compensation now.
        rt.controller.helm.thrust_yaw_ff = _thrust_yaw_ff_norm(rt.config)
        # Anchor mode caps thrust by the boat's top speed; keep it in step. The
        # vectored station-keeping law (#35) also mirrors the mount polarity so
        # a profile switch can't leave its azimuth mirrored.
        anchor = rt.controller.modes.get(ControlModeName.ANCHOR_HOLD)
        if anchor is not None and hasattr(anchor, "config"):
            anchor.config.boat_max_speed_mps = b.max_speed_mps
            anchor.config.steer_sign = rt.controller.helm.steer_sign
        # Waypoint mode's forward/reverse decision needs the boat's measured
        # speed, reverse efficiency, and turn rate.
        wp = rt.controller.modes.get(ControlModeName.WAYPOINT)
        if wp is not None and hasattr(wp, "config"):
            wp.config.reverse_efficiency = b.reverse_efficiency
            wp.config.turn_rate_dps = b.max_turn_rate_deg
            wp.config.boat_speed_mps = b.max_speed_mps

        # Rebuild the live physics params so mass/thrust/geometry changes bite.
        self._rebuild_boat_physics()

    def _rebuild_boat_physics(self) -> None:
        """Swap the simulator boat's physics params for freshly-built ones.

        The Fossen model precomputes its mass + damping matrices (and the
        derived surge drag / yaw inertia) in ``__post_init__``/``_build_matrices``
        from the params, so we replace ``params`` wholesale and re-derive rather
        than mutating fields in place. The simple kinematic model has no derived
        state, so swapping the dataclass is enough."""
        from ..runtime.builders import _build_boat_params

        rt = self._rt
        if rt.simulator is None:
            return
        boat = rt.simulator.boat
        params = _build_boat_params(rt.config)
        boat.params = params
        # Re-derive the Fossen matrices for the new params (no-op for "simple").
        rebuild = getattr(boat, "_build_matrices", None)
        if callable(rebuild):
            rebuild()

    def update_boat(self, fields: dict) -> dict:
        """Update the boat profile and apply what can change live.

        Also persists the change back into the active named profile (#75) so the
        existing ``POST /api/boat`` path and the profile store stay in sync."""
        rt = self._rt
        self._apply_boat_specs(fields)
        # Write the edited specs back into the active profile so they persist.
        if getattr(rt, "boats", None) is not None:
            from ..core.boat_profiles import specs_from_boat

            rt.boats.save(rt.boats.active_id, None, specs_from_boat(rt.config.boat))
        logger.info("boat profile updated: %s", fields)
        return self.boat_profile()

    # ------------------------------------------------------------------ #
    # Named boat profiles (#75)
    # ------------------------------------------------------------------ #

    def boat_profiles_list(self) -> dict:
        """``{active_id, profiles:[{id,name,...specs}, ...]}``."""
        return self._rt.boats.to_dict()

    def boat_profiles_create(self, name: str, specs: dict | None = None) -> dict:
        """Create a profile (specs default to the current active boat). Returns
        ``{id, ...}`` of the new profile."""
        from ..core.boat_profiles import specs_from_boat

        rt = self._rt
        if specs is None:
            specs = specs_from_boat(rt.config.boat)
        pid = rt.boats.create(name, specs)
        return rt.boats.get(pid) or {"id": pid}

    def boat_profiles_update(
        self, profile_id: str, name: str | None = None, specs: dict | None = None
    ) -> dict | None:
        """Update a profile's name/specs. If the edited profile is the active
        one, also apply the new specs live. Returns the updated profile or None
        if the id is unknown."""
        rt = self._rt
        if not rt.boats.save(profile_id, name, specs):
            return None
        if profile_id == rt.boats.active_id:
            active = rt.boats.active()
            if active is not None:
                self._apply_boat_specs(active["specs"])
            self._apply_active_boat_gains()
        return rt.boats.get(profile_id)

    def boat_profiles_activate(self, profile_id: str) -> dict | None:
        """Make a profile active and apply its specs to the live sim. Returns
        the applied boat profile dict, or None if the id is unknown."""
        rt = self._rt
        if not rt.boats.set_active(profile_id):
            return None
        active = rt.boats.active()
        if active is not None:
            self._apply_boat_specs(active["specs"])
        # Apply this profile's saved gains on top of its specs (else keep current).
        self._apply_active_boat_gains()
        logger.info("activated boat profile %s", profile_id)
        return self.boat_profile()

    def boat_profiles_delete(self, profile_id: str) -> bool:
        """Delete a profile (refuses the last one). If the deleted profile was
        active, apply whatever profile is active afterwards."""
        rt = self._rt
        if not rt.boats.delete(profile_id):
            return False
        active = rt.boats.active()
        if active is not None:
            self._apply_boat_specs(active["specs"])
        self._apply_active_boat_gains()
        return True

    # ------------------------------------------------------------------ #
    # Per-boat saved gain profiles (#31)
    # ------------------------------------------------------------------ #

    def _load_boat_gains(self) -> dict:
        """Read the per-profile gains sidecar (``boat_gains.json``); ``{}`` when
        absent or unreadable, so a missing/corrupt file never breaks startup."""
        try:
            with open(self._boat_gains_path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save_boat_gains_file(self) -> None:
        """Persist the per-profile gains map atomically."""
        rt = self._rt
        os.makedirs(rt.config.data_dir, exist_ok=True)
        tmp = self._boat_gains_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self._boat_gains, fh, indent=2)
        os.replace(tmp, self._boat_gains_path)

    def current_gains(self) -> dict:
        """Snapshot the live controller gains as a gains block (the shape stored
        per boat profile). Covers the helm heading PID, anchor/cruise/drift gains
        and the steering-gain schedule."""
        from ..core.models import ControlModeName

        c = self._rt.controller
        block: dict = {
            "heading": {
                "kp": c.helm.pid.kp,
                "ki": c.helm.pid.ki,
                "kd": c.helm.pid.kd,
            },
            "cruise": {"kp": c.cruise_pid.kp, "ki": c.cruise_pid.ki},
        }
        anchor = c.modes.get(ControlModeName.ANCHOR_HOLD)
        if anchor is not None and hasattr(anchor, "config"):
            block["anchor"] = {
                "kp": anchor.config.kp,
                "kd": anchor.config.kd,
                "idle_deadband_m": anchor.config.idle_deadband_m,
            }
        drift = c.modes.get(ControlModeName.DRIFT)
        if drift is not None and hasattr(drift, "pid"):
            block["drift"] = {"kp": drift.pid.kp, "ki": drift.pid.ki}
        sched = c.helm.gain_schedule
        if sched is not None:
            block["steer_schedule"] = {
                "sog_lo_kn": sched.sog_lo_kn,
                "sog_hi_kn": sched.sog_hi_kn,
                "mult_lo": sched.mult_lo,
                "mult_hi": sched.mult_hi,
                "mult_min": sched.mult_min,
                "mult_max": sched.mult_max,
            }
        return block

    def _apply_gains_block(self, gains: dict) -> None:
        """Apply a (partial) gains block to the live controllers. Missing
        sections/fields are left untouched, so a profile can carry just the gains
        it cares about."""
        from ..core.models import ControlModeName

        if not isinstance(gains, dict):
            return
        c = self._rt.controller
        h = gains.get("heading")
        if isinstance(h, dict):
            for attr in ("kp", "ki", "kd"):
                if h.get(attr) is not None:
                    setattr(c.helm.pid, attr, float(h[attr]))
            c.helm.pid.reset()
        a = gains.get("anchor")
        anchor = c.modes.get(ControlModeName.ANCHOR_HOLD)
        if isinstance(a, dict) and anchor is not None and hasattr(anchor, "config"):
            for attr in ("kp", "kd", "idle_deadband_m"):
                if a.get(attr) is not None:
                    setattr(anchor.config, attr, float(a[attr]))
        cr = gains.get("cruise")
        if isinstance(cr, dict):
            for attr in ("kp", "ki"):
                if cr.get(attr) is not None:
                    setattr(c.cruise_pid, attr, float(cr[attr]))
            c.cruise_pid.reset()
        dr = gains.get("drift")
        drift = c.modes.get(ControlModeName.DRIFT)
        if isinstance(dr, dict) and drift is not None and hasattr(drift, "pid"):
            for attr in ("kp", "ki"):
                if dr.get(attr) is not None:
                    setattr(drift.pid, attr, float(dr[attr]))
            drift.pid.reset()
        s = gains.get("steer_schedule")
        sched = c.helm.gain_schedule
        if isinstance(s, dict) and sched is not None:
            for attr in ("sog_lo_kn", "sog_hi_kn", "mult_lo", "mult_hi",
                         "mult_min", "mult_max"):
                if s.get(attr) is not None:
                    setattr(sched, attr, float(s[attr]))
        logger.info("applied boat gains: %s", gains)

    def _apply_active_boat_gains(self) -> None:
        """Apply the active profile's saved gains (if any) to the controllers."""
        rt = self._rt
        if getattr(rt, "boats", None) is None:
            return
        gains = self._boat_gains.get(rt.boats.active_id)
        if gains:
            self._apply_gains_block(gains)

    def save_boat_gains(self, profile_id: str | None = None) -> dict:
        """Persist the CURRENTLY-applied controller gains into a boat profile
        (defaults to the active one), closing the "persist applied gains back to
        a config file" debt. Returns the saved gains block."""
        rt = self._rt
        if getattr(rt, "boats", None) is None:
            return {}
        pid = profile_id or rt.boats.active_id
        block = self.current_gains()
        self._boat_gains[pid] = block
        self._save_boat_gains_file()
        logger.info("saved applied gains into boat profile %s", pid)
        return block

    def boat_gains(self, profile_id: str | None = None) -> dict:
        """Return the saved gains block for a profile (active one by default), or
        ``{}`` if that profile carries none."""
        rt = self._rt
        pid = profile_id or (
            rt.boats.active_id if getattr(rt, "boats", None) is not None else ""
        )
        return dict(self._boat_gains.get(pid, {}))

    def apply_tuned_gains(self, job: str, params: dict, *, persist: bool = False) -> None:
        """Apply auto-tuned gains to the live controller (used by /api/tune).

        With ``persist=True`` the tuned gains are ALSO written into the active
        boat profile's saved gains (``boat_gains.json``), closing the "persist
        applied gains back to a config file" debt. It defaults to ``False`` so
        the existing ``POST /api/tune`` behaviour (live-apply only) is unchanged.
        """
        from ..core.models import ControlModeName

        c = self._rt.controller
        if job == "heading":
            c.helm.pid.kp = float(params["heading_kp"])
            c.helm.pid.kd = float(params["heading_kd"])
            c.helm.pid.reset()
        elif job == "cruise":
            c.cruise_pid.kp = float(params["kp"])
            c.cruise_pid.ki = float(params["ki"])
            c.cruise_pid.reset()
        elif job == "drift":
            pid = c.modes[ControlModeName.DRIFT].pid
            pid.kp = float(params["kp"])
            pid.ki = float(params["ki"])
            pid.reset()
        elif job == "anchor":
            cfg = c.modes[ControlModeName.ANCHOR_HOLD].config
            cfg.kp = float(params["kp"])
            cfg.kd = float(params["kd"])
            cfg.idle_deadband_m = float(params["idle_deadband_m"])
        logger.info("applied tuned %s gains live: %s", job, params)
        if persist:
            self._persist_tuned_gains(job, params)

    def _persist_tuned_gains(self, job: str, params: dict) -> None:
        """Merge an auto-tuned job's gains into the active boat profile's saved
        gains (only the section that job tuned) and persist them."""
        rt = self._rt
        if getattr(rt, "boats", None) is None:
            return
        from ..analysis.tuning import gains_block_from_tuning

        frag = gains_block_from_tuning(job, params)
        if not frag:
            return
        pid = rt.boats.active_id
        block = dict(self._boat_gains.get(pid, {}))
        block.update(frag)  # replace only the tuned section(s)
        self._boat_gains[pid] = block
        self._save_boat_gains_file()
        logger.info("persisted tuned %s gains into boat profile %s", job, pid)
