# Chart-Grade Depth-Map / Bathymetry Overlay — Design Reference

Research reference for rendering a depth-map / bathymetry overlay on an HTML5 canvas
(`src/vanchor/ui/static/map.js`). Surveys how the major marine brands render depth,
what the public likes and dislikes, and distils a concrete spec.

Status: research only. Hex values from vendor products are best-estimates from
screenshots/marketing unless the source publishes a table (S-52, GEBCO/cmocean,
OpenSeaMap reimplementation do publish exact values — those are marked **exact**).

---

## 1. How the brands render depth

There are two opposing colour conventions in the wild. Get this decision right first:

- **Navigational / chart convention** — *shallow = dark/saturated, deep = light/white.*
  Used by IHO S-52 (NOAA ENC), Raymarine LightHouse, Humminbird LakeMaster,
  OpenSeaMap. Deliberate: shallow water = danger = stands out; deep/safe water
  recedes to white. Almost always **flat filled bands** with one bold **safety contour**.
- **Scientific / ocean convention** — *shallow = light, deep = dark navy.*
  Used by GEBCO, EMODnet, cmocean. **Smooth gradient + hillshade**, no single
  safety line. Looks great, reads as "terrain", not as "where can I run aground".

Anglers overwhelmingly prefer the navigational convention (see §8).

### Navionics (SonarChart / depth shading + safety depth)

- **Ramp (shallow→deep):** monochrome blue → white. Saturated blue shallow
  `#3F8FD0`/`#5BA3D9` → mid `#9FC9E8` → pale `#D7ECF8` → `#FFFFFF` deep. Lighter = deeper.
- **Bands vs gradient:** *near-smooth* because built from very dense (1-ft / 1-in)
  contour data; the underlying depth-area shading is discrete filled bands.
- **Isobaths:** HD contours ~1 ft (also 2 ft / 0.5 m sets), thin blue/grey; soundings
  labelled as small dark numerals.
- **Relief/hillshade:** Yes — "Relief Shading" / "SonarChart Shading" adds directional
  shadow for a 3D bottom look.
- **Transparency:** semi-transparent overlay; blends over vector basemap and satellite.
- **Safety/shallow highlight:** user "Safety Depth"; water shallower is shaded and the
  threshold is marked with a **red dotted** pattern.
- Sources: <https://www.navionics.com/charts/features/advanced-map-options>,
  <https://support.garmin.com/en-US/?faq=jxNs2pWnYL0EirWSjhCeS7>,
  <https://www.sportfishingmag.com/navionics-sonarchart-shading/>

### Garmin (Quickdraw Contours, LakeVü/BlueChart g3, Navionics+)

Two distinct systems:

- **Quickdraw / user depth-range shading (angler, multi-hue):** discrete bands, e.g.
  0–5 ft RED `#E53935`, 5–10 ft ORANGE `#FB8C00`, 10–15 ft YELLOW `#FDD835`,
  15–25 ft GREEN `#43A047`. Up to 10 bands (≈5 recommended). User-set contour colour.
- **LakeVü/BlueChart g3 (chart-grade):** traditional blue→white discrete bands **plus
  high-resolution relief shading** (true directional hillshade under the colour ramp).
- **Shallow shading:** water shallower than safe depth shaded **RED** (Auto-Guidance
  convention); deeper-than-safe left clear/white.
- Sources: <https://www8.garmin.com/manuals/webhelp/gpsmap_touch/EN-US/GUID-97DB8A8E-2D41-49B2-A98C-B941BDCAAEDA.html>,
  <https://www.garmin.com/en-US/newsroom/press-release/marine/2019-garmin-adds-high-resolution-relief-shading-to-its-premium-bluechart-g3-vision-and-lakev-g3-ultra-cartography/>

### Lowrance Genesis Live / C-MAP Social Map

- **Ramp:** conventional white→blue. Shallow `#FFFFFF`/`#EAF6FF` → mid
  `#9CDCF0`→`#24B0F0` → deep `#3A6FA0`→`#1F4E79`.
- **Bands:** discrete — user picks **up to 10 custom depth ranges from 16 colours**.
- **Isobaths:** selectable interval ½ ft / 1 ft / 2 ft; thin dark lines; independent
  **contour transparency** control.
- **Relief:** none in Genesis Live (flat banded fill). Relief is the separate C-MAP Reveal layer.
- **Safety shading:** enter a depth; shallower is tinted a warning colour.
- Sources: <https://www.genesismaps.com/Features/Shading>,
  <https://www.simrad-yachting.com/news/blog-posts/how-to-customize-colours-on-c-map-charts-with-custom-depth-shading/>

