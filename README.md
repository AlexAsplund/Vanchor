# Vanchor-NG

> **v1.0-alpha** — the start of a new major version. Vanchor-NG is a ground-up,
> software-first rewrite that **replaces the original [Vanchor](https://github.com/AlexAsplund/Vanchor) (0.1-alpha)**.

## What it is

Vanchor-NG turns a cheap **trolling motor** into a GPS-guided autopilot and
anchor system. Drop a **virtual anchor** to hold a spot, **hold a heading** like
a real autopilot, or tap the map and say **"take me here"** — the boat plans a
water-only route around islands and steers itself there, correcting for wind and
current drift along the way.

The headline is that **it runs entirely in simulation, with no hardware at all.**
A built-in physics simulator and simulated NMEA sensors close the control loop on
your laptop, so the whole navigation/control stack can be developed and tested
without a boat, a Pi, or a single wire. When you do have hardware, the same code
drives it — only the device construction changes.

It is a **PWA** (Progressive Web App): installable, works offline, and served by
the boat's own Raspberry Pi.

> **This is 1.0-alpha** — a from-scratch rewrite that supersedes the 0.1-alpha project.
> See [`RELEASE.md`](RELEASE.md) for release notes and migration notes.

## Screenshots

| | |
|:---:|:---:|
| ![Main view](docs/images/overview.png) | ![Depth mapping](docs/images/depth.png) |
| **Main view** — boat, instrument HUD, mode dock, steering gauge | **Depth mapping** — colour-ramped grid with radiating coverage + isobath contours |
| ![Menu](docs/images/settings.png) | ![Mobile](docs/images/mobile.png) |
| **Menu** — a centred command menu: 8 large category tiles open big, thumb-friendly sub-panels | **Mobile** — full-bleed map + a draggable bottom sheet; picking a mode slides its options up |

> All four run on the **built-in simulator** — no hardware, no boat, just `vanchor` on a laptop.

### Views — specialised, URL-addressable layouts

Reach any view at **`/view/<name>`** — deep-linkable and offline-capable. Each drops
the chart and rearranges the same live widgets for a job at the helm; every view
keeps an ever-present STOP.

| | | |
|:---:|:---:|:---:|
| ![Helm](docs/images/view-helm.png) | ![Instruments](docs/images/view-instruments.png) | ![Manual](docs/images/view-manual.png) |
| **Helm** (`/view/helm`) — big mode grid, quick actions, dominant STOP | **Instruments** (`/view/instruments`) — a large glance HUD | **Manual** (`/view/manual`) — big thrust + steering |

An opt-in **Daylight** high-contrast theme keeps it readable in direct sun (dark stays default):

![Daylight theme](docs/images/daylight.png)

### A few of the modes

| | |
|:---:|:---:|
| ![Route](docs/images/mode-route.png) | ![Anchor / Spot-Lock](docs/images/mode-anchor.png) |
| **Route** — build a path or tap "take me here"; smart water-only routing, with Loop / Patrol | **Anchor / Spot-Lock** — drop a virtual anchor and hold the spot, with a nudge jog |
| ![Work Area](docs/images/mode-work-area.png) | ![Trolling](docs/images/mode-trolling.png) |
| **Work Area** — visit a set of spots, hold at each, advance on a timer or the big button | **Trolling** — a sinusoidal S-curve weave at a held speed |

> One guide per mode lives in **[`docs/modes/`](docs/modes/)**.

## Highlights

**Navigation & control**

- **Virtual GPS anchoring (spot-lock)** — drop a virtual anchor and hold the spot,
  with heading-aware drift anticipation and a *spot-lock jog* to nudge the hold point.
- **Autopilot heading-hold** — set a compass heading and hold it.
- **Waypoint navigation** with cross-track correction and predictive drift
  compensation (crabs into wind/current so the *ground* track stays true).
- **Smart "take me here" water routing** — water-only routes that avoid land and
  islands: *Fastest* (visibility graph + A\*) or *Along-shoreline* (hugs the
  coast, into bays). Routes load editable and unstarted for review.
- **Loop-around-island routing** and **area-survey "map mode"** (lawnmower
  coverage over a drawn box/polygon).
- **Work Area mode** — work a set of spots: tap them in, or draw an area and
  auto-generate a grid; the boat travels to each, **holds station**, then advances
  — on a **dwell timer** or a big on-screen **"Go to next spot"** button — with an
  optional **per-spot hold heading**, and loop / there-and-back patrol over the set.
- **Cruise control** (hold knots) and **% engine-power** throttle.
- **Pause / resume / stop** mid-route; **record-a-track / replay / BackTrack**; GPX import.

**Fishing modes**

- **Contour-follow** — hold a chosen depth contour from the live sounder.
- **Circle / Orbit** — loop a marked point at a set radius (CW/CCW).
- **Trolling pattern** — a sinusoidal S-curve weave at a held speed.

**Safety pack**

- **Battery monitor** (state-of-charge, voltage, draw, range/time-to-empty) with
  auto-recommended **return-to-launch** on low battery.
- **Shallow-water / no-go geofence auto-stop**.
- **Link-loss failsafe** — holds position if the controlling phone drops off.
- **Man-overboard** (mark + return) and a **safety governor** (thrust slew
  limiting, reverse delay, loss-of-fix failsafe, anchor-drag alarm).

**Sensing & data**

- **Depth mapping** — a colour-ramped depth grid (marine shallow→deep) with
  radiating coverage from each sounding, plus an **isobath contour overlay**;
  persists across sessions.
- **Catch logging + analytics** — log species, length, weight; per-species stats,
  best time-of-day, best depth band, and a **heatmap**.
- **Trips + GPX export** — live distance/duration/avg-max speed and a past-trips list.
- **GPS-offset calibration** ("adjust my position") and sensor-anomaly spike rejection.
- **Auto-calibration drive** that measures top speed / accel / drag / turn-rate /
  steering sign, then **auto-tunes the PIDs**.

**Boats, devices & systems**

- **Multiple editable boat profiles** with ready presets (jon boat → bow/stern
  trolling → 15 HP outboard) and a **hull-character** handling model.
- **Per-device simulation OR real hardware** — GPS, compass, depth and motor each
  choose `sim` / `serial` / `nmea`; you can even **bench-test a steering servo**
  against a fully simulated autopilot.
- **Versioned backup / restore** of all persistent state (one ZIP).
- **Measure tool**, **reference grid**, a phone-friendly **mobile / remote-helm**
  mode, and **PWA / offline** support.

## Sim-first

The whole point of Vanchor-NG is that **you never need hardware to develop or
test it.** A built-in physics simulator owns ground truth; simulated GPS, compass
and depth sensors emit **real NMEA** (RMC/HDM/DPT) with realistic noise. The
navigator and controller can't tell sim data from a real receiver, so the entire
control stack runs and is tested on a laptop. A deterministic harness steps the
full loop in lockstep with seeded noise, so closed-loop tests run in milliseconds
and never flake.

## How it works

The data flows around one closed loop. The controller only ever reads the
**perceived** (noisy) sensor state — exactly as it will with real hardware —
while ground truth lives only in the simulator:

```
motor command ─▶ boat physics ─▶ GPS/compass/depth NMEA ─▶ navigator ─▶ state
      ▲                                                                  │
      └──────────────── helm ◀── control mode ◀──────────────────────────┘
```

Every simulated device implements the **same hardware interfaces** the real
serial gear does, so swapping to hardware changes only how devices are
constructed — nothing in the control logic. The backend is **Python + asyncio +
FastAPI** with a **WebSocket** telemetry stream; the front end is **vanilla JS +
Leaflet** (no build step, no framework).

## PWA / offline

The web UI is a Progressive Web App. It is **installable**, **loads offline**,
and uses a **network-first** service worker so it always prefers fresh data but
still works when the network drops. In a real deployment the boat's Raspberry Pi
serves the app directly to your phone.

## Quick start

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev,routing]"

