/* Vanchor-NG — Measure tool (#102).
 *
 * A "Measure" button beside the layers/overlay selector. Click it for a little
 * submenu: Line (tap vertices) or Freehand (press-drag). The running length
 * (start -> finish) is shown as a label ANCHORED TO THE LINE itself (so it sits
 * where you're drawing). Tapping that label offers "To waypoints" (drop the path
 * into the route editor) or "Remove".
 *
 * Standalone: uses the public VA.map surface (leaflet, addClickConsumer,
 * addPending) + VA.routeEditor.refresh — no map.js edit. Freehand uses pointer
 * events so it works on touch + mouse + pen. */
(function () {
  function boot() {
    if (!window.VA || !VA.map || !VA.map.leaflet || !window.L) {
      return setTimeout(boot, 200);
    }
    const map = VA.map.leaflet;
    const L = window.L;
    const mapEl = document.getElementById("map");
    const cont = map.getContainer();

    let mode = null; // null | "line" | "free"
    let pts = []; // [L.LatLng]
    let poly = null;
    let dots = [];
    let label = null; // L.marker (divIcon) riding the line, shows the distance
    let pop = null; // L.popup action menu
    let hint = null;
    let drawing = false;
    let ctlBtn = null;

    const totalM = () => {
      let d = 0;
      for (let i = 1; i < pts.length; i++) d += map.distance(pts[i - 1], pts[i]);
      return d;
    };
    const fmt = (m) =>
      m < 1000 ? Math.round(m) + " m" : (m / 1000).toFixed(m < 10000 ? 2 : 1) + " km";

    // ---- control button + Line/Freehand submenu ------------------------- //
    const Ctl = L.Control.extend({
      options: { position: "topleft" },
      onAdd() {
        const wrap = L.DomUtil.create("div", "measure-ctl");
        const btn = L.DomUtil.create("a", "measure-btn", wrap);
        btn.href = "#";
        btn.title = "Measure distance";
        btn.setAttribute("role", "button");
        btn.textContent = "📏";
        const sub = L.DomUtil.create("div", "measure-sub hidden", wrap);
        const mk = (m, txt) => {
          const bb = L.DomUtil.create("button", "", sub);
          bb.type = "button";
          bb.textContent = txt;
          L.DomEvent.on(bb, "click", (e) => {
            L.DomEvent.stop(e);
            sub.classList.add("hidden");
            start(m);
          });
        };
        mk("line", "╱ Line");
        mk("free", "✎ Freehand");
        L.DomEvent.disableClickPropagation(wrap);
        L.DomEvent.on(btn, "click", (e) => {
          L.DomEvent.stop(e);
          if (mode || pts.length) clearAll();
          else sub.classList.toggle("hidden");
        });
        ctlBtn = btn;
        return wrap;
      },
    });
    map.addControl(new Ctl());

    // ---- drawing -------------------------------------------------------- //
    function ensurePoly() {
      if (!poly) {
        poly = L.polyline([], {
          color: "#ffd54a",
          weight: 3,
          dashArray: "7,5",
          opacity: 0.95,
          interactive: false,
        }).addTo(map);
      }
    }
    function redraw() {
      ensurePoly();
      poly.setLatLngs(pts);
      dots.forEach((d) => map.removeLayer(d));
      dots = [];
      if (pts.length) {
        const ends = pts.length === 1 ? [pts[0]] : [pts[0], pts[pts.length - 1]];
        ends.forEach((p) =>
          dots.push(
            L.circleMarker(p, {
              radius: 4,
              weight: 2,
              color: "#ffd54a",
              fillColor: "#10141f",
              fillOpacity: 1,
              interactive: false,
            }).addTo(map)
          )
        );
      }
      updateLabel();
    }

    // The distance label rides the END of the line (where you're drawing).
    function updateLabel() {
      if (pts.length < 1) {
        if (label) { map.removeLayer(label); label = null; }
        closeMenu();
        return;
      }
      const at = pts[pts.length - 1];
      const html =
        '<span class="measure-label-pill">📏 ' +
        fmt(totalM()) +
        (pts.length >= 2 ? ' <em>·</em>' : "") +
        "</span>";
      const icon = L.divIcon({
        className: "measure-label",
        html,
        iconSize: null,
        iconAnchor: [-10, 8], // sit just below-right of the point, clear of the cursor
      });
      if (!label) {
        label = L.marker(at, { icon, interactive: true, keyboard: false, zIndexOffset: 1000 }).addTo(map);
        label.on("click", (e) => {
          L.DomEvent.stop(e);
          openMenu();
        });
      } else {
        label.setLatLng(at);
        label.setIcon(icon);
      }
    }

    function openMenu() {
      if (pts.length < 2) return; // need a real path to act on
      closeMenu();
      const box = L.DomUtil.create("div", "measure-pop");
      const wp = L.DomUtil.create("button", "", box);
      wp.type = "button";
      wp.textContent = "→ To waypoints";
      const rm = L.DomUtil.create("button", "", box);
      rm.type = "button";
      rm.textContent = "✕ Remove";
      L.DomEvent.on(wp, "click", (e) => { L.DomEvent.stop(e); toWaypoints(); });
      L.DomEvent.on(rm, "click", (e) => { L.DomEvent.stop(e); clearAll(); });
      pop = L.popup({ closeButton: false, className: "measure-pop-wrap", offset: [0, -2], autoPan: false })
        .setLatLng(pts[pts.length - 1])
        .setContent(box)
        .openOn(map);
    }
    function closeMenu() {
      if (pop) { map.closePopup(pop); pop = null; }
    }

    function showHint(text) {
      if (!hint) {
        hint = document.createElement("div");
        hint.className = "measure-hint";
        document.body.appendChild(hint);
      }
      hint.textContent = text;
      hint.classList.remove("hidden");
    }
    function hideHint() { if (hint) hint.classList.add("hidden"); }

    // ---- mode lifecycle ------------------------------------------------- //
    function start(m) {
      clearAll();
      mode = m;
      if (ctlBtn) ctlBtn.classList.add("on");
      mapEl.classList.add("measuring");
      if (m === "free") {
        map.dragging.disable();
        showHint("Drag on the map to draw — release when done");
      } else {
        showHint("Tap the map to add points — tap the distance label when done");
      }
    }
    function stop() {
      mode = null;
      if (ctlBtn) ctlBtn.classList.remove("on");
      mapEl.classList.remove("measuring");
      map.dragging.enable();
      hideHint();
    }
    function clearAll() {
      stop();
      closeMenu();
      drawing = false;
      pts = [];
      if (poly) { map.removeLayer(poly); poly = null; }
      dots.forEach((d) => map.removeLayer(d));
      dots = [];
      if (label) { map.removeLayer(label); label = null; }
    }

    // Line: each map tap drops a vertex (consume only while in line mode).
    VA.map.addClickConsumer((lat, lon) => {
      if (mode !== "line") return false;
      pts.push(L.latLng(lat, lon));
      redraw();
      return true;
    });

    // Freehand: POINTER events -> touch, pen and mouse alike.
    let pid = null;
    cont.addEventListener("pointerdown", (e) => {
      if (mode !== "free") return;
      e.preventDefault();
      pid = e.pointerId;
      try { cont.setPointerCapture(pid); } catch (_) {}
      drawing = true;
      pts = [map.mouseEventToLatLng(e)];
      redraw();
    });
    cont.addEventListener("pointermove", (e) => {
      if (mode !== "free" || !drawing) return;
      const ll = map.mouseEventToLatLng(e);
      const last = pts[pts.length - 1];
      if (!last || map.distance(last, ll) >= 3) {
        pts.push(ll);
        redraw();
      }
    });
    function endFree() {
      if (mode !== "free" || !drawing) return;
      drawing = false;
      try { cont.releasePointerCapture(pid); } catch (_) {}
      stop(); // keep the path + label for the action menu
      updateLabel();
    }
    cont.addEventListener("pointerup", endFree);
    cont.addEventListener("pointercancel", endFree);

    // ---- to-waypoints --------------------------------------------------- //
    function toWaypoints() {
      const minGap = pts.length > 30 ? Math.max(15, totalM() / 40) : 0;
      const simp = simplify(pts, minGap);
      if (simp.length >= 2 && VA.map.addPending) {
        simp.forEach((p) => VA.map.addPending(p.lat, p.lng));
        if (VA.routeEditor && VA.routeEditor.refresh) VA.routeEditor.refresh();
      }
      clearAll();
    }
    function simplify(arr, minGapM) {
      if (arr.length <= 2 || minGapM <= 0) return arr.slice();
      const out = [arr[0]];
      for (let i = 1; i < arr.length - 1; i++) {
        if (map.distance(out[out.length - 1], arr[i]) >= minGapM) out.push(arr[i]);
      }
      out.push(arr[arr.length - 1]);
      return out;
    }

    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && (mode || pts.length)) clearAll();
    });
  }
  boot();
})();
