/* Vanchor-NG — waypoints / route editor.
 *
 * The active-route waypoints (committed, from telemetry — draggable + a
 * long-press edit menu so a running route can be edited live), the pending
 * waypoints (the route the user is building pre-start), and the dashed route
 * line that joins them.
 *
 * Registers its OWN VA.onTelemetry handler for t.waypoints / t.active_waypoint.
 * Extends the public VA.map with the pending-/committed-route editor accessors
 * (pending, addPending, setPending, onWaypointChange, onRouteEdit,
 * redrawWaypoints). Reads the shared map from VA.mapCtx. Loads after
 * map-core.js.
 */
"use strict";

(function () {
  const VA = window.VA;
  const map = VA.mapCtx.map;

  const wpLayer = L.layerGroup().addTo(map);
  const pendingLayer = L.layerGroup().addTo(map);  // draggable, rebuilt on change only
  let routeLine = null;
  let pendingWaypoints = [];      // [{name, lat, lon}]
  let wpSeq = 0;
  let lastActiveIx = -1;

  // ---- waypoints / route -------------------------------------------------
  // Active-route waypoints (committed, from telemetry) are draggable so the user
  // can edit a running route live; on edit we re-send the whole route. We keep a
  // mutable local copy (editCommitted) that telemetry only overwrites when the
  // user isn't mid-edit, so a 5 Hz frame can't clobber a drag or the long-press
  // menu. (#51)
  let editCommitted = [];     // [{name,lat,lon}] live-editable copy
  let editing = false;        // true while dragging / menu open (suppress sync)
  let onRouteEdit = null;     // (waypoints, resumeIx) => void — re-send edited route
  let activeEditObj = null;   // the active waypoint object captured at edit start,
                              // so we can remap the resume index after the edit

  function sameWps(a, b) {
    if (a.length !== b.length) return false;
    for (let i = 0; i < a.length; i++) {
      if (Math.abs(a[i].lat - b[i].lat) > 1e-9 || Math.abs(a[i].lon - b[i].lon) > 1e-9 ||
        (a[i].name || "") !== (b[i].name || "") ||
        (a[i].throttle_pct ?? null) !== (b[i].throttle_pct ?? null) ||
        (a[i].speed_kn ?? null) !== (b[i].speed_kn ?? null)) return false;
    }
    return true;
  }

  // Marker tooltip: the waypoint name plus its optional speed attribute
  // (engine % or boat knots) so a speed-carrying mark is visible at a glance.
  function wpLabel(w, fallback) {
    const name = w.name || fallback;
    if (w.throttle_pct != null) return `${name} · ${Math.round(w.throttle_pct)}% power`;
    if (w.speed_kn != null) return `${name} · ${w.speed_kn} kn`;
    return name;
  }

  let lastDrawnActive = -2;
  function drawWaypoints(committed, activeIx) {
    let changed = false;
    if (committed !== undefined && committed !== null) {
      // Sync the editable copy from telemetry only when not mid-edit and the
      // server route actually differs (so user drags aren't fought).
      if (!editing) {
        const norm = committed.map((w, i) => ({
          name: w.name || "WP" + (i + 1), lat: w.lat, lon: w.lon,
          throttle_pct: w.throttle_pct ?? null, speed_kn: w.speed_kn ?? null,
        }));
        if (!sameWps(editCommitted, norm)) { editCommitted = norm; changed = true; }
      }
    } else {
      // Direct call (after a local edit) — always rebuild from editCommitted.
      changed = true;
    }
    if (activeIx !== undefined && activeIx !== null && activeIx !== lastActiveIx) {
      lastActiveIx = activeIx; changed = true;
    }
    const wps = editCommitted, active = lastActiveIx;
    // Only rebuild the draggable markers when the set/active index changed, so a
    // 5 Hz frame can't interrupt a long-press or thrash the DOM. (#51)
    if (changed || active !== lastDrawnActive) {
      lastDrawnActive = active;
      wpLayer.clearLayers();
      wps.forEach((w, i) => {
        const m = L.marker([w.lat, w.lon], {
          icon: committedIcon(i + 1, i === active), draggable: true, autoPan: true, zIndexOffset: 700,
        }).addTo(wpLayer).bindTooltip(wpLabel(w, "WP" + (i + 1)));
        wireCommittedMarker(m, i);
      });
    }
    // Route line: only rebuild when the waypoint set actually changed (direct
    // calls after a local edit set changed=true, covering pending edits). During
    // an active drag the drag handler updates routeLine live (see wire below), so
    // a 5 Hz telemetry frame with an unchanged set no longer re-sets the polyline.
    if (changed || !routeLine) {
      const pts = wps.map((w) => [w.lat, w.lon]).concat(pendingWaypoints.map((w) => [w.lat, w.lon]));
      if (!routeLine) routeLine = L.polyline(pts, { color: "#1be4ff", weight: 2, dashArray: "5,6", opacity: 0.7 }).addTo(map);
      else routeLine.setLatLngs(pts);
    }
  }

  function committedIcon(label, active) {
    return L.divIcon({
      className: "",
      html: `<div class="wp-pin wp-pin-active${active ? " wp-pin-now" : ""}">${label}</div>`,
      iconSize: [22, 22], iconAnchor: [11, 11],
    });
  }

  // Re-send the edited committed route through the same path startRoute uses.
  function sendRouteEdit() {
    const wps = editCommitted.map((w, i) => ({
      name: w.name || "WP" + (i + 1), lat: w.lat, lon: w.lon,
      throttle_pct: w.throttle_pct ?? null, speed_kn: w.speed_kn ?? null,
    }));
    // Preserve navigation progress across the edit: locate where the previously-
    // active waypoint landed in the edited list by OBJECT IDENTITY (so a
    // reorder/insert/delete remaps to the right mark, a drag keeps its index, and
    // a deleted active mark falls back to the clamped old index). The backend
    // resumes from this index instead of restarting at waypoint 0.
    let resume = lastActiveIx;
    if (activeEditObj) {
      const idx = editCommitted.indexOf(activeEditObj);
      resume = idx >= 0 ? idx : Math.min(lastActiveIx, editCommitted.length - 1);
    }
    resume = Math.max(0, Math.min(resume, editCommitted.length - 1));
    activeEditObj = null;
    if (onRouteEdit) onRouteEdit(wps, resume);
  }

  // Drag to move; press-and-hold ~3 s (held still) opens an edit menu.
  const LONGPRESS_MS = 3000, MOVE_TOL = 8; // px
  function wireCommittedMarker(m, ix) {
    let lpTimer = null, downPt = null, dragged = false;

    const clearLp = () => { if (lpTimer) { clearTimeout(lpTimer); lpTimer = null; } };

    m.on("dragstart", () => { activeEditObj = editCommitted[lastActiveIx] || null; editing = true; dragged = true; clearLp(); closeWpMenu(); });
    m.on("drag", (e) => {
      const ll = e.target.getLatLng();
      if (editCommitted[ix]) { editCommitted[ix].lat = ll.lat; editCommitted[ix].lon = ll.lng; }
      // redraw the route line live without rebuilding markers
      const pts = editCommitted.map((w) => [w.lat, w.lon]).concat(pendingWaypoints.map((w) => [w.lat, w.lon]));
      if (routeLine) routeLine.setLatLngs(pts);
    });
    m.on("dragend", () => { editing = false; sendRouteEdit(); drawWaypoints(); });

    // Long-press detection on the marker element (pointer events).
    const el = m.getElement && m.getElement();
    const startLp = (clientX, clientY) => {
      dragged = false;
      downPt = { x: clientX, y: clientY };
      clearLp();
      lpTimer = setTimeout(() => {
        if (!dragged) { editing = true; openWpMenu(m, ix); }
      }, LONGPRESS_MS);
    };
    const moveLp = (clientX, clientY) => {
      if (!downPt) return;
      if (Math.hypot(clientX - downPt.x, clientY - downPt.y) > MOVE_TOL) clearLp();
    };
    const node = el || (m._icon);
    if (node) {
      node.addEventListener("pointerdown", (ev) => startLp(ev.clientX, ev.clientY));
      node.addEventListener("pointermove", (ev) => moveLp(ev.clientX, ev.clientY));
      ["pointerup", "pointercancel", "pointerleave"].forEach((t) => node.addEventListener(t, clearLp));
    }
  }

  // ---- long-press waypoint edit menu -------------------------------------
  let wpMenu = null;
  function closeWpMenu() {
    if (wpMenu) { wpMenu.remove(); wpMenu = null; editing = false; }
  }

  // ---- per-waypoint speed form (engine % / boat knots) --------------------
  // Swaps the given menu's content for a small form editing w's optional speed
  // attribute (adopted by the boat ON ARRIVAL at the mark, applying to the legs
  // that follow until another speed takes over). onApply runs after the fields
  // are written (or cleared via "No speed change").
  function openSpeedForm(menu, w, title, onApply, onCancel) {
    const kind = w.throttle_pct != null ? "pct" : (w.speed_kn != null ? "kn" : "none");
    const val = w.throttle_pct != null ? w.throttle_pct : (w.speed_kn != null ? w.speed_kn : "");
    menu.innerHTML =
      `<div class="wp-menu-title">${title}</div>` +
      `<select class="wp-speed-kind" aria-label="speed type">` +
      `<option value="none"${kind === "none" ? " selected" : ""}>No speed change</option>` +
      `<option value="pct"${kind === "pct" ? " selected" : ""}>Engine power (%)</option>` +
      `<option value="kn"${kind === "kn" ? " selected" : ""}>Boat speed (knots)</option>` +
      `</select>` +
      `<input class="wp-speed-val" type="number" inputmode="decimal" min="0" step="0.1"` +
      ` value="${val}"${kind === "none" ? " disabled" : ""} aria-label="speed value" />` +
      `<button type="button" data-act="apply">Apply</button>` +
      `<button type="button" data-act="cancel" class="cancel">Cancel</button>`;
    const sel = menu.querySelector(".wp-speed-kind");
    const input = menu.querySelector(".wp-speed-val");
    sel.addEventListener("change", () => {
      input.disabled = sel.value === "none";
      if (!input.disabled) {
        input.placeholder = sel.value === "pct" ? "e.g. 40" : "e.g. 2.5";
        input.focus();
      }
    });
    menu.querySelector('[data-act="cancel"]').addEventListener("click", (e) => {
      e.stopPropagation(); onCancel();
    });
    menu.querySelector('[data-act="apply"]').addEventListener("click", (e) => {
      e.stopPropagation();
      w.throttle_pct = null; w.speed_kn = null;
      const v = parseFloat(input.value);
      if (sel.value === "pct" && Number.isFinite(v) && v > 0) w.throttle_pct = Math.min(100, v);
      else if (sel.value === "kn" && Number.isFinite(v) && v > 0) w.speed_kn = v;
      onApply();
    });
  }
  function openWpMenu(marker, ix) {
    closeWpMenu();
    if (VA.haptic) VA.haptic("press");   // confirm the 3 s hold registered
    editing = true;
    activeEditObj = editCommitted[lastActiveIx] || null;
    const pt = map.latLngToContainerPoint(marker.getLatLng());
    const menu = document.createElement("div");
    menu.className = "wp-menu glass";
    menu.innerHTML =
      `<div class="wp-menu-title">Waypoint ${ix + 1}</div>` +
      `<button type="button" data-act="before">Add waypoint before</button>` +
      `<button type="button" data-act="after">Add waypoint after</button>` +
      `<button type="button" data-act="speed">Set speed…</button>` +
      `<button type="button" data-act="delete" class="danger">Delete waypoint</button>` +
      `<button type="button" data-act="cancel" class="cancel">Cancel</button>`;
    menu.style.left = pt.x + "px";
    menu.style.top = pt.y + "px";
    const host = map.getContainer();
    host.appendChild(menu);
    wpMenu = menu;

    menu.querySelectorAll("button").forEach((b) => b.addEventListener("click", (e) => {
      e.stopPropagation();
      const act = b.dataset.act;
      if (act === "speed") {
        openSpeedForm(menu, editCommitted[ix], `Waypoint ${ix + 1} speed`,
          () => { closeWpMenu(); sendRouteEdit(); drawWaypoints(); },
          () => closeWpMenu());
        return;
      }
      if (act === "before" || act === "after") {
        const base = editCommitted[ix] || editCommitted[editCommitted.length - 1];
        const neighbor = act === "after"
          ? (editCommitted[ix + 1] || base)
          : (editCommitted[ix - 1] || base);
        // place the new waypoint midway toward the neighbour (or a small nudge)
        const nlat = base ? (base.lat + (neighbor ? neighbor.lat : base.lat)) / 2 : base.lat;
        const nlon = base ? (base.lon + (neighbor ? neighbor.lon : base.lon)) / 2 : base.lon;
        const insertAt = act === "after" ? ix + 1 : ix;
        editCommitted.splice(insertAt, 0, { name: "WP" + (++wpSeq), lat: nlat, lon: nlon });
      } else if (act === "delete") {
        editCommitted.splice(ix, 1);
      }
      closeWpMenu();
      if (act !== "cancel") { sendRouteEdit(); drawWaypoints(); }
    }));
    // tap elsewhere closes
    setTimeout(() => {
      const off = (ev) => { if (wpMenu && !wpMenu.contains(ev.target)) { closeWpMenu(); document.removeEventListener("pointerdown", off, true); } };
      document.addEventListener("pointerdown", off, true);
    }, 0);
  }
  map.on("zoomstart movestart", closeWpMenu);

  // Draggable pending waypoints (the route the user is building, pre-start).
  // Grab a marker and drag to move it; the route line follows live.
  function pendingIcon(label) {
    return L.divIcon({ className: "", html: `<div class="wp-pin">${label}</div>`, iconSize: [20, 20], iconAnchor: [10, 10] });
  }
  function drawPending() {
    pendingLayer.clearLayers();
    pendingWaypoints.forEach((w, i) => {
      const m = L.marker([w.lat, w.lon], {
        icon: pendingIcon(i + 1), draggable: true, autoPan: true, zIndexOffset: 800,
      }).addTo(pendingLayer).bindTooltip(wpLabel(w, "WP" + (i + 1)));
      m.on("drag", (e) => {
        const ll = e.target.getLatLng();
        w.lat = ll.lat; w.lon = ll.lng;
        drawWaypoints();           // route line tracks the drag
      });
      m.on("dragend", () => { drawWaypoints(); if (onWpChange) onWpChange(); });
      wireLongPress(m, () => openPendingMenu(m, i));
    });
  }
  let onWpChange = null;

  // Press-and-hold on a PENDING waypoint: speed / delete menu (mirrors the
  // committed-marker menu; edits stay local until the route is started).
  function wireLongPress(m, onFire) {
    let lpTimer = null, downPt = null;
    const clear = () => { if (lpTimer) { clearTimeout(lpTimer); lpTimer = null; } };
    const node = (m.getElement && m.getElement()) || m._icon;
    if (!node) return;
    node.addEventListener("pointerdown", (ev) => {
      downPt = { x: ev.clientX, y: ev.clientY };
      clear();
      lpTimer = setTimeout(onFire, LONGPRESS_MS);
    });
    node.addEventListener("pointermove", (ev) => {
      if (downPt && Math.hypot(ev.clientX - downPt.x, ev.clientY - downPt.y) > MOVE_TOL) clear();
    });
    ["pointerup", "pointercancel", "pointerleave"].forEach((t) => node.addEventListener(t, clear));
    m.on("dragstart", clear);
  }

  function openPendingMenu(marker, ix) {
    closeWpMenu();
    if (VA.haptic) VA.haptic("press");   // confirm the 3 s hold registered
    const pt = map.latLngToContainerPoint(marker.getLatLng());
    const menu = document.createElement("div");
    menu.className = "wp-menu glass";
    menu.innerHTML =
      `<div class="wp-menu-title">Waypoint ${ix + 1} (pending)</div>` +
      `<button type="button" data-act="speed">Set speed…</button>` +
      `<button type="button" data-act="delete" class="danger">Delete waypoint</button>` +
      `<button type="button" data-act="cancel" class="cancel">Cancel</button>`;
    menu.style.left = pt.x + "px";
    menu.style.top = pt.y + "px";
    map.getContainer().appendChild(menu);
    wpMenu = menu;
    const done = () => {
      closeWpMenu(); drawPending(); drawWaypoints();
      if (onWpChange) onWpChange();
      if (VA.routeEditor && VA.routeEditor.refresh) VA.routeEditor.refresh();
    };
    menu.querySelectorAll("button").forEach((b) => b.addEventListener("click", (e) => {
      e.stopPropagation();
      const act = b.dataset.act;
      if (act === "speed") {
        openSpeedForm(menu, pendingWaypoints[ix], `Waypoint ${ix + 1} speed`, done,
          () => closeWpMenu());
        return;
      }
      if (act === "delete") { pendingWaypoints.splice(ix, 1); done(); return; }
      closeWpMenu();
    }));
    setTimeout(() => {
      const off = (ev) => {
        if (wpMenu && !wpMenu.contains(ev.target)) {
          closeWpMenu(); document.removeEventListener("pointerdown", off, true);
        }
      };
      document.addEventListener("pointerdown", off, true);
    }, 0);
  }

  VA.onTelemetry(function renderWaypoints(t) {
    // Decimated frames OMIT `waypoints` entirely — treat absent as "no change"
    // and skip the redraw so the committed route + drag/menu state are retained.
    if (t.waypoints === undefined) return;
    drawWaypoints(t.waypoints, t.active_waypoint);
  });

  // ---- public API (extends VA.map) ---------------------------------------
  Object.assign(VA.map, {
    // pending-waypoint editor accessors
    pending() { return pendingWaypoints; },
    addPending(lat, lon) {
      pendingWaypoints.push({ name: "WP" + (++wpSeq), lat, lon });
      drawPending(); drawWaypoints();
    },
    setPending(arr) { pendingWaypoints = arr; drawPending(); drawWaypoints(); },
    onWaypointChange(fn) { onWpChange = fn; },
    // Re-send the live (committed) route after the user drags/edits its
    // waypoints. The callback receives [{name,lat,lon}].
    onRouteEdit(fn) { onRouteEdit = fn; },
    redrawWaypoints() { drawPending(); drawWaypoints(); },
    // The committed route as last seen/edited (from telemetry) + the active
    // waypoint index — for saving the active route and for Replace/Append
    // decisions when a new destination is delivered.
    committedRoute() {
      return {
        waypoints: editCommitted.map((w) => ({
          name: w.name, lat: w.lat, lon: w.lon,
          throttle_pct: w.throttle_pct ?? null, speed_kn: w.speed_kn ?? null,
        })),
        active: Math.max(0, lastActiveIx),
      };
    },
  });
})();
