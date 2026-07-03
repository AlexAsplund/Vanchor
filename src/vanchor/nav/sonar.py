"""Live sonar / fishfinder ingest, merged with the charted depth map (#45).

A fishfinder reports the depth *right now, under the boat*. The imported chart
(``DepthMap``) says how deep the water is *supposed* to be there. When the two
disagree -- and especially when the sounder reads materially **shallower** than
the chart -- something is wrong (an uncharted shoal, a silted-in channel, a
dropped datum, the wrong chart loaded): the classic grounding trap. This module
turns the raw depth feed into a normalized :class:`Sounding`, looks up the
charted depth at the boat's position, and raises a DIVERGENCE ALERT for the UI /
telemetry to surface.

Everything here is a **pure function pipeline** (parse -> look up -> compare ->
write one state field), so it is trivially testable with a fake ``DepthMap`` and
a plain ``NavigationState`` -- no runtime, no event loop, no hardware.

Supported inputs:

* NMEA 0183 depth sentences -- ``DPT`` (below transducer + offset), ``DBT``
  (below transducer), ``DBS`` (below surface). Parsed by :mod:`vanchor.nav.nmea`.
* A simple **NMEA 2000 gateway / Deeper-style** payload: a JSON string / bytes /
  ``dict`` such as ``{"depth": 3.4}`` or the PGN 128267 "Water Depth" shape
  ``{"pgn": 128267, "fields": {"Depth": 3.2, "Offset": 0.2}}``.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass

from ..core.geo import haversine_m
from ..core.models import GeoPoint
from . import nmea

# Metres per degree of latitude (≈ constant); longitude scaled by cos(lat).
_M_PER_DEG_LAT = 111_320.0


# --------------------------------------------------------------------------- #
# Normalized value types
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Sounding:
    """One depth measurement from the live sounder, normalized to metres.

    ``depth_m`` is positive-down water depth. ``below`` records the datum the
    reading is referenced to (``"surface"`` or ``"transducer"``) and ``source``
    the wire format it came from (the NMEA sentence type, or ``"n2k"``), purely
    for telemetry / debugging -- the merge logic treats them all the same.
    """

    depth_m: float
    source: str = "sonar"
    below: str = "surface"


@dataclass(frozen=True)
class Divergence:
    """Result of comparing a live sounding against the chart at a position.

    ``delta_m`` is ``measured - charted`` (so a **negative** delta means the
    sounder reads shallower than the chart -- the dangerous case). ``charted_m``
    is ``None`` when the chart has no data near the boat, in which case there is
    nothing to compare and ``alert`` is ``False``.
    """

    measured_m: float
    charted_m: float | None
    delta_m: float
    alert: bool


# --------------------------------------------------------------------------- #
# Parsing: raw feed -> Sounding
# --------------------------------------------------------------------------- #
def _sentence_kind(sentence: str) -> str:
    """The 3-letter sentence type (talker id dropped), or ``""`` if not NMEA."""
    s = sentence.strip()
    if not s.startswith("$"):
        return ""
    head = s[1:].split("*", 1)[0].split(",", 1)[0]
    return head[-3:] if len(head) >= 3 else head


def sounding_from_sentence(sentence: str) -> Sounding | None:
    """Parse an NMEA 0183 depth sentence into a :class:`Sounding`.

    Returns ``None`` for a non-depth (or unparseable) sentence rather than
    raising, so a caller can feed it a mixed NMEA stream and keep only the depth
    fixes. ``DBT`` is flagged ``below="transducer"``; ``DPT``/``DBS`` are treated
    as below-surface (DPT already folds in the transducer offset).
    """
    try:
        parsed = nmea.parse(sentence)
    except nmea.NmeaError:
        return None
    if not isinstance(parsed, nmea.Depth):
        return None
    kind = _sentence_kind(sentence)
    below = "transducer" if kind == "DBT" else "surface"
    return Sounding(depth_m=parsed.depth_m, source=kind or "nmea", below=below)


_DEPTH_KEYS = ("depth", "Depth", "depth_m", "depthMeters", "depth_meters",
               "water_depth", "waterDepth", "wd", "value")
_OFFSET_KEYS = ("offset", "Offset", "transducer_offset", "transducerOffset")


def _first_float(d: dict, keys: tuple[str, ...]) -> float | None:
    for k in keys:
        if k in d:
            try:
                return float(d[k])
            except (TypeError, ValueError):
                continue
    return None


def sounding_from_payload(payload) -> Sounding | None:
    """Parse a simple NMEA 2000 gateway / Deeper-style payload into a Sounding.

    Accepts a ``dict``, or a JSON ``str`` / ``bytes`` that decodes to one. Reads
    the depth from any of the common depth keys (``depth``/``depth_m``/... or a
    nested ``"fields"`` object for the PGN 128267 shape) and folds in an
    ``Offset`` if present (positive offset = transducer-to-waterline, i.e. it
    converts a below-transducer reading toward below-surface). Returns ``None``
    when no depth can be found, so junk on the wire is simply dropped.
    """
    if isinstance(payload, (bytes, bytearray)):
        try:
            payload = payload.decode("utf-8", errors="replace")
        except Exception:  # pragma: no cover - defensive
            return None
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            return None
    if not isinstance(payload, dict):
        return None
    # PGN-style payloads nest the readings under "fields"; otherwise scan the
    # top level.
    fields = payload.get("fields")
    src = payload if not isinstance(fields, dict) else fields
    depth = _first_float(src, _DEPTH_KEYS)
    if depth is None:
        return None
    offset = _first_float(src, _OFFSET_KEYS) or 0.0
    return Sounding(depth_m=abs(depth) + offset, source="n2k", below="surface")


# --------------------------------------------------------------------------- #
# Chart lookup + divergence (pure)
# --------------------------------------------------------------------------- #
def _bbox_around(point: GeoPoint, radius_m: float) -> tuple[float, float, float, float]:
    """A (west, south, east, north) box of ~``radius_m`` around ``point``."""
    dlat = radius_m / _M_PER_DEG_LAT
    dlon = radius_m / (_M_PER_DEG_LAT * max(0.01, math.cos(math.radians(point.lat))))
    return (point.lon - dlon, point.lat - dlat, point.lon + dlon, point.lat + dlat)


def charted_depth_at(depth_map, point: GeoPoint | None,
                     radius_m: float = 25.0) -> float | None:
    """Charted depth (m) at ``point`` from the ``DepthMap``, or ``None``.

    Merges the two chart layers that carry depth: the recorded/imported
    **soundings** (``depth_map.points``) and the imported **contours**
    (isobaths). Returns the depth of the single nearest sample within
    ``radius_m`` metres (a sounding point or a contour vertex, whichever is
    closer); ``None`` when the chart has nothing near the boat, so an unmapped
    area simply yields no comparison instead of a false alert.

    Pure w.r.t. the map -- it only reads. ``depth_map`` is duck-typed (needs
    ``points`` and, optionally, ``contours`` + ``contours_in``) so a test can
    pass a tiny fake.
    """
    if point is None or depth_map is None:
        return None
    best_d: float | None = None
    best_dist = float(radius_m)

    for rec in getattr(depth_map, "points", None) or ():
        try:
            la, lo, d = rec[0], rec[1], rec[2]
        except (TypeError, IndexError, KeyError):
            continue
        dist = haversine_m(point, GeoPoint(la, lo))
        if dist <= best_dist:
            best_dist = dist
            best_d = float(d)

    # Contours are the primary imported chart; scan the vertices in a small
    # window around the boat and keep the nearest that beats any point found.
    contours = getattr(depth_map, "contours", None)
    contours_in = getattr(depth_map, "contours_in", None)
    if contours and callable(contours_in):
        bbox = _bbox_around(point, radius_m)
        try:
            windowed = contours_in(bbox=bbox, limit=200)
        except Exception:  # pragma: no cover - defensive (fake maps etc.)
            windowed = []
        for c in windowed or ():
            d = c.get("d")
            if d is None:
                continue
            for vtx in c.get("pts") or ():
                if not isinstance(vtx, (list, tuple)) or len(vtx) < 2:
                    continue
                dist = haversine_m(point, GeoPoint(vtx[0], vtx[1]))
                if dist <= best_dist:
                    best_dist = dist
                    best_d = float(d)

    return best_d


def divergence(measured_m: float, charted_m: float | None, *,
               tol_m: float = 1.0, tol_frac: float = 0.15,
               shallow_only: bool = True) -> Divergence:
    """Compare a live measured depth against the charted depth.

    The tolerance is ``max(tol_m, tol_frac * charted)`` -- an absolute floor plus
    a proportional band, so a metre of disagreement is noise in 30 m of water but
    a real alert in 3 m. With ``shallow_only`` (default) an alert fires **only**
    when the sounder reads shallower than the chart beyond tolerance (the
    grounding-risk case); set it ``False`` for a symmetric "they disagree" check.

    Never alerts when there is no charted depth, and never on a non-positive
    ``measured_m`` (0 usually means the sounder lost bottom lock -- not a real
    shoal), so a dropout can't false-trip the alarm.
    """
    if charted_m is None or measured_m <= 0.0 or charted_m <= 0.0:
        return Divergence(measured_m, charted_m, 0.0, False)
    delta = measured_m - charted_m
    tol = max(float(tol_m), float(tol_frac) * abs(charted_m))
    alert = (delta < -tol) if shallow_only else (abs(delta) > tol)
    return Divergence(measured_m, charted_m, delta, alert)


# --------------------------------------------------------------------------- #
# The pipeline: ingest one sounding into the shared state
# --------------------------------------------------------------------------- #
def ingest(state, sounding: Sounding | None, depth_map, *,
           position: GeoPoint | None = None, radius_m: float = 25.0,
           tol_m: float = 1.0, tol_frac: float = 0.15) -> Divergence:
    """Merge one live ``sounding`` with the chart and write the result to state.

    Updates ``state.sonar_depth_m`` (latest measured depth), and the
    ``charted_depth_m`` / ``depth_divergence_m`` / ``depth_divergence_alert``
    fields, then returns the :class:`Divergence` for the caller. ``position``
    defaults to ``state.position`` (the boat's current fix). A ``None`` sounding
    (an unparseable / non-depth message) is a no-op that leaves the alert as it
    was and returns a benign result.

    Deliberately does NOT touch ``state.depth_m`` (owned by the navigator's NMEA
    path) or any safety/motor field -- this is a pure telemetry/alerting overlay.
    """
    if sounding is None:
        return Divergence(getattr(state, "sonar_depth_m", 0.0), None, 0.0, False)

    measured = float(sounding.depth_m)
    state.sonar_depth_m = measured

    pos = position if position is not None else getattr(state, "position", None)
    charted = charted_depth_at(depth_map, pos, radius_m)
    div = divergence(measured, charted, tol_m=tol_m, tol_frac=tol_frac)

    state.charted_depth_m = charted if charted is not None else 0.0
    state.depth_divergence_m = div.delta_m
    state.depth_divergence_alert = div.alert
    return div
