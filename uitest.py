"""Effect-verifying UI test: each step is isolated; report what works / breaks.

Self-contained: launches a fresh sim server on an ephemeral port (no leftover
state) and tears it down on exit, the same way e2e_smoke.py does.

    python uitest.py            # default port 8097
    UITEST_PORT=8090 python uitest.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

PORT = int(os.environ.get("UITEST_PORT", "8097"))
BASE = f"http://127.0.0.1:{PORT}"

# Resolve the `vanchor` CLI from the same venv as the running interpreter so
# the subprocess finds the right entry-point even when the shell venv is not
# activated (e.g. run as `python uitest.py` inside the project venv).
_VANCHOR = str(Path(sys.executable).parent / "vanchor")


def _req(path, data=None, timeout=10):
    url = BASE + path
    if data is not None:
        data = json.dumps(data).encode()
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    else:
        req = urllib.request.Request(url)
    return urllib.request.urlopen(req, timeout=timeout)


def state():
    return json.load(_req("/api/state"))


def post(path, body):
    _req(path, body).read()


def wait_ready(timeout_s=30):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            if _req("/api/state", timeout=2).status == 200:
                return True
        except Exception:
            time.sleep(0.5)
    return False


results = []


def main() -> int:
    # Kill anything already on the port, then start a fresh isolated server.
    subprocess.run(["fuser", "-k", f"{PORT}/tcp"], stderr=subprocess.DEVNULL,
                   stdout=subprocess.DEVNULL)
    time.sleep(0.5)
    workdir = tempfile.mkdtemp(prefix="vanchor-uitest-")
    print(f"uitest: isolated workdir={workdir} port={PORT}")
    proc = subprocess.Popen(
        [_VANCHOR, "--host", "127.0.0.1", "--port", str(PORT), "--log-level", "warning"],
        cwd=workdir,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.STDOUT,
    )
    try:
        if not wait_ready():
            print("  [FAIL] server did not become ready")
            proc.terminate()
            shutil.rmtree(workdir, ignore_errors=True)
            return 1
        _run_ui_tests()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        shutil.rmtree(workdir, ignore_errors=True)

    print(f"\n{'ACTION':<34}{'RESULT':<7}DETAIL")
    print("-" * 92)
    for lbl, r, d in results:
        print(f"{lbl:<34}{r:<7}{d}")
    nfail = sum(1 for _, r, _ in results if r == "FAIL")
    print(f"\n=== uitest: {len(results) - nfail}/{len(results)} steps passed ===")
    return 0 if nfail == 0 else 1


def _run_ui_tests():
    with sync_playwright() as p:
        b = p.chromium.launch(args=["--no-sandbox"])
        pg = b.new_page(viewport={"width": 1280, "height": 900})
        pg.set_default_timeout(5000)
        seen = []
        pg.on("pageerror", lambda e: seen.append("PAGEERROR: " + str(e)))
        pg.on("console", lambda m: seen.append("console.error: " + m.text)
              if m.type == "error" else None)
        pg.goto(BASE + "/", wait_until="networkidle", timeout=20000)
        pg.wait_for_timeout(1200)

        def step(label, fn, check=None):
            """Run one UI step: fn() action + optional check(); record result."""
            seen.clear()
            try:
                if fn is not None:
                    fn()
                    pg.wait_for_timeout(500)
                detail = check() if check else ""
                results.append((label, "ERR" if seen else "ok", str(detail)))
            except Exception as e:
                results.append((label, "FAIL", str(e).splitlines()[0][:70]))
            for e in seen[:2]:
                results.append(("  !" + label, "", e))
            seen.clear()

        def mode(m):
            pg.click(f'.mode-btn[data-mode="{m}"]')
            pg.wait_for_timeout(300)

        def jsclick(s):
            pg.eval_on_selector(s, "el=>el.click()")

        def setopen(s):
            pg.eval_on_selector(s, "el=>el.open=true")

        # Ensure a clean manual baseline before exercising modes.
        post("/api/command", {"type": "manual", "thrust": 0, "steering": 0})
        pg.wait_for_timeout(300)

        # ----- mode panels + motor commands ----------------------------------
        step("heading_hold + hdg-go",
             lambda: (mode("heading_hold"), pg.click("#hdg-go")),
             lambda: f'mode={state()["mode"]}')
        step("drift + drift-go",
             lambda: (mode("drift"), pg.click("#drift-go")),
             lambda: f'mode={state()["mode"]}')
        step("anchor panel open",
             lambda: mode("anchor_hold"),
             lambda: f'panel_visible={pg.locator("#ctx-anchor_hold").is_visible()}')
        step("anchor + anchor-go",
             lambda: pg.click("#anchor-go"),
             lambda: f'mode={state()["mode"]}')
        step("anchor jog-fwd",
             lambda: pg.click("#jog-fwd"),
             lambda: f'mode={state()["mode"]}')
        step("anchor hold-hdg switch", lambda: jsclick("#hold-hdg"))
        step("anchor radius=8",
             lambda: pg.eval_on_selector(
                 "#ar",
                 "el=>{el.value=8;el.dispatchEvent(new Event('input'));"
                 "el.dispatchEvent(new Event('change'))}"),
             lambda: f'r={state()["anchor_radius_m"]}')
        step("cruise toggle",
             lambda: (setopen("#cruise-card"), jsclick("#cruise-on")),
             lambda: f'enabled={state()["cruise"]["enabled"]}')
        step("track record",
             lambda: (setopen("#track-card"), pg.click("#track-rec")),
             lambda: f'rec={state()["track"]["recording"]}')
        step("track stop",
             lambda: pg.click("#track-rec"),
             lambda: f'rec={state()["track"]["recording"]}')

        # Route / waypoint mode: clicking the rail shows the editor; the boat
        # doesn't start a route until the user adds waypoints and hits Start.
        step("route mode -> editor visible",
             lambda: mode("waypoint"),
             lambda: "wp-arm visible=" + str(pg.is_visible("#wp-arm")))
        step("route arm add-waypoints",
             lambda: pg.click("#wp-arm"),
             lambda: "armed=" + str(pg.eval_on_selector(
                 "#wp-arm", "e=>e.classList.contains('active')")))

        # ----- settings drawer (open first so #setup-open is reachable) -----
        step("settings open",
             lambda: pg.click("#settings-open"),
             lambda: "vis=" + str(pg.is_visible("#settings-close")))
        step("settings theme switch",
             lambda: jsclick("#theme-toggle-box"),
             lambda: "light=" + str(pg.eval_on_selector(
                 "body", "e=>e.classList.contains('light')")))
        step("depth overlay toggle",
             lambda: jsclick("#depth-show") if pg.locator("#depth-show").count() else None)
        step("sim card visible",
             None,
             lambda: "vis=" + str(
                 pg.eval_on_selector("#sim-card",
                                     "e=>!e.classList.contains('hidden')")
                 if pg.locator("#sim-card").count() else "absent"))

        # Wizard: #setup-open lives inside the settings drawer — exercise it
        # while the drawer is still open, then close settings afterwards.
        step("wizard open",
             lambda: pg.click("#setup-open"),
             lambda: "vis=" + str(pg.is_visible("#wizard")))
        step("wizard consent+next",
             lambda: (jsclick("#wizard input[type=checkbox]"),
                      pg.wait_for_timeout(400),   # let wizard.js enable the Next button
                      pg.locator("#wiz-next:not([disabled])").click(timeout=3000)),
             lambda: "step2=" + str(pg.is_visible("#wizard")))
        step("wizard close",
             lambda: jsclick("#wiz-close") if pg.locator("#wiz-close").count()
             else pg.keyboard.press("Escape"))
        # The wizard may close the settings drawer when it opens; guard the
        # click so the step does not fail if settings is already hidden.
        step("settings close",
             lambda: pg.click("#settings-close")
             if pg.locator("#settings-close").is_visible() else None)

        # ----- remote helm ---------------------------------------------------
        step("remote open",
             lambda: pg.click("#remote-toggle"),
             lambda: "vis=" + str(pg.is_visible("#remote")))

        b.close()


if __name__ == "__main__":
    sys.exit(main())
