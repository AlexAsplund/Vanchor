# Vanchor-NG — Feature Overview

A software-first GPS autopilot / anchoring / waypoint system for a cheap trolling
motor. Python 3.12 · asyncio · FastAPI + WebSocket backend · vanilla-JS + Leaflet
web UI. The controller steers only on *perceived* (noisy) sensor state; ground
truth lives only in the simulator, and every simulated device implements the same
hardware interfaces real serial gear will — so swapping to real hardware changes
only device construction, not the control logic.

---

## 🖥️ GUI (web app)

A dark, futuristic "tactical HUD" marine app over a live map.

- **Map backdrop** — Leaflet with selectable basemaps: CARTO Dark / Light, **Esri
  World Imagery** (satellite), **OpenTopoMap**, plus an **OpenSeaMap sea-marks**
  overlay. Deep-zoom tile upscaling (no "no data" tiles).
- **North-up / heading-up orientation** — compass toggle rotates the chart so
  up = the bow (stabilized against compass jitter: low-pass + deadband +
  slew-limited easing); markers and menus stay upright.
- **Top status bar** — connection, GPS fix, SOG, heading, depth, **battery**,
  **remote-link** and **route-progress** (traveled ▸ remaining, m/km) chips.
- **Customizable HUD** — Speed, Heading (compass rose), Depth, Dist→Anchor,
  Battery widgets: individually toggleable, **drag-to-reposition**, saveable
  **layout profiles**, and an **opacity control** (fade just the tile or the text
  too). Optional tactical frame (heading tape + reticles).
- **Steering gauge** — closed-loop commanded-vs-feedback azimuth with cable-wrap arc.
- **Mode rail + contextual panels** — Manual, Anchor, Route, APB,
  Drift, Stop, Remote, and a **"🎣 More"** group for the fishing modes.
- **Remote helm** — full-screen, big-button mode for use at the helm/phone.
- **Sound feedback** — synthesized audio cues (offline, no files): alarms in
  three severities (calm beep → warble → siren) with a sonar-ping exception
  for depth warnings, notification chimes, a distinct motif per control mode,
  waypoint dings + route-complete fanfare, button ticks; per-category toggles,
  volume and previews in Settings → Sound & touch.
- **Haptic feedback** — vibration pulse on button presses (heavier on STOP,
  distinct buzz on safety alarms, long-press confirmation); Android/PWA,
  toggleable in Settings → Sound & touch.
- **Markers** — drop/import, selectable icons, with **"Take me here"** (Fastest /
  Along-shoreline) straight from a marker.
