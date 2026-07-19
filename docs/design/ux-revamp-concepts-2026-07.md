# UX Revamp — Concept Round, July 2026 (moderator synthesis)

**Inputs:** four design concepts with 16 rendered screens
(`.superpowers/ux/concepts/{instrument,mapfirst,twosurface,evolution}/`), three adversarial
critiques (`.superpowers/ux/debate-{fisherman,pragmatist,safety}.md`), and the round-1
expert review (`docs/design/ux-review-2026-07.md`, 45-item backlog, WP1–WP13).

**Owner decisions (binding, honored by all four concepts):** two-row peek bar (values +
STOP/mode/MOB always visible); wheel thrust snaps to zero on release, with a HOLD toggle;
Helm view suggested on first run only, then remembered; 600 ms hold-to-engage for
drive-away actions, anchor stays single-tap; Leif keeps its name with tap-to-read ⓘ;
Follow-APB auto-hides until a feed is detected; Daylight theme also switches map tiles;
boat icons purely visual.

A short gallery of the best screens: `.superpowers/ux/gallery.md`.

---

## 1. The debate, honestly summarized

### Scores

| Concept | Fisherman | Pragmatist | Safety-UX | Total |
|---|---|---|---|---|
| **Evolution** (polish the app we have) | 7 | **8** | **8** | **23** |
| **Instrument** (marine cluster) | **8** | 7 | **8** | **23** |
| **Two-Surface** (phone remote + tablet helm) | 7 | 4 | 7 | 18 |
| **Mapfirst** (the map is the app) | 6 | 4 | 6 | 16 |

Evolution and Instrument tie on points, but the tie is not a coin flip: **all three
critics' "what the hybrid should steal" sections independently name Evolution as the
skeleton** and Instrument as the source of grafts. Nobody proposed building on Instrument's
chassis; two of three explicitly costed its full fascia as a reskin the P0s don't need.

### Where the critics agreed (strongly)

- **The safety chassis is already decided and concept-independent.** The pragmatist's key
  structural observation, uncontested: all four concepts converge on the identical safety
  floor — two-row peek bar, full-width priority-stacked alarm strips with inline actions,
  amber SIMULATION pill, 600 ms hold-rings on drive-away, solid `#D21F3C` STOP. That is
  the owner's decisions echoed back, it kills every P0, and it is ~4 work packages
  (WP1–WP3 + parts of WP4) on the existing files. Each concept must justify what it costs
  *beyond* that chassis — and three of the four mostly couldn't.
- **Every concept punted phone landscape (R1).** The review's only L-sized item, found by
  all six round-1 experts, was hand-waved by all four designers ("falls out of the layer
  model" ×3; Two-Surface "declines to do landscape"). All three critics flagged it; the
  pragmatist called it the unpriced line item. The hybrid must mock it before building it.
- **Mapfirst's pin popup is the best single card of the contest** (distance · ETA · depth
  at the tap point, "HOLD ½s" vs "TAP" printed per row). All three critics said steal it
  verbatim; the pragmatist noted it's already priced as WP6 item 18.
- **Two-Surface's copywriting should be law** ("The boat left the watch circle 52 s ago
  and is drifting SW toward shore"; "Style only changes *how* Recover holds; nothing
  engages until you tap Recover"; "Let go → power drops to 0"). All three: hire that
  writer, reject that architecture.
- **Mapfirst's paradigm is rejected** by all three: the tap-the-water bet fails wet hands
  (fisherman), re-creates the one-undiscovered-gesture bug as a philosophy and costs a
  shell rewrite plus a perf-hostile wheel-over-map (pragmatist), and replaces fixed
  panic-time controls with spatial hunting (safety).
- **Two-Surface's phone-chart amputation is disqualifying** for the majority device: the
  phone-only fisherman loses take-me-here and spatial verification of where RECOVER will
  drive the boat (fisherman: "the phone IS the plotter"; safety: "removes a safety
  instrument"; pragmatist: deletes the app's best measured flows plus a permanent 2×
  layout tax).
