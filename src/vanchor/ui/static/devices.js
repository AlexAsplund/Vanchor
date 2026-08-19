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
    phone: "Phone (this device)",
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
  const CHANNEL_LABELS = {
    sim: "Simulated",
    serial: "Serial (wired)",
    none: "Not connected",
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

  // Split-channel source selects (inside the Advanced: split channels disclosure).
  const SPLIT_SRC_FIELDS = [
    { id: "dev-src-steering", key: "steering_source", kind: "steering" },
    { id: "dev-src-thrust",   key: "thrust_source",   kind: "thrust"   },
  ];

  let loaded = false;
  let options = DEFAULT_OPTS;
  let sourceTransports = {};   // { optKey: { source: "serial"|"i2c"|"none" } }
  let driverMenus = {};   // { source: menu-schema } — shown on selection
  let activeMenus = [];   // menus from the running devices (live values)
  let lastRestartRequired = false;
  // Tracks whether each split channel had a non-null persisted source at the
  // last render() call.  collect() uses this to gate per-channel payload keys:
  // a previously-configured channel must still be sendable even when the user
  // resets it back to Auto (source null).  An untouched-open disclosure with
  // both channels null at render time sends ZERO channel keys.
  let lastSplitRendered = { steering: false, thrust: false };

  function setStatus(msg, kind) {
    const el = $("dev-status");
    if (!el) return;
    el.textContent = msg || "";
    el.className = "hint" + (kind ? " " + kind : "");
  }

  // Dirty-state sticky Save (Task 4): the Save bar stays hidden until the form
  // is edited, then sticks to the bottom of the settings scroll. Cleared on
  // save success and on every (re)load, so Reset hides it too.
  let dirty = false;
  function setDirty(on) {
    dirty = !!on;
    const bar = $("dev-save-bar");
    if (bar) bar.classList.toggle("hidden", !dirty);
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
      : kind === "battery" ? BATTERY_LABELS
      : (kind === "steering" || kind === "thrust") ? CHANNEL_LABELS
      : SENSOR_LABELS;
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

  // Show each device's connection fields to match the SELECTED source's
  // transport: a serial source -> port + baud; an I2C source (e.g. the
  // magnetometer) -> a single I2C target field (no serial port auto-picked);
  // sim / nmea / none / phone -> no connection field at all. Driven by the
  // backend's source_transports map, so pluggable drivers classify themselves
  // and we never show a ttyUSB port for an I2C device.
  const CONN_KINDS = [
    { kind: "gps", sel: "dev-src-gps", opt: "gps" },
    { kind: "compass", sel: "dev-src-compass", opt: "compass" },
    { kind: "motor", sel: "dev-src-motor", opt: "motor" },
    { kind: "steering", sel: "dev-src-steering", opt: "steering" },
    { kind: "thrust", sel: "dev-src-thrust", opt: "thrust" },
  ];
  function transportFor(opt, val) {
    const m = sourceTransports[opt] || {};
    if (val && Object.prototype.hasOwnProperty.call(m, val)) return m[val];
    return val ? "serial" : "none";   // unknown wired source -> serial fields
  }
  function connEls(kind) {
    const input = $("dev-" + kind + "-port");
    const pick = $("dev-" + kind + "-port-pick");
    const baud = $("dev-" + kind + "-baud");
    const customRow = input && input.closest(".dev-srcrow");
    return {
      input, pick,
      pickRow: pick && pick.closest(".dev-srcrow"),
      customRow,
      lineRow: baud && baud.closest(".dev-lineopts"),
      label: customRow && customRow.querySelector("span"),
    };
  }
  function relabelConn(kind, el, i2c) {
    // Cache the HTML defaults once so serial mode restores them (motor keeps its
    // "…or i2c:<bus>:<addr>" hint; only a genuinely-I2C source is relabelled).
    if (el.input && el.input.dataset.origPh === undefined)
      el.input.dataset.origPh = el.input.placeholder || "";
    if (el.label && el.label.dataset.origText === undefined)
      el.label.dataset.origText = el.label.textContent || "";
    const nice = kind.charAt(0).toUpperCase() + kind.slice(1);
    if (i2c) {
      if (el.label) el.label.textContent = nice + " I2C bus";
      if (el.input) {
        el.input.placeholder = "i2c:1  or  i2c:1:0x0d  (bus[:address]; address optional = autodetect)";
        const v = (el.input.value || "").trim();
        if (!/^i2c:/i.test(v)) {
          el.input.dataset.serialValue = v;   // remember the serial port we replace
          el.input.value = "i2c:1";           // so an I2C source never shows a ttyUSB
        }
      }
    } else {
      if (el.label && el.label.dataset.origText !== undefined)
        el.label.textContent = el.label.dataset.origText;
      if (el.input && el.input.dataset.origPh !== undefined)
        el.input.placeholder = el.input.dataset.origPh;
      // Undo the i2c:1 we injected, so switching back to a serial source restores
      // the port the user had. Only touches the value WE overwrote (never motor's
      // legitimate i2c:<bus>:<addr>, which is serial-transport and never enters
      // the i2c branch, so serialValue is never set for it).
      if (el.input && el.input.dataset.serialValue !== undefined) {
        if (/^i2c:/i.test((el.input.value || "").trim()))
          el.input.value = el.input.dataset.serialValue;
        delete el.input.dataset.serialValue;
      }
    }
  }
  function syncConnFields() {
    CONN_KINDS.forEach(({ kind, sel, opt }) => {
      const s = $(sel);
      if (!s) return;
      const tr = transportFor(opt, s.value);
      const el = connEls(kind);
      const serial = tr === "serial", i2c = tr === "i2c";
      if (el.pickRow) el.pickRow.classList.toggle("hidden", !serial);   // serial-port picker
      if (el.lineRow) el.lineRow.classList.toggle("hidden", !serial);   // baud/parity/…
      if (el.customRow) {
        // i2c -> always show it (it's the I2C field); serial -> only when the
        // picker is on "Custom path…"; none -> hide.
        const showCustom = i2c || (serial && el.pick && el.pick.value === PORT_CUSTOM);
        el.customRow.classList.toggle("hidden", !showCustom);
      }
      relabelConn(kind, el, i2c);
    });
  }

  // Progressive disclosure (Task 4): hide rarely-relevant sections until they
  // apply. Hidden via .hidden only — ids/state stay intact and collect() still
  // reads their fields. Re-evaluated on config load and on source changes (NOT
  // on the toggles themselves, so an in-use section never vanishes mid-edit).
  function syncConditional() {
    const srcs = SRC_FIELDS.map((f) => {
      const s = $(f.id);
      return s ? s.value : "";
    });
    // NMEA bridge: some source consumes NMEA, or the bridge is already on.
    const nmeaOn = !!($("dev-nmea-enabled") && $("dev-nmea-enabled").checked);
    const nmeaSec = $("dev-nmea-section");
    if (nmeaSec) nmeaSec.classList.toggle("hidden", !(nmeaOn || srcs.indexOf("nmea") !== -1));
    // Phone-as-sensors: some source consumes the phone, or sharing is on.
    const phoneOn = !!($("phone-share") && $("phone-share").checked);
    const phoneCard = $("dev-card-phone");
    if (phoneCard) phoneCard.classList.toggle("hidden", !(phoneOn || srcs.indexOf("phone") !== -1));
    // One Calibrate per sensor (Task 5): the Compass card's Calibrate… button
    // is shown only for sources that HAVE a calibration flow; the click handler
    // (see wiring) routes it per source.
    const calBtn = $("dev-calibrate-compass");
    if (calBtn) {
      const cSrc = ($("dev-src-compass") || {}).value;
      calBtn.classList.toggle("hidden", cSrc !== "magnetometer" && cSrc !== "hwt901b");
    }
  }

  // Guided-setup front door (Task 5): while nothing is configured (hardware
  // mode off, or every device source is sim/none/Auto), the wizard button is
  // the prominent primary action with a hint; once real hardware is configured
  // it demotes to a quiet secondary button. Re-evaluated on config load and on
  // save success, so it demotes live after the config becomes configured.
  function syncFrontdoor(enabled, sources) {
    const wiz = $("hwwiz-open");
    if (!wiz) return;
    const unconfigured = !enabled ||
      sources.every((s) => s == null || s === "" || s === "sim" || s === "none");
    wiz.classList.toggle("btn-primary", unconfigured);
    wiz.classList.toggle("frontdoor", unconfigured);
    wiz.classList.toggle("btn-ghost", !unconfigured);
    const hint = $("hwwiz-open-hint");
    if (hint) hint.classList.toggle("hidden", !unconfigured);
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

  // Show/hide per-channel serial settings inside the split-channels disclosure.
  // Only shown when the corresponding source select is set to "serial".
  function syncSplitSerial() {
    ["steering", "thrust"].forEach(function (ch) {
      const sel = $("dev-src-" + ch);
      const box = $("dev-split-" + ch + "-serial");
      if (box) box.classList.toggle("hidden", !sel || sel.value !== "serial");
    });
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
    // Number("") is 0, not NaN -- an empty input must mean "not set" (null),
    // never 0. Sending gps_baud:0 made "save" impossible with a sim GPS
    // (server: "gps_baud must be a positive integer"), and empty sim-motor
    // fields silently posted 0 instead of leaving the value alone.
    if (v == null || String(v).trim() === "") return null;
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
    sourceTransports = (cfg.source_transports && typeof cfg.source_transports === "object")
      ? cfg.source_transports : {};
    const hw = cfg.hardware && typeof cfg.hardware === "object" ? cfg.hardware : {};
    const nmea = cfg.nmea_tcp && typeof cfg.nmea_tcp === "object" ? cfg.nmea_tcp : {};
    lastRestartRequired = !!cfg.restart_required;

    setEnabled(!!hw.enabled);

    SRC_FIELDS.forEach((f) => {
      const sel = $(f.id);
      fillSelect(sel, f.kind);
      setSelectValue(sel, hw[f.key]);
      if (sel) sel.dataset.prevSrc = sel.value;  // baseline for baud defaults
    });

    setVal("dev-gps-port", hw.gps_port);
    setVal("dev-compass-port", hw.compass_port);
    setVal("dev-motor-port", hw.motor_port);
    // Split channel ports — set before fillPortPicks so the pick syncs correctly.
    setVal("dev-steering-port", hw.steering_port);
    setVal("dev-thrust-port", hw.thrust_port);
    fillPortPicks();  // reflect the loaded ports in the dropdowns
    // Per-port serial line settings (baud + data bits + parity + stop bits).
    ["gps", "compass", "motor"].forEach((d) => {
      setVal("dev-" + d + "-baud", hw[d + "_baud"]);
      setVal("dev-" + d + "-bytesize", hw[d + "_bytesize"]);
      setVal("dev-" + d + "-parity", hw[d + "_parity"]);
      setVal("dev-" + d + "-stopbits", hw[d + "_stopbits"]);
    });
    // Split channel framing (baud + data bits + parity + stop bits).
    ["steering", "thrust"].forEach(function (ch) {
      setVal("dev-" + ch + "-baud", hw[ch + "_baud"]);
      setVal("dev-" + ch + "-bytesize", hw[ch + "_bytesize"]);
      setVal("dev-" + ch + "-parity", hw[ch + "_parity"]);
      setVal("dev-" + ch + "-stopbits", hw[ch + "_stopbits"]);
    });
    syncBaudPicks();  // reflect the loaded bauds in the dropdowns

    // Populate split-channel source selects.
    SPLIT_SRC_FIELDS.forEach((f) => {
      const sel = $(f.id);
      fillSelect(sel, f.kind);
      setSelectValue(sel, hw[f.key]);
      if (sel) sel.dataset.prevSrc = sel.value;  // baseline for baud defaults
    });

    // Auto-open/close the split channels disclosure to reflect the saved config.
    const splitDet = $("dev-split-details");
    if (splitDet) {
      splitDet.open = (hw.steering_source != null || hw.thrust_source != null);
    }
    // Record which channels have a persisted (non-null) source so collect() can
    // include their keys even if the user resets to Auto after opening.
    lastSplitRendered = {
      steering: hw.steering_source != null,
      thrust:   hw.thrust_source   != null,
    };
    syncSplitSerial();

    // Sim-motor actuation shaping (#36) — simulator-only response tuning.
    const sm = cfg.sim_motor && typeof cfg.sim_motor === "object" ? cfg.sim_motor : {};
    setVal("dev-simmotor-revdelay", sm.reverse_delay_s);
    setVal("dev-simmotor-slew", sm.thrust_slew_per_s);
    setVal("dev-simmotor-lag", sm.thrust_lag_tau_s);

    const nEn = $("dev-nmea-enabled");
    if (nEn) nEn.checked = !!nmea.enabled;
    setVal("dev-nmea-port", nmea.port);

    syncMode();
    syncConnFields();
    syncConditional();
    syncNmea();
    syncFrontdoor(!!hw.enabled,
      [hw.gps_source, hw.compass_source, hw.depth_source, hw.motor_source]);
    setBadge(hw.enabled ? "● hardware" : "sim");
    driverMenus = (cfg.driver_menus && typeof cfg.driver_menus === "object") ? cfg.driver_menus : {};
    activeMenus = Array.isArray(cfg.menus) ? cfg.menus : [];
    refreshMenus();
    setDirty(false);  // freshly-(re)loaded form is clean — hide the Save bar
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
  // Each menu is routed into its device's card slot (#dev-menus-<device>, e.g.
  // #dev-menus-compass) when that slot exists; anything without a matching slot
  // lands in the legacy #dev-menus fallback container at the panel bottom.
  function renderMenus(menus) {
    const fallback = $("dev-menus");
    if (!fallback) return;
    fallback.innerHTML = "";
    document.querySelectorAll("[id^='dev-menus-']").forEach((slot) => {
      slot.innerHTML = "";
    });
    (menus || []).forEach((menu) => {
      const box = document.createElement("div");
      box.className = "dev-menu";
      const h = document.createElement("div");
      h.className = "drawer-section";
      h.textContent = menu.title || (menu.device + " settings");
      box.appendChild(h);
      if (menu.notice) {   // first-run nudge / "not detected" callout (#magnetometer)
        const n = document.createElement("div");
        n.className = "hint dev-menu-notice";
        n.textContent = menu.notice;
        box.appendChild(n);
      }
      (menu.settings || []).forEach((s) => box.appendChild(renderSetting(menu.device, s, box)));
      if ((menu.actions || []).length) {
        const row = document.createElement("div");
        row.className = "btn-row";
        menu.actions.forEach((a) => {
          const btn = document.createElement("button");
          btn.type = "button"; btn.className = "btn-ghost";
          btn.dataset.action = a.name;   // deep-link target (Calibrate routing)
          btn.textContent = a.label || a.name;
          if (a.help) btn.title = a.help;
          if (a.disabled) {   // honest stub: greyed out, no dead-end POST
            btn.disabled = true;
            btn.textContent += " (coming soon)";
          } else {
            btn.addEventListener("click", () => runAction(menu.device, a.name, box));
          }
          row.appendChild(btn);
        });
        box.appendChild(row);
      }
      const out = document.createElement("div");
      out.className = "hint dev-menu-out";
      box.appendChild(out);
      const host = (menu.device && $("dev-menus-" + menu.device)) || fallback;
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
        // A raw dump (e.g. "Dump raw I2C") comes back as multi-line text: show it
        // in a selectable monospace block so it's easy to copy + share.
        let pre = box.querySelector(".dev-menu-dump");
        if (r && r.dump) {
          if (!pre) {
            pre = document.createElement("pre");
            pre.className = "dev-menu-dump";
            box.appendChild(pre);
          }
          pre.textContent = r.dump;
        } else if (pre) {
          pre.remove();
        }
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
    // Split channel keys (steering / thrust) — included per-channel only when
    // the disclosure is open AND the channel qualifies: (a) source select is
    // non-empty (user picked something), (b) port text is non-empty (user typed
    // a port), or (c) the channel had a persisted value at render time so the
    // user can reset it back to Auto (source=null).  An untouched-open
    // disclosure with both channels null at render time sends ZERO channel keys,
    // so a legacy config round-trips byte-identically even left open.
    const splitDet = $("dev-split-details");
    const splitHw = {};
    if (splitDet && splitDet.open) {
      ["steering", "thrust"].forEach(function (ch) {
        const s = $("dev-src-" + ch);
        const sv = s ? s.value : "";
        const portVal = textVal("dev-" + ch + "-port");
        if (sv !== "" || portVal !== null || lastSplitRendered[ch]) {
          splitHw[ch + "_source"] = sv === "" ? null : sv;
          if (portVal != null) splitHw[ch + "_port"] = portVal;
          const b = num($("dev-" + ch + "-baud") && $("dev-" + ch + "-baud").value);
          if (b != null) splitHw[ch + "_baud"] = b;
          const bs = num($("dev-" + ch + "-bytesize") && $("dev-" + ch + "-bytesize").value);
          if (bs != null) splitHw[ch + "_bytesize"] = bs;
          const par = ($("dev-" + ch + "-parity") || {}).value;
          if (par) splitHw[ch + "_parity"] = par;
          const sb = num($("dev-" + ch + "-stopbits") && $("dev-" + ch + "-stopbits").value);
          if (sb != null) splitHw[ch + "_stopbits"] = sb;
        }
      });
    }
    return {
      hardware: {
        enabled: readEnabled(),
        gps_port: textVal("dev-gps-port"),
        compass_port: textVal("dev-compass-port"),
        motor_port: textVal("dev-motor-port"),
        ...serial,
        ...splitHw,
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
    ["dev-gps-port-pick",      "dev-gps-port"],
    ["dev-compass-port-pick",  "dev-compass-port"],
    ["dev-motor-port-pick",    "dev-motor-port"],
    ["dev-steering-port-pick", "dev-steering-port"],
    ["dev-thrust-port-pick",   "dev-thrust-port"],
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
    syncConnFields();   // keep the box + custom-row visibility consistent
  }

  // Baud dropdowns (Task 4) — same pattern as the port picks: the number input
  // (id unchanged) stays the source of truth that collect() reads; the select
  // writes into it; "Custom…" reveals the input for oddball rates. An empty
  // input = "Default" (backend/driver default, not sent).
  const BAUD_PICKS = [
    ["dev-gps-baud-pick",      "dev-gps-baud"],
    ["dev-compass-baud-pick",  "dev-compass-baud"],
    ["dev-motor-baud-pick",    "dev-motor-baud"],
    ["dev-steering-baud-pick", "dev-steering-baud"],
    ["dev-thrust-baud-pick",   "dev-thrust-baud"],
  ];

  function syncBaudPick(pickId, inputId) {
    const sel = $(pickId), inp = $(inputId);
    if (!sel || !inp) return;
    const cur = (inp.value || "").trim();
    const known = Array.prototype.some.call(sel.options,
      (o) => o.value === cur && o.value !== PORT_CUSTOM);
    sel.value = (known || cur === "") ? cur : PORT_CUSTOM;
    inp.classList.toggle("hidden", sel.value !== PORT_CUSTOM);
  }

  function syncBaudPicks() {
    BAUD_PICKS.forEach(([pk, ip]) => syncBaudPick(pk, ip));
  }

  function onBaudPick(pickId, inputId) {
    const sel = $(pickId), inp = $(inputId);
    if (!sel || !inp) return;
    if (sel.value === PORT_CUSTOM) {
      inp.classList.remove("hidden");
      inp.focus();
    } else {
      inp.value = sel.value;  // the dropdown IS the source; mirror into the input
      inp.classList.add("hidden");
    }
  }

  // Driver-default baud per selected source. null = no opinion (no baud field,
  // or the backend default applies).
  function defaultBaud(kind, src) {
    if (src === "ublox") return 38400;      // u-blox M9N UBX
    if (src === "hwt901b") return 9600;     // WitMotion AHRS factory default
    if (src === "serial" || src === "both") {
      // NMEA-0183 sensors talk 4800; our motor/steering/thrust boards 115200.
      return (kind === "gps" || kind === "compass") ? 4800 : 115200;
    }
    return null;
  }

  // On a SOURCE change, fill the baud field with the new driver's default —
  // but only when it's empty or still at the PREVIOUS source's default; a
  // user-typed value is never clobbered. Survives source flapping
  // (ublox→serial→ublox keeps tracking) via data-prev-src on the select,
  // which render() re-baselines on every config load.
  function applyBaudDefault(kind, sel) {
    const inp = $("dev-" + kind + "-baud");
    if (!sel || !inp) return;
    const prev = sel.dataset.prevSrc || "";
    const next = sel.value;
    if (next !== prev) {
      const cur = (inp.value || "").trim();
      const prevDef = defaultBaud(kind, prev);
      const nextDef = defaultBaud(kind, next);
      if (cur === "" || (prevDef != null && Number(cur) === prevDef)) {
        if (nextDef != null) inp.value = String(nextDef);
        // New source has no baud opinion: drop a stale driver default (never a
        // user-typed value — those fail the guard above) so it isn't saved.
        else if (cur !== "" && prevDef != null) inp.value = "";
      }
      // Always re-sync the dropdown, even when no default was written — the
      // input is the source of truth and may have changed by other means.
      syncBaudPick("dev-" + kind + "-baud-pick", "dev-" + kind + "-baud");
    }
    sel.dataset.prevSrc = next;
  }

  // Source-select id -> baud field prefix (devices without a baud are absent).
  const BAUD_KIND_BY_SRC = {
    "dev-src-gps": "gps",
    "dev-src-compass": "compass",
    "dev-src-motor": "motor",
    "dev-src-steering": "steering",
    "dev-src-thrust": "thrust",
  };

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
        // The status may sit inside the hidden Save bar — surface it anyway.
        if (VA.toast) VA.toast("Couldn't load device config.");
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
          return;   // still dirty — the bar (and the error) stay visible
        }
        setStatus("Saved — restart the app to apply.", "ok");
        setDirty(false);  // hides the sticky bar (and #dev-status with it) …
        if (VA.toast) VA.toast("Saved — restart the app to apply.");  // … so toast it
        // Promote/demote the guided-setup front door to match the saved config.
        const hw = body.hardware || {};
        syncFrontdoor(!!hw.enabled,
          [hw.gps_source, hw.compass_source, hw.depth_source, hw.motor_source]);
      })
      .catch(() => {
        if (btn) btn.disabled = false;
        setStatus("Save failed.", "err");
      });
  }

  // ---- wiring -----------------------------------------------------------

  // Any edit inside the card marks the form dirty (delegated; programmatic
  // .value writes during render() fire no events, so loads stay clean).
  // The u-blox receiver toolbox applies immediately via its own button —
  // its controls are not part of collect(), so they don't dirty the form.
  ["input", "change"].forEach((ev) => {
    card.addEventListener(ev, (e) => {
      if (e.target && e.target.closest && e.target.closest("#ublox-tools-card")) return;
      setDirty(true);
    });
  });

  // Mode segmented control (buttons fire no input/change → mark dirty here).
  const seg = $("dev-mode");
  if (seg) {
    seg.addEventListener("click", (e) => {
      const b = e.target.closest("button[data-on]");
      if (!b) return;
      setEnabled(b.dataset.on === "true");
      syncMode();
      setDirty(true);
    });
  }

  // Source selects → toggle serial disclosure + show the picked driver's menu
  // (+ fill the new driver's default baud if the field isn't user-set).
  SRC_FIELDS.forEach((f) => {
    const sel = $(f.id);
    if (sel) sel.addEventListener("change", () => {
      const bk = BAUD_KIND_BY_SRC[f.id];
      if (bk) applyBaudDefault(bk, sel);
      syncConnFields();
      syncConditional();
      refreshMenus();
    });
  });

  // Warn when the u-blox GPS driver is chosen: it reconfigures the receiver
  // (NMEA off, 10 Hz, sea model). Blocking confirm; Cancel reverts the pick.
  (function () {
    const gsel = $("dev-src-gps"), dlg = $("ublox-warn-dlg");
    if (!gsel || !dlg) return;
    let prev = gsel.value;
    gsel.addEventListener("focus", () => { prev = gsel.value; });
    gsel.addEventListener("change", async () => {
      if (gsel.value !== "ublox" || prev === "ublox") { prev = gsel.value; return; }
      try {
        const d = await (await fetch("/api/tools/ublox/marine-config")).json();
        const ul = $("ublox-warn-list");
        if (ul) {
          ul.innerHTML = "";
          (d.summary || []).forEach((s) => {
            const li = document.createElement("li");
            li.textContent = s;
            ul.appendChild(li);
          });
        }
      } catch (e) { /* fall back to the dialog's static copy */ }
      dlg.returnValue = "";
      const onClose = () => {
        dlg.removeEventListener("close", onClose);
        if (dlg.returnValue !== "ok") {   // cancelled / Esc -> revert the pick
          gsel.value = prev;
          applyBaudDefault("gps", gsel);  // undo the ublox default baud too
          syncConnFields();
          syncConditional();
          refreshMenus();
        }
        prev = gsel.value;
      };
      dlg.addEventListener("close", onClose);
      if (typeof dlg.showModal === "function") dlg.showModal();
    });
    const ok = $("ublox-warn-ok"), cancel = $("ublox-warn-cancel");
    if (ok) ok.addEventListener("click", () => { dlg.returnValue = "ok"; dlg.close(); });
    if (cancel) cancel.addEventListener("click", () => { dlg.returnValue = "cancel"; dlg.close(); });
  })();

  // Split-channel source selects → toggle per-channel serial rows
  // (+ per-channel driver-default baud).
  SPLIT_SRC_FIELDS.forEach((f) => {
    const sel = $(f.id);
    if (sel) sel.addEventListener("change", () => {
      const bk = BAUD_KIND_BY_SRC[f.id];
      if (bk) applyBaudDefault(bk, sel);
      syncSplitSerial();
    });
  });

  // Serial-port dropdowns: mirror the pick into the (source-of-truth) input.
  PORT_PICKS.forEach(([pk, ip]) => {
    const sel = $(pk);
    if (sel) sel.addEventListener("change", () => onPortPick(pk, ip));
  });

  // Baud dropdowns: same mirroring into the (source-of-truth) number input.
  BAUD_PICKS.forEach(([pk, ip]) => {
    const sel = $(pk);
    if (sel) sel.addEventListener("change", () => onBaudPick(pk, ip));
  });

  // Compass "Calibrate…" routing (Task 5) — one entry point per sensor:
  //   magnetometer -> trigger the driver menu's spin-to-calibrate action (its
  //     rendered button, so the result lands in that menu's own output line);
  //   hwt901b -> deep-link to the sensor-fusion calibration card (the AHRS'
  //     own mag calibration is still a stub — fusion cal is what applies).
  const compassCalBtn = $("dev-calibrate-compass");
  if (compassCalBtn) compassCalBtn.addEventListener("click", () => {
    const src = ($("dev-src-compass") || {}).value;
    if (src === "magnetometer") {
      const slot = $("dev-menus-compass");
      const act = slot && slot.querySelector('button[data-action="calibrate_start"]');
      if (act) {
        act.scrollIntoView({ behavior: "smooth", block: "nearest" });
        act.click();
      } else if (slot) {   // menu not rendered (older backend) — just show it
        slot.scrollIntoView({ behavior: "smooth", block: "nearest" });
      }
    } else if (src === "hwt901b") {
      const fusion = $("dev-card-fusion");
      if (fusion) {
        fusion.open = true;
        fusion.scrollIntoView({ behavior: "smooth", block: "start" });
      }
    }
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
    // The status may sit inside the hidden Save bar — surface it anyway.
    if (VA.toast) VA.toast("Restarting the server…");
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
 * Records the boat's sensor noise/behaviour, then tunes the GNSS/INS fusion
 * filter to it. THREE capture modes, each individually runnable, each with its
 * own manoeuvre + duration:
 *   still        (~30 s) — boat still, motor off  -> gyro bias + noise -> gains
 *   align        (~15 s) — drive straight         -> compass/IMU mounting offset
 *   interference (~15 s) — bow tied, ramp motor    -> motor-magnetism quality score
 * "Calibrate all…" runs the three step-by-step for initial setup.
 *
 * Lazy-loads GET /api/fusion/calibration on the card's first open; if that 404s
 * (backend without this flow) the whole section stays hidden and no errors
 * surface — same graceful-degrade pattern as above.
 *
 * Contract (must match the backend):
 *   GET  /api/fusion/calibration ->
 *     { calibration:{…}|null, capturing:bool, capture_samples:int,
 *       capture_seconds:float, enabled:bool, recommendations:[str] }
 *       (recommendations = mitigation advice for the SAVED interference score.)
 *   POST /api/fusion/calibrate/start  body { mode } (still|align|interference)
 *        -> { ok:true, capturing:true }  (ok:false if fusion off)
 *   POST /api/fusion/calibrate/stop   -> { ok:true, mode, calibration:{…}, warnings:[str],
 *                                          recommendations:[str] (interference mode) }
 *                                        or { ok:false, error:str }
 *   POST /api/fusion/calibrate/save   body { calibration:{…} } -> { ok:true }
 *   POST /api/fusion/calibrate/reset  -> { ok:true }
 */
(function () {
  const $ = (id) => document.getElementById(id);
  const card = $("devices-card");
  const box = $("dev-calib");
  if (!card || !box || !window.VA) return;

  // Per-mode manoeuvre + capture duration (client-side auto-stop `ms`).
  const MODES = {
    still: { label: "Still", ms: 30000,
      instr: "Keep the boat STILL with the motor OFF." },
    align: { label: "Align heading", ms: 15000,
      instr: "Drive STRAIGHT at a steady cruise speed." },
    interference: { label: "Measure noise", ms: 20000,
      instr: "Tie the bow off so the boat can't rotate. Slowly ramp the motor from 0 toward full AND sweep the steering left↔right through its range." },
  };
  const SEQUENCE = ["still", "align", "interference"];

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
    { key: "heading_offset_deg", label: "Heading offset", unit: "°", d: 1 },
    { key: "motor_interference_deg", label: "Interference drift", unit: "°", d: 1 },
    { key: "motor_interference_slope", label: "Interference slope", unit: "°/thrust", d: 3 },
    { key: "motor_interference_score", label: "Interference quality", unit: "/100", d: 0 },
    { key: "samples", label: "Samples", unit: "", d: 0 },
    { key: "duration_s", label: "Duration", unit: "s", d: 1 },
  ];
  const FIELD_BY_KEY = {};
  FIELDS.forEach((f) => { FIELD_BY_KEY[f.key] = f; });

  // Which fields each mode's proposal shows (the score is a separate headline,
  // so interference's body omits it to avoid duplication).
  const MODE_FIELDS = {
    still: ["gyro_bias_dps", "heading_gain", "vel_tau_s", "dr_timeout_s",
      "crab_min_sog_mps", "crab_min_sog_measured_mps", "gps_pos_sigma_m",
      "gps_vel_sigma_mps", "heading_sigma_deg", "yaw_rate_sigma_dps",
      "samples", "duration_s"],
    align: ["heading_offset_deg", "samples", "duration_s"],
    interference: ["motor_interference_slope", "samples", "duration_s"],
  };

  let loaded = false;
  let enabled = false;      // fusion on?
  let capturing = false;    // a capture is in flight
  let activeMode = "still"; // mode of the in-flight / last capture
  let proposalMode = "still";
  let proposal = null;      // last stop() result awaiting Apply/Discard/Confirm
  let pollTimer = null;
  let stopTimer = null;
  let startedAt = 0;
  const seq = { active: false, idx: 0, saved: {} };  // "Calibrate all" state

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

  // Render the labelled numbers of a calibration object as text rows. `keys`
  // limits/orders which fields to show (defaults to all present FIELDS).
  function renderCal(host, cal, keys) {
    if (!host || !cal) return;
    host.innerHTML = "";
    const list = keys ? keys.map((k) => FIELD_BY_KEY[k]).filter(Boolean) : FIELDS;
    list.forEach((f) => {
      if (cal[f.key] == null) return;
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

  // Motor-interference quality colour band: red <40, amber 40-75, green >75.
  function scoreColor(s) {
    if (!Number.isFinite(s)) return "var(--muted)";
    if (s < 40) return "var(--stop)";
    if (s <= 75) return "#e0a13a";
    return "var(--accent-2)";
  }

  // Prominent 0-100 quality headline (interference mode). Pass null to hide.
  function showScore(cal) {
    const scoreBox = $("dev-calib-score");
    if (!scoreBox) return;
    if (!cal || cal.motor_interference_score == null) {
      scoreBox.classList.add("hidden");
      return;
    }
    const s = Number(cal.motor_interference_score);
    const num = $("dev-calib-score-num");
    if (num) {
      num.textContent = Number.isFinite(s) ? String(Math.round(s)) : "—";
      num.style.color = scoreColor(s);
    }
    const sub = $("dev-calib-score-sub");
    if (sub) {
      sub.textContent = cal.motor_interference_deg != null
        ? fmt(cal.motor_interference_deg, 1) + "° heading drift at full thrust" : "";
    }
    scoreBox.classList.remove("hidden");
  }

  // Servo-term status: motor_interference_sin/cos are both non-null once the
  // steering sweep was measured. Hidden unless an interference sweep ran at all.
  function renderServo(host, cal) {
    if (!host) return;
    const ran = cal && (cal.motor_interference_score != null
      || cal.motor_interference_deg != null
      || cal.motor_interference_sin != null
      || cal.motor_interference_cos != null);
    if (!ran) { host.classList.add("hidden"); host.textContent = ""; return; }
    const measured = cal.motor_interference_sin != null && cal.motor_interference_cos != null;
    host.className = "hint" + (measured ? " ok" : "");
    host.textContent = measured
      ? "Servo compensation: measured"
      : "Servo compensation: not measured — sweep the steering too";
    host.classList.remove("hidden");
  }

  // Render motor-interference mitigation advice (verbatim, from the backend) as a
  // "What to do about it" list. Given visual weight (tinted callout) when the
  // score is poor; kept low-key/collapsed when it's good. Hidden if no advice.
  function renderRecs(host, recs, score) {
    if (!host) return;
    host.innerHTML = "";
    host.className = "";
    const list = Array.isArray(recs)
      ? recs.filter((s) => typeof s === "string" && s.trim()) : [];
    if (!list.length) { host.classList.add("hidden"); return; }
    const s = Number(score);
    const good = Number.isFinite(s) && s > 75;
    const color = scoreColor(s);

    const ul = document.createElement("ul");
    ul.style.margin = "6px 0 0";
    ul.style.paddingLeft = "18px";
    list.forEach((t) => {
      const li = document.createElement("li");
      li.textContent = t;
      li.style.marginBottom = "3px";
      ul.appendChild(li);
    });

    if (good) {
      // Low-key: a collapsed disclosure — the actions barely matter here.
      const det = document.createElement("details");
      det.className = "hint";
      const sum = document.createElement("summary");
      sum.textContent = "What to do about it";
      det.append(sum, ul);
      host.className = "hint";
      host.appendChild(det);
    } else {
      // Prominent: a colour-tinted callout — this is when the actions matter.
      const callout = document.createElement("div");
      callout.style.borderLeft = "3px solid " + color;
      callout.style.background = "color-mix(in srgb, " + color + " 12%, transparent)";
      callout.style.borderRadius = "var(--r-sm)";
      callout.style.padding = "8px 10px";
      callout.style.marginTop = "8px";
      const h = document.createElement("div");
      h.textContent = "What to do about it";
      h.style.fontWeight = "700";
      h.style.color = color;
      callout.append(h, ul);
      host.appendChild(callout);
    }
    host.classList.remove("hidden");
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
      readout.className = "hint";
      if (cal && typeof cal === "object") renderCal(readout, cal);
      else readout.textContent = "Not calibrated — using defaults.";
    }
    if (reset) reset.classList.toggle("hidden", !(cal && typeof cal === "object"));
    renderServo($("dev-calib-saved-servo"), cal);
    // Mitigation advice for the saved interference score (empty -> hidden).
    renderRecs($("dev-calib-saved-recs"), data && data.recommendations,
      cal && cal.motor_interference_score);
    renderComp(data);
  }

  // Experimental interference-compensation switch. Reflects GET state; disabled
  // (greyed, with a hint) until an interference sweep has produced a drift model.
  // Hidden entirely on backends that don't report the field.
  function renderComp(data) {
    const wrap = $("dev-calib-comp");
    if (!wrap) return;
    const has = !!(data && "interference_comp_enabled" in data);
    wrap.classList.toggle("hidden", !has);
    if (!has) return;
    const hasModel = !!(data && data.has_interference_model);
    const tog = $("dev-calib-comp-toggle");
    if (tog) {
      tog.checked = !!data.interference_comp_enabled;
      tog.disabled = !hasModel;
      const sw = tog.closest(".switch");
      if (sw) sw.classList.toggle("dev-dim", !hasModel);
    }
    const need = $("dev-calib-comp-need");
    if (need) need.classList.toggle("hidden", hasModel);
  }

  function onCompToggle() {
    const tog = $("dev-calib-comp-toggle");
    if (!tog) return;
    const want = tog.checked;
    tog.disabled = true;
    setStatus(want ? "Enabling compensation…" : "Disabling compensation…", "busy");
    fetch("/api/fusion/interference-comp", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled: want }),
    })
      .then((r) => (r.ok ? r.json() : null))
      .then((j) => {
        if (!j || j.ok === false) {
          tog.checked = !want;     // revert the control on rejection
          tog.disabled = false;
          setStatus((j && j.error) || "Couldn't change compensation.", "err");
          return;
        }
        setStatus(j.enabled ? "Interference compensation on." : "Interference compensation off.", "ok");
        load();  // re-sync full state (enabled + model availability)
      })
      .catch(() => {
        tog.checked = !want;       // revert on network failure
        tog.disabled = false;
        setStatus("Couldn't change compensation.", "err");
      });
  }

  // ---- capture lifecycle ------------------------------------------------

  function captureMs() {
    return (MODES[activeMode] && MODES[activeMode].ms) || 30000;
  }

  function showCapturing(on) {
    capturing = on;
    const prog = $("dev-calib-progress");
    const stop = $("dev-calib-stop");
    if (prog) prog.classList.toggle("hidden", !on);
    if (stop) stop.classList.toggle("hidden", !on);
    // Lock the mode buttons + the sequence's Ready/Skip while capturing.
    const modes = $("dev-calib-modes");
    if (modes) modes.querySelectorAll("button").forEach((b) => { b.disabled = on; });
    ["dev-calib-seq-start", "dev-calib-seq-skip"].forEach((id) => {
      const b = $(id); if (b) b.disabled = on;
    });
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
      ? Math.max(0, Math.min(100, ((Date.now() - startedAt) / captureMs()) * 100))
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

  function doStart(mode) {
    mode = MODES[mode] ? mode : "still";
    if (!enabled) {
      setStatus("Fusion is off — turn it on to calibrate.", "err");
      return;
    }
    if (capturing) return;   // guard double-start
    activeMode = mode;
    hideProposal();
    const instr = $("dev-calib-instr");
    if (instr) instr.textContent = MODES[mode].instr;
    setStatus("Starting…", "busy");
    fetch("/api/fusion/calibrate/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mode }),
    })
      .then((r) => (r.ok ? r.json() : null))
      .then((j) => {
        if (!j || j.ok === false) {
          setStatus((j && j.error) || "Couldn't start — is fusion on?", "err");
          return;
        }
        startedAt = Date.now();
        showCapturing(true);
        updateProgress(0);
        setStatus("Capturing — " + MODES[mode].instr, "busy");
        clearTimers();
        pollTimer = setInterval(poll, 1000);
        stopTimer = setTimeout(doStop, captureMs());
      })
      .catch(() => setStatus("Couldn't start calibration.", "err"));
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
        showProposal(j.calibration, j.warnings, j.mode || activeMode, j.recommendations);
        if (seq.active) {
          setStatus("Review the result, then confirm to save & continue.", "ok");
          setSeqButtons("review");
        } else {
          setStatus("Capture complete — review the proposal below.", "ok");
        }
      })
      .catch(() => {
        setStatus("Calibration failed.", "err");
        proposal = null;
      });
  }

  function showProposal(cal, warnings, mode, recs) {
    proposalMode = MODES[mode] ? mode : activeMode;
    const wrap = $("dev-calib-proposal");
    const body = $("dev-calib-proposal-body");
    const warn = $("dev-calib-warnings");
    const acts = $("dev-calib-proposal-actions");
    if (warn) {
      const list = Array.isArray(warnings) ? warnings.filter(Boolean) : [];
      warn.classList.toggle("hidden", list.length === 0);
      warn.textContent = list.length ? "⚠ " + list.join("  ⚠ ") : "";
    }
    // In a guided sequence the seq panel owns the confirm/skip buttons.
    if (acts) acts.classList.toggle("hidden", seq.active);
    const isInterference = proposalMode === "interference";
    showScore(isInterference ? cal : null);
    // Servo-term status + mitigation advice sit under the score gauge
    // (interference mode only).
    const servoHost = $("dev-calib-servo");
    const recsHost = $("dev-calib-recs");
    if (isInterference) {
      renderServo(servoHost, cal);
      renderRecs(recsHost, recs, cal && cal.motor_interference_score);
    } else {
      if (servoHost) servoHost.classList.add("hidden");
      if (recsHost) recsHost.classList.add("hidden");
    }
    if (cal && typeof cal === "object") {
      renderCal(body, cal, MODE_FIELDS[proposalMode]);
    } else if (body) {
      body.innerHTML = "";
      body.textContent = "No calibration produced.";
    }
    if (wrap) wrap.classList.remove("hidden");
  }

  function hideProposal() {
    const wrap = $("dev-calib-proposal");
    if (wrap) wrap.classList.add("hidden");
    const scoreBox = $("dev-calib-score");
    if (scoreBox) scoreBox.classList.add("hidden");
    const servoHost = $("dev-calib-servo");
    if (servoHost) servoHost.classList.add("hidden");
    const recsHost = $("dev-calib-recs");
    if (recsHost) recsHost.classList.add("hidden");
    proposal = null;
  }

  // POST save; resolves true on success, false otherwise (never rejects).
  function postSave(cal) {
    return fetch("/api/fusion/calibrate/save", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ calibration: cal }),
    })
      .then((r) => (r.ok ? r.json() : null))
      .then((j) => !!(j && j.ok !== false))
      .catch(() => false);
  }

  function doApply() {
    if (!proposal) { hideProposal(); return; }
    setStatus("Saving…", "busy");
    postSave(proposal).then((ok) => {
      if (!ok) { setStatus("Save failed.", "err"); return; }
      hideProposal();
      setStatus("Calibration saved.", "ok");
      load();  // refresh the saved-state readout
    });
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

  // ---- guided "Calibrate all" sequence ----------------------------------

  function setSeqButtons(state) {
    const vis = {
      start: state === "ready",
      confirm: state === "review",
      skip: state === "ready" || state === "review",
      cancel: state !== "done",
      done: state === "done",
    };
    Object.keys(vis).forEach((k) => {
      const b = $("dev-calib-seq-" + k);
      if (b) b.classList.toggle("hidden", !vis[k]);
    });
  }

  function startSequence() {
    if (!enabled) {
      setStatus("Fusion is off — turn it on to calibrate.", "err");
      return;
    }
    if (capturing) return;
    seq.active = true; seq.idx = 0; seq.saved = {};
    const modes = $("dev-calib-modes");
    if (modes) modes.classList.add("hidden");
    const panel = $("dev-calib-seq");
    if (panel) panel.classList.remove("hidden");
    setStatus("");
    renderSeqStep();
  }

  function renderSeqStep() {
    if (seq.idx >= SEQUENCE.length) { finishSequence(); return; }
    const mode = SEQUENCE[seq.idx];
    const badge = $("dev-calib-seq-step");
    if (badge) badge.textContent = "Step " + (seq.idx + 1) + "/" + SEQUENCE.length + " · " + MODES[mode].label;
    const instr = $("dev-calib-seq-instr");
    if (instr) instr.textContent = MODES[mode].instr + "  (" + Math.round(MODES[mode].ms / 1000) + " s)";
    hideProposal();
    setSeqButtons("ready");
  }

  function seqStart() {
    if (seq.idx < SEQUENCE.length) doStart(SEQUENCE[seq.idx]);
  }

  function seqConfirm() {
    if (!proposal) { seqAdvance(); return; }
    const mode = proposalMode;
    setStatus("Saving…", "busy");
    postSave(proposal).then((ok) => {
      if (!ok) { setStatus("Save failed — retry or skip this step.", "err"); return; }
      seq.saved[mode] = proposal;
      seqAdvance();
    });
  }

  function seqSkip() {
    if (capturing) return;
    seqAdvance();
  }

  function seqAdvance() {
    hideProposal();
    seq.idx += 1;
    renderSeqStep();
  }

  function seqCancel() {
    endSequence();
    setStatus("Guided calibration cancelled.", "");
    load();
  }

  function endSequence() {
    if (capturing) {  // abort any in-flight capture on the backend too
      clearTimers();
      showCapturing(false);
      fetch("/api/fusion/calibrate/stop", { method: "POST" }).catch(() => {});
    }
    seq.active = false;
    const panel = $("dev-calib-seq");
    if (panel) panel.classList.add("hidden");
    const modes = $("dev-calib-modes");
    if (modes) modes.classList.remove("hidden");
    hideProposal();
  }

  function finishSequence() {
    const st = seq.saved.still, al = seq.saved.align, itf = seq.saved.interference;
    const parts = [];
    parts.push(st ? "✓ Gyro bias & gains set." : "• Still: skipped.");
    parts.push(al && al.heading_offset_deg != null
      ? "✓ Heading offset " + fmt(al.heading_offset_deg, 1) + "°."
      : "• Align: skipped.");
    parts.push(itf && itf.motor_interference_score != null
      ? "✓ Interference quality " + Math.round(itf.motor_interference_score) + "/100."
      : "• Noise: skipped.");
    const badge = $("dev-calib-seq-step");
    if (badge) badge.textContent = "Complete";
    const instr = $("dev-calib-seq-instr");
    if (instr) instr.textContent = parts.join("  ");
    hideProposal();
    setSeqButtons("done");
    setStatus("Guided calibration complete.", "ok");
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
          const instr = $("dev-calib-instr");
          if (instr) instr.textContent = "Capture in progress — follow the on-boat instruction.";
          setStatus("Capturing…", "busy");
          clearTimers();
          pollTimer = setInterval(poll, 1000);
          // No auto-stop timer: we don't know when this capture began.
        }
        loaded = true;
      })
      .catch(() => { box.classList.add("hidden"); });
  }

  // ---- wiring -----------------------------------------------------------

  // Per-mode buttons (delegated on the row via data-mode) + "Calibrate all".
  const modesRow = $("dev-calib-modes");
  if (modesRow) modesRow.addEventListener("click", (e) => {
    const b = e.target.closest("button[data-mode]");
    if (b) doStart(b.dataset.mode);
  });
  const allBtn = $("dev-calib-all");
  if (allBtn) allBtn.addEventListener("click", startSequence);

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

  const compTog = $("dev-calib-comp-toggle");
  if (compTog) compTog.addEventListener("change", onCompToggle);

  // Guided-sequence controls.
  const seqStartBtn = $("dev-calib-seq-start");
  if (seqStartBtn) seqStartBtn.addEventListener("click", seqStart);
  const seqConfirmBtn = $("dev-calib-seq-confirm");
  if (seqConfirmBtn) seqConfirmBtn.addEventListener("click", seqConfirm);
  const seqSkipBtn = $("dev-calib-seq-skip");
  if (seqSkipBtn) seqSkipBtn.addEventListener("click", seqSkip);
  const seqCancelBtn = $("dev-calib-seq-cancel");
  if (seqCancelBtn) seqCancelBtn.addEventListener("click", seqCancel);
  const seqDoneBtn = $("dev-calib-seq-done");
  if (seqDoneBtn) seqDoneBtn.addEventListener("click", () => { endSequence(); load(); });

  // Lazy: fetch on the card's first open (mirrors the config section above).
  card.addEventListener("toggle", () => {
    if (card.open && !loaded) load();
  });
})();

