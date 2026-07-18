# Task 1 report — Safety core: WP1 (alarm + STOP surfaces) + WP4 (alarm actions, modal safety)

Branch `dev/adoption-pack`. All work per `.superpowers/sdd/ux/task-1-brief.md`.
Suite: **2074 passed, 6 skipped, 10 deselected** (`.venv/bin/python -m pytest -q`).
`node --check` clean on all 10 touched JS files; `ruff check src/ tests/` clean.

## What was built (by architecture decision)

### D1 — One alarm surface: the strip stack
- `#banner` retired: element, CSS (`:552` block, mobile override, `:1868`-era
  media override, stray `#banner { top:132px }`), and `hud.js updateBanner()`
  all deleted. No test referenced it; `test_shell_partials.py` now asserts its
  absence.
- `.safety-banners` rewritten: `position:fixed; top:var(--topbar-h,52px);
  left:0; right:0; z-index:3000; pointer-events:none` — full-width at every
  viewport, container pointer-transparent by law; only `.sb-act`/buttons are
  `pointer-events:auto` (min-height 44px).
- Priority via CSS `order` exactly per brief, including the four dynamic
  health strips: fault 0 > MOB 1 > anchor 2 > fix-lost 3 > shallow 4 >
  hdg-stale 5 > batt-crit 6 > batt-warn 7 > link 8 > depth-stale 9 > rtl 10 >
  auto-apb 11 > governor 12. (Governor strip id is `#gov-banner`, a naming
  deviation from the brief's `#governor-banner` — noted below.)
- Emoji sweep: no color emoji in any strip; `test_no_emoji_in_safety_banner_messages`
  pins it. Alarm strips now pulse **brightness, not opacity** (the old opacity
  pulse made strip text unreadable half of every second over the map).
- `reverse_blocked`/`thrust_limited` advisories moved from hud.js into the new
  `#gov-banner` warn strip (safety.js `renderBanners`).

### D2 — Anchor/drag alarm strip (items 1+9)
`#anchor-alarm-banner` with `sb-title` + `sb-sub` + RECOVER (600 ms hold ring,
`<small>HOLD 0.6 s</small>`) + SILENCE (`2 MIN`) + `■ STOP` (`sb-act-stop`,
solid `var(--stop)` fill). `renderAnchorAlarmStrip(t)` in safety.js:
- Passive alarm (`anchor_alarm.firing`): title "ANCHOR ALARM — DRAGGING",
  sub `"{aa.distance_m} m from anchor · drifting {compass8} · m:ss"` (bearing
  computed alarm-point→boat, elapsed from a client-side firing edge). RECOVER
  visible → `{type:"anchor_alarm_recover"}` (server engages anchor_hold **at
  the alarm point**, not the drifted position).
- Active drag (`safety.drag_alarm`): title "ANCHOR DRAGGING", sub
  `"… m from hold point"`, RECOVER hidden (motor already fighting).
- SILENCE: sound-only for 120 s via new `VA.sound.silence(ms)`; the strip
  **stays visible** and the button shows a live countdown ("1:43").
- Strip message text wraps (never ellipsised) so the cause is always readable.

### D3 — Stale/critical overlays never block controls (item 2)
`core.js _banner`: `pointer-events:none` on both inline banners; STALE now
renders **below the topbar** (`top:52px`, `z-index:2900` — real alarms stack
above); STOP-not-confirmed keeps `top:0` z 100000. Verified: grip tap cycles
the sheet with the STALE strip up (S4).

### D4 — Two-row peek: STOP-left / mode-center / MOB-right (items 3+13)
- `PEEK_PX 76 → 152` + `--peek-h:152px`; every 76px coupling swept
  (`translateY` peek rules, follow-fab, toast bottom). The one remaining
  `76px` in style.css (`.comp-legend` at ~:1182) is a legend-stacking offset
  above `.depth-legend`, not a sheet coupling — commented as such.
- Peekbar DOM = visual order: `#sheet-stop` (solid red, 56px, left) /
  `#sheet-mode` (now a `<button>`; tap opens sheet to mid; degraded suffix
  wraps in alarm color) / `#sheet-mob` ("MAN OVERBOARD" spelled out, filled
  `#e8820c`, 56px, right, 600 ms hold via shared `bindHold`; short tap shows
  a hold-hint toast and after a completed hold the synthetic click is
  swallowed so the hint can't fire post-action).
- `#sheet-collapse` moved out of the peekbar up beside the grip (absolute,
  mid/full only) so STOP is always the leftmost action.
- One-style sweep: `.rbtn-stop`, `.prs-stop`, `.vd-stop`, `.sheet-stop` all
  solid `var(--stop)`+white (no translucent `color-mix`); daylight theme
  verified `rgb(210,31,60)` (A17 partial).
- Specificity fix worth knowing: the blanket
  `body.mobile .dock button { min-height:44px }` rule out-specifies
  `body.mobile .sheet-stop`; peek rules are therefore written as
  `body.mobile .sheet-peekbar .sheet-*` (and the grip as
  `body.mobile .sheet-head .sheet-grip`).

### D5 — Battery ladder (item 4)
- Client: `battLevel` → crit <10 / low <25. `#batt-warn-banner` (amber) /
  `#batt-crit-banner` (red) driven from `renderBattery`; copy "Battery 24% ·
  ~1.9 km range" (range only when finite and >0); RTL buttons hidden unless
  `t.launch.set` and are 600 ms hold-to-engage; edge-triggered `VA.logAlert`
  on threshold crossings; `#rtl-banner` hides while a battery strip is up
  (no double "return now" shouting).
- Server `evaluate_rtl_recommend`: the `range_m <= 0` early-return now
  recommends when `soc <= 10` ("unknown range is not infinite") and returns
  **before** the auto-RTL block — `_schedule_auto_rtl` gating untouched,
  pinned by `test_battery_rtl.py::TestAutoRtlGatingUntouched` (both auto_rtl
  off and the zero-range-even-with-auto_rtl-on cases).
- Server `evaluate_push_alerts`: new soc<25 / soc<10 push+alert-log edges
  (once per crossing, `prev` dict pattern), plus `alert_log.record(...)` on
  every existing edge (drag, battery-rtl, link, shallow, divergence,
  fix-lost) with boat lat/lon; the anchor-alarm `on_breach` hook records with
  the **alarm point's** coordinates.

### D6 — Alarms above modals + STOP in the modal header (item 10)
Z-order law documented in style.css (map 0 < dock 1150 < menu 1300 < scrim
1900 < command menu 2000 < stale 2900 < strips 3000 < toast 4000 < wizard
5000/6100 < critical-stop 100000; native `<dialog>` top layer = accepted
exception). `#cm-stop` compact solid-red pill in `.cm-appbar`: visible iff
`VA.motorActive` (brief's predicate: any non-manual mode, or manual with
|thrust|>0.05), instant `sendCritical`, closes the menu so the confirm banner
is visible.

### D7 — Honest degraded states (item 11)
`VA.modeSuffix(t)` (appcore.js) driven **only** by `safety.shallow_stop` /
`safety.nogo_stop` → " — STOPPED (shallow)" / " — STOPPED (no-go zone)" —
deliberately NOT inferred from zero thrust (a station-keeper at rest idles at
zero thrust). Consumers: `mobile.js` sheet-mode (+ `.stopped` alarm color,
wraps to 2 lines) and `map-boat.js` badge (suffix in rebuild key +
`data-stopped` styling). Shallow strip copy: "SHALLOW — auto-stopped · resumes
when deeper than X m" + `■ STOP`; **no Resume button** (governor re-evaluates
every tick; resume is automatic — documented in code). GPS-lost: boat marker
greys via `fix-stale` class **on the marker element only** (waypoints keep
their colors); badge gains "last fix Ns ago" quantized to 5 s buckets (no
per-frame divIcon rebuild).

### D8 — Server-persisted alert history (item 12)
New `src/vanchor/core/alertlog.py` (`AlertLog`: thread-safe, `alerts.json` in
data_dir, debounced atomic writes, corrupt-file tolerant, `path=None` →
memory-only). `Runtime.alert_log` init next to the other persistence.
`GET /api/alerts` + `POST /api/alerts/clear` in server.py (the demo-readonly
middleware already blocks the mutating POST). `alerts.js`: hydrates from the
API on boot (merge by ts+message, silent on 404/offline), per-entry
"Show on map" (pans) and hold-Recover on anchor-alarm entries while the alarm
is still firing, Clear behind an inline two-step confirm that also POSTs the
server clear.

### D9 — Tablet/desktop pinned STOP + MOB bar (item 3)
`#dock-stop-bar` as last dock child: `position:sticky; bottom:-10px` inside
the dock's own scroll (survives full dock scroll — verified by scripted
scroll-to-bottom shot), STOP left (flex 1) / MOB right, both 56px solid.
Hidden under `body.mobile` (peek row owns phones; also avoids the
`.dock-scroll` wrapper double-STOP). Wired in menu.js (`sendCritical` /
600 ms hold MOB).

