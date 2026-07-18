#!/usr/bin/env python3
"""Retake the README/docs screenshots against a live, moving simulation.

Boots an ISOLATED vanchor server (temp data dir, sim only, time-scaled so
tracks build quickly, the repo's depth chart symlinked in, boat started on a
charted lake), then drives each feature through the REAL command API and lets
the sim run before shooting — so every screenshot shows the mode genuinely
ACTIVE (badge, motion trail, overlays), not just its panel.

Usage:
    .venv/bin/python scripts/take_screenshots.py            # all shots
    .venv/bin/python scripts/take_screenshots.py trolling depth   # subset

Needs playwright + chromium (same install the e2e tests use) and network for
the map tiles.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OUT = REPO / "docs" / "images"
VANCHOR = str(Path(sys.executable).parent / "vanchor")

# Charted lake (the imported depth chart covers it) — boat starts on water.
LAKE = (59.8779, 12.0293)
TIME_SCALE = 5.0  # sim seconds per wall second


# --------------------------------------------------------------------------- #
# server plumbing
# --------------------------------------------------------------------------- #
def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_ready(base: str, timeout_s: float = 40) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            if urllib.request.urlopen(base + "/api/state", timeout=2).status == 200:
                return
        except Exception:
            time.sleep(0.4)
    raise RuntimeError("server did not become ready")


def boot_server(workdir: Path, time_scale: float = TIME_SCALE) -> tuple[subprocess.Popen, str]:
    port = _free_port()
    data = workdir / "vanchor_data"
    data.mkdir(parents=True, exist_ok=True)
    # Reuse the repo's imported depth chart (contours/soundings) read-only.
    for name in ("depthchart.npz", "depthmap.json"):
        src = REPO / "vanchor_data" / name
        if src.exists():
            (data / name).symlink_to(src)
    # Reuse the repo's offline water cache too, so shoreline/island routing
    # plans without an Overpass fetch. Per-file symlinks into a REAL directory:
    # any new cache entry the server writes lands in the temp dir, never the repo.
    wc_src = REPO / "vanchor_data" / "water_cache"
    if wc_src.is_dir():
        wc = data / "water_cache"
        wc.mkdir(exist_ok=True)
        for f in wc_src.iterdir():
            (wc / f.name).symlink_to(f)
    cfg = workdir / "config.yaml"
    cfg.write_text(
        "sim:\n"
        f"  start_lat: {LAKE[0]}\n"
        f"  start_lon: {LAKE[1]}\n"
        f"  time_scale: {time_scale}\n"
    )
    proc = subprocess.Popen(
        [VANCHOR, "--config", str(cfg), "--host", "127.0.0.1", "--port", str(port),
         "--log-level", "warning"],
        cwd=str(workdir), stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
    )
    base = f"http://127.0.0.1:{port}"
    _wait_ready(base)
    return proc, base


def post(base: str, path: str, body: dict) -> dict:
    req = urllib.request.Request(base + path, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=10))


def cmd(base: str, body: dict) -> None:
    post(base, "/api/command", body)


def state(base: str) -> dict:
    return json.load(urllib.request.urlopen(base + "/api/state", timeout=5))


# --------------------------------------------------------------------------- #
# page helpers
# --------------------------------------------------------------------------- #
def wait_app(page) -> None:
    page.wait_for_function("() => window.VA && VA.last && VA.last.mode !== undefined",
                           timeout=30_000)
    page.wait_for_timeout(1200)


def set_view(page, lat: float, lon: float, zoom: float, follow: bool = True) -> None:
    page.evaluate(
        "([lat, lon, z, follow]) => {"
        "  VA.mapCtx.follow.boat = follow;"
        "  VA.mapCtx.map.setView([lat, lon], z, {animate: false});"
        "}", [lat, lon, zoom, follow])


def set_zoom(page, zoom: float) -> None:
    page.evaluate("z => VA.mapCtx.map.setZoom(z, {animate: false})", zoom)


def set_layer(page, base_name: str | None = None, overlays: dict | None = None) -> None:
    """Switch base layer / toggle overlays by their layers-control labels."""
    page.evaluate(
        "([base, overlays]) => {"
        "  const ctl = document.querySelector('.leaflet-control-layers');"
        "  if (!ctl) return 'no-control';"
        "  const rows = ctl.querySelectorAll('label');"
        "  for (const row of rows) {"
        "    const txt = row.textContent.trim();"
        "    const input = row.querySelector('input');"
        "    if (!input) continue;"
        "    if (base && input.type === 'radio' && txt === base && !input.checked) input.click();"
        "    if (overlays && txt in overlays && input.type === 'checkbox' && "
        "        input.checked !== overlays[txt]) input.click();"
        "  }"
        "}", [base_name, overlays])


def boat_pos(base: str) -> tuple[float, float]:
    p = state(base).get("position") or {}
    return p.get("lat", LAKE[0]), p.get("lon", LAKE[1])


def shoot(page, name: str) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    page.wait_for_timeout(2500)  # let tiles/canvas settle
    page.screenshot(path=str(OUT / f"{name}.png"))
    print(f"  -> {name}.png")


def run_sim(page, seconds_wall: float) -> None:
    """Let the sim run with the page open so the client trail accumulates."""
    page.wait_for_timeout(int(seconds_wall * 1000))


def reset(page, base) -> None:
    """Stop the boat and reload the page: clears the client-side trail and any
    stale markers from the previous shot, so shots don't bleed into each other."""
    cmd(base, {"type": "stop"})
    cmd(base, {"type": "goto", "waypoints": []})   # clear any server-side route
    cmd(base, {"type": "stop"})
    page.reload(wait_until="domcontentloaded")
    wait_app(page)


