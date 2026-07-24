#!/usr/bin/env python3
"""Repeatable end-to-end smoke test for Vanchor-NG.

Spins up a FRESH, fully ISOLATED sim server (its own temp ``vanchor_data`` so no
leftover state — devices.json, profiles — can skew the result), exercises the
backend API + live control loop + the web UI (headless, if Playwright is
installed), then tears everything down. Designed to give the SAME result every
run.

    python e2e_smoke.py            # run the smoke; exit 0 = all checks passed
    SMOKE_PORT=8123 python e2e_smoke.py

It does not touch the repo's real ``vanchor_data/`` and cleans up its server +
temp dir on exit (even on failure).
"""
from __future__ import annotations

import io
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path

PORT = int(os.environ.get("SMOKE_PORT", "8099"))
BASE = f"http://127.0.0.1:{PORT}"
RESULTS: list[tuple[str, bool]] = []


def check(name: str, ok: object) -> None:
    RESULTS.append((name, bool(ok)))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}")


def _req(path: str, data=None, timeout=10):
    url = BASE + path
    if data is not None:
        data = json.dumps(data).encode()
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    else:
        req = urllib.request.Request(url)
    return urllib.request.urlopen(req, timeout=timeout)


def get(path: str):
    return json.load(_req("/api/" + path))


def post(path: str, data):
    return json.load(_req("/api/" + path, data=data))


def cmd(d):
    _req("/api/command", data=d).read()


def wait_ready(timeout_s=30) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            if _req("/api/state", timeout=2).status == 200:
                return True
        except Exception:
            time.sleep(0.5)
    return False


def backend_checks():
    st = get("state")
    check("telemetry: sim + has position", st.get("sim_enabled") and st.get("position"))
    check("boat presets seeded (>=5)", len(get("boat/profiles")["profiles"]) >= 5)

    dc = get("config/devices")
    check("device config GET (sim default)", dc["hardware"]["enabled"] is False and "options" in dc)
    sr = post("config/devices", {"hardware": {"gps_source": "nmea"}})
    reflected = get("config/devices")["hardware"]["gps_source"] == "nmea"
    post("config/devices", {"hardware": {"gps_source": None}})  # revert to full sim
    check("device config persists + reflects (restart to apply)",
          sr.get("ok") and sr.get("restart_required") is True and reflected)

    # Live control: a goto leg should converge.
    p = get("state")["position"]
    lat0, lon0 = p["lat"], p["lon"]
    tlat = lat0 + 150 / 111320
    tlon = lon0 + 120 / (111320 * math.cos(math.radians(lat0)))
    cmd({"type": "goto", "waypoints": [{"lat": tlat, "lon": tlon}]})
    time.sleep(2)
    d_start = get("state").get("distance_to_waypoint_m") or 1e9
    time.sleep(16)
    d_end = get("state").get("distance_to_waypoint_m") or 1e9
    check(f"live goto converges ({d_start:.0f} -> {d_end:.0f} m)", d_end < d_start - 5)

    cmd({"type": "anchor_hold"})
    time.sleep(1)
    check("anchor_hold engages", get("state").get("mode") == "anchor_hold")
    cmd({"type": "stop"})

    dg = get("depth/grid?cell_m=15")
    check("depth grid responds", isinstance(dg.get("cells"), list))

    # Backup -> restore round-trip (versioned zip + client state).
    zb = _req("/api/backup", data={"client": {"vanchor-theme": "dark"}}).read()
    z = zipfile.ZipFile(io.BytesIO(zb))
    check("backup zip has manifest + data", "manifest.json" in z.namelist() and "boats.json" in z.namelist())
    try:
        import requests
        rs = requests.post(BASE + "/api/restore", files={"file": ("b.zip", zb, "application/zip")}, timeout=15).json()
        check("restore round-trip + client", rs.get("ok") and rs.get("client", {}).get("vanchor-theme") == "dark")
    except ImportError:
        print("  [SKIP] restore (requests not installed)")