- **Evolution's inherited debts are real:** the unlabeled emoji view switcher survives
  into the safety zone (all three critics), MOB is a weak red-outline pill next to solid-red
  STOP (fisherman + safety), and its numerals are a size down from Instrument's
  glare-grade type (fisherman + pragmatist).
- **Daylight was proven by exactly one mock** (Mapfirst's tablet); everyone else asserted
  it in prose. Same lesson as landscape: unmocked = unproven.

### Where the critics clashed

- **STOP position: corner vs center.** The fisherman prefers Instrument's corner STOP
  ("corners are easier blind-thumb targets than centers") and dinged Two-Surface's
  mid-row STOP. The safety critic's hard rule is *consistency* — one style, one position,
  never side-swapped — and flagged Instrument for swapping STOP/MOB sides between its own
  screens (chart: STOP left; helm: STOP right) as "an automotive red flag."
  **Moderator call:** both are satisfiable — STOP bottom-LEFT corner, mode chip center,
  MOB (filled orange, spelled MAN OVERBOARD at least once per surface) bottom-right, on
  every screen, view, modal, and device, never swapped. This adjusts Evolution's mock
  (which had mode-left/STOP-center) and fixes Instrument's swap.
- **Battery: volts vs percent.** Fisherman: volts-first ("every boat guy runs on volts");
  safety: never voltage-only where a fisherman glances ("cannot map voltage to am-I-
  getting-home"). **Call:** Evolution's treatment was the only one both accepted — % AND
  volts AND color in the peek cell ("86% / 12.4 V"), thresholds per A5.
- **Two-Surface's overall worth.** Fisherman and safety score it 7 (best copy, best
  safety detail per pixel, only honest R1 answer); the pragmatist scores the architecture
  4 (2× maintenance tax, no incremental ship point). Resolved by separating content from
  architecture: the content ships, the split dies.
- **How much Instrument fascia to buy.** Fisherman: the bezelled cluster look *is* trust
  ("nobody asks what app is that — they ask what plotter is that"). Pragmatist: the
  bezels are paint, a 2458-line CSS rewrite that starves safety work. Safety: only the
  *laws* matter (strips/chrome-bands/color discipline). **Call:** adopt the laws and the
  type ramp now, defer the paint (see decision menu, option B).

---

## 2. Per-concept verdicts

### Evolution — "radical polish, familiar bones" — WINNER (chassis)

The review's own backlog, drawn. The pragmatist could map every pixel of its four mocks
to numbered round-1 items with named files, most already sized S/M; migration is a
sequence of independently shippable PRs on live files with the shell contract and the
~2000-test suite intact. The safety critic judged `screen3-anchor-alarm.png` **the single
best stress screen of all sixteen mocks** — six honest surfaces (topbar ALARM chip, bell
badge, stacked drag+battery strips with inline actions, on-map pill and drift tag, red
DRIFT/RING peek cell, amber battery cell) agreeing at once, and zero retraining is itself
a safety property. Its sins — emoji view switcher, weak outline MOB, two STOP styles in
its own alarm mock, smallest numerals, no daylight/landscape mock — are real but
fixable-in-place, not architectural.

**Best screens:** `screen3-anchor-alarm.png` (best stress screen of the round),
`screen4-tablet.png` (labeled jog pad, pinned STOP), `screen2-helm.png` (deadman taught on
the wheel, Anchor-here/Return on the helm page).

### Instrument — "marine instrument cluster" — RUNNER-UP (adopt its laws, defer its paint)

The best engineering ideas of the round: the three-primitive grammar (tile / strip /
chrome bar) with "status strips are pointer-transparent by law" and "modals open between
the chrome bands" — which kills A3/A10 structurally rather than by discipline — plus the
strictest color law and the glare-grade condensed type ramp. Its
`04-tablet-helm.png` was judged the most convincing tablet screen of all sixteen by two
critics (it is R4's fix list verbatim). Rejected as chassis because the full bezelled
fascia is a multi-week CSS rewrite that the P0s don't need, it swaps STOP/MOB sides
between its own screens, shows volts-only battery in the peek, and mocked neither
daylight nor landscape.

**Best screens:** `04-tablet-helm.png` (best tablet of the round), `03-anchor-alarm.png`
(best pure alarm strip + peek-echo trick), `02-helm-phone.png` (cluster glanceability,
TROLL promoted), `01-chart-main.png` (topbar-as-health-only law).

### Two-Surface — "fishing remote + helm station" — REJECTED architecture, HARVEST the content

The best words and the most safety detail per pixel anywhere in the exercise:
`phone-anchor-alarm.png` was called the best single safety screen in the contest
(fisherman) — the alarm sentence with cause + elapsed + consequence direction, the
Classic/Smart/Leif segmented control that retires A4/F1 with one printed sentence,
"Re-drop moves the point 38 m · undo for 10 s", the whole-surface red tint. The Fish tab
(LOG CATCH, bottom type, depth trend) is content nobody else had. But the two-product
split is a permanent 2× layout tax on a one-maintainer codebase, has no incremental ship
point, and amputates the phone chart — deleting take-me-here and spatial verification for
the majority device that has no helm tablet.

**Best screens:** `phone-anchor-alarm.png` (best copy + segmented control),
`phone-fish.png` (LOG CATCH, depth trend), `phone-drive.png` (best manual-driving screen:
outcome-named steer modes, deadman in words), `tablet-helm.png` (cruise-as-one-line,
Recent event log).

### Mapfirst — "the map is the app" — REJECTED paradigm, STEAL three parts

The prettiest work and the only concept that *proved* daylight (`tablet-helm.png`), plus
the best single card (the pin popup) and the best deadman labeling ("RELEASE → THRUST 0"
printed inside the dial). But the core bet — everything is a tap on the water — is the
review's O2 discoverability failure elevated to a philosophy, precision-gesture-hostile to
wet hands, structurally weak against mis-taps (single-tap "Anchor here" on every stray
water tap while underway), and its wheel-over-live-map is the round's worst perf/input
idea. It also deletes the praised Helm view and the entire existing shell contract.

**Best screens:** `chart-main.png` (the pin popup — best card of the round),
`tablet-helm.png` (the round's only daylight proof; best progress sentence),
`anchor-alarm.png` (best-anatomy alarm strip: cause + timer + RECOVER + STOP on board,
battery strip stacked beneath), `helm-drive.png` (deadman labeling).

---

## 3. Moderator recommendation — "Evolution+" (a specified hybrid)

**Chassis: Evolution.** Ship on the existing shell — same chart + sheet + view carousel,
same files, same test suite — as ordered, PR-sized work packages. Graft exactly the
following, none of which forces a paradigm:

**From Instrument (laws + type, not paint):**
1. The chrome-band grammar as CSS architecture: status strips are in-flow, full-width,
   pointer-transparent; topbar and peek bar are fixed and z-topmost; modals open *between*
   the bands. A3/A10 die by stacking rule, enforced in review.
2. The color law: solid red = STOP/alarm only; one cyan focus per screen; amber =
   caution/SIM; green = confirmed-healthy only; no translucent safety chrome in any theme.
3. The type ramp: condensed tabular numerals ≥26 px in the peek values row (Evolution's
   were a size too small for a bouncing console).
4. `04-tablet-helm.png` as the tablet Helm/Gauges target, and the peek-echo trick (mode
   chip + relevant value cell flip red with an alarm; SOG cell becomes ANCHOR/DRIFT when
   anchored/dragging).
   **Skipped:** the bezel/gradient fascia — paint can come later (Option B below).

**From Mapfirst:**
5. The pin popup verbatim: distance · ETA at cruise · depth-at-point; "HOLD 0.6 s" ring
   printed on "Take me here"; "Anchor here" single-tap — **but gated to hold when
   SOG > ~0.5 kn** (safety's amendment: the owner's single-tap-anchor decision was about
   the deliberate drop moment, not every stray water tap while underway).
6. The state-sentence mode chip as the single mode→display map ("Anchor — DRAGGING ·
   recover?"), killing F3 everywhere.
7. The sun/moon quick toggle in the map-control stack; Mapfirst's daylight tablet as the
   daylight template (light tiles + white cards + solid-red STOP).
8. Alarm-strip anatomy: cause + elapsed timer, RECOVER (hold-ring) + STOP *on the strip*,
   plus Instrument's time-boxed SILENCE (2 MIN).
   **Skipped:** overlay wheel, topbar deletion, tappable contours (revisit contours only
   after cmapper depth data ships).

**From Two-Surface (all copy + panel content):**
9. The copy standard, adopted as string-review law: alarm subtitles carry cause +
   elapsed + consequence direction; "motor holds at anchor point"; "Let go → power drops
   to 0"; "Re-drop here — moves the point N m · undo for 10 s".
10. The Classic | Smart | Leif segmented control with the printed sentence "Style only
    changes *how* Recover holds; nothing engages until you tap Recover" (A4/F1 dead at
    the copy level); ⓘ outside the control per the owner's Leif decision.
11. Outcome-named steer modes with subtitles ("Off the bow / Compass / Straight line");
    the LOG CATCH bar ("saves this spot · 4.2 m · 18:42") and depth-trend line inside the
    fishing panel — *on top of* the chart, not instead of it; cruise as one summary line
    shared by guided modes; the tablet "Recent" event log; full task-area red tint while
    a drag alarm is live.
    **Skipped:** the two-product split, the phone-chart amputation, portrait-lock.

**Hard rules (from the safety critique's failure list, adopted as acceptance criteria):**
- STOP: one style (solid red pill), one position (bottom-left corner of the peek row /
  pinned bar), every screen/view/modal/device, never side-swapped; repeated on alarm
  strips in the same style.
- MOB: filled orange, spelled "MAN OVERBOARD" at least once per surface, ≥56 px, always
  bottom-right.
- No color emoji, no unlabeled icons in safety chrome — the view switcher gets labels
  (O7 finally dies).
- Battery is never voltage-only where a fisherman glances: % + V + color.
- SIM pill says the full sentence ("SIMULATION — not your motor") on every device until
  first acknowledgment.
- The "screen3 checklist" as an alarm acceptance test: topbar worst-of chip, strip, mode
  chip, peek cell, and map tag must flip together, or the build fails.
- **Nothing merges until three missing mocks exist in pixels:** phone landscape
  (rod-holder), phone daylight, and a daylight alarm. Four concepts hand-waved R1; the
  hybrid does not get to.

### Migration onto the existing codebase

No shell rewrite. The work lands as CSS + markup + copy on the live files (`style.css`,
`mobile.js`, `safety.js`, `health.js`, `map-core.js`, `views.js`, partials); the served
shell contract and DOM ids survive, so the ~2000-test suite is untouched apart from a
handful of id assertions in `test_shell_partials.py`, and screenshot regression slots into
the existing `shot.py`/`uitest.py` harness. Build order (each step shippable alone, per
the pragmatist): WP1 alarm strips + peek STOP (the `style.css:2093` `transform:none` fix
is the opening one-liner) → two-row peek with condensed numerals → SIM pill (`demo.js`,
indicator already exists) → pin popup → anchor-panel copy/segmented control → tablet
pinned-STOP layout → daylight sweep → landscape (mocked first). Every P0 is dead within
the first three steps.

### What this means for the round-1 work packages

| WP | Fate | Change |
|---|---|---|
| WP1 alarm & STOP surfaces | **Survives** | Executed as drawn in `evolution/screen3` + Instrument strip grammar; add STOP-on-strip (same style) and time-boxed SILENCE. |
| WP2 glanceable truth | **Survives, upgraded** | Topbar becomes health-only whole-chips (numbers live in the peek); peek numerals adopt Instrument's condensed ≥26 px ramp; peek-echo/context cell added. |
| WP3 sim honesty | **Survives** | Full-sentence pill on every device until acknowledged. |
| WP4 alarm actions & modal safety | **Survives, simplified** | "Modals open between the bands" stacking law replaces per-modal fixes for item 10. |
| WP5 landscape & z-order | **Survives, re-scoped** | R1 must be *mocked before built* (all four concepts punted); the chrome-band grammar defines the reflow (peek → edge rail). Still the only L. |
| WP6 map interaction | **Survives, upgraded** | Item 18 becomes Mapfirst's pin popup verbatim + SOG-gated "Anchor here" hold. |
| WP7 mode grid & naming | **Survives, expanded** | Absorbs the Two-Surface copy pass: segmented Classic\|Smart\|Leif + printed safety sentence, outcome steer names, MAN OVERBOARD spelled, cruise-as-one-line. |
| WP8 manual driving | **Survives** | Owner decisions baked (snap-to-zero + HOLD), taught on the control per Mapfirst's labeling; wheel stays a panel, never a map overlay. |
| WP9 daylight | **Survives, upgraded** | Mapfirst's daylight tablet is the template; sun/moon toggle added; daylight-phone + daylight-alarm mocks required before merge. |
| WP10–13 (onboarding, settings IA, polish, PWA) | **Survive unchanged** | Except item 32: the view switcher gets text labels on all sizes — the emoji riddle dies. |
| **New (small) items** | — | LOG CATCH bar + depth trend in the fishing panel (extends F10); tablet Recent log (S); task-area red tint during alarm (S); mock-first gate for R1/S2. |
| **Dies** | — | Nothing from round 1. What dies is concept-side: Instrument's full bezel fascia (deferred, not deleted), Mapfirst's paradigm (overlay wheel, topbar deletion, tappable contours), Two-Surface's two-product split. |

---

## 4. Decision menu for the owner

**Option A — Evolution+ hybrid (recommended).** Ship the round-1 backlog on the existing
shell with the grafts specified above; landscape and daylight mocked before those WPs
build. *Tradeoff:* lowest risk, zero retraining, every P0 dead within the first three
steps, tests intact — but the app keeps its familiar look; least visual "wow", and the
Garmin-grade trust cosmetics the fisherman scored highest are deferred.

**Option B — Evolution+ now, Instrument fascia as phase 2.** Everything in A; after
P0/P1 land, a dedicated visual pass adopts the cluster look (bezels, black chrome,
machined icons) tile by tile. *Tradeoff:* ends at the most trustworthy-looking glass of
the round without ever blocking safety work — but it's a second visual churn for users,
roughly 2–4 extra weeks of CSS across 9 panels/wizards/modals, and phase 2s have a way of
not happening.

**Option C — Full Instrument now.** Adopt the cluster as chassis and fascia in one
program. *Tradeoff:* the strongest dock-test trust and glare legibility from day one
(fisherman's top score) — but a 2458-line CSS rewrite plus a restyle pass over all 63
modules lands *before* the safety floor ships, its own mocks show STOP side-swapping and
volts-only battery to fix, and neither daylight nor landscape was proven. Highest cost,
slowest P0 kill.

**Option D — A (or B) + Two-Surface remote as a later optional pack.** Ship the hybrid;
revisit the fishing-remote phone layout later as an opt-in "companion mode" (per the
paused pack-framework direction), keeping the full app as default. *Tradeoff:* preserves
the genuinely great remote concept for the tablet-owning minority — but accepts a
permanent second layout to maintain if ever built, an unanswered two-device authority
question, and it must never remove the chart from any surface that can issue a drive-away
command.