### D10 — Anchor re-drop safety (item 14)
`#anchor-engaged` block: "HOLDING — d m from point · r m circle" +
"■ RELEASE — stop holding" (→ `sendCritical stop`) + "Re-drop here — moves
the point" (replaces `#anchor-go`, which hides while anchored; idle two-step
drop unchanged). Re-drop snapshots the prev anchor at click, shows a 10 s
toast "Anchor moved N m → UNDO"; UNDO sends
`{type:"anchor_hold", anchor:{prev}}` (server adopts the point,
controller.py:701-703). `VA.toast` extended with `{actionLabel, onAction,
ttl}`.

## Contract impact
Zero new telemetry keys, zero new command types; `tests/test_contract.py`
passes unmodified (`return_to_launch`/`set_battery` were already declared).
New REST endpoints documented in their docstrings.

## Tests added
- `tests/test_alert_log.py` (19): record/snapshot/clear/cap/severity/
  persistence-roundtrip/corrupt-file + `GET /api/alerts` +
  `POST /api/alerts/clear` via TestClient (snapshot/clear only — no telemetry
  loops, per project memory) + Runtime-restart rehydration.
- `tests/test_battery_rtl.py` (14): `evaluate_rtl_recommend` zero-range/soc
  matrix (soc 8 → True, soc 80 → False, soc==10 boundary, no-soc, positive-
  range unchanged, no-launch), auto-RTL gating untouched (2), battery alert
  edges once-per-crossing + recovery re-arm (4), ladder handoff (2).
