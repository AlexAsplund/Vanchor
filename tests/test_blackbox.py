"""Tests for the always-on black-box flight recorder (roadmap #20).

Covers the three behaviours the recorder promises:

* the ring stays bounded (fixed-size deque);
* an injected alarm dumps the ring to a file that contains the pre-trigger
  history (and a post-trigger tail);
* the DESIRED vs APPLIED commands are both captured, so a governor clamp
  (applied != desired) is recorded.

Plus the Runtime wiring: the governor hook is installed (default-on config) and
feeds the recorder without changing the governed command, and can be disabled.
"""

from __future__ import annotations

import gzip
import json
import os

from vanchor.controller.safety import SafetyStatus
from vanchor.core.config import AppConfig
from vanchor.core.models import ControlModeName, GeoPoint, GpsFix, MotorCommand
from vanchor.obs.blackbox import BlackBox


class _FakeState:
    """Minimal stand-in for NavigationState with just what a frame reads."""

    def __init__(self, lat=59.0, lon=18.0, heading=90.0, sog=1.0, dist=3.0):
        self.position = GeoPoint(lat, lon)
        self.mode = ControlModeName.ANCHOR_HOLD
        self.heading_deg = heading
        self.sog_knots = sog
        self.distance_to_anchor_m = dist
        self.controller_fault = None


def _bb(tmp_path, **kw):
    """A BlackBox with a controllable clock (a mutable list holding 'now')."""
    clock = {"t": 0.0}
    params = dict(
        enabled=True,
        capacity=kw.pop("capacity", 8),
        sample_period_s=kw.pop("sample_period_s", 0.0),
        post_trigger_frames=kw.pop("post_trigger_frames", 0),
    )
    params.update(kw)
    bb = BlackBox(str(tmp_path), now_fn=lambda: clock["t"], **params)
    return bb, clock


def _cmd(thrust=0.0, steering=0.0):
    return MotorCommand(thrust=thrust, steering=steering)


# --------------------------------------------------------------------------- #
# Ring stays bounded
# --------------------------------------------------------------------------- #
def test_ring_is_bounded(tmp_path):
    bb, clock = _bb(tmp_path, capacity=5, sample_period_s=0.0)
    st = _FakeState()
    status = SafetyStatus()
    for i in range(50):
        clock["t"] = float(i)
        bb.observe(_cmd(0.1), _cmd(0.1), status, st)
    frames = bb.frames()
    assert len(frames) == 5  # never grows past capacity
    # Oldest evicted: the retained frames are the most recent timestamps.
    assert [f["t"] for f in frames] == [45.0, 46.0, 47.0, 48.0, 49.0]


def test_low_rate_sampling_decimates(tmp_path):
    # At a 1 s sample period, sub-second ticks between samples are dropped.
    bb, clock = _bb(tmp_path, capacity=100, sample_period_s=1.0)
    st = _FakeState()
    status = SafetyStatus()
    for i in range(20):
        clock["t"] = i * 0.1  # 0.0, 0.1, ... 1.9  -> only ~2 samples land
        bb.observe(_cmd(), _cmd(), status, st)
    # Samples at t=0.0 (first, -inf gap) and t>=1.0 -> exactly 2 frames.
    assert [f["t"] for f in bb.frames()] == [0.0, 1.0]


