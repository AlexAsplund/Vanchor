"""Black-box / backup / replay cluster extracted from Runtime (issue #77).

The 8 methods that handle the always-on black-box flight recorder, versioned
backup/restore, and debug-session replay live here.  ``SessionService`` holds a
back-reference to ``Runtime`` via ``self._rt`` for shared state that remains on
Runtime (``config``, ``blackbox``, ``debug``, ``replay``, ``state``,
``controller``, ``_now_fn``, ``_link_failsafe_engaged``, ``_depth_map_path``,
``_depth_chart_path``, ``_boat``, ``boats``, ``safety_geometry``, ``prefs``).

Private state ownership:
  ``blackbox``  — stays on Runtime (referenced by TelemetryBuilder + tests).
  ``debug``     — stays on Runtime (used by handle_command, _record_nmea, client_log).
  ``replay``    — stays on Runtime (read by TelemetryBuilder telemetry()).
"""

from __future__ import annotations

import asyncio
import logging
import math
import time

logger = logging.getLogger("vanchor.app")


class SessionService:
    """Black-box / backup / replay cluster -- split out of Runtime."""

    def __init__(self, rt) -> None:
        self._rt = rt   # back-reference to Runtime for shared state

    # ------------------------------------------------------------------ #
    # Always-on black-box flight recorder (#20)
    # ------------------------------------------------------------------ #

    def _build_blackbox(self, cfg) -> None:
        """Construct the black-box recorder and install its governor hook.

        Sizes the ring to hold ``blackbox_window_s`` of low-rate history plus one
        full post-trigger tail. A disabled recorder is a cheap no-op: no ring,
        and the governor hook is not installed (zero hot-path cost)."""
        from ..obs.blackbox import BlackBox

        rt = self._rt
        obs = getattr(cfg, "obs", None)
        if obs is None:  # pragma: no cover - defensive for partial configs
            from ..core.config import ObsConfig

            obs = ObsConfig()
        sample_hz = max(0.01, float(obs.blackbox_sample_hz))
        tick_hz = max(0.01, float(cfg.control.tick_hz))
        window_frames = int(math.ceil(max(0.0, obs.blackbox_window_s) * sample_hz))
        post_frames = int(round(max(0.0, obs.blackbox_post_trigger_s) * tick_hz))
        rt.blackbox = BlackBox(
            cfg.data_dir,
            enabled=bool(obs.blackbox_enabled),
            capacity=window_frames + post_frames + 8,
            sample_period_s=1.0 / sample_hz,
            post_trigger_frames=post_frames,
            now_fn=rt._now_fn,
        )
        self._install_blackbox_hook()

    def _install_blackbox_hook(self) -> None:
        """Wrap the safety governor's ``govern`` so every control tick feeds the
        black box the DESIRED (pre-governor) and APPLIED (post-governor) command
        plus the resulting alarms. The wrapper returns the governor's result
        bit-for-bit and swallows any recorder error, so it can NEVER change or
        break the governed command -- it only observes."""
        rt = self._rt
        bb = rt.blackbox
        if not bb.enabled:
            return
        gov = rt.controller.safety
        orig_govern = gov.govern
        state = rt.state
        runtime = rt

        def govern(command, *args, **kwargs):
            applied, status = orig_govern(command, *args, **kwargs)
            bb.observe(
                command,
                applied,
                status,
                state,
                controller_fault=state.controller_fault is not None,
                link_failsafe=runtime._link_failsafe_engaged,
            )
            return applied, status

        gov.govern = govern

    # ------------------------------------------------------------------ #
    # Black-box flight recorder (#20) -- read API for the UI
    # ------------------------------------------------------------------ #

    def blackbox_dumps(self) -> dict:
        """List recent black-box dump files (newest first) + whether it's on."""
        rt = self._rt
        return {"enabled": rt.blackbox.enabled, "dumps": rt.blackbox.dumps()}

    def blackbox_path_for(self, file_name: str) -> str | None:
        """Resolve a dump file name to a safe on-disk path (or ``None``)."""
        return self._rt.blackbox.path_for(file_name)

    # ------------------------------------------------------------------ #
    # Versioned backup / restore of all persistent state
    # ------------------------------------------------------------------ #

    def create_backup(self, client: dict | None = None, *, created_at: str | None = None) -> bytes:
        """Build a versioned backup ZIP of this runtime's ``data_dir`` (boats,
        depth map, devices, trips) plus the UI's ``client`` localStorage slice.

        ``created_at`` is an ISO8601 string the caller supplies (the endpoint
        passes the request time); when omitted we use the injected clock to make
        a UTC timestamp -- the backup module itself never calls ``datetime.now``.
        Returns the raw ``.zip`` bytes."""
        from ..core import backup

        rt = self._rt
        if created_at is None:
            created_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(rt._now_fn()))
        return backup.create_backup(
            rt.config.data_dir, client=client, created_at=created_at
        )

    def restore_backup(self, zip_bytes: bytes) -> dict:
        """Restore a backup ZIP into ``data_dir`` and reload what it can LIVE.

        Extracts the archive (overwriting the on-disk files), then refreshes the
        in-memory state it can without a restart: re-loads the boat profiles +
        the depth map from disk and re-applies the active profile, and reloads
        the device config. Anything that can't be refreshed live sets
        ``restart_required``. Returns the backup-module result dict plus
        ``restart_required``. Raises :class:`ValueError` (-> 400) on a bad zip."""
        from ..core import backup
        from ..nav.depth import DepthMap

        rt = self._rt
        result = backup.restore_backup(rt.config.data_dir, zip_bytes)
        restart_required = False

        # Boat profiles: rebuild the store from the restored boats.json and
        # re-apply the active profile so the live physics follow it.
        try:
            from ..core.boat_profiles import BoatProfileStore

            rt.boats = BoatProfileStore(rt.config.data_dir)
            active = rt.boats.active()
            if active is not None:
                rt._boat._apply_boat_specs(active["specs"])
            # Per-boat gains (#31) live in a sidecar; reload + re-apply too.
            rt._boat._boat_gains = rt._boat._load_boat_gains()
            rt._boat._apply_active_boat_gains()
        except Exception:  # pragma: no cover - defensive
            logger.exception("restore: reloading boat profiles failed")
            restart_required = True

        # Depth map: reload the restored soundings from disk.
        try:
            rt.depth_map = DepthMap()
            rt.depth_map.load(rt._depth_map_path, rt._depth_chart_path)
            rt._depth_saved_n = len(rt.depth_map.points)
        except Exception:  # pragma: no cover - defensive
            logger.exception("restore: reloading depth map failed")
            restart_required = True

        # Safety geometry (#23): rebuild the store from the restored safety.json
        # and re-apply it to the live governor + refresh prefs.
        try:
            from ..core.prefs import PrefsStore, SafetyGeometryStore

            rt.safety_geometry = SafetyGeometryStore(rt.config.data_dir)
            rt._apply_safety_geometry()
            rt.prefs = PrefsStore(rt.config.data_dir)
        except Exception:  # pragma: no cover - defensive
            logger.exception("restore: reloading safety geometry failed")
            restart_required = True

        # Device config: re-read the restored devices.json into the live config
        # and rebuild the device set (no restart). reload_devices is async, so
        # schedule it; if there's no running loop, defer to a restart.
        from ..core.config import apply_device_overrides
        apply_device_overrides(rt.config)
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(rt.reload_devices())
        except RuntimeError:
            # No event loop (e.g. a synchronous restore in a test) -> the new
            # device config will take effect on the next start/restart.
            restart_required = True

        result["restart_required"] = restart_required
        logger.info("backup restored (restart_required=%s)", restart_required)
        return result

    # ------------------------------------------------------------------ #
    # Debug session recording + replay
    # ------------------------------------------------------------------ #

    def start_replay(self, file_name: str) -> bool:
        rt = self._rt
        path = rt.debug.path_for(file_name)
        if path is None:
            return False
        return rt.replay.load(path, time.time())

    def stop_replay(self) -> None:
        self._rt.replay.stop()
