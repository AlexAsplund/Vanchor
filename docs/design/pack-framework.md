# Extension Pack Framework (HACS-style) — Design

Status: **draft for review** · Owner: TBD · Target: post-1.0

## 1. Goal

Let the community extend Vanchor-NG — **hardware *and* UI** — without editing core
code, in a Home-Assistant / **HACS-style** model. The distributable unit is a
**pack**: a single bundle that can contribute device drivers, **brand-new device
types**, and GUI (HUD widgets, map overlays, panels) — wired together with
**config flows** and kept compatible across versions by **versioned schemas**.

The motivating example, end-to-end: an **I²C relay-board pack** that ships the
relay driver, *defines* the "relay/switch" device type, and contributes a HUD
panel of labelled switch buttons with marine icons — all set up through config
flow, installed as one pack.

### Decisions locked (see §4, §8)
1. **In-process execution is accepted** for code contributions (the HACS bargain).
   No sandbox; the mitigation is the trust badge + explicit at-your-own-risk
   opt-in + the conformance gate — not isolation.
2. **Compatibility policy:** capability/manifest/widget schemas are
   **additive-only within a major**; a breaking change bumps the major and ships
   a migration, and the app supports a *window* of past versions.
3. **Declarative-first** for the curated registry; code/JS contributions arrive
   mainly via custom/external repos and vetted contributions.

## 2. Concepts

| Term | Meaning |
|------|---------|
| **Pack** | The distributable/installable unit (a repo or directory). Bundles one or more **contributions** + assets. |
| **Contribution** | A typed thing a pack provides: a `capability`, a `driver`, a `hud_widget`, an `overlay`, a `panel`, or `assets`. |
| **Capability** | A device *type*: a versioned **data + command** contract. Core ships `gps`, `compass`, `motor`, `depth`; **a pack can define new ones** (e.g. `relay`). |
| **Driver** | An implementation of a capability for a specific device/protocol (declarative spec **or** code). |
| **GUI contribution** | A `hud_widget`, map `overlay`, or settings/dashboard `panel`, built from the **widget palette** (declarative) or a custom JS component. |
| **Extension point** | A named place the app exposes for GUI contributions: `hud`, `map-overlays`, `panels`. |
| **Config flow** | Schema-driven, multi-step GUI setup — for a device *or* a widget (labels, icons, layout). |
| **Repository / Registry** | A source of packs (curated community repo + user-added external repos); the merged catalog. |

## 3. The pack manifest

Packs are **YAML** throughout — human-editable, diff-friendly, and approachable
for non-coders (which reinforces the declarative-first goal). One `pack.yaml`
declares everything the pack contributes; each contribution is versioned and
independently loadable. (The app's existing config is already YAML, so this
shares the loader/validation path.)

```yaml
manifest_version: 1
id: marine_relay_board
name: Marine I²C Relay Board
author: "…"
min_app_version: "1.4.0"
contributes:
  capabilities: [relay]            # defines a NEW device type (see §5)
  drivers:      [generic_i2c_relay]
  hud_widgets:  [relay_switches]
  assets:       ["icons/*.svg"]
```

Trust is per-pack but **graded per contribution**: declarative contributions are
always safe; code drivers and custom-JS widgets inherit the pack's repository
trust (`official` / `community` / `custom`).

## 4. Trust model, tiers & the safety floor

The HACS bargain — but the boat drives a physical motor, so trust is **graded**,
and the riskier tiers are gated behind a hard **safety floor that consent cannot
waive**.

| Tier | What it is | Gate |
|------|-----------|------|
| **Declarative** | Parse specs / palette widgets — no code runs | Safe; any source. Preferred for the curated registry. |
| **Reviewed code** | Python/JS vetted into the curated registry | `official` / `community` badge; runs supervised + isolated. |
| **Custom aux** | External-repo code providing an **aux** capability/widget | "At your own risk" opt-in; runs out-of-process / in a worker; **cannot touch the control path** (enforced, §5). |
| **Custom core** | External-repo code that **provides/replaces a core capability** (gps/compass/motor) or control logic | **Allowed — with the strongest, multi-step warning** ("you are replacing a safety-critical component with unreviewed code"). Permitted *only* once the safety floor exists; runs isolated; output still passes the governor + hardware deadman; manual override stays reachable. |

Allowing the **Custom core** tier (an explicit project decision — people *can*
upload core packs, warned) is what makes the floor below mandatory rather than
nice-to-have.

### Safety floor (non-negotiable — a warning cannot buy past it)

No warning makes "a pack froze and the motor won't stop" acceptable. These are
hard prerequisites for **any** code or core-capability pack:

1. **Hardware/firmware motor deadman** — the motor controller zeros thrust if it
   gets no valid command within ~500 ms. STOP then survives *any* software
   freeze, crash, or hostile pack. The linchpin; build it **first**.
2. **A STOP path that doesn't share fate with packs** — operator STOP must not sit
   behind a frozen pack on the same event loop (it does today). Backstopped by (1).
3. **Untrusted code is isolated** — custom Python drivers in a supervised
   **subprocess** (a busy-loop/leak/crash costs one driver, not the boat); custom
   JS widgets in a **worker / sandboxed iframe** (can't freeze the HUD thread or
   hijack commands).