# --------------------------------------------------------------------------- #
# the shots
# --------------------------------------------------------------------------- #
def shot_overview(page, base):
    reset(page, base)
    cmd(base, {"type": "heading_hold", "heading": 65})
    cmd(base, {"type": "cruise", "knots": 2.5})     # heading_hold steers; cruise drives
    run_sim(page, 14)
    la, lo = boat_pos(base)
    set_view(page, la, lo, 16)
    shoot(page, "overview")
    cmd(base, {"type": "stop"})


def shot_anchor(page, base):
    reset(page, base)
    cmd(base, {"type": "teleport", "lat": LAKE[0], "lon": LAKE[1]})
    page.wait_for_timeout(800)
    cmd(base, {"type": "anchor_hold", "radius_m": 8})
    run_sim(page, 10)
    la, lo = boat_pos(base)
    set_view(page, la, lo, 18)
    shoot(page, "mode-anchor")
    cmd(base, {"type": "stop"})


def shot_route(page, base):
    reset(page, base)
    cmd(base, {"type": "teleport", "lat": LAKE[0], "lon": LAKE[1]})
    page.wait_for_timeout(800)
    la, lo = LAKE
    wps = [
        {"name": "WP 1", "lat": la + 0.0022, "lon": lo + 0.0018},
        {"name": "WP 2", "lat": la + 0.0034, "lon": lo + 0.0075},
        {"name": "WP 3", "lat": la + 0.0012, "lon": lo + 0.0110},
    ]
    cmd(base, {"type": "goto", "waypoints": wps, "throttle": 0.7})
    run_sim(page, 22)
    bla, blo = boat_pos(base)
    # frame boat + remaining route (bias toward the route centroid)
    set_view(page, (bla + la + 0.0022) / 2, (blo + lo + 0.006) / 2, 15.6, follow=False)
    shoot(page, "mode-route")
    cmd(base, {"type": "stop"})


def shot_trolling(page, base):
    reset(page, base)
    cmd(base, {"type": "teleport", "lat": LAKE[0], "lon": LAKE[1]})
    page.wait_for_timeout(800)
    # Calm water for this shot so the commanded S-pattern reads cleanly.
    cmd(base, {"type": "set_environment", "current_speed": 0.0, "wind_speed": 0.0,
               "wind_variability": 0.0})
    cmd(base, {"type": "trolling", "base_heading": 70,
               "amplitude_deg": 12, "period_s": 60, "speed_knots": 2.2})
    run_sim(page, 10)                       # let the turn-in settle...
    page.reload(wait_until="domcontentloaded")  # ...then drop that messy bit of trail
    wait_app(page)
    page.evaluate(
        "() => { for (const [id, v] of [['troll-amp', 12], ['troll-period', 60],"
        " ['troll-speed', 2.2]]) { const el = document.getElementById(id);"
        " if (el) { el.value = v; el.dispatchEvent(new Event('input')); } } }")
    run_sim(page, 55)   # ~275 sim-seconds -> several clean S-wavelengths of trail
    la, lo = boat_pos(base)
    set_view(page, la, lo, 17.6)
    shoot(page, "mode-trolling")
    cmd(base, {"type": "stop"})


