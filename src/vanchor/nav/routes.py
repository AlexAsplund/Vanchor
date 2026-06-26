"""GPX route loading and saving.

A small, dependency-free reader/writer for GPX 1.1 route files built on the
stdlib :mod:`xml.etree.ElementTree`. We only care about waypoints, so both
free-standing ``<wpt>`` elements and ``<rtept>`` elements inside an ``<rte>``
are flattened into a single ordered list of :class:`~vanchor.core.models.Waypoint`.

Parsing is deliberately tolerant: the GPX default namespace is stripped so we
match by local tag name, individual points missing/holding bad coordinates are
skipped rather than aborting the whole load, and missing ``<name>`` elements are
defaulted to ``WP{i}``. Only XML that cannot be parsed at all raises a
:class:`ValueError`.
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET

from ..core.models import GeoPoint, Waypoint

logger = logging.getLogger("vanchor.routes")

GPX_NS = "http://www.topografix.com/GPX/1/1"


def _local(tag: str) -> str:
    """Strip an ``{namespace}`` prefix from an ElementTree tag."""
    return tag.rsplit("}", 1)[-1]


def _find_child_text(element: ET.Element, name: str) -> str | None:
    """Return the text of the first direct child with local tag ``name``."""
    for child in element:
        if _local(child.tag) == name:
            return (child.text or "").strip()
    return None


def _point_from_element(element: ET.Element, index: int) -> Waypoint | None:
    """Build a :class:`Waypoint` from a ``<wpt>``/``<rtept>`` element.

    Returns ``None`` (and logs) if the coordinates are missing or unparseable,
    so the caller can skip an individual bad point.
    """
    lat_raw = element.get("lat")
    lon_raw = element.get("lon")
    if lat_raw is None or lon_raw is None:
        logger.warning("skipping %s without lat/lon", _local(element.tag))
        return None
    try:
        lat = float(lat_raw)
        lon = float(lon_raw)
    except ValueError:
        logger.warning("skipping %s with bad coords %r/%r", _local(element.tag), lat_raw, lon_raw)
        return None

    name = _find_child_text(element, "name")
    if not name:
        name = f"WP{index}"
    return Waypoint(name=name, point=GeoPoint(lat=lat, lon=lon))


def parse_gpx(text: str) -> list[Waypoint]:
    """Parse GPX ``text`` into an ordered list of waypoints.

    Reads free-standing ``<wpt>`` elements first, then ``<rtept>`` elements
    inside each ``<rte>``. Tolerant of the GPX default ``xmlns``. Individual
    points that lack coordinates are skipped; only XML that fails to parse at
    all raises :class:`ValueError`.
    """
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise ValueError(f"malformed GPX/XML: {exc}") from exc

    waypoints: list[Waypoint] = []
    index = 0

    def consume(element: ET.Element) -> None:
        nonlocal index
        wp = _point_from_element(element, index)
        index += 1
        if wp is not None:
            waypoints.append(wp)

    # Top-level <wpt> elements.
    for element in root:
        if _local(element.tag) == "wpt":
            consume(element)

    # <rtept> inside each <rte>.
    for element in root:
        if _local(element.tag) == "rte":
            for child in element:
                if _local(child.tag) == "rtept":
                    consume(child)

    return waypoints


def serialize_gpx(waypoints: list[Waypoint], name: str = "route") -> str:
    """Serialize ``waypoints`` to a valid GPX 1.1 document as a string.

    The points are written as a single ``<rte>`` named ``name`` containing one
    ``<rtept>`` per waypoint. Missing names are defaulted to ``WP{i}``.
    """
    gpx = ET.Element(
        "gpx",
        {
            "version": "1.1",
            "creator": "vanchor-ng",
            "xmlns": GPX_NS,
        },
    )
    rte = ET.SubElement(gpx, "rte")
    rte_name = ET.SubElement(rte, "name")
    rte_name.text = name

    for i, wp in enumerate(waypoints):
        rtept = ET.SubElement(
            rte,
            "rtept",
            {"lat": repr(wp.point.lat), "lon": repr(wp.point.lon)},
        )
        pt_name = ET.SubElement(rtept, "name")
        pt_name.text = wp.name or f"WP{i}"

    ET.indent(gpx)
    body = ET.tostring(gpx, encoding="unicode")
    return f'<?xml version="1.0" encoding="UTF-8"?>\n{body}\n'