def frontend_checks():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("  [SKIP] frontend checks (playwright not installed)")
        return
    with sync_playwright() as pw:
        br = pw.chromium.launch(args=["--no-sandbox"])
        pg = br.new_page(viewport={"width": 1200, "height": 860})
        errs: list[str] = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)
        # Deterministic readiness instead of `networkidle`: the app holds a live
        # telemetry WebSocket and streams map tiles, so the 500 ms network-quiet
        # window `networkidle` needs may never occur within the timeout (observed
        # flaky in CI). Wait for the map's layers control to be built — that is
        # what every check below actually depends on.
        pg.goto(BASE + "/", wait_until="domcontentloaded", timeout=20000)
        # `attached`, not visible: the layers control is collapsed by default so
        # its overlay labels are in the DOM but hidden until expanded (the checks
        # read them via textContent, which does not need visibility).
        pg.wait_for_selector(
            ".leaflet-control-layers-overlays label", state="attached", timeout=20000
        )
        pg.wait_for_timeout(1000)
        check("UI loads, map present", pg.locator("#map").count() == 1)
        ovs = pg.evaluate(
            "()=>[...new Set([...document.querySelectorAll('.leaflet-control-layers-overlays label span')].map(s=>s.textContent.trim()))]"
        )
        check(f"overlays in layers panel ({len(ovs)})", len(ovs) >= 5)
        pg.evaluate("()=>document.getElementById('settings-open').click()")
        pg.wait_for_timeout(300)
        for cid in ("boat-card", "devices-card", "backup-card", "hud-card"):
            check(f"settings card #{cid}", pg.locator("#" + cid).count() == 1)
        check("measure tool control", pg.locator(".measure-btn").count() == 1)
        check("alert-history bell", pg.locator("#alerts-open").count() == 1)

        # --- Paint route (free-hand) ---------------------------------------
        # First the pure path->waypoints algorithm (deterministic): a long
        # straight into a sharp turn should collapse the straight yet keep detail
        # at the corner. The actual drawing is exercised further down with real
        # PointerEvents.
        n_wp = pg.evaluate(
            """() => {
                const base = {lat: 59.66275, lon: 13.32247};
                const path = [];
                for (let i = 0; i < 120; i++) path.push({lat: base.lat, lon: base.lon + 0.0009 * (i/119)});
                for (let i = 1; i < 120; i++) path.push({lat: base.lat + 0.0009 * (i/119), lon: base.lon + 0.0009});
                return VA.paint.pathToWaypoints(path, 1.2).length;
            }"""
        )
        check(f"paint algorithm yields waypoints ({n_wp})", 3 <= n_wp <= 80)
        # UI wiring: Paint button reveals the always-visible toolbar; the Draw/Pan
        # toggle flips. Wait deterministically for the wiring + the toolbar rather
        # than fixed sleeps — a slow CI runner would otherwise read the bar before
        # the click's synchronous reveal has been observed (this check is a required
        # gate, so it must not flake).
        pg.wait_for_function(
            "() => window.VA && VA.paint && document.getElementById('wp-paint') && document.getElementById('paint-bar')",
            timeout=10000,
        )

        def _enter_paint():
            pg.evaluate("()=>document.getElementById('wp-paint').click()")
            try:
                pg.wait_for_function(
                    "()=>{const b=document.getElementById('paint-bar');return b && !b.classList.contains('hidden');}",
                    timeout=4000,
                )
                return True
            except Exception:
                return False

        shown = _enter_paint()
        if not shown:  # one retry after a layout nudge (first-paint / mobile-settle race)
            pg.evaluate("()=>window.dispatchEvent(new Event('resize'))")
            pg.wait_for_timeout(200)
            shown = _enter_paint()
            if not shown:
                print("    paint diag:", pg.evaluate(
                    "()=>({paint:!!(window.VA&&VA.paint), btn:!!document.getElementById('wp-paint'),"
                    " bar:(document.getElementById('paint-bar')||{}).className, mobile:document.body.classList.contains('mobile')})"))
        check("paint toolbar shows", shown)
        check("finished + cancel visible", pg.locator("#paint-finish").is_visible() and pg.locator("#paint-cancel").is_visible())
        pg.evaluate("()=>document.getElementById('paint-toggle').click()")
        pg.wait_for_function("()=>document.getElementById('paint-toggle').classList.contains('panning')", timeout=2000)
        check("draw/pan toggle flips", pg.locator("#paint-toggle").evaluate("el=>el.classList.contains('panning')"))
        pg.evaluate("()=>document.getElementById('paint-toggle').click()")  # back to Draw

        # ACTUAL drawing: dispatch real PointerEvents for two strokes with a pan
        # toggle between them (resume). This drives onPaintDown/Move/Up end-to-end
        # — the earlier checks would miss a lat/lon-shaped bug where nothing draws.
        drew = pg.evaluate(
            """() => {
                const c = VA.map.leaflet.getContainer(), r = c.getBoundingClientRect();
                const fire = (t, x, y, b) => (t === 'pointerdown' ? c : window).dispatchEvent(
                    new PointerEvent(t, {bubbles:true, cancelable:true, pointerId:1,
                        pointerType:'mouse', button:0, buttons:b, clientX:r.left+x, clientY:r.top+y}));
                const stroke = (pts) => { fire('pointerdown', pts[0][0], pts[0][1], 1);
                    for (let i=1;i<pts.length;i++) fire('pointermove', pts[i][0], pts[i][1], 1);
                    fire('pointerup', pts[pts.length-1][0], pts[pts.length-1][1], 0); };
                const line = () => { let n=0; VA.map.leaflet.eachLayer(l=>{ if (l instanceof L.Polyline
                    && l.options.color==='#1be4ff' && l.options.weight===4) n=l.getLatLngs().length; }); return n; };
                const s1=[]; for (let i=0;i<24;i++) s1.push([120+i*18, 300+Math.sin(i/3)*90]); stroke(s1);
                const a1 = line();
                document.getElementById('paint-toggle').click();   // pause -> pan
                document.getElementById('paint-toggle').click();   // -> draw (resume)
                const s2=[]; for (let i=0;i<16;i++) s2.push([520+i*12, 500-i*10]); stroke(s2);
                return {a1, a2: line()};
            }"""
        )
        check(f"paint stroke draws a line ({drew['a1']} pts)", drew["a1"] >= 3)
        check(f"paint resumes after pause ({drew['a1']} -> {drew['a2']} pts)", drew["a2"] > drew["a1"])
        # Undo removes the last stroke (back to the pre-resume length).
        undone = pg.evaluate(
            """() => { const b=document.getElementById('paint-undo'); const was=b.disabled; b.click();
                let n=0; VA.map.leaflet.eachLayer(l=>{ if (l instanceof L.Polyline
                    && l.options.color==='#1be4ff' && l.options.weight===4) n=l.getLatLngs().length; });
                return {was, n}; }"""
        )
        check(f"undo removes last stroke ({drew['a2']} -> {undone['n']} pts)", (not undone["was"]) and undone["n"] == drew["a1"])
        pg.evaluate("()=>document.getElementById('paint-finish').click()")
        pg.wait_for_timeout(150)
        n_pending = pg.evaluate("()=>VA.map.pending().length")
        check(f"Finished builds waypoints ({n_pending})", 3 <= n_pending <= 80)
        check("trace-tight auto-enabled for paint", pg.evaluate("()=>document.getElementById('wp-tight').checked"))
        check("paint toolbar hides after finish", pg.locator("#paint-bar").evaluate("el=>el.classList.contains('hidden')"))
        pg.evaluate("()=>document.getElementById('wp-clear').click()")  # tidy up the pending route

        check("no console errors", not errs)
        if errs:
            print("    console errors:", errs[:5])
        br.close()


def main() -> int:
    subprocess.run(["fuser", "-k", f"{PORT}/tcp"], stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
    time.sleep(1)
    workdir = tempfile.mkdtemp(prefix="vanchor-e2e-")
    print(f"e2e smoke: isolated workdir={workdir} port={PORT}")
    proc = subprocess.Popen(
        # No --nmea-tcp: the smoke is self-contained and must not contend for the
        # fixed NMEA-TCP port with any other running instance.
        ["vanchor", "--host", "127.0.0.1", "--port", str(PORT), "--log-level", "warning"],
        cwd=workdir,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.STDOUT,
    )
    try:
        if not wait_ready():
            print("  [FAIL] server did not become ready")
            return 1
        print("-- backend + control --")
        backend_checks()
        print("-- frontend --")
        frontend_checks()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        shutil.rmtree(workdir, ignore_errors=True)

    npass = sum(1 for _, ok in RESULTS if ok)
    print(f"\n=== e2e smoke: {npass}/{len(RESULTS)} checks passed ===")
    return 0 if npass == len(RESULTS) and RESULTS else 1


if __name__ == "__main__":
    sys.exit(main())