- `tests/test_shell_partials.py` (+10): all new ids present, `#banner` absent,
  peekbar order, `#cm-stop` inside `.cm-appbar`, no-emoji regex over every
  `.sbanner` message, endpoints smoke.
- `scripts/take_screenshots.py`: new `alarm` scene (390px, alarm firing) with
  the bounding-box assertion — verified passing: `x=0, w=390 (viewport 390)`.
- `tests/conftest.py`: `alerts.json` added to the guarded repo-data files
  (one legacy non-isolated test writes it; the session guard now removes it).

## Screenshot evidence — `.superpowers/sdd/ux/t1-shots/` (LOOKED at, all pass)

| # | File(s) | Verified |
|---|---|---|
| S1 | `S1-phone-idle.png`, `S1-tablet-idle.png`, `S1-tablet-dock-scrolled.png` | Two-row peek; STOP bbox x=13 h=56 bottom=835≤844 (left slot), MOB right (x=241..377, orange, spelled out); tablet sticky bar on screen after full dock scroll |
| S2 | `S2-phone-anchor-alarm.png` | Full-width red strip x=0 w=390; title + full sub "67 m from anchor · drifting N · 0:01"; RECOVER/SILENCE/■ STOP on strip; map ring+tag |
| S3 | `S3-phone-multi-alarm.png` | 4 stacked ordered rows (MOB > anchor > shallow > batt-crit), every message fully readable |
| S4 | `S4-phone-stale.png`, `S4-phone-stale-sheet-open.png` | STALE strip below topbar; grip tap through it cycles sheet (peek→full) |
| S5 | `S5-phone-batt-24.png`, `S5-phone-batt-8.png` | Amber then red strip + chip; hold-RTL button (launch set) |
| S6 | `S6-phone-menu-during-alarm.png`, `S6-tablet-menu-during-alarm.png` | Strip paints above the open menu at both widths; `#cm-stop` in header (motor mode active) |
| S7 | `S7-phone-shallow-stop.png` | sheet-mode "Heading — STOPPED (shallow)" in alarm color; strip "resumes when deeper than 99.0 m" + ■ STOP; map badge suffixed |
| S8 | `S8-phone-gps-lost.png` | Greyed boat marker; badge "last fix 15s ago"; GPS LOST strip; topbar chip LOST |
| S9 | `S9-phone-anchored-panel.png`, `S9-phone-redrop-undo-toast.png` | HOLDING status + RELEASE + relabeled re-drop; toast "Anchor moved 40 m / UNDO" |
| S10 | `S10-phone-alerts-hydrated.png` | Server-hydrated entries after reload, per-entry "Show on map", Clear-all present |
| S11 | `S11-landscape-idle.png` | No new landscape regression (STOP/MOB visible; layout rebuild itself is Task 6) |
| SD | `SD-phone-daylight-idle.png` | Daylight peek STOP solid `rgb(210,31,60)` |

Plus `docs/images/mobile-anchor-alarm.png` from the new harness scene.

## Known deviations / notes for the reviewer
1. **Governor strip id** is `#gov-banner` (brief sketched `#governor-banner`);
   behavior identical, no test depends on the name.
2. **Anchor-alarm strip ids** are `aa-banner-*` (brief sketched `aab-*`);
   structure (`sb-body`/`sb-title`/`sb-sub`) matches the brief.
3. **Map mode-badge text can clip at the viewport edge** when the boat sits
   right-of-center (badge anchors up-right of the boat; pre-existing
   geometry, now more visible with the longer suffix). Candidate polish for
   Task 2/5.
4. Leaflet's mobile zoom control (top-right, `top:56px`) sits inside the
   strip band and is painted over while alarms are up; it stays tappable
   where no strip button overlaps (strips are pointer-transparent). The
   concepts accept chrome-band overlap; flagging for the map-interaction
   task (WP6).
5. `.comp-legend`'s `76px` is intentionally untouched (legend stacking
   offset, not a peek coupling) — commented in the CSS.
6. Peek height landed at **152px** (not the brief's sketched 148px): the
   grip(24) + instruments(46) + 56px action row + gaps/padding measure 150px
   in the real layout; 148 clipped the action row by 3px at 390x844.
7. The mobile MOB peek button carries the full "MAN OVERBOARD" label (not
   "MOB" + small print) — reads clearly at 390px and satisfies the
   spelled-out hard rule; happy to compress in review if the mode chip needs
   more width.
