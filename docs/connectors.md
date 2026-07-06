# Connectors — permissioned external integrations

A **connector** bridges vanchor's event bus to an external system (a TCP bridge, a
metrics server, an NMEA 2000 bus, an RF handset). Unlike a device driver (one
transducer → one signal), a connector is a multiplexed, often bidirectional bridge —
so it runs under an explicit **permission manifest** with **default-deny enforcement
and user consent**, mirroring the #43 `DriverContext` capability model.

## The trust model

Every connector declares a `ConnectorManifest`:

- `consumes` — bus topics it may *read out* of vanchor (e.g. `telemetry`, `nmea.out`).
- `produces` — topics it may *inject*; hard-limited to the ingress allowlist
  `{nmea.in, gps.fix_in, imu.in}`. Control topics (`motor.command`, …) are refused
  **even if a manifest lists them**.
- `control` — requests the governed command capability. Not a topic grant:
  `ctx.submit_command()` routes through `Runtime.handle_command`, the same validated
  path as the PWA (attributed in the command log as `connector:<name>`), where the
  safety floor re-clamps failsafes.
- `grant_lines` — the plain-language consent lines the UI shows before arming.

Rules that hold everywhere (enforced in `ConnectorContext`, tested in
`tests/test_connectors_core.py` / `test_connectors_runtime.py`):

1. **Default-deny allowlists only.** Anything not granted raises `PermissionError`.
2. **`{"type": "stop"}` always flows** — from any connector, granted or not. No grant
   can suppress STOP or the deadman; stopping is always safe.
3. **Consent is hash-bound.** Grants persist in `connectors.json` (data dir) with the
   manifest hash; a manifest change disarms the connector until the user re-approves
   in Settings → Devices → Connectors.
4. Defense-in-depth: even a connector that somehow published `motor.command` raw
   would move nothing — the motor is actuated only via `controller.motor.apply()`,
   never from that bus topic.

## Writing a connector

Subclass `Connector` (`src/vanchor/connectors/base.py`), register at import:

```python
MANIFEST = ConnectorManifest(name="...", label="...", description="...",
                             consumes=("telemetry",), produces=(), control=False,
                             grant_lines=("Read live telemetry",))

class MyConnector(Connector):
    manifest = MANIFEST
    async def start(self, ctx): ...   # subscribe/publish/submit via ctx ONLY
    async def stop(self): ...
    def debug(self) -> str: ...       # never raises; feeds the Debug view + recorder

register_connector("my-name", _build)  # build(settings: dict) -> Connector
```

In-tree modules in `src/vanchor/connectors/` self-register on import; pip-installed
packs use the `vanchor.connectors` entry-point group. Build settings come from the
grant store; the runtime injects `data_dir`. Lifecycle: armed connectors build+start
at boot (a failing connector is logged and skipped, never crashes startup) and can be
armed/disarmed live via `POST /api/connectors/{name}/arm`.

## Shipped connectors

| name | direction | notes |
|---|---|---|
| `nmea-tcp` | out `nmea.out` / in `nmea.in` | The reference connector (wraps the NMEA-TCP bridge). Legacy `nmea_tcp.enabled` config auto-arms it once; host/port edits re-sync into the grant at boot. |
| `metrics` | out `telemetry` | **Offline-first store-and-forward**: samples telemetry (1 Hz default) into gzip NDJSON parts under `data_dir/metrics_buffer/` (cap 50 MB, drop-oldest, survives restart) and POSTs parts to `settings.url` whenever the network is reachable (Bearer token optional). No url → pure local buffer. |
| `nmea2000` | in `gps.fix_in`, `nmea.in` / out CAN (+ opt-in thruster control) | Pure PGN codec (`nav/n2k.py`): 129025/129026 → rich `GpsFix`, 127250 → HDT, 128267 → DPT, 130306 decoded to debug. Egress broadcasts position + COG/SOG (course from the fusion ground-velocity vector) and — always on — the motor's own **thruster status** (PGN 128006 + 128008). **Thruster control** (PGN 128006 ingress) is opt-in, see below. **BENCH-VERIFY**: field layouts + SocketCAN transport are transcribed, not yet verified on a real bus. Single-frame PGNs only (no fast-packet). |
| `rf-remote` | control | The control-grant reference. Line protocol (`BTN STOP/ANCHOR/MANUAL`, `STICK t s`, `PING`) over a serial transport. **Deadman**: 1 s of stick silence while the remote is the *active driver* → one `{"type":"stop"}` (guaranteed path, survives grant revocation). Mode buttons hand off control and disarm the deadman — radio loss while anchored does **not** disturb the anchor hold. |

### Thruster (PGN 128006 / 128008)

The `nmea2000` connector participates in the N2K thruster ecosystem so the trolling
motor looks like a thruster to a control head.

- **Egress (always on, no grant):** alongside position/COG, it broadcasts the motor's
  current state as **128006 Thruster Control Status** (direction OFF when thrust is 0
  else Ready; speed % = `|thrust|·100`; azimuth = the commanded `steer_angle_deg` →
  rad) plus **128008 Thruster Motor Status** (current from the battery block if
  present, else NA). Both are single-frame per canboat.
- **Control ingress (opt-in, consent-gated):** turning on the connector setting
  `thruster_control` makes `_build` construct a **different manifest** — `control: true`
  plus an explicit grant line. Because the grant hash covers the manifest, flipping the
  setting **auto-disarms** the connector until the user re-consents to the new line
  (that re-consent, not any code flag, is the opt-in). A received 128006 addressed to
  our thruster id (or broadcast) maps to the governed
  `{"type":"manual","thrust":±speed/100,"steering":azimuth/max_steer_angle_deg}` via
  `submit_command` — never a raw bus write. Direction OFF → thrust 0. Steering is
  normalized by the `max_steer_angle_deg` setting (default 35°, since the connector
  can't read boat state) and clamped to `[-1,1]`.
- **Deadman (mirrors rf-remote exactly):** the active-driver latch arms only on a
  successfully-submitted *non-zero* command. On expiry (the received Command Timeout,
  clamped to `[0.25, 10] s`, else 1.0 s default) it submits exactly one guaranteed-path
  `{"type":"stop"}`, then disarms until commands resume. An OFF/zero command disarms;
  a transport EOF/error neutralizes **only** when the latch is armed. A loopback guard
  ignores 128006 frames from our own source address so our status broadcast can never
  self-command. **BENCH-VERIFY** the 128006/128008 layouts on a real bus.

## Known limitations

- The RF deadman cannot observe *external* mode changes: if the app engages anchor
  within the ~1 s expiry window after a non-zero stick, the deadman may still fire a
  stop (spring-return sticks that emit `STICK 0 0` disarm it first). Deliberate: the
  connector consumes no topics.
- N2K egress/ingress and the SocketCAN transport need a bench check against real
  hardware before first field use (flagged in-code).