/* Device debug viewer — a per-device "🐞 Debug" button next to each source select
 * opens ONE shared panel that live-streams that device's raw data by polling
 * GET /api/devices/{kind}/debug (kind ∈ gps|compass|depth|motor|battery) ~2×/s.
 *
 * Switching devices stops the old poll and starts the new; Close and collapsing
 * the card both stop it — the interval is cleared on every exit path so it can't
 * leak. Degrades gracefully: {ok:false} / fetch errors show the message text, and
 * a few consecutive failures stop the poll with a "stream ended" note.
 *
 * Contract:
 *   GET /api/devices/{kind}/debug ->
 *     { ok:true,  kind, source, debug:"<multi-line text>" }  or
 *     { ok:false, kind, debug:"No … device is active…" }
 */
(function () {
  const $ = (id) => document.getElementById(id);
  const card = $("devices-card");
  const viewer = $("dev-debug-viewer");
  const body = $("dev-body");
  if (!card || !viewer) return;

  const KINDS = { gps: 1, compass: 1, depth: 1, motor: 1, battery: 1, steering: 1, thrust: 1 };
  const POLL_MS = 500;
  const MAX_FAILS = 3;   // stop after this many consecutive failures

  let curKind = null;
  let timer = null;
  let fails = 0;

  function stopPoll() {
    if (timer) { clearInterval(timer); timer = null; }
  }

  function setOut(txt) {
    const out = $("dev-debug-out");
    if (out) out.textContent = txt == null ? "" : String(txt);
  }

  function setMeta(txt) {
    const m = $("dev-debug-meta");
    if (m) m.textContent = txt || "";
  }

  function nowStr() {
    return new Date().toTimeString().slice(0, 8);
  }

  // Brief "live" pulse on the status dot each successful update.
  function pulse() {
    const dot = $("dev-debug-dot");
    if (!dot) return;
    dot.style.opacity = "1";
    setTimeout(() => { if (dot) dot.style.opacity = "0.25"; }, 160);
  }

  function render(kind, j) {
    const title = $("dev-debug-title");
    if (title) {
      const src = j && j.source ? j.source : "";
      title.textContent = "Raw data — " + kind + (src ? " (" + src + ")" : "");
    }
    // Show whatever text the backend gave (raw snapshot, or the ok:false message).
    setOut(j && j.debug != null ? j.debug : "(no data)");
  }

  function onFail() {
    fails += 1;
    if (fails >= MAX_FAILS) {
      stopPoll();
      setMeta("stream ended (" + nowStr() + ")");
    } else {
      setMeta("connection problem…");
    }
  }

  function poll() {
    const kind = curKind;
    if (!kind) return;
    fetch("/api/devices/" + kind + "/debug")
      .then((r) => (r.ok ? r.json() : null))
      .then((j) => {
        if (kind !== curKind) return;   // switched/closed mid-request
        if (!j) { onFail(); return; }
        fails = 0;
        render(kind, j);
        setMeta("updated " + nowStr());
        pulse();
      })
      .catch(() => { if (kind === curKind) onFail(); });
  }

  function openDebug(kind, host) {
    if (!KINDS[kind]) return;
    stopPoll();               // never leak the previous poll on switch
    curKind = kind;
    fails = 0;
    // Move the ONE shared viewer into the invoking device's card so the raw
    // data shows up next to the device it belongs to (Task 3). No host (e.g.
    // an unexpected caller) -> leave it wherever it currently is.
    if (host && viewer.parentElement !== host) host.appendChild(viewer);
    viewer.classList.remove("hidden");
    const title = $("dev-debug-title");
    if (title) title.textContent = "Raw data — " + kind;
    setOut("…");
    setMeta("");
    poll();                   // immediate first snapshot
    timer = setInterval(poll, POLL_MS);
  }

  function closeDebug() {
    stopPoll();
    curKind = null;
    viewer.classList.add("hidden");
  }

  // Debug buttons (delegated on the whole card body — the buttons now live
  // inside each device's sub-card, incl. steering/thrust inside the motor
  // card's split disclosure). preventDefault stops the enclosing <label> from
  // re-focusing its select. The viewer is moved into the sub-card containing
  // the clicked button (steering/thrust resolve to the motor card).
  if (body) body.addEventListener("click", (e) => {
    const b = e.target.closest("button[data-debug]");
    if (!b) return;
    e.preventDefault();
    e.stopPropagation();
    openDebug(b.dataset.debug, b.closest("details.dev-card"));
  });

  const closeBtn = $("dev-debug-close");
  if (closeBtn) closeBtn.addEventListener("click", closeDebug);

  // Never leak a poll: stop when the card is collapsed.
  card.addEventListener("toggle", () => { if (!card.open) closeDebug(); });

  // Also stop the debug poll when the split-channels disclosure is collapsed.
  const splitDetails = $("dev-split-details");
  if (splitDetails) {
    splitDetails.addEventListener("toggle", () => { if (!splitDetails.open) closeDebug(); });
  }

  // And when the sub-card currently hosting the viewer is collapsed.
  document.querySelectorAll("#dev-body details.dev-card").forEach((d) => {
    d.addEventListener("toggle", () => {
      if (!d.open && curKind != null && d.contains(viewer)) closeDebug();
    });
  });
})();

