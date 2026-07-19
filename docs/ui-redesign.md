# UI Rehaul — command menu, views, reachability

> **Status: SHIPPED (Evolution+, July 2026).** This document was the design spec;
> it has been fully executed across UX tasks 1–6 on branch `dev/adoption-pack`.
> For the authoritative post-ship review (findings, screenshots, expert-tag
> breakdown) see [docs/design/ux-review-2026-07.md](design/ux-review-2026-07.md).
> For the pre-ship concepts and IA rationale see
> [docs/design/ux-revamp-concepts-2026-07.md](design/ux-revamp-concepts-2026-07.md).
> The shipped JS/CSS lives under `src/vanchor/ui/static/` (menu.js, views.js,
> mobile.js, layout.js, safety.js, etc.); the spec below is preserved as design
> rationale.

---

Goal: the app is a control surface used **on a boat** — wet hands, glare, motion,
one-handed. Nothing wrong with the map/visualisation; the problem is *reach*.
The right-side settings drawer is cramped and its categories are muddled. This
rehaul makes controls big and central, fixes the information architecture, adds
specialised **views**, and works **equally well on phone and on a landscape
tablet/PC/touchscreen**.

Hard constraints:
- **Preserve every existing element id and JS handler.** The `<details class="card"
  id="…">` panels keep their ids + internal markup so all module JS keeps working;
  we change the *container* (drawer → centred modal) and *grouping*, not the cards.
- **Mobile == landscape.** Every surface must be laid out and tested at both a
  narrow phone (≈390 px portrait) and a wide landscape (≈1280 px) — big touch
  targets (min 56 px), no horizontal scroll, STOP always reachable.
- **STOP is sacred.** STOP controls are never covered, dimmed, or made to scroll.

## 1. Command menu (replaces the right drawer)

The ☰ button opens a **centred command modal**, not a side drawer.

- **Home screen:** a grid of large **category tiles** (icon + label + one-line
  hint), each ≥ 96 px tall, thumb-sized. Tapping a tile slides to that category's
  **sub-panel** (the existing cards, stacked, with big summaries/buttons). A back
  chevron returns to the tile grid; the modal title shows the current category.
- **Layout:** on phone the modal is a full-bleed sheet (top app-bar with
  title + back + close). On landscape it's a centred panel (~min(920px, 92vw),
  ~86vh) over a dimmed backdrop. Tile grid: 2 cols phone, 3–4 cols landscape.
- **Behaviour:** open/close via ☰ and Esc/backdrop-tap; deep-link a category
  (menu remembers last category is *not* required). Reuse `id="settings"` as the
  modal root so `settings.js`/`map-boat.js` selectors still resolve; add a
  `.command-menu` class and an inner `.cm-home` (tiles) + `.cm-panel` (active
  category) structure. Cards move under new category groups but keep their ids.
- The mode rail and dock panels stay where they are (they are the primary
  control surface, not "menu"), but see §4 for the Helm view.

## 2. Information architecture (re-categorised)

New categories (tiles), with the cards that move into each:

1. **Boat & tuning** — Boat profiles/specs · GPS calibration · Auto-tune (PID).
   *(calibration + tuning belong with the boat, not scattered.)*
2. **Map & charts** — Map markers · Depth map · Offline maps.
   *(REMOVE the time-series "Charts" card from here — see Data.)*
3. **Display** — Appearance (theme) · HUD overlay · Layout & profiles · Views.
4. **Safety** — Safety & power (governor, RTL, MOB, no-go zones, min-depth).
5. **Fishing** — Catch logger · Trips.
6. **Devices** — Devices & hardware.
7. **Data & system** — Live data · **Time-series graphs** (the old "Charts",
   renamed to end the nautical-chart name collision) · Debug recorder ·
   NMEA/log console · Command audit · Backup & restore.
8. **Simulator** — Simulator + weather/environment.

Rationale for the moves: the biggest real defect is "Charts" (uPlot time-series)
living under **Map & charts** next to nautical charting — renamed to
**Time-series graphs** and moved to **Data & system**. Calibration/tuning
consolidated under **Boat & tuning**. Everything else stays but is reachable
behind one clear tile instead of a long scroll.

## 3. Views (specialised, URL-addressable, customisable)

A **view** is a preset arrangement of the same live widgets — some views drop the
chart entirely. Views are reachable at **`/view/<name>`** (server serves the same
shell; `views.js` reads `location.pathname` and sets `body[data-view=<name>]`;
CSS defines each view's layout). The SW serves the shell for `/view/*`
navigations so views work offline.

Preset views:
- **chart** (default, `/` and `/view/chart`) — the full rehauled UI: map + HUD +
  mode rail + menu.
- **helm** (`/view/helm`) — no chart. A big **mode button grid**, a dominant
  **STOP**, quick actions (Anchor here · RTL · MOB), and a compact
  heading/speed/ depth strip. For fast reach standing at the helm.
- **instruments** (`/view/instruments`) — no chart. A large arranged HUD
  (speed, heading rose, depth, battery, dist-to-anchor) filling the screen for a
  mounted glance display.
- **manual** (`/view/manual`) — no chart. Big manual **thrust + steering**
  controls + STOP + heading/speed readout, for hand-driving.

Customisation: a **Views** card (under Display) lists views, lets you switch, and
toggles which HUD widgets / quick-actions each view shows; persisted server-side
via `PUT /api/prefs` (from roadmap #23) so it survives and syncs across devices.
Full drag-arrange is a follow-up; ship the toggles + presets now. A small
**view switcher** is reachable (topbar chip or a menu tile) and each view has an
always-present way back to the full chart view.

Every view keeps STOP and a link/stale banner. Every view must pass mobile +
landscape.

## 4. Theme & colour

Keep the "tactical HUD" identity (Chakra Petch / JetBrains Mono, cyan/teal/violet
on near-black) but improve:
- **Legibility:** raise `--muted` contrast; ensure text meets ~AA on glass;
  slightly larger base control sizing for gloved/wet use.
- **Daylight theme:** add a high-contrast **Daylight** theme (toggle in
  Appearance, persisted) — a bright, high-contrast palette for direct sun where
  the dark theme washes out. This is a genuine marine need, not just cosmetics.
- Consistent accent usage: primary action = accent cyan; OK/active = teal;
  warning = amber; STOP/alarm = coral (unchanged — safety colour is stable).

## 5. Execution

1. **Command menu + recategorisation + Appearance/Daylight theme** (index.html
   menu restructure, style.css modal + theme tokens, new `menu.js`, `settings.js`).
2. **Views system + URL routing + view switcher + mobile/landscape parity**
   (`views.js`, `server.py` `/view/<name>`, `sw.js` navigation, index.html view
   containers, style.css per-view layouts).
3. **Screenshot review** at 390 px and 1280 px, iterate.

Verification: `e2e_smoke.py` (no console errors) + Playwright screenshots at both
viewports for the menu home, one category, and each view; `node --check` all JS.
