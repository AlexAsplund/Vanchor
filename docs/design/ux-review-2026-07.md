# Vanchor-NG UX Review — July 2026 (synthesis)

**Sources:** six expert hands-on reviews driven against a live isolated sim server
(Playwright, real UI, 390x844 / 844x390 / 1180x820, plus 360x780 for robustness), branch
`dev/adoption-pack`. Raw reviews and all screenshots: `.superpowers/ux/` (`expert-*.md`,
`shots/`). Expert tags used throughout: **[safety]** (safety-critical UX),
**[flows]** (task-flow), **[ia]** (information architecture), **[ergo]** (mobile ergonomics),
**[first]** (first-time user), **[robust]** (PWA/robustness).

**Persona everywhere:** recreational fisherman, wet hands, glare, waves, maybe gloves,
not a tech person, often mid-panic when the screens that matter appear.

---

## 1. Executive summary — the five themes that matter on the water

### Theme 1 — The safety layer fails exactly where the fisherman lives (unanimous)
All six experts independently found that **the default phone-portrait chart screen — the state
the app opens in and returns to — has no reachable STOP** while a mode is actively driving the
boat (`#sheet-stop` measured at y=875 in an 844px viewport). The project's own stated safety
floor ("STOP always works") is violated at the UI layer: STOP works, but it isn't *there*.
The same screen also hides MOB entirely, and on tablet STOP scrolls away inside the dock.
The Helm view proves the team knows how to do this right (huge pinned STOP + MOB) — the
default view just never inherited it.

