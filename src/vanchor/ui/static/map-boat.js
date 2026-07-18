/* Vanchor-NG — boat marker.
 *
 * The boat marker (glowing vessel + heading + trolling-motor direction
 * indicator), its active-mode badge, the tappable vitals popup ("Anchor here"
 * / "Weigh anchor"), the smoothed GPS-fix dot, the true-to-scale icon sizing,
 * the live trail, the boat-follow pan, and the Settings boat-icon design picker
 * (#84).
 *
 * Registers its OWN VA.onTelemetry handler for the boat position / heading /
 * motor / mode + GPS dot + trail + follow-pan. Reads the boat designs from
 * VA.mapCtx.boatIcon and the shared map + follow state from VA.mapCtx. Loads
 * after map-core.js and map-boaticon.js.
 */
"use strict";

(function () {
  const VA = window.VA;
  const ctx = VA.mapCtx;
  const map = ctx.map;

  // Which boat to draw when BOTH a sim boat (truth) and a real GPS fix exist
  // (Settings -> Simulator -> "Boat shown on map"): "auto" (real GPS when the GPS
  // source is real hardware, else the sim boat), "sim", or "gps". localStorage-
  // backed; settings.js dispatches "va:boatsource" on change.
  let _boatSource = "auto";
  try { _boatSource = localStorage.getItem("mapBoatSource") || "auto"; } catch (e) { /* ignore */ }
  window.addEventListener("va:boatsource", function (e) {
    _boatSource = (e && e.detail) || "auto";
  });
  const START = ctx.START;
  const follow = ctx.follow;
  // Recenter FAB (#follow-fab): the click -> VA.map.recenter() is wired elsewhere;
  // here we reveal it (on desktop too) whenever the boat drifts out of the map view.
  const followFab = document.getElementById("follow-fab");
  const { BOAT_DESIGNS, BOW_X, BOW_Y, boatDiv, updateMotorIndicator } = ctx.boatIcon;

  const BOAT_ICON_KEY = "vanchor-boat-icon";
  function getBoatIconId() {
    try {
      const v = localStorage.getItem(BOAT_ICON_KEY);
      if (v && BOAT_DESIGNS[v]) return v;
    } catch (e) { /* ignore */ }
    return "current";
  }
  let boatIconId = getBoatIconId();

  const boatMarker = L.marker(START, { icon: boatDiv(boatIconId), zIndexOffset: 1000 }).addTo(map);

  // ---- active-mode badge + tappable boat info popup ----------------------
  // A small glyph+label beside the boat shows the ACTIVE mode at a glance;
  // tapping the boat opens its vitals + an "Anchor here" action.
  const MODE_META = {
    manual: ["🕹", "Manual"], anchor_hold: ["⚓", "Anchor"],
    anchor_ml: ["⚓", "Anchor · Smart"], anchor_leif: ["⚓", "Anchor · Leif"],
    heading_hold: ["🧭", "Heading"], waypoint: ["📍", "Route"], follow_apb: ["🛰", "Follow APB"],
    drift: ["🌀", "Drift"], orbit: ["🔄", "Orbit"], contour: ["📈", "Contour"],
    trolling: ["🎣", "Trolling"], work_area: ["🎯", "Work Area"],
    cruise: ["🚤", "Cruise"], stop: ["■", "Stopped"],
  };
  const modeMeta = (m) => MODE_META[m] || ["•", m || "—"];

  // Pixel-gated setLatLng: GPS/compass noise moves the markers by fractions of
  // a pixel on every 5-10 Hz frame; each ungated set repositions the marker
  // element and repaints, so an idle chart view never stops rastering. Only
  // reposition once a marker would visibly move (>=0.5 px at current zoom). (perf)
  function setLatLngIfMoved(marker, lat, lon) {
    const p = map.project([lat, lon], map.getZoom());
    const c = marker._vaPt;
    if (c && c.z === map.getZoom()
        && Math.abs(c.x - p.x) < 0.5 && Math.abs(c.y - p.y) < 0.5) return;
    marker._vaPt = { x: p.x, y: p.y, z: map.getZoom() };
    marker.setLatLng([lat, lon]);
  }

  // ---- mode pill (fixed screen corner, not map-riding) --------------------
  // Replaces the old marker-riding badge. Updated from VA.modeSentence so the
  // pill always matches the sheet-mode sentence. Right-edge clip is prevented
  // by CSS max-width + overflow:hidden on #mode-pill.
  let _pillKey;
  function updateModePill(t) {
    const sentence = VA.modeSentence ? VA.modeSentence(t) : (t.mode || "—");
    const aa = t && t.anchor_alarm;
    const dragging = !!(aa && aa.firing);
    const key = sentence + (dragging ? "d" : "");
    if (key === _pillKey) return;
    _pillKey = key;
    const pill = document.getElementById("mode-pill");
    if (!pill) return;
    pill.textContent = sentence;
    pill.dataset.mode = t.mode || "manual";
    pill.classList.toggle("dragging", dragging);
  }

  function _distM(aLat, aLon, bLat, bLon) {
    const R = 6371000, k = Math.PI / 180;
    const dLat = (bLat - aLat) * k, dLon = (bLon - aLon) * k;
    const s = Math.sin(dLat / 2) ** 2 + Math.cos(aLat * k) * Math.cos(bLat * k) * Math.sin(dLon / 2) ** 2;
    return 2 * R * Math.asin(Math.sqrt(s));
  }
  function buildBoatPopup() {
    const t = VA.last || {};
    const [g, label] = modeMeta(t.mode);
    const pos = t.position;
    const row = (k, v) => `<div class="bp-row"><span>${k}</span><b>${v}</b></div>`;
    const sog = Number.isFinite(t.sog_knots) ? t.sog_knots.toFixed(1) + " kn" : "—";
    const hdg = Number.isFinite(t.heading_deg) ? Math.round(t.heading_deg) + "°" : "—";
    const depth = Number.isFinite(t.depth_m) ? t.depth_m.toFixed(1) + " m" : "—";
    // Gate anchor rows/actions on "actively anchored" (anchor data + anchor mode)
    const anchored = !!t.anchor && typeof t.mode === "string" && t.mode.startsWith("anchor");
    let anchorRow = "";
    if (anchored && pos) {
      const d = _distM(pos.lat, pos.lon, t.anchor.lat, t.anchor.lon);
      const r = Number.isFinite(t.anchor_radius_m) ? Math.round(t.anchor_radius_m) : "?";
      anchorRow = row("From anchor", `${d.toFixed(1)} m / ${r} m`);
    }
    return `<div class="boat-popup">
        <div class="bp-mode"><span class="bp-i">${g}</span>${label}</div>
        <div class="bp-rows">
          <div class="bp-row"><span>Position</span><b class="selectable">${pos ? pos.lat.toFixed(5) + ", " + pos.lon.toFixed(5) : "—"}</b></div>
          ${row("Heading", hdg)}${row("Speed", sog)}${row("Depth", depth)}${anchorRow}
        </div>
        <div class="bp-actions">
          <button class="bp-btn bp-anchor" data-act="anchor">⚓ Anchor here</button>
          ${anchored ? `<button class="bp-btn bp-weigh" data-act="weigh">■ Weigh anchor</button>` : ""}
        </div></div>`;
  }
  boatMarker.bindPopup(buildBoatPopup, { className: "boat-popup-wrap", autoPanPadding: [24, 24] });
  boatMarker.on("popupopen", (e) => {
    const node = e.popup.getElement();
    if (!node) return;
    const anchorBtn = node.querySelector('[data-act="anchor"]');
    if (anchorBtn) anchorBtn.addEventListener("click", () => {
      const r = parseFloat(document.getElementById("ar")?.value) || 6;
      const holdEl = document.getElementById("hold-hdg");
      VA.send({ type: "anchor_hold", radius_m: r, hold_heading: holdEl ? holdEl.checked : true });
      boatMarker.closePopup();
    });
    const weighBtn = node.querySelector('[data-act="weigh"]');
    if (weighBtn) weighBtn.addEventListener("click", () => {
      VA.send({ type: "stop" });
      boatMarker.closePopup();
    });
  });

  // ---- smoothed GPS-fix dot ----------------------------------------------
  const gpsMarker = L.circleMarker(START, {
    radius: 4, color: "#1be4ff", fillColor: "#1be4ff", fillOpacity: 0.9, weight: 1,
  }).addTo(map).bindTooltip("GPS fix");
  let _gpsLat = null, _gpsLon = null;  // low-passed GPS dot position (display only)

  // ---- live trail --------------------------------------------------------
  // Shared SVG renderer on purpose: a dedicated canvas renderer was measured
  // WORSE here — it adds a second full-container-sized composited layer (huge
  // under the heading-up oversized square) for an overlay pane that is nearly
  // empty anyway. The wins live in the append/throttle logic below. (perf)
  const trail = L.polyline([], {
    color: "#1be4ff", weight: 2, opacity: 0.45, interactive: false,
  }).addTo(map);
  const trailPts = [];
  let _trailMs = 0;

  // ---- boat icon scaling -------------------------------------------------
  // The boat is a fixed-pixel icon by default, so it looks tiny when zoomed
  // in. Scale it to the boat's REAL length (a minimum size floor so it never
  // vanishes, but no maximum so it grows true-to-scale as you zoom in).
  const BASE_ICON_PX = 48; // icon height in px = the minimum on-screen size
  let _boatEl = null, _boatLat = null, _boatHdg = 0;
  function boatScale(lat) {
    const z = map.getZoom();
    const mpp = (40075016.686 * Math.cos((lat * Math.PI) / 180)) / Math.pow(2, z + 8);
    const lenM = (VA.last && VA.last.boat && VA.last.boat.length_m) || 4.1;
    return Math.max(1, lenM / mpp / BASE_ICON_PX);
  }
  let _lastBoatTf = null;
  function applyBoatTransform() {
    if (!_boatEl || _boatLat === null) return;
    // Quantize to 1° / 0.001 scale: compass jitter on a moored boat otherwise
    // rewrites the transform (and the drop-shadow lift filter) on every 5-10 Hz
    // telemetry frame for a change nobody can see at 48 px (1° = 0.4 px at the
    // icon edge). The gate key includes the LIFT inputs (tilt + chart bearing)
    // too, so toggling tilt or a rotating chart still refreshes the filter. (perf)
    const rot = Math.round(VA.smoothAngle("boat", _boatHdg));
    const tf = `rotate(${rot}deg) scale(${boatScale(_boatLat).toFixed(3)})`;
    const mr = VA.mapRot;
    const tilt = (mr && mr.mode && mr.mode() === "head" && mr.tilt) ? mr.tilt() : 0;
    const brg = tilt && mr.bearing ? Math.round(mr.bearing()) : 0;
    const key = `${tf}|${tilt}|${brg}`;
    if (key === _lastBoatTf) return;
    _lastBoatTf = key;
    _boatEl.style.transform = tf;
    applyBoatLift(rot);
  }

  // Pseudo-3D hull under heading-up TILT: extrude the silhouette with a stack
  // of 1px drop-shadows toward screen-down (the "near" edge), plus a soft
  // ground shadow. Works for EVERY boat design (it extrudes whatever alpha
  // silhouette the SVG has) and scales with the icon (filter offsets are in
  // the icon's local px, so a bigger boat gets a proportionally thicker hull).
  function applyBoatLift(rot) {
    const mr = VA.mapRot;
    const tilt = (mr && mr.mode && mr.mode() === "head" && mr.tilt) ? mr.tilt() : 0;
    if (!tilt) {
      if (_boatEl.style.filter) _boatEl.style.filter = "";
      return;
    }
    // Screen-down mapped into the ICON's local frame: the icon's total screen
    // rotation is its own map-frame rotation plus the chart bearing.
    const theta = ((rot + ((mr.bearing && mr.bearing()) || 0)) * Math.PI) / 180;
    const ux = Math.sin(theta), uy = Math.cos(theta);
    const layers = Math.max(2, Math.round(tilt / 9));   // 0..60° -> 2..7 px of hull
    let f = "";
    for (let i = 1; i <= layers; i++) {
      f += `drop-shadow(${(ux * i).toFixed(2)}px ${(uy * i).toFixed(2)}px 0 rgba(7, 24, 36, 0.92)) `;
    }
    f += `drop-shadow(${(ux * (layers + 2)).toFixed(2)}px ${(uy * (layers + 2)).toFixed(2)}px 3px rgba(0, 0, 0, 0.45))`;
    _boatEl.style.filter = f;
  }
  map.on("zoomend", applyBoatTransform);
  VA.mapBoat = { setIcon: (id) => setBoatIcon(id) };

  // ---- boat icon design picker (#84) -------------------------------------
  // Rebuild the boat marker's icon with the selected design, then re-grab the
  // fresh SVG element and re-apply heading/zoom transform + the motor needle so
  // it updates live (no reload). setIcon() replaces the DOM node, so the cached
  // _boatEl must be refreshed.
  function setBoatIcon(id) {
    if (!BOAT_DESIGNS[id]) id = "current";
    boatIconId = id;
    try { localStorage.setItem(BOAT_ICON_KEY, id); } catch (e) { /* ignore */ }
    boatMarker.setIcon(boatDiv(id));
    const el = boatMarker.getElement()?.querySelector(".boat-icon");
    if (el) {
      _boatEl = el;
      _lastBoatTf = null;   // fresh DOM node: force the transform re-apply
      applyBoatTransform();
      updateMotorIndicator(el, VA.last && VA.last.motor);
    }
  }

  // Inject a themed "Boat icon" picker card into the Settings drawer. Uses the
  // existing CSS custom props / card styling so it matches the rest of the UI.
  function injectBoatIconPicker() {
    const host = document.querySelector("#settings .drawer-body") || document.getElementById("settings");
    if (!host || document.getElementById("boat-icon-picker")) return;

    if (!document.getElementById("boat-icon-picker-css")) {
      const st = document.createElement("style");
      st.id = "boat-icon-picker-css";
      st.textContent = `
        #boat-icon-picker .boat-icon-grid {
          display: grid; grid-template-columns: repeat(auto-fit, minmax(84px, 1fr));
          gap: 8px; margin-top: 8px;
        }
        #boat-icon-picker .boat-choice {
          display: flex; flex-direction: column; align-items: center; gap: 4px;
          padding: 8px 4px; cursor: pointer;
          background: var(--glass, rgba(255,255,255,0.04));
          border: 1px solid var(--line, rgba(255,255,255,0.12));
          border-radius: var(--r, 10px);
          color: var(--muted, #9fb0bd); font-size: 11px; text-align: center;
          transition: border-color .15s, color .15s, background .15s;
        }
        #boat-icon-picker .boat-choice:hover { border-color: var(--accent, #1be4ff); }
        #boat-icon-picker .boat-choice.sel {
          border-color: var(--accent, #1be4ff);
          color: var(--text, #eaf6fb);
          box-shadow: 0 0 0 1px var(--accent, #1be4ff) inset;
        }
        #boat-icon-picker .boat-choice svg { display: block; height: 40px; width: auto; }
        #boat-icon-picker .boat-choice input { display: none; }`;
      document.head.appendChild(st);
    }

    const card = document.createElement("div");
    card.className = "card";
    card.id = "boat-icon-picker";
    const head = document.createElement("div");
    head.className = "summary";
    head.textContent = "Boat icon";
    card.appendChild(head);

    const grid = document.createElement("div");
    grid.className = "boat-icon-grid";
    Object.keys(BOAT_DESIGNS).forEach((id) => {
      const d = BOAT_DESIGNS[id];
      const label = document.createElement("label");
      label.className = "boat-choice" + (id === boatIconId ? " sel" : "");
      label.dataset.id = id;
      // Small upright preview (no rotation/scale; reuse the design body).
      label.innerHTML =
        `<svg width="34" height="48" viewBox="0 0 34 48" style="overflow:visible">${d.body()}</svg>` +
        `<input type="radio" name="boat-icon" value="${id}"${id === boatIconId ? " checked" : ""}>` +
        `<span>${d.label}</span>`;
      label.addEventListener("click", () => {
        setBoatIcon(id);
        grid.querySelectorAll(".boat-choice").forEach((c) => c.classList.toggle("sel", c.dataset.id === id));
        const input = label.querySelector("input");
        if (input) input.checked = true;
      });
      grid.appendChild(label);
    });
    card.appendChild(grid);
    host.appendChild(card);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", injectBoatIconPicker);
  } else {
    injectBoatIconPicker();
  }

  // ---- follow-pan --------------------------------------------------------
  // While driving, the boat outruns the 2 px gate on EVERY telemetry frame, so
  // a plain panTo would run the full move/moveend cascade (tile-layer updates,
  // overlay re-glue, layer-tree commit) at up to 10 Hz — the single biggest
  // main-thread cost found by profiling. Instead: shift the map pane directly
  // (Leaflet's own drag/animation primitive — all panes, markers and overlays
  // ride along, and getCenter()/coordinate math read the pane position so they
  // stay exact), and fire the real move/moveend at most every 400 ms plus a
  // trailing one when the motion stops, so tile loading and moveend listeners
  // still catch up promptly. (perf)
  let _lastMoveEndMs = 0, _panTrailer = null;
  function fireMoveEnd() {
    _lastMoveEndMs = Date.now();
    map.fire("move");
    map.fire("moveend");
  }
  function followPan(dx, dy) {
    if (map._animatingZoom) return;
    if (!map._rawPanBy) {   // Leaflet internal gone: fall back to plain pan
      map.panBy([dx, dy], { animate: false });
      return;
    }
    map._rawPanBy(L.point(Math.round(dx), Math.round(dy)));
    if (Date.now() - _lastMoveEndMs >= 400) fireMoveEnd();
    else {
      clearTimeout(_panTrailer);
      _panTrailer = setTimeout(fireMoveEnd, 450);
    }
  }

  // ---- per-frame render --------------------------------------------------
  // Prefer sim ground truth when present; otherwise use the GPS position +
  // heading so the boat still renders on a real boat (truth is sim-only).
  VA.onTelemetry(function renderBoat(t) {
    const truth = t.truth;
    // Boat-source setting: prefer the real GPS fix over the sim boat per the
    // user's choice. "auto" = real GPS whenever the GPS source is real hardware.
    const gpsReal = !!(t.devices && t.devices.gps && t.devices.gps.source
                       && t.devices.gps.source !== "sim");
    const preferGps = _boatSource === "gps" ? true
      : _boatSource === "sim" ? false : gpsReal;
    const useTruth = !!truth && !(preferGps && t.position);
    const src = useTruth ? truth : (t.position || truth);
    const lat = src ? src.lat : null;
    const lon = src ? src.lon : null;
    const hdg = useTruth && Number.isFinite(truth.heading_deg) ? truth.heading_deg : t.heading_deg;
    if (lat !== null && lon !== null && Number.isFinite(lat) && Number.isFinite(lon)) {
      setLatLngIfMoved(boatMarker, lat, lon);
      // Show the recenter FAB when the boat is off-screen (auto-hidden while
      // following, since the boat is then kept in view).
      if (followFab) followFab.classList.toggle("offscreen", !map.getBounds().contains([lat, lon]));
      updateModePill(t);
      const el = boatMarker.getElement()?.querySelector(".boat-icon");
      if (el) {
        _boatEl = el; _boatLat = lat; _boatHdg = Number.isFinite(hdg) ? hdg : _boatHdg;
        applyBoatTransform();
        updateMotorIndicator(el, t.motor);
      }
      // Only grow the trail when the boat has actually moved since the last
      // pushed point, and at most 2 Hz — while driving, a 10 Hz telemetry rate
      // would otherwise repaint the full 600-point line every frame for ~0.3 m
      // of new track. addLatLng appends without rebuilding the latlng array.
      const lastPt = trailPts[trailPts.length - 1];
      const TRAIL_EPS = 1e-6;   // ~0.1 m in lat/lon
      const nowMs = Date.now();
      if ((!lastPt || Math.abs(lastPt[0] - lat) > TRAIL_EPS || Math.abs(lastPt[1] - lon) > TRAIL_EPS)
          && nowMs - _trailMs >= 500) {
        _trailMs = nowMs;
        trailPts.push([lat, lon]);
        if (trailPts.length > 600) {
          // Amortize the trim: drop the oldest 60 in one rebuild instead of
          // shifting + full re-set on every appended point past the cap.
          trailPts.splice(0, 60);
          trail.setLatLngs(trailPts);
        } else {
          trail.addLatLng([lat, lon]);
        }
      }
      if (follow.boat) {
        // Skip the follow-pan when the boat is already ~centred: at 5 Hz the
        // sub-pixel drift otherwise fires a moveend cascade through all 4 tile
        // layers every frame. Compare the boat's on-screen point to where it
        // should sit (view centre, offset by the follow offset). (perf)
        const size = map.getSize();
        const bp = map.latLngToContainerPoint([lat, lon]);
        const wantX = size.x / 2 + (follow.offsetX || 0);
        const wantY = size.y / 2 + (follow.offsetY || 0);
        const dx = bp.x - wantX, dy = bp.y - wantY;
        if (Math.abs(dx) >= 2 || Math.abs(dy) >= 2) followPan(dx, dy);
      }
    }
    // GPS fix stale: grey the BOAT marker (only) when the fix is lost (either
    // the governor's failsafe flag or the health monitor's staleness flag).
    const fixLost = !!((t.safety && t.safety.fix_lost) || (t.health && t.health.fix_lost));
    const boatEl = boatMarker.getElement();
    if (boatEl) boatEl.classList.toggle("fix-stale", fixLost);

    // Smooth the displayed GPS dot. Raw 1 Hz fixes carry a few metres of noise
    // that makes the dot jump around; a real plotter low-passes it so it sits
    // steady. (The control loop still steers on the raw fix, not this.)
    if (t.position) {
      const a = 0.18;
      if (_gpsLat === null) { _gpsLat = t.position.lat; _gpsLon = t.position.lon; }
      else {
        _gpsLat += (t.position.lat - _gpsLat) * a;
        _gpsLon += (t.position.lon - _gpsLon) * a;
      }
      setLatLngIfMoved(gpsMarker, _gpsLat, _gpsLon);
    }
  });

  // ---- manual COURSE-hold track line (steer_course) -------------------------
  // The line the boat is following: from the anchored engage point along the
  // set bearing (a little behind it, far ahead). Removed when course mode ends.
  let courseLine = null;
  VA.onTelemetry((t) => {
    if (!t || t.manual_course === undefined) return;   // decimated frame: no change
    const mc = t.manual_course;
    if (!mc || t.mode !== "manual") {
      if (courseLine) { map.removeLayer(courseLine); courseLine = null; }
      return;
    }
    const R = 6371000, rad = Math.PI / 180;
    const dest = (lat, lon, d, brg) => {
      const f1 = lat * rad, l1 = lon * rad, tc = brg * rad, dr = d / R;
      const f2 = Math.asin(Math.sin(f1) * Math.cos(dr) + Math.cos(f1) * Math.sin(dr) * Math.cos(tc));
      const l2 = l1 + Math.atan2(Math.sin(tc) * Math.sin(dr) * Math.cos(f1),
                                 Math.cos(dr) - Math.sin(f1) * Math.sin(f2));
      return [f2 / rad, l2 / rad];
    };
    const pts = [dest(mc.lat, mc.lon, -200, mc.bearing),
                 [mc.lat, mc.lon],
                 dest(mc.lat, mc.lon, 20000, mc.bearing)];
    if (!courseLine) {
      courseLine = L.polyline(pts, { color: "#27f5b1", weight: 2, dashArray: "10,8", opacity: 0.75 }).addTo(map);
    } else {
      courseLine.setLatLngs(pts);
    }
  });
})();
