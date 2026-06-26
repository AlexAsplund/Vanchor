/* Vanchor-NG — Trip log UI (task #66).
 *
 * Front-end for the trip-log backend (added in parallel by another agent). This
 * module self-gates: if `GET /api/trips` 404s the whole "Trips" Settings card
 * stays hidden, so the UI degrades gracefully when the backend isn't present.
 *
 * Live trip readout: reads telemetry frame field
 *   trip: {active, name, distance_m, duration_s, avg_speed_kn, max_speed_kn}
 * and shows distance / duration / avg & max speed while a trip is active, plus
 * Start trip / Stop trip buttons that send commands:
 *   {type:"trip_start", name}  /  {type:"trip_stop"}
 *
 * Past trips: `GET /api/trips` lists saved trips (name, date, distance,
 * duration, avg/max speed). Clicking one fetches `GET /api/trips/{id}` for its
 * points and draws the track on the map; each row offers Export GPX (link to
 * `GET /api/trips/{id}.gpx`) and Delete (`DELETE /api/trips/{id}`).
 */
"use strict";

(function () {
  const $ = (id) => document.getElementById(id);
  const map = window.VA && VA.map && VA.map.leaflet;
  const L = window.L;

  const card = $("trips-card");
  const trackLayer = (map && L) ? L.layerGroup() : null;
  let trackLine = null;
  let activeTripId = null;     // currently-drawn past trip
  let endpointOk = false;

  function escapeHtml(s) {
    return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }
  function fmtDist(m) {
    if (!Number.isFinite(m)) return "—";
    return m >= 1000 ? (m / 1000).toFixed(2) + " km" : Math.round(m) + " m";
  }
  function fmtDuration(s) {
    if (!Number.isFinite(s) || s < 0) return "—";
    s = Math.round(s);
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
    if (h > 0) return h + "h " + m + "m";
    if (m > 0) return m + "m " + sec + "s";
    return sec + "s";
  }
  function fmtSpeed(kn) { return Number.isFinite(kn) ? kn.toFixed(1) + " kn" : "—"; }
  function fmtDate(d) {
    if (!d) return "";
    // accept ISO strings or epoch seconds/ms
    let dt;
    if (typeof d === "number") dt = new Date(d < 1e12 ? d * 1000 : d);
    else dt = new Date(d);
    return isNaN(dt.getTime()) ? String(d) : dt.toISOString().slice(0, 16).replace("T", " ");
  }

  // ---- live trip readout (telemetry) -------------------------------------
  function updateLive(t) {
    if (!endpointOk) return;
    const trip = t && t.trip ? t.trip : null;
    const active = !!(trip && trip.active);
    const live = $("trip-live");
    if (live) {
      live.classList.toggle("trip-live-idle", !active);
      live.classList.toggle("trip-live-active", active);
    }
    VA.setText("trip-live-name", active ? (trip.name || "Trip") : "No active trip");
    VA.setText("trip-dist", active ? fmtDist(Number(trip.distance_m)) : "—");
    VA.setText("trip-dur", active ? fmtDuration(Number(trip.duration_s)) : "—");
    VA.setText("trip-avg", active ? fmtSpeed(Number(trip.avg_speed_kn)) : "—");
    VA.setText("trip-max", active ? fmtSpeed(Number(trip.max_speed_kn)) : "—");
    const startBtn = $("trip-start"), stopBtn = $("trip-stop"), nameInput = $("trip-name");
    if (startBtn) startBtn.classList.toggle("hidden", active);
    if (nameInput) nameInput.classList.toggle("hidden", active);
    if (stopBtn) stopBtn.classList.toggle("hidden", !active);
    const badge = $("trips-card-state");
    if (badge) badge.textContent = active ? "● REC" : "";
  }
  if (VA.onTelemetry) VA.onTelemetry(updateLive);

  // ---- start / stop commands ---------------------------------------------
  const startBtn = $("trip-start");
  if (startBtn) startBtn.addEventListener("click", () => {
    const nameInput = $("trip-name");
    const name = (nameInput && nameInput.value.trim()) || ("Trip " + new Date().toISOString().slice(0, 16).replace("T", " "));
    if (VA.send) VA.send({ type: "trip_start", name });
    if (nameInput) nameInput.value = "";
    const st = $("trip-status"); if (st) { st.className = "hint ok"; st.textContent = "Trip started."; }
  });
  const stopBtn = $("trip-stop");
  if (stopBtn) stopBtn.addEventListener("click", () => {
    if (VA.send) VA.send({ type: "trip_stop" });
    const st = $("trip-status"); if (st) { st.className = "hint ok"; st.textContent = "Trip stopped — saved."; }
    setTimeout(loadTrips, 600);   // give the backend a moment to persist
  });

  // ---- past-trips list ----------------------------------------------------
  async function loadTrips() {
    if (!card) return;
    let trips;
    try {
      const r = await fetch("/api/trips");
      if (r.status === 404) { gate(false); return; }
      if (!r.ok) throw new Error("status " + r.status);
      trips = await r.json();
    } catch (e) {
      // network error or backend absent → keep the card hidden / degrade.
      if (!endpointOk) { gate(false); return; }
      return;
    }
    gate(true);
    if (trips && Array.isArray(trips.trips)) trips = trips.trips;   // accept {trips:[...]} or [...]
    renderList(Array.isArray(trips) ? trips : []);
  }

  function gate(ok) {
    endpointOk = ok;
    if (card) card.classList.toggle("hidden", !ok);
  }

  function renderList(trips) {
    const list = $("trips-list");
    if (!list) return;
    list.innerHTML = "";
    if (!trips.length) {
      const li = document.createElement("li");
      li.className = "wp-empty"; li.textContent = "No saved trips yet.";
      list.appendChild(li);
      return;
    }
    trips.forEach((tr) => {
      const id = tr.id != null ? tr.id : tr.name;
      const li = document.createElement("li");
      li.className = "trip-row" + (String(id) === String(activeTripId) ? " trip-row-active" : "");

      const info = document.createElement("button");
      info.type = "button"; info.className = "trip-info";
      info.innerHTML =
        `<b>${escapeHtml(tr.name || ("Trip " + id))}</b>` +
        `<small>${escapeHtml(fmtDate(tr.date || tr.started_at || tr.start_time))}</small>` +
        `<span class="trip-row-stats">${fmtDist(Number(tr.distance_m))} · ${fmtDuration(Number(tr.duration_s))} · ` +
        `avg ${fmtSpeed(Number(tr.avg_speed_kn))} · max ${fmtSpeed(Number(tr.max_speed_kn))}</span>`;
      info.addEventListener("click", () => showTrack(id, li));

      const gpx = document.createElement("a");
      gpx.className = "trip-act trip-gpx"; gpx.textContent = "GPX"; gpx.title = "Export GPX";
      gpx.href = "/api/trips/" + encodeURIComponent(id) + ".gpx";
      gpx.setAttribute("download", (tr.name || ("trip-" + id)).replace(/[^\w.-]+/g, "_") + ".gpx");

      const del = document.createElement("button");
      del.type = "button"; del.className = "trip-act trip-del"; del.textContent = "✕"; del.title = "Delete trip";
      del.addEventListener("click", () => deleteTrip(id));

      li.append(info, gpx, del);
      list.appendChild(li);
    });
  }

  async function showTrack(id, li) {
    if (!map || !trackLayer) return;
    try {
      const r = await fetch("/api/trips/" + encodeURIComponent(id));
      if (!r.ok) throw new Error("status " + r.status);
      const data = await r.json();
      const pts = extractPoints(data);
      if (!pts.length) { setStatus("Trip has no track points.", "err"); return; }
      drawTrack(pts);
      activeTripId = id;
      // highlight selected row
      const list = $("trips-list");
      if (list) list.querySelectorAll(".trip-row").forEach((el) => el.classList.remove("trip-row-active"));
      if (li) li.classList.add("trip-row-active");
      const bounds = L.latLngBounds(pts);
      if (bounds.isValid()) map.fitBounds(bounds, { padding: [40, 40], maxZoom: 17 });
      // close the settings drawer so the track is visible on phones
      const settings = $("settings"); if (settings) settings.classList.add("hidden");
      setStatus("Showing trip track (" + pts.length + " points).", "ok");
    } catch (e) {
      setStatus("Couldn't load trip track.", "err");
    }
  }

  function extractPoints(data) {
    // accept {points:[[lat,lon],...]}, {points:[{lat,lon}]}, or {track:{points}}
    let pts = (data && (data.points || (data.track && data.track.points))) || [];
    if (!Array.isArray(pts)) return [];
    return pts.map((p) => {
      if (Array.isArray(p)) return [p[0], p[1]];
      if (p && Number.isFinite(p.lat) && Number.isFinite(p.lon)) return [p.lat, p.lon];
      return null;
    }).filter((p) => p && Number.isFinite(p[0]) && Number.isFinite(p[1]));
  }

  function drawTrack(pts) {
    if (!trackLayer.getLayers().length || !trackLine) {
      trackLayer.clearLayers();
      trackLine = L.polyline(pts, { color: "#8b7bff", weight: 4, opacity: 0.9 });
      trackLayer.addLayer(trackLine);
      const start = L.circleMarker(pts[0], { radius: 6, color: "#2ce8b0", fillColor: "#2ce8b0", fillOpacity: 0.9, weight: 1 }).bindTooltip("Start");
      const end = L.circleMarker(pts[pts.length - 1], { radius: 6, color: "#ff5d7e", fillColor: "#ff5d7e", fillOpacity: 0.9, weight: 1 }).bindTooltip("End");
      trackLayer.addLayer(start); trackLayer.addLayer(end);
    } else {
      trackLayer.clearLayers();
      trackLine = L.polyline(pts, { color: "#8b7bff", weight: 4, opacity: 0.9 });
      trackLayer.addLayer(trackLine);
      trackLayer.addLayer(L.circleMarker(pts[0], { radius: 6, color: "#2ce8b0", fillColor: "#2ce8b0", fillOpacity: 0.9, weight: 1 }).bindTooltip("Start"));
      trackLayer.addLayer(L.circleMarker(pts[pts.length - 1], { radius: 6, color: "#ff5d7e", fillColor: "#ff5d7e", fillOpacity: 0.9, weight: 1 }).bindTooltip("End"));
    }
    if (!map.hasLayer(trackLayer)) trackLayer.addTo(map);
  }

  async function deleteTrip(id) {
    if (!window.confirm("Delete this trip? This cannot be undone.")) return;
    try {
      const r = await fetch("/api/trips/" + encodeURIComponent(id), { method: "DELETE" });
      if (!r.ok && r.status !== 204) throw new Error("status " + r.status);
      if (String(id) === String(activeTripId)) { clearTrack(); activeTripId = null; }
      setStatus("Trip deleted.", "ok");
      loadTrips();
    } catch (e) {
      setStatus("Couldn't delete trip.", "err");
    }
  }

  function clearTrack() {
    if (trackLayer && map && map.hasLayer(trackLayer)) map.removeLayer(trackLayer);
    if (trackLayer) trackLayer.clearLayers();
    trackLine = null;
  }

  function setStatus(msg, kind) {
    const st = $("trip-status");
    if (st) { st.className = "hint" + (kind ? " " + kind : ""); st.textContent = msg; }
  }

  // ---- refresh the list when the Trips card is opened ---------------------
  if (card) card.addEventListener("toggle", () => { if (card.open) loadTrips(); });

  // ---- init: probe the endpoint once so the card self-gates --------------
  loadTrips();

  VA.trips = { reload: loadTrips, clearTrack };
})();