4. **The safety governor stays in the command path** — every control source,
   core-pack or built-in, passes the existing slew/reverse/limit governor before
   the motor.
5. **Manual override is always reachable** and is a core capability no pack can
   displace.

> No Python sandbox exists, so isolation (3), not the conformance gate, is what
> contains a crash/hang/leak — the gate (§10) checks output *shape*, not runtime
> stability. The reliability review confirmed that, as the code stands today, a
> single in-process pack busy-loop freezes the control loop **and** STOP; the
> floor closes that.

## 5. Custom device types (capabilities)

A capability is a **data contract + a command contract**, versioned. Packs can
**define new ones**, which is what makes the framework open-ended.

```yaml
# contributed by the relay pack — capabilities/relay.yaml
capabilities:
  relay:
    version: 1
    state:                              # what the device REPORTS
      channels: [{ id: string, on: bool }]
    commands:                           # what the app may SEND
      set:    { channel: string, on: bool }
      toggle: { channel: string }
```

- **Sensors** (a new read-only type) publish `state` into a **namespaced** area of
  the telemetry; **actuators** (relay) accept `commands`.
- **Control-path boundary — enforced, not just stated.** An *aux* capability
  (relay, extra sensor) lives on a **namespaced `aux.*` bus the control loop never
  reads**, and the loader's **allowlist** rejects any aux pack that tries to
  register as — or publish to — a core capability (`gps`/`compass`/`motor`/`depth`,
  `nav.fix`, `motor.command`). A standing test asserts no aux pack can change the
  fix or the motor command. (Today this is only prose; the reliability review
  flagged it as the one safety mechanism that *must* be code + CI.) **Core**
  capability packs are the separate, higher tier in §4 — allowed, but only behind
  the safety floor + strongest warnings, with their output still passing the
  governor + deadman.
- GUI binds to capabilities by name (§6), so a HUD widget can drive any
  capability's commands and reflect its state without the core knowing the type.

## 6. GUI contributions

The app exposes **extension points** packs can fill:

- `hud` — overlay widgets on the main view.
- `map-overlays` — Leaflet layers (registered into the existing layers control).
- `panels` — settings/dashboard cards.

Two ways to build one, same trust split as drivers:

- **Declarative widget (from the palette).** The app ships a closed component
  palette — `button-grid`, `toggle`, `gauge`, `value`, `label`, `icon`,
  `indicator`, … — and the widget is a *composition* spec **bound to capability
  data/commands**. No code runs. Covers the relay switch-panel, status readouts,
  most HUD needs.
- **Custom JS component** (a web component / "card", à la Lovelace custom cards)
  for what the palette can't express — at-your-own-risk for external packs.

**Binding** is the key idea: a widget declares *which capability state it shows*
and *which commands its controls send*. Example (the relay panel):

```yaml
hud_widgets:
  relay_switches:
    kind: declarative
    component: button-grid
    bind: { capability: relay }
    items: config.channels                    # from config flow (§7)
    item:
      label: "{{ item.label }}"
      icon:  "{{ item.icon }}"
      state: "state.channels[item.id].on"      # reflects device state
      onPress: { command: toggle, args: { channel: "{{ item.id }}" } }
```

## 7. Config flow (devices *and* widgets)

Same schema-driven, multi-step form for both. A device flow gathers
transport/port/calibration; a widget flow gathers labels, icons, layout. The
relay pack's flow does both in one wizard:

```yaml
config_schema:
  steps:
    - id: bus
      fields:
        - { key: i2c_address, type: hex,    label: "I²C address", default: "0x20" }
        - { key: count,       type: number, label: Channels,      default: 4 }
    - id: channels
      kind: repeat
      for: count                                  # one group per channel
      fields:
        - { key: label, type: text, label: "Name (e.g. Nav lights)" }
        - { key: icon,  type: icon, palette: marine }
    - id: place
      kind: layout
      target: hud                                 # where the panel sits
```

Field types are a closed, app-rendered set (`serial_port`, `hex`, `enum`,
`number`, `bool`, `text`, `icon`, `repeat`, `layout`, `action`). A device flow
may include a **probe** step that confirms the device responds (or validates
against a sim replay) before saving.

## 8. Backwards compatibility

The "older packs keep working" guarantee, across all contribution types:

1. **Versioned schemas, additive-only within a major** — capabilities
   (`relay@1`), the **widget palette** (`button-grid@1`), and the manifest
   (`manifest_version`). New optional fields never break old packs.
2. **Migrators** read old `manifest_version`/schema shapes forward; breaking
   changes bump the major and ship a migration. The app supports a *window* of
   past majors.
3. **Capability/feature negotiation** — packs advertise what they provide and
   require; the app uses what's there and degrades the rest.
4. **`min_app_version`** hides packs an old app can't load instead of failing at
   runtime.

## 9. Sim-first conformance & safety gate

Every contribution must pass conformance before it goes live:

