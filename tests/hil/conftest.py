"""Fixtures for the HIL bench tests.

Two things happen here:

1. Every test in this directory is auto-tagged ``@pytest.mark.hil`` (so authors
   of new bench tests don't have to remember the marker), which means the whole
   directory is skipped by default -- see the repo-root ``conftest.py``.
2. A ``bench`` fixture provides a connection to the physical rig. It is written
   so that it degrades gracefully: if the bench cannot be reached it raises
   ``pytest.skip`` rather than erroring, so even a stray ``VANCHOR_HIL=1`` on a
   machine with nothing plugged in stays green.

The bench object is intentionally a thin, documented protocol (``send_command``
/ ``read_motion``) rather than a concrete driver, because the physical wiring
(serial port, baud, GPS source) differs per bench and belongs in the operator's
local environment, not in the committed test.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

_HIL_DIR = Path(__file__).parent.resolve()


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Tag tests *in this directory* with the ``hil`` marker.

    A ``pytest_collection_modifyitems`` hook receives every item in the whole
    session, so we must scope the auto-marking to files under ``tests/hil/`` --
    otherwise we would (wrongly) tag the entire suite as HIL and skip it.
    """
    for item in items:
        try:
            item_path = Path(str(item.fspath)).resolve()
        except Exception:  # pragma: no cover - defensive
            continue
        if _HIL_DIR in item_path.parents or item_path.parent == _HIL_DIR:
            item.add_marker(pytest.mark.hil)


@pytest.fixture
def bench():
    """Connect to the physical HIL bench, or skip if it is not reachable.

    The concrete driver is selected from the environment so the committed test
    carries no machine-specific wiring:

    * ``VANCHOR_HIL_PORT``  -- serial device for the Arduino motor controller
      (e.g. ``/dev/ttyACM0``); defaults to a common Linux value.
    * ``VANCHOR_HIL_BAUD``  -- serial baud rate (default 115200).

    Yields an object exposing ``send_command(thrust, steering)`` and
    ``read_motion()`` -> an object with ``.speed_mps`` / ``.heading_deg``. On a
    machine without the rig this raises :func:`pytest.skip`.
    """
    port = os.environ.get("VANCHOR_HIL_PORT", "/dev/ttyACM0")
    baud = int(os.environ.get("VANCHOR_HIL_BAUD", "115200"))

    try:  # pragma: no cover - exercised only on a real bench
        # Lazy import: pyserial is an optional extra and must not be required to
        # collect (skipped) HIL tests on a dev box or in CI.
        import serial  # type: ignore

        conn = serial.Serial(port, baud, timeout=1.0)
    except Exception as exc:  # pragma: no cover - no bench in CI
        pytest.skip(f"HIL bench not reachable on {port}@{baud}: {exc}")

    try:  # pragma: no cover - exercised only on a real bench
        yield _BenchAdapter(conn)
    finally:  # pragma: no cover
        conn.close()


class _BenchAdapter:  # pragma: no cover - only instantiated on a real bench
    """Adapts a raw serial connection to the motor-controller line protocol.

    The firmware (see ``firmware/``) accepts ``T<thrust> S<steering>\\n`` motor
    commands and streams back GPS/heading telemetry. This adapter is deliberately
    minimal; a real bench build would flesh out :meth:`read_motion` against the
    actual telemetry frames.
    """

    def __init__(self, conn) -> None:
        self._conn = conn

    def send_command(self, thrust: float, steering: float) -> None:
        line = f"T{thrust:+.3f} S{steering:+.3f}\n".encode()
        self._conn.write(line)
        self._conn.flush()

    def read_motion(self):
        raw = self._conn.readline().decode(errors="replace").strip()
        return _parse_motion(raw)


def _parse_motion(line: str):  # pragma: no cover - real-bench telemetry parsing
    """Parse a ``SPEED=<mps> HEAD=<deg>`` telemetry line into a small record."""
    from types import SimpleNamespace

    speed = heading = 0.0
    for tok in line.split():
        if tok.startswith("SPEED="):
            speed = float(tok[6:])
        elif tok.startswith("HEAD="):
            heading = float(tok[5:])
    return SimpleNamespace(speed_mps=speed, heading_deg=heading)