### C-MAP Reveal / Reveal X (relief shading)

- **Ramp:** blue-green bathymetric. Shallow aqua/turquoise `#C9EFE6`/`#9CDCF0` →
  `#5FB0C8`→`#2E7FB0` → dark navy `#1F4E79`/`#14365A`.
- **Bands vs gradient:** Reveal relief = **smooth/continuous** (photo-realistic 3D look).
  Separate "Custom Depth Shading" mode = discrete per-1-m bands.
- **Relief/hillshade — the defining feature:** directional hillshade computed from the
  bathy grid composited over the colour ramp (NW light ~315° az, ~45° alt). This is the
  single most-praised look in public forums (§8).
- **Isobaths:** High-res down to 1 ft; secondary once relief is on.
- Sources: <https://www.c-map.com/all-charts/reveal/>,
  <https://insightgenesis.wordpress.com/2019/04/24/c-map-reveal-charts-feature-photo-realistic-sea-floor-imagery-for-simrad-lowrance-bg-users/>

### Humminbird LakeMaster / VX / AutoChart Live

Most explicit, best-documented highlight system. Confirmed semantics: *shallow-water
highlight = RED, depth highlight = GREEN, depth fill = shades of bluish tints.*

- **Ramp:** pale blue `#E6F2FA`/`#BFE0F2` → mid `#6FB3DC`→`#3E8DC6` → dark `#2A6296`→`#173E66`.
  Dark = deep, white = shallow (navigational convention).
- **Bands:** discrete — up to 4 user-customisable ranges (VX: all colours interchangeable).
- **Isobaths:** signature **1-ft** contours; thin dark lines; "Follow the Contour".
- **Relief:** "2D Shaded Relief" (a light shaded-contour effect, not full hillshade).
- **Depth Range Highlight (the killer feature):** set Highlight Min/Max; every cell whose
  depth ∈ [min,max] is flooded **bright green** (`#33CC33`–`#5BE35B`) across the whole
  lake — "paint the 12–15 ft band and see where it is everywhere."
- **Shallow Water Highlight:** all water shallower than a threshold painted **red**
  (`#E23B3B`–`#FF4040`, semi-transparent), adjustable 0–60 ft.
- **Water Level Offset:** signed global offset (e.g. −3 ft on a drawn-down reservoir)
  added to every charted depth *before* classification: `effective = charted + offset`,
  then re-run band + highlight classification. Re-maps contours, shallow red, depth green.
- Sources: <https://humminbird.johnsonoutdoors.com/us/learn/mapping/lakemaster>,
  <https://virtualangling.com/learn-how-to/electronics/humminbird/humminbird-tutorials-water-level-offset-depth-highlight-and-contour-following/>,
  <https://www.wired2fish.com/electronics/humminbird-lakemaster-vx-premium-digital-chart-review>

### Raymarine LightHouse Charts

- **Ramp (shallow→deep):** dark→light blue→white. Practical day ramp `#0d3f73` →
  `#1f5fa6` → `#5b93c9` → `#a9cce8` → `#ffffff`. Keyed to 3 user thresholds (shallow/safe/deep).
- **Bands:** quantized filled bands ("depth gradient" setting only flips direction).
- **Isobaths/safety contour:** **safety contour drawn thicker + medium-blue**; spot
  soundings = small numbers with **white halo**. Fishing mode adds dense HD contours.
- **Danger shading:** shallow area = semi-transparent **red cross-hatch** 0→threshold.
- **Relief:** none (3D impression from bands + contours only).
- Sources: Raymarine LightHouse 4 manual (manualslib id 3159685, p.204);
  <https://www.raymarine.com/en-us/our-products/marine-charts/lighthouse-charts>

### NOAA ENC — IHO S-52 Presentation Library (the authoritative, citable values)

Convention: **darker blue = shallower/danger; white = deeper/safe** (deliberate inverse
of paper charts). Values below are **exact** from IHO PresLib 4.0 as shipped in OpenCPN
`chartsymbols.xml`, DAY_BRIGHT table.