/* Connectors consent UI — pluggable integrations (Devices panel).
 *
 * Lazy-loads GET /api/connectors on the card's first <details> toggle;
 * hides the "Connectors" card silently on 404 / older backend.
 *
 * Consent flow: enabling a connector shows an inline confirm block —
 * "This allows <label> to:" with grant_lines bullets + Enable / Cancel.
 * ONLY the Enable button POSTs {enabled:true}. Disabling POSTs immediately
 * with no confirm. needs_reconsent shows a badge; clicking it re-runs the
 * consent flow. The toggle running the consent flow can never skip it.
 *
 * Debug viewer: parallel to the device viewer above — different DOM ids,
 * same 500 ms poll / one-at-a-time / no-leak pattern.
 *
 * Contract:
 *   GET  /api/connectors ->
 *     { connectors: [{name, label, description, grant_lines, control,
 *                     armed, needs_reconsent, running, status}] }
 *   POST /api/connectors/{name}/arm  body { enabled: bool }
 *     -> { ok, running }  or  { ok:false, error }
 *   GET  /api/connectors/{name}/debug  -> { ok, name, debug }
 */
(function () {
  const $ = (id) => document.getElementById(id);
  const card = $("conn-card");
  if (!card) return;

  const POLL_MS = 500;
  const MAX_FAILS = 3;

  let loaded = false;

  // ---- connector debug viewer (one connector at a time) ----------------- //

  let curDebugName = null;
  let debugTimer = null;
  let debugFails = 0;

  function stopDebugPoll() {
    if (debugTimer) { clearInterval(debugTimer); debugTimer = null; }
  }

  function setDebugOut(txt) {
    const el = $("conn-debug-out");
    if (el) el.textContent = txt == null ? "" : String(txt);
  }

  function setDebugMeta(txt) {
    const el = $("conn-debug-meta");
    if (el) el.textContent = txt || "";
  }

  function nowStr() {
    return new Date().toTimeString().slice(0, 8);
  }

  function debugPulse() {
    const dot = $("conn-debug-dot");
    if (!dot) return;
    dot.style.opacity = "1";
    setTimeout(() => { if (dot) dot.style.opacity = "0.25"; }, 160);
  }

  function onDebugFail() {
    debugFails += 1;
    if (debugFails >= MAX_FAILS) {
      stopDebugPoll();
      setDebugMeta("stream ended (" + nowStr() + ")");
    } else {
      setDebugMeta("connection problem…");
    }
  }

  function pollDebug() {
    const name = curDebugName;
    if (!name) return;
    fetch("/api/connectors/" + encodeURIComponent(name) + "/debug")
      .then((r) => (r.ok ? r.json() : null))
      .then((j) => {
        if (name !== curDebugName) return;   // switched / closed mid-flight
        if (!j) { onDebugFail(); return; }
        debugFails = 0;
        const title = $("conn-debug-title");
        if (title) title.textContent = "Debug — " + (j.name || name);
        setDebugOut(j.debug != null ? j.debug : "(no data)");
        setDebugMeta("updated " + nowStr());
        debugPulse();
      })
      .catch(() => { if (name === curDebugName) onDebugFail(); });
  }

  function openDebug(name) {
    stopDebugPoll();               // never leak the previous poll on switch
    curDebugName = name;
    debugFails = 0;
    const viewer = $("conn-debug-viewer");
    if (viewer) viewer.classList.remove("hidden");
    const title = $("conn-debug-title");
    if (title) title.textContent = "Debug — " + name;
    setDebugOut("…");
    setDebugMeta("");
    pollDebug();                   // immediate first snapshot
    debugTimer = setInterval(pollDebug, POLL_MS);
  }

  function closeDebug() {
    stopDebugPoll();
    curDebugName = null;
    const viewer = $("conn-debug-viewer");
    if (viewer) viewer.classList.add("hidden");
  }

  // ---- connector row rendering ------------------------------------------ //

  // POST /api/connectors/{name}/arm — resolves to {ok, ...} or {ok:false,error}.
  function postArm(name, enabled) {
    return fetch("/api/connectors/" + encodeURIComponent(name) + "/arm", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled }),
    })
      .then((r) => r.json())
      .catch(() => ({ ok: false, error: "network error" }));
  }

  // Minimal HTML-escape for label text inside innerHTML (belt+suspenders).
  function esc(s) {
    return String(s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;")
      .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }

  // POST /api/connectors/{name}/settings — resolves to {ok,...} or {ok:false,error}.
  function postSettings(name, values) {
    return fetch("/api/connectors/" + encodeURIComponent(name) + "/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(values),
    })
      .then((r) => {
        if (r.status === 404) return { ok: false, error: "settings_not_supported", status: 404 };
        return r.json();
      })
      .catch(() => ({ ok: false, error: "network error" }));
  }

  // Build the settings form DOM for one connector. Returns the form container div.
  // Each input gets data-settings-key and data-settings-type for value collection.
  function buildSettingsForm(schema, currentSettings) {
    const form = document.createElement("div");
    (schema || []).forEach((field) => {
      const key = field.key || "";
      const ftype = field.type || "str";
      const fLabel = field.label || key;
      const fHint = field.hint || "";
      const fPlaceholder = field.placeholder || "";
      const isSecret = !!field.secret;
      const fDefault = field.default !== undefined ? field.default : "";
      const storedVal = (currentSettings && currentSettings[key] !== undefined)
        ? currentSettings[key] : fDefault;

      const rowEl = document.createElement("div");
      rowEl.className = "dev-srcrow";
      rowEl.style.cssText = "display:flex;align-items:center;gap:8px;margin-bottom:4px;";

      const spanEl = document.createElement("span");
      spanEl.textContent = fLabel;
      spanEl.style.cssText = "min-width:120px;font-size:13px;";

      var inputEl;
      if (ftype === "bool") {
        const switchWrap = document.createElement("label");
        switchWrap.className = "switch";
        inputEl = document.createElement("input");
        inputEl.type = "checkbox";
        inputEl.checked = !!storedVal;
        const trackEl = document.createElement("span");
        trackEl.className = "track";
        switchWrap.append(inputEl, trackEl);
        rowEl.append(spanEl, switchWrap);
      } else {
        inputEl = document.createElement("input");
        inputEl.autocomplete = "off";
        inputEl.spellcheck = false;
        inputEl.style.cssText = "flex:1;min-width:0;";
        if (ftype === "int") {
          inputEl.type = "number";
          inputEl.step = "1";
          inputEl.value = String(storedVal !== "" ? storedVal : "");
        } else if (ftype === "float") {
          inputEl.type = "number";
          inputEl.step = "any";
          inputEl.value = String(storedVal !== "" ? storedVal : "");
        } else if (isSecret) {
          inputEl.type = "password";
          inputEl.autocomplete = "new-password";
          // "•••" = value is set but hidden; send it back as-is to leave unchanged.
          inputEl.value = String(storedVal);
          if (!storedVal && fPlaceholder) inputEl.placeholder = fPlaceholder;
        } else {
          inputEl.type = "text";
          inputEl.value = String(storedVal);
          if (fPlaceholder) inputEl.placeholder = fPlaceholder;
        }
        rowEl.append(spanEl, inputEl);
      }
      inputEl.dataset.settingsKey = key;
      inputEl.dataset.settingsType = ftype;
      form.appendChild(rowEl);

      if (fHint) {
        const hintEl = document.createElement("div");
        hintEl.className = "hint";
        hintEl.style.cssText = "margin: 0 0 6px 128px; font-size: 11px;";
        hintEl.textContent = fHint;
        form.appendChild(hintEl);
      }
    });
    return form;
  }

  // Collect values from a settings form. Returns {key: value, ...}.
  function collectFormValues(form, schema) {
    var values = {};
    (schema || []).forEach((field) => {
      const key = field.key || "";
      const ftype = field.type || "str";
      const inputEl = form.querySelector("[data-settings-key=\"" + key + "\"]");
      if (!inputEl) return;
      if (ftype === "bool") {
        values[key] = !!inputEl.checked;
      } else if (ftype === "int") {
        values[key] = inputEl.value !== "" ? parseInt(inputEl.value, 10) : 0;
      } else if (ftype === "float") {
        values[key] = inputEl.value !== "" ? parseFloat(inputEl.value) : 0;
      } else {
        values[key] = inputEl.value;   // includes "•••" for unchanged secrets
      }
    });
    return values;
  }

  function renderConnector(c) {
    const name = c && typeof c.name === "string" ? c.name : "";
    const label = c && typeof c.label === "string" ? c.label : name;
    const description = c && typeof c.description === "string" ? c.description : "";
    const grantLines = Array.isArray(c.grant_lines) ? c.grant_lines : [];
    const isControl = !!c.control;
    const isArmed = !!c.armed;
    const needsReconsent = !!c.needs_reconsent;
    const isRunning = !!c.running;
    const schema = Array.isArray(c.settings_schema) ? c.settings_schema : [];
    const currentSettings = (c.settings && typeof c.settings === "object") ? c.settings : {};
    const hasSettings = schema.length > 0;

    const row = document.createElement("div");
    row.dataset.connName = name;
    row.style.cssText = "padding: 10px 0; border-bottom: 1px solid rgba(255,255,255,0.06);";

    // ---- header: status dot · label · badges · spacer · settings · debug · toggle ----
    const header = document.createElement("div");
    header.style.cssText = "display:flex; align-items:center; gap:8px; flex-wrap:wrap;";

    const statusDot = document.createElement("span");
    statusDot.title = isRunning ? "running" : "not running";
    statusDot.style.cssText = "font-size:10px; flex-shrink:0; color:" +
      (isRunning ? "var(--accent-2, #3ecf8e)" : "rgba(255,255,255,0.28)") + ";";
    statusDot.textContent = "●";   // ●

    const lbl = document.createElement("b");
    lbl.textContent = label;

    // "⚠ Can control the motor" — only for control:true connectors.
    const ctrlBadge = document.createElement("span");
    ctrlBadge.className = "badge" + (isControl ? "" : " hidden");
    if (isControl) {
      ctrlBadge.style.cssText =
        "background:rgba(255,60,60,0.15);color:#ff6060;" +
        "border:1px solid rgba(255,60,60,0.35);";
    }
    ctrlBadge.textContent = "⚠ Can control the motor";

    // "permissions changed — re-approve" badge.
    const reconsentBadge = document.createElement("span");
    reconsentBadge.className = "badge" + (needsReconsent ? "" : " hidden");
    if (needsReconsent) {
      reconsentBadge.style.cssText =
        "background:rgba(224,161,58,0.15);color:#e0a13a;" +
        "border:1px solid rgba(224,161,58,0.35);cursor:pointer;";
    }
    reconsentBadge.title = "Permissions changed — click to re-approve";
    reconsentBadge.textContent = "permissions changed — re-approve";

    const spacer = document.createElement("span");
    spacer.style.flex = "1";

    // ⚙ Settings button — shown only when the connector has a schema.
    const settingsBtn = document.createElement("button");
    settingsBtn.type = "button";
    settingsBtn.className = "btn-ghost" + (hasSettings ? "" : " hidden");
    settingsBtn.title = "Configure settings";
    settingsBtn.textContent = "⚙ Settings";   // ⚙

    const debugBtn = document.createElement("button");
    debugBtn.type = "button";
    debugBtn.className = "btn-ghost";
    debugBtn.title = "Live debug data";
    debugBtn.textContent = "🐞 Debug";   // 🐞

    const switchLbl = document.createElement("label");
    switchLbl.className = "switch";
    const toggle = document.createElement("input");
    toggle.type = "checkbox";
    toggle.checked = isArmed || needsReconsent;
    const track = document.createElement("span");
    track.className = "track";
    switchLbl.append(toggle, track);

    header.append(statusDot, lbl, ctrlBadge, reconsentBadge, spacer, settingsBtn, debugBtn, switchLbl);
    row.appendChild(header);

    // Description hint
    if (description) {
      const desc = document.createElement("div");
      desc.className = "hint";
      desc.style.marginTop = "4px";
      desc.textContent = description;
      row.appendChild(desc);
    }

    // Grant lines — always visible so the user knows what was consented to.
    if (grantLines.length) {
      const ul = document.createElement("ul");
      ul.className = "hint";
      ul.style.cssText = "margin: 6px 0 0; padding-left: 18px;";
      grantLines.forEach((line) => {
        const li = document.createElement("li");
        li.textContent = typeof line === "string" ? line : String(line);
        ul.appendChild(li);
      });
      row.appendChild(ul);
    }

    // ---- inline consent confirm block (hidden until Enable is triggered) ----
    const consentBlock = document.createElement("div");
    consentBlock.className = "hidden";
    consentBlock.style.cssText =
      "margin-top:10px;padding:10px 12px;border-radius:var(--r-sm,6px);" +
      "background:rgba(0,0,0,0.2);border:1px solid rgba(255,255,255,0.08);";

    if (grantLines.length > 0) {
      const consentTitle = document.createElement("div");
      consentTitle.className = "hint";
      consentTitle.innerHTML = "This allows <b>" + esc(label) + "</b> to:";

      const consentUl = document.createElement("ul");
      consentUl.style.cssText = "margin: 6px 0 8px; padding-left: 18px;";
      grantLines.forEach((line) => {
        const li = document.createElement("li");
        li.className = "hint";
        li.style.marginBottom = "2px";
        li.textContent = typeof line === "string" ? line : String(line);
        consentUl.appendChild(li);
      });

      consentBlock.append(consentTitle, consentUl);
    }

    const consentBtnRow = document.createElement("div");
    consentBtnRow.className = "btn-row";
    const enableBtn = document.createElement("button");
    enableBtn.type = "button";
    enableBtn.className = "btn-primary";
    enableBtn.textContent = "Enable";
    const cancelBtn = document.createElement("button");
    cancelBtn.type = "button";
    cancelBtn.className = "btn-ghost";
    cancelBtn.textContent = "Cancel";
    consentBtnRow.append(enableBtn, cancelBtn);

    consentBlock.append(consentBtnRow);
    row.appendChild(consentBlock);

    // ---- inline settings panel (hidden until ⚙ Settings is clicked) --------
    const settingsPanel = document.createElement("div");
    settingsPanel.className = "hidden";
    settingsPanel.style.cssText =
      "margin-top:10px;padding:10px 12px;border-radius:var(--r-sm,6px);" +
      "background:rgba(0,0,0,0.2);border:1px solid rgba(255,255,255,0.08);";

    const settingsTitle = document.createElement("div");
    settingsTitle.className = "ctx-sub";
    settingsTitle.style.marginBottom = "8px";
    settingsTitle.textContent = "Settings";
    settingsPanel.appendChild(settingsTitle);

    // The form is rebuilt when Cancel resets it; keep a mutable reference.
    var settingsForm = buildSettingsForm(schema, currentSettings);
    settingsPanel.appendChild(settingsForm);

    const settingsBtnRow = document.createElement("div");
    settingsBtnRow.className = "btn-row";
    const saveBtn = document.createElement("button");
    saveBtn.type = "button";
    saveBtn.className = "btn-primary";
    saveBtn.textContent = "Save";
    const cancelSettingsBtn = document.createElement("button");
    cancelSettingsBtn.type = "button";
    cancelSettingsBtn.className = "btn-ghost";
    cancelSettingsBtn.textContent = "Cancel";
    settingsBtnRow.append(saveBtn, cancelSettingsBtn);
    settingsPanel.appendChild(settingsBtnRow);

    const settingsStatusEl = document.createElement("div");
    settingsStatusEl.className = "hint";
    settingsStatusEl.style.minHeight = "1.2em";
    settingsPanel.appendChild(settingsStatusEl);

    row.appendChild(settingsPanel);

    // Per-row status feedback line (for arm/disarm flow).
    const statusEl = document.createElement("div");
    statusEl.className = "hint";
    statusEl.style.minHeight = "1.2em";
    row.appendChild(statusEl);

    // ---- helpers -----------------------------------------------------------

    function setStatus(msg, kind) {
      statusEl.textContent = msg || "";
      statusEl.className = "hint" + (kind ? " " + kind : "");
    }

    function setSettingsStatus(msg, kind) {
      settingsStatusEl.textContent = msg || "";
      settingsStatusEl.className = "hint" + (kind ? " " + kind : "");
    }

    function showConsentBlock() { consentBlock.classList.remove("hidden"); }
    function hideConsentBlock() { consentBlock.classList.add("hidden"); }

    function showSettingsPanel() {
      hideConsentBlock();
      settingsPanel.classList.remove("hidden");
    }
    function hideSettingsPanel() { settingsPanel.classList.add("hidden"); }

    // ---- event wiring ------------------------------------------------------

    // Toggle change — two paths: enable (consent required) or disable (immediate).
    toggle.addEventListener("change", () => {
      const wantsOn = toggle.checked;
      if (!wantsOn) {
        // Disable: always immediate, no confirm flow.
        hideConsentBlock();
        hideSettingsPanel();
        toggle.disabled = true;
        setStatus("Disabling…", "busy");
        postArm(name, false).then((res) => {
          toggle.disabled = false;
          if (!res || !res.ok) {
            toggle.checked = isArmed || needsReconsent;   // revert on error
            setStatus("Failed: " + ((res && res.error) || "unknown"), "err");
            return;
          }
          toggle.checked = false;
          reconsentBadge.classList.add("hidden");
          setStatus("Disabled.", "");
          setTimeout(load, 400);
        });
      } else {
        // Enable: show consent flow; NEVER POST without the confirm step.
        toggle.checked = false;   // revert visual until confirmed
        hideSettingsPanel();
        showConsentBlock();
        setStatus("", "");
      }
    });

    // Reconsent badge click: surface the consent flow for re-approval.
    reconsentBadge.addEventListener("click", () => {
      showConsentBlock();
    });

    // Consent "Enable" — the ONLY path that POSTs {enabled:true}.
    enableBtn.addEventListener("click", () => {
      hideConsentBlock();
      toggle.disabled = true;
      setStatus("Enabling…", "busy");
      postArm(name, true).then((res) => {
        toggle.disabled = false;
        if (!res || !res.ok) {
          toggle.checked = false;
          setStatus("Failed: " + ((res && res.error) || "unknown"), "err");
          return;
        }
        toggle.checked = true;
        reconsentBadge.classList.add("hidden");
        setStatus(res.running
          ? "Enabled and running."
          : "Enabled (starting up…).", "ok");
        setTimeout(load, 600);
      });
    });

    // Consent "Cancel" — close the block, restore the toggle's prior state.
    cancelBtn.addEventListener("click", () => {
      hideConsentBlock();
      toggle.checked = isArmed || needsReconsent;
      setStatus("", "");
    });

    // ⚙ Settings button — toggle the settings panel.
    settingsBtn.addEventListener("click", () => {
      if (settingsPanel.classList.contains("hidden")) {
        hideConsentBlock();
        showSettingsPanel();
        setSettingsStatus("", "");
      } else {
        hideSettingsPanel();
      }
    });

    // Settings "Save" — collect values and POST.
    saveBtn.addEventListener("click", () => {
      const values = collectFormValues(settingsForm, schema);
      saveBtn.disabled = true;
      cancelSettingsBtn.disabled = true;
      setSettingsStatus("Saving…", "busy");
      postSettings(name, values).then((res) => {
        saveBtn.disabled = false;
        cancelSettingsBtn.disabled = false;
        if (!res) { setSettingsStatus("No response.", "err"); return; }
        if (res.error === "settings_not_supported") {
          // Endpoint doesn't exist (old backend) — hide the button permanently.
          settingsBtn.classList.add("hidden");
          hideSettingsPanel();
          setSettingsStatus("", "");
          return;
        }
        if (!res.ok) {
          setSettingsStatus("Error: " + (res.error || "unknown"), "err");
          return;
        }
        // Success.
        hideSettingsPanel();
        if (res.needs_reconsent) {
          setStatus("Saved. Re-approval required — permissions changed.", "warn");
          setTimeout(load, 400);
        } else {
          setStatus("Settings saved.", "ok");
          setTimeout(load, 400);
        }
      });
    });

    // Settings "Cancel" — hide and reset form to original values.
    cancelSettingsBtn.addEventListener("click", () => {
      // Rebuild the form from the original stored values (discard dirty edits).
      settingsPanel.removeChild(settingsForm);
      settingsForm = buildSettingsForm(schema, currentSettings);
      settingsPanel.insertBefore(settingsForm, settingsBtnRow);
      setSettingsStatus("", "");
      hideSettingsPanel();
    });

    // Debug button — toggle the shared viewer open/closed for this connector.
    debugBtn.addEventListener("click", () => {
      if (curDebugName === name) {
        closeDebug();
      } else {
        openDebug(name);
        const viewer = $("conn-debug-viewer");
        if (viewer) viewer.scrollIntoView({ behavior: "smooth", block: "nearest" });
      }
    });

    return row;
  }

  // ---- list rendering ------------------------------------------------------ //

  function renderConnectors(list) {
    const host = $("conn-list");
    if (!host) return;
    host.innerHTML = "";
    const items = Array.isArray(list) ? list : [];
    if (!items.length) {
      const hint = document.createElement("div");
      hint.className = "hint";
      hint.textContent = "No connectors registered.";
      host.appendChild(hint);
      return;
    }
    items.forEach((c) => {
      if (!c || typeof c.name !== "string") return;
      host.appendChild(renderConnector(c));
    });
  }

  // ---- load / degrade ------------------------------------------------------ //

  function showUnavailable() {
    card.classList.add("hidden");
  }

  function setBadge(txt) {
    const b = $("conn-state");
    if (b) b.textContent = txt || "";
  }

  function load() {
    fetch("/api/connectors")
      .then((r) => {
        if (r.status === 404) { showUnavailable(); return null; }
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      })
      .then((data) => {
        if (!data) return;   // 404 already handled
        const list = Array.isArray(data.connectors) ? data.connectors : [];
        const body = $("conn-body");
        if (body) body.classList.remove("hidden");
        renderConnectors(list);
        const nArmed = list.filter((c) => !!c.armed).length;
        setBadge(list.length ? nArmed + "/" + list.length + " armed" : "");
        loaded = true;
      })
      .catch(() => { showUnavailable(); });
  }

  // ---- wiring -------------------------------------------------------------- //

  const closeBtn = $("conn-debug-close");
  if (closeBtn) closeBtn.addEventListener("click", closeDebug);

  // Lazy: fetch only on the card's first open; stop debug on collapse.
  card.addEventListener("toggle", () => {
    if (card.open && !loaded) load();
    if (!card.open) closeDebug();   // never leak the poll
  });
})();

