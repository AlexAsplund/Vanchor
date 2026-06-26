/* Vanchor-NG — Motor placement control (shared module).
 *
 * A touch-friendly, top-down boat illustration (bow up) on which the user taps
 * to place the trolling motor. The tap is converted to two boat offsets:
 *
 *   thruster_offset_m  longitudinal from the boat's CG (centre),
 *                      + = forward toward the bow, − = aft toward the stern.
 *   thruster_y_m       lateral from the centerline, + = starboard (right).
 *
 * Bow / Stern buttons choose which end you're mounting on; the tap is then
 * snapped/constrained to that half so an off-centre bow or side-of-stern mount
 * is captured accurately.
 *
 * Geometry (SVG user units, bow pointing UP / −y):
 *   - The boat outline is drawn in a fixed VIEW box; length_m maps to the hull
 *     drawn height, beam_m to the drawn width. The drawn aspect ratio matches
 *     the real length:beam ratio so the picture is to scale.
 *   - Centre (CG) sits at the vertical middle of the hull. Tapping above centre
 *     => forward (+offset); below => aft (−offset). Tapping right of the
 *     centreline => starboard (+y); left => port (−y).
 *
 * Usage:
 *   const mp = VA.MotorPlace.create({
 *     root: <element>,            // container to render into
 *     idPrefix: "wiz-mp",         // unique id prefix (avoid collisions)
 *     onChange: (offset_m, y_m, mount) => { ... },  // debounced placement
 *   });
 *   mp.setBoat({ length_m, beam_m });          // (re)scale the picture
 *   mp.setValue(thruster_offset_m, thruster_y_m, thruster_mount);
 *   mp.getValue();  // -> { offset_m, y_m, mount }
 */
"use strict";

