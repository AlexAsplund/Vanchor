/* Vanchor-NG — debug recorder + replay UI.
 * Start/stop a recording, list sessions (download / replay), and show replay
 * state. Talks to /api/debug/*. */
"use strict";
(function () {
  const $ = (id) => document.getElementById(id);
  const recBtn = $("debug-rec");
  if (!recBtn) return;
  let recording = false;

  async function refresh() {
    let d;
    try { d = await VA.getJSON("/api/debug/sessions"); } catch (e) { return; }
    const list = $("debug-sessions");
    if (list) {
      list.innerHTML = "";
      (d.sessions || []).forEach((s) => {
        const kb = (s.bytes / 1024).toFixed(0);
        const li = document.createElement("li");
        // Session name/file arrive from an unauthenticated POST — escape both
        // before interpolating into innerHTML (text and attribute contexts).
        const nameE = VA.escapeHtml(s.name);
        const fileE = VA.escapeHtml(s.file);
        li.innerHTML =
          `<span class="ds-name" title="${nameE}">${nameE}</span>` +
          `<span class="ds-size">${kb} KB</span>` +
          `<a class="ds-act" href="/api/debug/download?file=${encodeURIComponent(s.file)}" download>⬇</a>` +
          `<button class="ds-act ds-play" data-file="${fileE}" title="Replay">▶</button>`;
        list.appendChild(li);
      });
      list.querySelectorAll(".ds-play").forEach((b) =>
        b.addEventListener("click", () => VA.postJSON("/api/debug/replay", { file: b.dataset.file })));
    }
  }

  recBtn.addEventListener("click", async () => {
    if (!recording) {
      await VA.postJSON("/api/debug/start", {});
    } else {
      await VA.postJSON("/api/debug/stop", {});
      setTimeout(refresh, 200);
    }
  });
  $("debug-refresh").addEventListener("click", refresh);
  const stopReplay = () => VA.postJSON("/api/debug/replay/stop", {});
  $("replay-stop").addEventListener("click", stopReplay);
  $("replay-ind-stop").addEventListener("click", stopReplay);

  // Reflect live recording + replay state from telemetry.
  VA.onTelemetry((t) => {
    const dbg = t.debug || {};
    recording = !!dbg.recording;
    recBtn.textContent = recording ? "■ Stop recording" : "● Start recording";
    recBtn.classList.toggle("btn-stop", recording);
    const badge = $("debug-state");
    if (badge) badge.textContent = recording ? "● REC" : "";
    const info = $("debug-rec-info");
    if (info) {
      info.textContent = recording
        ? "Recording " + (dbg.name || "") + " — " +
          Object.entries(dbg.counts || {}).map(([k, v]) => `${k}:${v}`).join(" ")
        : "";
    }
    const rep = t.replay || {};
    const ind = $("replay-indicator");
    const bar = $("replay-bar");
    if (rep.active) {
      const pct = Math.round((rep.progress || 0) * 100) + "%";
      if (ind) { ind.classList.remove("hidden"); VA.setText("replay-ind-pct", pct); }
      if (bar) { bar.classList.remove("hidden"); VA.setText("replay-name", rep.name || ""); VA.setText("replay-pct", pct); }
    } else {
      if (ind) ind.classList.add("hidden");
      if (bar) bar.classList.add("hidden");
    }
  });

  refresh();
})();
