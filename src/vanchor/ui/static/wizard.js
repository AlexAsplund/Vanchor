/* Vanchor-NG — Boat setup wizard.
 *
 * A polished 4-step modal flow:
 *   0 Welcome + safety disclaimer (must consent to proceed)
 *   1 Boat specs form  — prefilled from GET /api/boat, saved via POST /api/boat
 *   2 Auto-calibration drive — POST /api/calibrate {mode:"quick"}, live progress
 *     from telemetry `calibration`, cancel via POST /api/calibrate/cancel
 *   3 Results — render calibration.results, "Apply & Finish" closes the wizard
 *
 * Backend applies tuning automatically on completion; step 3 is the summary.
 */
"use strict";

(function () {
  const wizard = document.getElementById("wizard");
  const scrim = document.getElementById("wiz-scrim");
  if (!wizard) return;

  const STEPS = 4;
  let step = 0;
  let calibRunning = false;
  let calibDone = false;
  let lastResults = null;

  // Numeric spec fields and their POST keys (segmented mount handled separately).
  // max_speed_mps is intentionally NOT here -- the auto-calibration drive
  // measures it, the user shouldn't have to guess it.
  const SPEC_FIELDS = [
    "length_m", "beam_m", "mass_kg",
    "max_thrust_n", "shaft_dia_mm", "steer_range_deg", "hull_tracking",
  ];

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
  function updateHullCaption(v) {
    const cap = document.getElementById("wiz-hull-caption");
    if (cap) cap.textContent = hullText(v);
  }
  let mountVal = "bow";
  // Motor placement control (shared module). Captures thruster_offset_m /
  // thruster_y_m by tapping a top-down boat; replaces the old bow/stern segment.
  let mp = null;
  let mpOffset = 0;   // thruster_offset_m  (+ forward toward bow)
  let mpY = 0;        // thruster_y_m       (+ starboard)
  function initMotorPlace() {
    if (mp || !VA.MotorPlace) return;
    const host = document.getElementById("wiz-mp");
    if (!host) return;
    mp = VA.MotorPlace.create({
      root: host,
      idPrefix: "wiz-mp",
      onChange: (off, y, mnt) => {
        mpOffset = off; mpY = y; mountVal = mnt;
      },
    });
  }

  // ---- open / close ------------------------------------------------------
  function applyFollowOffset() {
    if (!window.VA || !VA.map || !VA.map.setFollowOffset) return;
    const card = wizard.querySelector(".wiz-card");
    if (!card) return;
    const r = card.getBoundingClientRect();
    const W = window.innerWidth, H = window.innerHeight;
    if (document.body.classList.contains("mobile")) {
      VA.map.setFollowOffset(0, r.top / 2 - H / 2);          // boat above the bottom panel
    } else {
      VA.map.setFollowOffset((r.right + W) / 2 - W / 2, 0);  // boat to the right of the side panel
    }
    if (VA.map.recenter) VA.map.recenter();                  // keep following so it stays in view
  }
  function open() {
    // The Setup button now lives inside the Settings drawer; close it so the
    // wizard isn't stacked on top of the open drawer.
    const drawer = document.getElementById("settings");
    if (drawer) drawer.classList.add("hidden");
    document.querySelectorAll(".scrim").forEach((s) => {
      if (s !== scrim) s.classList.add("hidden");
    });
    wizard.classList.remove("hidden");
    scrim.classList.remove("hidden");
    requestAnimationFrame(applyFollowOffset);
    window.addEventListener("resize", applyFollowOffset);
    goStep(0);
    loadBoat();
  }
  function close() {
    wizard.classList.add("hidden");
    scrim.classList.add("hidden");
    window.removeEventListener("resize", applyFollowOffset);
    if (window.VA && VA.map && VA.map.setFollowOffset) VA.map.setFollowOffset(0, 0);
  }
  VA.openWizard = open;

  document.getElementById("wiz-close").addEventListener("click", close);
  scrim.addEventListener("click", close);

  // ---- stepper navigation ------------------------------------------------
  function goStep(n) {
    step = Math.max(0, Math.min(STEPS - 1, n));
    wizard.querySelectorAll(".wiz-step").forEach((el) =>
      el.classList.toggle("hidden", Number(el.dataset.step) !== step));
    wizard.querySelectorAll(".wiz-stepper .step").forEach((el) => {
      const i = Number(el.dataset.step);
      el.classList.toggle("active", i === step);
      el.classList.toggle("done", i < step);
    });
    const back = document.getElementById("wiz-back");
    const next = document.getElementById("wiz-next");
    const finish = document.getElementById("wiz-finish");
    back.disabled = step === 0;
    // Step gating: step 0 needs consent; step 3 shows Finish instead of Next.
    finish.classList.toggle("hidden", step !== 3);
    next.classList.toggle("hidden", step === 3);
    refreshNextEnabled();
    const subs = ["Read the safety notice", "Confirm your boat profile",
      "Watch your boat calibrate itself", "Review the measured results"];
    VA.setText("wiz-subtitle", subs[step]);
  }

  function refreshNextEnabled() {
    const next = document.getElementById("wiz-next");
    if (step === 0) {
      next.disabled = !document.getElementById("wiz-consent").checked;
    } else if (step === 2) {
      // Can't advance to results until calibration has finished.
      next.disabled = !calibDone;
    } else {
      next.disabled = false;
    }
  }

  document.getElementById("wiz-back").addEventListener("click", () => goStep(step - 1));
  document.getElementById("wiz-next").addEventListener("click", () => {
    if (step === 1) saveBoat();          // persist specs when leaving step 2
    goStep(step + 1);
    if (step === 3 && lastResults) renderResults(lastResults);
  });
  // wiz-finish: onboard.js also listens on this button to mark wizard complete.
  document.getElementById("wiz-finish").addEventListener("click", close);
  document.getElementById("wiz-consent").addEventListener("change", refreshNextEnabled);

  // ---- step 2: boat specs ------------------------------------------------
  function specEl(field) { return wizard.querySelector(`.spec[data-field="${field}"]`); }

  async function loadBoat() {
    let boat = null;
    try { boat = await VA.getJSON("/api/boat"); } catch (e) { /* fall back to telemetry */ }
    if (!boat && VA.last && VA.last.boat) boat = VA.last.boat;
    boat = boat || {};
    SPEC_FIELDS.forEach((f) => {
      const el = specEl(f);
      if (!el) return;
      const input = el.querySelector("input[type=range]");
      const out = el.querySelector(".spec-out");
      const v = Number(boat[f]);
      if (Number.isFinite(v)) {
        // widen the slider bounds if the value sits outside them
        if (v < Number(input.min)) input.min = v;
        if (v > Number(input.max)) input.max = v;
        input.value = v;
      }
      if (f === "max_thrust_n") {
        const lbs = (Number(input.value) / 4.448).toFixed(0);
        out.textContent = input.value + " N (~" + lbs + " lb)";
      } else {
        out.textContent = input.value;
      }
      if (f === "hull_tracking") updateHullCaption(input.value);
    });
    mountVal = boat.thruster_mount === "stern" ? "stern" : "bow";
    // Seed the motor-placement control. Offset sign carries fore/aft; fall back
    // to the legacy mount string when the new offsets are absent.
    initMotorPlace();
    if (mp) {
      mp.setBoat({ length_m: Number(boat.length_m), beam_m: Number(boat.beam_m) });
      const off = Number(boat.thruster_offset_m);
      const y = Number(boat.thruster_y_m);
      mpOffset = Number.isFinite(off) ? off
        : (mountVal === "stern" ? -0.3 : 0.3);
      mpY = Number.isFinite(y) ? y : 0;
      mp.setValue(mpOffset, mpY,
        Number.isFinite(off) ? undefined : mountVal);
      const v = mp.getValue();
      mpOffset = v.offset_m; mpY = v.y_m; mountVal = v.mount;
    }
  }

  // wire spec sliders -> live output
  SPEC_FIELDS.forEach((f) => {
    const el = specEl(f);
    if (!el) return;
    const input = el.querySelector("input[type=range]");
    const out = el.querySelector(".spec-out");
    input.addEventListener("input", () => {
      if (f === "max_thrust_n") {
        const lbs = (Number(input.value) / 4.448).toFixed(0);
        out.textContent = input.value + " N (~" + lbs + " lb)";
      } else {
        out.textContent = input.value;
      }
      if (f === "hull_tracking") updateHullCaption(input.value);
      // Rescale the motor-placement picture when length/beam change.
      if ((f === "length_m" || f === "beam_m") && mp) {
        const lEl = specEl("length_m"), bEl = specEl("beam_m");
        mp.setBoat({
          length_m: lEl ? parseFloat(lEl.querySelector("input[type=range]").value) : undefined,
          beam_m: bEl ? parseFloat(bEl.querySelector("input[type=range]").value) : undefined,
        });
      }
    });
  });

  // Thrust preset chips (lb → N). Sets the range slider + output.
  document.querySelectorAll(".thrust-preset").forEach(function (btn) {
    btn.addEventListener("click", function () {
      const newtons = Number(btn.dataset.n);
      if (!Number.isFinite(newtons)) return;
      const el = specEl("max_thrust_n");
      if (!el) return;
      const input = el.querySelector("input[type=range]");
      const out   = el.querySelector(".spec-out");
      if (!input) return;
      if (newtons < Number(input.min)) input.min = newtons;
      if (newtons > Number(input.max)) input.max = newtons;
      input.value = newtons;
      const lbs = (newtons / 4.448).toFixed(0);
      if (out) out.textContent = newtons + " N (~" + lbs + " lb)";
    });
  });

  async function saveBoat() {
    // Offsets are the source of truth; keep thruster_mount for back-compat.
    if (mp) { const v = mp.getValue(); mpOffset = v.offset_m; mpY = v.y_m; mountVal = v.mount; }
    const body = {
      thruster_mount: mountVal,
      thruster_offset_m: mpOffset,
      thruster_y_m: mpY,
    };
    SPEC_FIELDS.forEach((f) => {
      const el = specEl(f);
      if (el) body[f] = parseFloat(el.querySelector("input[type=range]").value);
    });
    const status = document.getElementById("spec-status");
    if (status) status.textContent = "saving profile…";
    try {
      await VA.postJSON("/api/boat", body);
      if (status) status.textContent = "✓ profile saved";
      VA.logLine("boat profile saved");
    } catch (e) {
      if (status) status.textContent = "save failed: " + e;
    }
  }

  // ---- step 3: calibration -----------------------------------------------
  const PHASE_LABELS = {
    idle: "Idle", straight: "Straight runs", turn: "Turning",
    coast: "Coasting", tuning: "Auto-tuning", done: "Done", error: "Error",
  };
  const PHASE_ORDER = ["straight", "turn", "coast", "tuning", "done"];

  document.getElementById("calib-start").addEventListener("click", async () => {
    // Hide error card + reset button label when retrying.
    const errCard = document.getElementById("calib-error");
    if (errCard) errCard.classList.add("hidden");
    const startBtn = document.getElementById("calib-start");
    if (startBtn) startBtn.textContent = "▶ Start calibration";
    try {
      await VA.postJSON("/api/calibrate", { mode: "quick" });
      calibRunning = true; calibDone = false;
      document.getElementById("calib-start").classList.add("hidden");
      document.getElementById("calib-cancel").classList.remove("hidden");
      VA.setText("calib-message", "Starting calibration drive…");
      VA.logLine("calibration started (quick)");
    } catch (e) {
      VA.setText("calib-message", "Failed to start: " + e);
    }
  });

  document.getElementById("calib-cancel").addEventListener("click", async () => {
    try { await VA.postJSON("/api/calibrate/cancel", {}); VA.logLine("calibration cancelled"); }
    catch (e) { /* ignore */ }
    endCalib(false);
  });

  function endCalib(finished) {
    calibRunning = false;
    calibDone = finished;
    document.getElementById("calib-start").classList.remove("hidden");
    document.getElementById("calib-cancel").classList.add("hidden");
    refreshNextEnabled();
  }

  // Live calibration progress comes from telemetry; only update while the
  // wizard is open and on (or just leaving) the calibration step.
  VA.onTelemetry(function renderCalib(t) {
    if (wizard.classList.contains("hidden")) return;
    const c = t.calibration;
    // live readouts always reflect current boat motion during calibration
    VA.setText("calib-sog", VA.fin(t.sog_knots) === null ? "—" : t.sog_knots.toFixed(2));
    VA.setText("calib-hdg", VA.fin(t.heading_deg) === null ? "—" : Math.round(t.heading_deg));
    if (!c || typeof c !== "object") return;

    const phase = c.phase || "idle";
    VA.setText("calib-phase", PHASE_LABELS[phase] || phase);
    // Show the plain message only when it's not an error-ish string
    // (raw errno/traceback → handled by the #calib-error block instead).
    const errPat = /Errno|Exception|Traceback|No such file/;
    if (c.message && phase !== "error" && !errPat.test(c.message)) {
      VA.setText("calib-message", c.message);
    }

    const prog = Number.isFinite(c.progress) ? Math.max(0, Math.min(1, c.progress)) : 0;
    const fill = document.getElementById("calib-fill");
    if (fill) fill.style.width = (prog * 100).toFixed(0) + "%";
    VA.setText("calib-pct", Math.round(prog * 100));

    // phase chips lit up to the current phase
    const idx = PHASE_ORDER.indexOf(phase);
    document.querySelectorAll("#calib-phases span").forEach((sp) => {
      const i = PHASE_ORDER.indexOf(sp.dataset.phase);
      sp.classList.toggle("active", i === idx);
      sp.classList.toggle("done", i >= 0 && idx >= 0 && i < idx);
    });

    if (c.results) lastResults = c.results;

    if (phase === "done" && c.running === false && !calibDone) {
      endCalib(true);
      VA.setText("calib-message", c.message || "Calibration complete.");
      VA.logLine("calibration complete");
      if (lastResults) renderResults(lastResults);
    } else if (phase === "error") {
      endCalib(false);
      const stage = document.getElementById("calib-stage");
      if (stage) stage.classList.add("error");
      // Show plain-language error card + hide raw message from #calib-message.
      const errCard = document.getElementById("calib-error");
      const errMsg  = document.getElementById("calib-error-msg");
      const errRaw  = document.getElementById("calib-error-raw");
      if (errCard) errCard.classList.remove("hidden");
      if (errMsg) errMsg.textContent =
        "Calibration hit a problem and stopped — the boat’s motor is stopped." +
        " Check the GPS fix and clear water, then retry.";
      if (errRaw && c.message) errRaw.textContent = c.message;
      // Relabel the start button to ↻ Retry.
      const startBtn = document.getElementById("calib-start");
      if (startBtn) startBtn.textContent = "↻ Retry calibration";
    } else if (c.running) {
      calibRunning = true;
    }
  });

  // ---- step 4: results ---------------------------------------------------
  // max_speed_mps rendered in knots (1.9438 kn/m/s); friendly labels throughout.
  const RESULT_ROWS = [
    ["max_speed_mps", "Measured top speed", "kn", 1],
    ["accel_tau_s", "Time to pick up speed", "s", 2],
    ["drag_tau_s", "Time to coast down", "s", 2],
    ["max_turn_rate_dps", "Turning speed", "°/s", 1],
    ["steering_sign", "Steering direction", "", 0],
  ];
  function renderResults(r) {
    const grid = document.getElementById("results-grid");
    if (grid) {
      grid.innerHTML = "";
      RESULT_ROWS.forEach(([key, label, unit, dec]) => {
        const v = r[key];
        const card = document.createElement("div");
        card.className = "result-card";
        const lab = document.createElement("div");
        lab.className = "result-label"; lab.textContent = label;
        const val = document.createElement("div");
        val.className = "result-val";
        let txt = "—";
        if (Number.isFinite(Number(v))) {
          if (key === "steering_sign") {
            txt = Number(v) >= 0 ? "Normal" : "Reversed";
          } else if (key === "max_speed_mps") {
            txt = (Number(v) * 1.9438).toFixed(dec);
          } else {
            txt = Number(v).toFixed(dec);
          }
        }
        val.innerHTML = `${txt}${unit ? ` <small>${unit}</small>` : ""}`;
        card.append(lab, val);
        grid.appendChild(card);
      });
    }
    // tuned gains grid
    const gains = document.getElementById("results-gains");
    if (gains) {
      gains.innerHTML = "";
      const tuned = r.tuned || {};
      const keys = Object.keys(tuned);
      if (!keys.length) {
        gains.textContent = "—";
      } else {
        const fmt = (x) => {
          const n = Number(x);
          return Number.isFinite(n) ? n.toPrecision(4).replace(/\.?0+$/, "") : String(x);
        };
        keys.forEach((k) => {
          const val = tuned[k];
          // Each job's tuned value is a gains object ({kp,ki,kd}); flatten it to
          // "kp 0.035  ki 0  kd 0.012" rather than rendering "[object Object]".
          const text = (val && typeof val === "object")
            ? Object.keys(val).map((g) => `${g} ${fmt(val[g])}`).join("   ")
            : fmt(val);
          const kEl = document.createElement("span"); kEl.className = "k"; kEl.textContent = k;
          const bEl = document.createElement("span"); bEl.className = "base"; bEl.textContent = "";
          const tEl = document.createElement("span"); tEl.className = "tuned"; tEl.textContent = text;
          gains.append(kEl, bEl, tEl);
        });
      }
    }
  }

  // ---- entry point -------------------------------------------------------
  document.getElementById("setup-open").addEventListener("click", open);
})();
