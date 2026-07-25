# Extensibility

> How Vanchor is extended — the plug-in architecture, the seams every subsystem
> exposes, the capability a plug-in receives, and the safety floor that stays
> off-limits. Status: **drivers + connectors are live today; the uniform kernel
> and the other seams are planned.** Design, not all built.

## The pattern already exists (twice)

Two subsystems are already fully pluggable via Python **entry points**, and they
share the right skeleton:

| | Group | Registry | Guardrail |
|---|---|---|---|
| **Device drivers** | `vanchor.drivers` | `hardware/registry.py` (`register_context_driver`, `DRIVER_API_VERSION`) | capability-**publish whitelist** — a driver publishes a fix/health, never a control topic |
| **Connectors** | `vanchor.connectors` | `connectors/registry.py` (`register_connector`, `ConnectorManifest`) | **consent/grants** store; re-consent on manifest change |

Both follow: **entry-point discovery → typed registry → versioned API → narrow
capability → safety gate.** That skeleton is the whole design. The goal is to
extract it once and apply it to *every* part.

## The extension kernel (`vanchor.ext`)

**Shipped (#96) and consumed by drivers + connectors.** One small leaf package
(`src/vanchor/ext/`, imports nothing from app/runtime/controller) now owns the
shared machinery that `_iter_entry_points` was copy-pasted for in
`hardware/drivers/__init__.py` and `connectors/__init__.py`:

- **`discover(group)`** — entry-point discovery across importlib versions; never
  raises when zero packs are installed. **Shipped + consumed:** both driver and
  connector loaders call `discover(GROUP)`; their local `_iter_entry_points` is
  gone.
- **`Registry`** — a typed name→factory map with API-version + duplicate checks
  (log-and-skip, never raises). **Shipped** as scaffolding; drivers/connectors
  keep their bespoke registries for now — migrating them onto `Registry` is
  follow-up.
- **`Manifest`** — a frozen (hashable, so consent can key on it) dataclass:
  name, version, kind, targeted API version, declared capabilities, author.
  **Shipped.**
- **`Capability`** — the narrow marker base a plug-in receives instead of
  `Runtime`. **Shipped** (minimal; per-seam verbs land on subclasses).
- **Lifecycle** — `on_load(kernel)`, `on_start`, `on_stop`, `on_config_change`.
  *Planned.*

Drivers and connectors are the first two *consumers* of the kernel (via
`discover`) rather than two bespoke discovery implementations; full migration of
their registries to `Registry` remains follow-up.

## The seams — a plug-in group for every part

| Part | Today | Group | Unlocks |
|---|---|---|---|
| **Control modes** | hardcoded dict in `controller.py` | `vanchor.modes` | new autopilot behaviours as packs |
| **Route / nav planners** | fastest/shoreline/island/survey/contour hardcoded | `vanchor.planners` | new routing strategies |
| **Commands** | `CommandDispatcher` branch-per-type | `vanchor.commands` | new UI actions / endpoints |
| **Telemetry** | fixed dict in `TelemetryBuilder` | `vanchor.telemetry` | additive, **namespaced** extra keys |
| **Estimators / fusion** | hardcoded | `vanchor.estimators` | alt sensor fusion, bias learners |
| **UI views / widgets / panels** | `views.js` VIEW_REGISTRY + static partials | `vanchor.views` | custom on-water screens |
| **Alarms / notifiers** | push only | `vanchor.notifiers` | MQTT, webhook, email, … |
| **Analysis metrics / report cards** | hardcoded | `vanchor.metrics` | shared eval/tuning recipes |
| **Sim models / faults / sea-states** | `sim.model` switch | `vanchor.sim` | shared regression scenarios |
| **Data (no code)** | ad hoc | — | boat profiles, gains, ML policies, depth charts, routes, scenarios |

Start with **data + sim** (no code execution, trivially safe), then **modes /
telemetry / commands** (pure additive), then **views** (sandboxed UI), and only
then anything that touches hardware.

## Adding a whole new part (meta-extensibility)

A plug-in that wants to add an *entirely new subsystem* — not just slot into an
existing seam — receives a `Capability` (the **kernel object**) with exactly
these verbs. That set is enough to build almost anything, and it never exposes
the `Runtime`, the motor, or the governor:

- **event bus** — pub/sub on *namespaced* topics (`ext.<name>.*`); a read-only
  `state_snapshot()` (never a mutable handle).
- **scheduler** — `every(hz, fn)` to run a periodic task on the existing async loop.
- **config** — a persisted, namespaced key/value slice.
- **routes** — mount HTTP/WS endpoints under `/ext/<name>/…`.
- **telemetry** — contribute namespaced frame keys.
- **ui** — contribute a JS/CSS/partial asset bundle + a view/widget descriptor.
- **alarms** — raise a named alarm through the existing alert path.

Because the plug-in only ever talks to the capability, core evolves underneath it,
and a hung/rogue plug-in degrades safely — the supervisor + hardware watchdog
already zero the motor on fault. Run untrusted plug-ins in a thread/subprocess.

## The safety floor — never a plug-in concern

This is the first design constraint, not an afterthought. A plug-in **must never
be able to weaken** the safety floor:

- the **motor deadman**, the **link/fix failsafes**, and **STOP-always-works**
  live in core and are off-limits to plug-ins;
- plug-ins get the **narrow capability**, never the `Runtime`, never the motor,
  never the governor internals;
- a **safety-floor config lockout** hot-reload, profiles, backup-restore, and
  packs can never override;
- everything a plug-in adds is **additive/observational by default**; anything
  touching hardware, serial, or the network is **consent-gated** (as connectors
  already are).

If a capability can't be exposed without risking the floor, it isn't exposed.

## Distribution — HACS-style packs

- A pack is a small **pip-installable** package (or, for data/view packs, a
  zip/JSON bundle) with a manifest: name, version, kind, targeted API version,
  declared capabilities, author.
- Core discovers installed packs via **entry points** — no runtime code download;
  install is an explicit, offline-on-the-boat `pip install`.
- **Registry**: model it on HACS — a **git-backed index** (a JSON list of repos +
  versions), not a hosted store. GitHub/Gitea is the CDN; nothing to run.
- **Curation tiers**: `core` (shipped) → `verified` (reviewed, meets the safety
  contract, signed/checksummed) → `community` (unreviewed, clearly labelled, extra
  friction + capability limits). Drivers especially should be `verified` before
  one-click.
- The app ships an **offline-first** installed-packs view; browsing/installing new
  packs needs connectivity (done on shore, not mid-lake).

## Two real wrinkles

1. **UI packs vs. the no-build frontend + service-worker shell.** A view pack
   contributes JS/CSS/partials that must land in the SW `SHELL` precache. Fix: the
   backend serves a plug-in **asset manifest** and the SW precache list is
   generated from it instead of a static array.
2. **Telemetry contract drift.** The additive rule (namespaced keys, never rename
   or remove) plus the existing `/api/contract` self-check keeps plug-in telemetry
   from silently breaking clients.

## Adjacent community loops

- **Session upload:** opt-in "upload last session on WiFi" turns real-water
  incidents into replayable sim scenarios (a natural *sim* pack).
- **Depth charts:** the cmapper GeoJSON import is a natural first *data* pack.
- **ML anchor policies:** `anchor_policy.json` / `anchor_leif.json` are already
  just weights — a trained tune is a trivial data pack.

## Phasing

1. **Keystone — `vanchor.ext`.** Extract the shared discovery/registry/capability
   machinery; make drivers + connectors consume it. Then migrate **control modes**
   to the registry as the first new seam.
2. **Additive seams** — telemetry contributors, command handlers, planners,
   estimators, notifiers, metrics (each pure/observational, low risk).
3. **View packs** — the asset-manifest + SW-precache change; sandboxed UI.
4. **The registry + curation** — the git index, the installed-packs UI, signing.
5. **Driver packs at scale** — once the capability contract is proven, starting
   with the battery monitor as the reference non-core driver.

## Open questions to settle

- Signing/verification: how much, and who holds the keys for `verified`?
- Capability granularity + isolation: thread vs. process for a rogue/hung plug-in?
- Versioning: how do packs declare, and how does core enforce, the API version?
- Provenance for shared data (depth charts, ML policies).
