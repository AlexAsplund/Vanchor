/**
 * hwwizard.js — Hardware setup wizard (adoption pack, task 4).
 *
 * IIFE that attaches VA.openHwWizard(). Opens a 5-step modal:
 *   0 Scan  → 1 GPS  → 2 Compass  → 3 Motor  → 4 Finish
 *
 * All data flows through /api/hw/scan (GET) and /api/hw/probe (POST).
 * Writing config uses the existing PATCH /api/config/devices endpoint.
 * Never sends motor commands; probe is read-only observe-only.
 */
(function hwwizardIIFE() {
  "use strict";

  /* ------------------------------------------------------------------ *
   * Helpers                                                              *
   * ------------------------------------------------------------------ */

  function $(id) { return document.getElementById(id); }

  function show(el) { el && el.classList.remove("hidden"); }
  function hide(el) { el && el.classList.add("hidden"); }

  function setStatus(id, msg, isErr) {
    const el = $(id);
    if (!el) return;
    el.textContent = msg;
    el.classList.toggle("err", !!isErr);
  }

  function clearResult(kind) {
    hide($(`hwwiz-${kind}-result`));
    const det = $(`hwwiz-${kind}-detected`);
    if (det) { det.textContent = ""; det.className = "badge"; }
    const tbl = $(`hwwiz-${kind}-sample`);
    if (tbl) tbl.innerHTML = "";
    const pre = $(`hwwiz-${kind}-preview`);
    if (pre) pre.textContent = "";
  }

  function renderResult(kind, data) {
    const wrap = $(`hwwiz-${kind}-result`);
    if (!wrap) return;
    show(wrap);

    const det = $(`hwwiz-${kind}-detected`);
    if (det) {
      det.textContent = data.detected || "unknown";
      det.className = "badge";
      if (data.confidence === "high") det.classList.add("badge-ok");
      else if (data.confidence === "medium") det.classList.add("badge-warn");
      else det.classList.add("badge-err");
    }

    const tbl = $(`hwwiz-${kind}-sample`);
    if (tbl && data.sample) {
      tbl.innerHTML = Object.entries(data.sample)
        .map(([k, v]) => `<tr><td>${k}</td><td><code>${v}</code></td></tr>`)
        .join("");
    }

    const pre = $(`hwwiz-${kind}-preview`);
    if (pre && Array.isArray(data.raw_preview)) {
      pre.textContent = data.raw_preview.join("\n");
    }

    // INFO ident block
    if (data.ident && typeof data.ident === "object") {
      if (tbl) {
        tbl.innerHTML += Object.entries(data.ident)
          .map(([k, v]) => `<tr><td>fw:${k}</td><td><code>${v}</code></td></tr>`)
          .join("");
      }
    }
  }

  function populatePorts(selectId, ports, i2cBuses) {
    const sel = $(selectId);
    if (!sel) return;
    sel.innerHTML = '<option value="">— select port —</option>';
    if (Array.isArray(ports)) {
      ports.forEach(p => {
        const opt = document.createElement("option");
        opt.value = p.path;
        opt.textContent = p.description ? `${p.path}  [${p.description}]` : p.path;
        if (p.hint) opt.title = p.hint;
        sel.appendChild(opt);
      });
    }
    if (Array.isArray(i2cBuses)) {
      i2cBuses.forEach(b => {
        const opt = document.createElement("option");
        opt.value = `i2c:${b.bus}`;
        opt.textContent = `I²C bus ${b.bus}`;
        sel.appendChild(opt);
      });
    }
    // Add a manual entry option
    const manOpt = document.createElement("option");
    manOpt.value = "__manual__";
    manOpt.textContent = "Custom path…";
    sel.appendChild(manOpt);
  }

  /* ------------------------------------------------------------------ *
   * State                                                                *
   * ------------------------------------------------------------------ */

  let _step = 0;
  const STEPS = ["scan", "gps", "compass", "motor", "finish"];
  const DEVICE_STEPS = ["gps", "compass", "motor"];

  // Collected results per device kind
  const _results = { gps: null, compass: null, motor: null };
  // Decisions: "use" | "skip" | null
  const _decisions = { gps: null, compass: null, motor: null };

  let _scanData = null;  // last /api/hw/scan response
  let _probing = false;

  /* ------------------------------------------------------------------ *
   * Navigation                                                           *
   * ------------------------------------------------------------------ */

  function _goTo(step) {
    _step = Math.max(0, Math.min(STEPS.length - 1, step));
    // Update stepper
    const items = document.querySelectorAll("#hwwiz-stepper .step");
    items.forEach(li => {
      const s = parseInt(li.dataset.step, 10);
      li.classList.toggle("active", s === _step);
      li.classList.toggle("done", s < _step);
    });
    // Show correct section
    document.querySelectorAll("#hwwiz .wiz-step").forEach(sec => {
      const s = parseInt(sec.dataset.step, 10);
      sec.classList.toggle("hidden", s !== _step);
    });
    // Update subtitle
    const subtitles = ["Scan & identify your devices", "GPS receiver",
                       "Compass / AHRS", "Motor controller", "Review & save"];
    const sub = $("hwwiz-subtitle");
    if (sub) sub.textContent = subtitles[_step] || "";
    // Back / next / finish buttons
    const back = $("hwwiz-back");
    const next = $("hwwiz-next");
    const finish = $("hwwiz-finish");
    if (back) back.disabled = (_step === 0);
    if (next) next.classList.toggle("hidden", _step === STEPS.length - 1);
    if (finish) finish.classList.toggle("hidden", _step !== STEPS.length - 1);
    // Populate review on last step
    if (_step === STEPS.length - 1) _buildReview();
  }

  function _next() {
    if (_step < STEPS.length - 1) _goTo(_step + 1);
  }

  function _back() {
    if (_step > 0) _goTo(_step - 1);
  }

  /* ------------------------------------------------------------------ *
   * Scan                                                                 *
   * ------------------------------------------------------------------ */

  async function _doScan() {
    const btn = $("hwwiz-rescan");
    const list = $("hwwiz-scan-list");
    const caps = $("hwwiz-scan-caps");
    if (btn) btn.disabled = true;
    if (list) list.innerHTML = '<p class="hint">Scanning…</p>';
    if (caps) hide(caps);

    try {
      const resp = await fetch("/api/hw/scan");
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      _scanData = await resp.json();
    } catch (e) {
      if (list) list.innerHTML = `<p class="hint err">Scan failed: ${e.message}</p>`;
      if (btn) btn.disabled = false;
      return;
    }

    // Render capabilities
    if (caps && _scanData.capabilities) {
      const c = _scanData.capabilities;
      const lines = [];
      if (c.serial) lines.push("Serial");
      if (c.i2c) lines.push("I²C");
      if (!c.serial && !c.i2c) lines.push("No hardware capabilities detected (demo/sim mode?)");
      caps.textContent = `Available: ${lines.join(", ")}`;
      show(caps);
    }

    // Render port list
    const ports = _scanData.ports || [];
    const buses = _scanData.i2c_buses || [];
    const known = _scanData.known_i2c || [];
    let html = "";
    if (ports.length === 0 && buses.length === 0) {
      html = '<p class="hint">No serial ports or I²C buses found. Connect your devices and rescan.</p>';
    } else {
      ports.forEach(p => {
        html += `<div class="scan-row"><code>${p.path}</code>`;
        if (p.description) html += ` <span class="hint">${p.description}</span>`;
        if (p.hint) html += ` <span class="hint">→ ${p.hint}</span>`;
        html += "</div>";
      });
      buses.forEach(b => {
        html += `<div class="scan-row"><code>I²C bus ${b.bus}</code>`;
        if (b.label) html += ` <span class="hint">${b.label}</span>`;
        html += "</div>";
      });
      known.forEach(k => {
        html += `<div class="scan-row hint">I²C 0x${k.addr.toString(16).padStart(2, "0")} @ bus ${k.bus}: ${k.desc}</div>`;
      });
    }
    if (list) list.innerHTML = html;

    // Populate port selects in later steps
    DEVICE_STEPS.forEach(kind => {
      populatePorts(`hwwiz-${kind}-port`, ports, buses);
      // Pre-select hinted port if available
      const sel = $(`hwwiz-${kind}-port`);
      if (sel) {
        const hinted = ports.find(p => p.hint && p.hint.includes(kind));
        if (hinted) sel.value = hinted.path;
      }
    });

    if (btn) btn.disabled = false;
  }

  /* ------------------------------------------------------------------ *
   * Probe                                                                *
   * ------------------------------------------------------------------ */

  async function _doProbe(kind) {
    if (_probing) return;
    _probing = true;

    const portSel = $(`hwwiz-${kind}-port`);
    const statusEl = $(`hwwiz-${kind}-status`);
    const probeBtn = $(`hwwiz-${kind}-probe`);

    clearResult(kind);
    if (probeBtn) probeBtn.disabled = true;
    setStatus(`hwwiz-${kind}-status`, "Probing…");

    const portVal = portSel ? portSel.value : "";
    if (!portVal || portVal === "__manual__") {
      const custom = prompt("Enter port path (e.g. /dev/ttyUSB0 or i2c:1:0x42):");
      if (!custom) {
        setStatus(`hwwiz-${kind}-status`, "", false);
        if (probeBtn) probeBtn.disabled = false;
        _probing = false;
        return;
      }
      if (portSel) portSel.value = "__manual__";
    }

    let port = portVal === "__manual__" ? null : portVal;
    if (!port) {
      if (probeBtn) probeBtn.disabled = false;
      _probing = false;
      return;
    }

    // Build payload
    const payload = { target: "serial", port };
    // Baud hints per device kind
    const baudHints = {
      gps:     [38400, 9600, 4800, 115200],
      compass: [9600, 115200, 4800],
      motor:   [115200, 4800],
    };
    payload.bauds = baudHints[kind] || [115200, 38400, 9600, 4800];
    payload.duration_s = 2.5;

    if (kind === "gps") {
      const activeEl = $("hwwiz-gps-active");
      payload.active_ubx_ident = activeEl ? activeEl.checked : false;
    }

    // I2C variant for motor (bus:addr string)
    if (port.startsWith("i2c:")) {
      const parts = port.split(":");
      const bus = parseInt(parts[1], 10);
      // Known helm Pico address
      const addr = parts[2] ? parseInt(parts[2], 0) : 0x42;
      payload.target = "i2c";
      delete payload.port;
      delete payload.bauds;
      delete payload.duration_s;
      payload.bus = bus;
      payload.addr = addr;
      payload.kind = kind === "motor" ? "helm-pico" : "auto";
    }

    try {
      const resp = await fetch("/api/hw/probe", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const data = await resp.json();
      if (resp.status === 409) {
        setStatus(`hwwiz-${kind}-status`, "Another probe is already running. Please wait.", true);
        _probing = false;
        if (probeBtn) probeBtn.disabled = false;
        return;
      }
      if (!resp.ok) {
        setStatus(`hwwiz-${kind}-status`, data.detail || `Error ${resp.status}`, true);
        _probing = false;
        if (probeBtn) probeBtn.disabled = false;
        return;
      }

      _results[kind] = data;
      renderResult(kind, data);

      // Confidence-aware status message
      const det = data.detected || "unknown";
      const conf = data.confidence || "none";
      const kindLabels = {
        "ublox": "u-blox GPS",
        "nmea-gps": "NMEA GPS",
        "nmea-compass": "NMEA compass",
        "nmea-depth": "NMEA depth",
        "witmotion-imu": "WitMotion IMU",
        "vanchor-motor": "Vanchor motor",
        "unknown": "unknown device",
      };
      const label = kindLabels[det] || det;
      let msg = "";
      if (conf === "high") msg = `Detected: ${label} (high confidence)`;
      else if (conf === "medium") msg = `Likely: ${label} (medium confidence) — verify`;
      else msg = `No matching device found on this port`;
      setStatus(`hwwiz-${kind}-status`, msg, conf === "none");

      // Check mismatch (e.g. GPS probe on motor step)
      const expectedMap = {
        gps:     ["ublox", "nmea-gps"],
        compass: ["witmotion-imu", "nmea-compass"],
        motor:   ["vanchor-motor"],
      };
      const expected = expectedMap[kind] || [];
      const mismatch = conf !== "none" && !expected.includes(det);
      const forceEl = $(`hwwiz-${kind}-force`);
      if (forceEl) {
        forceEl.closest("label") && (forceEl.closest("label").style.display =
          mismatch ? "" : "none");
      }
      if (mismatch) {
        setStatus(`hwwiz-${kind}-status`,
          `Warning: expected ${expected.join(" or ")} but detected ${label}. Enable "Use anyway" to override.`,
          true);
      }
    } catch (e) {
      setStatus(`hwwiz-${kind}-status`, `Network error: ${e.message}`, true);
    }

    _probing = false;
    if (probeBtn) probeBtn.disabled = false;
  }

  /* ------------------------------------------------------------------ *
   * Review & save                                                        *
   * ------------------------------------------------------------------ */

  function _buildReview() {
    const tbody = $("hwwiz-review-body");
    if (!tbody) return;

    const rows = [];
    DEVICE_STEPS.forEach(kind => {
      const skipEl = $(`hwwiz-${kind}-skip`);
      const skipped = skipEl && skipEl.checked;
      const result = _results[kind];
      const forceEl = $(`hwwiz-${kind}-force`);
      const forced = forceEl && forceEl.checked;

      let action = "—";
      let detected = "not probed";
      if (skipped) {
        action = "Skip (keep current)";
      } else if (result) {
        detected = result.detected || "unknown";
        if (result.suggest) {
          const s = result.suggest;
          const parts = [];
          const fields = s.fields || {};
          if (s.source) parts.push(`source: ${s.source}`);
          const portVal = fields[`${kind}_port`];
          const baudVal = fields[`${kind}_baud`] || fields.baudrate;
          if (portVal) parts.push(`port: ${portVal}`);
          if (baudVal) parts.push(`baud: ${baudVal}`);
          action = parts.length ? `Set ${parts.join(", ")}` : "Apply suggestion";
          if (forced) action += " (forced)";
        } else {
          action = result.detected !== "unknown"
            ? `Detected (no auto-config — apply manually)`
            : "No device found";
        }
      }

      rows.push(`<tr><td>${kind}</td><td>${detected}</td><td>${action}</td></tr>`);
    });

    tbody.innerHTML = rows.join("");
  }

  async function _doSave() {
    const statusEl = $("hwwiz-save-status");
    setStatus("hwwiz-save-status", "Saving…");
    const finBtn = $("hwwiz-finish");
    if (finBtn) finBtn.disabled = true;

    // Build a config patch from suggestions; suggest.fields already carries
    // the correct config keys (gps_port, gps_baud, compass_source, etc.)
    const patch = {};
    DEVICE_STEPS.forEach(kind => {
      const skipEl = $(`hwwiz-${kind}-skip`);
      if (skipEl && skipEl.checked) return;
      const result = _results[kind];
      if (!result || !result.suggest) return;
      const s = result.suggest;
      if (s.fields) Object.assign(patch, s.fields);
    });

    if (Object.keys(patch).length === 0) {
      setStatus("hwwiz-save-status", "Nothing to save (no suggestions). Update settings manually.", false);
      if (finBtn) finBtn.disabled = false;
      return;
    }

    try {
      const resp = await fetch("/api/config/devices", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({hardware: patch}),
      });
      if (!resp.ok) {
        const d = await resp.json().catch(() => ({}));
        setStatus("hwwiz-save-status", `Save failed: ${d.detail || resp.status}`, true);
        if (finBtn) finBtn.disabled = false;
        return;
      }
    } catch (e) {
      setStatus("hwwiz-save-status", `Network error: ${e.message}`, true);
      if (finBtn) finBtn.disabled = false;
      return;
    }

    setStatus("hwwiz-save-status", "Saved! Restarting the server to apply…");

    // Trigger restart
    try {
      await fetch("/api/restart", { method: "POST" });
    } catch (_) {
      // best-effort
    }

    // Countdown
    const cd = $("hwwiz-restart-countdown");
    if (cd) {
      show(cd);
      let n = 8;
      const tick = () => {
        cd.textContent = `Reloading in ${n}s…`;
        if (n-- > 0) setTimeout(tick, 1000);
        else location.reload();
      };
      tick();
    } else {
      setTimeout(() => location.reload(), 8000);
    }
  }

  /* ------------------------------------------------------------------ *
   * Open / Close                                                         *
   * ------------------------------------------------------------------ */

  function _open() {
    const wiz = $("hwwiz");
    const scrim = $("hwwiz-scrim");
    if (!wiz) return;
    show(wiz);
    show(scrim);
    _step = -1;  // force redraw
    _goTo(0);
    // Auto-scan on open
    _doScan();
  }

  function _close() {
    const wiz = $("hwwiz");
    const scrim = $("hwwiz-scrim");
    if (wiz) hide(wiz);
    if (scrim) hide(scrim);
  }

  /* ------------------------------------------------------------------ *
   * Wire up event listeners (deferred until DOM ready)                   *
   * ------------------------------------------------------------------ */

  function _wire() {
    const closeBtn = $("hwwiz-close");
    const scrim    = $("hwwiz-scrim");
    const openBtn  = $("hwwiz-open");
    const backBtn  = $("hwwiz-back");
    const nextBtn  = $("hwwiz-next");
    const finBtn   = $("hwwiz-finish");
    const rescanBtn = $("hwwiz-rescan");

    if (closeBtn) closeBtn.addEventListener("click", _close);
    if (scrim)    scrim.addEventListener("click", _close);
    if (openBtn)  openBtn.addEventListener("click", _open);
    if (backBtn)  backBtn.addEventListener("click", _back);
    if (nextBtn)  nextBtn.addEventListener("click", _next);
    if (finBtn)   finBtn.addEventListener("click", _doSave);
    if (rescanBtn) rescanBtn.addEventListener("click", _doScan);

    // Per-device probe buttons
    DEVICE_STEPS.forEach(kind => {
      const probeBtn = $(`hwwiz-${kind}-probe`);
      if (probeBtn) probeBtn.addEventListener("click", () => _doProbe(kind));
      // Skip checkbox toggles probe button state
      const skipEl = $(`hwwiz-${kind}-skip`);
      if (skipEl && probeBtn) {
        skipEl.addEventListener("change", () => {
          probeBtn.disabled = skipEl.checked;
          if (skipEl.checked) clearResult(kind);
        });
      }
    });

    // Keyboard: Escape closes
    document.addEventListener("keydown", e => {
      if (e.key === "Escape") {
        const wiz = $("hwwiz");
        if (wiz && !wiz.classList.contains("hidden")) _close();
      }
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", _wire);
  } else {
    _wire();
  }

  /* ------------------------------------------------------------------ *
   * Public API                                                           *
   * ------------------------------------------------------------------ */

  const VA = (typeof window !== "undefined") ? (window.VA || (window.VA = {})) : {};
  VA.openHwWizard = _open;

})();
