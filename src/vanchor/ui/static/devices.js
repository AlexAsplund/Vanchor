/* Vanchor-NG — Devices & hardware config (Settings → "Devices & hardware").
 *
 * Lazy-loads on the card's first <details> `toggle`: GET /api/config/devices,
 * then renders a master Mode (Simulation vs Hardware), per-device source selects
 * (GPS / Compass / Depth → Sim · Serial · NMEA; Motor → Sim · Serial · Both),
 * serial-port inputs (revealed only when any source is "serial"), and the NMEA
 * TCP bridge toggle/port.
 *
 * Save → POST /api/config/devices with {hardware, nmea_tcp}. Changes apply on
 * RESTART (the backend reports restart_required), so on success we show a clear
 * "Saved — restart the app to apply" notice. "Reset to current" re-fetches.
 *
 * Degrades gracefully: if GET 404s (older backend), the card shows an
 * "unavailable" hint and hides the form. Everything guards missing fields.
 *
 * Contract (must match the backend):
 *   GET  /api/config/devices ->
 *     { hardware:{enabled, gps_port, compass_port, motor_port, baudrate,
 *                 gps_source, compass_source, depth_source, motor_source},
 *       nmea_tcp:{enabled, port},
 *       options:{sensor:["sim","serial","nmea"], motor:["sim","serial","both"]},
 *       restart_required }
 *   POST /api/config/devices  body {hardware:{...}, nmea_tcp:{...}}
 *     -> { ok:true, restart_required:true }
 *   A null *_source means "follow `enabled`" (Auto).
 */
"use strict";