| Token | Meaning | Hex (RGB) |
|---|---|---|
| DEPIT | Intertidal | `#83B295` (131,178,149) grey-green |
| DEPVS | Very shallow (0→shallow contour) | `#73B6EF` (115,182,239) darkest blue |
| DEPMS | Medium-shallow (shallow→safety) | `#98C5F2` (152,197,242) |
| DEPMD | Medium-deep (safety→deep) | `#BAD5E1` (186,213,225) |
| DEPDW | Deep (>deep contour) | `#D4EAEE` (212,234,238) pale near-white |
| DEPCN | Contour line | `#7D898C` (125,137,140) |
| CHBLK | Lines/text | `#070707`; NODTA `#A3B4B7`; CHGRF `#A3B4B7` |

- **Two-shade** (IMO default): one **safety contour**, shallow=blue (DEPVS) / deep=white
  (DEPDW). **Four-shade** (optional, richer): adds shallow + deep contours → DEPVS/DEPMS
  unsafe, DEPMD/DEPDW safe.
- **Safety contour:** the key isobath; drawn **bold/thick** (DEPCN); snaps to next deeper
  available contour. Optional shallow cross-hatch (DIAMOND1) over sub-safety water.
- **Fill:** flat opaque polygons, no gradients. **Soundings:** black if ≤ safety depth, grey if deeper.
- Sources: <https://raw.githubusercontent.com/OpenCPN/OpenCPN/master/data/s57data/chartsymbols.xml>;
  IHO S-52 PresLib 4.0 / S-52 Ed 6.1.1 <https://iho.int/uploads/user/pubs/standards/s-52/S-52%20Edition%206.1.1%20-%20June%202015.pdf>

### GEBCO / EMODnet (open scientific palettes)

Convention: **light shallow → dark navy deep, smooth gradients.**

- **GEBCO blue ramp** (approx; GEBCO publishes no official hex):
  `#d8f2fe, #a8ddf0, #7ec4e3, #5aa9d6, #3d8dc4, #2b6cae, #1f4f96, #16356f, #0d1f4a`.
- **cmocean `deep`** (**exact**, citable scientific ramp; starts yellow-green):
  `#fdfecc → #a7e0a7 → #5ab6a4 → #4a949f → #3f7097 → #3e5992 → #352a4a → #281a2c`.
  Source: <https://github.com/matplotlib/cmocean>.
- **EMODnet blue ramp** (approx):
  `#eaf6fb, #c4e6f5, #9bd0ea, #6fb4dc, #4b95cc, #3275ba, #1f579e, #133c7c, #0a2257`.
- **Bands vs gradient:** web maps = smooth; atlas reproductions band at
  0, −10, −20, −50, −100, −200, −500, −1000, −2000, −3000, −4000, −5000, −6000 m
  (emphasise −200 shelf break).
- **Hillshade:** GEBCO ships an explicit shaded-relief layer (composite via multiply).
- Sources: <https://www.gebco.net/>, <https://emodnet.ec.europa.eu/en/bathymetry>

### OpenSeaMap

Convention: saturated cyan shallow → near-white deep (paper-chart style). Exact values
from the `prozessor13/seamap` MapLibre reimplementation:

- **Depth-area fill (shallow→deep, exact):** 0 m `#cadbc1` (pale green-grey) · 0–2 m
  `#68cafe` · 2–5 m `#73cefe` · 5–10 m `#83d4fe` · 10–20 m `#9adcfe` · 20–50 m
  `#bae7fe` · >50 m `#e9f7ff`. Filled opaque bands.
- **Isobaths:** `#777` mid-grey, **0.5 px, 50% opacity**; intervals
  0/2/5/10/20/50/100/250/500/1000… m; integer labels in `#777`.
- **Soundings:** `#777`; deep = integers (sparse), shallow (0.1–5 m) = one decimal (dense).
- **Hillshade:** bathymetric hillshade (`combined` method, exaggeration ~0.14–0.2) from
  Terrarium-RGB tiles; contours/soundings generated client-side via `maplibre-contour`.
- Sources: <https://github.com/prozessor13/seamap>; <https://wiki.openstreetmap.org/wiki/Water_depth>

---

## 2. RECOMMENDED colour ramp

### Primary ramp — "Angler high-contrast" (recommended default)

Matches the public's stated preference (§8): shallow danger pops, deep recedes to dark.
Filled bands, fully recolourable. Ordered **shallow → deep**:

| Band | Depth (ft) | Hex | Note |
|---|---|---|---|
| 0 | 0–2 | `#D7263D` | red — very shallow / hazard |
| 1 | 2–5 | `#F46036` | orange |
| 2 | 5–10 | `#FFD23F` | yellow |
| 3 | 10–15 | `#7FB800` | green (typical "fish zone" start) |
| 4 | 15–25 | `#2EA2C9` | light blue |
| 5 | 25–40 | `#2167A8` | mid blue |
| 6 | 40–60 | `#16456E` | deep blue |
| 7 | >60 | `#0B2540` | darkest navy |

Make every band's colour, min and max user-editable (LakeMaster/Genesis pattern).

### Second option — "S-52 chart-grade" (standards-grounded, citable)

Use when you want navigational credibility (matches NOAA ENC / LightHouse / OpenSeaMap).
Monochrome blue, deep = light/white, flat opaque bands, one bold safety contour.
Ordered **shallow → deep**:

`#73B6EF` (very shallow) → `#98C5F2` → `#BAD5E1` → `#D4EAEE` → `#FFFFFF` (deep)

with intertidal `#83B295`. This is the literal S-52 DAY_BRIGHT four-shade table.

> Pick the ramp convention deliberately and **do not mix** light-deep and dark-deep in one
> palette. Default to Primary; expose Secondary as a selectable "Chart" theme.

---

## 3. Isobath (contour) scheme

- **Interval:** 1 ft inland / shallow (the LakeMaster signature, universally praised),
  promote to 2 ft then 5 ft / 5 m where source data is coarse. Pick interval by zoom +
  data resolution, not a fixed value.
- **Colour:** thin neutral grey-blue, e.g. `#5A6B73` over the angler ramp, or S-52
  `#7D898C` over the chart ramp. Stay neutral so it reads over every fill band.
- **Weight:** ordinary contours **1.0 px** (0.5–0.75 px at low zoom, OpenSeaMap uses 0.5 px
  @ 50% opacity to avoid clutter). **Index contours** (every 5th, or 10/20/50 ft) **1.75–2 px**.
- **Safety contour:** the single user-set isobath drawn **bold (2.5–3 px), dark** — `#5F6A60`
  for chart style, or red for angler style. This is the primary at-a-glance safe/unsafe line.
- **Labels:** integer depth on index contours only, placed inline (break the line under the
  text), small, with a 1 px halo in the basemap colour for legibility (LightHouse uses white
  halo). **Do not label every contour** — clutter is the #1 complaint (§8). Soundings: black
  if ≤ safety depth, grey if deeper.

---

## 4. Smoothing / interpolation

Yes — smooth, but truthfully.

- **Fill:** classify per-cell into bands; **smooth band boundaries**, do not show raw grid
  stair-steps. "Blocky / stair-stepped / pixelated" is a top complaint (§8). Practical
  approaches on canvas: (a) marching-squares to vector polygons then draw smoothed paths;
  or (b) bilinear-resample the depth grid before classifying so band edges fall between cells.
- **Contours:** generate via marching-squares on a bilinearly-interpolated grid, then apply
  Chaikin / Catmull-Rom smoothing. Avoid over-smoothing that invents structure.
- **Honesty flag:** crowd-sourced / interpolated contours are widely distrusted when they look
  as authoritative as surveyed ones ("just drawn between the 10-ft lines"). Render
  interpolated/low-confidence contours **dashed or at lower opacity** so they read as
  approximate. Apply the **water-level offset before** classification:
  `effective = charted + offset`.
- Keep the look **stable across the whole zoom range** — shading vanishing or going blurry on
  zoom is a repeated complaint. Pre-build per-zoom contour sets rather than scaling one set.

---

## 5. Relief / hillshade

The C-MAP Reveal look is the most-praised aesthetic. Layer it under the colour fill:

1. Compute surface normals from the depth grid (Sobel/central differences on a slightly
   blurred grid to suppress noise).
2. Lambert shade against a light vector at **azimuth 315° (NW), altitude ~45°**;
   add a mild vertical exaggeration (~3–6×) so subtle ledges read.
3. Composite grayscale hillshade over the colour ramp via **multiply at ~25–40% opacity**
   (GEBCO/cmocean practice). Too strong darkens the chart and buries contours — another complaint.

Critically: **render relief AND fine contours together.** The single most-repeated public
complaint is brands forcing an either/or (you must disable 1-ft contours to see relief).
Keep them as independent, simultaneously-visible layers with their own opacity controls.

On canvas, precompute the hillshade to an offscreen `ImageData` per tile/viewport and blit it;
don't recompute per frame.

---

## 6. Legend

