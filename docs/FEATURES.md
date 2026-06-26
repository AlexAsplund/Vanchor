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
- **Top status bar** — connection, GPS fix, SOG, heading, depth, **battery**,
  **remote-link** and **route-progress** (traveled ▸ remaining, m/km) chips.
- **Customizable HUD** — Speed, Heading (compass rose), Depth, Dist→Anchor,
  Battery widgets: individually toggleable, **drag-to-reposition**, saveable
  **layout profiles**, and an **opacity control** (fade just the tile or the text
  too). Optional tactical frame (heading tape + reticles).
- **Steering gauge** — closed-loop commanded-vs-feedback azimuth with cable-wrap arc.
- **Mode rail + contextual panels** — Manual, Anchor, Heading-hold, Route, APB,
  Drift, Stop, Remote, and a **"🎣 More"** group for the fishing modes.
- **Remote helm** — full-screen, big-button mode for use at the helm/phone.
- **Markers** — drop/import, selectable icons, with **"Take me here"** (Fastest /
  Along-shoreline) straight from a marker.
- **Route editor** — explicit **"Add waypoint"** mode (taps don't litter pins),
  drag waypoints (pending *and* active), long-press menu (insert/delete),
  **Save / Load** named routes.
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
- **Anchor-hold / Spot-Lock** — heading-aware station keeping with drift
  anticipation and **Spot-Lock Jog** (nudge the hold point).
- **Heading-hold** autopilot.
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
- **Record-a-track / replay / BackTrack**; GPX route import.

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
  (~370 tests).

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
- **Hardware (prototype):** 3D-printable steering gearbox designed parametrically
  with build123d (see `cad/`).
