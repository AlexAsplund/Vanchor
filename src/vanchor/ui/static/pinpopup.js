/* Vanchor-NG — pin popup (WP6 item 18).
 *
 * An idle tap on the map (no tool armed) opens a glass card popup with three
 * actions: "Take me here" (600 ms hold), "Anchor here" (single tap when
 * SOG ≤ 0.5 kn and ≤ 50 m away; hold otherwise), "Drop marker" (single tap).
 *
 * Wire-up: route.js calls VA.pinPopup.open(lat, lon) from the final else
 * branch of its VA.map.setOnMapClick handler. All commands go through the
 * existing VA.send / VA.routeChoice paths.
 */
"use strict";

(function () {
  function boot() {
    if (!window.VA || !VA.map || !VA.map.leaflet || !window.L) {
      return setTimeout(boot, 200);
    }
    const map = VA.map.leaflet;

    let currentPopup = null;
    // F8: generation counter — each open() increments this so old telemetry
    // watchers self-disable immediately (no unsubscribe from VA.onTelemetry).
    let _telGen = 0;

    function open(lat, lon) {
      const myGen = ++_telGen;
      // Close any existing popup
      if (currentPopup) { map.closePopup(currentPopup); currentPopup = null; }

      // Distance from boat
      const pos = VA.last && VA.last.position;
      const distM = (pos && VA.geo && VA.geo.haversineM)
        ? VA.geo.haversineM(pos.lat, pos.lon, lat, lon) : null;
      const distStr = distM !== null
        ? (distM < 1000 ? Math.round(distM) + " m" : (distM / 1000).toFixed(1) + " km")
        : "—";

      // ETA segment
      const sog = VA.fin ? VA.fin(VA.last && VA.last.sog_knots) : null;
      const sogUsed = (sog !== null && sog >= 0.5) ? sog : 2.0;
      const sogLabel = (sog !== null && sog >= 0.5) ? sogUsed.toFixed(1) + " kn" : "2.0 kn";
      let etaStr = "";
      if (pos && distM !== null) {
        const mins = Math.round(distM / (sogUsed * 0.514444) / 60);
        etaStr = " · ~" + (mins < 1 ? "<1" : mins) + " min at " + sogLabel;
      }

      // SOG gate: near+fast = hold required
      const isNear = distM !== null && distM <= 50;
      const isUnderway = sog !== null && sog > 0.5;
      // Anchor badge: near+slow = TAP, near+fast = HOLD 0.6s, far = HOLD 0.6s
      const anchorBadge = isNear && !isUnderway ? "TAP" : "HOLD 0.6&nbsp;s";
      const anchorSub = !isNear && pos !== null
        ? '<span class="pp-sub">drives there, then holds</span>' : "";

      const html = "<div class=\"pin-popup\">"
        + "<div class=\"pp-head\"><b>" + distStr + "</b><span class=\"pp-head-meta\">" + etaStr + "</span></div>"
        + "<button class=\"pp-btn\" data-act=\"goto\" type=\"button\">"
        + "<span class=\"pp-icon\">&#x27A4;</span>"
        + "<span class=\"pp-label\">Take me here</span>"
        + "<span class=\"pp-gate\">HOLD 0.6s</span>"
        + "</button>"
        + "<button class=\"pp-btn\" data-act=\"anchor\" type=\"button\">"
        + "<span class=\"pp-icon\">&#x2693;</span>"
        + "<span class=\"pp-label\">Anchor here" + anchorSub + "</span>"
        + "<span class=\"pp-gate\" id=\"pp-anchor-gate\">" + anchorBadge + "</span>"
        + "</button>"
        + "<button class=\"pp-btn\" data-act=\"marker\" type=\"button\">"
        + "<span class=\"pp-icon\">&#x25CE;</span>"
        + "<span class=\"pp-label\">Drop marker</span>"
        + "<span class=\"pp-gate\">TAP</span>"
        + "</button>"
        + "</div>";

      const popup = L.popup({
        className: "pin-popup-wrap",
        closeButton: true,
        autoPanPadding: [24, 24],
        minWidth: 240,
      }).setLatLng([lat, lon]).setContent(html).openOn(map);
      currentPopup = popup;

      const node = popup.getElement();
      if (!node) return;

      // Async depth fetch
      fetch("/api/depth/at?lat=" + lat.toFixed(7) + "&lon=" + lon.toFixed(7))
        .then(function(r) { return r.ok ? r.json() : null; })
        .then(function(r) {
          if (!r || r.ok === false || !Number.isFinite(r.depth_m)) return;
          const headMeta = node.querySelector(".pp-head-meta");
          if (!headMeta || !headMeta.isConnected) return;
          headMeta.textContent = etaStr + " · " + r.depth_m.toFixed(1) + " m deep";
        }).catch(function() {});

      // Take me here — always 600 ms hold-to-engage
      const gotoBtn = node.querySelector("[data-act=\"goto\"]");
      if (gotoBtn && VA.bindHold) {
        VA.bindHold(gotoBtn, 600, function() {
          map.closePopup(popup);
          if (VA.routeEditor && VA.routeEditor.gotoTo) VA.routeEditor.gotoTo(lat, lon);
        });
      }

      // Anchor here — distance + SOG gated
      const anchorBtn = node.querySelector("[data-act=\"anchor\"]");
      if (anchorBtn) {
        const doAnchor = function() {
          map.closePopup(popup);
          if (!isNear) {
            // Far: drive there then hold
            const engage = function() {
              VA.send({
                type: "goto",
                waypoints: [{ name: "ANCHOR", lat: lat, lon: lon }],
                throttle: 0.6,
                on_arrival: "anchor",
              });
            };
            if (VA.routeChoice) VA.routeChoice.deliver([{ name: "ANCHOR", lat: lat, lon: lon }], engage);
            else engage();
          } else {
            // Near: anchor at tapped point
            if (VA.anchorCtl && VA.anchorCtl.engageAt) VA.anchorCtl.engageAt(lat, lon);
            else VA.send({ type: "anchor_hold", anchor: { lat: lat, lon: lon } });
          }
        };
        // F1: hold always available (moving/far cases); tap only when currently
        // near+slow at activation time (live check prevents stationary-open →
        // boat-starts-moving → bare tap engages hold while underway).
        if (VA.bindHold) VA.bindHold(anchorBtn, 600, doAnchor);
        anchorBtn.addEventListener("click", function() {
          // bindHold swallows the click after a completed hold (justFired),
          // so this handler only fires on a genuine bare tap.
          const liveSog = VA.fin ? VA.fin(VA.last && VA.last.sog_knots) : null;
          const liveUnderway = liveSog !== null && liveSog > 0.5;
          if (!isNear || liveUnderway) return;  // moving or far — require hold
          doAnchor();
        });
        // Live badge update while popup is open (SOG changes).
        // F8: guard with generation so handlers from old opens self-disable.
        if (VA.onTelemetry) {
          VA.onTelemetry(function ppSogWatch(t) {
            if (_telGen !== myGen) return;  // superseded by a later open()
            const gate = node.querySelector("#pp-anchor-gate");
            if (!gate || !gate.isConnected) { return; }
            const liveSog = VA.fin ? VA.fin(t.sog_knots) : null;
            const liveUnderway = liveSog !== null && liveSog > 0.5;
            gate.innerHTML = (isNear && !liveUnderway) ? "TAP" : "HOLD 0.6&nbsp;s";
          });
        }
      }

      // Drop marker — single tap
      const markerBtn = node.querySelector("[data-act=\"marker\"]");
      if (markerBtn) {
        markerBtn.addEventListener("click", function() {
          map.closePopup(popup);
          if (VA.markers && VA.markers.create) VA.markers.create(lat, lon);
          else if (VA.markers && VA.markers.createMarker) VA.markers.createMarker(lat, lon);
        });
      }

      popup.on("remove", function() { currentPopup = null; });
    }

    VA.pinPopup = { open: open };
  }
  boot();
})();
