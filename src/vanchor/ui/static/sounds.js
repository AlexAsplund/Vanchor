/* Vanchor-NG — sound feedback (Web Audio, fully synthesized).
 *
 * Short synthesized cues for the things worth hearing with your eyes on the
 * water: safety ALARMS in three severities (low = calm double beep, medium =
 * two-tone warble, high = fast siren) plus a sonar-ping exception for depth
 * warnings, NOTIFICATIONS (chime), MODE changes
 * (a distinct little motif per control mode, so "the boat just went into
 * anchor hold" is recognizable without looking), WAYPOINTS (a ding per mark
 * reached + a fanfare when the route completes) and UI button clicks (subtle
 * tick, heavier on STOP).
 *
 * Everything is generated with the Web Audio API — no audio files, so the PWA
 * stays tiny and fully offline. The AudioContext is created lazily on the
 * first user gesture (browser autoplay policy); telemetry-driven sounds stay
 * silent until then.
 *
 * Customizable in Settings → Display → Sound: master enable, volume, and a
 * per-category switch (alarm / notify / mode / nav / ui) each with a preview
 * button. Persisted in localStorage ("vanchor-sounds").
 *
 * Other modules: VA.sound.play(name) (respects toggles) — e.g. "alarm",
 * "notify", "nav.waypoint" — and VA.sound.preview(cat) (ignores toggles).
 */
"use strict";

