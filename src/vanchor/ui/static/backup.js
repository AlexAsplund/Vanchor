/* Vanchor-NG — Backup & restore (Settings → "Backup & restore").
 *
 * Two operations, both talking to the matching backend:
 *
 *  - DOWNLOAD: collect every localStorage key starting with "vanchor-" into a
 *    {client} map, POST it to /api/backup, and stream the returned ZIP file to a
 *    browser download. We use fetch() directly (not VA.postJSON) because the
 *    response is a binary blob, not JSON. The filename comes from the
 *    Content-Disposition header when present, else "vanchor-backup-<date>.zip".
 *
 *  - RESTORE: POST a user-picked .zip as multipart/form-data (field "file") to
 *    /api/restore. On success the backend returns the decoded client map; we
 *    write each key/value back into localStorage, surface any warnings + the
 *    backup's app/schema version + created_at, then reload so the restored data
 *    takes effect. Confirmed first — it overwrites current data.
 *
 * Contract (must match the backend):
 *   POST /api/backup   body {client:{<localStorage map>}}
 *     -> ZIP file (application/zip, Content-Disposition: attachment)
 *   POST /api/restore  multipart/form-data, field "file"
 *     -> { ok, schema_version, app_version, created_at, restored:[...],
 *          client:{...}, warnings:[...], restart_required }
 */
"use strict";

(function () {
  const $ = (id) => document.getElementById(id);
  const card = $("backup-card");
  if (!card || !window.VA) return;

  const PREFIX = "vanchor-";

  // ---- helpers ----------------------------------------------------------

  function setStatus(el, msg, kind) {
    if (!el) return;
    el.textContent = msg || "";
    el.className = "hint" + (kind ? " " + kind : "");
  }

  function dlStatus(msg, kind) { setStatus($("backup-dl-status"), msg, kind); }
  function rsStatus(msg, kind) { setStatus($("backup-rs-status"), msg, kind); }

  // Collect every "vanchor-" localStorage key into a plain {key: value} map.
  function collectClient() {
    const out = {};
    try {
      for (let i = 0; i < localStorage.length; i++) {
        const k = localStorage.key(i);
        if (k && k.indexOf(PREFIX) === 0) {
          const v = localStorage.getItem(k);
          if (v != null) out[k] = v;
        }
      }
    } catch (e) { /* private mode / disabled storage */ }
    return out;
  }

  function dateStamp() {
    const d = new Date();
    const p = (n) => String(n).padStart(2, "0");
    return d.getFullYear() + "-" + p(d.getMonth() + 1) + "-" + p(d.getDate());
  }

  // Pull a filename out of a Content-Disposition header, if any.
  function filenameFromDisposition(cd) {
    if (!cd) return null;
    // RFC 5987 filename*=UTF-8''... first, then plain filename="...".
    let m = /filename\*=(?:UTF-8'')?["']?([^;"']+)/i.exec(cd);
    if (m && m[1]) { try { return decodeURIComponent(m[1]); } catch (e) { return m[1]; } }
    m = /filename=["']?([^;"']+)/i.exec(cd);
    return m && m[1] ? m[1] : null;
  }

  function triggerDownload(blob, filename) {
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    // Revoke after the click has had a chance to start the download.
    setTimeout(() => URL.revokeObjectURL(url), 4000);
  }

  // ---- download ---------------------------------------------------------

  async function downloadBackup() {
    const btn = $("backup-download");
    if (btn) btn.disabled = true;
    dlStatus("Building backup…", "busy");
    try {
      const client = collectClient();
      const resp = await fetch("/api/backup", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ client }),
      });
      if (!resp.ok) {
        let detail = "";
        try { const j = await resp.json(); detail = j && (j.detail || j.error) || ""; } catch (e) {}
        throw new Error(detail || ("HTTP " + resp.status));
      }
      const blob = await resp.blob();
      const name =
        filenameFromDisposition(resp.headers.get("Content-Disposition")) ||
        ("vanchor-backup-" + dateStamp() + ".zip");
      triggerDownload(blob, name);
      const kb = Math.max(1, Math.round(blob.size / 1024));
      dlStatus("Saved " + name + " (" + kb + " KB).", "ok");
    } catch (e) {
      dlStatus("Backup failed: " + (e && e.message ? e.message : e), "err");
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  // ---- restore ----------------------------------------------------------

  function restoreSummary(j) {
    const bits = [];
    if (j.app_version) bits.push("app " + j.app_version);
    if (j.schema_version != null) bits.push("schema " + j.schema_version);
    if (j.created_at) bits.push("from " + j.created_at);
    return bits.length ? " (" + bits.join(" · ") + ")" : "";
  }

  async function restoreBackup() {
    const input = $("backup-file");
    const file = input && input.files && input.files[0];
    if (!file) { rsStatus("Pick a .zip backup file first.", "err"); return; }
    if (!window.confirm(
      "Restore from \"" + file.name + "\"?\n\n" +
      "This OVERWRITES the current settings and on-device data, then reloads the app."
    )) return;

    const btn = $("backup-restore");
    if (btn) btn.disabled = true;
    rsStatus("Restoring…", "busy");
    try {
      const fd = new FormData();
      fd.append("file", file, file.name);
      const resp = await fetch("/api/restore", { method: "POST", body: fd });
      let j = null;
      try { j = await resp.json(); } catch (e) {}
      if (!resp.ok || !j || j.ok === false) {
        const detail = (j && (j.detail || j.error)) || ("HTTP " + resp.status);
        throw new Error(detail);
      }

      // Write the restored client map back into localStorage.
      const client = j.client || {};
      let n = 0;
      try {
        Object.keys(client).forEach((k) => {
          // Enforce the app's namespace: only ever write keys the app owns, so a
          // tampered/foreign backup can't set arbitrary localStorage entries.
          if (!String(k).startsWith("vanchor-")) return;
          const v = client[k];
          localStorage.setItem(k, v == null ? "" : String(v));
          n++;
        });
      } catch (e) { /* storage may reject; keep going to the reload */ }

      const warnings = Array.isArray(j.warnings) ? j.warnings : [];
      let msg = "Restored " + n + " setting" + (n === 1 ? "" : "s") + restoreSummary(j) + " — reloading…";
      if (warnings.length) msg += " Warnings: " + warnings.join("; ") + ".";
      if (j.restart_required) msg += " The app/server may need a restart to fully apply.";
      rsStatus(msg, warnings.length ? "busy" : "ok");

      setTimeout(() => { window.location.reload(); }, warnings.length || j.restart_required ? 2600 : 1400);
    } catch (e) {
      rsStatus("Restore failed: " + (e && e.message ? e.message : e), "err");
      if (btn) btn.disabled = false;
    }
  }

  // ---- wiring -----------------------------------------------------------

  const dlBtn = $("backup-download");
  if (dlBtn) dlBtn.addEventListener("click", downloadBackup);

  const rsBtn = $("backup-restore");
  if (rsBtn) rsBtn.addEventListener("click", restoreBackup);

  // Clear stale status when a new file is chosen.
  const fileInput = $("backup-file");
  if (fileInput) fileInput.addEventListener("change", () => rsStatus(""));
})();