(function () {
  const $ = (id) => document.getElementById(id);
  const card = $("devices-card");
  if (!card || !window.VA) return;

  // Labels per option value. Sensors and motor share most, Motor differs.
  const SENSOR_LABELS = {
    sim: "Simulated",
    serial: "Serial (wired)",
    nmea: "NMEA (from phone/plotter)",
  };
  const MOTOR_LABELS = {
    sim: "Simulated",
    serial: "Serial (real servo)",
    both: "Both (sim boat + real servo)",
  };
  const AUTO_LABEL = "Auto (follows mode)";

  // Fallbacks if the backend omits `options`.
  const DEFAULT_OPTS = {
    sensor: ["sim", "serial", "nmea"],
    motor: ["sim", "serial", "both"],
  };

  const SRC_FIELDS = [
    { id: "dev-src-gps", key: "gps_source", kind: "sensor" },
    { id: "dev-src-compass", key: "compass_source", kind: "sensor" },
    { id: "dev-src-depth", key: "depth_source", kind: "sensor" },
    { id: "dev-src-motor", key: "motor_source", kind: "motor" },
  ];

  let loaded = false;
  let options = DEFAULT_OPTS;
  let lastRestartRequired = false;

  function setStatus(msg, kind) {
    const el = $("dev-status");
    if (!el) return;
    el.textContent = msg || "";
    el.className = "hint" + (kind ? " " + kind : "");
  }

  function setBadge(txt) {
    const b = $("dev-state");
    if (b) b.textContent = txt || "";
  }

  // ---- rendering --------------------------------------------------------

  function fillSelect(sel, kind) {
    if (!sel) return;
    const vals = (options && options[kind]) || DEFAULT_OPTS[kind] || [];
    const labels = kind === "motor" ? MOTOR_LABELS : SENSOR_LABELS;
    sel.innerHTML = "";
    // Null source = Auto.
    const auto = document.createElement("option");
    auto.value = "";
    auto.textContent = AUTO_LABEL;
    sel.appendChild(auto);
    vals.forEach((v) => {
      const o = document.createElement("option");
      o.value = v;
      o.textContent = labels[v] || v;
      sel.appendChild(o);
    });
  }

  function setSelectValue(sel, val) {
    if (!sel) return;
    // null / undefined -> Auto (""). Unknown value also falls back to Auto.
    const want = val == null ? "" : String(val);
    const has = Array.prototype.some.call(sel.options, (o) => o.value === want);
    sel.value = has ? want : "";
  }

  // Reveal serial settings only when any source is explicitly "serial".
  function anySerial() {
    return SRC_FIELDS.some((f) => {
      const sel = $(f.id);
      return sel && sel.value === "serial";
    });
  }

  function syncSerial() {
    const box = $("dev-serial");
    if (box) box.classList.toggle("hidden", !anySerial());
  }

  function syncMode() {
    const enabled = readEnabled();
    const seg = $("dev-mode");
    if (seg) {
      Array.prototype.forEach.call(seg.querySelectorAll("button"), (b) => {
        b.classList.toggle("on", b.dataset.on === String(enabled));
      });
    }
  }

  function syncNmea() {
    const on = $("dev-nmea-enabled");
    const row = $("dev-nmea-port-row");
    if (row) row.classList.toggle("dev-dim", !(on && on.checked));
  }

  // ---- form <-> state ---------------------------------------------------

  // Mode is stored on the seg's selected button (data-on "true"/"false").
  function readEnabled() {
    const seg = $("dev-mode");
    if (!seg) return false;
    const on = seg.querySelector("button.on");
    return on ? on.dataset.on === "true" : false;
  }

  function setEnabled(enabled) {
    const seg = $("dev-mode");
    if (!seg) return;
    Array.prototype.forEach.call(seg.querySelectorAll("button"), (b) => {
      b.classList.toggle("on", b.dataset.on === String(!!enabled));
    });
  }

  function num(v) {
    const n = Number(v);
    return Number.isFinite(n) ? n : null;
  }

  function setVal(id, v) {
    const el = $(id);
    if (el) el.value = v == null ? "" : String(v);
  }

  function render(cfg) {
    cfg = cfg || {};
    options = cfg.options && typeof cfg.options === "object" ? cfg.options : DEFAULT_OPTS;
    const hw = cfg.hardware && typeof cfg.hardware === "object" ? cfg.hardware : {};
    const nmea = cfg.nmea_tcp && typeof cfg.nmea_tcp === "object" ? cfg.nmea_tcp : {};
    lastRestartRequired = !!cfg.restart_required;

    setEnabled(!!hw.enabled);

    SRC_FIELDS.forEach((f) => {
      const sel = $(f.id);
      fillSelect(sel, f.kind);
      setSelectValue(sel, hw[f.key]);
    });

    setVal("dev-gps-port", hw.gps_port);
    setVal("dev-compass-port", hw.compass_port);
    setVal("dev-motor-port", hw.motor_port);
    setVal("dev-baudrate", hw.baudrate);

    const nEn = $("dev-nmea-enabled");
    if (nEn) nEn.checked = !!nmea.enabled;
    setVal("dev-nmea-port", nmea.port);

    syncMode();
    syncSerial();
    syncNmea();
    setBadge(hw.enabled ? "● hardware" : "sim");
  }

  // Assemble the POST body. Empty source select -> null (Auto). Empty text
  // ports -> null so the backend keeps/uses its default. baudrate/port -> number.
  function collect() {
    const srcVal = (id) => {
      const sel = $(id);
      const v = sel ? sel.value : "";
      return v === "" ? null : v;
    };
    const textVal = (id) => {
      const el = $(id);
      const v = el ? el.value.trim() : "";
      return v === "" ? null : v;
    };
    const nEn = $("dev-nmea-enabled");
    return {
      hardware: {
        enabled: readEnabled(),
        gps_port: textVal("dev-gps-port"),
        compass_port: textVal("dev-compass-port"),
        motor_port: textVal("dev-motor-port"),
        baudrate: num($("dev-baudrate") && $("dev-baudrate").value),
        gps_source: srcVal("dev-src-gps"),
        compass_source: srcVal("dev-src-compass"),
        depth_source: srcVal("dev-src-depth"),
        motor_source: srcVal("dev-src-motor"),
      },
      nmea_tcp: {
        enabled: !!(nEn && nEn.checked),
        port: num($("dev-nmea-port") && $("dev-nmea-port").value),
      },
    };
  }

  // ---- load / save ------------------------------------------------------

  function showUnavailable() {
    const u = $("dev-unavailable");
    const body = $("dev-body");
    if (u) u.classList.remove("hidden");
    if (body) body.classList.add("hidden");
    setBadge("n/a");
  }

  // Fetch directly (not VA.getJSON) so we can read the HTTP status: an older
  // backend returns 404 here, which must degrade to "unavailable", not error.
  function load() {
    setStatus("Loading…", "busy");
    fetch("/api/config/devices")
      .then((r) => {
        if (r.status === 404) {
          showUnavailable();
          setStatus("");
          return null;
        }
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      })
      .then((cfg) => {
        if (!cfg) return; // 404 already handled
        // A valid device config has hardware/options; anything else = not this API.
        if (!cfg.hardware && !cfg.options) {
          showUnavailable();
          setStatus("");
          return;
        }
        const u = $("dev-unavailable");
        const body = $("dev-body");
        if (u) u.classList.add("hidden");
        if (body) body.classList.remove("hidden");
        render(cfg);
        setStatus("");
        loaded = true;
      })
      .catch(() => {
        setStatus("Couldn't load device config.", "err");
      });
  }

  function save() {
    const body = collect();
    const btn = $("dev-save");
    if (btn) btn.disabled = true;
    setStatus("Saving…", "busy");
    VA.postJSON("/api/config/devices", body)
      .then((res) => {
        if (btn) btn.disabled = false;
        if (res && res.ok === false) {
          setStatus("Save rejected: " + (res.error || "invalid"), "err");
          return;
        }
        setStatus("Saved — restart the app to apply.", "ok");
      })
      .catch(() => {
        if (btn) btn.disabled = false;
        setStatus("Save failed.", "err");
      });
  }

  // ---- wiring -----------------------------------------------------------

  // Mode segmented control.
  const seg = $("dev-mode");
  if (seg) {
    seg.addEventListener("click", (e) => {
      const b = e.target.closest("button[data-on]");
      if (!b) return;
      setEnabled(b.dataset.on === "true");
      syncMode();
    });
  }

  // Source selects → toggle serial disclosure.
  SRC_FIELDS.forEach((f) => {
    const sel = $(f.id);
    if (sel) sel.addEventListener("change", syncSerial);
  });

  const nEn = $("dev-nmea-enabled");
  if (nEn) nEn.addEventListener("change", syncNmea);

  const saveBtn = $("dev-save");
  if (saveBtn) saveBtn.addEventListener("click", save);

  const resetBtn = $("dev-reset");
  if (resetBtn) resetBtn.addEventListener("click", load);

  // Lazy: fetch only on the card's first open.
  card.addEventListener("toggle", () => {
    if (card.open && !loaded) load();
  });
})();
