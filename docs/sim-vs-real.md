# Simulation vs Real Hardware — fidelity review (2026-07-15)

A systematic audit of how closely the simulator matches the real boat, per
subsystem: actuation, sensors/timing, and controls/config. Three findings
categories: **verified parity**, **gaps fixed by this review**, and **known
gaps** (documented; fix needs bench/water data or would invalidate trained
artifacts).

The architectural guarantee held up well: the controller steers only on
*perceived* sensor state, the UI/command path is byte-identical in both
worlds, and every sim-only tool (teleport, weather, battery setter, fault
injection) is properly gated behind `simulator is not None`.

---

## Verified parity (no action needed)

| Area | Notes |
|---|---|
| Control loop + governor | Same code, same 5 Hz cadence, same slew limits (thrust 2.0/s, steering 95°/s-equivalent) applied to COMMANDS in both worlds. |
| All software failsafes | Fix-loss, shallow stop, no-go, heading-stale, drag alarm, battery ladder, RTL, link loss, land guard: identical governor/supervisor code paths, all exercisable in sim (chaos suite pins them). |
| Calibration + auto-tune | `CalibrationRunner` and the tuners are hardware-agnostic; same persistence. |
| Learned anchor policies | `steer_sign` and azimuth scaling read from config/model identically in both paths; no sim/real fork. |
| Fossen physics | Audited against Fossen's reference implementation earlier (2026-07-09); missing current-rotation term fixed then. |
| Phone GPS sparseness | The ~6 s browser fix cadence vs the 3 s fix-failsafe is bridged by the device's 1 Hz reissue loop (end-to-end test pins the recorded field cadence). |
| Magnetic declination | Sim compass reports true heading with declination pinned to 0 (a zero-declination world); real compass reports magnetic + navigator applies declination. Downstream identical — intentional. |
| GPS offset on sim | "Adjust my position" TELEPORTS the sim boat instead of installing an offset (a bias-free sim GPS has nothing to correct; an offset would displace chart-relative behaviours — fixed earlier today). Real GPS keeps normal offset calibration. Intentional fork, documented at the code site. |

## Gaps FIXED by this review

1. **Sim motor now feels like the firmware by default.** The real engine
   firmware unconditionally applies `THROTTLE_SLEW_PER_S = 1.0` and a
   1000 ms reverse dead-time (`engine.ino`); the sim's actuation shaping
   existed but defaulted OFF. `SimMotorConfig` now defaults to
   `thrust_slew_per_s: 1.0`, `reverse_delay_s: 1.0` — the sim boat
   accelerates and reverses like the real one. Set the fields to `0` to
   restore the legacy instant motor (older recorded scenarios and tuned
   gains were captured with instant actuation).
2. **Firmware watchdog simulated.** If the control loop stops commanding the
   (sim) motor for >0.8 s, thrust goes neutral and the head holds — mirroring
   `VANCHOR_WATCHDOG_MS`. Previously the sim boat would drive forever on a
   stale command; the real one coasts within a second.
3. **Wire quantization simulated.** Thrust now quantizes to 8-bit PWM
   (1/255) and steering to the CMD line's integer 1/100 steps, always — the
   real boat can only realize those values. (The v2.1 split `STEERD` channel
   is finer, 0.1°; the sim models the coarser legacy CMD as the worst case.)
4. **`time_scale` no longer starves the sensors.** Sim sensor cadences are
   now per SIM-second (scaled by `time_scale`), so a sped-up sim keeps the
   same fixes-per-boat-second as reality. The CONTROL LOOP still runs
   wall-clock, so `time_scale != 1` remains a visualization tool — never a
   yardstick for control quality (see the recording-rig note in
   `scripts/record_guide.py`).
5. **Sim GPS rate matches the M9N.** `sensors.gps_hz` default 5 → 10 Hz,
   matching the marine configuration the u-blox driver programs
   (`cfg_marine_10hz`).

## Known gaps — documented, not fixed (and why)