# --------------------------------------------------------------------------- #
# Alarm -> dump with pre-trigger history
# --------------------------------------------------------------------------- #
def test_alarm_dumps_pretrigger_frames(tmp_path):
    bb, clock = _bb(tmp_path, capacity=20, sample_period_s=0.0, post_trigger_frames=0)
    st = _FakeState()
    quiet = SafetyStatus()
    # Build up pre-trigger history at distinct positions.
    for i in range(5):
        clock["t"] = float(i)
        st.position = GeoPoint(59.0 + i * 1e-4, 18.0)
        bb.observe(_cmd(0.2), _cmd(0.2), quiet, st)
    assert bb.dumps() == []  # nothing dumped yet

    # Drag alarm trips.
    clock["t"] = 5.0
    st.position = GeoPoint(59.0 + 5e-4, 18.0)
    bb.observe(_cmd(0.2), _cmd(0.2), SafetyStatus(drag_alarm=True), st)

    dumps = bb.dumps()
    assert len(dumps) == 1
    path = bb.path_for(dumps[0]["file"])
    assert path is not None and os.path.isfile(path)
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        doc = json.load(fh)
    # Pre-trigger frames present (all 5 quiet + the trigger frame = 6).
    assert doc["meta"]["alarms"] == ["drag_alarm"]
    frames = doc["frames"]
    assert len(frames) == 6
    # The earliest quiet frame is included (pre-trigger history).
    assert frames[0]["t"] == 0.0
    assert frames[0]["alarms"] == []
    # The trigger frame carries the alarm.
    assert frames[-1]["alarms"] == ["drag_alarm"]
    assert "drag_alarm" in doc["meta"]["active_alarms"]


def test_post_trigger_tail_captured_before_dump(tmp_path):
    bb, clock = _bb(tmp_path, capacity=50, sample_period_s=0.0, post_trigger_frames=3)
    st = _FakeState()
    quiet = SafetyStatus()
    for i in range(2):
        clock["t"] = float(i)
        bb.observe(_cmd(), _cmd(), quiet, st)

    # Alarm trips -> arms a 3-frame tail; no dump yet.
    clock["t"] = 2.0
    bb.observe(_cmd(), _cmd(), SafetyStatus(fix_lost=True), st)
    assert bb.dumps() == []

    # Three more ticks (alarm cleared) fill the tail, then it dumps.
    for i in range(3):
        clock["t"] = 3.0 + i
        bb.observe(_cmd(), _cmd(), quiet, st)
    dumps = bb.dumps()
    assert len(dumps) == 1
    with gzip.open(bb.path_for(dumps[0]["file"]), "rt", encoding="utf-8") as fh:
        doc = json.load(fh)
    # 2 pre + 1 trigger + 3 tail = 6 frames.
    assert len(doc["frames"]) == 6
    assert doc["meta"]["alarms"] == ["fix_lost"]


def test_alarm_must_transition_not_stay_high(tmp_path):
    # A persistently-high alarm dumps ONCE (on the rising edge), not every tick.
    bb, clock = _bb(tmp_path, capacity=20, sample_period_s=0.0, post_trigger_frames=0)
    st = _FakeState()
    for i in range(5):
        clock["t"] = float(i)
        bb.observe(_cmd(), _cmd(), SafetyStatus(nogo_stop=True), st)
    assert len(bb.dumps()) == 1


def test_controller_fault_and_link_failsafe_trigger(tmp_path):
    bb, clock = _bb(tmp_path, capacity=20, sample_period_s=0.0, post_trigger_frames=0)
    st = _FakeState()
    quiet = SafetyStatus()
    clock["t"] = 0.0
    bb.observe(_cmd(), _cmd(), quiet, st)
    # Controller fault (passed as a boolean by the caller) is an alarm source.
    clock["t"] = 1.0
    bb.observe(_cmd(), _cmd(), quiet, st, controller_fault=True)
    assert len(bb.dumps()) == 1
    # Link failsafe is a distinct source and a fresh rising edge -> new dump.
    clock["t"] = 2.0
    bb.observe(_cmd(), _cmd(), quiet, st, controller_fault=True, link_failsafe=True)
    assert len(bb.dumps()) == 2


