"""Unit tests for the breadcrumb track recorder."""

import pytest

from vanchor.core.geo import destination_point, haversine_m
from vanchor.core.models import GeoPoint
from vanchor.nav.track import TrackRecorder

HERE = GeoPoint(59.66275, 13.32247)


def test_records_only_when_far_enough():
    r = TrackRecorder(min_distance_m=10.0)
    r.start()
    r.maybe_record(HERE)
    r.maybe_record(destination_point(HERE, 5.0, 0.0))  # too close -> skipped
    r.maybe_record(destination_point(HERE, 15.0, 0.0))  # far enough -> kept
    assert len(r.points) == 2


def test_not_recording_when_stopped():
    r = TrackRecorder(min_distance_m=1.0)
    r.maybe_record(HERE)  # not started
    assert r.points == []
    r.start()
    r.maybe_record(HERE)
    r.stop()
    r.maybe_record(destination_point(HERE, 50.0, 0.0))
    assert len(r.points) == 1


def test_start_seeds_and_clears_previous():
    r = TrackRecorder(min_distance_m=1.0)
    r.start(seed=HERE)
    assert r.points == [HERE]
    r.start()  # fresh
    assert r.points == []


def test_as_waypoints_forward_and_reverse():
    r = TrackRecorder(min_distance_m=1.0)
    r.start()
    pts = [HERE, destination_point(HERE, 20.0, 90.0), destination_point(HERE, 40.0, 90.0)]
    for p in pts:
        r.maybe_record(p)
    fwd = r.as_waypoints()
    assert [w.point for w in fwd] == pts
    rev = r.as_waypoints(reverse=True)
    assert [w.point for w in rev] == list(reversed(pts))
    assert rev[0].name == "T0"  # renumbered from the new start


def test_max_points_bound():
    r = TrackRecorder(min_distance_m=0.0, max_points=5)
    r.start()
    for i in range(20):
        r.maybe_record(destination_point(HERE, i, 0.0))
    assert len(r.points) <= 5