- **Drivers:** replay a recorded byte-stream → assert valid capability data.
- **Capabilities:** schema is self-consistent; commands round-trip in a sim stub.
- **Widgets:** render in a headless harness against sample capability state;
  bindings resolve; declared commands exist on the bound capability.

This is the safety gate (a `custom` contribution that fails never reaches the
boat) and lets packs be **built and reviewed without hardware** — fitting the
project's sim-first ethos. A simulated relay/sensor stub exercises the whole
chain in CI.

## 10. Worked example — the I²C relay pack (end to end)

One installable pack contributes, and config flow wires up:

1. **Capability** `relay@1` — channel state + `set`/`toggle` commands (§5).
2. **Driver** `generic_i2c_relay` — declarative I²C map implementing `relay@1`;
   flow asks I²C address + channel count.
3. **HUD widget** `relay_switches` — a `button-grid` bound to `relay`; flow asks
   a label + marine icon per channel and where to place the panel (§6, §7).
4. **Assets** — marine SVG icons (nav lights, bilge pump, livewell, horn…).

Operator experience: install the pack → device menu shows "Marine I²C Relay
Board" → config flow (address, 4 channels, label + icon each, place on HUD) →
a switch panel appears on the HUD; pressing a button sends `toggle` to the
relay and the button reflects the real channel state. **Zero code touched.**

## 11. Mapping onto the codebase

| Today | Becomes |
|-------|---------|
| `hardware/interfaces.py` | Versioned **core** capability contracts; the bus also carries **custom** capabilities (namespaced, aux-only). |
| `serial_devices.py` drivers | First `official` drivers in the registry. |
| `devices.json` source-selection | Per-slot **device-instance** config from config flow. |
| `devices.js` / device card | Registry picklist + config-flow renderer + repo management. |
| HUD / overlays / map layers control | Named **extension points**; a **widget palette** + binding layer renders declarative widgets; custom-JS host for code widgets. |
| `core/config.py` data-dir | `packs/` dir (installed packs + registry cache). |

`yaw_rate` is promoted to a **measured** field so the HWT901B compass driver that
`provides` it replaces the heading-diff estimate — the device half's first real
driver, complementing the relay pack as the GUI+custom-type example.

## 12. Scope discipline & staged rollout

**MVP first.** A maintainability review flagged the full design as HACS-scale
against a much smaller codebase (no typed device-state bus, NMEA-string sensors,
flat string command-dispatch, hand-written vanilla-JS frontend with no build
step). The leanest version that still meets the goal:

- **Code-first device packs riding the existing source-selection /
  `reload_devices` machinery** — the device half grafts on for near-zero new core
  surface (no interpreter, no bus needed). **Lead with the HWT901B driver** (a
  small, clean win + promotes `yaw_rate` to a measured field), *not* the relay
  (which forces every heavy subsystem at once).
- **Formalize the JS extension API that already works** — `VA.map.addOverlay`,
  `VA.onTelemetry`, `VA.send`. `catch.js` / `analytics.js` are already overlay
  packs in all but name. Document them as the stable GUI-pack surface.
- **One tolerant `manifest_version`**, matching the app's existing
  "ignore-unknown / default-missing, partial configs always valid" config rule —
  not four independently-versioned schemas with migration matrices.

Defer the declarative driver interpreter, the widget-palette/binding DSL,
independent capability/palette versioning, and the registry/repo/trust machinery
until ≥3 real packs prove the shape (premature declarative formats are always
wrong).

### Stages

- **Stage 0 — Contracts & doc.** Version the core capabilities + a single manifest
  schema. This doc. HWT901B as the lead worked example.
- **Safety floor (build FIRST, before any code pack).** Hardware/firmware motor
  deadman + isolated STOP + subprocess isolation for untrusted drivers + worker/
  iframe for custom JS + the control-path allowlist (§4, §5). **Hard prerequisite
  for Stages 3–4** — especially the *Custom core* tier.
- **Stage 1 — Local pack runtime (code-first).** Pack loader from a `packs/` dir
  riding source-selection; aux capability namespacing; the documented JS API;
  conformance + adversarial harness. Ship the **HWT901B driver** locally.
- **Stage 2 — Community registry.** Curated catalog; trust badges; updates.
- **Stage 3 — Add-your-own external repos.** HACS-style custom repos with the
  at-your-own-risk opt-in. *(Requires the safety floor.)*
- **Stage 4 — Code / core contributions.** Out-of-process Python drivers + worker
  JS widgets, including **core-capability & control-logic packs behind the
  strongest warnings**. *(Requires the safety floor; this is the "upload core
  packs, warned" tier.)*

## 13. Open questions / risks

- **Contribution isolation:** a crashing driver/widget must degrade gracefully,
  not take the autopilot/HUD down. Supervised tasks; the existing failsafes apply.
- **Custom-capability ↔ control-loop boundary:** enforce in code that aux
  capabilities can never register as a core control source.
- **Widget-palette scope:** how rich before people reach for custom JS? Versioning
  the palette is part of the compat surface.
- **Update trust / signing** for code packs beyond the trust badge.
- **Port/bus/asset namespacing** across packs on one Pi.
- **Naming/taxonomy** for capabilities + manufacturer/model ids (avoid clashes).
