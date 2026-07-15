/* Vanchor-NG — boat-icon designs.
 *
 * The selectable top-down vessel designs (#84): the SVG bodies, the shared
 * trolling-motor direction needle (MOTOR_G / BOW_X / BOW_Y), the BOAT_DESIGNS
 * registry, the divIcon builder, and the motor-indicator updater.
 *
 * Pure data + builders, no map state. Exposed on VA.mapCtx.boatIcon for
 * map-boat.js (which owns the marker + the Settings picker that lists these
 * designs). Loads after map-core.js (it writes onto VA.mapCtx).
 */
"use strict";

(function () {
  const VA = window.VA;

  // All designs are drawn bow-up (= north at heading 0°), share the same
  // 34×48-ish icon box / anchor / scale, keep the root <svg class="boat-icon">
  // so applyBoatTransform() still finds + rotates + scales it, and carry a
  // shared #motor direction-needle group near the bow so updateMotorIndicator()
  // keeps working.
  const BOW_X = 17, BOW_Y = 3.2;

  // Shared trolling-motor direction needle, appended near the bow of every
  // design. updateMotorIndicator() drives #motor-line / #motor-head.
  const MOTOR_G = `
        <circle cx="17" cy="3.2" r="2.4" fill="#04222b" stroke="#bff8ff" stroke-width="0.8"/>
        <g id="motor" transform="rotate(0 17 3.2)" style="visibility:hidden">
          <line id="motor-line" x1="17" y1="3.2" x2="17" y2="3.2"
                stroke="#22d3a6" stroke-width="2.6" stroke-linecap="round"/>
          <polygon id="motor-head" points="0,0 0,0 0,0" fill="#22d3a6"/>
        </g>`;

  // --- design bodies (everything inside <svg>, minus the motor group) -------

  // Current — the original glowing cyan vessel, kept exactly as-is.
  function bodyCurrent() {
    return `
        <defs>
          <linearGradient id="boatHull" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0" stop-color="#1be4ff"/>
            <stop offset="1" stop-color="#0a8fb0"/>
          </linearGradient>
        </defs>
        <polygon class="boat-glow" points="17,2 27,18 26,44 8,44 7,18"/>
        <polygon class="boat-hull" points="17,2 27,18 26,44 8,44 7,18"
                 fill="url(#boatHull)" stroke="#bff8ff" stroke-width="1.2" stroke-linejoin="round"/>
        <path d="M17,6 L23,18 L11,18 Z" fill="#ffffff" opacity="0.35"/>
        <rect x="12" y="24" width="10" height="8" rx="2" fill="#04222b" opacity="0.65"/>`;
  }

  // Bass boat — sleek low fishing boat: sharp pointed bow, wide flat casting
  // deck, low gunwales, console + two seats, transom outboard at the stern.
  function bodyBass() {
    return `
        <defs>
          <linearGradient id="bassHull" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0" stop-color="#ff4d6d"/>
            <stop offset="1" stop-color="#a3122e"/>
          </linearGradient>
        </defs>
        <!-- outboard at transom (stern, bottom) -->
        <rect x="14.6" y="44" width="4.8" height="4" rx="1" fill="#0c0f14" stroke="#9fb0bd" stroke-width="0.5"/>
        <line x1="17" y1="46" x2="17" y2="48.6" stroke="#9fb0bd" stroke-width="1.4" stroke-linecap="round"/>
        <!-- glow + sharp-bowed hull -->
        <path class="boat-glow" d="M17,1 C24,9 26,16 25.5,30 L24,44 L10,44 L8.5,30 C8,16 10,9 17,1 Z"/>
        <path d="M17,1 C24,9 26,16 25.5,30 L24,44 L10,44 L8.5,30 C8,16 10,9 17,1 Z"
              fill="url(#bassHull)" stroke="#ffd0d8" stroke-width="1.1" stroke-linejoin="round"/>
        <!-- bright flat casting deck up front -->
        <path d="M17,4 C22,10 23,15 22.6,22 L11.4,22 C11,15 12,10 17,4 Z" fill="#f3f6f8" opacity="0.85"/>
        <!-- low gunwale inner line -->
        <path d="M17,6 C22.5,12 24,18 23.4,40 L10.6,40 C10,18 11.5,12 17,6 Z"
              fill="none" stroke="#ffe3e9" stroke-width="0.6" opacity="0.7"/>
        <!-- console -->
        <rect x="13.4" y="25" width="7.2" height="5" rx="1.3" fill="#1a1f26" stroke="#cfd8df" stroke-width="0.5"/>
        <!-- two seats -->
        <circle cx="17" cy="33.5" r="2" fill="#1a1f26" stroke="#cfd8df" stroke-width="0.5"/>
        <circle cx="17" cy="38.5" r="2" fill="#1a1f26" stroke="#cfd8df" stroke-width="0.5"/>`;
  }

  // Titanic — ocean liner: long black hull, pointed bow + rounded stern, white
  // superstructure down the centre, 4 angled funnels, lifeboats along the sides.
  function bodyTitanic() {
    let lifeboats = "";
    for (let i = 0; i < 5; i++) {
      const y = 18 + i * 5;
      lifeboats += `<ellipse cx="11.2" cy="${y}" rx="1" ry="2" fill="#caa64a" stroke="#3a2c0f" stroke-width="0.3"/>`;
      lifeboats += `<ellipse cx="22.8" cy="${y}" rx="1" ry="2" fill="#caa64a" stroke="#3a2c0f" stroke-width="0.3"/>`;
    }
    let funnels = "";
    const fy = [14, 21, 28, 35];
    for (let i = 0; i < 4; i++) {
      // angled (raked) buff funnels with black tops
      funnels += `<g transform="rotate(-12 17 ${fy[i]})">
            <rect x="14.5" y="${fy[i] - 3}" width="5" height="6" rx="1.4" fill="#e8b04b" stroke="#5a4410" stroke-width="0.4"/>
            <rect x="14.5" y="${fy[i] - 3}" width="5" height="1.6" rx="0.8" fill="#15110a"/>
          </g>`;
    }
    return `
        <defs>
          <linearGradient id="titanHull" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0" stop-color="#2a2f36"/>
            <stop offset="1" stop-color="#0a0d11"/>
          </linearGradient>
        </defs>
        <!-- long black hull: pointed bow (top), rounded stern (bottom) -->
        <path class="boat-glow" d="M17,1 C21,5 23,9 23,14 L23,40 C23,44 20.5,47 17,47 C13.5,47 11,44 11,40 L11,14 C11,9 13,5 17,1 Z"/>
        <path d="M17,1 C21,5 23,9 23,14 L23,40 C23,44 20.5,47 17,47 C13.5,47 11,44 11,40 L11,14 C11,9 13,5 17,1 Z"
              fill="url(#titanHull)" stroke="#aeb8c2" stroke-width="0.9" stroke-linejoin="round"/>
        <!-- white superstructure down the centre -->
        <rect x="13.5" y="9" width="7" height="32" rx="2.4" fill="#eef2f5" opacity="0.95"/>
        <line x1="17" y1="9" x2="17" y2="41" stroke="#c4ccd2" stroke-width="0.4"/>
        ${lifeboats}
        ${funnels}
        <!-- forward mast hint -->
        <circle cx="17" cy="6.5" r="1.1" fill="#eef2f5"/>`;
  }

  // Narco sub — low-profile semi-submersible: narrow grey hull mostly awash
  // (translucent so it reads as barely above water), a tiny cockpit/intake hump,
  // a faint wake. Sinister and low.
  function bodyNarco() {
    return `
        <defs>
          <linearGradient id="narcoHull" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0" stop-color="#5b6670"/>
            <stop offset="1" stop-color="#2b333b"/>
          </linearGradient>
        </defs>
        <!-- faint wake astern -->
        <path d="M13,44 Q17,52 21,44" fill="none" stroke="#7fd9ff" stroke-width="1" opacity="0.25"/>
        <!-- awash outer wash (very translucent) -->
        <path d="M17,4 C20.5,12 21,20 21,30 L20,44 L14,44 L13,30 C13,20 13.5,12 17,4 Z"
              fill="#9fdcff" opacity="0.14"/>
        <!-- narrow low hull, translucent = barely above water -->
        <path class="boat-glow" d="M17,6 C19.6,13 20,20 20,30 L19,43 L15,43 L14,30 C14,20 14.4,13 17,6 Z" style="opacity:0.35"/>
        <path d="M17,6 C19.6,13 20,20 20,30 L19,43 L15,43 L14,30 C14,20 14.4,13 17,6 Z"
              fill="url(#narcoHull)" stroke="#8a99a6" stroke-width="0.7" stroke-linejoin="round" opacity="0.78"/>
        <!-- tiny cockpit / intake hump -->
        <ellipse cx="17" cy="22" rx="2.1" ry="3" fill="#10151a" stroke="#9fb0bd" stroke-width="0.5" opacity="0.95"/>
        <rect x="16.3" y="14" width="1.4" height="3" rx="0.5" fill="#0a0e12"/>`;
  }

  // Yellow submarine — Beatles-style: rounded yellow hull, conning tower with a
  // periscope, round portholes along the side, tail fins + a hint of propeller.
  function bodyYellowSub() {
    let ports = "";
    for (let i = 0; i < 4; i++) {
      const y = 16 + i * 6;
      ports += `<circle cx="17" cy="${y}" r="1.5" fill="#bfeaff" stroke="#1c3a52" stroke-width="0.6"/>`;
    }
    return `
        <defs>
          <linearGradient id="subHull" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0" stop-color="#ffe14d"/>
            <stop offset="1" stop-color="#e0a400"/>
          </linearGradient>
        </defs>
        <!-- propeller hint at stern -->
        <line x1="13.5" y1="46" x2="20.5" y2="46" stroke="#cfd8df" stroke-width="1.2" stroke-linecap="round"/>
        <circle cx="17" cy="46" r="1" fill="#9fb0bd"/>
        <!-- tail fins -->
        <path d="M9,40 L14,43 L14,45 Z" fill="#caa400"/>
        <path d="M25,40 L20,43 L20,45 Z" fill="#caa400"/>
        <!-- rounded yellow hull -->
        <path class="boat-glow" d="M17,2 C22,8 22,12 22,24 C22,36 21,44 17,46 C13,44 12,36 12,24 C12,12 12,8 17,2 Z"/>
        <path d="M17,2 C22,8 22,12 22,24 C22,36 21,44 17,46 C13,44 12,36 12,24 C12,12 12,8 17,2 Z"
              fill="url(#subHull)" stroke="#fff4c2" stroke-width="1.1" stroke-linejoin="round"/>
        <!-- conning tower -->
        <rect x="14.4" y="20" width="5.2" height="8" rx="2.2" fill="#ffd400" stroke="#8a6a00" stroke-width="0.7"/>
        <!-- periscope -->
        <line x1="17" y1="20" x2="17" y2="14" stroke="#5a4400" stroke-width="1.3" stroke-linecap="round"/>
        <line x1="17" y1="14" x2="19" y2="14" stroke="#5a4400" stroke-width="1.3" stroke-linecap="round"/>
        ${ports}`;
  }

  // Kayak — slim double-ended touring hull: narrow tapered deck with a bright
  // sheer line, oval cockpit with a paddler, deck bungees fore and aft, and a
  // paddle laid across the cockpit.
  function bodyKayak() {
    return `
        <defs>
          <linearGradient id="kayakHull" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0" stop-color="#ffb020"/>
            <stop offset="1" stop-color="#c96f00"/>
          </linearGradient>
        </defs>
        <!-- slim double-ended hull (pointed both ends) -->
        <path class="boat-glow" d="M17,1 C20.5,10 21.5,16 21.5,24 C21.5,32 20.5,38 17,47 C13.5,38 12.5,32 12.5,24 C12.5,16 13.5,10 17,1 Z"/>
        <path d="M17,1 C20.5,10 21.5,16 21.5,24 C21.5,32 20.5,38 17,47 C13.5,38 12.5,32 12.5,24 C12.5,16 13.5,10 17,1 Z"
              fill="url(#kayakHull)" stroke="#ffe2b0" stroke-width="1" stroke-linejoin="round"/>
        <!-- centre seam -->
        <line x1="17" y1="3" x2="17" y2="45" stroke="#8a4d00" stroke-width="0.5" opacity="0.7"/>
        <!-- deck bungees fore + aft -->
        <path d="M14.4,10 L19.6,13 M19.6,10 L14.4,13" stroke="#2b2f36" stroke-width="0.6" opacity="0.8"/>
        <path d="M14.6,36 L19.4,39 M19.4,36 L14.6,39" stroke="#2b2f36" stroke-width="0.6" opacity="0.8"/>
        <!-- paddle laid across, ahead of the cockpit -->
        <g transform="rotate(14 17 19.5)">
          <line x1="5" y1="19.5" x2="29" y2="19.5" stroke="#e8e2d2" stroke-width="1.1" stroke-linecap="round"/>
          <ellipse cx="5.5" cy="19.5" rx="2.4" ry="1.3" fill="#e8e2d2"/>
          <ellipse cx="28.5" cy="19.5" rx="2.4" ry="1.3" fill="#e8e2d2"/>
        </g>
        <!-- oval cockpit + paddler -->
        <ellipse cx="17" cy="25.5" rx="3.6" ry="5.2" fill="#101418" stroke="#ffd68a" stroke-width="0.7"/>
        <circle cx="17" cy="24.6" r="1.7" fill="#e0a96d"/>
        <path d="M14.2,27.8 A2.8,2.2 0 0 0 19.8,27.8 L19,29.4 L15,29.4 Z" fill="#c0392b"/>`;
  }

  const BOAT_DESIGNS = {
    current: { label: "Current", body: bodyCurrent },
    bass: { label: "Bass boat", body: bodyBass },
    kayak: { label: "Kayak", body: bodyKayak },
    titanic: { label: "Titanic", body: bodyTitanic },
    narco: { label: "Narco sub", body: bodyNarco },
    yellowsub: { label: "Yellow submarine", body: bodyYellowSub },
  };

  function boatDiv(id) {
    const design = BOAT_DESIGNS[id] || BOAT_DESIGNS.current;
    const svg = `
      <svg width="34" height="48" viewBox="0 0 34 48" class="boat-icon"
           style="transform-origin:50% 50%; overflow:visible">
        ${design.body()}
        ${MOTOR_G}
      </svg>`;
    return L.divIcon({ className: "", html: svg, iconSize: [34, 48], iconAnchor: [17, 24] });
  }

  function updateMotorIndicator(svgEl, motor) {
    const g = svgEl.querySelector("#motor");
    if (!g) return;
    const line = g.querySelector("#motor-line");
    const head = g.querySelector("#motor-head");
    const ang = motor && Number.isFinite(motor.steer_angle_deg) ? motor.steer_angle_deg : null;
    const thrust = motor && Number.isFinite(motor.thrust) ? motor.thrust : null;
    if (ang === null || thrust === null || Math.abs(thrust) < 0.02) {
      g.style.visibility = "hidden";
      return;
    }
    g.style.visibility = "visible";
    g.setAttribute("transform", `rotate(${ang} ${BOW_X} ${BOW_Y})`);
    const color = thrust < 0 ? "#ffb454" : "#22d3a6"; // reverse = amber
    const len = 5 + Math.min(1, Math.abs(thrust)) * 16;
    const tipY = BOW_Y - len;
    line.setAttribute("x2", BOW_X);
    line.setAttribute("y2", tipY);
    line.setAttribute("stroke", color);
    const w = 3.4;
    head.setAttribute("points",
      `${BOW_X - w},${tipY + w} ${BOW_X + w},${tipY + w} ${BOW_X},${tipY - w}`);
    head.setAttribute("fill", color);
  }

  VA.mapCtx.boatIcon = {
    BOAT_DESIGNS,
    BOW_X, BOW_Y,
    boatDiv,
    updateMotorIndicator,
  };
})();
