# API contract

Vanchor's HTTP/WebSocket API is a **versioned, self-describing contract** — the
lesson borrowed from SignalK: *the data model is the contract*. Rather than an
informal, drifting telemetry dict, the payload shape is declared in one place
([`src/vanchor/core/contract.py`](../src/vanchor/core/contract.py)) and served
live so any client can introspect it.

## Discover it at runtime

```
GET /api/contract
```

returns:

```json
{
  "schema_version": "1.0",
  "envelope_version": 3,
  "units": "angles in degrees, distances in metres, speed in knots ...",
  "telemetry": { "heading_deg": {"type": "number", "unit": "deg", "desc": "..."}, ... },
  "commands":  { "anchor_hold": {"desc": "PID position hold at a point"}, ... }
}
```

- **`schema_version`** — the payload contract version (semver-ish). Breaking a
  field's meaning/type/unit bumps it; additive fields are a minor bump.
- **`envelope_version`** — the WebSocket envelope protocol version (`{v, type,
  seq, ts}` from #21), separate from the payload schema.
- **`telemetry`** — every top-level field of `GET /api/state` / the WS telemetry
  frame, with a coarse `type`, an optional `unit`, and a one-line `desc`.
- **`commands`** — every command `type` the server accepts (`POST /api/command`
  / the WS command channel).

## It can't silently drift

[`tests/test_contract.py`](../tests/test_contract.py) fails if the server ever
emits a telemetry key, or the controller accepts a command, that isn't declared
here. So the contract stays honest without manual upkeep — add a field, declare
it, or CI is red.

## Units

Angles are **degrees** (true, 0–360), distances **metres**, speed-over-ground
**knots**, drift **m/s**, depth **metres**. (SignalK-style SI/radians is *not*
used on the wire — the mapping to SI lives in any future SignalK bridge, not the
core API.)

## Endpoints (overview)

- `GET /api/state` — a full telemetry snapshot (same shape as the WS frame).
- `GET /api/contract` — this contract.
- `POST /api/command` / WS — send a command (`{type, ...}`); STOP is dual-path.
- `GET /api/config/devices`, `GET /api/devices/serial-ports` — device config +
  auto-detected serial ports.

See [`docs/ui-contract.md`](ui-contract.md) for the full command payloads.
