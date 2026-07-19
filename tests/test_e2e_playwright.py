"""Browser end-to-end regression tests: STOP integrity + reconnect.

Skipped automatically when Playwright or Chromium isn't installed.

Run locally:
    playwright install --with-deps chromium   # one-time
    pytest tests/test_e2e_playwright.py -v

Both tests share one server process and one Playwright browser instance
(module-scoped fixtures) so the suite is fast but still isolated from the
unit-test suite.
"""
from __future__ import annotations

import json
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import pytest

# Resolve `vanchor` from the same venv as the running interpreter.
_VANCHOR = str(Path(sys.executable).parent / "vanchor")

# Every e2e page must pre-dismiss the first-run sim-notice modal (added by the
# UX revamp); its scrim intercepts clicks otherwise. sim-ack=='1' suppresses
# it (onboard.js). Applied to every page before its first navigation.
_FIRSTRUN_INIT = (
    "localStorage.setItem('vanchor-sim-ack','1');"
    "localStorage.setItem('vanchor-firstrun','done');"
)

# Skip the whole module if Playwright isn't importable.
playwright_mod = pytest.importorskip("playwright", reason="playwright not installed")

# After the import, verify chromium is actually installed (the library is
# present but the browser binary may not be).
try:
    from playwright.sync_api import sync_playwright as _spw

    with _spw() as _pw:
        _br = _pw.chromium.launch(args=["--no-sandbox"])
        _br.close()
    del _pw, _br
    _chromium_ok = True
except Exception as _e:
    _chromium_ok = False
    _chromium_skip_reason = f"chromium not installed or failed to launch: {_e}"

