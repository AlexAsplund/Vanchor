/* u-blox receiver toolbox — the "Receiver setup (u-blox)…" disclosure inside
 * the GPS card (Devices panel; formerly its own Tools tab). Reads stats and
 * applies UBX settings (NMEA on/off, rate, baud) on any serial port via
 * /api/tools/ublox/*, independent of the configured GPS source. Talks to the
 * same device-independent backend the ublox-select warning uses.
 *
 * On each open of the disclosure the port list is refreshed and, when the
 * configured GPS port looks serial (/dev/…), the port + connection baud are
 * prefilled from the GPS card's own fields (#dev-gps-port / #dev-gps-baud). */
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

  // Prefill from the GPS card's configured connection: preselect the configured
  // serial port (adding it if the scan missed it, so the prefill survives a
  // port-list refresh) and mirror the configured baud into #ubxt-baud.
  function prefillFromConfig() {
    const cfgPort = (($("dev-gps-port") || {}).value || "").trim();
    const psel = $("ubxt-port");
    if (psel && /^\/dev\//.test(cfgPort)) {
      const has = Array.prototype.some.call(psel.options, (o) => o.value === cfgPort);
      if (!has) {
        const o = document.createElement("option");
        o.value = cfgPort;
        o.textContent = cfgPort + "  (configured GPS)";
        psel.appendChild(o);
      }
      psel.value = cfgPort;
    }
    const cfgBaud = (($("dev-gps-baud") || {}).value || "").trim();
    const bsel = $("ubxt-baud");
    if (bsel && cfgBaud) {
      const has = Array.prototype.some.call(bsel.options, (o) => o.value === cfgBaud);
      if (!has) {
        const o = document.createElement("option");
        o.textContent = cfgBaud;   // option value defaults to its text
        bsel.appendChild(o);
      }
      bsel.value = cfgBaud;
    }
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
        // Baud-divergence warning: the receiver's UART baud was changed but the
        // GPS connection baud (#dev-gps-baud) still expects the old rate. Warn
        // only — never auto-change the config.
        if (body.new_baud != null) {
          const cur = parseInt((($("dev-gps-baud") || {}).value || "").trim(), 10);
          if (!Number.isFinite(cur) || cur !== body.new_baud) {
            const warn = document.createElement("div");
            warn.className = "hint err";
            warn.textContent = "⚠ Receiver UART baud changed to " + body.new_baud +
              " — update the GPS connection baud (currently " +
              (Number.isFinite(cur) ? cur : "driver default") +
              ") or the GPS will stop decoding.";
            res.appendChild(warn);
          }
        }
        if (window.VA && VA.toast) VA.toast("u-blox settings applied");
      } else {
        res.textContent = "Failed: " + (d.error || JSON.stringify(d.acks || {}));
      }
    } catch (e) { res.textContent = "Error: " + e; }
  }

  function init() {
    const read = $("ubxt-read"), apply = $("ubxt-apply");
    if (!read && !apply) return;   // toolbox markup not present
    if (read) read.addEventListener("click", readStats);
    if (apply) apply.addEventListener("click", applySettings);
    // Refresh the port list + prefill from the GPS config on every open of the
    // disclosure (prefill runs AFTER the refresh so it can't be clobbered).
    const det = $("ublox-tools-card");
    if (det) {
      det.addEventListener("toggle", () => {
        if (det.open) loadPorts().then(prefillFromConfig);
      });
    }
  }

  if (document.readyState !== "loading") init();
  else document.addEventListener("DOMContentLoaded", init);
})();
