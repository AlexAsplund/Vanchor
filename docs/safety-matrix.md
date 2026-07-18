# Safety matrix — the safety floor and the tests that prove it

This is the authoritative map of the vanchor-ng **safety floor**: for each
failure mode, which layer detects/acts on it, what the boat does as a result,
and the executable test that proves the behaviour. It is the human-readable
companion to `tests/test_chaos.py` (fault-injection) plus the existing
`tests/test_safety.py`, `tests/test_staleness.py`, `tests/test_safety_power.py`
and `tests/test_serial_devices.py`.

The non-negotiable invariant behind every row: **a fault must leave the boat
motionless (commanded thrust ~0) or holding station — never running away, and
STOP must always work.** File:line citations below are against the tree at the
time of writing (`fix/review-2026-07`).

## Defence-in-depth layers

| Layer | Where | Role |
| --- | --- | --- |
| Firmware serial watchdog | `firmware/engine/engine.ino:285-287`, `firmware/common/vanchor_protocol.h:97-98` (`VANCHOR_WATCHDOG_MS 800`) | No valid `CMD` for 800 ms → ramp motor to neutral (does not flip direction, just stops). The last line of defence when software above it is dead. |
| Supervised controller loop | `src/vanchor/controller/controller.py:803-836` (`_tick_once`) | Any exception in a mode/helm/governor/motor is caught; the motor is best-effort zeroed and `state.controller_fault` is set; the loop survives. |
| Safety governor | `src/vanchor/controller/safety.py:213-379` (`govern`) | Pure per-tick filter between helm and motor: staleness coast, shallow/no-go stop, loss-of-fix coast, reverse interlock, thrust/steer slew, drag alarm. |
| Serial motor driver | `src/vanchor/hardware/serial_devices.py:379-505` | Reverse-delay interlock at the wire; write-while-down is dropped (never raised); read supervisor reconnects across unplug. |
| App-level failsafes | `src/vanchor/app.py:1143-1181` | Link-loss failsafe (guided → anchor-hold, manual-with-thrust → stop), keyed off the injected monotonic clock. |

## Failure-mode matrix

