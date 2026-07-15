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

  // Rubber duck — the bathtub classic from above: round yellow body, round
  // head up front with an orange beak at the bow, wing bumps, a tail nub.
  function bodyDuck() {
    return `
        <defs>
          <radialGradient id="duckBody" cx="50%" cy="42%">
            <stop offset="0" stop-color="#ffe14d"/>
            <stop offset="1" stop-color="#f0b400"/>
          </radialGradient>
          <radialGradient id="duckHead" cx="50%" cy="40%">
            <stop offset="0" stop-color="#ffe97a"/>
            <stop offset="1" stop-color="#f5c211"/>
          </radialGradient>
        </defs>
        <!-- glow silhouette -->
        <ellipse class="boat-glow" cx="17" cy="30" rx="11" ry="15"/>
        <!-- beak (the bow) -->
        <path d="M17,1.5 C19.6,2 20.8,3.4 20.6,5.2 C20.4,6.9 18.9,7.8 17,7.8 C15.1,7.8 13.6,6.9 13.4,5.2 C13.2,3.4 14.4,2 17,1.5 Z"
              fill="#ff8a1e" stroke="#c95f00" stroke-width="0.7"/>
        <line x1="14" y1="5" x2="20" y2="5" stroke="#c95f00" stroke-width="0.5" opacity="0.8"/>
        <!-- head -->
        <circle cx="17" cy="12.5" r="7.2" fill="url(#duckHead)" stroke="#c98f00" stroke-width="0.9"/>
        <!-- eyes -->
        <circle cx="13.7" cy="10.6" r="1.25" fill="#181a1e"/>
        <circle cx="20.3" cy="10.6" r="1.25" fill="#181a1e"/>
        <circle cx="14.1" cy="10.2" r="0.4" fill="#ffffff"/>
        <circle cx="20.7" cy="10.2" r="0.4" fill="#ffffff"/>
        <!-- body -->
        <path d="M17,17 C24,17 27.5,22 27.5,29 C27.5,37.5 23.5,44.5 17,45.5 C10.5,44.5 6.5,37.5 6.5,29 C6.5,22 10,17 17,17 Z"
              fill="url(#duckBody)" stroke="#c98f00" stroke-width="0.9"/>
        <!-- wing bumps -->
        <path d="M9,25 C7,29 7.5,34 10,38 C11.5,35 11.5,28 9,25 Z" fill="#f5c211" stroke="#c98f00" stroke-width="0.6"/>
        <path d="M25,25 C27,29 26.5,34 24,38 C22.5,35 22.5,28 25,25 Z" fill="#f5c211" stroke="#c98f00" stroke-width="0.6"/>
        <!-- tail nub -->
        <path d="M14,44.5 C15.5,47.2 18.5,47.2 20,44.5 C18.5,45.8 15.5,45.8 14,44.5 Z"
              fill="#f0b400" stroke="#c98f00" stroke-width="0.6"/>`;
  }

  // Moby Dick — the white whale from above: huge blunt sperm-whale forehead at
  // the bow, tapering scarred body, side fins, broad tail flukes astern — with
  // a couple of old harpoons still stuck in his back.
  function bodyMoby() {
    return `
        <defs>
          <linearGradient id="mobyBody" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0" stop-color="#f4f1e8"/>
            <stop offset="1" stop-color="#cfc9b8"/>
          </linearGradient>
        </defs>
        <!-- glow silhouette -->
        <path class="boat-glow" d="M17,1.5 C24,1.5 26.5,7 26.5,13 C26.5,22 24,30 21,37 L26,45 L17,41.5 L8,45 L13,37 C10,30 7.5,22 7.5,13 C7.5,7 10,1.5 17,1.5 Z"/>
        <!-- body: blunt head, taper, tail root -->
        <path d="M17,1.5 C24,1.5 26.5,7 26.5,13 C26.5,22 24,30 21,37 L17,40 L13,37 C10,30 7.5,22 7.5,13 C7.5,7 10,1.5 17,1.5 Z"
              fill="url(#mobyBody)" stroke="#9d9686" stroke-width="0.9" stroke-linejoin="round"/>
        <!-- tail flukes -->
        <path d="M17,38.5 C18.5,41 22,42.5 26,44.8 C24.5,40 21.5,37.5 18.6,36.6 Z" fill="#e8e4d6" stroke="#9d9686" stroke-width="0.7"/>
        <path d="M17,38.5 C15.5,41 12,42.5 8,44.8 C9.5,40 12.5,37.5 15.4,36.6 Z" fill="#e8e4d6" stroke="#9d9686" stroke-width="0.7"/>
        <!-- side fins -->
        <path d="M8.4,18 C5.5,20 4.5,23.5 5.5,26.5 C7.5,24.5 8.8,21.5 8.4,18 Z" fill="#e8e4d6" stroke="#9d9686" stroke-width="0.6"/>
        <path d="M25.6,18 C28.5,20 29.5,23.5 28.5,26.5 C26.5,24.5 25.2,21.5 25.6,18 Z" fill="#e8e4d6" stroke="#9d9686" stroke-width="0.6"/>
        <!-- blowhole + spout hint -->
        <circle cx="17" cy="6.5" r="1" fill="#8b8474"/>
        <path d="M17,5.5 C15.8,4 15.8,2.8 17,1.8 C18.2,2.8 18.2,4 17,5.5 Z" fill="#bfe6f2" opacity="0.75"/>
        <!-- the famous scars -->
        <path d="M12,12 L16,14.5 M13,22 L18,20 M19,27 L23,29" stroke="#b3ab98" stroke-width="0.7" opacity="0.9"/>
        <!-- two old harpoons, lines trailing -->
        <g transform="rotate(28 21 15)">
          <line x1="21" y1="15" x2="21" y2="9.5" stroke="#5a4632" stroke-width="0.9"/>
          <path d="M21,8 L19.9,10 L22.1,10 Z" fill="#3c3229"/>
        </g>
        <g transform="rotate(-24 12.6 27)">
          <line x1="12.6" y1="27" x2="12.6" y2="21.5" stroke="#5a4632" stroke-width="0.9"/>
          <path d="M12.6,20 L11.5,22 L13.7,22 Z" fill="#3c3229"/>
        </g>
        <path d="M22.5,10 C25,12 26,15 25.5,18" stroke="#7a6a52" stroke-width="0.5" fill="none" opacity="0.8"/>`;
  }

  // Kraken — from above: bulbous mantle at the bow, huge glowing eyes, and
  // eight curling tentacles trailing astern with sucker dots.
  function bodyKraken() {
    const tent = (x0, cx1, cx2, x1, w, flip) => `
        <path d="M${x0},26 C${cx1},32 ${cx2},38 ${x1},45.5" fill="none"
              stroke="#123a3f" stroke-width="${w}" stroke-linecap="round"/>
        <path d="M${x1},45.5 q ${flip ? "-" : ""}2.6,1.8 ${flip ? "-" : ""}1.4,3" fill="none"
              stroke="#123a3f" stroke-width="${Math.max(1, w - 1)}" stroke-linecap="round"/>`;
    return `
        <defs>
          <radialGradient id="krakenHead" cx="50%" cy="38%">
            <stop offset="0" stop-color="#2c7a72"/>
            <stop offset="1" stop-color="#0d3b3f"/>
          </radialGradient>
        </defs>
        <!-- glow silhouette -->
        <ellipse class="boat-glow" cx="17" cy="17" rx="11.5" ry="14"/>
        <!-- tentacles (drawn first, trailing astern) -->
        ${tent(10, 4, 2.5, 4, 2.6, true)}
        ${tent(13, 10, 8, 9, 3, true)}
        ${tent(16, 15, 13.5, 13.5, 3.2, true)}
        ${tent(18, 19, 20.5, 20.5, 3.2, false)}
        ${tent(21, 24, 26, 25, 3, false)}
        ${tent(24, 30, 31.5, 30, 2.6, false)}
        <!-- sucker dots on the two centre tentacles -->
        <circle cx="12.6" cy="34" r="0.55" fill="#7fd9c8"/><circle cx="12" cy="38" r="0.55" fill="#7fd9c8"/>
        <circle cx="20.9" cy="34" r="0.55" fill="#7fd9c8"/><circle cx="21.6" cy="38" r="0.55" fill="#7fd9c8"/>
        <!-- mantle (the bow) -->
        <path d="M17,1.5 C24.5,1.5 28.5,8 28.5,15.5 C28.5,23 23.5,28.5 17,28.5 C10.5,28.5 5.5,23 5.5,15.5 C5.5,8 9.5,1.5 17,1.5 Z"
              fill="url(#krakenHead)" stroke="#7fd9c8" stroke-width="0.9"/>
        <!-- mantle ridge -->
        <path d="M17,2.5 C19.5,7 19.5,12 17,16 C14.5,12 14.5,7 17,2.5 Z" fill="#3d9c8f" opacity="0.5"/>
        <!-- eyes -->
        <circle cx="11.8" cy="19.5" r="2.6" fill="#eafff6"/>
        <circle cx="22.2" cy="19.5" r="2.6" fill="#eafff6"/>
        <circle cx="11.8" cy="19.9" r="1.3" fill="#082026"/>
        <circle cx="22.2" cy="19.9" r="1.3" fill="#082026"/>
        <circle cx="12.3" cy="19.2" r="0.45" fill="#9ff5df"/>
        <circle cx="22.7" cy="19.2" r="0.45" fill="#9ff5df"/>`;
  }

  const BOAT_DESIGNS = {
    current: { label: "Current", body: bodyCurrent },
    bass: { label: "Bass boat", body: bodyBass },
    kayak: { label: "Kayak", body: bodyKayak },
    duck: { label: "Rubber duck", body: bodyDuck },
    moby: { label: "Moby Dick", body: bodyMoby },
    kraken: { label: "Kraken", body: bodyKraken },
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