/* Vanchor-NG — phone-as-sensor streaming (hardware.drivers.phone).
 *
 * When "Share this phone's GPS & compass" is on, this browser streams
 * geolocation fixes + device-orientation heading to the boat over the WS
 * (VA.sendVolatile: fire-and-forget, never queued). The SERVER arbitrates:
 * one feeder per sensor kind; others are rejected; a slot frees only when the
 * feeding client disconnects. Selecting source "Phone" in Devices is what
 * actually consumes the stream. Disclaimer: crude, varies wildly by device.
 */
"use strict";

(function () {
  const $ = (id) => document.getElementById(id);
  const box = $("phone-share");
  if (!box || !window.VA) return;
  const statusEl = $("phone-share-status");
  const KEY = "vanchor-phone-share";

  let geoWatch = null;
  let keepAlive = null;      // re-send timer: browsers only fire watchPosition
  let lastFix = null;        // on CHANGE, so a stationary phone starves the boat
  let lastFixT = 0;
  let orientHandler = null;
  let lastCompassSend = 0;
  const state = { gps: "", compass: "" };   // last server-reported status

  function setStatus(msg) {
    if (!statusEl) return;
    statusEl.hidden = !msg;
    statusEl.textContent = msg || "";
  }

  function summarize() {
    const words = [];
    for (const kind of ["gps", "compass"]) {
      const st = state[kind];
      if (!st) continue;
      if (st === "accepted") words.push(kind.toUpperCase() + ": feeding the boat");
      else if (st === "rejected") words.push(kind.toUpperCase() + ": another device is feeding");
      else if (st === "inactive") words.push(kind.toUpperCase() + ": select source “Phone” in Devices");
    }
    if (words.length) setStatus("📱 " + words.join(" · "));
  }
  VA.onPhoneSensors((t) => {
    if (t && t.kind) {
      state[t.kind] = t.status || "";
      if (VA.rum) VA.rum("phone_feeder", t.kind + " -> " + t.status);
      summarize();
    }
  });

  function startGps() {
    if (geoWatch !== null) return;
    if (!("geolocation" in navigator)) {
      setStatus("⚠ No geolocation in this browser.");
      return;
    }
    if (!window.isSecureContext) {
      setStatus("⚠ GPS sharing needs the https:// address (compass may still work).");
    }
    try {
      geoWatch = navigator.geolocation.watchPosition((pos) => {
        const c = pos.coords;
        const now = Date.now();
        if (lastFixT && now - lastFixT > 5000 && VA.rum) {
          VA.rum("geo_gap", ((now - lastFixT) / 1000).toFixed(1) + "s between fixes", "warn");
        }
        lastFix = {
          type: "phone_gps", lat: c.latitude, lon: c.longitude,
          accuracy: c.accuracy, speed: c.speed, heading: c.heading,
        };
        lastFixT = now;
        VA.sendVolatile(lastFix);
      }, (err) => {
        if (VA.rum) VA.rum("geo_error", (err && err.message) || "denied", "warn");
        setStatus("⚠ GPS sharing failed: " + (err && err.message ? err.message : "denied"));
      }, { enableHighAccuracy: true, maximumAge: 1000, timeout: 15000 });
      // Keep-alive: if the watcher goes quiet (stationary phone), re-send the
      // last fix for up to 15 s so the boat doesn't declare the fix lost; past
      // that the loss is real — stay silent and let the gap show honestly.
      keepAlive = setInterval(() => {
        if (!lastFix || !lastFixT) return;
        const age = Date.now() - lastFixT;
        if (age > 2500 && age < 15000) {
          VA.sendVolatile(Object.assign({ cached: true }, lastFix));
        }
      }, 2000);
    } catch (e) { /* ignore */ }
  }

  function onOrient(ev) {
    // iOS exposes a fused magnetic heading; elsewhere absolute alpha is
    // 0 = north counter-clockwise, so heading = 360 - alpha.
    let heading = null;
    if (typeof ev.webkitCompassHeading === "number") heading = ev.webkitCompassHeading;
    else if (ev.absolute === true && typeof ev.alpha === "number") heading = 360 - ev.alpha;
    if (heading === null || !isFinite(heading)) return;
    const now = Date.now();
    if (now - lastCompassSend < 250) return;   // ~4 Hz is plenty
    lastCompassSend = now;
    VA.sendVolatile({ type: "phone_compass", heading: (heading % 360 + 360) % 360 });
  }

  function startCompass() {
    if (orientHandler) return;
    orientHandler = onOrient;
    if ("ondeviceorientationabsolute" in window) {
      window.addEventListener("deviceorientationabsolute", orientHandler);
    } else {
      window.addEventListener("deviceorientation", orientHandler);
    }
  }

  function stopAll() {
    if (geoWatch !== null && "geolocation" in navigator) {
      try { navigator.geolocation.clearWatch(geoWatch); } catch (e) { /* ignore */ }
    }
    geoWatch = null;
    if (keepAlive) { clearInterval(keepAlive); keepAlive = null; }
    lastFix = null; lastFixT = 0;
    if (orientHandler) {
      window.removeEventListener("deviceorientationabsolute", orientHandler);
      window.removeEventListener("deviceorientation", orientHandler);
      orientHandler = null;
    }
    state.gps = state.compass = "";
    setStatus("");
  }

  async function start() {
    // iOS needs an explicit permission request from a user gesture.
    try {
      if (typeof DeviceOrientationEvent !== "undefined" &&
          typeof DeviceOrientationEvent.requestPermission === "function") {
        await DeviceOrientationEvent.requestPermission();
      }
    } catch (e) { /* denied -> compass just won't fire */ }
    startGps();
    startCompass();
    setStatus("📱 Sharing… waiting for the boat to accept.");
  }

  box.addEventListener("change", () => {
    try { localStorage.setItem(KEY, box.checked ? "1" : "0"); } catch (e) { /* ignore */ }
    if (box.checked) start(); else stopAll();
  });

  // Restore across reloads. (GPS permission persists; iOS orientation
  // permission may need the toggle re-flipped once per session.)
  let saved = "0";
  try { saved = localStorage.getItem(KEY) || "0"; } catch (e) { /* ignore */ }
  if (saved === "1") { box.checked = true; start(); }
})();