- **Route editor** — explicit **"Add waypoint"** mode (taps don't litter pins),
  drag waypoints (pending *and* active), long-press menu (insert/delete/**set
  speed**), **Save / Load** named routes (pending *or* the active route).
- **Per-waypoint speeds** — a waypoint can carry an engine-**%** or boat-**knots**
  speed, adopted on arrival for the following legs (a manual speed change wins
  until the next speed-carrying waypoint).
- **Replace or Append** — any *Take me here* action onto an existing
  active/pending route asks whether to replace it or append to its end
  (appending to a running route doesn't restart it).
- **Depth map overlay** — averaged, **colour-ramped depth chart** (marine
  shallow→deep) with a legend, not raw dots; sonar-cone-scaled footprints.
- **Catch logger** — floating launcher; log species + length + weight; **catch
  analytics** (per-species stats, best time-of-day, best depth band, heatmap).
- **Trip log** — live distance/duration/avg-max speed; past-trips list with track
  preview and **GPX export**.
- **Init-boat wizard** (in the menu) + **multiple editable boat profiles** applied
  live; auto-calibration drive; auto PID-tune panel.
- **Settings drawer** — weather presets/controls, GPS-position calibration, sonar
  cone angle, offline-map downloader (+ storage mgmt), debug recorder/replay, species editor.
- **Safety banners** — shallow / no-go / man-overboard / return-to-launch / link-loss.

---

## ⚙️ Functions (capabilities)

**Control modes**
- **Manual** (thrust/steering, full ±180° swing, snap-to-zero).
- **Anchor hold** — heading-aware station keeping with drift
  anticipation and **anchor jog** (nudge the hold point).
- **Anchor alarm (motor off)** — passive GPS watch circle over the physical
  anchor: arm from the Anchor panel; server-side 1 Hz watch keeps alarming
  even while the phone sleeps (banner + high-severity sound + telemetry);
  one-tap Recover engages `anchor_hold` at the alarm point. Persisted across
  restarts (`anchor_alarm.json`). Zero motor commands while passive.
- **Manual steering wheel** — dual-ring gyro dial (live compass card over a
  boat-frame ring): drag around = direction, outward = power; hub reads
  relative° + true° + thrust; ghost tick shows the actual head angle;
  Relative/Absolute/Course steering modes (absolute holds a compass bearing
  while the boat yaws; course follows the straight track line drawn from the
  engage point, cross-track corrected).
- **Heading-hold** autopilot (API/connectors only — the UI tile was
  superseded by Manual's Absolute/Course steering).
- **Waypoint / Route** following with cross-track correction **and predictive
  drift compensation** (crabs into wind/current so the ground track holds).
- **Follow-APB** (external autopilot bearing).
- **Drift** mode (controlled drift speed).
- **Contour-follow** — hold a chosen depth contour from the live sounder.
- **Circle / Orbit** — loop a marked point at a set radius (CW/CCW).
- **Trolling pattern** — sinusoidal S-curve weave at a held speed.

**Navigation & routing**
- **Smart "Take me here" water routing** — *Fastest* (visibility graph + A\*,
  ~0.1 s) and *Along-shoreline* (hugs the coast, into bays), water-only, restricted
  to the boat's own lake. Routes load editable/unstarted for review.
- **Area survey "map mode"** — lawnmower coverage route over a drawn box/polygon.
- **Cruise control** (hold knots) and **% engine-power** throttle, in one
  always-available speed control.
- **Pause / Resume / Stop** navigation (anchor mid-route, then resume).
- **Return-to-Launch** (auto-recommended on low battery), **Man-Overboard**
  (mark + return), **link-loss failsafe** (hold position if the phone drops),
  **shallow-water / geofence auto-stop**.
- **Record-a-track / replay / retrace**; GPX route import.

**Sensing & data**
- **Depth mapping** — accumulates soundings, persists across sessions, gridded
  colour overlay, sonar-cone footprint sizing.
- **Battery monitor** — state-of-charge, voltage, draw, range/time-to-empty.
- **GPS-offset calibration** ("adjust my position"), **sensor-anomaly spike
  rejection**, smoothed GPS display.
- **Auto-calibration drive** measures top speed / accel / drag / turn-rate /
  steering sign, then **auto-tunes the PIDs**.
- **Catch log + analytics**, **trip log + GPX export**.

**Safety & systems**
- **Safety governor** — thrust slew limiting, reverse delay, loss-of-fix failsafe,
  drag alarm, steering-rate limiting.
- **Boat profiles** — multiple named boats, edited in the menu, applied live
  (physics rebuilt on switch).
- **NMEA-over-TCP** server (feed a phone/plotter), **debug recorder** (gzip NDJSON
  capture + replay), typed YAML/JSON config.
- **Offline operation** — pre-download map tiles + the routing chart for an area.
- **Safety matrix + chaos suite** — `docs/safety-matrix.md` maps 12 failure modes
  (Pi crash, GPS loss, link loss, serial loss, compass silence, …) to their
  detecting layer and proving test; `tests/test_chaos.py` (24 deterministic
  fault-injection tests) encodes this as a runnable CI gate.

---

## 🌊 Simulation

The simulator owns ground truth; simulated GPS/compass/depth emit **real NMEA**
(RMC/HDM/DPT) with realistic noise, so the navigator and controller can't tell sim
from real hardware.

- **Boat physics** — a custom **3-DOF maneuvering model** (surge/sway/yaw) for a
  **bow-mounted steerable trolling motor** (thrust vectoring; steering authority
  scales with thrust; bow-vs-stern mount flips the yaw). A simpler kinematic model
  is also available. Boat geometry/mass/thrust are fully configurable per profile.
  - **Credit:** the hydrodynamics model follows the marine-craft equations of
    **Prof. Thor I. Fossen** — *Handbook of Marine Craft Hydrodynamics and Motion
    Control* (Wiley). Our implementation (`sim/fossen.py`) is an independent,
    dependency-free Python realization of that 3-DOF framework; all credit for the
    underlying modelling approach goes to Fossen.
- **Environment** — wind, current and **gusts** (Ornstein-Uhlenbeck process), plus
  slow session-scale weather wander; tunable, with **presets** (calm / lake /
  river / coastal). True ground velocity drives COG/SOG so drift is observable.
- **Bathymetry** — synthetic lake bottom feeding the simulated depth sounder.
- **Battery** — discharge model driven by motor load.
- **Deterministic harness** — the whole loop (physics, sensors, navigator,
  controller) steps in lockstep with seeded noise for fast, non-flaky tests
  (~718 tests).

**Default sim location:** Lake Vänern / Visten area near Karlstad, Sweden
(59.66275 N, 13.32247 E). Water/routing data from **OpenStreetMap** (Overpass).

---

## 🛠️ Tech & credits

- **Backend:** Python 3.12, asyncio, FastAPI, uvicorn, WebSocket. Routing geometry
  via shapely + networkx + pyproj.
- **Frontend:** vanilla JS, **Leaflet**, uPlot.
- **Map/data:** © OpenStreetMap contributors, © CARTO, © Esri/Maxar, OpenSeaMap,
  OpenTopoMap.
- **Marine dynamics:** modelling after **Thor I. Fossen**.
- **Hardware (prototype):** a 3D-printable **worm-gear steering servo** with
  AS5600 magnetic angle feedback (STLs + build notes in [`cad/`](../cad/)).
