/* Vanchor-NG — recorded track.
 *
 * The recorded-track polyline (the purple breadcrumb of the saved/replayed
 * track, distinct from the boat's live cyan trail in map-boat.js).
 *
 * Registers its OWN VA.onTelemetry handler for just t.track. Reads the shared
 * map from VA.mapCtx. Loads after map-core.js.
 */
"use strict";

(function () {
  const VA = window.VA;
  const map = VA.mapCtx.map;

  let trackLine = null;
  function updateTrack(track) {
    if (!track) return;                 // no track key this frame -> retain the line
    const pts = track.points;
    // Decimated frames carry `track` (scalar keys) but OMIT `points`; treat the
    // absent array as "no change" and retain the current line. Only act when
    // `points` is actually an array: draw when non-empty, clear when explicitly
    // empty.
    if (!Array.isArray(pts)) return;
    if (pts.length) {
      if (!trackLine) trackLine = L.polyline(pts, { color: "#c084fc", weight: 3, opacity: 0.85 }).addTo(map);
      else trackLine.setLatLngs(pts);
    } else if (trackLine) trackLine.setLatLngs([]);
  }

  VA.onTelemetry(function renderTrack(t) {
    updateTrack(t.track);
  });
})();