- Vertical stack of band swatches with depth range labels (`0–2`, `2–5`, … `>60 ft`),
  collapsible. Show the active **depth-units** (ft / m) and the current **water-level offset**.
- Indicate the **safety depth** line on the legend (a marked tick / bold rule).
- If a **depth-range highlight** is active, show its band swatch + range distinctly.
- Keep it small, semi-transparent, corner-anchored; one-tap hide (toggling shading on/off is a
  praised feature).

---

## 7. Performance on a canvas (large areas)

- **Tile it.** Classify + shade + hillshade per tile into an offscreen `OffscreenCanvas` /
  `ImageData`; cache by `{tileXY, zoom, palette, offset}`. Invalidate only on palette/offset
  change. Use a Web Worker for marching-squares + hillshade to keep the main thread responsive.
- **Two-layer composite:** (a) raster fill+hillshade (cheap to blit, rarely changes),
  (b) vector contours/labels/highlights redrawn on pan/zoom. Don't rebuild the raster on pan.
- **Pre-quantise** the depth grid to the band set so fill is index→colour lookup, not per-pixel math.
- **LOD:** coarser grid + wider contour interval when zoomed out; finer near the boat. Cull
  off-screen tiles. Decimate contour vertices (Douglas-Peucker) per zoom.
- **Highlights** (shallow-red / depth-green) are cheap recolour passes over the quantised index
  buffer — recompute only when the user changes the highlight band, then cache.
- Prefer `requestAnimationFrame` batching; avoid per-pixel `getImageData`/`putImageData` in the
  hot path — work in pre-built buffers. `devicePixelRatio`-aware sizing for crisp lines.

---

## 8. Do / Don't (from public preference)

Sourced from The Hull Truth, Bass Boat Central (bbcboards), Walleye/Austin/Wayne's-Words
forums, Wired2Fish, Tilt Fishing, talkseafishing.

**DO**

- Make a **LakeMaster-style depth-range highlight** the headline feature: user-set min/max
  band flooded in a high-contrast colour (green), multiple simultaneous bands, plus a separate
  **shallow-water red highlight**. ("one of the most useful tools for fishing I've seen.")
  <https://www.bbcboards.net/showthread.php?t=981224>
- Default to **shallow = warm/light/green, deep = dark**, high contrast; let users **recolour
  every band**. <https://www.bbcboards.net/showthread.php?t=904698>
- Offer a **C-MAP-Reveal-style relief shading** option — the most-praised "look"
  ("game changer", "almost don't need a fishfinder").
  <https://www.thehulltruth.com/marine-electronics-forum/1282376-cmap-reveal.html>
- **Render relief AND fine contours together** (the #1 fix to the top complaint).
  <https://www.thehulltruth.com/marine-electronics-forum/1266792-navionics-platinum-shading-contours.html>
- Provide **1-ft contours** with **complete coverage**, and a **water-level offset**.
  <https://tiltfishing.com/lakemaster-vs-navionics-which-lake-maps-should-you-use/>
- Easy **on/off toggle** for shading, independent contour-density control, stable look across zoom.

**DON'T**

- Don't force **shading vs contours as mutually exclusive** (Navionics' top gripe).
- Don't let shading go **blurry / pixelated / partial / vanish on zoom**.
  <https://www.bbcboards.net/showthread.php?t=1200397>,
  <https://www.bbcboards.net/showthread.php?t=987696>
- Don't make charts **too dark** / bury contours under heavy shading.
  <https://support.garmin.com/en-US/?faq=XnF4ICVGKg0pFfxuxIeld8>
- Don't present **interpolated/crowd-sourced contours as authoritative** — flag them as approximate.
  <https://www.thehulltruth.com/marine-electronics-forum/1229047-navionics-vs-hummingbird-lakemaster-software.html>
- Don't **over-label** contours (clutter) or use **garish, low-contrast, or stair-stepped** fills.
- Don't ship the **white-deep** convention as the only option — many anglers dislike Navionics'
  inverse and prefer dark-deep. <https://wayneswords.net/threads/lakemaster-vs-navionics.10473/>

---

## Quick build target

Default theme = **Angler high-contrast** banded fill (§2 primary) + neutral 1-ft contours
with bold safety line (§3) + optional NW hillshade composited at ~30% (§5) + LakeMaster-style
green depth-range highlight and red shallow highlight (§8) + water-level offset, all on a
tiled, worker-backed offscreen-canvas pipeline (§7). Offer **S-52 chart-grade** as a second
selectable theme for navigational credibility.
