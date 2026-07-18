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

  VA.onTelemetry(function renderAnchor(t) {
    lastAnchor = t.anchor || null;
    if (t.anchor) {
      const ll = [t.anchor.lat, t.anchor.lon];
      if (!anchorMarker) {
        anchorMarker = L.marker(ll).addTo(map).bindTooltip("⚓");
        anchorCircle = L.circle(ll, { radius: t.anchor_radius_m, color: "#ff5a7a", weight: 2, fill: false }).addTo(map);
      }
      anchorMarker.setLatLng(ll);
      anchorCircle.setLatLng(ll).setRadius(t.anchor_radius_m);
    } else if (anchorMarker) {
      map.removeLayer(anchorMarker); map.removeLayer(anchorCircle);
      anchorMarker = anchorCircle = null;
    }
  });

  VA.map.getLastAnchor = function () { return lastAnchor; };

  let alarmMarker = null, alarmCircle = null;

  VA.onTelemetry(function renderAlarmAnchor(t) {
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
    } else if (alarmMarker) {
      map.removeLayer(alarmMarker); map.removeLayer(alarmCircle);
      alarmMarker = alarmCircle = null;
    }
  });
})();
