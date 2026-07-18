/* Vanchor-NG — WiFi setup card (Settings -> "Data & system").
 *
 * Only active on the Pi SD image where NetworkManager is present.
 * On dev machines / sim installs /api/system/wifi returns {available:false}
 * and the card stays hidden.
 *
 * Flow:
 *  1. On module load: GET /api/system/wifi; if available, unhide card and fill status.
 *  2. Scan button: GET /api/system/wifi/scan; render network list.
 *  3. Network item click: prompt() for password; POST /api/system/wifi/join;
 *     display note + countdown hint.
 *
 * Uses VA.getJSON / VA.postJSON (core.js).
 */
"use strict";

(function () {
  if (!window.VA) return;

  const $ = (id) => document.getElementById(id);

  // ---- helpers ------------------------------------------------------------

  function setText(id, val) {
    const el = $(id);
    if (el) el.textContent = val != null ? String(val) : "—";
  }

  function setStatus(msg, kind) {
    const el = $("wifi-status");
    if (!el) return;
    el.textContent = msg || "";
    el.className = "hint" + (kind ? " " + kind : "");
  }

  function renderBadge(mode) {
    const el = $("wifi-state");
    if (!el) return;
    el.textContent = mode || "";
    el.className = "badge" + (mode === "hotspot" ? " badge-warn" : mode === "wifi" ? " badge-ok" : "");
  }

  // ---- refresh / status ---------------------------------------------------

  async function refresh() {
    const card = $("wifi-card");
    if (!card) return;
    let data;
    try {
      data = await VA.getJSON("/api/system/wifi");
    } catch (e) {
      return; // silently skip on connection error
    }
    if (!data || !data.available) {
      // nmcli not present -- hide card (default: hidden)
      return;
    }
    card.classList.remove("hidden");
    renderBadge(data.mode);
    setText("wifi-mode", data.mode || "—");
    setText("wifi-ssid", data.ssid || (data.mode === "offline" ? "(none)" : "—"));
    setText("wifi-ip", data.ip || "—");

    if (data.last_join) {
      const lj = data.last_join;
      const ago = lj.finished_at ? Math.round((Date.now() / 1000 - lj.finished_at) / 60) : null;
      const agoStr = ago != null ? ` (${ago} min ago)` : "";
      if (lj.ok) {
        setStatus("Last join: " + lj.ssid + " succeeded" + agoStr, "success");
      } else {
        setStatus("Last join: " + lj.ssid + " failed" + agoStr + (lj.error ? " — " + lj.error : ""), "warn");
      }
    }
  }

  // ---- scan ---------------------------------------------------------------

  async function doScan() {
    const btn = $("wifi-scan");
    const list = $("wifi-list");
    if (!list) return;
    if (btn) btn.disabled = true;
    setStatus("Scanning…");
    list.innerHTML = "";

    let data;
    try {
      data = await VA.getJSON("/api/system/wifi/scan");
    } catch (e) {
      setStatus("Scan failed: " + e.message, "warn");
      if (btn) btn.disabled = false;
      return;
    }

    if (!data || !data.available) {
      setStatus("WiFi not available on this device.", "warn");
      if (btn) btn.disabled = false;
      return;
    }

    if (!data.networks || !data.networks.length) {
      setStatus("No networks found.");
      if (btn) btn.disabled = false;
      return;
    }

    setStatus("");
    data.networks.forEach(function (net) {
      const li = document.createElement("li");
      const lock = net.security ? " 🔒" : "";
      li.textContent = net.ssid + " — " + net.signal + "%" + lock + (net.in_use ? " (connected)" : "");
      li.style.cursor = "pointer";
      li.addEventListener("click", function () {
        doJoin(net.ssid, !!net.security);
      });
      list.appendChild(li);
    });

    if (btn) btn.disabled = false;
  }

  // ---- join ---------------------------------------------------------------

  async function doJoin(ssid, hasPassword) {
    var psk = "";
    if (hasPassword) {
      psk = window.prompt("Password for \"" + ssid + "\" (leave blank for open network):", "") || "";
      if (psk === null) return; // cancelled
    }

    setStatus("Joining " + ssid + "…");

    let data;
    try {
      data = await VA.postJSON("/api/system/wifi/join", { ssid: ssid, psk: psk });
    } catch (e) {
      setStatus("Join request failed: " + e.message, "warn");
      return;
    }

    if (data && data.ok === false) {
      setStatus("Could not join: " + (data.error || "unknown error"), "warn");
      return;
    }

    // Success — show countdown hint
    setStatus(
      (data.note || "Joining in background.") +
      " The setup hotspot will drop — reconnect your phone/laptop to \"" +
      ssid + "\" then open http://vanchor.local:8000",
      "info"
    );
    // Refresh status after ~60 s to pick up the join result
    setTimeout(refresh, 65000);
  }

  // ---- wire up ------------------------------------------------------------

  window.addEventListener("DOMContentLoaded", function () {
    var scanBtn = $("wifi-scan");
    if (scanBtn) scanBtn.addEventListener("click", doScan);
    refresh();
  });

  // Expose for external refresh (e.g. from settings panel open event)
  VA.wifi = { refresh: refresh };
})();