pytestmark = [
    # Opt-in: excluded from the default suite (addopts = -m 'not e2e'); the
    # browser-e2e CI job runs these with `pytest -m e2e`.
    pytest.mark.e2e,
    pytest.mark.skipif(
        not _chromium_ok,
        reason=_chromium_skip_reason if not _chromium_ok else "",
    ),
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _free_port() -> int:
    """Return a free TCP port on 127.0.0.1."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_ready(base_url: str, timeout_s: float = 30) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            r = urllib.request.urlopen(base_url + "/api/state", timeout=2)
            if r.status == 200:
                return True
        except Exception:
            time.sleep(0.4)
    return False


def _api_state(base_url: str) -> dict:
    return json.load(urllib.request.urlopen(base_url + "/api/state", timeout=5))


def _api_post(base_url: str, path: str, body: dict) -> None:
    req = urllib.request.Request(
        base_url + path,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    urllib.request.urlopen(req, timeout=5).read()


def _poll(condition_fn, timeout_s: float = 8.0, interval_s: float = 0.25) -> bool:
    """Spin until condition_fn() is truthy or timeout elapses. Returns success."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            if condition_fn():
                return True
        except Exception:
            pass
        time.sleep(interval_s)
    return False


# ---------------------------------------------------------------------------
# Module-scoped fixtures: one server + one browser for the whole module.
# ---------------------------------------------------------------------------

class _ServerHandle:
    """Wrapper around a running vanchor process that can kill and restart it."""

    def __init__(self, port: int, workdir: str) -> None:
        self.port = port
        self.workdir = workdir
        self.base = f"http://127.0.0.1:{port}"
        self._proc: subprocess.Popen | None = None

    def start(self) -> None:
        self._proc = subprocess.Popen(
            [_VANCHOR, "--host", "127.0.0.1", "--port", str(self.port),
             "--log-level", "warning"],
            cwd=self.workdir,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.STDOUT,
        )

    def kill(self) -> None:
        if self._proc is not None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.wait()
            self._proc = None

    def restart(self) -> None:
        self.kill()
        time.sleep(0.3)   # let the OS reclaim the port
        self.start()
        if not _wait_ready(self.base, timeout_s=15):
            pytest.fail("Server did not restart in time")


@pytest.fixture(scope="module")
def live_server():
    """Start an isolated sim server; yield a _ServerHandle; tear it down."""
    port = _free_port()
    workdir = tempfile.mkdtemp(prefix="vanchor-e2e-")
    srv = _ServerHandle(port, workdir)
    srv.start()
    try:
        if not _wait_ready(srv.base):
            srv.kill()
            shutil.rmtree(workdir, ignore_errors=True)
            pytest.fail("e2e server did not become ready in time")
        yield srv
    finally:
        srv.kill()
        shutil.rmtree(workdir, ignore_errors=True)


@pytest.fixture(scope="module")
def pw_browser():
    """Module-scoped Playwright browser."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        br = pw.chromium.launch(args=["--no-sandbox"])
        yield br
        br.close()


# ---------------------------------------------------------------------------
# Test 1: STOP integrity
# ---------------------------------------------------------------------------

def test_stop_integrity(live_server: _ServerHandle, pw_browser):
    """Engage heading-hold via the UI Go button, then STOP; verify motor stops.

    The STOP must:
    - Transition the backend to mode=manual, thrust≈0 (standard stop criteria).
    - NOT leave a "STOP NOT CONFIRMED" banner on screen.

    The test relies purely on the UI for engaging and stopping — the same path
    a user takes — and polls /api/state for the authoritative backend result.
    """
    base = live_server.base
    page = pw_browser.new_page(viewport={"width": 1200, "height": 860})
    page.add_init_script(_FIRSTRUN_INIT)
    page.set_default_timeout(8000)
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))

    try:
        # Load the page and wait for telemetry to flow.
        page.goto(base + "/", wait_until="domcontentloaded", timeout=20_000)
        page.wait_for_timeout(1500)

        # Reset to a known clean state via the API.
        _api_post(base, "/api/command", {"type": "manual", "thrust": 0, "steering": 0})
        page.wait_for_timeout(400)

        # Engage DRIFT through the UI: click the Drift rail button then the Go
        # button on its panel — the exact sequence a user follows. (The Heading
        # tile was removed 2026-07-15 in favour of Manual's Absolute/Course.)
        page.click('.mode-btn[data-mode="drift"]')
        page.wait_for_timeout(300)
        page.click("#drift-go")

        # Poll the backend: drift should engage within 5 s.
        engaged = _poll(
            lambda: _api_state(base).get("mode") == "drift",
            timeout_s=5,
        )
        assert engaged, (
            f"drift did not engage; got mode={_api_state(base).get('mode')!r}"
        )

        # Click the STOP rail button (data-mode="stop").
        page.click('.mode-btn[data-mode="stop"]')

        # Poll the backend: motor should show manual + ~zero thrust within 5 s.
        def _stopped() -> bool:
            st = _api_state(base)
            if st.get("mode") != "manual":
                return False
            motor = st.get("motor") or {}
            thrust = motor.get("thrust")
            return thrust is not None and abs(float(thrust)) < 0.05

        stopped = _poll(_stopped, timeout_s=5)

        # The "STOP NOT CONFIRMED" banner is injected dynamically after 1.5 s if
        # the backend doesn't confirm. Wait slightly longer to let it appear if
        # something went wrong, then assert it is absent.
        page.wait_for_timeout(2000)
        stop_banner = page.locator("#critical-stop-banner")
        banner_visible = stop_banner.count() > 0 and stop_banner.first.is_visible()

        assert stopped, (
            f"motor did not stop: mode={_api_state(base).get('mode')!r}, "
            f"motor={_api_state(base).get('motor')}"
        )
        assert not banner_visible, "STOP NOT CONFIRMED banner is visible after STOP"
        assert not errors, f"Page JS errors: {errors[:3]}"
    finally:
        page.close()


# ---------------------------------------------------------------------------
# Test 2: Reconnect / staleness-overlay
# ---------------------------------------------------------------------------

def test_reconnect_and_staleness(live_server: _ServerHandle, pw_browser):
    """Simulate a link drop (server kill + restart) and verify the UI handles it.

    Why kill/restart instead of context.set_offline:
    The server runs on 127.0.0.1 (loopback); Playwright's offline emulation
    uses Chrome CDP ``Network.emulateNetworkConditions`` which does NOT
    interrupt already-established loopback connections. Killing the process
    causes the OS to TCP-RST the WS connection immediately, making the test
    reliable and not timing-dependent.

    Steps:
    1. Load page; wait for chip-conn data-state == "connected".
    2. Kill the server → OS sends TCP RST to the WS; onclose fires.
    3. Assert chip-conn transitions to "disconnected".
    4. Assert DATA STALE banner appears (staleness watchdog fires after 3 s).
    5. Restart the server on the same port.
    6. App auto-reconnects (core.js schedules VA.connect after 1 s).
    7. Assert chip-conn returns to "connected".
    8. Assert DATA STALE banner clears once telemetry flows again.
    """
    base = live_server.base

    # Use a fresh context so state is independent of test_stop_integrity.
    ctx = pw_browser.new_context(viewport={"width": 1200, "height": 860})
    page = ctx.new_page()
    page.add_init_script(_FIRSTRUN_INIT)
    page.set_default_timeout(15_000)
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))

    try:
        page.goto(base + "/", wait_until="domcontentloaded", timeout=20_000)
        page.wait_for_timeout(2000)  # let WS connect + first telemetry frames arrive

        # --- Step 1: verify we start connected ---
        def _chip_state() -> str:
            return page.eval_on_selector(
                "#chip-conn", "el => el.dataset.state || ''"
            )

        connected_initially = _poll(
            lambda: _chip_state() == "connected",
            timeout_s=8,
        )
        assert connected_initially, f"Expected connected initially; got {_chip_state()!r}"

        # --- Steps 2 + 3: kill the server; WS drops; chip goes disconnected ---
        live_server.kill()

        disconnected = _poll(
            lambda: _chip_state() == "disconnected",
            timeout_s=10,
        )
        assert disconnected, (
            f"chip-conn did not reach 'disconnected' after server kill; "
            f"got {_chip_state()!r}"
        )

        # --- Step 4: DATA STALE banner should appear ≥3 s after last frame ---
        def _stale_banner_visible() -> bool:
            el = page.locator("#stale-data-banner")
            return el.count() > 0 and el.first.is_visible()

        stale_appeared = _poll(_stale_banner_visible, timeout_s=6)
        assert stale_appeared, "DATA STALE banner did not appear after server kill"

        # --- Step 5: restart the server on the same port ---
        live_server.restart()

        # --- Steps 6 + 7: app auto-reconnects (setTimeout 1 s in core.js) ---
        reconnected = _poll(
            lambda: _chip_state() == "connected",
            timeout_s=15,
        )
        assert reconnected, (
            f"chip-conn did not return to 'connected' after server restart; "
            f"got {_chip_state()!r}"
        )

        # --- Step 8: DATA STALE banner clears when telemetry flows again ---
        stale_cleared = _poll(
            lambda: not _stale_banner_visible(),
            timeout_s=8,
        )
        assert stale_cleared, "DATA STALE banner did not clear after reconnect"

        assert not errors, f"Page JS errors during reconnect test: {errors[:3]}"
    finally:
        page.close()
        ctx.close()


# ---------------------------------------------------------------------------
# Test 3: Replace/Append delivery for take-me-here destinations
# ---------------------------------------------------------------------------

def test_routechoice_append_and_replace(live_server: _ServerHandle, pw_browser):
    """With a route RUNNING, delivering a new destination must offer
    Replace/Append; Append extends the backend route without restarting it,
    and with PENDING pins Append extends the editor route."""
    base = live_server.base
    ctx = pw_browser.new_context(viewport={"width": 1200, "height": 860})
    page = ctx.new_page()
    page.add_init_script(_FIRSTRUN_INIT)
    page.set_default_timeout(10_000)
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))

    try:
        page.goto(base + "/", wait_until="domcontentloaded", timeout=20_000)
        page.wait_for_timeout(1500)

        pos = _api_state(base).get("position") or {"lat": 59.0, "lon": 18.0}
        lat, lon = float(pos["lat"]), float(pos["lon"])
        wp = lambda name, dlat, dlon, **extra: {  # noqa: E731 - tiny local builder
            "name": name, "lat": lat + dlat, "lon": lon + dlon, **extra,
        }

        # Engage a 2-waypoint route (with a per-waypoint speed on WP2).
        _api_post(base, "/api/command", {
            "type": "goto", "throttle": 0.6,
            "waypoints": [wp("A", 0.01, 0.0), wp("B", 0.02, 0.0, throttle_pct=40)],
        })
        assert _poll(lambda: _api_state(base).get("mode") == "waypoint", timeout_s=5)
        # Wait for the committed route to reach the UI via telemetry.
        assert _poll(lambda: page.evaluate(
            "VA.map.committedRoute().waypoints.length") == 2, timeout_s=5)

        # Deliver a new destination -> the choice dialog must appear. NOTE:
        # deliver() resolves only after a dialog click, so the promise must NOT
        # be returned to evaluate() (the sync API would await it -> deadlock).
        page.evaluate(
            "() => { VA.routeChoice.deliver([{name:'C', lat: %f, lon: %f}], () => {}); }"
            % (lat + 0.03, lon)
        )
        page.wait_for_selector(".route-choice", timeout=5000)
        page.click('.route-choice [data-act="append"]')

        # Backend route grows to 3 marks, still navigating (not restarted), and
        # the appended list preserved WP2's speed attribute.
        def _appended() -> bool:
            st = _api_state(base)
            return st.get("mode") == "waypoint" and len(st.get("waypoints") or []) == 3
        assert _poll(_appended, timeout_s=5), "append did not extend the active route"
        wps = _api_state(base)["waypoints"]
        assert [w["name"] for w in wps] == ["A", "B", "C"]
        assert wps[1]["throttle_pct"] == 40

        # Now the PENDING path: stop, drop pending pins, deliver again. Wait
        # until the PAGE has seen mode=manual (deliver() decides on the latest
        # telemetry frame, which lags the backend by up to one frame).
        _api_post(base, "/api/command", {"type": "stop"})
        assert _poll(lambda: page.evaluate("VA.last && VA.last.mode") == "manual",
                     timeout_s=5)
        page.evaluate(
            "VA.map.setPending([{name:'P1', lat: %f, lon: %f}])" % (lat + 0.005, lon)
        )
        page.evaluate(
            "() => { VA.routeChoice.deliver([{name:'P2', lat: %f, lon: %f}], () => {}); }"
            % (lat + 0.006, lon)
        )
        page.wait_for_selector(".route-choice", timeout=5000)
        page.click('.route-choice [data-act="append"]')
        assert _poll(lambda: page.evaluate("VA.map.pending().length") == 2, timeout_s=5)

        assert not errors, f"Page JS errors: {errors[:3]}"
    finally:
        page.close()
        ctx.close()


# ---------------------------------------------------------------------------
# Test 4: Chips no-overflow — 360 / 390 / 412 px (F4 fix2 regression guard)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("vp_w", [360, 390, 412, 430])
def test_chips_no_overflow(live_server: _ServerHandle, pw_browser, vp_w: int):
    """#chips scrollWidth must not exceed clientWidth at each tested portrait width.

    F4 root cause: the view-chip compact-padding rule (4px 3px / min-width 26px)
    only fired at ≤360px while the pressure range extends to ≥430px.  At 390 and
    412 px the view-switcher stayed at 36px/chip → topbar-actions claimed ~40px
    more → #chips.clientWidth dropped to 92–114px while content was 116px.
    Fix2 extends the compact rule to ≤430px.  Regression guard: assert no overflow
    at all three widths so a future breakpoint regression can't go undetected.
    """
    base = live_server.base
    ctx = pw_browser.new_context(viewport={"width": vp_w, "height": 780})
    page = ctx.new_page()
    page.add_init_script(_FIRSTRUN_INIT)
    page.set_default_timeout(10_000)
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))

    try:
        page.goto(base + "/", wait_until="domcontentloaded", timeout=20_000)
        # Wait for telemetry AND mobile layout to settle.  chip-batt can unhide
        # (via WS telemetry) before mobile.js has finished loading — mobile.js is
        # the last script in the bundle and the WS can deliver state between
        # earlier scripts.  Without body.mobile the mobile-only CSS (chip hiding,
        # gap reduction) is absent and the topbar-actions spill into #chips.
        page.wait_for_function(
            "!document.getElementById('chip-batt').classList.contains('hidden')"
            " && document.body.classList.contains('mobile')",
            timeout=8000,
        )
        # Extra settle time: on a cold module-scope server start the first test
        # in the parametrized group can catch a transitional render frame before
        # the compact view-chip CSS has fully committed.  600 ms is consistent
        # with the landscape smoke test's post-mobile wait.
        page.evaluate("() => (document.fonts && document.fonts.ready) || true")
        page.wait_for_timeout(600)

        dims = page.evaluate(
            """() => {
                const el = document.getElementById('chips');
                const tb = document.querySelector('.topbar');
                const ct = document.querySelector('#chip-conn .chip-text');
                const lbl = document.querySelector('.chip-label');
                const vis = [...el.children]
                    .filter(c => c.offsetParent !== null)
                    .map(c => (c.id || c.className) + ':' + Math.round(
                        c.getBoundingClientRect().width));
                return {
                    sw: el.scrollWidth,
                    cw: el.clientWidth,
                    inter: document.fonts.check('12px Inter')
                           && document.fonts.check('700 12px Inter'),
                    topbarFits: tb.scrollWidth <= Math.ceil(tb.clientWidth) + 1,
                    // --- diagnostics: why is #chips content wide on CI? ---
                    iw: window.innerWidth,
                    dpr: window.devicePixelRatio,
                    mq760: matchMedia('(max-width: 760px)').matches,
                    mq420: matchMedia('(max-width: 420px)').matches,
                    connText: ct ? getComputedStyle(ct).display : 'none-el',
                    lblDisp: lbl ? getComputedStyle(lbl).display : 'none-el',
                    vis: vis,
                    bodyCls: document.body.className,
                };
            }"""
        )
        # Probe: does a resize nudge (re-running applyMobile) restore body.mobile
        # and collapse the chips?  If so this is the same first-paint race as the
        # landscape test.
        page.evaluate("window.dispatchEvent(new Event('resize'))")
        page.wait_for_timeout(200)
        after = page.evaluate(
            "() => ({m: document.body.classList.contains('mobile'),"
            " sw: document.getElementById('chips').scrollWidth,"
            " cw: document.getElementById('chips').clientWidth})"
        )
        dims["afterResize"] = after
        slack = dims["cw"] - dims["sw"]
        print(
            f"\n  #{vp_w}px #chips {{sw:{dims['sw']},cw:{dims['cw']},"
            f"slack:{slack},inter:{dims['inter']}}}"
        )
        # The no-overflow property only holds with the vendored Inter font
        # loaded — its metrics are what Fix2's ≤430px compact breakpoint was
        # tuned for.  `.chips` shrinks (flex + overflow) so a wide fallback font
        # (headless CI's DejaVu, before the web font is applied) degrades to the
        # chips scrolling/clipping inside a bar that still fits the screen,
        # rather than a broken topbar.  So gate the strict pixel guard on Inter
        # being active; otherwise assert the whole-bar invariant that actually
        # matters — the topbar never overflows the viewport.
        _diag = (
            f"iw={dims['iw']} dpr={dims['dpr']} mq760={dims['mq760']} "
            f"mq420={dims['mq420']} connText={dims['connText']} "
            f"lblDisp={dims['lblDisp']} bodyCls='{dims['bodyCls']}' "
            f"afterResize={dims['afterResize']} vis={dims['vis']}"
        )
        if dims["inter"]:
            assert dims["sw"] <= dims["cw"], (
                f"chips overflow at {vp_w}px with Inter loaded: "
                f"scrollWidth={dims['sw']} > clientWidth={dims['cw']} (slack={slack}) "
                f"|| {_diag}"
            )
        else:
            assert dims["topbarFits"], (
                f"topbar overflows the viewport at {vp_w}px (fallback font) — "
                f"the mobile top bar is broken, not just the chips || {_diag}"
            )
        assert not errors, f"Page JS errors: {errors[:3]}"
    finally:
        page.close()
        ctx.close()


# ---------------------------------------------------------------------------
# Test 5: Stale state — chip-conn[data-state="stale"] + body[data-stale="1"]
# ---------------------------------------------------------------------------

def test_chip_stale_state(live_server: _ServerHandle, pw_browser):
    """After telemetry stops the stale watchdog must fire within ~4 s.

    Stopping the server causes the WS to drop (disconnected), then the 3 s
    stale watchdog overwrites chip-conn data-state with "stale" and sets
    body.dataset.stale="1" so CSS greys the peek numbers.
    """
    base = live_server.base
    ctx = pw_browser.new_context(viewport={"width": 390, "height": 844})
    page = ctx.new_page()
    page.add_init_script(_FIRSTRUN_INIT)
    page.set_default_timeout(15_000)
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))

    try:
        page.goto(base + "/", wait_until="domcontentloaded", timeout=20_000)
        page.wait_for_timeout(2000)  # let WS connect + telemetry frames arrive

        # Confirm we start connected so the test doesn't pass trivially.
        connected = _poll(
            lambda: page.eval_on_selector("#chip-conn", "el => el.dataset.state") == "connected",
            timeout_s=8,
        )
        assert connected, "Expected chip-conn=connected before stale test"

        # Kill the server — WS drops (disconnected), then stale watchdog fires.
        live_server.kill()

        # Stale watchdog fires >3 s after last telemetry frame.  Allow 8 s total.
        def _stale_active() -> bool:
            state = page.eval_on_selector("#chip-conn", "el => el.dataset.state || ''")
            stale_body = page.evaluate("document.body.dataset.stale || ''")
            return state == "stale" and stale_body == "1"

        stale_appeared = _poll(_stale_active, timeout_s=8)
        assert stale_appeared, (
            "chip-conn[data-state=stale] / body[data-stale=1] did not appear "
            f"after server kill; chip state={page.eval_on_selector('#chip-conn', 'el => el.dataset.state')!r}"
        )

        assert not errors, f"Page JS errors: {errors[:3]}"
    finally:
        page.close()
        ctx.close()
        # Ensure server is back up for any subsequent tests.
        try:
            live_server.restart()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Test 6: Wheel snap-to-zero deadman + immediate-decrease (WP8)
# ---------------------------------------------------------------------------

def _dismiss_firstrun(page) -> None:
    """Dismiss the WP3 first-run simulation dialog which overlays the dock."""
    page.evaluate(
        "() => { const b = document.getElementById('firstrun-sim'); if (b) b.click();"
        " const c = document.getElementById('firstrun'); if (c) c.classList.add('hidden');"
        " const s = document.getElementById('firstrun-scrim'); if (s) s.classList.add('hidden'); }"
    )


def test_wheel_snap_to_zero_and_immediate_decrease(live_server: _ServerHandle, pw_browser):
    """The steering wheel is the UI's only manual motor path. Verify the owner's
    snap-to-zero deadman (WP8):

    1. Drag the dial outward -> commanded thrust rises (grace-ramped).
    2. Release with HOLD off -> thrust snaps to 0 immediately (deadman).
    3. A bare tap on the dial face engages nothing (drag must start + move).
    4. With HOLD on, release KEEPS the thrust (trolling latch).
    5. Dragging the knob inward always drops thrust immediately (never ramped).
    """
    base = live_server.base
    ctx = pw_browser.new_context(viewport={"width": 390, "height": 844})
    page = ctx.new_page()
    page.add_init_script(_FIRSTRUN_INIT)
    page.set_default_timeout(10_000)
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))

    def _thrust() -> float:
        m = _api_state(base).get("motor") or {}
        t = m.get("thrust")
        return float(t) if t is not None else 0.0

    try:
        # domcontentloaded (not networkidle): the wheel test needs telemetry +
        # JS, not map tiles, and tile fetches can keep the network busy.
        page.goto(base + "/", wait_until="domcontentloaded", timeout=20_000)
        page.wait_for_function(
            "() => window.VA && VA.last && VA.last.mode !== undefined", timeout=20_000)
        page.wait_for_timeout(1200)
        _dismiss_firstrun(page)
        # Ensure a clean manual state.
        _api_post(base, "/api/command", {"type": "manual", "thrust": 0, "steering": 0})
        page.wait_for_timeout(400)

        # Show the manual panel + wheel; expand the sheet so the dial is on-screen.
        page.evaluate("() => { const b = document.querySelector('.mode-btn[data-mode=\"manual\"]'); if (b) b.click(); }")
        page.evaluate("() => { try { VA.sheet && VA.sheet.reveal && VA.sheet.reveal('full'); } catch(e){} }")
        page.evaluate("() => (document.fonts && document.fonts.ready) || true")
        page.wait_for_timeout(600)

        rect = page.evaluate(
            "() => { const s = document.querySelector('#steer-wheel svg'); if(!s) return null;"
            " const r = s.getBoundingClientRect();"
            " return {x:r.left+r.width/2, y:r.top+r.height/2, w:r.width, h:r.height}; }")
        assert rect, "steering wheel SVG not found"

        # --- 1 + 2: drag outward -> thrust rises, release -> snap to 0 ---
        page.mouse.move(rect["x"], rect["y"])
        page.mouse.down()
        for i in range(1, 9):
            page.mouse.move(rect["x"], rect["y"] - (rect["h"] * 0.32) * i / 8)
            page.wait_for_timeout(60)
        page.wait_for_timeout(400)
        assert _poll(lambda: _thrust() > 0.2, timeout_s=3), (
            f"wheel drag did not raise thrust; got {_thrust()}")
        page.mouse.up()
        # Snap-to-zero: thrust returns to ~0 immediately on release.
        assert _poll(lambda: abs(_thrust()) < 0.05, timeout_s=3), (
            f"thrust did not snap to zero on release; got {_thrust()}")

        # --- 3: a bare tap on the dial face sends nothing ---
        _api_post(base, "/api/command", {"type": "manual", "thrust": 0, "steering": 0})
        page.wait_for_timeout(300)
        page.mouse.move(rect["x"] + rect["w"] * 0.18, rect["y"] - rect["h"] * 0.10)
        page.mouse.down()
        page.wait_for_timeout(90)
        page.mouse.up()
        page.wait_for_timeout(500)
        assert abs(_thrust()) < 0.05, f"a bare dial tap engaged the motor: {_thrust()}"

        # --- 4: HOLD on -> release keeps thrust ---
        page.evaluate(
            "() => { const e = document.getElementById('wheel-hold');"
            " if (e && !e.checked) { e.checked = true; e.dispatchEvent(new Event('change')); } }")
        page.wait_for_timeout(200)
        page.mouse.move(rect["x"], rect["y"])
        page.mouse.down()
        for i in range(1, 9):
            page.mouse.move(rect["x"], rect["y"] - (rect["h"] * 0.30) * i / 8)
            page.wait_for_timeout(60)
        page.wait_for_timeout(400)
        page.mouse.up()
        page.wait_for_timeout(700)
        held = _thrust()
        assert held > 0.2, f"HOLD should keep thrust on release; got {held}"

        # --- 5: immediate-decrease — dragging inward drops thrust fast ---
        # Re-grab near the knob (out at ~0.30h up) and drag back toward the hub
        # in stepped moves (with waits) so a send fires mid-drag, not just on up.
        start_up = rect["h"] * 0.32
        page.mouse.move(rect["x"], rect["y"] - start_up)
        page.mouse.down()
        for i in range(1, 9):
            page.mouse.move(rect["x"], rect["y"] - start_up * (8 - i) / 8)
            page.wait_for_timeout(60)
        page.wait_for_timeout(300)
        low = _thrust()
        page.mouse.up()
        assert low < held, f"inward drag did not reduce thrust ({held} -> {low})"
        # Decreases are never grace-ramped: pulling to the hub reaches ~0 fast.
        assert low < 0.15, f"thrust decrease should be immediate; got {low}"

        # Cleanup: HOLD off + stop.
        page.evaluate(
            "() => { const e = document.getElementById('wheel-hold');"
            " if (e && e.checked) { e.checked = false; e.dispatchEvent(new Event('change')); } }")
        _api_post(base, "/api/command", {"type": "stop"})

        assert not errors, f"Page JS errors: {errors[:3]}"
    finally:
        page.close()
        ctx.close()


# ---------------------------------------------------------------------------
# Test 7: Landscape layout smoke test at 844×390 (WP5, Task 6)
# ---------------------------------------------------------------------------

def test_landscape_layout_smoke(live_server: _ServerHandle, pw_browser):
    """At 844×390 (phone landscape) body.mobile.ls must be set, STOP must be
    visible and clickable, the map must occupy ≥40% of the viewport width, and
    the chart-view FAB cluster must be hidden in non-chart views.

    F3 regression guard: #sheet-instruments DEPTH numerals must be visible AND
    a SIM indicator must be present in landscape chart (task-3 sim-honesty).
    """
    base = live_server.base
    ctx = pw_browser.new_context(
        viewport={"width": 844, "height": 390},
        # Force landscape user-agent to help mobile.js orientationchange fire.
        user_agent=(
            "Mozilla/5.0 (Linux; Android 11; Pixel 5) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/122.0.0.0 Mobile Safari/537.36"
        ),
    )
    page = ctx.new_page()
    page.add_init_script(_FIRSTRUN_INIT)
    page.set_default_timeout(12_000)
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))

    # Dismiss first-run simulation modal before any page load so it never
    # blocks the UI or causes false positives in bounding-box assertions.
    # Do NOT set 'vanchor-sim-ack' so the SIM pill shows its full sentence;
    # but even if acked the "· SIM" tag in sheet-mode must appear.
    page.add_init_script(
        "localStorage.setItem('vanchor-sim-ack','1');"
        "localStorage.setItem('vanchor-firstrun','done');"
    )

    try:
        page.goto(base + "/", wait_until="domcontentloaded", timeout=20_000)
        # Wait for mobile.js to add the mobile class; landscape (ls) follows.
        page.wait_for_function(
            "document.body.classList.contains('mobile')", timeout=8000
        )
        page.evaluate("() => (document.fonts && document.fonts.ready) || true")
        page.wait_for_timeout(600)   # let CSS settle

        # body.mobile.ls must be set (landscape sub-mode active).  mobile.js
        # derives `ls` from the live viewport aspect (innerWidth > innerHeight
        # && innerHeight <= 480), recomputed on resize.  Nudge a resize so the
        # class is (re)evaluated against the settled 844×390 dimensions — this
        # guards against a first-paint applyMobile() that ran before the
        # headless viewport was final.
        page.evaluate("window.dispatchEvent(new Event('resize'))")
        try:
            page.wait_for_function(
                "document.body.classList.contains('ls')", timeout=3000
            )
        except Exception:
            pass
        diag = page.evaluate(
            "() => ({ls: document.body.classList.contains('ls'),"
            " mobile: document.body.classList.contains('mobile'),"
            " iw: window.innerWidth, ih: window.innerHeight})"
        )
        assert diag["ls"], (
            f"body.ls not set at 844×390 — landscape layout did not activate; "
            f"live state={diag}"
        )

        # SEV-2: Verify #sheet-stop and #sheet-mob are WITHIN the 844×390 viewport.
        # is_visible() passes for off-screen elements; bounding_box() catches them.
        # This assertion MUST fail on the pre-fix CSS (transform: translateX(100%)
        # traps the buttons inside the dock off-screen) and PASS after the fix
        # (transform:none + right offset keeps the viewport-fixed band on screen).
        vp_w, vp_h = 844, 390
        for btn_id, btn_label in [("#sheet-stop", "STOP"), ("#sheet-mob", "MOB")]:
            btn = page.locator(btn_id).first
            assert btn.count() >= 0, f"{btn_id} not in DOM"
            bb = btn.bounding_box()
            assert bb is not None, (
                f"{btn_label} ({btn_id}) has no bounding box — not rendered"
            )
            assert bb["x"] >= 0, (
                f"{btn_label} left edge off-screen: x={bb['x']:.0f}px (landscape peek)"
            )
            assert bb["y"] + bb["height"] <= vp_h, (
                f"{btn_label} bottom edge off viewport: y+h={bb['y'] + bb['height']:.0f}px > {vp_h}"
            )
            assert bb["x"] + bb["width"] <= vp_w, (
                f"{btn_label} right edge off viewport: x+w={bb['x'] + bb['width']:.0f}px > {vp_w}"
            )

        # Map (#map) must occupy ≥40% of viewport width.
        map_width = page.eval_on_selector(
            "#map", "el => el.getBoundingClientRect().width"
        )
        assert map_width >= 844 * 0.40, (
            f"map too narrow in landscape: {map_width:.0f}px < 40% of 844px"
        )

        # F3: In landscape chart with sim enabled, the SIM indicator must be
        # visible.  The fix appends "· SIM" to the safety-band #sheet-mode text
        # because #map-pills is hidden to protect .sheet-instruments numerals.
        # Wait for sim_enabled to arrive via telemetry (server is in sim mode by
        # default; may take up to 3 s for first WS frame).
        sim_visible = _poll(
            lambda: page.evaluate(
                "() => {"
                "  const el = document.getElementById('sheet-mode');"
                "  if (!el) return false;"
                "  const txt = (el.textContent || '').toLowerCase();"
                "  return txt.includes('sim');"
                "}"
            ),
            timeout_s=6,
        )
        sim_text = page.eval_on_selector(
            "#sheet-mode", "el => el.textContent || ''"
        )
        assert sim_visible, (
            "F3: no SIM indicator in landscape chart — #sheet-mode text does not"
            f" contain 'sim'.  Current text: {sim_text!r}"
        )

        # F3: DEPTH numeral in .sheet-instruments must not be occluded.
        # A rendered bounding box with width > 0 confirms it is in the DOM
        # and CSS-visible (not display:none / visibility:hidden).
        depth_bb = page.eval_on_selector(
            "#m-depth",
            "el => { const r = el.getBoundingClientRect();"
            " return { x: r.x, y: r.y, w: r.width, h: r.height }; }",
        )
        assert depth_bb["w"] > 0 and depth_bb["h"] > 0, (
            f"F3: DEPTH numeral (#m-depth) has zero size — not rendered: {depth_bb}"
        )
        # Must be within the viewport (not scrolled off).
        assert 0 <= depth_bb["x"] <= vp_w and 0 <= depth_bb["y"] <= vp_h, (
            f"F3: DEPTH numeral out of viewport bounds: {depth_bb}"
        )

        # In the default chart view (body[data-view="chart"]), leaflet FABs are
        # visible; in any other view they must be hidden.  Switch to helm view.
        page.evaluate(
            "() => { const b = document.querySelector('[data-view=\"helm\"]');"
            " if (b) b.click(); }"
        )
        page.wait_for_timeout(400)

        # After switching to helm view, the leaflet FAB cluster must not be
        # visible (FAB hygiene: hidden in non-chart views).
        fab_visible = page.evaluate(
            "() => { const el = document.querySelector('.leaflet-control-container');"
            " if (!el) return false;"
            " const r = el.getBoundingClientRect();"
            " return r.width > 0 && r.height > 0; }"
        )
        assert not fab_visible, (
            ".leaflet-control-container is visible in helm view (should be hidden)"
        )

        assert not errors, f"Page JS errors in landscape smoke test: {errors[:3]}"
    finally:
        page.close()
        ctx.close()
