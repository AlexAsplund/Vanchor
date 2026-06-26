# Vanchor-NG v2.0-alpha — Release Notes

**Vanchor-NG v2.0-alpha** is a **ground-up rewrite** of Vanchor that **replaces
the original 1.x project**. It is software-first: the entire GPS
autopilot / anchoring / waypoint stack runs and is tested in simulation, with no
hardware required, and ships as an installable, offline-capable PWA.

This is the **first alpha of the new 2.0 line.** It is meant for development and
testing. Expect rough edges and breaking changes before 2.0 stabilises.

## What's new (the headline feature set)

- **Virtual GPS anchoring (spot-lock)** with heading-aware drift anticipation and
  a spot-lock jog.
- **Autopilot heading-hold** and **waypoint navigation** with cross-track
  correction and predictive drift (wind/current) compensation.
- **Smart "take me here" water routing** — water-only routes around land/islands
  (Fastest A\* or Along-shoreline), plus loop-around-island and lawnmower
  area-survey routes.
- **Fishing modes** — contour-follow, circle/orbit, and a trolling S-curve pattern.
- **Safety pack** — battery monitor + auto return-to-launch, shallow-water / no-go
  auto-stop, link-loss failsafe, man-overboard, and a thrust/steering safety governor.
- **Depth mapping** — colour-ramped depth grid with radiating coverage and an
  isobath contour overlay; persists across sessions.
- **Catch logging + analytics heatmap**, **trips + GPX export**.
- **Multiple editable boat profiles** with ready presets and a hull-character
  handling model; an **auto-calibration drive** that auto-tunes the PIDs.
- **Per-device sim or real hardware** (GPS/compass/depth/motor each `sim` /
  `serial` / `nmea`), including bench-testing a real servo against a simulated boat.
- **Versioned backup / restore**, a measure tool, a reference grid, mobile /
  remote-helm mode, and **PWA / offline** support.
- **Configuration** via YAML and a `.env` (`VANCHOR_*` environment variables).

For the full feature breakdown see [`docs/FEATURES.md`](docs/FEATURES.md); for
the overview and quick start see [`README.md`](README.md).

## Experimental / alpha caveats

- **Sim-first.** The simulation path is mature and heavily tested. Everything else
  should be read against that.
- **Real-hardware paths are experimental.** The serial GPS / compass / motor
  drivers mirror the simulated devices and share the same interfaces, but are far
  less exercised on real gear.
- **Smart routing needs OpenStreetMap data.** "Take me here" / island / survey
  routing requires OSM water geometry (via Overpass) and the `routing` extra
  installed. Pre-fetch tiles + charts for an area to use it offline.
- **Live device hot-swap is NOT enabled.** Editing the device/hardware config
  persists the change and **applies it on the next restart** — it is not swapped
  in live (see below). Other device changes are the same: device-config edits
  take effect on restart.
- **Early alpha generally** — APIs, config shapes, and on-disk formats may change.

### Device config applies on restart

A live device hot-swap was prototyped and then **reverted as unreliable** — it
could trip the fix-loss failsafe mid-operation. Today, `POST /api/config/devices`
**persists** the new config to `devices.json` and it **applies on the next
restart** (`restart_required: true`). A `Runtime.reload_devices()` method exists
but is **not auto-invoked**.

## Migrating from 1.x

Vanchor-NG 2.0 is a **full replacement** for the original Vanchor (1.x), not an
in-place upgrade. **This repository supersedes the old one.** There is no
automatic data migration:

- **Back up any old Vanchor (1.x) data** before switching.
- Install and run Vanchor-NG fresh (see the [Quick start](README.md#quick-start)).
- Recreate boat profiles, routes and settings in the new app; use its **backup /
  restore** to preserve state going forward.

## For maintainers

> **These are suggested commands for the maintainer to run — they have NOT been
> run.** They are guidance for cutting the release, not a record of actions taken.

Suggested release steps:

1. **Tag the old project** so the final 1.x state is preserved:
   ```bash
   # in the OLD vanchor repo
   git tag -a 1.0-beta -m "Final 1.x release before the 2.0 rewrite"
   git push origin 1.0-beta
   ```
2. **Publish this rewrite as `2.0-alpha`** (it replaces the old project):
   ```bash
   # in THIS vanchor-ng repo
   git tag -a 2.0-alpha -m "Vanchor-NG 2.0-alpha — ground-up software-first rewrite"
   git push origin 2.0-alpha
   ```
3. Create a GitHub release from the `2.0-alpha` tag, linking these notes, and note
   in the old project's README that it is superseded by Vanchor-NG 2.0.

Before tagging, verify the suite is green:

```bash
python -m pytest -q
python e2e_smoke.py
```
