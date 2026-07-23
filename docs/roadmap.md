# Roadmap

What's next. For what's **done**, see the [CHANGELOG](../CHANGELOG.md) and
[FEATURES.md](FEATURES.md) — the original v1.0-alpha roadmap and the 2026-07
full-project review (Phases 0–7: safety floor, robustness, UI/API, nav/control,
sim depth, hardware, community groundwork) all shipped.

## Adoption pack — in progress

Making Vanchor attractive and easy for new users. Shipped: SD image + supervisor,
hardware setup wizard, demo mode, push notifications, passive anchor-alarm. Still
open:

- **WiFi onboarding** — hotspot-first captive "join your WiFi" page. *(S)*
- **Guided "first anchor" overlay** — script the magic moment once. *(S)*
- **Simple/advanced UI split** — map + anchor + take-me-here + STOP by default;
  expert features behind a toggle. *(M)*
- **Spot memory** — named spots with depth/notes; anchor-at / route-to. *(S)*
- **Weather overlay + "holdable?" check** — forecast vs the trained wind cap. *(M)*
- **One-tap trolling presets**; **metric/imperial + i18n**; **in-UI trip replay
  polish**. *(S each)*
- **NMEA-0183 output over WiFi** — feed fish finders / OpenCPN / Navionics. *(M)*
- **Pre-departure check screen**; **battery endurance estimate** (INA226 +
  thrust history). *(S / M)*
- **Depth-chart sharing** (cmapper export/import exists; community library later)
  and the **driver-pack install UI** — both lead into [extension
  packs](extension-packs.md). *(M)*

## Control & ML

- **Reverse dead-time retrain (L).** The anchor-policy training env models
  steering slew but not the motor's 1 s forward↔reverse dead-time, so Smart/Leif
  over-reverse and the interlock blocks ~45% of their commands. Shipped a
  deployment mitigation (output low-pass, `thrust_tau_s`); the real fix is to add
  the dead-time + a reversal penalty to `experiments/anchor_policy/env.py`,
  regenerate, and re-run the promote gauntlet. See [anchor-ml.md](anchor-ml.md).
- **`sqrt`-shaped anchor-hold position term** and **min-effective-thrust /
  deadband compensation** — both cheap to prototype in the Fossen sim. *(S)*
- **Background gyro-bias learning**, persisted across boots; **passive
  self-improving compass cal** with a health meter. *(S / M)*

## Safety & robustness

- **PWA-heartbeat failsafe** defaulting to HOLD (not just serial-link loss), with
  the trigger reason in the alarm/push. *(S)*
- **Uniform `FailsafeAction {STOP, HOLD, RETURN}`**, one per trigger, default
  HOLD. *(M)*
- **Brownout story** — Pi reset → MCU link-loss failsafe (never open-throttle),
  documented + tested. *(S)*
- **SITL-style regression** — assert the shipped control stack at real-time in CI;
  **deterministic session replay** through the real stack from recordings. *(M)*
- **Firmware heartbeat round-trip** (sequence echoed in the `A` line) so the Pi
  detects one-way serial failure. *(S)*

## Hardware

- **Steering-head hardware selection** — RC servo (MVP) vs custom worm gearbox;
  neither is bench-verified yet. See [custom-hardware.md](custom-hardware.md).
- **Interactive magnetometer calibration** — the 360° hard/soft-iron fit exists
  but the raw mag vector isn't plumbed into the navigator (HWT901B emits fused
  heading); surface it to close the loop. *(S)*
- **Hardware watchdog chain** — Pi heartbeat GPIO → external relay on the motor
  supply (covers a Pi hard-hang the firmware watchdog can't). *(M)*
- **Battery / driver / watchdog health** — configurable via YAML now; expose in
  the Settings UI + telemetry. *(S)*
- Bench-verify: N2K + SocketCAN, the split-channel line protocols, the helm PCB
  I²C tunnel, and HIL (bench Arduino) tests.

## Sim fidelity

The known sim-vs-real gaps (steering-head lag, prop spin-up, sim steering
feedback, sea-state/IMU model, realistic GPS/compass noise) are tracked in
[simulator.md](simulator.md) — most need bench/water data to close.

## Future — extension packs

A HACS-style way to share drivers, views, tunes, charts, and scenarios without
editing core. Designed, not built; the keystone is the versioned driver API +
capability object. Design and the non-negotiable safety floor in
[extension-packs.md](extension-packs.md).
