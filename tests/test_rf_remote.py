"""Tests for the RfRemoteConnector (Task 6: the control grant + expiry deadman).

This is the SAFETY-CRITICAL connector — the first that can command the motor.
The tests lead the implementation (TDD) and are organised safety-first:

1. Ungranted STICK is dropped and the sink NEVER sees it; the loop survives;
   an ungranted "BTN STOP" STILL reaches the sink (Global Constraint 3).
2. The expiry deadman fires EXACTLY ONE zero command after silence, then stays
   quiet until sticks resume (which re-arms it).
3. STICK maps to the exact governed manual command with clamped floats.
4. Buttons map to the real command shapes (stop / anchor_hold / manual-zero).
5. Garbage lines are ignored + counted; out-of-range floats are clamped; a
   malformed STICK never yields an unclamped/NaN value.
6. Transport EOF -> zero command + reconnect attempt (no real sleeps).
7. debug() never raises and returns a string.

The command shapes are the REAL ones accepted by ``Runtime.handle_command`` ->
``Controller.handle_command`` (see src/vanchor/controller/controller.py):
  * stop           -> {"type": "stop"}
  * manual/STICK   -> {"type": "manual", "thrust": <float>, "steering": <float>}
  * anchor         -> {"type": "anchor_hold"}
"""

from __future__ import annotations

import asyncio
import dataclasses

import pytest

from vanchor.connectors.context import ConnectorContext
from vanchor.connectors.rf_remote import MANIFEST, RfRemoteConnector
from vanchor.core.events import EventBus
from vanchor.hardware.serial_link import FakeSerialTransport


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