| # | Failure mode | Detecting / acting layer | Resulting boat behaviour | Proving test (`file::test`) |
| --- | --- | --- | --- | --- |
| 1 | Pi process crash / hang | Firmware serial watchdog (`engine.ino:285-287`) | `CMD` stops arriving → after 800 ms firmware ramps motor to neutral (stops). Direction is held, not flipped. | **Not software-testable in this repo** — firmware behaviour on the Arduino. See "Not covered". |
| 2 | Controller tick exception (a mode's `update` raises) | Supervised loop (`controller.py:803-836`) | Exception caught, does not kill the loop; motor best-effort zeroed; `state.controller_fault` set; a later clean tick clears it. | `tests/test_chaos.py::test_controller_tick_exception_zeroes_motor` |
| 3 | GPS fix loss | Governor loss-of-fix failsafe (`safety.py:285-296`), ON by default (`safety.py:71`) | After `fix_timeout_s` (3 s) without a fresh fix, thrust forced to 0 → boat coasts rather than steaming blind. | `tests/test_chaos.py::test_gps_fix_loss_forces_coast_default_on`; also `tests/test_safety.py::test_fix_lost_after_timeout_forces_zero`, `tests/test_staleness.py::test_fix_failsafe_on_by_default_in_config` |
| 4 | Compass loss / freeze in a guided mode | Governor staleness coast (`safety.py:243-260`), fed by `controller._sensor_ages` (`controller.py:368-378`) | Heading age > `heading_stale_s` (3 s) while NOT manual → thrust 0, steering head held; recovers when a fresh heading arrives. Manual driving unaffected. | `tests/test_chaos.py::test_compass_silence_coasts_in_heading_hold`; also `tests/test_staleness.py::test_heading_stale_in_heading_hold_forces_zero_then_recovers` |
| 5 | Depth sounder freeze | Governor shallow-stop staleness rule (`safety.py:262-278`) | A STALE sounding (age > `depth_stale_s` 10 s) is treated as UNKNOWN: the shallow-stop neither false-stops on a frozen deep value nor trusts a frozen shallow value. A *fresh* shallow reading still stops. | `tests/test_chaos.py::test_depth_freeze_treated_as_unknown_by_shallow_stop` |
| 6a | UI link loss while GUIDED | App link failsafe (`app.py::evaluate_link_failsafe`) | After `link_loss_timeout_s` with no client while underway: **continue the mission unsupervised** (default, `link_loss_continue_mission: true` — a locked phone must not park an active route; geofence/depth/battery failsafes still apply), or `anchor_hold` (hold position) when set to `false`. | `tests/test_chaos.py::test_link_loss_guided_continues_mission_by_default`, `::test_link_loss_guided_engages_anchor_hold_when_opted_out`; also `tests/test_safety_power.py::test_link_failsafe_engages_after_timeout` |
| 6b | UI link loss while DRIVING MANUALLY | App link failsafe (`app.py:1172-1175`) | Manual + thrust above eps counts as underway (`_underway`, `app.py:1143-1153`) → link loss issues `stop` (mode stays MANUAL, thrust zeroed). No target to hold to, so cut the motor. | `tests/test_chaos.py::test_link_loss_manual_driving_stops`; also `tests/test_safety_power.py::test_link_failsafe_stops_manual_driving` |
| 7 | Serial device unplug | Read supervisor reconnect (`serial_devices.py:128-194`) + `flush` drop-on-down (`serial_devices.py:425-452`) | EOF/read error → close + exponential-backoff reopen forever; a write while the link is down is dropped (marks unhealthy, never raises). Firmware watchdog neutrals the motor in the gap. | `tests/test_chaos.py::test_serial_motor_eof_reconnects_and_flush_survives_down_link`; also `tests/test_serial_devices.py::test_sensor_reconnects_after_eof_and_resumes` |
| 8 | Rapid thrust reversal (incl. through-zero) | Governor reverse interlock (`safety.py:298-323`, sticky `_last_applied_dir`) AND serial driver interlock (`serial_devices.py:468-505`) | A sign flip — even one that passes through a zero-thrust tick — is held at 0 until thrust has rested near zero for the reverse delay, at BOTH layers. | `tests/test_chaos.py::test_through_zero_reversal_gated_in_governor`, `::test_through_zero_reversal_gated_in_serial_driver`; also `tests/test_safety.py::test_reverse_blocked_right_after_forward_then_allowed_after_delay`, `tests/test_serial_devices.py::test_reverse_delay_not_bypassed_through_zero` |
| 9 | Shallow water / no-go zone approach | Governor shallow-stop + geofence lookahead (`safety.py:262-283`, `_in_or_near_nogo:185-211`) | Fresh depth < `min_depth_m` → thrust 0; inside or within `nogo_lookahead_m` of a no-go polygon → thrust 0 (stops BEFORE entering). | `tests/test_chaos.py::test_shallow_water_and_nogo_lookahead_stop`; also `tests/test_safety_power.py::test_shallow_water_cuts_thrust`, `::test_nogo_lookahead_stops_before_entering` |
| 10 | Anchor drag | Governor drag alarm (`safety.py:356-371`) | In ANCHOR_HOLD/ANCHOR_ML, drift beyond `drag_alarm_factor` × radius raises `status.drag_alarm` (WORK_AREA excluded to avoid false trips while travelling). | `tests/test_chaos.py::test_anchor_drag_raises_alarm` |
| 13 | Passive anchor alarm — boat outside the armed alarm circle | App-level watcher (`core/anchor_alarm.py::AnchorAlarmWatcher.evaluate`, called from `Runtime._supervise_once`) | Alarm only (banner + high-severity sound + telemetry `anchor_alarm.firing`); **NO motor action**; optional one-tap recover engages `anchor_hold` at the alarm point via the standard command path (all failsafes apply). `AnchorAlarmWatcher` holds no reference to controller/helm/governor/motor. | `tests/test_anchor_alarm.py::test_passive_watch_never_touches_motor_or_mode` |
| 11 | Wall-clock step (NTP) mid-session | Monotonic timers throughout (governor accumulates `dt`; link failsafe reads `_mono_fn`, `app.py:1155-1181`) | A wall-clock jump can neither prematurely engage nor indefinitely defer a failsafe — only monotonic elapsed time matters. | `tests/test_chaos.py::test_wall_clock_step_does_not_disturb_link_failsafe` |
| 12 | STOP command from every mode | Controller `stop` handler (`controller.py:631-634`) via WS + POST (`ui/server.py:281-283`) | `stop` clears paused nav, sets manual (0,0), switches to MANUAL. Boat comes to rest; works from every motor-engaging mode and over both transports. | `tests/test_chaos.py::test_stop_from_every_mode_zeroes_and_goes_manual[*]` (11 modes), `::test_stop_via_post_api_goes_manual_zero` |

## Known gaps

Gaps found while writing the tests, kept visible rather than silently worked
around. None is a new regression — each is a documented design boundary.

- **Frozen shallow sounder does not stop the boat (row 5).** By design, a depth
  sounding that has gone stale (age > `depth_stale_s`) is treated as UNKNOWN, so
  the shallow-water stop will NOT act on the last (possibly shallow) reading
  (`safety.py:268-273`). This is deliberate — a frozen value is not trusted —
  but it means a sounder that freezes *while over genuinely shallow water* will
  not by itself cut thrust; the boat relies on the no-go geofence, the operator,
  and GPS-based limits instead. Encoded (and asserted as the current behaviour)
  in `test_depth_freeze_treated_as_unknown_by_shallow_stop`. No `xfail` needed —
  the behaviour is intentional and covered — but flagged here as a real
  coverage boundary of the shallow-stop.
- **Link-loss detection cadence is not in the safety floor unit.** The DIRECT
  `evaluate_link_failsafe` method is proven here; the *scheduling* of it (which
  task calls it, how often) lives in `app.py`'s supervisor/telemetry path and is
  being refactored concurrently, so it is deliberately not pinned by these
  tests. The floor guarantee (given a call, the correct action fires past the
  timeout on the monotonic clock) is what these tests lock down.

## Residual risks / not covered

- **Pi hard-hang with the motor supply live (row 1).** The only backstop is the
  firmware serial watchdog (800 ms → neutral). If the firmware or the motor
  supply itself is compromised, nothing above it can help. A hardware watchdog
  chain (independent MCU / relay that cuts motor power on a heartbeat loss) is
  **roadmap item 44** and is the correct fix; it is not implemented yet. Not
  software-testable in this repo.
- **Firmware behaviour is untested here.** Rows 1 and the wire-level half of
  row 8 depend on `firmware/engine/engine.ino` running on the Arduino; there is
  no HIL rig in this repo, so those are asserted only at the Python boundary
  (command stops being written / the driver-side interlock).
- **Single-tack crab bias in declination learning.** The compass-declination
  learner can acquire a heading bias if the boat only ever runs one tack; this
  is a slow accuracy drift, not a runaway, but it is a known un-modelled error
  source for guided heading hold.
- **Boot-time-absent serial port needs a manual reload.** The read supervisor
  reconnects across a *drop* (unplug/replug) once the device has been opened,
  but a port that is absent at process start (never opened) is a separate path
  that currently needs a manual driver reload; auto-probing a not-yet-present
  port at boot is not implemented.
- **Depth staleness vs. shallow-stop trade-off** (see Known gaps, row 5).