def shot_work_area(page, base):
    reset(page, base)
    cmd(base, {"type": "teleport", "lat": LAKE[0], "lon": LAKE[1]})
    page.wait_for_timeout(800)
    la, lo = LAKE
    spots = [
        {"name": "Spot 1", "lat": la + 0.0008, "lon": lo + 0.0012},
        {"name": "Spot 2", "lat": la + 0.0016, "lon": lo + 0.0030},
        {"name": "Spot 3", "lat": la + 0.0002, "lon": lo + 0.0042},
    ]
    cmd(base, {"type": "work_area", "waypoints": spots, "dwell_s": 600})
    run_sim(page, 25)   # reach spot 1 and hold there
    set_view(page, la + 0.0010, lo + 0.0027, 16.8, follow=False)
    shoot(page, "mode-work-area")
    cmd(base, {"type": "stop"})


def shot_depth(page, base):
    reset(page, base)
    cmd(base, {"type": "teleport", "lat": 59.8783, "lon": 12.0301})
    page.wait_for_timeout(800)
    set_layer(page, overlays={"Depth map": True, "Depth contours": True})
    set_view(page, 59.8783, 12.0301, 15.2, follow=False)
    page.wait_for_timeout(3500)  # contour/composition fetch + draw
    shoot(page, "depth")
    set_layer(page, overlays={"Depth map": False, "Depth contours": False})


def shot_daylight(page, base):
    cmd(base, {"type": "stop"})
    page.evaluate("localStorage.setItem('vanchor-theme', 'daylight')")
    page.reload()
    wait_app(page)
    set_layer(page, base_name="Light")
    cmd(base, {"type": "heading_hold", "heading": 40})
    cmd(base, {"type": "cruise", "knots": 2.0})
    run_sim(page, 10)
    la, lo = boat_pos(base)
    set_view(page, la, lo, 16)
    shoot(page, "daylight")
    cmd(base, {"type": "stop"})
    page.evaluate("localStorage.removeItem('vanchor-theme')")
    page.reload()
    wait_app(page)
    set_layer(page, base_name="Dark")


def shot_settings(page, base):
    cmd(base, {"type": "stop"})
    page.evaluate("() => { const b = document.getElementById('settings-open'); if (b) b.click(); }")
    page.wait_for_timeout(900)
    shoot(page, "settings")
    page.evaluate("() => { const b = document.getElementById('settings-close'); if (b) b.click(); }")
    page.wait_for_timeout(400)


def shot_views(page, base):
    cmd(base, {"type": "heading_hold", "heading": 120})
    cmd(base, {"type": "cruise", "knots": 2.4})
    run_sim(page, 6)
    for view, name in (("helm", "view-helm"), ("instruments", "view-instruments")):
        page.goto(page.url.split("/view/")[0].split("?")[0].rstrip("/").replace("/view", "")
                  if "/view/" in page.url else page.url, wait_until="domcontentloaded")
        page.goto(f"{BASE_URL}/view/{view}", wait_until="domcontentloaded")
        wait_app(page)
        page.wait_for_timeout(1500)
        shoot(page, name)
    # manual view with live thrust
    cmd(base, {"type": "manual", "thrust": 0.45, "steering": 0.15})
    page.goto(f"{BASE_URL}/view/manual", wait_until="domcontentloaded")
    wait_app(page)
    # push the on-screen sliders so the view reads as actively driving
    page.evaluate(
        "() => { for (const [id, v] of [['thrust', 0.45], ['steer', 0.15]]) {"
        "  const el = document.getElementById(id);"
        "  if (el) { el.value = v; el.dispatchEvent(new Event('input', {bubbles: true})); } } }")
    page.wait_for_timeout(1500)
    shoot(page, "view-manual")
    cmd(base, {"type": "stop"})
    page.goto(BASE_URL, wait_until="domcontentloaded")
    wait_app(page)