class _Clock:
    """A tiny injectable monotonic clock — no real time passes in tests."""

    def __init__(self, t: float = 0.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


class _Sink:
    """A stub command sink that records every command it is handed."""

    def __init__(self) -> None:
        self.commands: list[dict] = []

    def __call__(self, cmd: dict) -> None:
        self.commands.append(cmd)


def _ctx(bus: EventBus, sink: _Sink, clock: _Clock, *, control: bool) -> ConnectorContext:
    """Build a context whose manifest carries (or withholds) the control grant.

    Enforcement lives in the context, keyed on ``manifest.control`` — so the
    ungranted case is modelled by handing the context a control=False manifest.
    """
    manifest = dataclasses.replace(MANIFEST, control=control)
    return ConnectorContext(bus, manifest, command_sink=sink, mono_fn=clock)


@pytest.fixture()
def bus() -> EventBus:
    return EventBus()


# ─────────────────────────────────────────────────────────────────────────────
# 1. SAFETY: ungranted STICK dropped; STOP still flows; loop survives
# ─────────────────────────────────────────────────────────────────────────────


def test_ungranted_stick_dropped_sink_never_called(bus: EventBus) -> None:
    sink = _Sink()
    clock = _Clock()
    conn = RfRemoteConnector(FakeSerialTransport(), mono_fn=clock)
    conn._ctx = _ctx(bus, sink, clock, control=False)

    conn._process_line("STICK 0.5 -0.2")

    assert sink.commands == []  # the motor command NEVER reached the sink
    assert conn._denied == 1


def test_ungranted_btn_stop_still_reaches_sink(bus: EventBus) -> None:
    """Constraint 3: STOP is accepted from any connector, granted or not."""
    sink = _Sink()
    clock = _Clock()
    conn = RfRemoteConnector(FakeSerialTransport(), mono_fn=clock)
    conn._ctx = _ctx(bus, sink, clock, control=False)

    conn._process_line("STICK 0.9 0.9")  # dropped
    conn._process_line("BTN STOP")       # must flow anyway
    conn._process_line("STICK 0.1 0.1")  # loop still alive; still dropped

    assert sink.commands == [{"type": "stop"}]
    assert conn._denied == 2  # both sticks denied, loop survived between them


# ─────────────────────────────────────────────────────────────────────────────
# 2. EXPIRY DEADMAN
# ─────────────────────────────────────────────────────────────────────────────


def test_expiry_fires_exactly_one_zero_command(bus: EventBus) -> None:
    sink = _Sink()
    clock = _Clock()
    conn = RfRemoteConnector(FakeSerialTransport(), mono_fn=clock, expiry_s=1.0)
    conn._ctx = _ctx(bus, sink, clock, control=True)

    conn._process_line("STICK 0.5 -0.2")
    assert sink.commands == [{"type": "manual", "thrust": 0.5, "steering": -0.2}]

    # Not yet expired.
    clock.advance(0.9)
    conn._check_expiry()
    assert len(sink.commands) == 1

    # Past expiry -> exactly ONE zero command.
    clock.advance(0.2)  # total 1.1 s of silence
    conn._check_expiry()
    assert sink.commands[-1] == {"type": "manual", "thrust": 0.0, "steering": 0.0}
    assert len(sink.commands) == 2

    # Stays quiet on further checks (no second zero).
    clock.advance(5.0)
    conn._check_expiry()
    conn._check_expiry()
    assert len(sink.commands) == 2


def test_sticks_resuming_rearms_the_deadman(bus: EventBus) -> None:
    sink = _Sink()
    clock = _Clock()
    conn = RfRemoteConnector(FakeSerialTransport(), mono_fn=clock, expiry_s=1.0)
    conn._ctx = _ctx(bus, sink, clock, control=True)

    conn._process_line("STICK 0.4 0.0")
    clock.advance(1.5)
    conn._check_expiry()  # fires zero #1
    assert sink.commands[-1] == {"type": "manual", "thrust": 0.0, "steering": 0.0}
    zero_count_1 = sum(
        1 for c in sink.commands if c == {"type": "manual", "thrust": 0.0, "steering": 0.0}
    )
    assert zero_count_1 == 1

    # Sticks resume (non-zero) -> re-armed.
    conn._process_line("STICK 0.6 0.1")
    clock.advance(1.5)
    conn._check_expiry()  # fires zero #2
    zero_count_2 = sum(
        1 for c in sink.commands if c == {"type": "manual", "thrust": 0.0, "steering": 0.0}
    )
    assert zero_count_2 == 2


def test_deadman_does_not_fire_when_last_stick_was_zero(bus: EventBus) -> None:
    """A zeroed stick then silence must NOT emit a redundant zero command."""
    sink = _Sink()
    clock = _Clock()
    conn = RfRemoteConnector(FakeSerialTransport(), mono_fn=clock, expiry_s=1.0)
    conn._ctx = _ctx(bus, sink, clock, control=True)

    conn._process_line("STICK 0.0 0.0")  # explicit zero (1 command)
    clock.advance(5.0)
    conn._check_expiry()

    assert sink.commands == [{"type": "manual", "thrust": 0.0, "steering": 0.0}]


def test_ungranted_deadman_never_reaches_sink(bus: EventBus) -> None:
    """Without control, a stick is denied so there is nothing to zero; the
    watchdog must never sneak a motor command past the grant."""
    sink = _Sink()
    clock = _Clock()
    conn = RfRemoteConnector(FakeSerialTransport(), mono_fn=clock, expiry_s=1.0)
    conn._ctx = _ctx(bus, sink, clock, control=False)

    conn._process_line("STICK 0.5 0.5")  # denied
    clock.advance(5.0)
    conn._check_expiry()

    assert sink.commands == []


# ─────────────────────────────────────────────────────────────────────────────
# 3./4. Mapping to the REAL command shapes
# ─────────────────────────────────────────────────────────────────────────────


def test_stick_maps_to_governed_manual_command(bus: EventBus) -> None:
    sink = _Sink()
    clock = _Clock()
    conn = RfRemoteConnector(FakeSerialTransport(), mono_fn=clock)
    conn._ctx = _ctx(bus, sink, clock, control=True)

    conn._process_line("STICK 0.5 -0.2")

    assert sink.commands == [{"type": "manual", "thrust": 0.5, "steering": -0.2}]


def test_button_command_mapping(bus: EventBus) -> None:
    sink = _Sink()
    clock = _Clock()
    conn = RfRemoteConnector(FakeSerialTransport(), mono_fn=clock)
    conn._ctx = _ctx(bus, sink, clock, control=True)

    conn._process_line("BTN STOP")
    conn._process_line("BTN ANCHOR")
    conn._process_line("BTN MANUAL")

    assert sink.commands == [
        {"type": "stop"},
        {"type": "anchor_hold"},
        {"type": "manual", "thrust": 0.0, "steering": 0.0},
    ]


def test_ping_is_a_noop_but_counts_as_a_line(bus: EventBus) -> None:
    sink = _Sink()
    clock = _Clock()
    conn = RfRemoteConnector(FakeSerialTransport(), mono_fn=clock)
    conn._ctx = _ctx(bus, sink, clock, control=True)

    conn._process_line("PING")

    assert sink.commands == []
    assert conn._dropped == 0  # PING is a valid line, not garbage
    assert conn._lines == 1


# ─────────────────────────────────────────────────────────────────────────────
# 5. Garbage + clamping
# ─────────────────────────────────────────────────────────────────────────────


def test_garbage_lines_ignored_and_counted(bus: EventBus) -> None:
    sink = _Sink()
    clock = _Clock()
    conn = RfRemoteConnector(FakeSerialTransport(), mono_fn=clock)
    conn._ctx = _ctx(bus, sink, clock, control=True)

    for line in ("", "hello world", "STICK", "STICK 0.1", "STICK a b",
                 "BTN WOBBLE", "STICK 0.1 0.2 0.3"):
        conn._process_line(line)

    assert sink.commands == []
    assert conn._dropped == 7


def test_out_of_range_stick_is_clamped(bus: EventBus) -> None:
    sink = _Sink()
    clock = _Clock()
    conn = RfRemoteConnector(FakeSerialTransport(), mono_fn=clock)
    conn._ctx = _ctx(bus, sink, clock, control=True)

    conn._process_line("STICK 5 -9")
    conn._process_line("STICK -3.5 42")

    assert sink.commands == [
        {"type": "manual", "thrust": 1.0, "steering": -1.0},
        {"type": "manual", "thrust": -1.0, "steering": 1.0},
    ]


def test_nonfinite_stick_is_rejected_as_garbage(bus: EventBus) -> None:
    """nan/inf must never slip through the clamp as a live motor value."""
    sink = _Sink()
    clock = _Clock()
    conn = RfRemoteConnector(FakeSerialTransport(), mono_fn=clock)
    conn._ctx = _ctx(bus, sink, clock, control=True)

    conn._process_line("STICK nan 0.1")
    conn._process_line("STICK inf -inf")

    assert sink.commands == []
    assert conn._dropped == 2


# ─────────────────────────────────────────────────────────────────────────────
# 6. EOF -> zero command + reconnect (async, no real sleeps)
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_eof_submits_zero_and_reconnects(bus: EventBus) -> None:
    sink = _Sink()
    clock = _Clock()
    transport = FakeSerialTransport()
    conn = RfRemoteConnector(
        transport, mono_fn=clock, backoff_start=0.0, backoff_max=0.0,
    )
    conn._ctx = _ctx(bus, sink, clock, control=True)

    await conn.start(conn._ctx)
    try:
        # Process a real stick first so there is a link, then drop the link.
        transport.feed("STICK 0.5 0.5")
        transport.feed_eof()
        # Let the read loop run: consume the stick, hit EOF, submit zero, reopen.
        for _ in range(50):
            await asyncio.sleep(0)
            if transport.open_calls >= 2 and any(
                c == {"type": "manual", "thrust": 0.0, "steering": 0.0}
                for c in sink.commands
            ):
                break
    finally:
        await conn.stop()

    assert {"type": "manual", "thrust": 0.5, "steering": 0.5} in sink.commands
    assert {"type": "manual", "thrust": 0.0, "steering": 0.0} in sink.commands
    assert transport.open_calls >= 2  # reconnected after EOF


@pytest.mark.asyncio
async def test_reconnect_backs_off_over_failed_opens(bus: EventBus) -> None:
    sink = _Sink()
    clock = _Clock()
    transport = FakeSerialTransport()
    transport.fail_opens(2)  # first two reopen attempts fail
    conn = RfRemoteConnector(
        transport, mono_fn=clock, backoff_start=0.0, backoff_max=0.0,
    )
    conn._ctx = _ctx(bus, sink, clock, control=True)

    await conn.start(conn._ctx)
    try:
        transport.feed_eof()
        for _ in range(80):
            await asyncio.sleep(0)
            if transport.open_calls >= 4:
                break
    finally:
        await conn.stop()

    # initial open + at least the two failed reopens + one success
    assert transport.open_calls >= 4


# ─────────────────────────────────────────────────────────────────────────────
# 7. Manifest + debug
# ─────────────────────────────────────────────────────────────────────────────


def test_manifest_declares_control_and_no_bus_topics() -> None:
    assert MANIFEST.name == "rf-remote"
    assert MANIFEST.control is True
    assert MANIFEST.consumes == ()
    assert MANIFEST.produces == ()
    assert len(MANIFEST.grant_lines) == 2


def test_debug_never_raises_and_is_a_string(bus: EventBus) -> None:
    conn = RfRemoteConnector(FakeSerialTransport())
    assert isinstance(conn.debug(), str)  # before start

    clock = _Clock()
    sink = _Sink()
    conn._ctx = _ctx(bus, sink, clock, control=True)
    conn._process_line("STICK 0.3 0.4")
    out = conn.debug()
    assert isinstance(out, str)
    assert "rf-remote" in out or "RfRemote" in out