(function () {
  const NS = "http://www.w3.org/2000/svg";

  // SVG view geometry. The hull occupies a margin-inset box inside this view.
  const VIEW_W = 200;
  const VIEW_H = 320;
  const MARGIN_X = 30;   // horizontal padding around the hull
  const MARGIN_Y = 24;   // vertical padding around the hull
  const HULL_W = VIEW_W - 2 * MARGIN_X;   // drawn beam extent
  const HULL_H = VIEW_H - 2 * MARGIN_Y;   // drawn length extent
  const CX = VIEW_W / 2;                  // centreline (x)
  const CY = VIEW_H / 2;                  // CG / midships (y)

  function el(tag, attrs) {
    const node = document.createElementNS(NS, tag);
    if (attrs) for (const k in attrs) node.setAttribute(k, attrs[k]);
    return node;
  }

  function clamp(v, lo, hi) { return Math.max(lo, Math.min(hi, v)); }

  function create(opts) {
    opts = opts || {};
    const root = opts.root;
    const pfx = opts.idPrefix || "mp";
    const onChange = typeof opts.onChange === "function" ? opts.onChange : function () {};
    if (!root) return null;

    let lengthM = 4.0;
    let beamM = 1.6;
    let mount = "bow";       // which end we're placing on ("bow" | "stern")
    let offsetM = 0;         // + forward
    let yM = 0;              // + starboard

    // ---- DOM scaffold ----------------------------------------------------
    root.classList.add("motorplace");
    root.innerHTML = "";

    const seg = el2("div", "mp-seg");
    seg.setAttribute("role", "group");
    const btnBow = el2("button", "mp-end");
    btnBow.type = "button"; btnBow.dataset.val = "bow"; btnBow.textContent = "Bow";
    btnBow.id = pfx + "-bow";
    const btnStern = el2("button", "mp-end");
    btnStern.type = "button"; btnStern.dataset.val = "stern"; btnStern.textContent = "Stern";
    btnStern.id = pfx + "-stern";
    seg.append(btnBow, btnStern);

    const svg = el("svg", {
      viewBox: `0 0 ${VIEW_W} ${VIEW_H}`,
      class: "mp-svg",
      id: pfx + "-svg",
      role: "img",
      "aria-label": "Top-down boat; tap to place the trolling motor",
    });
    // touch-action none so dragging doesn't scroll the page on phones
    svg.style.touchAction = "none";

    // hull outline path (bow up). Bow is a rounded point at top.
    const left = MARGIN_X, right = VIEW_W - MARGIN_X;
    const top = MARGIN_Y, bot = VIEW_H - MARGIN_Y;
    const bowTip = top - 6;
    const hull = el("path", {
      d:
        `M ${CX} ${bowTip} ` +
        `C ${right} ${top + 4}, ${right} ${CY - 30}, ${right} ${CY} ` +
        `C ${right} ${bot - 40}, ${CX + 40} ${bot}, ${CX} ${bot} ` +
        `C ${CX - 40} ${bot}, ${left} ${bot - 40}, ${left} ${CY} ` +
        `C ${left} ${CY - 30}, ${left} ${top + 4}, ${CX} ${bowTip} Z`,
      class: "mp-hull",
    });
    // centreline + midships (CG) reference lines
    const centerline = el("line", {
      x1: CX, y1: top, x2: CX, y2: bot, class: "mp-centerline",
    });
    const midline = el("line", {
      x1: left + 6, y1: CY, x2: right - 6, y2: CY, class: "mp-midline",
    });
    // small "CG" tick label
    const cgDot = el("circle", { cx: CX, cy: CY, r: 3, class: "mp-cg" });
    // bow label arrow
    const bowLbl = el("text", { x: CX, y: top - 10, class: "mp-bowlbl" });
    bowLbl.textContent = "BOW";

    // motor marker (group: ring + prop hub)
    const marker = el("g", { class: "mp-marker", id: pfx + "-marker" });
    const mRing = el("circle", { cx: CX, cy: CY, r: 11, class: "mp-marker-ring" });
    const mHub = el("circle", { cx: CX, cy: CY, r: 3.5, class: "mp-marker-hub" });
    marker.append(mRing, mHub);
    marker.style.display = "none";

    svg.append(hull, centerline, midline, cgDot, bowLbl, marker);

    const readout = el2("div", "mp-readout");
    readout.id = pfx + "-readout";
    readout.textContent = "Tap the boat where the motor is mounted.";

    root.append(seg, svg, readout);

    function el2(tag, cls) { const n = document.createElement(tag); if (cls) n.className = cls; return n; }

    // ---- geometry <-> metres --------------------------------------------
    // Drawn y for a longitudinal offset (m): +offset is forward (smaller y).
    function offsetToY(off) {
      // full hull height HULL_H spans length_m metres, centred at CY.
      return CY - (off / Math.max(lengthM, 0.01)) * HULL_H;
    }
    function yToOffset(py) {
      return clamp((CY - py) / HULL_H * lengthM, -lengthM / 2, lengthM / 2);
    }
    // Drawn x for a lateral offset (m): +y is starboard (larger x).
    function lateralToX(ly) {
      return CX + (ly / Math.max(beamM, 0.01)) * HULL_W;
    }
    function xToLateral(px) {
      return clamp((px - CX) / HULL_W * beamM, -beamM / 2, beamM / 2);
    }

    // ---- rendering -------------------------------------------------------
    function updateSeg() {
      btnBow.classList.toggle("on", mount === "bow");
      btnStern.classList.toggle("on", mount === "stern");
    }

    function fmt(m) { return Math.abs(m).toFixed(2); }

    function updateReadout() {
      const fa = offsetM >= 0
        ? `${fmt(offsetM)} m fwd of centre`
        : `${fmt(offsetM)} m aft of centre`;
      const lat = Math.abs(yM) < 0.005
        ? "on centreline"
        : (yM >= 0 ? `${fmt(yM)} m to starboard` : `${fmt(yM)} m to port`);
      readout.textContent = `${fa}, ${lat}`;
    }

    function placeMarker() {
      const px = lateralToX(yM);
      const py = offsetToY(offsetM);
      mRing.setAttribute("cx", px); mRing.setAttribute("cy", py);
      mHub.setAttribute("cx", px); mHub.setAttribute("cy", py);
      marker.style.display = "";
    }

    function render() {
      updateSeg();
      placeMarker();
      updateReadout();
    }

    // ---- tap handling ----------------------------------------------------
    // Convert a client point to SVG user coords.
    function toSvgPoint(clientX, clientY) {
      const rect = svg.getBoundingClientRect();
      const sx = VIEW_W / rect.width;
      const sy = VIEW_H / rect.height;
      return { x: (clientX - rect.left) * sx, y: (clientY - rect.top) * sy };
    }

    function applyTap(clientX, clientY, commit) {
      const p = toSvgPoint(clientX, clientY);
      let off = yToOffset(p.y);
      // Constrain to the chosen half: bow => forward (>=0), stern => aft (<=0).
      if (mount === "bow") off = Math.max(0, off);
      else off = Math.min(0, off);
      offsetM = off;
      yM = xToLateral(p.x);
      render();
      if (commit) commitChange();
    }

    let debTimer = null;
    function commitChange() {
      clearTimeout(debTimer);
      debTimer = setTimeout(() => onChange(offsetM, yM, mount), 350);
    }

    let dragging = false;
    function onDown(e) {
      dragging = true;
      const pt = pointFrom(e);
      applyTap(pt.x, pt.y, false);
      try { svg.setPointerCapture(e.pointerId); } catch (_) {}
      e.preventDefault();
    }
    function onMove(e) {
      if (!dragging) return;
      const pt = pointFrom(e);
      applyTap(pt.x, pt.y, false);
      e.preventDefault();
    }
    function onUp(e) {
      if (!dragging) return;
      dragging = false;
      const pt = pointFrom(e);
      applyTap(pt.x, pt.y, true);
      try { svg.releasePointerCapture(e.pointerId); } catch (_) {}
      e.preventDefault();
    }
    function pointFrom(e) {
      if (e.touches && e.touches[0]) return { x: e.touches[0].clientX, y: e.touches[0].clientY };
      if (e.changedTouches && e.changedTouches[0])
        return { x: e.changedTouches[0].clientX, y: e.changedTouches[0].clientY };
      return { x: e.clientX, y: e.clientY };
    }

    if (window.PointerEvent) {
      svg.addEventListener("pointerdown", onDown);
      svg.addEventListener("pointermove", onMove);
      svg.addEventListener("pointerup", onUp);
      svg.addEventListener("pointercancel", onUp);
    } else {
      svg.addEventListener("mousedown", onDown);
      window.addEventListener("mousemove", onMove);
      window.addEventListener("mouseup", onUp);
      svg.addEventListener("touchstart", onDown, { passive: false });
      svg.addEventListener("touchmove", onMove, { passive: false });
      svg.addEventListener("touchend", onUp, { passive: false });
    }

    btnBow.addEventListener("click", () => {
      mount = "bow";
      if (offsetM < 0) offsetM = Math.abs(offsetM);
      render(); commitChange();
    });
    btnStern.addEventListener("click", () => {
      mount = "stern";
      if (offsetM > 0) offsetM = -offsetM;
      render(); commitChange();
    });

    // ---- public API ------------------------------------------------------
    function setBoat(boat) {
      boat = boat || {};
      const L = Number(boat.length_m);
      const B = Number(boat.beam_m);
      if (Number.isFinite(L) && L > 0) lengthM = L;
      if (Number.isFinite(B) && B > 0) beamM = B;
      render();
    }
    function setValue(off, y, mnt) {
      const o = Number(off), yy = Number(y);
      if (mnt === "bow" || mnt === "stern") mount = mnt;
      if (Number.isFinite(o)) {
        offsetM = o;
        // Infer/repair the end choice from the sign if a mount wasn't given.
        if (mnt !== "bow" && mnt !== "stern") mount = o >= 0 ? "bow" : "stern";
      }
      if (Number.isFinite(yy)) yM = yy;
      // keep offset consistent with the selected half
      if (mount === "bow") offsetM = Math.max(0, offsetM);
      else offsetM = Math.min(0, offsetM);
      render();
    }
    function getValue() { return { offset_m: offsetM, y_m: yM, mount: mount }; }

    updateSeg();
    return { setBoat, setValue, getValue, root };
  }

  window.VA = window.VA || {};
  VA.MotorPlace = { create };
})();