### Theme 2 — The alarm channel is broken end-to-end at phone width
When things go wrong, the phone actively fails the user: alarm banners render **half
off-screen** ("🚨 ANCHOR ALARM — dragging" reads as "LARM — dragging"; root cause identified
at `style.css:2093` — a mobile override keeps the base rule's `translateX(-50%)`);
simultaneous alarms **overprint into an unreadable smear**; the DATA STALE banner **physically
covers the sheet grip and intercepts taps**, locking the user out of the controls during link
trouble; battery reached **6–12% with zero pixels changing** above the fold; and through all
of it the topbar keeps saying "connected · GPS OK" with a green dot. Alarms also carry no
action — the excellent one-tap RECOVER exists but is buried three levels deep.

### Theme 3 — The best screens and fastest paths are undiscoverable
Anchor-in-3-taps, contour-troll-in-4, the boat-tap popup (2-tap anchor), the Helm view, the
Go-to arm flow — the *content* is best-in-class. But: the first screen is a dark map with every
control below the fold; the collapsed sheet shows instrument **labels without values**; the
topbar clips heading to a single misleading digit; map taps do nothing and never hint at
"Take me here"; the Helm view hides behind a 30x34px unlabeled emoji. A first-timer needs to be
shown the grip bar once — the app never does it. And nothing on a fresh install says the boat
is **simulated**.

### Theme 4 — Engineering vocabulary sits in the fishing path
"APB" occupies a prime mode slot while TROLL hides behind MORE. The flagship Anchor panel
stacks four look-alike toggles ("Smart station-keeping (learned)", "Leif ⓘ", "Vectored
thrust") whose explanations live in hover tooltips that don't exist on touch — and tapping
Leif's ⓘ **actually starts the motor** (the info icon sits inside the switch label, and
variant toggles engage the mode directly). Raw mode strings (`contour_follow`, "anchor leif")
leak to the badge, and in `anchor_leif` no mode tile highlights at all.

### Theme 5 — Layout debt at the extremes: landscape phone broken, tablet under-tuned
Phone landscape (a phone in a rod holder / cheap dash mount) gets the desktop layout: panels
collide, DROP ANCHOR HERE renders behind the instrument strip, the map is a sliver, and the
helm view's tiles collapse into unusable slivers. The tablet chart layout is genuinely good
("uses the width, not a stretched phone UI" — three experts verbatim) but needs bigger targets,
a pinned STOP, a mode chip in the topbar, and a fix for the steering-gauge ghost text that
bleeds through the dock on every wide viewport (5 experts hit it).

**What is genuinely good (all experts, keep it):** STOP semantics (instant, idempotent,
"STOP NOT CONFIRMED — check link" honesty); the anchor-alarm plain-language copy ("the real
anchor holds the boat"); the two-step anchor engagement; the Init wizard's safety gate; the
Helm/Instruments views; the tablet chart skeleton; fix-loss motor failsafe; offline app-shell
+ fast load; the 9-card menu top level; add-waypoints auto-collapse pattern.

---

## 2. Findings by area (merged & deduplicated)

Severity is the consensus of the experts who found each issue. Screenshot prefixes map to
experts: `p*/l*/t*-` [ergo or ia], `ft-` [first], `pp-/pl-/tl-` [flows], `rb-` [robust],
`safety-` [safety].

### 2.1 Alarms & safety

| # | Finding | Severity | Evidence | Fix |
|---|---|---|---|---|
| A1 | **No STOP on the default portrait chart** (collapsed sheet): `#sheet-stop` at y=875/844; mode-grid STOP also below fold; topbar has none. Two precise gestures to stop a driving boat. | **Blocker** — all 6 experts ([ergo] B1, [first] M2, [ia] 1, [safety] 3, [robust] M3, [flows] via F-05) | `p1-main.png`, `safety-p-anchor-engaged-map.png`, `rb-portrait-main.png`, `ft-pp-15-anchor-dropped-map.png` | Persistent STOP whenever a motor mode is active: red STOP in the collapsed peek row (raise the snap height, see A9) or a floating 56px+ red pill bottom-right. Verify at 390x844 *collapsed*. Files: `index.html` (sheet head), `mobile.js`, `style.css`. |
| A2 | **Alarm banners clipped half off-screen at 390px** — every alarm ("G ALARM", "LARM — dragging"). Root cause found: `body.mobile #banner` (`style.css:2093`) overrides `left` but keeps `transform: translateX(-50%)` from `style.css:552`; `#banner` measured at x=-167. | **Blocker** — [safety] 1, [flows] F-01 | `safety-p-drag-alarm-map.png`, `pp-21-alarm-tripped.png`, `safety-p-shallow-stop.png` | Add `transform: none` to the mobile override; make mobile alarm banners full-width strips under the topbar. Add a 390px regression screenshot test. |
| A3 | **DATA STALE banner covers the sheet grip and intercepts pointer events** — during link loss the user cannot open the sheet to reach STOP (40s of retried taps never landed). | **Blocker** — [safety] 2, [robust] m8 | `safety-p-linkloss-map.png`, `rb-p-linkloss-45s.png` | `pointer-events: none` on `#stale-data-banner`; relocate it to a strip below the topbar. Rule: status elements never overlay controls. |
| A4 | **Tapping the Leif ⓘ starts the motor.** The info icon sits inside the `<label class="switch">`; flipping any anchor variant toggle immediately engages `anchor_leif`/`anchor_ml` from Manual — no DROP ANCHOR needed (verified server-side). | **Blocker** — [flows] F-02 | `pp-16-leif-info-tap.png` | Move ⓘ outside the label / `stopPropagation`; variant toggles select *style* only — engagement stays exclusively on DROP ANCHOR HERE. Files: anchor panel markup in `index.html`/partials, `controls.js` or `map-anchor.js`. |
| A5 | **Battery emergency is silent.** SOC driven to 18% → 12% → 6–7%: no banner, no alert entry, chip clipped away, status dot stayed green; `rtl_recommended` stayed false even at `range_m: 0` with a launch point 200m away. | **Blocker** — [flows] F-03, [ia] 8, [safety] 7 | `pp-34-lowbatt.png`, `p-batt-crit.png`, `safety-p-battery-low.png` | Fixed SOC thresholds (amber <25%, red <10%) → banner + alert-history entry + chip color, independent of the RTL range ladder; fix `rtl_recommended` to trigger when usable range < distance-home. Server side (`safety.js` UI + runtime battery monitor). |
| A6 | **Link loss: topbar keeps claiming "connected · GPS OK · LINK OK"** (green dot, fresh-looking frozen numbers) while the stale banner counts up. False-safe indication. | Major — [robust] M1, [safety] 11 | `rb-p-linkloss-15s.png`, `safety-p-linkloss-map.png` | Drive `chip-conn`/status dot from the same staleness clock as the banner; grey out or "--" stale numeric chips. `health.js`/`core.js`. |
| A7 | **Simultaneous alarms overprint into a smear** — `#banner` and `.safety-banners` both at top:52px; drag+shallow+battery rendered "G ALARM"/"opped" fused. | Major — [safety] 4 | `safety-p-multi-alarm.png`, `safety-p-gps-lost-map.png` | One alarm surface, vertically stacked, priority-ordered (MOB > drag/anchor > shallow > battery > link), each row full-width. |
| A8 | **Alarm banners carry no action** — drag/anchor banners are inert; "⚓ RECOVER — HOLD AT ALARM POINT" is 3 levels deep. RTL and MOB banners already have inline buttons — pattern exists. | Major — [safety] 6 | `safety-p-aa-firing-sheet.png` | Add "Recover" + "Stop" buttons on drag/anchor-alarm banners, mirroring `rtl-banner-go`/`mob-banner-clear`. `safety.js`. |
| A9 | **Collapsed peek strip shows labels ("SOG HDG DEPTH BATT") with the values below the fold.** The default screen's only instruments are captions without data. | Major — 5 experts ([ergo] M2, [flows] F-05, [ia] 17, [robust] M3, [safety] 9b) | `p1-main.png`, `pp-01-launch.png`, `safety-home-portrait.png` | Raise the collapsed snap height ~28–40px so the value row fits; use the reclaimed row for STOP (A1). `mobile.js` snap points. |
| A10 | **Opening the Menu hides an active alarm and every STOP.** A live drag alarm pulsed invisibly behind the settings modal. | Major — [safety] 5, [ergo] m8 | `safety-t-settings-during-alarm.png` | Re-render the alarm strip above modals; compact ■ STOP in the modal header when a motor mode is active. `menu.js`, `settings.js`. |
| A11 | **MOB is unreachable from the chart view** — exists only in Helm/Instruments views. | Major — [safety] 10 | `safety-p-view-helm.png` vs `safety-home-portrait.png` | Add MOB to the chart (peek bar or long-press FAB). |
| A12 | **GPS lost: boat marker and numbers keep full-confidence styling** on a dead position (chip says LOST — good; map says nothing). | Major — [safety] 12 | `safety-p-gps-lost-map.png` | Stale-fix treatment: grey marker + "last fix 42s ago" tag. `map-boat.js`. |
| A13 | **Shallow auto-stop leaves the mode pill claiming "Heading"** — is it still driving? Will it resume? UI doesn't say. | Major — [safety] 13 | `safety-p-shallow-stop.png` | Mode pill → "Heading (STOPPED — shallow)" in alarm color; banner gains Resume / Stop-mode actions. |
| A14 | **Alert history is per-page: alarms vanish on reload**; and "Clear" is the hottest control in the dialog (evidence loss one tap away). | Major — [flows] F-13, [safety] 18 | `pp-37-alerts.png`, `safety-p-drag-alarm-history.png` | Persist recent alerts server-side, hydrate on load; per-entry action ("Recover", "Show on map"); Clear behind confirm. `alerts.js` + server. |
| A15 | **Panic re-taps on DROP ANCHOR HERE silently move the hold point** (~45m to the drifted position, verified). | Minor — [safety] 15 | — | When anchored, relabel "Re-drop here (moves point)" and/or undo toast "Anchor moved 45m → undo". |
| A16 | **RTL sits beside SET LAUNCH HERE with zero engagement friction** — one drives the boat away, one marks a point, same size, adjacent. | Minor — [safety] 16 (contested — see Open question 1) | `safety-t-route-underway.png` | Visually separate the pair now; hold-to-engage pending owner decision. |
| A17 | **Daylight theme STOP pill is white on pale pink — 1.8:1 contrast.** The one control that must be readable in sun is the least readable. | Major — [ergo] M5 | `p7-daylight-sheet.png` | Solid `#D21F3C` fill + white text (4.9:1) in daylight; no low-alpha fills on safety chrome. `style.css`. |
| A18 | **Alert bell permanently warning-yellow with zero alerts**; no badge/count when something does land. | Minor — [ergo] m10, [ia] 24, [robust] m6 | `p1-main.png` | Grey outline bell when empty; badge with count + top-severity color when not. |
| A19 | **Green master status dot never changes** through drag alarm, GPS loss, shallow stop, link loss. | Minor — [safety] 21 | — | Make it worst-of health (green/amber/red) or remove it. |

### 2.2 Onboarding & first run

| # | Finding | Severity | Evidence | Fix |
|---|---|---|---|---|
| O1 | **Nothing says the boat is SIMULATED.** Fresh install boots in sim with "connected · GPS OK" and a moving boat — every signal says "this is your boat". The `demo-indicator` badge exists but is hidden (public-demo only). | **Blocker** — [first] B1 | `ft-pp-01-first-open.png`, `ft-tl-01-first-open.png` | Persistent "SIMULATION — not your motor" pill (tappable → Devices/hardware wizard) whenever mode=sim; first-run choice "Set up my real boat / Play with the simulator". `demo.js`, `devices.js`. |
| O2 | **First screen: everything below the fold**, no hint that the grip opens controls; sheet-grip cycling is discoverable only by accident. | Major — [first] M1, [robust] p3, [ergo] P2 | `ft-pp-01-first-open.png` | First-run coach mark on the grip ("swipe up for controls") or start at half height on first run. |
| O3 | **Nothing points at the Init Boat wizard** (3 levels deep); autopilot runs untuned defaults forever. Also titled "Init Boat" — dev-speak. | Major — [first] M5, [flows] F-28 | `ft-pp-04-menu-open.png` | First-run card "New boat? Run setup (10 min on open water)" + "Get started" menu tile until completed once; rename "Boat setup". `menu.js`, `wizard.js`. |
| O4 | **Calibration failure dead-ends with a raw Python errno** (`[Errno 2] No such file or directory: 'vanchor_data'`), Next disabled, retry unlabeled. Boat did stop (good). | Major — [first] M6 | `ft-pp-10-calib-done.png` | Plain-language error + explicit RETRY + raw string behind "details". `wizard.js`. |
| O5 | **Hardware wizard opens on raw `/dev/serial/by-path/...` strings and allows SAVE & RESTART with nothing probed.** | Major — [first] M10 | `ft-pp-26-hw-wizard-step1.png`, `ft-pp-27-hwwiz-step5.png` | Friendly device names ("USB GPS (u-blox) — port 1"); disable Save until ≥1 device probed or explicit "keep simulator"; plain step intros. `hwwizard.js`. |
| O6 | **Wizard units/jargon**: "Max thrust N" (motors are sold in lbs), "m/s" results in a knots app, "ACCELERATION TIME CONST", sliders for exact numbers, results text clipping. | Minor — [first] m2/m3, [ia] 22 | `ft-pp-07-wizard-step2.png`, `ft-pp-11-wizard-step4.png` | lb-thrust presets (30/40/55/80), knots everywhere, tap-to-type fields, friendly result labels. |
| O7 | **The best first-timer screens (Helm/big Manual) hide behind four unlabeled 30x34px emoji** — most first-timers will never see them. | Major — [first] M7, [ergo] m2, [ia] 12, [flows] F-26 | `ft-pp-30-helm-view.png`, `t-view-helm.png` | ≥44px switcher targets, text labels on wide screens ("Chart / Helm / Gauges / Manual"), first-run Helm suggestion on tablet. See Open question 2 for phone default. `views.js`, `index.html`. |
| O8 | **Devices/Safety category pages are one collapsed accordion on an empty screen** (name echo: Safety → "Safety & power"); inconsistent with Sound & touch which auto-expands. | Minor — [ia] 14, [first] m11 | `p-set-4-safety.png`, `ft-pp-23-devices-top.png` | Auto-expand when a category has ≤2 sections; drop the echoed heading. `menu.js`. |

### 2.3 Driving / steering (Manual)

| # | Finding | Severity | Evidence | Fix |
|---|---|---|---|---|
| D1 | **Wheel only responds if the drag starts on the 26px knob** — center/face drags silently ignored (`steerwheel.js` `pointerdown` rejects starts beyond KNOB_R+12). Reviewer's first two attempts produced zero output. | Major — [ergo] M3 | `p6-wheel-active.png` | Accept drag start anywhere on the dial (ease knob to finger); keep knob-grab for fine control. `steerwheel.js`. |
| D2 | **~56px of radial travel spans 0→100% thrust, and thrust latches on release** (verified: flick left motor at `thrust: 1.0` after release). A wave-jolt slip = boat at 100% with no visible STOP (A1). | Major — [ergo] M4 | `p6-wheel-active.png` | Widen the radial band to the whole dial radius; grace ramp for >40% jumps; release behavior per Open question 3. `steerwheel.js` (R_H_MIN/R_H_MAX). |
| D3 | **Cruise block prepended to every guided-mode panel**: two speed sliders with different values on one screen (which drives the boat?), three speed vocabularies (kn / kn / %), and up to **three differently-styled STOPs at once**; pushes real mode controls below the fold. | Major — [ia] 6+7, [flows] F-20, [safety] 17 | `p-mode-troll-panel.png`, `p-mode-route-panel.png`, `pp-03-route-panel.png` | Collapse cruise block to a one-line summary chip ("Cruise 2.0 kn ▸"); one speed control per panel (the mode's own, in knots; % only for Manual thrust); exactly one STOP style per screen; Pause/Resume as a single toggle. `controls.js`, `navctl.js`, partials. |
| D4 | **Thrust slider is bidirectional with no FWD/REV marking**, 26–28px tall. | Minor — [ergo] m3 | `p2-sheet-expanded.png` | "R \| 0 \| F" ticks, 44px hit area, detent at 0. |
| D5 | **Wheel instruction paragraph permanently rendered** (4 lines, clipped mid-sentence in portrait). | Minor — [ia] 28, [ergo] P3 | `t-main-idle.png` | One line + ⓘ tap-to-expand; teach Relative/Absolute/Course via one-time tooltips. |
| D6 | **Steer-mode vocabulary** (Relative/Absolute/Course) describes reference frames, not outcomes. | Minor — [ergo] m1 | `p2-more-modes.png` | Rename to outcomes ("Off the bow / Compass / Straight line"), technical term second. |
| D7 | **START ROUTE with no waypoints silently does nothing** — reads as "app is broken" on a bouncing boat. | Major — [flows] F-09 | `pp-06-route-going.png` | Disable until ≥1 waypoint + toast "No waypoints yet — tap Add waypoints, then tap the map". `route.js`. |

### 2.4 Anchoring & fishing modes

| # | Finding | Severity | Evidence | Fix |
|---|---|---|---|---|
| F1 | **Anchor panel variant toggles are incomprehensible and mis-modeled**: four identical-looking switches, of which two are mutually exclusive styles (flipping Leif silently un-flips Smart — looks like a glitch); descriptions live in `title` hover tooltips that cannot be read on touch; "Leif"/"(learned)"/"Vectored" are opaque. Found independently by six experts. | Major — [flows] F-06+F-07, [ia] 5, [safety] 19, [robust] m4, [first] m1, [ergo] m1 | `pp-11-anchor-panel.png`, `p-mode-anchor-panel.png`, `ft-pp-13-anchor-panel.png` | Labelled segmented control "Anchor style: Classic \| Smart \| Leif" with one visible plain sentence each ("Smart: learned station-keeping, uses less battery", "Leif: experimental — never idles, expect constant motion"); "Hold heading" and "Vectored thrust" as separate options behind an "Advanced" disclosure; ⓘ opens a small sheet. Naming per Open question 4. |
| F2 | **Weak engagement feedback: the panel is identical before and after DROP ANCHOR** — still shouts "DROP ANCHOR HERE" while anchored; "did it take?" answered nowhere near the finger. | Major — [ia] 9, [robust] M10 | `p-mode-anchor-panel.png` vs `p-anchor-engaged.png`, `rb-t-anchor-engaged.png` | Swap CTA to engaged state ("⚓ HOLDING — 5 m circle · MOVE / RELEASE"), tint panel header with active-mode color, show dist-to-anchor in panel. Pairs with A15. |
| F3 | **Raw mode strings leak** (`contour_follow`, lowercase "anchor leif") and **no mode tile highlights in `anchor_leif`** — the state glance fails on the most exotic mode. | Major — [flows] F-08 | `pp-26-contour-going.png`, `pp-16-leif-info-tap.png` | Single mode→display-name map used everywhere; ANCHOR tile highlights for all `anchor_*`. |
| F4 | **TROLL (headline fishing feature) hides behind MORE + a scroll, while APB (NMEA niche) holds a prime slot.** MORE tray also wraps below the fold. | Major — [ia] 16, [first] m4/m15, [flows] F-14, [ergo] m12; minor [ia] 19 | `p-sheet-state1.png`, `p-more-open.png`, `ft-pp-34-more-panel.png` | Swap: TROLL to main grid, APB behind MORE (Open question 6); auto-grow the sheet when MORE opens; one-line subtitles on submodes. |
| F5 | **Mode vocabulary**: APB (pure jargon, satellite icon), CONTOUR (fishermen say "follow the depth line"), DRIFT panel opens with a *target heading* slider (not what drift fishing means), MOB bare acronym in helm view. | Major — [ia] 4 | `p-more-open.png`, `t-view-helm.png` | Rename with fishing language first, term second ("Depth line (contour)"); spell "MAN OVERBOARD"; DRIFT gets a one-line subtitle. |
| F6 | **Anchor watch circle invisible at normal zoom** (5m radius sub-pixel at z16) — user can't see what the boat will hold. | Minor — [flows] F-16, [first] m7 | `tl-03-anchored.png`, `ft-pp-15-anchor-dropped-map.png` | Minimum on-screen pixel radius for the circle; dist→anchor in the sheet head while anchored. `map-anchor.js`. |
| F7 | **Stale DIST→ANCHOR shown when not anchored** ("10.7 m" through Manual and Route); boat popup offers "Weigh anchor" regardless of state. Five experts hit it. | Minor — [ergo] m6, [flows] F-17, [ia] 26, [safety] 23, [first] m6 | `t1-main.png`, `safety-t-home.png` | Blank ("—")/hide the tile and "Weigh anchor" unless an anchor mode is active. `hud.js`, `map-boat.js`. |
| F8 | **JOG ANCHOR arrows unlabeled** — boat-relative or compass? meters per tap? A wrong guess jogs toward the rocks. | Minor — [flows] F-18, [ia] 21 | `pp-11-anchor-panel.png` | Label N/S/E/W (or bow-relative with boat glyph) + "1 m per tap"; compact 3x3 pad. |
| F9 | **SET ALARM HERE gives no map feedback** (sheet stays up covering the ring); while *dragging*, the collapsed sheet strip stays neutral. | Minor — [flows] F-24+F-25 | `pp-20-alarm-set.png`, `pp-21-alarm-tripped.png` | Auto-collapse to map on set (reuse add-waypoints pattern) + toast; collapsed sheet head turns red "DRAGGING — 66 m" while alarm active. |
| F10 | **Logging a catch is unreachable on default phone portrait** — the 🎣 FAB is part of the HUD overlay which is off by default in portrait; Fishing settings tell you to tap a button that isn't on screen. | Major — [flows] F-10 | `pp-01-launch.png` vs `tl-01-launch.png` | Show the catch FAB on the portrait map regardless of the HUD pref, or add "Log catch" to the boat-tap popup. `catch.js`, `hud.js`. |
| F11 | **Anchor-alarm no-entry marker lingers across sessions unexplained**, no way to clear from the map. | Minor — [robust] m9 | `rb-s-main.png` | Tapping it names it and offers "Clear alarm". |

### 2.5 Map

| # | Finding | Severity | Evidence | Fix |
|---|---|---|---|---|
| M1 | **"Take me here" is undiscoverable**: map tap does nothing, long-press responds below the fold, the feature hides in a collapsed accordion below the steering wheel (phone) and *three levels deep* on tablet — most-used fisherman action, most steps. | Major — [first] M3, [robust] M7, [flows] F-21, [ergo] m4 | `ft-pp-17-map-tap.png`, `rb-t-dock-scrolled-bottom.png` | **Plain map tap opens a pin popup: "Take me here · Anchor here · Drop marker".** Single change that collapses the discovery problem; also promote Go-to next to the mode grid; auto-expand the sheet when a ctx panel is invoked from the map. `map-core.js`, `routechoice.js`. |
| M2 | **Armed states are invisible**: Go-to armed and Add-waypoints armed leave the map pixel-identical; no banner, no way to finish/cancel from the map; inverse risk of littering waypoints later. (Once found, Go-to's armed state on tablet is already right — reuse it.) | Major — [first] M4, [ia] 13, [ergo] m5 | `ft-pp-20-goto-armed.png`, `p-route-addwp-armed.png` | One shared armed-state pattern: sticky banner "Tap the map to … — N added — ✓ Done / Cancel". Apply to go-to, add-waypoints, measure, set-alarm. |
| M3 | **Leaflet controls are 30–34px on every viewport** including the helm tablet at arm's length; 6px apart; mis-hit arms the measure tool silently. | Minor→Major — [ergo] m7, [flows] F-15, [robust] M8 | `rb-t-main.png` | Custom-sized controls ≥44px (≥48px on ≥1000px viewports). `map-core.js`, `style.css`. |
| M4 | **Boat-tap popup (best quick-anchor path, 2 taps) is undiscoverable.** | Minor — [flows] F-23 | `pp-53-boat-popup.png` | First-run coach mark or subtle pulse on the boat marker. |
| M5 | **Heading-up compass**: unlabeled 30px button, no state feedback, tile labels render sideways when rotated; a second orientation UI in settings, unlinked. | Major — [robust] M9 | `rb-p-maprot-tap1.png` | Toast on toggle ("Heading-up"/"North-up"), N/HDG text badge on the button; counter-rotate or drop labels when rotated. `maprotate.js`. |
| M6 | **Mode badge pill rides the boat marker and covers waypoints/water** right where the action is. | Minor — [ergo] m11 | `p11-route-2wp.png` | Move the mode badge to a fixed screen corner. |
| M7 | **Stale route pins persist after the route stops** across all clients while the panel says "No pending waypoints" — two sources of truth. | Minor — [ia] 26, [safety] 22 | `p-batt-crit.png`, `safety-t-drag-alarm.png` | Stopping/clearing a route visibly retires its pins (or restyles as "saved route"). `map-waypoints.js`. |
| M8 | **Dark chart: water and land near-identical near-black**; shipped depth overlays off by default — "where's the shallow?" unanswered. | Minor — [first] m14 | `ft-pp-01-first-open.png` | Default the Depth overlay on where chart data covers the area; raise land/water contrast in the dark theme. `map-depth.js`. |
| M9 | **Naming: Map vs Chart vs 🗺** for the same destination; "⌄ Map" collapse pill reads as a map menu. | Minor — [ia] 20, [flows] F-22 | `p-sheet-state1.png` | Pick "Chart" everywhere; chevron-only grip. |

### 2.6 Menus & settings

| # | Finding | Severity | Evidence | Fix |
|---|---|---|---|---|
| S1 | **Topbar chips clip/overlap at phone widths** — HDG renders as a single digit ("7" for 78° — misreadable as real data), DEPTH/BATT/LINK/ROUTE-progress entirely hidden behind an undiscoverable scroll (`scrollWidth 370 / clientWidth 150`); gone entirely at 360px. Found by all six experts. | Major — [ergo] M1, [flows] F-04, [ia] 3, [robust] M2, [safety] 8, [first] M11 | `pp-10-route-underway.png`, `rb-s-main.png`, `safety-home-portrait.png` | Responsive chip policy: drop chips whole, never clip mid-digit; icon/state-dot mode at narrow widths keeping GPS/link/battery health visible; move ROUTE progress into the sheet head; view switcher must never underlap the chip row. `index.html`, `style.css`, `core.js`. |
| S2 | **Daylight theme leaves the map (90% of pixels) dark**, topbar/patches of chrome stay dark too, and the toggle is 4 taps deep (Menu → Display → Appearance). The docs screenshot script manually flips the base layer — users won't know to. | **Blocker** [robust] B1 / Major [ergo] M6+m9+m10, [flows] F-27 | `rb-p-daylight-map-moving.png`, `p7-daylight-main.png` | Theme switch also swaps the base tile layer (dark ↔ light; respect an explicit user layer override — Open question 7); sweep all chrome under the theme variable; sun/moon quick toggle in the topbar or layers control. `settings.js`, `map-core.js`. |
| S3 | **Data & system buries user-critical items** (updates, backup, WiFi ranked 6th–8th) under four developer consoles ("NMEA / log console", "Command audit"). | Major — [ia] 15 | `t-settings-data.png` | Reorder user-first; visually separated "Advanced / troubleshooting" group. `menu.js`. |
| S4 | **"Notifications" means two different things on one page**; push opt-in buried under Sound & touch while the anchor-alarm card promises background watching; raw pip error (`pywebpush not installed...`) shown to fishermen. | Major — [flows] F-11, [ia] 18 | `tl-06-soundtouch.png`, `tl-07-push.png` | Rename "Notification sounds" / "Phone notifications (push)"; cross-link opt-in inside the anchor-alarm card; human error copy. `push.js`, `sounds.js`. |
| S5 | **Boat & tuning jargon at first contact** ("Reverse eff", "Auto-tune (PID)"); **joke presets (Titanic, Narco sub) double as tuning identifiers** — easter egg vs consequence is undecidable for a non-tech user. | Minor — [ia] 22+23, [first] p5 | `p-set-0-boat-tuning.png` | Advanced fields behind "Advanced boat numbers"; wizard + presets on top; honest preset names with jokes as subtitles (Open question 8). `boat.js`. |
| S6 | **Supervisor update/force uses native `confirm()`** — tiny buttons, dismissible, off-brand; the 409 underway interlock and wording are right. | Minor — [safety] 20 | `safety-t-supervisor-card.png`, `supervisor.js:333-379` | In-app modal with big buttons; hold-or-type for the force path. |
| S7 | **Settings drawer removes live status entirely** while a mode drives (subsumed by A10). | Minor — [ergo] m8 | `p3-settings.png` | Slim status/STOP strip atop the drawer when engaged. |
| S8 | **Role banner blots out the whole topbar on observer clients** for as long as they observe. | Minor — [ia] 27 | `p-role-banner-viewer.png` | Collapse after ~5s to a slim amber strip below the topbar; plainer wording ("Viewing only — tap to take over"). `roles.js`. |

### 2.7 PWA & robustness

| # | Finding | Severity | Evidence | Fix |
|---|---|---|---|---|
| R1 | **Phone landscape (844x390) gets the desktop layout and is effectively broken**: HUD strip + right dock collide (battery tile hidden behind the dock), DROP ANCHOR HERE renders behind the instruments strip / below the fold, wheel knob 280px below the fold with no scroll affordance, map reduced to a sliver, catch FAB half-buried — and the **helm view's tiles collapse into unusable slivers** exactly where big buttons matter. Found by all six experts. | Major (worst layout in the app) — [ergo] M7+M8, [first] M8, [flows] F-12, [ia] 2, [safety] 9, [robust] M6 | `l1-main.png`, `ft-pl-03-anchor-panel.png`, `safety-l-view-helm.png`, `rb-l-main.png` | Treat height ≤ ~480px as mobile-landscape: keep the sheet pattern (sheet right, map left, ≥40% map width), one-row compact HUD (or hidden while a panel is open), primary CTA pinned visible in short panels, landscape grid template for helm tiles, dock collapsible. `layout.js`, `style.css`, `views.js`. |
| R2 | **Steering-gauge ghost text (CMD/ANGLE/WRAP) bleeds through dock panels** on tablet and landscape — overlaps Track, Go-to, the anchor alarm slider. Five experts hit it; "looks broken, hurts trust". | Major — [ergo] M10, [flows] F-19, [ia] 29, [robust] M5, [first] m8 | `t1-main.png`, `rb-t-goto-accordion.png` | Clip/z-index the gauge inside its own layer; hide when a panel overlaps its rect. `steering.js`, `style.css`. |
| R3 | **Chart FABs (layers/compass/ruler) bleed onto the Helm view** on portrait and tablet — stray controls on a safety screen. | Minor — [first] m9, [ergo] M7 | `ft-pp-30-helm-view.png`, `t2-view-helm.png` | Hide map FABs outside chart view. `views.js`. |
| R4 | **Tablet chart: STOP scrolls away with the dock; no mode chip in the topbar; tiles are *smaller* than phone (64x55)** at arm's length. Helm view proves the fix. | Major — [robust] M4, [ia] 10+11, [safety] 14, [ergo] M9+M11 | `rb-t-dock-scrolled-bottom.png`, `t-main-idle.png` | Pin STOP (full-width red bar) outside the dock scroll region; add a mode chip (icon+word, active color) to the wide topbar; scale grid tiles ≥80x80 on ≥1000px; ≥13px tile labels everywhere. |
| R5 | **PWA fully installable but never offered**; on `http://` boat-AP origins the SW won't register and offline evaporates silently. | Major — [robust] M12 | manifest/SW audit | "Install app on your phone" menu card (native prompt + iOS steps); one-line warning when running without SW/secure context. `offline.js`, `menu.js`. |
| R6 | **Screen-wake behavior invisible** — automatic wake lock (good) but no status/toggle anywhere, and it silently dies on plain-HTTP installs. | Major — [robust] M11 | — | "Screen stays awake while engaged" line + toggle in Display/Safety; amber note when unavailable. `wakelock.js`. |
| R7 | **Offline reload never says "offline"** (dashes + blank grid only); reconnect is silent. | Minor — [robust] m1+m2 | `rb-p-offline-reload.png` | Offline banner on shell load + nudge to Offline maps; brief "Link restored" toast. |
| R8 | **Assorted honesty glitches**: HDG shows "360°" vs "0°" inconsistently; "Calibrating…" drives the boat while the badge says "Manual"; unlabeled range figure under BATTERY; RESUME looks enabled when nothing is paused; Work Area "Dwell" slider live-looking under Manual advance. | Minor/Polish — [robust] m3+m5, [first] m5, [ergo] P4, [safety] 17, [ia] 25+30 | various | Modulo HDG to 0–359; "Calibrating…" badge; label "~range"; disable inapplicable buttons; hide inactive sliders. |

### Where experts contradicted each other (and the calls made here)

1. **"One STOP per screen" [ia #7] vs "more STOP visibility" [safety #3].** Not actually in
   conflict once separated: the IA complaint is about three *differently-styled* STOPs at once;
   the safety complaint is about *zero* in the resting state. Call: exactly one STOP *style*
   (the red pill), exactly one instance visible, in **every** state — dedupe the cruise-block ■
   and rely on the persistent pill.
2. **Engagement friction.** [safety] wants hold-to-engage on RTL; [flows] measured and praised
   the app's tap economy and instant engagement. Call: keep single-tap for anchor/goto (timing
   at the spot matters), consider hold-to-engage only for RTL (autonomous travel to a
   possibly-stale point) — escalated to Open question 1.
3. **First-run default view.** [first] and [ergo] lean Helm-first on phones; [flows]/[ia]
   implicitly defend the chart (all their good tap counts start there). Escalated to Open
   question 2 — this is product taste.
4. **Peek-bar contents.** Five experts want values there, four want STOP, [safety] also wants
   MOB, [ia] wants the mode chip. They can't all fit in one 44px row. Call: raise the collapsed
   snap to two compact rows (values + mode/STOP); confirmed as Open question 5.
5. **Daylight → base layer.** [robust] says always swap; [ergo] says swap *unless the user
   explicitly overrode the layer*. Call: [ergo]'s version (respect explicit override) — Open
   question 7 confirms.
6. **APB.** [ia] notes an auto-engage-on-APB-feed setting already exists, so the tile could
   vanish entirely; [flows] suggests renaming/hiding. Call: move behind MORE now, auto-surface
   when a feed is detected — Open question 6 for full removal.

---

## 3. Action items — prioritized backlog

Sizes: **S** ≤ half a day, **M** 1–3 days, **L** ≥ a week. Work packages (WP) are coherent
SDD-able chunks; items within a WP share files and should ship together.

### P0 — must fix (safety floor + first-boot trust) — 8 items

**WP1 · Alarm & STOP surfaces** (`style.css`, `safety.js`, `mobile.js`, `index.html`)

1. **Fix alarm banner rendering at phone width.** Add `transform: none` to the
   `body.mobile #banner` override (`style.css:2093`, base rule `:552`); make mobile alarm
   banners full-width strips below the topbar; stack simultaneous alarms vertically,
   priority-ordered (MOB > drag/anchor > shallow > battery > link); add a 390px screenshot
   regression test. *(A2, A7)* — **M**
2. **Stale-data banner must never block controls.** `pointer-events: none` on
   `#stale-data-banner` and relocate it below the topbar, away from the sheet grip. *(A3)* — **S**
3. **Persistent STOP in every state.** Portrait: red STOP in the collapsed peek row; tablet
   chart: full-width red STOP bar pinned outside the dock scroll. Keep instant, no-confirm
   semantics. *(A1, R4-part)* — **M**
4. **Battery warn/alarm ladder.** Fixed SOC thresholds (amber <25%, red <10%) → banner +
   alert-history entry + chip state, independent of the RTL estimate; fix `rtl_recommended`
   to fire when usable range < distance-home (it stayed false at `range_m: 0`). Server +
   `safety.js`. *(A5)* — **M**

**WP2 · Glanceable truth** (`index.html`, `style.css`, `core.js`, `health.js`, `mobile.js`)

5. **Responsive topbar chips — never clip, never lie.** Drop chips whole at narrow widths
   (icon/state-dot mode), keep GPS/link/battery health always visible, never render partial
   digits, view switcher never underlaps the chip row. *(S1)* — **M**
6. **Collapsed peek shows values.** Raise the collapsed snap ~28–40px so the SOG/HDG/DEPTH/BATT
   numbers are on screen (creates the row for item 3). *(A9)* — **S**
7. **Link-loss consistency.** Drive `chip-conn` + status dot from the same staleness clock as
   the banner; grey/"--" stale numbers. *(A6)* — **S**

**WP3 · Sim honesty** (`demo.js`, `devices.js`)

8. **"SIMULATION — not your motor" pill** whenever mode=sim, tappable → hardware setup;
   first-run choice "Set up my real boat / Play with the simulator". *(O1)* — **S/M**

### P1 — should fix — 20 items

**WP4 · Alarm actions & modal safety** (`safety.js`, `alerts.js`, `menu.js`, `map-boat.js`)

9. **Actions on alarm banners**: "Recover" + "Stop" on drag/anchor alarms, mirroring
   `rtl-banner-go`/`mob-banner-clear`. *(A8)* — **S**
10. **Alarms + STOP survive modals**: alarm strip renders above the Menu/dialogs; compact ■ STOP
    in the modal header while a motor mode is active. *(A10, S7)* — **M**
11. **Honest degraded states**: shallow auto-stop → "…(STOPPED — shallow)" pill + Resume/Stop
    actions; GPS-lost → grey boat marker + "last fix Ns ago". *(A13, A12)* — **M**
12. **Persist alert history server-side** + hydrate on load; per-entry actions; Clear behind a
    confirm. *(A14)* — **M**
13. **MOB on the chart view** (peek bar or long-press FAB). *(A11)* — **S**
14. **Anchor re-drop safety**: engaged-state CTA ("⚓ HOLDING — MOVE / RELEASE"), re-drop
    relabeled "moves point", undo toast "Anchor moved 45 m → undo". *(F2, A15)* — **M**

**WP5 · Landscape & z-order** (`layout.js`, `style.css`, `views.js`, `steering.js`, `catch.js`)

15. **Mobile-landscape layout** (height ≤ ~480px): sheet-right/map-left, ≥40% map width,
    compact one-row HUD, primary CTA pinned in short panels, helm-grid landscape template,
    collapsible dock. *(R1)* — **L**
16. **Clip the steering-gauge ghost text** inside its own layer; hide when a panel overlaps.
    *(R2)* — **S**
17. **Hide chart FABs outside the chart view**; dock the catch FAB somewhere nothing overlaps,
    and show it on portrait regardless of the HUD pref. *(R3, F10)* — **S**

**WP6 · Map interaction** (`map-core.js`, `routechoice.js`, `route.js`, `map-anchor.js`)

18. **Map-tap pin popup: "Take me here · Anchor here · Drop marker".** The single
    highest-leverage discoverability fix in the review. *(M1)* — **M**
19. **Shared armed-state banner** ("Tap the map to … — N added — ✓ Done / Cancel") applied to
    Go-to, Add-waypoints, measure, Set-alarm; auto-expand the sheet when a map-invoked ctx
    panel opens below the fold. *(M2, F9)* — **M**
20. **Leaflet controls ≥44px** (≥48px on ≥1000px viewports). *(M3)* — **S**
21. **Anchor visibility**: minimum pixel radius for the watch circle; dist→anchor in the sheet
    head while anchored; collapsed sheet head turns red "DRAGGING — 66 m" during an alarm.
    *(F6, F9-part)* — **S**

**WP7 · Mode grid & naming** (`controls.js`, `views.js`, `index.html`, partials)

22. **Single mode→display-name map** used by badge/sheet/tiles; ANCHOR highlights for all
    `anchor_*`; casing/naming pass (Chart vs Map, "⌄ Map" → chevron). *(F3, M9, R8-part)* — **S**
23. **TROLL to the main grid, APB behind MORE** (auto-surface when a feed is detected);
    MORE tray scrolls into view on expand; subtitles on submodes; rename CONTOUR → "Depth line";
    spell out "MAN OVERBOARD". *(F4, F5)* — **M**
24. **Anchor style segmented control** ("Classic | Smart | Leif") with visible one-line plain
    descriptions; Hold-heading/Vectored behind "Advanced"; ⓘ opens a sheet (and, from P0 work,
    never engages). *(F1, A4 follow-up)* — **M**
25. **STOP isolation & dedupe**: collapse the cruise block to a one-line summary chip; one
    speed control per panel (knots); Pause/Resume single toggle; STOP not sandwiched between
    DRIFT and REMOTE (isolate or extra margin); START ROUTE disabled until ≥1 waypoint + toast.
    *(D3, D7, [ergo] M12)* — **M**

**WP8 · Manual driving** (`steerwheel.js`, `controls.js`)

26. **Wheel accepts drag start anywhere on the dial** (ease knob to finger; keep knob-grab for
    fine control). *(D1)* — **S**
27. **Thrust gain & release**: widen the radial band to the full dial radius; grace ramp for
    >40% jumps; release behavior per Open question 3; thrust slider gets "R | 0 | F" ticks and
    a 44px hit area. *(D2, D4)* — **M**

**WP9 · Daylight theme** (`settings.js`, `map-core.js`, `style.css`)

28. **Daylight = whole screen bright**: theme binds the base tile layer (unless the user
    explicitly overrode the layer), sweeps all chrome (topbar, floating controls), and gives
    STOP a solid red fill (≥4.5:1). Quick sun/moon toggle in the topbar/layers control.
    *(S2, A17)* — **M**

### P2 — polish / lower-frequency — 17 items

**WP10 · Onboarding & wizards** (`menu.js`, `wizard.js`, `hwwizard.js`)

29. First-run coach marks: grip ("swipe up for controls"), boat-marker pulse; "Get started"
    menu tile until the wizard completes once. *(O2, O3, M4)* — **M**
30. Wizard polish: rename "Boat setup"; plain-language calibration errors + explicit RETRY
    (raw errno behind "details"); "Calibrating…" map badge; lb-thrust presets; knots in
    results; portrait stepper labels; fix result-card clipping. *(O4, O6, R8-part)* — **M**
31. Hardware wizard: friendly device names over raw `/dev/serial/by-path`; Save & Restart
    gated on ≥1 probe or explicit "keep simulator". *(O5)* — **M**
32. View switcher ≥44px with labels on wide screens; first-run Helm suggestion on
    tablet-sized viewports. *(O7)* — **S**

**WP11 · Settings IA** (`menu.js`, `sounds.js`, `push.js`, `boat.js`, `supervisor.js`, `roles.js`)

33. Auto-expand category pages with ≤2 sections; kill echoed headings. *(O8)* — **S**
34. Data & system reordered user-first with a separated "Advanced / troubleshooting" group.
    *(S3)* — **S**
35. Rename "Notification sounds" / "Phone notifications (push)"; push opt-in cross-linked from
    the anchor-alarm card; human error copy for missing pywebpush. *(S4)* — **S**
36. Boat & tuning: advanced fields behind a disclosure; honest tuning-preset names with joke
    subtitles (per Open question 8); rename "Auto-tune (PID)". *(S5)* — **S**
37. Supervisor confirm() → in-app modal, hold-or-type for force-while-underway. *(S6)* — **S**
38. Observer role banner collapses to a slim strip after ~5s. *(S8)* — **S**

**WP12 · Status & map polish** (`alerts.js`, `hud.js`, `map-*.js`, `maprotate.js`)

39. Bell grey when empty, badge count + severity color when not; Clear demoted. *(A18)* — **S**
40. Master status dot = worst-of health, or removed. *(A19)* — **S**
41. DIST→ANCHOR and "Weigh anchor" hidden unless anchored; stale route pins retired on stop.
    *(F7, M7)* — **S**
42. Mode badge to a fixed screen corner. *(M6)* — **S**
43. Heading-up: toast on toggle, N/HDG badge, label handling when rotated; link the two
    orientation UIs. *(M5)* — **S**
44. Dark-theme land/water contrast + depth overlay on by default where charted; anchor-alarm
    map marker tappable → "Clear alarm". *(M8, F11)* — **M**
45. Honesty sweep: HDG modulo 0–359; "~range" label under BATTERY; RESUME disabled when not
    paused; Work Area dwell slider hidden under Manual advance; jog-pad labels + "1 m per tap";
    RTL visually separated from SET LAUNCH; volume-label wrap; attribution z-order; empty
    instruments cell; wheel hint → one line + ⓘ. *(R8, F8, A16-part, D5)* — **M**

**WP13 · PWA** (`offline.js`, `wakelock.js`, `menu.js`)

46. "Install app on your phone" card (native prompt + iOS steps); warning when SW/secure
    context unavailable ("Offline mode unavailable on this connection"). *(R5)* — **M**
47. Wake-lock visibility ("Screen stays awake while engaged" + toggle + amber note when
    unavailable); offline banner on shell load; "Link restored" toast. *(R6, R7)* — **S**

**Counts: P0 = 8 · P1 = 20 · P2 = 17 · total 45 items** (covering ~140 raw expert findings
after dedup; every raw finding maps to an item above or was explicitly kept as-is in the
"what works" lists).

---

## 4. Open questions for the owner

Max 8, each with the tradeoff. These are the calls the experts disagreed on or that need
product taste.

1. **Engagement friction:** should RTL (and other autonomous "drive away" actions) require a
   600ms hold-to-engage, or stay single-tap? Tradeoff: hold-to-engage prevents mis-taps that
   drive the boat toward a possibly-stale point (safety expert), but breaks the app's
   best-in-class tap economy and adds a gesture that can fail with wet hands (flows expert
   praised instant engagement).
2. **First-run default view on phones:** Helm (big tiles, big STOP — best for novices, per
   first-time + ergonomics) or Chart (map context, praised 3-tap anchor, per flows/IA)?
   Tradeoff: Helm maximizes first-session safety and discoverability; Chart is what regulars
   live in, and demoting it adds a switching step for everyone forever.
3. **Manual wheel release behavior:** should thrust snap to zero on release by default
   (dead-man style, with a "hold thrust" option for trolling), or keep the current latch (a
   slip leaves the boat at 100%)? Tradeoff: snap-to-zero is safer but breaks set-and-forget
   trolling; the latch is the current behavior users may rely on.
4. **"Leif":** keep the pet name (with a proper tap-to-read description), or rename to a
   descriptive label like "Experimental hold (beta)"? Tradeoff: personality and brand vs. four
   experts flagging it as opaque jargon in the safety path.
5. **Collapsed peek bar real estate (~44–72px):** the experts want values (5 experts), STOP
   (4), the mode chip, and MOB (safety) in the same strip. OK to raise the collapsed snap to
   two compact rows (values row + mode/STOP row), or must the peek stay one row (then: which
   two win)?
6. **APB mode:** hide entirely unless an APB feed is configured/detected (IA), or keep it
   visible behind MORE so chartplotter owners can find it? Tradeoff: auto-hide is cleanest for
   the fishing persona but makes the feature invisible to the integrator who just wired up
   their plotter.
7. **Daylight theme and the base map:** when the user flips to Daylight, should the app switch
   the map to the Light tiles even if the user had explicitly chosen the dark layer earlier?
   Tradeoff: full-screen brightness in glare (the entire point of the theme) vs. respecting an
   explicit user layer choice.
8. **Joke boat presets (Titanic, Narco sub, Kraken) as tuning identifiers:** keep the humor
   as-is, rename presets to honest labels (Small/Medium/Heavy) with jokes as flavor subtitles,
   or drop them? Tradeoff: charm vs. a non-tech user unable to tell easter egg from
   consequential tuning choice (and "Narco sub" may land badly in some markets).
