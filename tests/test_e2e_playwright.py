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
    page.set_default_timeout(8000)
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))

    try:
        # Load the page and wait for telemetry to flow.
        page.goto(base + "/", wait_until="networkidle", timeout=20_000)
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
    page.set_default_timeout(15_000)
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))

    try:
        page.goto(base + "/", wait_until="networkidle", timeout=20_000)
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
    page.set_default_timeout(10_000)
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))

    try:
        page.goto(base + "/", wait_until="networkidle", timeout=20_000)
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
# Test 4: Chips no-overflow at 360px (M1 fix verification)
# ---------------------------------------------------------------------------

def test_chips_no_overflow_360px(live_server: _ServerHandle, pw_browser):
    """#chips scrollWidth must not exceed clientWidth at 360×780 mobile viewport.

    At 360px the topbar-actions (4 view-chips + alerts + settings) claim ~220px
    leaving ~120px for the health chips.  The M1 fix reduces chip padding/font
    and drops chip-fix to dot-only so all three chips fit within that budget.
    """
    base = live_server.base
    ctx = pw_browser.new_context(viewport={"width": 360, "height": 780})
    page = ctx.new_page()
    page.set_default_timeout(10_000)
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))

    try:
        page.goto(base + "/", wait_until="networkidle", timeout=20_000)
        # Wait for telemetry to flow so chip-batt is visible (worst-case width).
        page.wait_for_function(
            "!document.getElementById('chip-batt').classList.contains('hidden')",
            timeout=5000,
        )
        page.wait_for_timeout(300)  # let layout settle

        dims = page.eval_on_selector(
            "#chips",
            "el => ({ sw: el.scrollWidth, cw: el.clientWidth })",
        )
        assert dims["sw"] <= dims["cw"], (
            f"chips overflow at 360px: scrollWidth={dims['sw']} > clientWidth={dims['cw']}"
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
    page.set_default_timeout(15_000)
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))

    try:
        page.goto(base + "/", wait_until="networkidle", timeout=20_000)
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
            state = page.eval_on_selector("#chip-conn", "el => el.dataset.state or ''")
            stale_body = page.evaluate("document.body.dataset.stale or ''")
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
