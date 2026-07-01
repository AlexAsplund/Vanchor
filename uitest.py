"""Effect-verifying UI test: each step is isolated; report what works / breaks."""
import json, urllib.request
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8000"
def state(): return json.load(urllib.request.urlopen(BASE + "/api/state"))
def post(p, b): urllib.request.urlopen(urllib.request.Request(BASE+p, data=json.dumps(b).encode(), headers={"Content-Type":"application/json"}))

results = []
with sync_playwright() as p:
    b = p.chromium.launch(args=["--no-sandbox"])
    pg = b.new_page(viewport={"width":1280,"height":900}); pg.set_default_timeout(2000)
    seen=[]
    pg.on("pageerror", lambda e: seen.append("PAGEERROR: "+str(e)))
    pg.on("console", lambda m: seen.append("console.error: "+m.text) if m.type=="error" else None)
    pg.goto(BASE+"/", wait_until="networkidle", timeout=20000); pg.wait_for_timeout(1200)

    def step(label, fn, check=None):
        seen.clear()
        try:
            fn(); pg.wait_for_timeout(500)
            detail = check() if check else ""
            results.append((label, "ERR" if seen else "ok", str(detail)))
        except Exception as e:
            results.append((label, "FAIL", str(e).splitlines()[0][:70]))
        for e in seen[:2]: results.append(("  !"+label, "", e))
        seen.clear()
    def mode(m): pg.click(f'.mode-btn[data-mode="{m}"]'); pg.wait_for_timeout(300)
    def jsclick(s): pg.eval_on_selector(s, "el=>el.click()")
    def setopen(s): pg.eval_on_selector(s, "el=>el.open=true")

    post("/api/command", {"type":"manual","thrust":0,"steering":0}); pg.wait_for_timeout(300)

    step("heading_hold + hdg-go", lambda:(mode("heading_hold"), pg.click("#hdg-go")), lambda:f'mode={state()["mode"]}')
    step("drift + drift-go", lambda:(mode("drift"), pg.click("#drift-go")), lambda:f'mode={state()["mode"]}')
    step("anchor + jog-fwd", lambda:(mode("anchor_hold"), pg.click("#jog-fwd")), lambda:f'mode={state()["mode"]}')
    step("anchor hold-hdg switch", lambda: jsclick("#hold-hdg"))
    step("anchor radius=8", lambda: pg.eval_on_selector("#ar","el=>{el.value=8;el.dispatchEvent(new Event('input'));el.dispatchEvent(new Event('change'))}"), lambda:f'r={state()["anchor_radius_m"]}')
    step("cruise toggle", lambda:(setopen("#cruise-card"), jsclick("#cruise-on")), lambda:f'enabled={state()["cruise"]["enabled"]}')
    step("track record", lambda:(setopen("#track-card"), pg.click("#track-rec")), lambda:f'rec={state()["track"]["recording"]}')
    step("track stop", lambda: pg.click("#track-rec"), lambda:f'rec={state()["track"]["recording"]}')
    # ROUTE: selecting Route mode opens the editor panel (it must NOT auto-start
    # a route — that would engage the motor). Exercise the real editor controls.
    step("route mode -> editor visible", lambda: mode("waypoint"), lambda:"wp-arm visible="+str(pg.is_visible("#wp-arm")))
    step("route arm add-waypoints", lambda: pg.click("#wp-arm"), lambda:"armed="+str(pg.eval_on_selector("#wp-arm","e=>e.classList.contains('active')")))
    step("settings open", lambda: pg.click("#settings-open"), lambda:"vis="+str(pg.is_visible("#settings-close")))
    step("settings theme switch", lambda: jsclick("#theme-toggle-box"), lambda:"light="+str(pg.eval_on_selector("body","e=>e.classList.contains('light')")))
    step("depth overlay toggle", lambda: jsclick("#depth-show") if pg.locator("#depth-show").count() else None)
    step("sim card visible", None, lambda:"vis="+str(pg.eval_on_selector("#sim-card","e=>!e.classList.contains('hidden')") if pg.locator("#sim-card").count() else "absent"))
    step("settings close", lambda: pg.click("#settings-close"))
    step("wizard open", lambda: pg.click("#setup-open"), lambda:"vis="+str(pg.is_visible("#wizard")))
    step("wizard consent+next", lambda:(jsclick("#wizard input[type=checkbox]"), pg.click("#wizard >> text=Next")), lambda:"step2="+str(pg.is_visible("#wizard")))
    step("wizard close", lambda: jsclick("#wiz-close") if pg.locator("#wiz-close").count() else pg.keyboard.press("Escape"))
    step("remote open", lambda: pg.click("#remote-toggle"), lambda:"vis="+str(pg.is_visible("#remote")))
    b.close()

print(f"{'ACTION':<34}{'RESULT':<7}DETAIL")
print("-"*92)
for l,r,d in results: print(f"{l:<34}{r:<7}{d}")
