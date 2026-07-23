# Extension packs

> Status: **design notes only — not yet built.** This sketches how a
> community/sharing layer *could* work, so the enabling piece (a versioned
> driver API) gets built in a shape that doesn't paint us into a corner. It
> stays small and safety-first.

## Why

Vanchor is an open, software-first autopilot people will build **variants** of:
different motors, sensors, boats, and workflows. A community layer lets that
diversity be *shared* instead of forked — so a driver someone wrote for their
depth sounder, or a station-keeping tune for their skiff, is one install away for
the next person, without everyone editing core.

## The non-negotiable: the safety floor is never a pack concern

This is the first design constraint, not an afterthought. A community pack **must
never be able to weaken** the safety floor:

- the motor deadman, the link/fix failsafes, and **STOP-always-works** live in
  core and are **off-limits to packs**;
- packs receive a **narrow capability object**, never the `Runtime`, never the
  motor, never the governor internals;
- there is a **safety-floor config lockout** (roadmap #50) that hot-reload,
  profiles, backup-restore, and packs can *never* override;
- a misbehaving or hanging pack must degrade to a safe state (the supervisor +
  hardware watchdog already zero the motor on fault).

If a capability can't be exposed without risking the floor, it isn't exposed.

## What gets shared ("packs")

Five kinds, in rough order of value and safety-sensitivity:

| Pack kind | Examples | Risk | Enables |
|---|---|---|---|
| **Data** | boat profiles, tuned gains, ML anchor policies, depth charts, routes/trips, sim scenarios | low (pure data) | the easy, high-value wins first |
| **View/GUI** | specialised `/view/<name>` layouts, HUD widgets, panels | low (sandboxed UI) | custom on-water screens |
| **Device drivers** | battery monitor, extra GPS/compass, sonar gateway | **high** (touch hardware/serial) | new hardware without core edits |
| **Analysis** | new metrics, report cards, tuning strategies | low (offline) | shared eval/tuning recipes |
| **Sim** | fault scenarios, sea-states, boat dynamics | low (offline) | shared test/regression cases |

Start with **data + sim** packs (no code execution, trivially safe), then
**view** packs (sandboxed UI), and only then **device drivers** (which need the
versioned capability API to be safe).

## The pack model (HACS-style, pip-installable)

- A pack is a small **pip-installable Python package** (or, for data/view packs, a
  zip/JSON bundle) with a **manifest**: name, version, kind, the vanchor API
  version it targets, declared capabilities, and author.
- Core discovers installed packs via **entry points** (roadmap #43 — "entry-point
  discovery so packs are pip-installable"). No dynamic code download at runtime;
  installation is an explicit, offline-on-the-boat `pip install` step.
- Drivers register against the **versioned driver API** (#43): all four device
  kinds (gps/depth/motor/battery) route through the registry, and a driver gets a
  **narrow capability object** (publish-a-fix, report-health, read-config) instead
  of `runtime: Any`. That contract is the whole ballgame for driver-pack safety.
- View packs register a view/widget descriptor; they run in the existing
  browser sandbox and talk only to the public `/api` + telemetry surface.

## The registry (keep the infra near-zero)

- Model it on HACS: a **git-backed index** (a JSON list of pack repos + versions)
  rather than hosting a store. No servers to run; GitHub/Gitea is the CDN.
- The app ships an **offline-first** installed-packs view; browsing/installing new
  packs needs connectivity (done on shore, not mid-lake).
- **Curation tiers**: `core` (shipped) → `verified` (reviewed, meets the safety
  contract, checksum/signature) → `community` (unreviewed, clearly labelled,
  extra install friction and capability limits). Drivers especially should be
  `verified` before they're one-click.

## Adjacent community loops (already partly on the roadmap)

- **Session upload (#48):** opt-in "upload last session on WiFi" turns real-water
  incidents into replayable sim scenarios — the black-box/debug recordings become
  shareable regression cases. Needs a clear privacy story (explicit opt-in, strip
  location if asked, user owns their data).
- **Depth charts:** the cmapper GeoJSON import path is a natural first *data*
  pack — shared bathymetry with live chart-vs-sounder divergence alerts (#45).
- **ML anchor policies:** `anchor_policy.json` / `anchor_leif.json` are already
  just weights — a trained tune for a particular hull is a trivial data pack.

## Rough phasing

1. **Prereq — #43 versioned driver API + capability object + entry-point
   discovery.** Nothing safe ships without this; it's the keystone.
2. **Data + sim packs** (boat profiles, gains, ML policies, depth charts,
   scenarios) — pure data, ship first.
3. **View packs** — sandboxed UI, low risk.
4. **The registry + docs (#52)** — the git-index, the installed-packs UI, the
   curation tiers.
5. **Driver packs** — only once #43's capability contract is proven, starting
   with the battery monitor (#42) as the reference non-core driver.

## Open questions to settle before building

- Signing/verification: how much, and who holds the keys for `verified`?
- Capability granularity: what exactly does a driver capability object expose,
  and how is a hung/rogue driver isolated (thread vs process)?
- Versioning/compat: how do packs declare + we enforce the vanchor API version?
- Data-pack provenance: how do we attribute/track shared depth charts + policies?
