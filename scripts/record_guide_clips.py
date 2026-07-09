#!/usr/bin/env python3
"""Choreographies for the getting-started guide clips (see record_guide.py).

Each function is registered with @clip("name") and runs against a live,
connected page (1280x800, video recording already rolling). The philosophy:
click the REAL UI the way a person would — a visible synthetic cursor glides
to each control, slider thumbs are dragged with the mouse, and the sim's
closed loop does the rest. Scene state (pose + weather) is set through the
command API with all randomness zeroed so every run tells the same story.

Timing note: the shared sim server runs at take_screenshots.TIME_SCALE (5x),
so a few wall-seconds of footage show tens of sim-seconds of boat motion.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from take_screenshots import LAKE, cmd, state, wait_app  # noqa: E402

# The rig (record_guide.py) may be running as __main__ (python record_guide.py)
# or as an imported module; grab whichever instance actually registered CLIPS
# so our @clip decorations land in the dict the rig iterates.
_rig = sys.modules.get("record_guide") or sys.modules["__main__"]
clip = _rig.clip

CALM = {
    "type": "set_environment",
    "current_speed": 0.0, "current_dir": 0.0,
    "wind_speed": 0.0, "wind_dir": 0.0,
    "gust_amplitude_mps": 0.0, "wind_variability": 0.0,
    "current_variability": 0.0,
}
# Steady deterministic push for the anchor clip: a firm current + wind with
# zero gusts/variability -> identical drift every run.
PUSH = {
    "type": "set_environment",
    "current_speed": 0.5, "current_dir": 90.0,
    "wind_speed": 4.0, "wind_dir": 120.0,
    "gust_amplitude_mps": 0.0, "wind_variability": 0.0,
    "current_variability": 0.0,
}


# --------------------------------------------------------------------------- #
# camera-friendly interaction helpers
# --------------------------------------------------------------------------- #
def add_cursor(page) -> None:
    """Inject a visible cursor dot that tracks the Playwright mouse, so the
    viewer sees the pointer glide to and press each control."""
    page.evaluate(
        "() => {"
        "  if (document.getElementById('__va_cursor')) return;"
        "  const d = document.createElement('div');"
        "  d.id = '__va_cursor';"
        "  d.style.cssText = 'position:fixed;z-index:2147483647;width:20px;"
        "height:20px;border-radius:50%;border:2px solid #fff;"
        "background:rgba(27,228,255,.35);box-shadow:0 0 12px rgba(27,228,255,.9);"
        "pointer-events:none;left:-60px;top:-60px;"
        "transform:translate(-50%,-50%)';"
        "  document.body.appendChild(d);"
        "  addEventListener('mousemove', e => {"
        "    d.style.left = e.clientX + 'px'; d.style.top = e.clientY + 'px';"
        "  }, true);"
        "  addEventListener('mousedown', () => {"
        "    d.style.background = 'rgba(255,140,90,.65)';"
        "    d.style.width = '26px'; d.style.height = '26px';"
        "  }, true);"
        "  addEventListener('mouseup', () => {"
        "    d.style.background = 'rgba(27,228,255,.35)';"
        "    d.style.width = '20px'; d.style.height = '20px';"
        "  }, true);"
        "}")


def glide_click(page, selector: str, settle_ms: int = 350) -> None:
    """Move the visible cursor to an element (real mouse move) and click it."""
    el = page.locator(selector).first
    el.scroll_into_view_if_needed()
    page.wait_for_timeout(120)
    box = el.bounding_box()
    if box is None:
        raise RuntimeError(f"no bounding box for {selector}")
    x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    page.mouse.move(x, y, steps=22)
    page.wait_for_timeout(140)
    page.mouse.down()
    page.wait_for_timeout(90)
    page.mouse.up()
    page.wait_for_timeout(settle_ms)


def drag_slider(page, selector: str, target: float, steps: int = 20) -> None:
    """Drag a range input's thumb to `target` with the mouse so the motion is
    visible, then snap the exact value (one deterministic input event)."""
    el = page.locator(selector).first
    el.scroll_into_view_if_needed()
    page.wait_for_timeout(80)
    box = el.bounding_box()
    mn = float(el.evaluate("e => e.min"))
    mx = float(el.evaluate("e => e.max"))
    cur = float(el.evaluate("e => e.value"))
    pad = 9  # half a typical thumb width
    span = box["width"] - 2 * pad

    def xat(v: float) -> float:
        return box["x"] + pad + (v - mn) / (mx - mn) * span

    y = box["y"] + box["height"] / 2
    page.mouse.move(xat(cur), y, steps=14)
    page.wait_for_timeout(80)
    page.mouse.down()
    page.mouse.move(xat(target), y, steps=steps)
    page.mouse.up()
    # The mouse lands within a pixel or two of `target`; snap the exact value so
    # the command stream is identical run to run.
    el.evaluate(
        "(e, v) => { e.value = v; e.dispatchEvent(new Event('input', {bubbles: true})); }",
        target)
    page.wait_for_timeout(120)


def set_view(page, lat: float, lon: float, zoom: float, follow: bool = True) -> None:
    page.evaluate(
        "([lat, lon, z, follow]) => {"
        "  VA.mapCtx.follow.boat = follow;"
        "  VA.mapCtx.map.setView([lat, lon], z, {animate: false});"
        "}", [lat, lon, zoom, follow])


def take_helm(page) -> None:
    """If a previous clip's client still counts as helm, claim it silently."""
    page.evaluate("() => { const b = document.getElementById('role-banner-take');"
                  " if (b && b.offsetParent) b.click(); }")