(function () {
  const VA = (window.VA = window.VA || {});
  const supported = typeof window.AudioContext === "function" ||
    typeof window.webkitAudioContext === "function";

  // ---- persisted config -----------------------------------------------------
  const KEY = "vanchor-sounds";
  const cfg = {
    master: true,
    volume: 60,                                          // 0..100
    cats: { alarm: true, notify: true, mode: true, nav: true, ui: true },
  };
  try {
    const raw = localStorage.getItem(KEY);
    if (raw) {
      const o = JSON.parse(raw);
      if (o && typeof o === "object") {
        if (typeof o.master === "boolean") cfg.master = o.master;
        if (Number.isFinite(o.volume)) cfg.volume = Math.max(0, Math.min(100, o.volume));
        if (o.cats && typeof o.cats === "object") {
          for (const k of Object.keys(cfg.cats)) {
            if (typeof o.cats[k] === "boolean") cfg.cats[k] = o.cats[k];
          }
        }
      }
    }
  } catch (e) { /* ignore corrupt storage */ }
  function save() {
    try { localStorage.setItem(KEY, JSON.stringify(cfg)); } catch (e) { /* ignore */ }
  }

  // ---- lazy AudioContext (autoplay policy: needs a user gesture) ------------
  let ctx = null, master = null;
  function ensureCtx() {
    if (!supported) return null;
    if (!ctx) {
      const AC = window.AudioContext || window.webkitAudioContext;
      try { ctx = new AC(); } catch (e) { return null; }
      master = ctx.createGain();
      master.connect(ctx.destination);
      applyVolume();
    }
    if (ctx.state === "suspended") { try { ctx.resume(); } catch (e) { /* ignore */ } }
    return ctx;
  }
  function applyVolume() {
    // Perceptual-ish curve: gain = (volume%)^2 so the slider's low end is usable.
    if (master) master.gain.value = Math.pow(cfg.volume / 100, 2);
  }
  // Arm the context on the first gesture so telemetry sounds can play later.
  document.addEventListener("pointerdown", () => { if (cfg.master) ensureCtx(); }, true);

  // ---- tiny synth ------------------------------------------------------------
  // A sound is a sequence of notes {f: Hz, d: seconds, w: waveform, g: gain,
  // e: envelope}. f=0 is a rest. The default envelope is attack-hold-release
  // (short ramps avoid clicks); e:"ping" is attack + exponential decay — a
  // sonar-like ping (used by the depth warning).
  const N = (f, d, w, g, e) => ({ f, d, w: w || "sine", g: g == null ? 0.5 : g, e });

  function playSeq(notes) {
    const c = ensureCtx();
    if (!c || !master) return;
    let t = c.currentTime + 0.02;
    for (const n of notes) {
      if (n.f > 0) {
        const o = c.createOscillator(), g = c.createGain();
        o.type = n.w;
        o.frequency.value = n.f;
        const peak = Math.max(0.001, n.g);
        g.gain.setValueAtTime(0.0001, t);
        if (n.e === "ping") {
          g.gain.linearRampToValueAtTime(peak, t + 0.006);
          g.gain.exponentialRampToValueAtTime(0.0001, t + n.d);
        } else {
          const hold = Math.max(0.012, n.d - 0.03);
          g.gain.linearRampToValueAtTime(peak, t + 0.008);
          g.gain.setValueAtTime(peak, t + hold);
          g.gain.linearRampToValueAtTime(0.0001, t + n.d);
        }
        o.connect(g);
        g.connect(master);
        o.start(t);
        o.stop(t + n.d + 0.02);
      }
      t += n.d + 0.025;
    }
  }

  // ---- the sound set ----------------------------------------------------------
  const SOUNDS = {
    // Safety alarms by severity — three distinct, escalating patterns:
    //   low    = calm double beep (attention, no urgency)
    //   medium = two-tone warble (act soon)
    //   high   = fast aggressive siren (act NOW)
    // plus the depth exception: a sonar-style ping with a quieter echo —
    // unmistakably "depth", noticeable without being grating.
    "alarm.low": { cat: "alarm", notes: [N(587, .12, "triangle", .4), N(587, .18, "triangle", .35)] },
    "alarm.medium": { cat: "alarm", notes: [N(880, .12, "square", .5), N(660, .12, "square", .5),
                                            N(880, .12, "square", .5), N(660, .12, "square", .5),
                                            N(880, .20, "square", .5)] },
    "alarm.high": { cat: "alarm", notes: [N(988, .08, "square", .55), N(740, .08, "square", .55),
                                          N(988, .08, "square", .55), N(740, .08, "square", .55),
                                          N(988, .08, "square", .55), N(740, .08, "square", .55),
                                          N(1175, .25, "square", .55)] },
    "alarm.depth": { cat: "alarm", notes: [N(1175, .5, "sine", .45, "ping"),
                                           N(1175, .38, "sine", .16, "ping")] },
    // Notifications: warn = rising chime, info = single soft note.
    "notify": { cat: "notify", notes: [N(660, .09), N(880, .16)] },
    "notify.info": { cat: "notify", notes: [N(740, .12, "sine", .3)] },
    // Navigation: waypoint reached ding + route-complete fanfare.
    "nav.waypoint": { cat: "nav", notes: [N(1047, .08, "sine", .45), N(1319, .13, "sine", .4)] },
    "nav.complete": { cat: "nav", notes: [N(523, .09), N(659, .09), N(784, .09), N(1047, .22)] },
    // UI: subtle tick per press, heavier on STOP/destructive.
    "ui.tap": { cat: "ui", notes: [N(1500, .025, "sine", .12)] },
    "ui.heavy": { cat: "ui", notes: [N(220, .09, "square", .3)] },
  };

  // One short motif per control mode, so each engagement is recognizable by
  // ear. Families share a shape: anchors descend low, underway modes ascend.
  const MODE_MOTIFS = {
    manual: [N(440, .14)],
    anchor_hold: [N(392, .09), N(262, .18)],
    anchor_ml: [N(392, .09), N(294, .08), N(262, .16)],
    anchor_leif: [N(392, .09), N(330, .08), N(262, .16)],
    heading_hold: [N(587, .09), N(587, .13)],
    waypoint: [N(523, .08), N(659, .08), N(784, .14)],
    work_area: [N(523, .08), N(784, .08), N(659, .13)],
    follow_apb: [N(587, .08), N(698, .14)],
    drift: [N(494, .12), N(440, .16)],
    contour_follow: [N(440, .08), N(554, .08), N(440, .13)],
    orbit: [N(659, .08), N(523, .08), N(659, .13)],
    trolling: [N(494, .08), N(587, .08), N(494, .13)],
  };

  function play(name) {
    if (name === "alarm") name = "alarm.medium";   // legacy alias
    const s = SOUNDS[name];
    if (!s || !supported || !cfg.master || !cfg.cats[s.cat]) return;
    playSeq(s.notes);
  }
  function playMode(mode) {
    if (!supported || !cfg.master || !cfg.cats.mode) return;
    playSeq(MODE_MOTIFS[mode] || MODE_MOTIFS.manual);
  }
  // Previews ignore the enable switches (you're choosing what to enable) but
  // still go through the master volume.
  function preview(cat) {
    if (!supported) return;
    if (cat === "alarm" || cat === "alarm-high") playSeq(SOUNDS["alarm.high"].notes);
    else if (cat === "alarm-medium") playSeq(SOUNDS["alarm.medium"].notes);
    else if (cat === "alarm-low") playSeq(SOUNDS["alarm.low"].notes);
    else if (cat === "alarm-depth") playSeq(SOUNDS["alarm.depth"].notes);
    else if (cat === "notify") playSeq(SOUNDS["notify"].notes);
    else if (cat === "mode") playSeq(MODE_MOTIFS.anchor_hold);
    else if (cat === "nav") playSeq(SOUNDS["nav.waypoint"].notes);
    else if (cat === "ui") playSeq(SOUNDS["ui.tap"].notes);
  }

  // ---- silence: suppress alarm sounds for `ms` milliseconds ---------------
  // Client-only: sets a timestamp until which alarm plays are skipped.
  // Used by the anchor-alarm SILENCE button (2-minute snooze).
  let _silencedUntil = 0;
  function silence(ms) {
    _silencedUntil = Date.now() + ms;
  }
  // Wrap play so every alarm path respects the silence window (preview and
  // UI ticks call playSeq directly and are unaffected).
  const _origPlay = play;
  play = function (name) {
    if (Date.now() < _silencedUntil) return;
    _origPlay(name);
  };

  VA.sound = { play, playMode, preview, silence, isSupported: () => supported };

  // ---- UI click ticks ---------------------------------------------------------
  // Mirrors haptics.js: one capture-phase pointerdown listener over button-like
  // controls; disabled controls stay silent.
  const BUTTONISH =
    'button, [role="button"], .mode-btn, .cm-tile, label.switch, .seg > *, a.btn';
  const HEAVY = '[data-mode="stop"], .danger, .btn-stop';
  document.addEventListener("pointerdown", (e) => {
    if (!supported || !cfg.master || !cfg.cats.ui) return;
    const el = e.target && e.target.closest ? e.target.closest(BUTTONISH) : null;
    if (!el || el.disabled || el.getAttribute("aria-disabled") === "true") return;
    play(el.matches(HEAVY) ? "ui.heavy" : "ui.tap");
  }, true);

  // ---- telemetry watcher: mode changes, waypoints reached, route complete ----
  let prevMode = null;          // null until the first frame (no sound on load)
  let prevActive = null, prevCount = null, prevComplete = false;
  VA.onTelemetry((t) => {
    if (!t || typeof t.mode !== "string") return;
    if (prevMode !== null && t.mode !== prevMode) playMode(t.mode);
    prevMode = t.mode;

    // Route completion (rising edge) outranks the per-waypoint ding.
    const complete = t.route_complete === true;
    if (complete && !prevComplete) play("nav.complete");
    prevComplete = complete;

    // Waypoint reached: active_waypoint moved while navigating the SAME route
    // (waypoint count unchanged — a live edit that inserts/deletes marks moves
    // the index without a mark being reached). Decimated frames omit the
    // waypoints array; carry the last-known count across them.
    const count = Array.isArray(t.waypoints) ? t.waypoints.length : prevCount;
    if (t.mode === "waypoint" && Number.isInteger(t.active_waypoint)) {
      if (prevActive !== null && count === prevCount &&
          t.active_waypoint !== prevActive && !complete) {
        play("nav.waypoint");
      }
      prevActive = t.active_waypoint;
    } else {
      prevActive = null;
    }
    prevCount = count;
  });

  // ---- settings card (Display → Sound) ----------------------------------------
  const masterBox = document.getElementById("snd-master");
  const volSlider = document.getElementById("snd-volume");
  const volOut = document.getElementById("snd-volume-val");
  if (masterBox) {
    masterBox.checked = supported && cfg.master;
    masterBox.disabled = !supported;
    masterBox.addEventListener("change", () => {
      cfg.master = masterBox.checked;
      save();
      if (cfg.master) { ensureCtx(); playSeq(SOUNDS["notify.info"].notes); }
    });
  }
  if (volSlider) {
    volSlider.value = String(cfg.volume);
    if (volOut) volOut.textContent = String(cfg.volume);
    volSlider.addEventListener("input", () => {
      cfg.volume = parseFloat(volSlider.value) || 0;
      if (volOut) volOut.textContent = String(cfg.volume);
      applyVolume();
      save();
    });
    // A confirmation blip when the user releases the slider, at the new volume.
    volSlider.addEventListener("change", () => { if (cfg.master) playSeq(SOUNDS["notify.info"].notes); });
  }
  document.querySelectorAll(".snd-cat").forEach((box) => {
    const cat = box.dataset.cat;
    if (!(cat in cfg.cats)) return;
    box.checked = cfg.cats[cat];
    box.disabled = !supported;
    box.addEventListener("change", () => { cfg.cats[cat] = box.checked; save(); });
  });
  document.querySelectorAll(".snd-test").forEach((btn) => {
    btn.disabled = !supported;
    btn.addEventListener("click", () => preview(btn.dataset.cat));
  });
  const hint = document.getElementById("sound-hint");
  if (hint && !supported) hint.textContent = "Web Audio is not available in this browser — sounds are off.";
})();
