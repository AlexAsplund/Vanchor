/* Tools tab: u-blox toolbox. Reads stats and applies UBX settings (NMEA on/off,
 * rate, baud) on any serial port via /api/tools/ublox/*, independent of the
 * configured GPS source. Talks to the same device-independent backend the
 * ublox-select warning uses. */
(function () {
  "use strict";
  const $ = (id) => document.getElementById(id);

  function portPath(p) {
    if (typeof p === "string") return p;
    return (p && (p.port || p.device || p.path || p.name)) || "";
  }

  async function loadPorts() {
    const sel = $("ubxt-port");
    if (!sel) return;
    try {
      const r = await fetch("/api/devices/serial-ports");
      const d = await r.json();
      const cur = sel.value;
      const ports = (d && d.ports) || [];
      sel.innerHTML = "";
      if (!ports.length) {
        const o = document.createElement("option");
        o.value = ""; o.textContent = "— no serial ports found —";
        sel.appendChild(o);
        return;
      }
      ports.forEach((p) => {
        const path = portPath(p);
        if (!path) return;
        const o = document.createElement("option");
        o.value = path;
        const note = (typeof p === "object" && (p.hint || p.owner)) || "";
        o.textContent = path + (note ? "  (" + note + ")" : "");
        sel.appendChild(o);
      });
      if (cur) sel.value = cur;
    } catch (e) { /* offline / no endpoint — leave the placeholder */ }
  }

  function fmtStats(d) {
    if (!d || !d.ok) return "Error: " + ((d && d.error) || "no response");
    const L = [];
    const pr = d.protocols || {};
    L.push("Protocols streaming:  NMEA " + (pr.nmea ? "ON" : "off") +
           "   ·   UBX " + (pr.ubx ? "ON" : "off"));
    if (d.nmea_types && d.nmea_types.length) L.push("NMEA sentences: " + d.nmea_types.join(" "));
    if (d.fix) {
      const f = d.fix;
      L.push("Fix: type " + f.fix_type + (f.valid ? " (valid)" : " (no lock)") +
             "  ·  " + f.num_sv + " sats");
      if (f.valid) L.push("     " + f.lat + ", " + f.lon + "   ±" + f.h_acc_m + " m   " + f.sog_knots + " kn");
    } else {
      L.push("Fix: none seen");
    }
    if (d.version && d.version.sw) L.push("Firmware: " + d.version.sw + (d.version.hw ? "  (hw " + d.version.hw + ")" : ""));
    const c = d.counters || {};
    L.push("(" + (c.nmea_sentences || 0) + " NMEA + " + (c.ubx_frames || 0) + " UBX frames in " + (c.seconds || 0) + " s)");
    return L.join("\n");
  }

  async function readStats() {
    const port = $("ubxt-port").value, baud = $("ubxt-baud").value;
    const out = $("ubxt-stats");
    if (!port) { out.textContent = "Pick a serial port first."; return; }
    out.textContent = "Reading " + port + " …";
    try {
      const r = await fetch("/api/tools/ublox/stats?port=" + encodeURIComponent(port) +
                            "&baud=" + encodeURIComponent(baud));
      out.textContent = fmtStats(await r.json());
    } catch (e) { out.textContent = "Error: " + e; }
  }

  async function applySettings() {
    const port = $("ubxt-port").value;
    const res = $("ubxt-result");
    if (!port) { res.textContent = "Pick a serial port first."; return; }
    const body = {
      port: port,
      baud: parseInt($("ubxt-baud").value, 10),
      persist: $("ubxt-persist").checked,
    };
    const nmea = $("ubxt-nmea").value;
    if (nmea !== "") body.nmea = nmea === "1";
    const rate = $("ubxt-rate").value;
    if (rate !== "") body.rate_hz = parseFloat(rate);
    const nb = $("ubxt-newbaud").value;
    if (nb !== "") body.new_baud = parseInt(nb, 10);
    if (body.nmea === undefined && body.rate_hz === undefined && body.new_baud === undefined) {
      res.textContent = "Nothing selected to change.";
      return;
    }
    res.textContent = "Applying …";
    try {
      const r = await fetch("/api/tools/ublox/apply", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const d = await r.json();
      if (d.ok) {
        res.textContent = "Applied ✓" + (d.acks ? "  (" + Object.keys(d.acks).join(", ") + " ACKed)" : "") +
                          (body.persist ? " · saved to flash" : "");
        if (window.VA && VA.toast) VA.toast("u-blox settings applied");
      } else {
        res.textContent = "Failed: " + (d.error || JSON.stringify(d.acks || {}));
      }
    } catch (e) { res.textContent = "Error: " + e; }
  }

  function init() {
    const read = $("ubxt-read"), apply = $("ubxt-apply");
    if (!read && !apply) return;   // Tools panel not present
    if (read) read.addEventListener("click", readStats);
    if (apply) apply.addEventListener("click", applySettings);
    // Refresh the port list whenever the Tools tile is opened, and once now.
    const tile = document.querySelector('.cm-tile[data-cat="tools"]');
    if (tile) tile.addEventListener("click", loadPorts);
    loadPorts();
  }

  if (document.readyState !== "loading") init();
  else document.addEventListener("DOMContentLoaded", init);
})();
