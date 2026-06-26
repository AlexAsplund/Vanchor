"""Headless mobile screenshot + console capture of the running UI."""
import sys
from playwright.sync_api import sync_playwright

URL = "http://127.0.0.1:8000/"
OUT = sys.argv[1] if len(sys.argv) > 1 else "/tmp/claude-1000/-home-alex-projects-vanchor-ng/c2764d2c-7102-48ba-b8b7-6ad9f8af3f78/scratchpad/ui_mobile.png"

with sync_playwright() as p:
    b = p.chromium.launch(args=["--no-sandbox"])
    page = b.new_page(viewport={"width": 390, "height": 844}, device_scale_factor=2)
    msgs, errs = [], []
    tiles = {"ok": 0, "fail": 0}
    page.on("console", lambda m: msgs.append(f"{m.type}: {m.text}"))
    page.on("pageerror", lambda e: errs.append(str(e)))
    page.on("response", lambda r: tiles.__setitem__("ok", tiles["ok"] + 1) if ("cartocdn" in r.url and r.status == 200) else None)
    page.on("requestfailed", lambda r: tiles.__setitem__("fail", tiles["fail"] + 1) if "cartocdn" in r.url else None)
    page.goto(URL, wait_until="networkidle", timeout=20000)
    page.wait_for_timeout(2500)
    page.screenshot(path=OUT, full_page=False)
    print("PAGE ERRORS:", errs or "none")
    print("CONSOLE err/warn:", [m for m in msgs if m.startswith(("error", "warning"))] or "none")
    print(f"CARTO tiles: ok={tiles['ok']} fail={tiles['fail']}")
    print("saved", OUT)
    b.close()
