/* Vanchor-NG — helm-Pico firmware flasher (Devices → Motor → Helm firmware).
 *
 * The split of labour mirrors the app-update path: the PHONE (which has
 * internet over cellular; the boat usually has none) fetches the UF2 from the
 * vanchor-pcb GitHub releases, then uploads it to the boat, and the boat
 * flashes the USB-connected Pico with picotool — no BOOTSEL button, no
 * computer, no toolchain. Releases are tagged fw-v*; assets are
 * vanchor-helm-<board>.uf2. UF2s are small (~0.5 MB) so a single POST is fine
 * (no chunking needed, unlike the app bundles).
 */
"use strict";

(function () {
  const VA = window.VA;
  const RELEASES_URL =
    "https://api.github.com/repos/AlexAsplund/vanchor-pcb/releases?per_page=10";

  const $ = (id) => document.getElementById(id);
  const setStatus = (msg) => { const el = $("pico-fw-status"); if (el) el.textContent = msg; };

  let _latest = null;   // { tag, asset: {name, browser_download_url, size} }

  function assetForBoard(release, board) {
    const want = "vanchor-helm-" + board + ".uf2";
    return (release.assets || []).find((a) => a && a.name === want) || null;
  }

  function latestFirmware(releases, board) {
    for (const r of releases) {
      if (!r || !r.tag_name || !r.tag_name.startsWith("fw-v")) continue;
      const asset = assetForBoard(r, board);
      if (asset) return { tag: r.tag_name, asset };
    }
    return null;
  }

  async function doCheck() {
    const board = $("pico-fw-board") ? $("pico-fw-board").value : "pico2";
    setStatus("");
    VA.setText("pico-fw-latest", "Checking…");
    try {
      const resp = await fetch(RELEASES_URL, { headers: { Accept: "application/vnd.github+json" } });
      if (!resp.ok) throw new Error("GitHub API " + resp.status);
      _latest = latestFirmware(await resp.json(), board);
      if (!_latest) {
        VA.setText("pico-fw-latest", "No firmware release found for this board.");
        return;
      }
      VA.setText("pico-fw-latest",
        "Latest: " + _latest.tag + " (" + Math.round(_latest.asset.size / 1024) + " kB)");
      const btn = $("pico-fw-flash");
      if (btn) btn.style.display = "";
    } catch (err) {
      _latest = null;
      VA.setText("pico-fw-latest",
        "Check failed: " + err.message + " — is this device online?");
    }
  }

  async function uploadAndFlash(blob, label) {
    setStatus("Uploading " + label + " to the boat…");
    const fd = new FormData();
    fd.append("file", blob, label);
    let resp, data;
    try {
      resp = await fetch("/api/hw/pico/flash", { method: "POST", body: fd });
      data = await resp.json();
    } catch (err) {
      setStatus("Upload failed: " + err.message);
      return;
    }
    if (resp.status === 409) {
      setStatus("Refused: " + (data.detail || "boat is underway — stop first."));
      return;
    }
    if (!resp.ok || !data.ok) {
      setStatus("Flash failed: " + (data.error || data.output || "unknown error"));
      return;
    }
    setStatus("Flashed OK — the Pico restarts into the new firmware. " +
      (data.output ? "" : "") + "If the motor card shows an error chip for a " +
      "few seconds, that is the USB re-plug and it heals itself.");
    if (VA.logAlert) VA.logAlert("info", "Helm firmware flashed");
  }

  async function doFlashLatest() {
    if (!_latest) return;
    const btn = $("pico-fw-flash");
    if (btn) btn.disabled = true;
    try {
      setStatus("Downloading " + _latest.asset.name + " with this device's internet…");
      const resp = await fetch(_latest.asset.browser_download_url);
      if (!resp.ok) throw new Error("download " + resp.status);
      const blob = await resp.blob();
      await uploadAndFlash(blob, _latest.asset.name);
    } catch (err) {
      setStatus("Download failed: " + err.message);
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  async function doFlashFile() {
    const input = $("pico-fw-file");
    const f = input && input.files && input.files[0];
    if (!f) { setStatus("Pick a .uf2 file first."); return; }
    const btn = $("pico-fw-flash-file");
    if (btn) btn.disabled = true;
    try { await uploadAndFlash(f, f.name || "firmware.uf2"); }
    finally { if (btn) btn.disabled = false; }
  }

  function init() {
    if (!$("pico-fw-card")) return;
    const bind = (id, fn) => { const el = $(id); if (el) el.addEventListener("click", fn); };
    bind("pico-fw-check", doCheck);
    bind("pico-fw-flash", doFlashLatest);
    bind("pico-fw-flash-file", doFlashFile);
    // Hide the whole card when this install cannot flash (no picotool).
    fetch("/api/hw/pico/flash").then((r) => r.json()).then((d) => {
      if (!d.picotool) {
        const card = $("pico-fw-card");
        if (card) {
          card.querySelectorAll("button").forEach((b) => { b.disabled = true; });
          setStatus("This install has no flasher (picotool) — update the " +
            "boat to the latest SD image to enable it.");
        }
      }
    }).catch(() => {});
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else init();
})();
