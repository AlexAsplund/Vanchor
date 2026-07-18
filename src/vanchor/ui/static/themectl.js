/* Vanchor-NG — sun/moon quick-toggle map control (WP9 S2).
 *
 * A single-button Leaflet control in the topleft stack that switches between
 * daylight (sun icon) and dark (moon icon) without going into the settings
 * drawer. Mirrors the state from VA.theme (exported by settings.js). Position:
 * bottom of the topleft stack, below the N-UP rotate control.
 */
"use strict";

(function () {
  function boot() {
    if (!window.VA || !VA.map || !VA.map.leaflet || !window.L || !VA.theme) {
      return setTimeout(boot, 200);
    }
    const map = VA.map.leaflet;

    // SVG icons (inline, no external deps)
    const SUN_SVG = "<svg viewBox=\"0 0 24 24\" width=\"20\" height=\"20\" fill=\"none\""
      + " stroke=\"currentColor\" stroke-width=\"1.8\" stroke-linecap=\"round\""
      + " stroke-linejoin=\"round\" aria-hidden=\"true\">"
      + "<circle cx=\"12\" cy=\"12\" r=\"5\"/>"
      + "<line x1=\"12\" y1=\"1\" x2=\"12\" y2=\"3\"/>"
      + "<line x1=\"12\" y1=\"21\" x2=\"12\" y2=\"23\"/>"
      + "<line x1=\"4.22\" y1=\"4.22\" x2=\"5.64\" y2=\"5.64\"/>"
      + "<line x1=\"18.36\" y1=\"18.36\" x2=\"19.78\" y2=\"19.78\"/>"
      + "<line x1=\"1\" y1=\"12\" x2=\"3\" y2=\"12\"/>"
      + "<line x1=\"21\" y1=\"12\" x2=\"23\" y2=\"12\"/>"
      + "<line x1=\"4.22\" y1=\"19.78\" x2=\"5.64\" y2=\"18.36\"/>"
      + "<line x1=\"18.36\" y1=\"5.64\" x2=\"19.78\" y2=\"4.22\"/>"
      + "</svg>";

    const MOON_SVG = "<svg viewBox=\"0 0 24 24\" width=\"20\" height=\"20\" fill=\"none\""
      + " stroke=\"currentColor\" stroke-width=\"1.8\" stroke-linecap=\"round\""
      + " stroke-linejoin=\"round\" aria-hidden=\"true\">"
      + "<path d=\"M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z\"/>"
      + "</svg>";

    let btn = null;
    function updateIcon() {
      if (!btn) return;
      const isDaylight = VA.theme.current() === "daylight";
      // In daylight: show moon (clicking will go dark)
      // In dark: show sun (clicking will go daylight)
      btn.innerHTML = isDaylight ? MOON_SVG : SUN_SVG;
      btn.setAttribute("aria-label",
        isDaylight ? "Switch to dark theme" : "Switch to daylight theme");
      btn.setAttribute("title",
        isDaylight ? "Dark theme" : "Daylight theme");
    }

    const Ctl = L.Control.extend({
      options: { position: "topleft" },
      onAdd: function() {
        const wrap = L.DomUtil.create("div", "leaflet-bar theme-ctl");
        btn = L.DomUtil.create("a", "theme-btn", wrap);
        btn.href = "#";
        btn.setAttribute("role", "button");
        L.DomEvent.disableClickPropagation(wrap);
        L.DomEvent.on(btn, "click", function(e) {
          L.DomEvent.stop(e);
          const next = VA.theme.current() === "daylight" ? "dark" : "daylight";
          VA.theme.set(next);
        });
        updateIcon();
        return wrap;
      },
    });
    map.addControl(new Ctl());

    // Keep icon in sync with theme changes
    window.addEventListener("va:theme", updateIcon);
  }
  boot();
})();
