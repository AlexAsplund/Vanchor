/* Vanchor-NG — Boat settings card (#75).
 *
 * Lives in the Settings drawer. Two parts:
 *   1. Profiles — pick / add / rename / delete / switch named boat profiles via
 *      the /api/boat/profiles endpoints. Degrades gracefully: if those 404 the
 *      whole profiles block stays hidden and the card works as a single-boat
 *      editor against POST /api/boat.
 *   2. Spec editor — sliders + a segmented mount control for the active boat.
 *      Editing any field applies LIVE via POST /api/boat (slider drags are
 *      debounced) and also patches the active profile so it persists. The
 *      current values come from GET /api/boat (telemetry `boat` as a fallback).
 *
 * max_speed_mps is deliberately not editable — calibration measures it.
 */
"use strict";

(function () {
  const card = document.getElementById("boat-card");
  if (!card) return;

  // Numeric spec fields the editor exposes (mount handled separately).
  const SPEC_FIELDS = [
    "length_m", "beam_m", "mass_kg", "max_thrust_n", "reverse_efficiency",
    "max_steer_angle_deg", "autopilot_steer_deg", "shaft_dia_mm",
    "steer_range_deg", "steer_reduction", "sonar_cone_deg", "hull_tracking",
  ];
  const MOUNTS = ["bow", "center", "stern"];

  const grid = document.getElementById("boat-spec-grid");
  const specStatus = document.getElementById("boat-spec-status");
  const appliedTag = document.getElementById("boat-applied");
  const cardState = document.getElementById("boat-card-state");

  const profilesWrap = document.getElementById("boat-profiles");
  const sel = document.getElementById("boat-profile-select");
  const btnAdd = document.getElementById("boat-profile-add");
  const btnRename = document.getElementById("boat-profile-rename");
  const btnDelete = document.getElementById("boat-profile-delete");
  const profStatus = document.getElementById("boat-profile-status");

  let mountVal = "bow";
  let mpOffset = 0;   // thruster_offset_m (+ forward toward bow)
  let mpY = 0;        // thruster_y_m      (+ starboard)
  let mp = null;      // motor-placement control (shared module)
  let profilesEnabled = false;
  let profiles = [];
  let activeId = null;
  let loading = false; // suppress live-apply while we programmatically set inputs

  // ---- helpers -----------------------------------------------------------
  function specEl(field) { return grid.querySelector(`.spec[data-field="${field}"]`); }

  const hullCaption = document.getElementById("boat-hull-caption");
  // Map a hull_tracking value (~0.35..2.5) to a short, intuitive caption.
  function hullText(v) {
    v = Number(v);
    if (!Number.isFinite(v)) return "";
    if (v < 0.6) return "Jon boat — loose, snappy";
    if (v < 0.85) return "Skiff — light tracking";
    if (v < 1.2) return "Skiff — balanced";
    if (v < 1.7) return "Semi-V — steady";
    if (v < 2.2) return "Deep-V — firm tracking";
    return "Keelboat — tracks straight, sluggish";
  }
  function updateHullCaption(v) { if (hullCaption) hullCaption.textContent = hullText(v); }

  let appliedTimer = null;
  function flashApplied() {
    if (!appliedTag) return;
    appliedTag.classList.remove("hidden");
    clearTimeout(appliedTimer);
    appliedTimer = setTimeout(() => appliedTag.classList.add("hidden"), 1400);
  }

  function initMotorPlace() {
    if (mp || !VA.MotorPlace) return;
    const host = document.getElementById("boat-mp");
    if (!host) return;
    mp = VA.MotorPlace.create({
      root: host,
      idPrefix: "boat-mp",
      onChange: (off, y, mnt) => {
        mpOffset = off; mpY = y; mountVal = mnt;
        if (loading) return;
        queueApply("thruster_offset_m", off);
        queueApply("thruster_y_m", y);
        queueApply("thruster_mount", mnt);
      },
    });
  }

  // Populate the editor controls from a specs object (without firing live-apply).
  function fillSpecs(boat) {
    boat = boat || {};
    loading = true;
    SPEC_FIELDS.forEach((f) => {
      const el = specEl(f);
      if (!el) return;
      const input = el.querySelector("input[type=range]");
      const out = el.querySelector(".spec-out");
      const v = Number(boat[f]);
      if (Number.isFinite(v)) {
        if (v < Number(input.min)) input.min = v;
        if (v > Number(input.max)) input.max = v;
        input.value = v;
      }
      out.textContent = input.value;
      if (f === "hull_tracking") updateHullCaption(input.value);
    });
    // Seed the motor-placement control from the offsets (legacy mount fallback).
    mountVal = MOUNTS.includes(boat.thruster_mount) ? boat.thruster_mount : "bow";
    initMotorPlace();
    if (mp) {
      mp.setBoat({ length_m: Number(boat.length_m), beam_m: Number(boat.beam_m) });
      const off = Number(boat.thruster_offset_m);
      const y = Number(boat.thruster_y_m);
      mpOffset = Number.isFinite(off) ? off
        : (mountVal === "stern" ? -0.3 : (mountVal === "center" ? 0 : 0.3));
      mpY = Number.isFinite(y) ? y : 0;
      mp.setValue(mpOffset, mpY, Number.isFinite(off) ? undefined
        : (mountVal === "stern" ? "stern" : "bow"));
      const v = mp.getValue();
      mpOffset = v.offset_m; mpY = v.y_m; mountVal = v.mount;
    }
    loading = false;
  }

  // Collect the current editor values into a specs object.
  function readSpecs() {
    if (mp) { const v = mp.getValue(); mpOffset = v.offset_m; mpY = v.y_m; mountVal = v.mount; }
    const out = {
      thruster_mount: mountVal,
      thruster_offset_m: mpOffset,
      thruster_y_m: mpY,
    };
    SPEC_FIELDS.forEach((f) => {
      const el = specEl(f);
      if (el) out[f] = parseFloat(el.querySelector("input[type=range]").value);
    });
    return out;
  }

  // ---- live apply (POST /api/boat) + profile patch ----------------------
  async function applyFields(fields) {
    if (specStatus) specStatus.textContent = "applying…";
    try {
      await VA.postJSON("/api/boat", fields);
      flashApplied();
      if (specStatus) specStatus.textContent = "";
      VA.logLine("boat specs applied: " + Object.keys(fields).join(", "));
    } catch (e) {
      if (specStatus) specStatus.textContent = "apply failed: " + e;
      return;
    }
    // Mirror the change onto the active profile so it persists.
    if (profilesEnabled && activeId != null) {
      try { await VA.postJSON(`/api/boat/profiles/${activeId}`, { specs: fields }); }
      catch (e) { /* profile update is best-effort */ }
    }
  }

  // Debounce slider drags so we POST once the user pauses, not every tick.
  let applyTimer = null;
  const pending = {};
  function queueApply(field, value) {
    pending[field] = value;
    clearTimeout(applyTimer);
    applyTimer = setTimeout(() => {
      const fields = Object.assign({}, pending);
      for (const k of Object.keys(pending)) delete pending[k];
      applyFields(fields);
    }, 350);
  }

  // ---- wire spec inputs --------------------------------------------------
  SPEC_FIELDS.forEach((f) => {
    const el = specEl(f);
    if (!el) return;
    const input = el.querySelector("input[type=range]");
    const out = el.querySelector(".spec-out");
    input.addEventListener("input", () => {
      out.textContent = input.value;
      if (f === "hull_tracking") updateHullCaption(input.value);
      // Rescale the motor-placement picture when length/beam change.
      if ((f === "length_m" || f === "beam_m") && mp) {
        const lEl = specEl("length_m"), bEl = specEl("beam_m");
        mp.setBoat({
          length_m: lEl ? parseFloat(lEl.querySelector("input[type=range]").value) : undefined,
          beam_m: bEl ? parseFloat(bEl.querySelector("input[type=range]").value) : undefined,
        });
      }
      if (loading) return;
      queueApply(f, parseFloat(input.value));
    });
  });

  // ---- load current specs (GET /api/boat, telemetry fallback) -----------
  async function loadActiveSpecs() {
    let boat = null;
    try { boat = await VA.getJSON("/api/boat"); } catch (e) { /* fallback below */ }
    if (!boat || typeof boat !== "object") {
      boat = (VA.last && VA.last.boat) || {};
    }
    fillSpecs(boat);
  }

  // ---- profiles ----------------------------------------------------------
  function setProfStatus(msg) { if (profStatus) profStatus.textContent = msg || ""; }

  function renderProfiles() {
    if (!profilesEnabled) return;
    sel.innerHTML = "";
    profiles.forEach((p) => {
      const opt = document.createElement("option");
      opt.value = p.id;
      opt.textContent = (p.name || ("Boat " + p.id)) +
        (String(p.id) === String(activeId) ? "  ● active" : "");
      if (String(p.id) === String(activeId)) opt.selected = true;
      sel.appendChild(opt);
    });
    // Delete is disabled when only one profile remains.
    btnDelete.disabled = profiles.length <= 1;
    const active = profiles.find((p) => String(p.id) === String(activeId));
    if (cardState) cardState.textContent = active ? (active.name || "") : "";
  }

  async function refreshProfiles() {
    let data = null;
    try { data = await VA.getJSON("/api/boat/profiles"); }
    catch (e) { data = null; }
    // 404 / unsupported -> hide profiles UI, keep the single-boat editor.
    if (!data || !Array.isArray(data.profiles)) {
      profilesEnabled = false;
      profilesWrap.classList.add("hidden");
      return;
    }
    profilesEnabled = true;
    profilesWrap.classList.remove("hidden");
    profiles = data.profiles;
    activeId = data.active_id != null ? data.active_id
      : (profiles[0] && profiles[0].id);
    renderProfiles();
  }

  // Switch active profile on selection (activate applies it live).
  sel.addEventListener("change", async () => {
    const id = sel.value;
    if (id == null || String(id) === String(activeId)) return;
    setProfStatus("switching…");
    try {
      await VA.postJSON(`/api/boat/profiles/${id}/activate`, {});
      activeId = id;
      VA.logLine("active boat profile -> " + id);
      await loadActiveSpecs();
      renderProfiles();
      setProfStatus("");
    } catch (e) {
      setProfStatus("switch failed: " + e);
      renderProfiles(); // revert selection to current active
    }
  });

  btnAdd.addEventListener("click", async () => {
    const name = (window.prompt("Name for the new boat profile?") || "").trim();
    if (!name) return;
    setProfStatus("creating…");
    try {
      // Seed the new profile with the current editor values.
      const created = await VA.postJSON("/api/boat/profiles", { name, specs: readSpecs() });
      await refreshProfiles();
      // Activate the freshly created profile if the backend returned its id.
      if (created && created.id != null) {
        sel.value = created.id;
        sel.dispatchEvent(new Event("change"));
      }
      setProfStatus("");
    } catch (e) {
      setProfStatus("create failed: " + e);
    }
  });

  btnRename.addEventListener("click", async () => {
    const active = profiles.find((p) => String(p.id) === String(activeId));
    const cur = active ? (active.name || "") : "";
    const name = (window.prompt("Rename boat profile:", cur) || "").trim();
    if (!name || name === cur) return;
    setProfStatus("renaming…");
    try {
      await VA.postJSON(`/api/boat/profiles/${activeId}`, { name });
      if (active) active.name = name;
      renderProfiles();
      setProfStatus("");
    } catch (e) {
      setProfStatus("rename failed: " + e);
    }
  });

  btnDelete.addEventListener("click", async () => {
    if (profiles.length <= 1) return;
    const active = profiles.find((p) => String(p.id) === String(activeId));
    const nm = active ? (active.name || activeId) : activeId;
    if (!window.confirm(`Delete boat profile “${nm}”?`)) return;
    setProfStatus("deleting…");
    try {
      await fetch(`/api/boat/profiles/${activeId}`, { method: "DELETE" });
      await refreshProfiles();   // backend picks a new active; reflect it
      await loadActiveSpecs();
      setProfStatus("");
    } catch (e) {
      setProfStatus("delete failed: " + e);
    }
  });

  // ---- init: refresh when the drawer / card is opened -------------------
  let loadedOnce = false;
  async function ensureLoaded() {
    if (loadedOnce) return;
    loadedOnce = true;
    await refreshProfiles();
    await loadActiveSpecs();
  }
  // Load the first time the Boat card is expanded (cheap + lazy).
  card.addEventListener("toggle", () => { if (card.open) ensureLoaded(); });
  // Also load if the card happens to start open.
  if (card.open) ensureLoaded();
})();
