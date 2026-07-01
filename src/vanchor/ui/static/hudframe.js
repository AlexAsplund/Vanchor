/* Vanchor-NG — tactical HUD frame.
 * Drives the aircraft-style scrolling heading tape and keeps the range-ring
 * reticle centred on the boat. Decorative; reads heading from telemetry. */
"use strict";
(function () {
  const track = document.getElementById("ht-track");
  const read = document.getElementById("ht-read");
  if (!track) return;

  // Cache the tape container width to avoid a forced synchronous layout on
  // every telemetry frame.  Refreshed on resize, orientationchange, and when
  // the HUD frame is toggled (which may affect the layout).
  let cachedWidth = track.parentElement.clientWidth;
  function refreshWidth() { cachedWidth = track.parentElement.clientWidth; }
  window.addEventListener("resize", refreshWidth);
  window.addEventListener("orientationchange", refreshWidth);

  const PX = 4;          // pixels per degree
  const MIN = -100, MAX = 460;
  const cardinals = { 0: "N", 90: "E", 180: "S", 270: "W" };
  let html = "";
  for (let d = MIN; d <= MAX; d += 5) {
    const x = (d - MIN) * PX;
    const maj = d % 30 === 0;
    html += `<div class="ht-tick ${maj ? "maj" : ""}" style="left:${x}px"></div>`;
    if (maj) {
      const dd = ((d % 360) + 360) % 360;
      const card = cardinals[dd];
      html += `<div class="ht-lbl ${card ? "card" : ""}" style="left:${x}px">${card || String(dd).padStart(3, "0")}</div>`;
    }
  }
  track.innerHTML = html;
  track.style.width = (MAX - MIN) * PX + "px";

  // Keep the range-ring reticle on the boat (fall back to screen centre).
  const center = document.getElementById("tac-center");
  function recentre(t) {
    if (!center || !window.VA || !VA.map || !VA.map.leaflet) return;
    const truth = t.truth, pos = t.position;
    const lat = truth ? truth.lat : pos ? pos.lat : null;
    const lon = truth ? truth.lon : pos ? pos.lon : null;
    if (lat == null || !Number.isFinite(lat)) return;
    try {
      const p = VA.map.leaflet.latLngToContainerPoint([lat, lon]);
      center.style.left = p.x + "px";
      center.style.top = p.y + "px";
    } catch (e) { /* map not ready */ }
  }

  // Light low-pass on an unwrapped heading so the tape glides (no sensor-noise
  // jitter), tracking the fractional degree; the CSS transition smooths frames.
  // Smoothed, continuous heading kept in [0,360). When it crosses the 0/360
  // boundary we shift by 360 with the CSS transition momentarily off, so the
  // tape glides through the wrap instead of spinning the long way (the ticks
  // repeat every 360°, so the shift is visually seamless).
  let disp = null;
  VA.onTelemetry(function (t) {
    const h = VA.fin(t.heading_deg);
    if (h !== null) {
      if (disp === null) disp = ((h % 360) + 360) % 360;
      else disp += (((h - disp + 540) % 360) - 180) * 0.4;  // low-pass, shortest way
      let jumped = false;
      while (disp < 0) { disp += 360; jumped = true; }
      while (disp >= 360) { disp -= 360; jumped = true; }
      const win = cachedWidth;
      const x = win / 2 - (disp - MIN) * PX;
      if (jumped) {
        const prev = track.style.transition;
        track.style.transition = "none";
        track.style.transform = `translateX(${x}px)`;
        void track.offsetWidth;            // force reflow so the jump isn't animated
        track.style.transition = prev;
      } else {
        track.style.transform = `translateX(${x}px)`;
      }
      if (read) read.textContent = disp.toFixed(1).padStart(5, "0") + "°";
    }
    recentre(t);
  });

  // --- tactical HUD frame toggle (Settings; OFF by default) ---
  const FRAME_KEY = "vanchor-hudframe";
  const frameBox = document.getElementById("hud-frame-toggle");
  const applyFrame = (on) => {
    document.body.classList.toggle("hud-frame-on", on);
    if (frameBox) frameBox.checked = on;
    refreshWidth();  // class change may affect tape container width
  };
  let frameOn = false;
  try { frameOn = localStorage.getItem(FRAME_KEY) === "1"; } catch (e) { /* ignore */ }
  applyFrame(frameOn);
  if (frameBox) frameBox.addEventListener("change", () => {
    applyFrame(frameBox.checked);
    try { localStorage.setItem(FRAME_KEY, frameBox.checked ? "1" : "0"); } catch (e) { /* ignore */ }
  });

  // --- collapsible control dock on phones (starts collapsed for max chart) ---
  const handle = document.getElementById("dock-handle");
  if (handle) {
    if (window.matchMedia("(max-width: 760px)").matches) document.body.classList.add("dock-collapsed");
    handle.addEventListener("click", () => document.body.classList.toggle("dock-collapsed"));
  }
})();