# --------------------------------------------------------------------------- #
# desired != applied (governor clamp) is recorded
# --------------------------------------------------------------------------- #
def test_desired_vs_applied_recorded(tmp_path):
    bb, clock = _bb(tmp_path, capacity=20, sample_period_s=0.0, post_trigger_frames=0)
    st = _FakeState()
    clock["t"] = 0.0
    # Governor slew-limited the big thrust step: applied < desired.
    bb.observe(_cmd(thrust=1.0), _cmd(thrust=0.2),
               SafetyStatus(thrust_limited=True), st)
    frame = bb.frames()[-1]
    assert frame["desired"]["thrust"] == 1.0
    assert frame["applied"]["thrust"] == 0.2
    assert frame["desired"]["thrust"] != frame["applied"]["thrust"]
    assert frame["limited"]["thrust"] is True


def test_disabled_recorder_is_noop(tmp_path):
    bb = BlackBox(str(tmp_path), enabled=False, sample_period_s=0.0)
    bb.observe(_cmd(1.0), _cmd(0.0), SafetyStatus(drag_alarm=True), _FakeState())
    assert bb.frames() == []
    assert bb.dumps() == []


def test_path_for_rejects_traversal(tmp_path):
    bb, _ = _bb(tmp_path)
    assert bb.path_for("../../etc/passwd") is None
    assert bb.path_for("not-a-dump.txt") is None


# --------------------------------------------------------------------------- #
# Runtime wiring
# --------------------------------------------------------------------------- #
def _runtime(tmp_path, **obs):
    from vanchor.app import Runtime

    cfg = AppConfig()
    cfg.data_dir = str(tmp_path)
    for k, v in obs.items():
        setattr(cfg.obs, k, v)
    return Runtime(cfg)


def test_runtime_installs_hook_and_records(tmp_path):
    rt = _runtime(tmp_path)
    assert rt.blackbox.enabled
    # Prime a fix so the frame carries a real position.
    rt.state.fix = GpsFix(point=GeoPoint(59.0, 18.0))
    gov = rt.controller.safety
    # A short dt makes the 0 -> 1 thrust step exceed the slew limit, so the
    # governor clamps it (applied < desired) -- exactly the case to capture.
    applied, status = gov.govern(
        MotorCommand(thrust=1.0, steering=0.0), rt.state, 0.1, True,
        heading_age_s=0.0, depth_age_s=0.0,
    )
    assert applied.thrust < 1.0
    # ... and the black box captured both sides of that.
    frames = rt.blackbox.frames()
    assert len(frames) == 1
    assert frames[0]["desired"]["thrust"] == 1.0
    assert frames[0]["applied"]["thrust"] == round(applied.thrust, 4)


def test_runtime_hook_does_not_change_governed_command(tmp_path):
    """The recorder must be transparent: the governed result is identical with
    and without the hook installed."""
    with_hook = _runtime(tmp_path / "on")
    without = _runtime(tmp_path / "off", blackbox_enabled=False)
    assert not without.blackbox.enabled

    cmd = MotorCommand(thrust=0.6, steering=0.4)
    a1, _ = with_hook.controller.safety.govern(
        cmd, with_hook.state, 1.0, True, heading_age_s=0.0, depth_age_s=0.0)
    a2, _ = without.controller.safety.govern(
        cmd, without.state, 1.0, True, heading_age_s=0.0, depth_age_s=0.0)
    assert (a1.thrust, a1.steering) == (a2.thrust, a2.steering)


def test_runtime_alarm_dumps_file(tmp_path):
    # Zero tail -> the alarm tick dumps immediately (inline write, no loop).
    rt = _runtime(tmp_path, blackbox_post_trigger_s=0.0)
    rt.state.fix = GpsFix(point=GeoPoint(59.0, 18.0))
    gov = rt.controller.safety
    # A controller fault is surfaced to the hook via state.controller_fault.
    rt.state.controller_fault = "BoomError: kaboom"
    gov.govern(MotorCommand(thrust=0.1), rt.state, 1.0, True,
               heading_age_s=0.0, depth_age_s=0.0)
    dumps = rt.blackbox.dumps()
    assert len(dumps) == 1
    assert "controller_fault" in dumps[0]["file"]
