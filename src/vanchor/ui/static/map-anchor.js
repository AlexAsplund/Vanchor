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
})();