def shot_mobile(pw, browser, base):
    ctx = browser.new_context(viewport={"width": 390, "height": 844},
                              device_scale_factor=2, is_mobile=True, has_touch=True)
    # clear any server-side route/mode from earlier shots (no page needed)
    cmd(base, {"type": "stop"})
    cmd(base, {"type": "goto", "waypoints": []})
    cmd(base, {"type": "stop"})
    page = ctx.new_page()
    page.goto(BASE_URL, wait_until="domcontentloaded")
    wait_app(page)
    # the desktop context may hold the helm -> claim it so no banner shows
    page.evaluate("() => { const b = document.getElementById('role-banner-take');"
                  " if (b) b.click(); }")
    page.wait_for_timeout(5500)   # let the 'you have the helm' toast fade
    cmd(base, {"type": "anchor_hold", "radius_m": 8})
    page.wait_for_timeout(4000)
    la, lo = boat_pos(base)
    page.evaluate("([lat, lon]) => VA.mapCtx.map.setView([lat, lon], 17.5, {animate:false})",
                  [la, lo])
    shoot(page, "mobile")
    cmd(base, {"type": "stop"})
    ctx.close()


def shot_alarm_mobile(pw, browser, base):
    """Phone-width anchor-alarm regression (UX Task 1, item 1 / A2+A7).

    Arms the passive anchor alarm, teleports the boat outside the watch
    circle, and asserts the alarm strip renders as a FULL-WIDTH row at
    390 px — x == 0 and width == viewport (the old #banner pill rendered at
    x ≈ -167 and clipped to "G ALARM")."""
    ctx = browser.new_context(viewport={"width": 390, "height": 844},
                              device_scale_factor=2, is_mobile=True, has_touch=True)
    cmd(base, {"type": "stop"})
    page = ctx.new_page()
    page.goto(BASE_URL, wait_until="domcontentloaded")
    wait_app(page)
    la, lo = boat_pos(base)
    cmd(base, {"type": "anchor_alarm_set", "lat": la, "lon": lo, "radius_m": 10})
    cmd(base, {"type": "teleport", "lat": la + 0.0006, "lon": lo})  # ~65 m out
    page.wait_for_function(
        "() => { const el = document.getElementById('anchor-alarm-banner');"
        " return el && !el.classList.contains('hidden'); }", timeout=20_000)
    la2, lo2 = boat_pos(base)
    page.evaluate("([lat, lon]) => VA.mapCtx.map.setView([lat, lon], 17, {animate:false})",
                  [la2, lo2])
    shoot(page, "mobile-anchor-alarm")
    box = page.evaluate(
        "() => { const r = document.getElementById('anchor-alarm-banner')"
        ".getBoundingClientRect(); return {x: r.x, w: r.width}; }")
    vw = page.evaluate("() => window.innerWidth")
    assert box["x"] >= 0, f"alarm strip clipped left: x={box['x']}"
    assert abs(box["w"] - vw) < 1, f"alarm strip not full-width: {box['w']} vs {vw}"
    print(f"  alarm strip bbox OK: x={box['x']}, w={box['w']} (viewport {vw})")
    cmd(base, {"type": "anchor_alarm_clear"})
    cmd(base, {"type": "stop"})
    ctx.close()


SHOTS = {
    "overview": shot_overview,
    "anchor": shot_anchor,
    "route": shot_route,
    "trolling": shot_trolling,
    "work-area": shot_work_area,
    "depth": shot_depth,
    "daylight": shot_daylight,
    "settings": shot_settings,
    "views": shot_views,
}

BASE_URL = ""


def main() -> None:
    global BASE_URL
    only = set(sys.argv[1:])
    from playwright.sync_api import sync_playwright

    with tempfile.TemporaryDirectory(prefix="vanchor-shots-") as wd:
        proc, base = boot_server(Path(wd))
        BASE_URL = base
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(args=["--no-sandbox"])
                ctx = browser.new_context(viewport={"width": 1280, "height": 800})
                page = ctx.new_page()
                page.goto(base, wait_until="domcontentloaded")
                wait_app(page)
                for name, fn in SHOTS.items():
                    if only and name not in only:
                        continue
                    print(f"[shot] {name}")
                    fn(page, base)
                ctx.close()   # a second connected client shows the presence banner
                if not only or "mobile" in only:
                    print("[shot] mobile")
                    shot_mobile(pw, browser, base)
                if not only or "alarm" in only:
                    print("[shot] alarm")
                    shot_alarm_mobile(pw, browser, base)
                browser.close()
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                proc.kill()


if __name__ == "__main__":
    main()
