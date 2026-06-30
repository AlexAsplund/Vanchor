/* Vanchor-NG — route editor + go-to + ETA module.
 *
 * The waypoint route editor (arm/add/list/start), live editing of an active
 * route, the island-loop flag, the tap-map go-to action, and the top-bar route
 * progress chip (traveled ▸ remaining + time taken / time left ETA).
 *
 * Exposes VA.routeEditor for other modules (smart routing, saved routes, island
 * loops) to refresh the editor, start the route, or mark it a continuous loop.
 */
"use strict";

(function () {
  const { $, send, modeCommands } = VA.ui;

  // ===== route editor + go-to ===============================================
  // Waypoints are only dropped while "Add waypoints" mode is armed, so an ordinary
  // map tap (panning, deselecting) doesn't litter the route with pins.
  let wpArmed = false;
  function setWpArmed(on) {
    wpArmed = on;
    const b = $("wp-arm");
    if (b) { b.classList.toggle("active", on); b.textContent = on ? "✓ Tap map to add — done" : "＋ Add waypoints"; }
    if (on) setGotoArmed(false);  // mutually exclusive with Go-to
  }
  VA.map.setOnMapClick((lat, lon, armed) => {
    if (armed) { gotoTo(lat, lon); setGotoArmed(false); return; }
    if (!wpArmed) return;          // not in add-waypoint mode -> ignore the tap
    VA.map.addPending(lat, lon);
    renderWpList();
  });
  const wpArmBtn = $("wp-arm");
  if (wpArmBtn) wpArmBtn.addEventListener("click", () => setWpArmed(!wpArmed));

  function renderWpList() {
    const list = $("wp-list");
    if (!list) return;
    list.innerHTML = "";
    const pending = VA.map.pending();
    if (!pending.length) {
      const li = document.createElement("li");
      li.className = "wp-empty"; li.textContent = "No pending waypoints.";
      list.appendChild(li);
      VA.map.redrawWaypoints();
      return;
    }
    pending.forEach((w, i) => {
      const li = document.createElement("li");
      const ix = document.createElement("span");
      ix.className = "ix"; ix.textContent = (i + 1) + ".";
      const name = document.createElement("input");
      name.className = "wp-name"; name.type = "text"; name.value = w.name;
      name.setAttribute("aria-label", "waypoint name");
      name.addEventListener("input", () => { w.name = name.value; });
      const del = document.createElement("button");
      del.className = "del"; del.textContent = "✕";
      del.setAttribute("aria-label", "delete waypoint");
      del.addEventListener("click", () => { pending.splice(i, 1); renderWpList(); });
      li.append(ix, name, del);
      list.appendChild(li);
    });
    VA.map.redrawWaypoints();
  }

  // Loop-route flag: when an "island loop" route is loaded into the editor, the
  // start command must carry `loop:true` so the boat circles continuously. The
  // island module (island.js) sets this via VA.routeEditor.setLoop(true). It is
  // cleared whenever a *normal* route is loaded/started or the route is cleared.
  let routeIsLoop = false;
  function setLoopFlag(on) {
    routeIsLoop = !!on;
    updateLoopIndicator();
  }
  function updateLoopIndicator(active) {
    // Show while a loop route is pending (loaded, unstarted) or active (boat in
    // waypoint mode running a loop route the user just started).
    const show = routeIsLoop || !!active;
    const el = $("loop-indicator");
    if (el) el.classList.toggle("hidden", !show);
    // Keep the island card's summary badge in sync (it's set when a loop loads).
    const badge = $("island-loop-badge");
    if (badge) badge.classList.toggle("hidden", !show);
  }

  // Patrol flag: at each END of the route, reverse and run it back -- a
  // there-and-back patrol (distinct from the closed island loop). Driven by the
  // #wp-patrol checkbox in the route editor.
  const patrolBox = $("wp-patrol");
  const routeIsPatrol = () => !!(patrolBox && patrolBox.checked);
  function updatePatrolIndicator(active) {
    const el = $("patrol-indicator");
    if (el) el.classList.toggle("hidden", !(routeIsPatrol() || !!active));
  }
  if (patrolBox) patrolBox.addEventListener("change", () => updatePatrolIndicator());

  function startRoute() {
    const pending = VA.map.pending();
    if (!pending.length) { VA.logLine("start route: no waypoints"); return; }
    const cmd = { type: "goto", waypoints: pending.map((w) => ({ name: w.name, lat: w.lat, lon: w.lon })), throttle: 0.6 };
    if (routeIsLoop) cmd.loop = true;       // circle continuously around the island
    if (routeIsPatrol()) cmd.patrol = true; // run the route there-and-back continuously
    send(cmd);
    // The route is now ACTIVE — its committed waypoints come back via telemetry
    // and render as the active (coloured) route. Clear the editable "not started"
    // pins so they don't sit on top of and hide the active ones.
    VA.map.setPending([]);
    renderWpList();
    setWpArmed(false);
    // Keep routeIsLoop set so the live route-edit re-send (below) preserves the
    // loop, and the indicator stays lit while the loop route runs.
  }

  // Route/Waypoint is a setup-style mode: clicking the rail builds the route.
  modeCommands.waypoint = startRoute;

  // Let other modules (smart routing, saved routes, live route editing) refresh
  // the editor list after they inject pending waypoints via VA.map.setPending(...),
  // or re-send the active route after an edit. setLoop()/clearLoop() let the
  // island module mark the pending route as a continuous loop.
  VA.routeEditor = {
    refresh: renderWpList,
    startRoute,
    setLoop: setLoopFlag,
    clearLoop: () => setLoopFlag(false),
    isLoop: () => routeIsLoop,
  };

  // ---- top-bar route progress: traveled ▸ remaining (#69) ----
  function fmtDist(m) {
    if (!Number.isFinite(m) || m < 0) return "—";
    return m < 1000 ? Math.round(m) + " m" : (m / 1000).toFixed(m < 10000 ? 1 : 0) + " km";
  }
  function haversineM(aLat, aLon, bLat, bLon) {
    const R = 6371000, rad = Math.PI / 180;
    const dLat = (bLat - aLat) * rad, dLon = (bLon - aLon) * rad;
    const h = Math.sin(dLat / 2) ** 2 +
      Math.cos(aLat * rad) * Math.cos(bLat * rad) * Math.sin(dLon / 2) ** 2;
    return 2 * R * Math.asin(Math.min(1, Math.sqrt(h)));
  }
  // mm:ss (or h:mm:ss) clock formatting for the route chip ETA (#100).
  function fmtClock(s) {
    if (!Number.isFinite(s) || s < 0) return "—";
    s = Math.round(s);
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), ss = s % 60;
    const pad = (n) => String(n).padStart(2, "0");
    return h > 0 ? `${h}:${pad(m)}:${pad(ss)}` : `${m}:${pad(ss)}`;
  }
  // Route ETA state (#100): start time of the active route + a rolling SOG window.
  let routeStartMs = null;          // when the route first became active
  const SOG_WINDOW_MS = 45000;      // rolling-average window
  const SOG_MIN_MS = 0.05;          // ignore near-zero samples (~0.1 kn)
  const sogSamples = [];            // [{ ms, mps }]
  function rollingSpeedMps(nowMs) {
    // Drop samples older than the window, then average the (non-near-zero) ones.
    while (sogSamples.length && nowMs - sogSamples[0].ms > SOG_WINDOW_MS) sogSamples.shift();
    let sum = 0, n = 0;
    for (const s of sogSamples) { if (s.mps > SOG_MIN_MS) { sum += s.mps; n++; } }
    return n ? sum / n : null;
  }
  VA.onTelemetry((t) => {
    const chip = $("chip-route");
    if (!chip) return;
    const nowMs = Date.now();
    // Always feed the rolling speed window from SOG (knots → m/s) so it's warm.
    const sogKn = VA.fin ? VA.fin(t.sog_knots) : (Number.isFinite(t.sog_knots) ? t.sog_knots : null);
    if (sogKn !== null) sogSamples.push({ ms: nowMs, mps: sogKn * 0.514444 });

    const wps = Array.isArray(t.waypoints) ? t.waypoints : [];
    const pos = t.position || t.truth;
    if (t.mode !== "waypoint" || wps.length < 1 || !pos) {
      chip.classList.add("hidden");
      routeStartMs = null;   // route no longer active — reset elapsed clock
      return;
    }
    const active = Math.max(0, Math.min(wps.length - 1, Number.isInteger(t.active_waypoint) ? t.active_waypoint : 0));
    let traveled = 0, remainingLegs = 0;
    for (let i = 0; i < wps.length - 1; i++) {
      const d = haversineM(wps[i].lat, wps[i].lon, wps[i + 1].lat, wps[i + 1].lon);
      if (i < active) traveled += d; else remainingLegs += d;
    }
    const remaining = haversineM(pos.lat, pos.lon, wps[active].lat, wps[active].lon) + remainingLegs;
    VA.setText("chip-route-trav", fmtDist(traveled));
    VA.setText("chip-route-left", fmtDist(remaining));

    // ---- time taken / time left (#100) ----
    // Mark the route active the first frame remaining distance is finite.
    if (routeStartMs === null && Number.isFinite(remaining)) routeStartMs = nowMs;
    const takenS = routeStartMs !== null ? (nowMs - routeStartMs) / 1000 : null;
    const avgMps = rollingSpeedMps(nowMs);
    const leftS = (avgMps && Number.isFinite(remaining)) ? remaining / avgMps : null;
    VA.setText("chip-route-taken", takenS === null ? "—" : fmtClock(takenS));
    VA.setText("chip-route-tleft", leftS === null ? "—" : fmtClock(leftS));

    chip.classList.remove("hidden");
  });

  // Keep the loop indicator lit while an active loop route is running on the boat
  // (telemetry reflects it). Falls back to the pending flag when not in waypoint.
  VA.onTelemetry((t) => {
    const activeLoop = t && t.mode === "waypoint" && t.loop === true;
    updateLoopIndicator(activeLoop);
    updatePatrolIndicator(t && t.mode === "waypoint" && t.route_patrol === true);
  });

  $("wp-go").addEventListener("click", startRoute);
  $("wp-clear").addEventListener("click", () => {
    VA.map.setPending([]); renderWpList(); setWpArmed(false);
    setLoopFlag(false);   // clearing the route drops any island-loop flag
  });

  // Live editing of an ACTIVE route: when the user drags or edits a committed
  // waypoint on the map, re-send the whole route so navigation adjusts live (#51).
  if (VA.map.onRouteEdit) VA.map.onRouteEdit((waypoints, resume) => {
    if (!waypoints || !waypoints.length) { send({ type: "stop" }); setLoopFlag(false); return; }
    const cmd = { type: "goto", waypoints, throttle: 0.6 };
    if (routeIsLoop) cmd.loop = true;   // (loop/patrol also preserved server-side on edits)
    // active = resume index: keep navigating from the current target instead of
    // restarting at waypoint 1 when a committed waypoint is dragged/edited (#51).
    if (Number.isInteger(resume)) cmd.active = resume;
    send(cmd);
  });

  // go-to (tap map)
  const gotoArm = $("goto-arm");
  const gotoAction = $("goto-action");
  function setGotoArmed(on) {
    VA.map.setGotoArmed(on);
    if (gotoArm) {
      gotoArm.classList.toggle("active", on);
      gotoArm.textContent = on ? "Tap the map… (cancel)" : "Tap map to go";
    }
  }
  function gotoTo(lat, lon) {
    const on_arrival = gotoAction ? gotoAction.value : "anchor";
    VA.map.setGotoMarker(lat, lon);
    send({ type: "goto", waypoints: [{ name: "GOTO", lat, lon }], throttle: 0.6, on_arrival });
  }
  if (gotoArm) gotoArm.addEventListener("click", () => setGotoArmed(!VA.map.isGotoArmed()));

  // ---- Along contour: tap a depth contour -> a track that follows it -------
  // Arms a click consumer; the tapped point is sent to /api/route/contour, which
  // returns the chained isobath as waypoints. They load into the editor (above)
  // for review + Start (Patrol optional); a closed contour auto-sets Loop.
  let contourArmed = false;
  const contourBtn = $("wp-contour"), contourStatus = $("contour-status");
  function setContourArmed(on) {
    contourArmed = on;
    if (contourBtn) {
      contourBtn.classList.toggle("active", on);
      contourBtn.textContent = on ? "Tap a contour… (cancel)" : "▽ Pick a contour";
    }
    if (on && VA.map.setContourShow) VA.map.setContourShow(true);  // show contours to tap
  }
  if (contourBtn) contourBtn.addEventListener("click", () => setContourArmed(!contourArmed));
  VA.map.addClickConsumer((lat, lon) => {
    if (!contourArmed) return false;        // not our turn -> let other handlers run
    setContourArmed(false);
    if (contourStatus) contourStatus.textContent = "Finding contour…";
    VA.postJSON("/api/route/contour", { lat, lon }).then((r) => {
      if (!r || !r.ok || !r.waypoints || !r.waypoints.length) {
        if (contourStatus) contourStatus.textContent = (r && r.message) || "No contour there.";
        return;
      }
      VA.map.setPending(r.waypoints); renderWpList(); setWpArmed(false);
      setLoopFlag(!!r.loop);               // closed isobath -> loop
      if (contourStatus) contourStatus.textContent = r.message + " Review, then Start route.";
    }).catch(() => { if (contourStatus) contourStatus.textContent = "Contour lookup failed."; });
    return true;                            // consumed the click
  });

  // Render the (empty) waypoint list once at startup.
  renderWpList();
})();
