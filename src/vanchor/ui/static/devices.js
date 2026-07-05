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
    hwt901b: "HWT901B AHRS",
    ublox: "u-blox M9N (UBX)",
    none: "Not connected",
  };
  const MOTOR_LABELS = {
    sim: "Simulated",
    serial: "Serial (real servo)",
    both: "Both (sim boat + real servo)",
    none: "Not connected",
  };
  const BATTERY_LABELS = {
    sim: "Simulated",
    none: "None (no gauge)",
    ina226: "INA226 shunt gauge",
  };
  const AUTO_LABEL = "Auto (follows mode)";

  // Fallbacks if the backend omits `options`.
  const DEFAULT_OPTS = {
    sensor: ["sim", "serial", "nmea", "none"],
    gps: ["sim", "serial", "nmea", "none", "ublox"],
    compass: ["sim", "serial", "nmea", "hwt901b", "none"],
    motor: ["sim", "serial", "both", "none"],
    battery: ["sim", "none", "ina226"],
  };

  const SRC_FIELDS = [
    { id: "dev-src-gps", key: "gps_source", kind: "gps" },
    { id: "dev-src-compass", key: "compass_source", kind: "compass" },
    { id: "dev-src-depth", key: "depth_source", kind: "sensor" },
    { id: "dev-src-motor", key: "motor_source", kind: "motor" },
    { id: "dev-src-battery", key: "battery_source", kind: "battery" },
  ];

  let loaded = false;
  let options = DEFAULT_OPTS;
  let driverMenus = {};   // { source: menu-schema } — shown on selection
  let activeMenus = [];   // menus from the running devices (live values)
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
    const labels = kind === "motor" ? MOTOR_LABELS
      : kind === "battery" ? BATTERY_LABELS : SENSOR_LABELS;
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

  // Reveal serial settings (ports + baud) when any source is WIRED -- i.e. not
  // Auto/sim/nmea. Covers "serial", motor "both", and pluggable serial drivers
  // like "hwt901b" (which needs its port set), without hardcoding driver names.
  const _WIRELESS = { "": 1, sim: 1, nmea: 1 };
  function anySerial() {
    return SRC_FIELDS.some((f) => {
      const sel = $(f.id);
      return sel && !(sel.value in _WIRELESS);
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
    fillPortPicks();  // reflect the loaded ports in the dropdowns
    // Per-port serial line settings (baud + data bits + parity + stop bits).
    ["gps", "compass", "motor"].forEach((d) => {
      setVal("dev-" + d + "-baud", hw[d + "_baud"]);
      setVal("dev-" + d + "-bytesize", hw[d + "_bytesize"]);
      setVal("dev-" + d + "-parity", hw[d + "_parity"]);
      setVal("dev-" + d + "-stopbits", hw[d + "_stopbits"]);
    });

    // Sim-motor actuation shaping (#36) — simulator-only response tuning.
    const sm = cfg.sim_motor && typeof cfg.sim_motor === "object" ? cfg.sim_motor : {};
    setVal("dev-simmotor-revdelay", sm.reverse_delay_s);
    setVal("dev-simmotor-slew", sm.thrust_slew_per_s);
    setVal("dev-simmotor-lag", sm.thrust_lag_tau_s);

    const nEn = $("dev-nmea-enabled");
    if (nEn) nEn.checked = !!nmea.enabled;
    setVal("dev-nmea-port", nmea.port);

    syncMode();
    syncSerial();
    syncNmea();
    setBadge(hw.enabled ? "● hardware" : "sim");
    driverMenus = (cfg.driver_menus && typeof cfg.driver_menus === "object") ? cfg.driver_menus : {};
    activeMenus = Array.isArray(cfg.menus) ? cfg.menus : [];
    refreshMenus();
  }

  // Show the menu for the currently-SELECTED source of each device (from the
  // driver schema, so it appears the instant you pick e.g. HWT901B — before any
  // restart), plus any running device's live menu not already covered.
  function refreshMenus() {
    const list = [];
    SRC_FIELDS.forEach((f) => {
      const sel = $(f.id);
      const src = sel ? sel.value : "";
      if (src && driverMenus[src]) list.push(driverMenus[src]);
    });
    (activeMenus || []).forEach((m) => {
      if (!list.some((x) => x.device === m.device)) list.push(m);
    });
    renderMenus(list);
  }

  // ---- device-specific menus (driver device_menu(): settings + actions) --
  // Rendered generically from the schema each active device advertises; a
  // setting change POSTs /api/device/setting, an action POSTs /api/device/action.
  function renderMenus(menus) {
    const host = $("dev-menus");
    if (!host) return;
    host.innerHTML = "";
    (menus || []).forEach((menu) => {
      const box = document.createElement("div");
      box.className = "dev-menu";
      const h = document.createElement("div");
      h.className = "drawer-section";
      h.textContent = menu.title || (menu.device + " settings");
      box.appendChild(h);
      (menu.settings || []).forEach((s) => box.appendChild(renderSetting(menu.device, s, box)));
      if ((menu.actions || []).length) {
        const row = document.createElement("div");
        row.className = "btn-row";
        menu.actions.forEach((a) => {
          const btn = document.createElement("button");
          btn.type = "button"; btn.className = "btn-ghost";
          btn.textContent = a.label || a.name;
          if (a.help) btn.title = a.help;
          btn.addEventListener("click", () => runAction(menu.device, a.name, box));
          row.appendChild(btn);
        });
        box.appendChild(row);
      }
      const out = document.createElement("div");
      out.className = "hint dev-menu-out";
      box.appendChild(out);
      host.appendChild(box);
      applyShownWhen(box);
    });
  }

  function renderSetting(device, s, box) {
    const wrap = document.createElement("label");
    wrap.className = "slider-row dev-set";
    wrap.dataset.key = s.key;
    if (s.shown_when) wrap.dataset.shownWhen = JSON.stringify(s.shown_when);
    if (s.help) wrap.title = s.help;
    const lab = document.createElement("span");
    lab.textContent = s.label + (s.unit ? " (" + s.unit + ")" : "");
    let input;
    if (s.type === "select") {
      input = document.createElement("select");
      (s.options || []).forEach((o) => {
        const op = document.createElement("option");
        op.value = o; op.textContent = o;
        if (o === s.value) op.selected = true;
        input.appendChild(op);
      });
    } else if (s.type === "toggle") {
      input = document.createElement("input");
      input.type = "checkbox"; input.checked = !!s.value;
    } else {
      input = document.createElement("input");
      input.type = "number";
      if (s.min != null) input.min = s.min;
      if (s.max != null) input.max = s.max;
      if (s.step != null) input.step = s.step;
      input.value = s.value;
    }
    input.dataset.ctrl = s.key;
    input.addEventListener("change", () => {
      const value = s.type === "toggle" ? input.checked
        : s.type === "number" ? parseFloat(input.value) : input.value;
      VA.postJSON("/api/device/setting", { device, key: s.key, value })
        .then((r) => {
          applyShownWhen(box);   // e.g. reveal manual declination
          const out = box.querySelector(".dev-menu-out");
          if (out) out.textContent = (r && r.restart_required)
            ? "Saved — restart to apply." : "Saved.";
        })
        .catch(() => {});
    });
    wrap.append(lab, input);
    return wrap;
  }

  function applyShownWhen(box) {
    box.querySelectorAll(".dev-set[data-shown-when]").forEach((el) => {
      let cond;
      try { cond = JSON.parse(el.dataset.shownWhen); } catch (e) { return; }
      const show = Object.keys(cond).every((k) => {
        const ctrl = box.querySelector('[data-ctrl="' + k + '"]');
        const v = ctrl ? (ctrl.type === "checkbox" ? ctrl.checked : ctrl.value) : undefined;
        return String(v) === String(cond[k]);
      });
      el.classList.toggle("hidden", !show);
    });
  }

  function runAction(device, name, box) {
    const out = box.querySelector(".dev-menu-out");
    if (out) out.textContent = "…";
    VA.postJSON("/api/device/action", { device, action: name })
      .then((r) => {
        if (!out) return;
        let msg = (r && r.message) || (r && r.ok ? "Done." : "Action failed.");
        if (r && r.status) msg += "  " + Object.entries(r.status)
          .map(([k, v]) => k + "=" + v).join(", ");
        out.textContent = msg;
      })
      .catch(() => { if (out) out.textContent = "Action failed."; });
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
    // Sim-motor shaping: only send keys the user actually set (null would clobber).
    const simMotor = {};
    [["reverse_delay_s", "dev-simmotor-revdelay"],
     ["thrust_slew_per_s", "dev-simmotor-slew"],
     ["thrust_lag_tau_s", "dev-simmotor-lag"]].forEach(([k, id]) => {
      const v = num($(id) && $(id).value);
      if (v != null) simMotor[k] = v;
    });
    // Per-port serial line settings -> only send keys the user actually set.
    const serial = {};
    ["gps", "compass", "motor"].forEach((d) => {
      const b = num($("dev-" + d + "-baud") && $("dev-" + d + "-baud").value);
      if (b != null) serial[d + "_baud"] = b;
      const bs = num($("dev-" + d + "-bytesize") && $("dev-" + d + "-bytesize").value);
      if (bs != null) serial[d + "_bytesize"] = bs;
      const par = ($("dev-" + d + "-parity") || {}).value;
      if (par) serial[d + "_parity"] = par;
      const sb = num($("dev-" + d + "-stopbits") && $("dev-" + d + "-stopbits").value);
      if (sb != null) serial[d + "_stopbits"] = sb;
    });
    return {
      hardware: {
        enabled: readEnabled(),
        gps_port: textVal("dev-gps-port"),
        compass_port: textVal("dev-compass-port"),
        motor_port: textVal("dev-motor-port"),
        ...serial,
        gps_source: srcVal("dev-src-gps"),
        compass_source: srcVal("dev-src-compass"),
        depth_source: srcVal("dev-src-depth"),
        motor_source: srcVal("dev-src-motor"),
        battery_source: srcVal("dev-src-battery"),
      },
      nmea_tcp: {
        enabled: !!(nEn && nEn.checked),
        port: num($("dev-nmea-port") && $("dev-nmea-port").value),
      },
      sim_motor: simMotor,
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
  // Auto-detect serial ports (OpenPlotter-style). Each port field is a DROPDOWN
  // of the detected devices (stable /dev/serial/by-id + on-board UART aliases
  // first, marked ★), plus a "Custom path…" option that reveals a text field for
  // anything not auto-detected. The hidden text input stays the source of truth
  // (collect() reads it); the dropdown just writes the chosen path into it.
  const PORT_PICKS = [
    ["dev-gps-port-pick", "dev-gps-port"],
    ["dev-compass-port-pick", "dev-compass-port"],
    ["dev-motor-port-pick", "dev-motor-port"],
  ];
  const PORT_CUSTOM = "__custom__";
  let serialPorts = [];

  function fillPortPicks() {
    PORT_PICKS.forEach(([pickId, inputId]) => {
      const sel = $(pickId);
      if (!sel) return;
      const cur = ($(inputId) || {}).value || "";
      sel.innerHTML = "";
      const none = document.createElement("option");
      none.value = ""; none.textContent = "— none —";
      sel.appendChild(none);
      let matched = cur === "";
      serialPorts.forEach((p) => {
        const o = document.createElement("option");
        o.value = p.path;
        o.textContent = (p.stable ? "★ " : "") + (p.description || p.path);
        if (p.path === cur) matched = true;
        sel.appendChild(o);
      });
      if (cur && !matched) { // preserve a configured path that wasn't detected
        const o = document.createElement("option");
        o.value = cur; o.textContent = cur + " (configured)";
        sel.appendChild(o);
      }
      const custom = document.createElement("option");
      custom.value = PORT_CUSTOM; custom.textContent = "Custom path…";
      sel.appendChild(custom);
      syncPortPick(pickId, inputId);
    });
  }

  function syncPortPick(pickId, inputId) {
    const sel = $(pickId), inp = $(inputId);
    if (!sel || !inp) return;
    const cur = inp.value || "";
    const known = Array.prototype.some.call(sel.options,
      (o) => o.value === cur && o.value !== PORT_CUSTOM);
    const customRow = inp.closest(".dev-port-custom");
    sel.value = (known || cur === "") ? cur : PORT_CUSTOM;
    if (customRow) customRow.classList.toggle("hidden", sel.value !== PORT_CUSTOM);
  }

  function onPortPick(pickId, inputId) {
    const sel = $(pickId), inp = $(inputId);
    if (!sel || !inp) return;
    const customRow = inp.closest(".dev-port-custom");
    if (sel.value === PORT_CUSTOM) {
      if (customRow) customRow.classList.remove("hidden");
      inp.focus();
    } else {
      inp.value = sel.value;  // the dropdown IS the source; mirror into the input
      if (customRow) customRow.classList.add("hidden");
    }
  }

  function loadSerialPorts() {
    fetch("/api/devices/serial-ports")
      .then((r) => (r.ok ? r.json() : null))
      .then((j) => {
        serialPorts = (j && Array.isArray(j.ports)) ? j.ports : [];
        const dl = $("dev-serial-ports");  // suggestions for the custom text inputs
        if (dl) {
          dl.innerHTML = "";
          serialPorts.forEach((p) => {
            const o = document.createElement("option");
            o.value = p.path;
            if (p.description && p.description !== p.path) o.label = p.description;
            dl.appendChild(o);
          });
        }
        fillPortPicks();
      })
      .catch(() => {});
  }

  function load() {
    setStatus("Loading…", "busy");
    loadSerialPorts();
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

  // Source selects → toggle serial disclosure + show the picked driver's menu.
  SRC_FIELDS.forEach((f) => {
    const sel = $(f.id);
    if (sel) sel.addEventListener("change", () => { syncSerial(); refreshMenus(); });
  });

  // Serial-port dropdowns: mirror the pick into the (source-of-truth) input.
  PORT_PICKS.forEach(([pk, ip]) => {
    const sel = $(pk);
    if (sel) sel.addEventListener("change", () => onPortPick(pk, ip));
  });

  const nEn = $("dev-nmea-enabled");
  if (nEn) nEn.addEventListener("change", syncNmea);

  const saveBtn = $("dev-save");
  if (saveBtn) saveBtn.addEventListener("click", save);

  const resetBtn = $("dev-reset");
  if (resetBtn) resetBtn.addEventListener("click", load);

  const restartBtn = $("dev-restart");
  if (restartBtn) restartBtn.addEventListener("click", function () {
    if (!confirm("Restart the server now? The connection will drop for a few seconds.")) return;
    setStatus("Restarting the server…", "ok");
    // The response may not arrive before the process re-execs; ignore errors.
    VA.postJSON("/api/restart", {}).catch(function () {});
    // Poll until the server is back up, then reload the page.
    setTimeout(function waitBack() {
      fetch("/api/state")
        .then(function () { location.reload(); })
        .catch(function () { setTimeout(waitBack, 800); });
    }, 2500);
  });

  // Lazy: fetch only on the card's first open.
  card.addEventListener("toggle", () => {
    if (card.open && !loaded) load();
  });
})();

/* Sensor calibration (fusion) — a self-contained subsection of the Devices card.
 *
 * Records the boat's sensor noise while it sits still (motor off), then tunes the
 * GNSS/INS fusion filter to it. Lazy-loads GET /api/fusion/calibration on the
 * card's first open; if that 404s (backend without this flow) the whole section
 * stays hidden and no errors surface — same graceful-degrade pattern as above.
 *
 * Contract (must match the backend):
 *   GET  /api/fusion/calibration ->
 *     { calibration:{…}|null, capturing:bool, capture_samples:int,
 *       capture_seconds:float, enabled:bool }
 *   POST /api/fusion/calibrate/start -> { ok:true, capturing:true }  (ok:false if off)
 *   POST /api/fusion/calibrate/stop  -> { ok:true, calibration:{…}, warnings:[str] }
 *                                       or { ok:false, error:str }
 *   POST /api/fusion/calibrate/save  body { calibration:{…} } -> { ok:true }
 *   POST /api/fusion/calibrate/reset -> { ok:true }
 */
(function () {
  const $ = (id) => document.getElementById(id);
  const card = $("devices-card");
  const box = $("dev-calib");
  if (!card || !box || !window.VA) return;

  // How long a capture runs (client-side auto-stop). The button label says 30 s.
  const CAPTURE_MS = 30000;

  // Fields shown in the readout / proposal, in order. `d` = decimal places.
  const FIELDS = [
    { key: "gyro_bias_dps", label: "Gyro bias", unit: "°/s", d: 3 },
    { key: "heading_gain", label: "Heading gain", unit: "", d: 3 },
    { key: "vel_tau_s", label: "Velocity τ", unit: "s", d: 2 },
    { key: "dr_timeout_s", label: "Dead-reckoning timeout", unit: "s", d: 1 },
    { key: "crab_min_sog_mps", label: "Crab min SOG", unit: "m/s", d: 2 },
    { key: "crab_min_sog_measured_mps", label: "Crab min SOG (measured)", unit: "m/s", d: 2 },
    { key: "gps_pos_sigma_m", label: "GPS position σ", unit: "m", d: 2 },
    { key: "gps_vel_sigma_mps", label: "GPS velocity σ", unit: "m/s", d: 3 },
    { key: "heading_sigma_deg", label: "Heading σ", unit: "°", d: 2 },
    { key: "yaw_rate_sigma_dps", label: "Yaw-rate σ", unit: "°/s", d: 3 },
    { key: "samples", label: "Samples", unit: "", d: 0 },
    { key: "duration_s", label: "Duration", unit: "s", d: 1 },
  ];

  let loaded = false;
  let enabled = false;    // fusion on?
  let capturing = false;  // a capture is in flight
  let proposal = null;    // last stop() result awaiting Apply/Discard
  let pollTimer = null;
  let stopTimer = null;
  let startedAt = 0;

  function setStatus(msg, kind) {
    const el = $("dev-calib-status");
    if (!el) return;
    el.textContent = msg || "";
    el.className = "hint" + (kind ? " " + kind : "");
  }

  function fmt(v, d) {
    const n = Number(v);
    if (v == null || !Number.isFinite(n)) return "—";
    return d != null ? n.toFixed(d) : String(n);
  }

  // Render the labelled numbers of a calibration object as text rows.
  function renderCal(host, cal) {
    if (!host) return;
    host.innerHTML = "";
    FIELDS.forEach((f) => {
      if (!(f.key in cal) || cal[f.key] == null) return;
      const row = document.createElement("div");
      row.className = "dev-srcrow";
      const lab = document.createElement("span");
      lab.textContent = f.label;
      const val = document.createElement("b");
      val.textContent = fmt(cal[f.key], f.d) + (f.unit ? " " + f.unit : "");
      row.append(lab, val);
      host.appendChild(row);
    });
  }

  // Reflect the current (saved) calibration + fusion-enabled state in the UI.
  function renderState(data) {
    enabled = !!(data && data.enabled);
    const cal = data && data.calibration;
    const readout = $("dev-calib-readout");
    const reset = $("dev-calib-reset");
    const disabled = $("dev-calib-disabled");

    if (disabled) disabled.classList.toggle("hidden", enabled);

    if (readout) {
      if (cal && typeof cal === "object") {
        readout.className = "hint";
        renderCal(readout, cal);
      } else {
        readout.className = "hint";
        readout.textContent = "Not calibrated — using defaults.";
      }
    }
    if (reset) reset.classList.toggle("hidden", !(cal && typeof cal === "object"));
  }

  // ---- capture lifecycle ------------------------------------------------

  function showCapturing(on) {
    capturing = on;
    const prog = $("dev-calib-progress");
    const start = $("dev-calib-start");
    const stop = $("dev-calib-stop");
    if (prog) prog.classList.toggle("hidden", !on);
    if (start) start.disabled = on;
    if (stop) stop.classList.toggle("hidden", !on);
    if (!on) {
      const fill = $("dev-calib-fill");
      if (fill) fill.style.width = "0%";
    }
  }

  function updateProgress(samples) {
    const s = $("dev-calib-samples");
    if (s && samples != null && Number.isFinite(Number(samples))) {
      s.textContent = String(samples);
    }
    const pct = startedAt
      ? Math.max(0, Math.min(100, ((Date.now() - startedAt) / CAPTURE_MS) * 100))
      : 0;
    const fill = $("dev-calib-fill");
    if (fill) fill.style.width = pct.toFixed(0) + "%";
    const p = $("dev-calib-pct");
    if (p) p.textContent = String(Math.round(pct));
  }

  function clearTimers() {
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
    if (stopTimer) { clearTimeout(stopTimer); stopTimer = null; }
  }

  function poll() {
    fetch("/api/fusion/calibration")
      .then((r) => (r.ok ? r.json() : null))
      .then((j) => {
        if (!j) return;
        if (capturing) updateProgress(j.capture_samples);
        // If the backend says capture ended on its own, stop cleanly.
        if (capturing && j.capturing === false) doStop();
      })
      .catch(() => {});
  }

  function doStart() {
    if (!enabled) {
      setStatus("Fusion is off — turn it on to calibrate.", "err");
      return;
    }
    setStatus("Starting…", "busy");
    const start = $("dev-calib-start");
    if (start) start.disabled = true;
    fetch("/api/fusion/calibrate/start", { method: "POST" })
      .then((r) => (r.ok ? r.json() : null))
      .then((j) => {
        if (!j || j.ok === false) {
          setStatus((j && j.error) || "Couldn't start — is fusion on?", "err");
          if (start) start.disabled = false;
          return;
        }
        startedAt = Date.now();
        showCapturing(true);
        updateProgress(0);
        setStatus("Capturing — keep the boat still with the motor off.", "busy");
        clearTimers();
        pollTimer = setInterval(poll, 1000);
        stopTimer = setTimeout(doStop, CAPTURE_MS);
      })
      .catch(() => {
        setStatus("Couldn't start calibration.", "err");
        if (start) start.disabled = false;
      });
  }

  function doStop() {
    clearTimers();
    if (!capturing) return;    // guard double-stop (timer + manual + poll)
    showCapturing(false);
    setStatus("Analysing…", "busy");
    fetch("/api/fusion/calibrate/stop", { method: "POST" })
      .then((r) => (r.ok ? r.json() : null))
      .then((j) => {
        if (!j || j.ok === false) {
          setStatus((j && j.error) || "Calibration failed.", "err");
          proposal = null;
          return;
        }
        proposal = j.calibration || null;
        showProposal(j.calibration, j.warnings);
        setStatus("Capture complete — review the proposal below.", "ok");
      })
      .catch(() => {
        setStatus("Calibration failed.", "err");
        proposal = null;
      });
  }

  function showProposal(cal, warnings) {
    const wrap = $("dev-calib-proposal");
    const body = $("dev-calib-proposal-body");
    const warn = $("dev-calib-warnings");
    if (warn) {
      const list = Array.isArray(warnings) ? warnings.filter(Boolean) : [];
      warn.classList.toggle("hidden", list.length === 0);
      warn.textContent = list.length ? "⚠ " + list.join("  ⚠ ") : "";
    }
    if (cal && typeof cal === "object") {
      renderCal(body, cal);
      if (wrap) wrap.classList.remove("hidden");
    } else {
      if (body) body.textContent = "No calibration produced.";
      if (wrap) wrap.classList.remove("hidden");
    }
  }

  function hideProposal() {
    const wrap = $("dev-calib-proposal");
    if (wrap) wrap.classList.add("hidden");
    proposal = null;
  }

  function doApply() {
    if (!proposal) { hideProposal(); return; }
    setStatus("Saving…", "busy");
    fetch("/api/fusion/calibrate/save", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ calibration: proposal }),
    })
      .then((r) => (r.ok ? r.json() : null))
      .then((j) => {
        if (!j || j.ok === false) {
          setStatus((j && j.error) || "Save failed.", "err");
          return;
        }
        hideProposal();
        setStatus("Calibration saved.", "ok");
        load();  // refresh the saved-state readout
      })
      .catch(() => setStatus("Save failed.", "err"));
  }

  function doReset() {
    setStatus("Resetting…", "busy");
    fetch("/api/fusion/calibrate/reset", { method: "POST" })
      .then((r) => (r.ok ? r.json() : null))
      .then((j) => {
        if (!j || j.ok === false) {
          setStatus((j && j.error) || "Reset failed.", "err");
          return;
        }
        hideProposal();
        setStatus("Reset to defaults.", "ok");
        load();
      })
      .catch(() => setStatus("Reset failed.", "err"));
  }

  // ---- load -------------------------------------------------------------

  function load() {
    fetch("/api/fusion/calibration")
      .then((r) => {
        if (!r.ok) return null;   // 404 / older backend -> stay hidden
        return r.json();
      })
      .then((data) => {
        if (!data || typeof data !== "object" || !("enabled" in data)) {
          box.classList.add("hidden");
          return;
        }
        box.classList.remove("hidden");
        renderState(data);
        // Resume the UI if a capture is already running (e.g. after a reload).
        if (data.capturing && !capturing) {
          startedAt = startedAt || Date.now();
          showCapturing(true);
          updateProgress(data.capture_samples);
          setStatus("Capturing — keep the boat still with the motor off.", "busy");
          clearTimers();
          pollTimer = setInterval(poll, 1000);
          // No auto-stop timer: we don't know when this capture began.
        }
        loaded = true;
      })
      .catch(() => { box.classList.add("hidden"); });
  }

  // ---- wiring -----------------------------------------------------------

  const startBtn = $("dev-calib-start");
  if (startBtn) startBtn.addEventListener("click", doStart);
  const stopBtn = $("dev-calib-stop");
  if (stopBtn) stopBtn.addEventListener("click", doStop);
  const applyBtn = $("dev-calib-apply");
  if (applyBtn) applyBtn.addEventListener("click", doApply);
  const discardBtn = $("dev-calib-discard");
  if (discardBtn) discardBtn.addEventListener("click", () => {
    hideProposal();
    setStatus("Discarded.", "");
  });
  const resetBtn = $("dev-calib-reset");
  if (resetBtn) resetBtn.addEventListener("click", doReset);

  // Lazy: fetch on the card's first open (mirrors the config section above).
  card.addEventListener("toggle", () => {
    if (card.open && !loaded) load();
  });
})();
