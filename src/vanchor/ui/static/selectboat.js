/* Vanchor-NG — Simulator "Select Boat" dialog (#96).
 *
 * Adds a phone-friendly modal that lists the preset boats from
 * GET /api/boat/profiles as cards. Each card shows a short character blurb
 * derived from its specs (hull_tracking / max_speed_mps / thruster_mount /
 * thruster_y_m) and a Select button that activates the profile via
 * POST /api/boat/profiles/{id}/activate, closes the dialog and shows a brief
 * confirmation. The currently-active boat is highlighted.
 *
 * Degrades gracefully: if the endpoints 404 the dialog shows an explanatory
 * message instead of cards.
 */
"use strict";

(function () {
  const VA = (window.VA = window.VA || {});
  const $ = (id) => document.getElementById(id);

  const openBtn = $("select-boat-open");
  const dlg = $("select-boat-dialog");
  const cardsEl = $("select-boat-cards");
  const statusEl = $("select-boat-status");
  const closeBtn = $("select-boat-close");
  const currentEl = $("select-boat-current");
  if (!openBtn || !dlg) return;   // markup absent — nothing to wire

  let activeId = null;

  // ---- spec → human blurb --------------------------------------------------
  function mps2kn(v) { return v / 0.514444; }

  function speedWord(maxMps) {
    if (!Number.isFinite(maxMps)) return "";
    if (maxMps >= 5) return "fast, planing";
    if (maxMps >= 3) return "brisk";
    return "trolling-speed";
  }
  function trackWord(ht) {
    if (!Number.isFinite(ht)) return "";
    if (ht <= 0.5) return "loose & snappy";
    if (ht < 0.9) return "a little loose";
    if (ht <= 1.2) return "balanced";
    return "tracks firm & straight";
  }
  function mountPhrase(mount, yoff) {
    const m = mount === "stern" ? "stern-mounted motor" : "bow-mounted motor";
    if (Number.isFinite(yoff) && Math.abs(yoff) > 0.05) {
      return m + ", clamped off-centre";
    }
    return m;
  }

  // Build a one-line character blurb from the profile specs.
  function blurb(p) {
    const bits = [];
    const speed = speedWord(p.max_speed_mps);
    const track = trackWord(p.hull_tracking);
    const mount = mountPhrase(p.thruster_mount, p.thruster_y_m);
    if (Number.isFinite(p.length_m) && p.length_m <= 3.8) bits.push("short, light hull");
    if (speed) bits.push(speed);
    if (track) bits.push(track);
    if (mount) bits.push(mount);
    return bits.join(" · ");
  }

  function specChips(p) {
    const out = [];
    if (Number.isFinite(p.max_speed_mps)) out.push("max " + mps2kn(p.max_speed_mps).toFixed(1) + " kn");
    if (Number.isFinite(p.max_thrust_n)) out.push(Math.round(p.max_thrust_n) + " N");
    if (Number.isFinite(p.length_m)) out.push(p.length_m.toFixed(1) + " m");
    if (p.thruster_mount) out.push(p.thruster_mount);
    return out;
  }

  // ---- render --------------------------------------------------------------
  function renderCards(profiles) {
    cardsEl.textContent = "";
    if (!Array.isArray(profiles) || !profiles.length) {
      const p = document.createElement("p");
      p.className = "sb-empty";
      p.textContent = "No boat presets available.";
      cardsEl.appendChild(p);
      return;
    }
    for (const p of profiles) {
      const card = document.createElement("div");
      card.className = "sb-card";
      if (p.id === activeId) card.classList.add("active");

      const name = document.createElement("div");
      name.className = "sb-name";
      name.textContent = p.name || p.id;
      if (p.id === activeId) {
        const tag = document.createElement("span");
        tag.className = "sb-active-tag";
        tag.textContent = "ACTIVE";
        name.appendChild(tag);
      }

      const desc = document.createElement("div");
      desc.className = "sb-desc";
      desc.textContent = blurb(p) || "—";

      const chips = document.createElement("div");
      chips.className = "sb-chips";
      for (const c of specChips(p)) {
        const s = document.createElement("span");
        s.className = "sb-chip";
        s.textContent = c;
        chips.appendChild(s);
      }

      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "sb-select";
      btn.textContent = p.id === activeId ? "Selected" : "Select";
      btn.disabled = p.id === activeId;
      btn.addEventListener("click", () => activate(p));

      card.appendChild(name);
      card.appendChild(desc);
      card.appendChild(chips);
      card.appendChild(btn);
      cardsEl.appendChild(card);
    }
  }

  function setStatus(msg, kind) {
    if (!statusEl) return;
    statusEl.textContent = msg || "";
    statusEl.dataset.kind = kind || "";
  }

  // ---- data ----------------------------------------------------------------
  async function load() {
    setStatus("Loading boats…", "");
    cardsEl.textContent = "";
    try {
      const data = await VA.getJSON("/api/boat/profiles");
      activeId = data && data.active_id != null ? data.active_id : null;
      const profiles = (data && Array.isArray(data.profiles)) ? data.profiles : [];
      renderCards(profiles);
      setStatus("", "");
      updateCurrentLabel(profiles);
    } catch (e) {
      cardsEl.textContent = "";
      setStatus("Boat profiles unavailable.", "err");
    }
  }

  function updateCurrentLabel(profiles) {
    if (!currentEl) return;
    const act = (profiles || []).find((p) => p.id === activeId);
    currentEl.textContent = act ? ("Active boat: " + (act.name || act.id)) : "";
  }

  async function activate(p) {
    setStatus("Activating " + (p.name || p.id) + "…", "");
    try {
      await VA.postJSON("/api/boat/profiles/" + encodeURIComponent(p.id) + "/activate", {});
      activeId = p.id;
      close();
      if (currentEl) currentEl.textContent = "Active boat: " + (p.name || p.id);
      // Brief confirmation toast under the button.
      if (currentEl) {
        currentEl.classList.add("sb-confirm");
        setTimeout(() => currentEl.classList.remove("sb-confirm"), 1800);
      }
      if (VA.logAlert) VA.logAlert("info", "Boat profile activated: " + (p.name || p.id));
    } catch (e) {
      setStatus("Activation failed.", "err");
    }
  }

  // ---- open / close --------------------------------------------------------
  function open() {
    if (typeof dlg.showModal === "function") { if (!dlg.open) dlg.showModal(); }
    else dlg.setAttribute("open", "");
    load();
  }
  function close() {
    if (typeof dlg.close === "function" && dlg.open) dlg.close();
    else dlg.removeAttribute("open");
  }

  openBtn.addEventListener("click", open);
  if (closeBtn) closeBtn.addEventListener("click", close);
  dlg.addEventListener("click", (e) => { if (e.target === dlg) close(); });

  // Keep the "Active boat" label warm even before the dialog is first opened.
  (async function initLabel() {
    try {
      const data = await VA.getJSON("/api/boat/profiles");
      activeId = data && data.active_id != null ? data.active_id : null;
      updateCurrentLabel((data && data.profiles) || []);
    } catch (e) { /* ignore — sim card may be hidden anyway */ }
  })();

  VA.selectBoat = { open, close, reload: load };
})();
