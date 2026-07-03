/* Vanchor-NG — debug recorder + replay UI.
 * Start/stop a recording, list sessions (download / replay), and show replay
 * state. Talks to /api/debug/*. */
"use strict";
(function () {
  const $ = (id) => document.getElementById(id);
  const recBtn = $("debug-rec");
  if (!recBtn) return;
  let recording = null;   // null = not yet rendered (forces first-frame write)

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

  // --- Opt-in "upload last session on WiFi" (#48) ------------------------
  // A DELIBERATE user action: uploads never happen automatically. The button
  // is injected here (no index.html edit) into the debug card's button row.
  // First click walks the user through opting in + setting a destination URL
  // (persisted in the prefs KV store), then POSTs the latest session.
  const uploadBtn = document.createElement("button");
  uploadBtn.id = "session-upload";
  uploadBtn.className = "btn-ghost";
  uploadBtn.textContent = "⤴ Upload last session";
  uploadBtn.title =
    "Opt-in: package the most recent session and send it to your configured URL";
  (recBtn.parentNode || $("debug-card")).appendChild(uploadBtn);
  const uploadInfo = document.createElement("div");
  uploadInfo.id = "session-upload-info";
  uploadInfo.className = "hint";
  (recBtn.parentNode || $("debug-card")).appendChild(uploadInfo);

  async function ensureOptIn() {
    // Read the current opt-in + destination straight from the server prefs so a
    // fresh device reflects the durable choice. Returns true when good to go.
    let prefs = {};
    try { prefs = await VA.getJSON("/api/prefs"); } catch (e) { prefs = {}; }
    let url = String(prefs.session_upload_url || "");
    const enabled = !!prefs.session_upload_enabled;
    if (!enabled || !url) {
      const entered = window.prompt(
        "Upload the latest session to a destination URL?\n" +
        "This is opt-in and only happens when you click Upload.\n\n" +
        "Enter the HTTPS endpoint that receives the zip (blank to cancel):",
        url,
      );
      if (!entered) return false;
      url = entered.trim();
      if (!url) return false;
      // Persist the opt-in flag + URL so it is a durable, deliberate choice.
      await fetch("/api/prefs", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          session_upload_enabled: true,
          session_upload_url: url,
        }),
      });
    }
    return true;
  }

  uploadBtn.addEventListener("click", async () => {
    if (!(await ensureOptIn())) {
      uploadInfo.textContent = "Upload cancelled — opt-in required.";
      return;
    }
    uploadBtn.disabled = true;
    uploadInfo.textContent = "Uploading latest session…";
    let r;
    try {
      r = await VA.postJSON("/api/session/upload", {});
    } catch (e) {
      uploadInfo.textContent = "Upload failed: " + e;
      uploadBtn.disabled = false;
      return;
    }
    uploadInfo.textContent = r && r.ok
      ? "Uploaded " + (r.filename || "session") +
        " (" + Math.round((r.bytes || 0) / 1024) + " KB)"
      : "Upload failed: " + ((r && r.error) || "unknown error");
    uploadBtn.disabled = false;
  });
  const stopReplay = () => VA.postJSON("/api/debug/replay/stop", {});
  $("replay-stop").addEventListener("click", stopReplay);
  $("replay-ind-stop").addEventListener("click", stopReplay);

  // Reflect live recording + replay state from telemetry.
  VA.onTelemetry((t) => {
    const dbg = t.debug || {};
    const nowRecording = !!dbg.recording;
    // Only touch the button when the recording state actually changes.
    if (nowRecording !== recording) {
      recording = nowRecording;
      recBtn.textContent = recording ? "■ Stop recording" : "● Start recording";
      recBtn.classList.toggle("btn-stop", recording);
    }
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
