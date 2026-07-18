/* Vanchor-NG — anchor marker.
 *
 * The dropped-anchor marker + its watch circle (radius = anchor_radius_m), and
 * the getLastAnchor() accessor on VA.map.
 *
 * Registers its OWN VA.onTelemetry handler for just the anchor state. Reads the
 * shared map from VA.mapCtx; extends the public VA.map. Loads after map-core.js.
 */
"use strict";

(function () {
  const VA = window.VA;
  const map = VA.mapCtx.map;

  let anchorMarker = null, anchorCircle = null, lastAnchor = null;
  let anchorHalo = null;

  // Ground resolution (m per screen pixel) at a latitude + zoom — Web Mercator.
  function metersPerPixel(lat, zoom) {
    return (156543.03392 * Math.cos(lat * Math.PI / 180)) / Math.pow(2, zoom);
  }

  // Item 21: a fixed-pixel dashed halo when the true-scale circle would render
  // smaller than 18px on screen (honesty: the true circle stays; the halo just
  // makes a tight watch ring visible). Returns the (possibly updated) halo.
  function updateHalo(halo, ll, radiusM, lat, color) {
    const px = radiusM / metersPerPixel(lat, map.getZoom());
    if (px < 18) {
      if (!halo) return L.circleMarker(ll, { radius: 18, dashArray: "4 4", fill: false, color: color, weight: 2 }).addTo(map);
      halo.setLatLng(ll);
      return halo;
    }
    if (halo) { map.removeLayer(halo); }
    return null;
  }

  function renderAnchor(t) {
    lastAnchor = t.anchor || null;
    if (t.anchor) {
      const ll = [t.anchor.lat, t.anchor.lon];
      if (!anchorMarker) {
        anchorMarker = L.marker(ll).addTo(map).bindTooltip("⚓");
        anchorCircle = L.circle(ll, { radius: t.anchor_radius_m, color: "#ff5a7a", weight: 2, fill: false }).addTo(map);
      }
      anchorMarker.setLatLng(ll);
      anchorCircle.setLatLng(ll).setRadius(t.anchor_radius_m);
      anchorHalo = updateHalo(anchorHalo, ll, t.anchor_radius_m, t.anchor.lat, "#ff5a7a");
    } else if (anchorMarker) {
      map.removeLayer(anchorMarker); map.removeLayer(anchorCircle);
      anchorMarker = anchorCircle = null;
      if (anchorHalo) { map.removeLayer(anchorHalo); anchorHalo = null; }
    }
  }
  VA.onTelemetry(renderAnchor);

  VA.map.getLastAnchor = function () { return lastAnchor; };

  let alarmMarker = null, alarmCircle = null, alarmHalo = null;

  function renderAlarmAnchor(t) {
    const aa = t.anchor_alarm;
    const show = aa && aa.armed && Number.isFinite(aa.lat) && Number.isFinite(aa.lon);
    if (show) {
      const ll = [aa.lat, aa.lon];
      const color = aa.firing ? "#ff3b30" : "#ffb020";
      if (!alarmMarker) {
        alarmMarker = L.marker(ll).addTo(map);
        // Interactive popup instead of emoji tooltip
        const rm = Number.isFinite(aa.radius_m) ? Math.round(aa.radius_m) : "?";
        const setAt = aa.set_at ? new Date(aa.set_at * 1000).toLocaleTimeString() : "—";
        alarmMarker.bindPopup(
          `<div class="aa-popup">
            <div class="aa-title">Anchor alarm — watching a ${rm} m circle</div>
            <div class="aa-detail">Set at ${setAt}</div>
            <button class="aa-clear" type="button">Clear alarm</button>
           </div>`,
          { className: "aa-popup-wrap" }
        );
        alarmMarker.on("popupopen", (e) => {
          const node = e.popup.getElement();
          const btn = node && node.querySelector(".aa-clear");
          if (btn) btn.addEventListener("click", () => {
            VA.send({ type: "anchor_alarm_clear" });
            alarmMarker.closePopup();
          });
        });
        alarmCircle = L.circle(ll, { radius: aa.radius_m, color, weight: 2,
                                     dashArray: "6 6", fill: false }).addTo(map);
      }
      alarmMarker.setLatLng(ll);
      alarmCircle.setLatLng(ll).setRadius(aa.radius_m).setStyle({ color });
      alarmHalo = updateHalo(alarmHalo, ll, aa.radius_m, aa.lat, color);
    } else if (alarmMarker) {
      map.removeLayer(alarmMarker); map.removeLayer(alarmCircle);
      alarmMarker = alarmCircle = null;
      if (alarmHalo) { map.removeLayer(alarmHalo); alarmHalo = null; }
    }
  }
  VA.onTelemetry(renderAlarmAnchor);

  // Refresh both halos on zoom (the px threshold is zoom-dependent).
  map.on("zoomend", function () {
    if (VA.last) { renderAnchor(VA.last); renderAlarmAnchor(VA.last); }
  });
})();