| Gap | Severity | Why not fixed / what to do |
|---|---|---|
| **Reverse dead-time: training env vs deployment.** The ES training env models steering slew (`--steer-rate-dps`) and action-rate penalties but NOT the motor's 1 s forward↔reverse dead-time (`SimMotorConfig.reverse_delay_s: 1.0`). Trained Smart/Leif policies flip thrust sign several times a second; at deployment the governor and firmware block ~45% of those commands, zeroing the braking the policy wanted and leaving it hunting. **Mitigated** by `AnchorMLMode.thrust_tau_s = 0.7` s output low-pass (blocked-reversal events drop from 45% → 16% of ticks; hold quality unchanged in A/B). The real fix is a retrain with the dead-time added to `env.py` + a thrust-reversal penalty (roadmap item — see [anchor-ml.md](anchor-ml.md)). | Medium | Mitigated (output low-pass, no retrain needed to ship). Retrain: add `reverse_delay_s` to the training env (see anchor-ml.md). |
| **Steering head physical lag.** The governor slews steering *commands* identically in both worlds, but the real head additionally has a PID position loop with a 1.2° deadband, settle time and stall handling; the sim applies the (slewed) command directly to the physics. | Medium | Needs a bench-measured model (deadband + first-order settle) — guessing constants would be worse than none. Standing item: measure on the bench, then add to `SimMotorConfig` and consider a policy retrain (`--steer-rate-dps` already models the slew in training). |
| **No steering feedback channel in sim.** Real firmware reports the measured head angle (`A` line); telemetry `steering.angle_deg` is measured on real hardware but modeled (commanded) in sim. UI ghost needles show command, not truth, in sim. | Medium | Pairs with the lag model above — simulate feedback once the head dynamics exist. |
| **Prop spin-up lag.** Thrust force follows the (slewed) command instantly; a real prop takes ~100–500 ms to bite. `thrust_lag_tau_s` exists but defaults 0 — no firmware analog to copy, needs water data. | Medium | Measure from the accel phase of a real calibration run, then set per-boat. |
| **Velocity fusion not exercised by default.** `sensors.gps_velocity` defaults false, so the sim feeds NMEA-style fixes and the GNSS/INS fusion (velocity, crab, dead-reckoning) stays dormant — the M9N always supplies velocity. | High (test coverage, not behavior) | Flipping the default changes navigator dynamics under every existing test/tuning; instead set `gps_velocity: true` in your sim config when testing fusion features. Candidate for a dedicated "hardware-fidelity" config preset. |
| **Ideal GPS noise by default.** The OU multipath walk + phantom-velocity models exist (`gps_jitter: indoor`) but default off; real M9N shows ~0.4 m/s phantom velocity under multipath. | Medium | Enable `gps_jitter` in config when stress-testing; default stays clean so regressions are attributable. |
| **Compass realism.** Sim compass = truth + white noise; a real HWT901B has tilt-coupled error, mounting offset and lag, so the offset-learning calibration path is never exercised in sim. | Medium | Add `heading_offset_deg`/tilt-coupling params to SimCompass when the calibration path needs sim coverage. |
| **IMU yaw rate is differentiated heading**, not a gyro model — noisier character than the real HWT901B gyro. | Medium | Document-only until fusion consumes yaw rate more aggressively. |
| **Depth sounder is a point sample** — no transducer cone/footprint integration, no transducer offset. | Low-Med | Chart accumulation already models the cone footprint on the *recording* side; the live sample stays a point until a cone model is worth its complexity. |
| **External GPIO hardware watchdog** (relay motor-cut on Pi hang) can't exist in sim. | Medium | Real-hardware verification item on the deploy checklist. |
| **Serial line loss / CRC rejection** behaviors live in the chaos suite rather than the default sim path. | Low | `tests/test_chaos.py` covers reconnect + drop semantics deterministically. |
| **`s_acc_mps` is a placeholder** (0.05 hardcoded) in sim fixes; real M9N reports its own estimate. | Low | Derive from `position_noise_m` if accuracy-weighting ever consumes it. |

## Operator guidance

- Anything tuned or validated in sim should get a **short shakedown on the
  water**: with the new firmware-matching actuation defaults the sim is much
  closer, but head-settle and prop spin-up remain optimistic, so autopilot
  gains may need a nudge down on the real boat.
- For fusion / M9N-specific testing in sim, set `sensors.gps_velocity: true`
  (and optionally `gps_jitter: indoor`).
- `sim.time_scale != 1` is for making demo videos, not for judging control.
