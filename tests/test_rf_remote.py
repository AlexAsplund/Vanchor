"""Tests for the RfRemoteConnector (Task 6: the control grant + expiry deadman).

This is the SAFETY-CRITICAL connector — the first that can command the motor.
The tests lead the implementation (TDD) and are organised safety-first:

1. Ungranted STICK is dropped and the sink NEVER sees it; the loop survives;
   an ungranted "BTN STOP" STILL reaches the sink (Global Constraint 3).
2. The expiry deadman fires EXACTLY ONE ``{"type":"stop"}`` neutralizer after
   silence (only when the remote is the ACTIVE driver), then stays quiet until
   sticks resume (which re-arms it).  Mode buttons disarm the latch so the
   deadman does not fire while the autopilot owns the motor.
3. STICK maps to the exact governed manual command with clamped floats.
4. Buttons map to the real command shapes (stop / anchor_hold / manual-zero).
5. Garbage lines are ignored + counted; out-of-range floats are clamped; a
   malformed STICK never yields an unclamped/NaN value.
6. Transport EOF -> ``{"type":"stop"}`` neutralizer (ONLY when remote is the
   ACTIVE driver) + reconnect attempt (no real sleeps); otherwise reconnects
   silently.
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

    # Past expiry -> exactly ONE neutralizing command (a guaranteed-path stop).
    clock.advance(0.2)  # total 1.1 s of silence
    conn._check_expiry()
    assert sink.commands[-1] == {"type": "stop"}
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
    conn._check_expiry()  # fires neutralizer #1
    assert sink.commands[-1] == {"type": "stop"}
    stop_count_1 = sum(1 for c in sink.commands if c == {"type": "stop"})
    assert stop_count_1 == 1

    # Sticks resume (non-zero) -> re-armed.
    conn._process_line("STICK 0.6 0.1")
    clock.advance(1.5)
    conn._check_expiry()  # fires neutralizer #2
    stop_count_2 = sum(1 for c in sink.commands if c == {"type": "stop"})
    assert stop_count_2 == 2


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


def test_btn_anchor_disarms_deadman(bus: EventBus) -> None:
    """FIX 1: a mode button hands control off, so it must DISARM the deadman.

    Hazard: STICK arms the latch, BTN ANCHOR puts the boat in spot-lock, radio
    goes quiet — the stale latch must NOT let the watchdog yank the boat out of
    its autonomous anchor hold with a neutralizing command.
    """
    sink = _Sink()
    clock = _Clock()
    conn = RfRemoteConnector(FakeSerialTransport(), mono_fn=clock, expiry_s=1.0)
    conn._ctx = _ctx(bus, sink, clock, control=True)

    conn._process_line("STICK 0.5 0.5")  # arms the deadman
    conn._process_line("BTN ANCHOR")      # hands control to the autopilot
    clock.advance(5.0)                    # radio silence well past expiry
    conn._check_expiry()

    # ONLY the stick and the anchor_hold — no neutralizer of ANY type.
    assert sink.commands == [
        {"type": "manual", "thrust": 0.5, "steering": 0.5},
        {"type": "anchor_hold"},
    ]


def test_deadman_neutralizes_with_stop(bus: EventBus) -> None:
    """FIX 3: the deadman's neutralizer is a guaranteed-path {"type": "stop"}."""
    sink = _Sink()
    clock = _Clock()
    conn = RfRemoteConnector(FakeSerialTransport(), mono_fn=clock, expiry_s=1.0)
    conn._ctx = _ctx(bus, sink, clock, control=True)

    conn._process_line("STICK 0.5 0.5")
    clock.advance(1.5)  # radio silence past expiry
    conn._check_expiry()

    assert sink.commands[-1] == {"type": "stop"}
    assert sum(1 for c in sink.commands if c == {"type": "stop"}) == 1


def test_neutralization_survives_revoked_grant(bus: EventBus) -> None:
    """FIX 3: neutralization must reach the motor even if the grant is revoked.

    Build the context UNGRANTED (control=False) and force the latch armed
    directly — this simulates the control grant being revoked mid-drive while a
    non-zero stick was the active driver. Because the neutralizer is a
    {"type": "stop"} it bypasses the grant (Constraint 3) and STILL reaches the
    sink; a manual-zero would have been denied and never arrived. This test must
    FAIL if anyone reverts fix 3 to a manual-zero neutralizer.
    """
    sink = _Sink()
    clock = _Clock()
    conn = RfRemoteConnector(FakeSerialTransport(), mono_fn=clock, expiry_s=1.0)
    conn._ctx = _ctx(bus, sink, clock, control=False)  # ungranted / revoked

    # Force the active-driver latch armed (simulate revoke-mid-drive).
    conn._last_stick_mono = clock.t
    conn._last_stick_nonzero = True

    clock.advance(1.5)  # past expiry
    conn._check_expiry()

    assert sink.commands == [{"type": "stop"}]


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
        # Let the read loop run: consume the stick, hit EOF, neutralize, reopen.
        for _ in range(50):
            await asyncio.sleep(0)
            if transport.open_calls >= 2 and {"type": "stop"} in sink.commands:
                break
    finally:
        await conn.stop()

    assert {"type": "manual", "thrust": 0.5, "steering": 0.5} in sink.commands
    assert {"type": "stop"} in sink.commands  # FIX 3: guaranteed-path neutralizer
    assert transport.open_calls >= 2  # reconnected after EOF


@pytest.mark.asyncio
async def test_eof_only_neutralizes_active_driver(bus: EventBus) -> None:
    """FIX 2: the EOF neutralization is gated on the SAME active-driver latch as
    the deadman — a control-INPUT link loss must not disturb an autonomous mode
    the remote isn't driving.
    """
    # (i) remote is NOT the active driver (handed off via BTN ANCHOR) -> EOF
    #     must NOT neutralize.
    sink = _Sink()
    clock = _Clock()
    transport = FakeSerialTransport()
    conn = RfRemoteConnector(
        transport, mono_fn=clock, backoff_start=0.0, backoff_max=0.0,
    )
    conn._ctx = _ctx(bus, sink, clock, control=True)

    await conn.start(conn._ctx)
    try:
        transport.feed("STICK 0.5 0.5")  # arms
        transport.feed("BTN ANCHOR")      # hands off -> disarms
        transport.feed_eof()
        for _ in range(50):
            await asyncio.sleep(0)
            if transport.open_calls >= 2:
                break
    finally:
        await conn.stop()

    assert {"type": "stop"} not in sink.commands
    assert {"type": "manual", "thrust": 0.0, "steering": 0.0} not in sink.commands
    assert sink.commands == [
        {"type": "manual", "thrust": 0.5, "steering": 0.5},
        {"type": "anchor_hold"},
    ]
    assert transport.open_calls >= 2  # still reconnected

    # (ii) remote IS the active driver -> EOF neutralizes with exactly one stop.
    sink2 = _Sink()
    clock2 = _Clock()
    transport2 = FakeSerialTransport()
    conn2 = RfRemoteConnector(
        transport2, mono_fn=clock2, backoff_start=0.0, backoff_max=0.0,
    )
    conn2._ctx = _ctx(bus, sink2, clock2, control=True)

    await conn2.start(conn2._ctx)
    try:
        transport2.feed("STICK 0.5 0.5")
        transport2.feed_eof()
        for _ in range(50):
            await asyncio.sleep(0)
            if transport2.open_calls >= 2 and {"type": "stop"} in sink2.commands:
                break
    finally:
        await conn2.stop()

    assert sum(1 for c in sink2.commands if c == {"type": "stop"}) == 1
    assert transport2.open_calls >= 2


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
