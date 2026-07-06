"""RF remote connector — the control grant, with an expiry deadman.

This is the SAFETY-CRITICAL connector: the first that can command the motor. A
cheap RF/serial handset sends a tiny line protocol; this bridges it to the boat
through the SAME governed path the app uses (``ctx.submit_command`` ->
``Runtime.handle_command``). Nothing here ever touches the bus or the motor
directly.

Line protocol (newline-delimited text over :class:`SerialTransport`):

* ``BTN STOP``               -> ``{"type": "stop"}``            (always accepted)
* ``BTN ANCHOR``             -> ``{"type": "anchor_hold"}``     (spot-lock here)
* ``BTN MANUAL``             -> ``{"type": "manual", "thrust": 0.0, "steering": 0.0}``
* ``STICK <thrust> <steer>`` -> ``{"type": "manual", "thrust": t, "steering": s}``
  with both floats clamped to ``[-1, 1]``; a non-finite value is rejected.
* ``PING``                   -> keep-alive; no command, no effect on the deadman.

Anything else is counted (``dropped``) and ignored. The command shapes above are
the REAL ones :meth:`Controller.handle_command` accepts (verified against
``src/vanchor/controller/controller.py``) — nothing is invented.

**Expiry deadman (the safety heart).** The remote is expected to stream STICK
updates continuously. A watchdog on the injectable monotonic clock watches for
*radio silence*: if the last STICK is older than ``expiry_s`` (default 1.0 s)
AND the remote is the ACTIVE driver (the ``_last_stick_nonzero`` latch is set),
it submits exactly ONE neutralizing ``{"type": "stop"}`` and then stays quiet
until sticks resume (which re-arms it). A transport EOF/error likewise
neutralizes — but ONLY when that same active-driver latch is set — then
reconnects with exponential backoff (mirroring the serial-device reader).

The latch is a genuine *active-driver* flag: a non-zero STICK arms it, and any
successfully submitted mode BUTTON (STOP / ANCHOR / MANUAL) DISARMS it, because
a mode button hands control to the autopilot or is itself a neutral state — the
deadman must not fire against a mode the remote is no longer driving. The
neutralizer is a ``stop`` (not a manual-zero) so it is guaranteed to reach the
motor even if the control grant was revoked mid-session (Global Constraint 3).

**The grant is enforced by the context, not here.** The connector always *tries*
``ctx.submit_command``; a :exc:`PermissionError` (ungranted) is caught, counted
(``denied``) and dropped, and the read loop keeps running. ``BTN STOP`` still
flows because the context forwards ``{"type": "stop"}`` from any connector
(Global Constraint 3).
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from typing import Callable

from ..hardware.serial_link import SerialTransport
from .base import Connector, ConnectorManifest
from .context import ConnectorContext
from .registry import register_connector

logger = logging.getLogger("vanchor.connectors.rf_remote")

# Throttle window (seconds) for repeated denied-command warnings.
_DENY_LOG_INTERVAL = 5.0

# The manual-mode engage command carried by ``BTN MANUAL`` — a zeroed manual
# command (there is no standalone "set mode manual"). This is a control-grant
# command, NOT the neutralizer.
_ZERO_CMD: dict = {"type": "manual", "thrust": 0.0, "steering": 0.0}

# The command the deadman fire AND the (gated) EOF path submit to neutralize the
# boat. It is a plain ``{"type": "stop"}`` deliberately (FIX 3): stop is
# guaranteed to flow through ``ctx.submit_command`` even if the control grant is
# revoked mid-session (Global Constraint 3), and it zeroes thrust + enters MANUAL
# via the governed path. With the active-driver gating of FIX 1/2 it can only
# fire when the remote WAS the active manual driver, so its mode effect is
# equivalent to the old manual-zero — but it can no longer be silently denied.
_NEUTRALIZE_CMD: dict = {"type": "stop"}


# ─────────────────────────────────────────────────────────────────────────────
# Manifest
# ─────────────────────────────────────────────────────────────────────────────

MANIFEST = ConnectorManifest(
    name="rf-remote",
    label="RF Remote",
    description=(
        "A physical RF/serial handset that can drive the motor (thrust + "
        "steering) and switch modes (STOP, anchor spot-lock, manual). Commands "
        "go through the same governed path as the app and expire after 1 s of "
        "radio silence, so a dropped link stops the boat."
    ),
    consumes=(),
    produces=(),
    control=True,
    grant_lines=(
        "Control the motor (thrust + steering) — commands go through the same "
        "governed path as the app and expire after 1 s of radio silence",
        "Engage modes: STOP, anchor (spot-lock), manual",
    ),
)


def _clamp_unit(x: float) -> float:
    """Clamp ``x`` to ``[-1.0, 1.0]`` (finite values only)."""
    if x < -1.0:
        return -1.0
    if x > 1.0:
        return 1.0
    return x


# ─────────────────────────────────────────────────────────────────────────────
# Connector
# ─────────────────────────────────────────────────────────────────────────────


class RfRemoteConnector(Connector):
    """Bridge an RF/serial handset to the governed command path with a deadman.

    Parameters
    ----------
    transport:
        The line-oriented :class:`SerialTransport` to read from (tests inject a
        :class:`~vanchor.hardware.serial_link.FakeSerialTransport`).
    mono_fn:
        Injectable monotonic clock (default :func:`time.monotonic`) — drives the
        expiry deadman so tests need no real sleeps.
    expiry_s:
        Radio-silence timeout after which the active-driver latch triggers a
        ``{"type": "stop"}`` neutralizer (default 1.0 s).  Only fires when the
        latch is set (remote is the ACTIVE driver); idle remotes are unaffected.
    watchdog_poll_s:
        How often the background watchdog re-checks the clock (default 0.1 s).
        Tests drive :meth:`_check_expiry` directly and ignore this.
    backoff_start / backoff_max:
        Exponential reconnect backoff bounds after a transport EOF/error
        (defaults 1.0 s / 15.0 s, mirroring the serial reader).
    """

    manifest = MANIFEST

    def __init__(
        self,
        transport: SerialTransport,
        *,
        mono_fn: Callable[[], float] = time.monotonic,
        expiry_s: float = 1.0,
        watchdog_poll_s: float = 0.1,
        backoff_start: float = 1.0,
        backoff_max: float = 15.0,
    ) -> None:
        self._transport = transport
        self._mono = mono_fn
        self._expiry_s = expiry_s
        self._watchdog_poll_s = watchdog_poll_s
        self._backoff_start = backoff_start
        self._backoff_max = backoff_max

        self._ctx: ConnectorContext | None = None
        self._stop = False
        self._read_task: asyncio.Task | None = None
        self._watchdog_task: asyncio.Task | None = None

        # Deadman state. ``_last_stick_mono`` is the clock time of the last
        # SUBMITTED stick; ``_last_stick_nonzero`` latches whether it moved the
        # motor. The deadman fires once (clearing the latch) and re-arms only
        # when a new non-zero stick is submitted.
        self._last_stick_mono: float | None = None
        self._last_stick_nonzero: bool = False
        self._expiry_fired: bool = False

        # Debug / introspection.
        self._last_line: str = ""
        self._last_cmd: dict | None = None
        self._last_cmd_mono: float | None = None
        self._lines: int = 0
        self._dropped: int = 0
        self._denied: int = 0
        self._last_deny_log: float = 0.0

    # ── Lifecycle ─────────────────────────────────────────────────────────── #

    async def start(self, ctx: ConnectorContext) -> None:
        """Start the read loop and the deadman watchdog."""
        self._ctx = ctx
        self._stop = False
        self._read_task = asyncio.ensure_future(self._run_read())
        self._watchdog_task = asyncio.ensure_future(self._run_watchdog())

    async def stop(self) -> None:
        """Stop both loops and close the transport."""
        self._stop = True
        for task in (self._read_task, self._watchdog_task):
            if task is not None:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
        self._read_task = None
        self._watchdog_task = None
        try:
            await self._transport.close()
        except Exception:  # noqa: BLE001 - best effort
            pass

    # ── Command submission (the ONLY seam to the motor) ───────────────────── #

    def _submit(self, cmd: dict) -> bool:
        """Submit ``cmd`` via the governed context. Returns True iff forwarded.

        A :exc:`PermissionError` (ungranted, non-STOP) is counted + throttle-
        logged + dropped; any other error is swallowed. Either way the read loop
        and watchdog survive. This is the ONLY path to a motor-affecting command
        — there is no raw-bus fallback anywhere in this module.
        """
        ctx = self._ctx
        if ctx is None:
            return False
        try:
            ctx.submit_command(cmd)
        except PermissionError:
            self._denied += 1
            now = self._mono()
            if (now - self._last_deny_log) >= _DENY_LOG_INTERVAL:
                self._last_deny_log = now
                logger.warning(
                    "rf-remote: command %r denied (no control grant); dropping",
                    cmd.get("type"),
                )
            return False
        except Exception as exc:  # noqa: BLE001 - never let the loop die
            logger.warning("rf-remote: command %r failed: %s", cmd.get("type"), exc)
            return False
        self._last_cmd = cmd
        self._last_cmd_mono = self._mono()
        return True

    # ── Line parsing / dispatch ───────────────────────────────────────────── #

    def _process_line(self, line: str) -> None:
        """Parse and dispatch a single protocol line (synchronous).

        Never raises: an unparseable line is counted (``dropped``) and ignored.
        """
        self._lines += 1
        self._last_line = line
        parts = line.split()
        if not parts:
            self._dropped += 1
            return
        head = parts[0].upper()

        if head == "PING":
            return  # keep-alive only; no command, no deadman effect

        if head == "BTN":
            self._process_button(parts)
            return

        if head == "STICK":
            self._process_stick(parts)
            return

        # Unknown verb.
        self._dropped += 1

    def _process_button(self, parts: list[str]) -> None:
        if len(parts) != 2:
            self._dropped += 1
            return
        which = parts[1].upper()
        if which == "STOP":
            submitted = self._submit({"type": "stop"})
        elif which == "ANCHOR":
            submitted = self._submit({"type": "anchor_hold"})
        elif which == "MANUAL":
            # No standalone "set mode manual" command exists; the "manual"
            # command IS the manual-mode engage — send it zeroed.
            submitted = self._submit(dict(_ZERO_CMD))
        else:
            self._dropped += 1
            return
        if submitted:
            # FIX 1 (disarm on hand-off): every successfully submitted mode
            # button DISARMS the deadman. The deadman only guards the case where
            # the remote is the ACTIVE driver; a mode button either hands control
            # to the autopilot (anchor) or is itself a neutral state (stop /
            # manual-engage). Without this, a stale stick latch would let the
            # watchdog yank the boat out of an autonomous anchor hold.
            self._disarm_deadman()

    def _disarm_deadman(self) -> None:
        """Clear the active-driver latch and its timestamp.

        After this the deadman is idle until a new non-zero stick re-arms it (see
        :meth:`_process_stick`). Used by the mode-button hand-off (FIX 1) and by
        the gated EOF path (FIX 2).
        """
        self._last_stick_nonzero = False
        self._last_stick_mono = None

    def _process_stick(self, parts: list[str]) -> None:
        if len(parts) != 3:
            self._dropped += 1
            return
        try:
            thrust = float(parts[1])
            steering = float(parts[2])
        except ValueError:
            self._dropped += 1
            return
        # Reject non-finite BEFORE clamping — nan/inf must never reach the motor.
        if not (math.isfinite(thrust) and math.isfinite(steering)):
            self._dropped += 1
            return
        thrust = _clamp_unit(thrust)
        steering = _clamp_unit(steering)
        cmd = {"type": "manual", "thrust": thrust, "steering": steering}
        if self._submit(cmd):
            # Only a SUBMITTED stick arms the deadman: if the command was denied
            # nothing moved the motor, so there is nothing to zero later.
            self._last_stick_mono = self._mono()
            self._last_stick_nonzero = thrust != 0.0 or steering != 0.0
            self._expiry_fired = False

    # ── Expiry deadman ────────────────────────────────────────────────────── #

    def _check_expiry(self) -> None:
        """Fire the deadman once if the last non-zero stick has gone stale.

        Synchronous + defensive: it can be driven directly by tests (no sleeps)
        and is called on a cadence by :meth:`_run_watchdog`. It never raises.
        """
        try:
            if self._last_stick_mono is None or not self._last_stick_nonzero:
                return
            age = self._mono() - self._last_stick_mono
            if age < self._expiry_s:
                return
            # Fire exactly one neutralizer, then disarm until sticks resume.
            # FIX 3: the neutralizer is a guaranteed-path {"type": "stop"}.
            # Submit before disarm so the cleared latch cannot double-fire:
            # both operations are synchronous with no await between them.
            self._expiry_fired = True
            self._submit(dict(_NEUTRALIZE_CMD))
            self._disarm_deadman()
        except Exception as exc:  # noqa: BLE001 - the deadman must never crash
            logger.warning("rf-remote: deadman check error: %s", exc)

    async def _run_watchdog(self) -> None:
        """Background loop: re-check the deadman every ``watchdog_poll_s``."""
        while not self._stop:
            await asyncio.sleep(self._watchdog_poll_s)
            if self._stop:
                break
            self._check_expiry()

    # ── Read loop + reconnect ─────────────────────────────────────────────── #

    async def _run_read(self) -> None:
        """Open the transport and read lines forever, reconnecting on drops.

        On EOF or any read error, a ``{"type": "stop"}`` neutralizer is
        submitted — but ONLY when the remote is the ACTIVE driver (the
        ``_last_stick_nonzero`` latch is set).  A link drop must not disturb
        an autonomous mode (e.g. anchor hold) the remote is no longer driving.
        After neutralizing, the transport is reopened with exponential backoff.
        Mirrors the serial-device supervised reader.
        """
        backoff = self._backoff_start
        # Initial open (with backoff on failure).
        while not self._stop:
            try:
                await self._transport.open()
                break
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning("rf-remote: open failed (%s); retrying", exc)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, self._backoff_max) if backoff else 0.0

        while not self._stop:
            try:
                line = await self._transport.read_line()
            except asyncio.CancelledError:
                raise
            except (ValueError, asyncio.LimitOverrunError) as exc:
                # Oversized/garbage line — discard, keep the link.
                self._dropped += 1
                logger.debug("rf-remote: garbage line discarded: %s", exc)
                continue
            except Exception as exc:  # noqa: BLE001 - EOF or transport error
                # FIX 2 (EOF gating): neutralize ONLY if the remote is currently
                # the ACTIVE driver — i.e. the deadman is armed. A control-INPUT
                # link loss must not disturb an autonomous mode the remote isn't
                # driving (e.g. an anchor hold engaged via BTN ANCHOR). This
                # deliberately amends the original brief's UNCONDITIONAL wording;
                # the spec-owner adjudicated that an input-only link drop should
                # not yank an autonomously-anchored boat into manual. Then disarm
                # (FIX 4: the cleared latch means no path can double-fire — the
                # concurrent watchdog now sees an idle deadman).
                if self._last_stick_nonzero:
                    logger.warning(
                        "rf-remote: link lost (%s); neutralizing + reconnecting", exc
                    )
                    # Submit before disarm so the cleared latch cannot
                    # double-fire; both operations are synchronous with no
                    # await between them.
                    self._submit(dict(_NEUTRALIZE_CMD))
                    self._disarm_deadman()
                else:
                    logger.warning(
                        "rf-remote: link lost (%s); not active driver, reconnecting", exc
                    )
                await self._reconnect()
                continue
            self._process_line(line)

    async def _reconnect(self) -> None:
        """Close and reopen the transport with exponential backoff."""
        try:
            await self._transport.close()
        except Exception:  # noqa: BLE001 - port may already be gone
            pass
        backoff = self._backoff_start
        while not self._stop:
            await asyncio.sleep(backoff)
            if self._stop:
                return
            try:
                await self._transport.open()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                backoff = min(backoff * 2, self._backoff_max) if backoff else 0.0
                logger.warning("rf-remote: reconnect failed (%s); retrying", exc)
                continue
            logger.info("rf-remote: link reconnected")
            return

    # ── Introspection ─────────────────────────────────────────────────────── #

    def status(self) -> dict:
        return {
            "lines": self._lines,
            "dropped": self._dropped,
            "denied": self._denied,
            "expiry_armed": self._last_stick_nonzero,
        }

    def debug(self) -> str:
        """Human-readable debug string. Never raises."""
        try:
            age = (
                None
                if self._last_cmd_mono is None
                else round(self._mono() - self._last_cmd_mono, 2)
            )
            armed = self._last_stick_nonzero
            return (
                "RfRemoteConnector (rf-remote)\n"
                f"  last_line   : {self._last_line!r}\n"
                f"  last_cmd    : {self._last_cmd!r} (age {age}s)\n"
                f"  expiry      : armed={armed} fired={self._expiry_fired} "
                f"expiry_s={self._expiry_s}\n"
                f"  counts      : lines={self._lines} dropped={self._dropped} "
                f"denied={self._denied}"
            )
        except Exception:  # noqa: BLE001
            return "RfRemoteConnector: debug error"


# ─────────────────────────────────────────────────────────────────────────────
# Factory + registration
# ─────────────────────────────────────────────────────────────────────────────


def _build(settings: dict) -> Connector:
    """Factory: build an :class:`RfRemoteConnector` from persisted settings."""
    from ..hardware.serial_link import PySerialTransport  # noqa: PLC0415

    port = str(settings.get("port", "/dev/ttyUSB0"))
    baudrate = int(settings.get("baudrate", 115200))
    expiry_s = float(settings.get("expiry_s", 1.0))
    transport = PySerialTransport(port, baudrate)  # pragma: no cover - real port
    return RfRemoteConnector(transport, expiry_s=expiry_s)


register_connector(
    "rf-remote",
    _build,
    label="RF Remote",
)