vanchor --host 0.0.0.0 --port 8000
```

Then open **http://localhost:8000**. Vanchor-NG **defaults to simulation**, so
this just works with no hardware. You'll see a boat on the map — drop an anchor,
set a heading, or tap "take me here" and watch it steer.

## Configuration

Vanchor-NG reads configuration from a **YAML file** *and* from **environment
variables** via a `.env` file (copy `.env.example` and edit it). The key
`VANCHOR_*` variables:

| Variable | Purpose |
|----------|---------|
| `VANCHOR_HOST`, `VANCHOR_PORT` | server bind address / port |
| `VANCHOR_DATA_DIR` | where persistent data lives (boats, depth map, trips, charts) |
| `VANCHOR_MODEL` | boat physics model (`fossen` / simple) |
| `VANCHOR_HARDWARE` | master switch: simulation vs. real hardware |
| `VANCHOR_GPS_SOURCE`, `VANCHOR_COMPASS_SOURCE`, `VANCHOR_DEPTH_SOURCE`, `VANCHOR_MOTOR_SOURCE` | per-device source (`sim` / `serial` / `nmea`; motor also `both`) |
| `VANCHOR_GPS_PORT`, `VANCHOR_COMPASS_PORT`, `VANCHOR_MOTOR_PORT` | serial ports for real devices |
| `VANCHOR_BAUDRATE` | serial baud rate |
| `VANCHOR_NMEA_TCP`, `VANCHOR_NMEA_TCP_HOST`, `VANCHOR_NMEA_TCP_PORT` | NMEA-over-TCP bridge (feed/read from a phone or plotter) |
| `VANCHOR_SIM_START_LAT`, `VANCHOR_SIM_START_LON` | simulator start position |
| `VANCHOR_OVERPASS_URLS` | OSM Overpass endpoints for water/routing data |
| `VANCHOR_USER_AGENT` | HTTP User-Agent for OSM requests |

See **`.env.example`** for the full list and defaults. **Device-config changes
apply on the next restart** (see below).

## Testing

```bash
python -m pytest -q     # unit + deterministic closed-loop integration tests
python e2e_smoke.py     # isolated end-to-end smoke test
```

The integration tests run the full navigator + controller + simulator loop
deterministically (no asyncio, no wall-clock, seeded sensor noise) and assert
that, e.g., anchor-hold converges and stays within a few metres under continuous
wind + current drift.

## Project layout

```
src/vanchor/
  app.py        config-driven Runtime wiring + CLI entrypoint
  core/         events, models, geo, pid, state, config, boat profiles, backup
  nav/          nmea, navigator, routing/water, depth, survey, track, trip
  sim/          fossen (3-DOF) + simple physics, devices, bathymetry, weather, battery
  hardware/     real serial / NMEA device + motor drivers (mirror the sim devices)
  controller/   controller (+ Helm), modes, calibration, safety
  ui/           server.py (FastAPI WS + REST), static/ (Leaflet PWA)
  analysis/     headless scenario runner + auto-tuner
tests/          pytest suite + deterministic harness
docs/           human docs + docs/llms/ AI developer guide
```

## Documentation map

- **Human docs** live in **[`docs/`](docs/)** — start at [`docs/README.md`](docs/README.md)
  for an index (architecture, features, APIs, simulator, firmware, analysis,
  roadmap, assumptions).
- **The AI / LLM developer guide** lives in **[`docs/llms/`](docs/llms/)** — a
  curated, per-subsystem guide written for LLMs working on the code (also linked
  from [`AGENTS.md`](AGENTS.md)).

## Alpha status

This is an **early alpha (1.0-alpha)** intended for **development and testing**.
The project is **sim-first**: the simulation path is the mature, well-tested one.
Real-hardware support is provided and mirrors the simulated devices, but is far
less exercised — treat it as experimental. Expect rough edges and breaking
changes as 1.0 takes shape.

## License

**MIT** — see [`LICENSE`](LICENSE).

A clean-room rewrite; no original Vanchor source was copied. The 3-DOF
hydrodynamics follow the marine-craft equations of **Prof. Thor I. Fossen**
(*Handbook of Marine Craft Hydrodynamics and Motion Control*, Wiley); our
`sim/fossen.py` is an independent, dependency-free Python realization of that
framework.