def fresh_scene(page, base: str, env: dict, lat: float, lon: float,
                heading: float = 25.0) -> None:
    """Deterministic scene reset: stop everything, set the weather, snap the
    boat pose, then reload so the client-side trail/markers start clean."""
    cmd(base, {"type": "stop"})
    cmd(base, {"type": "goto", "waypoints": []})
    cmd(base, {"type": "stop"})
    cmd(base, env)
    cmd(base, {"type": "teleport", "lat": lat, "lon": lon, "heading": heading})
    page.reload(wait_until="domcontentloaded")
    wait_app(page)
    take_helm(page)
    add_cursor(page)


def wait_state(base: str, pred, timeout_s: float, poll_s: float = 0.25) -> bool:
    """Poll /api/state until pred(state) is true (drives clip pacing off the
    sim's actual behaviour instead of fixed sleeps)."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if pred(state(base)):
            return True
        time.sleep(poll_s)
    return False


# --------------------------------------------------------------------------- #
# 1. first-launch (~15 s): the app connects, chips go live, boat on the chart.
# --------------------------------------------------------------------------- #
@clip("first-launch")
def clip_first_launch(page, base):
    # A perfectly still opening: no wind, no current, boat at the lake spot.
    fresh_scene(page, base, CALM, LAKE[0], LAKE[1], heading=25)
    # The reload above IS the story: the viewer watches the chips populate.
    page.wait_for_timeout(2600)
    # Center the chart on the boat, then click the Chart view chip like a user.
    la, lo = LAKE
    set_view(page, la, lo, 16.5)
    page.wait_for_timeout(900)
    glide_click(page, ".view-chip[data-view='chart']")
    page.wait_for_timeout(3200)


# --------------------------------------------------------------------------- #
# 2. manual-driving (~12 s): thrust + steering sliders, boat arcs off, coasts.
# --------------------------------------------------------------------------- #
@clip("manual-driving")
def clip_manual_driving(page, base):
    fresh_scene(page, base, CALM, LAKE[0], LAKE[1], heading=25)
    set_view(page, LAKE[0], LAKE[1], 16.8)
    # Take direct control: the Manual tile, then the two sliders.
    glide_click(page, ".mode-btn[data-mode='manual']", settle_ms=200)
    drag_slider(page, "#thrust", 0.6)
    drag_slider(page, "#steer", 0.25)
    # Let the boat accelerate and carve a visible arc (sim runs 5x).
    page.wait_for_timeout(2900)
    # Ease the thrust back to zero and coast.
    drag_slider(page, "#thrust", 0.0)
    page.wait_for_timeout(1500)
    cmd(base, {"type": "stop"})


# --------------------------------------------------------------------------- #
# 3. follow-route (~20 s): tap waypoints on the chart, press Start, track legs.
# --------------------------------------------------------------------------- #
@clip("follow-route")
def clip_follow_route(page, base):
    fresh_scene(page, base, CALM, LAKE[0], LAKE[1], heading=60)
    set_view(page, LAKE[0], LAKE[1], 18.3, follow=False)
    # Open the Route panel and arm waypoint-adding (the real two-step flow).
    glide_click(page, ".mode-btn[data-mode='waypoint']", settle_ms=150)
    glide_click(page, "#wp-arm", settle_ms=150)
    # Tap three fixed points on the chart. The boat renders at screen center
    # (640, 400); the dock covers the right ~310 px, so keep pins left of it.
    # Short legs (~45 m total at zoom 18.3) so the run fits the clip budget.
    for x, y in ((685, 350), (660, 300), (615, 280)):
        page.mouse.move(x, y, steps=12)
        page.wait_for_timeout(100)
        page.mouse.click(x, y)
        page.wait_for_timeout(260)
    glide_click(page, "#wp-arm", settle_ms=150)   # done adding
    glide_click(page, "#wp-go", settle_ms=200)    # ▶ Start route
    # Ride along: wait for the leg indicator to advance, then for arrival.
    wait_state(base, lambda s: s.get("active_waypoint", 0) >= 1, 10)
    wait_state(base, lambda s: s.get("active_waypoint", 0) >= 2, 8)
    wait_state(base, lambda s: s.get("mode") != "waypoint"
               or s.get("distance_to_waypoint_m", 99) < 6.0, 8)
    page.wait_for_timeout(900)
    cmd(base, {"type": "stop"})


# --------------------------------------------------------------------------- #
# 4. big-stop (~10 s): boat underway, one STOP tap, everything goes quiet.
# --------------------------------------------------------------------------- #
@clip("big-stop")
def clip_big_stop(page, base):
    fresh_scene(page, base, CALM, LAKE[0], LAKE[1], heading=25)
    set_view(page, LAKE[0], LAKE[1], 16.8)
    # Get underway with a real slider push (non-zero thrust, visible motion).
    # (The Manual panel is already showing — manual is the default mode.)
    drag_slider(page, "#thrust", 0.7, steps=14)
    page.wait_for_timeout(2000)
    # The golden rule: one tap on STOP (the mode rail's red tile on desktop).
    glide_click(page, ".mode-btn.mode-stop", settle_ms=200)
    # Thrust is cut instantly; the boat coasts down.
    page.wait_for_timeout(2600)
    cmd(base, {"type": "stop"})


# --------------------------------------------------------------------------- #
# 5. drop-anchor (~25 s, HERO): ring drops, wind pushes, the boat breathes back.
# Recorded LAST: there is no clear-anchor command, so the dropped anchor's
# pin/ring would otherwise linger on the shared server into later clips.
# --------------------------------------------------------------------------- #
@clip("drop-anchor")
def clip_drop_anchor(page, base):
    # Steady deterministic push so the drift-out/pull-back loop repeats cleanly.
    fresh_scene(page, base, PUSH, LAKE[0], LAKE[1], heading=25)
    set_view(page, LAKE[0], LAKE[1], 17.8)
    glide_click(page, ".mode-btn[data-mode='anchor_hold']", settle_ms=250)
    drag_slider(page, "#ar", 8)
    glide_click(page, "#anchor-go", settle_ms=250)
    # Hands off — the station-keeping loop is the show. Pace the clip off the
    # real distance-to-anchor: out past ~5 m, back inside ~3 m, twice.
    t0 = time.time()
    for _ in range(2):
        wait_state(base, lambda s: s.get("distance_to_anchor_m", 0) > 5.0, 8)
        page.wait_for_timeout(300)
        wait_state(base, lambda s: s.get("distance_to_anchor_m", 99) < 3.0, 8)
        page.wait_for_timeout(300)
    # Top up so the hero clip breathes at least ~16 s after the drop.
    remainder = 16.0 - (time.time() - t0)
    if remainder > 0:
        page.wait_for_timeout(int(remainder * 1000))
    cmd(base, {"type": "stop"})
