/* Vanchor-NG — multi-client roles banner (#24).
 *
 * The server designates one connected client the HELM; the rest are OBSERVERS.
 * When THIS client is an observer, show a small banner:
 *   "Observing — another helm is connected · [Take helm]"
 * with a Take Helm button that claims the helm cooperatively (single-user boat,
 * no auth). When this client IS the helm and others are connected, show a quiet
 * "N connected" hint instead. Hidden entirely when helm + alone.
 *
 * SAFETY FLOOR: this module never touches the STOP control. STOP stays fully
 * functional for observers — the server enforces role gating but always honours
 * `stop` from anyone. Other controls are only dimmed (a hint), never disabled;
 * the server is the authority.
 *
 * Self-contained + inline-styled (like core.js's safety banners) so it renders
 * even if the cached CSS is stale.
 */
"use strict";

(function () {
  const BANNER_ID = "role-banner";

  function el() {
    let node = document.getElementById(BANNER_ID);
    if (node) return node;
    node = document.createElement("div");
    node.id = BANNER_ID;
    node.setAttribute("role", "status");
    node.setAttribute("aria-live", "polite");
    // z-index below the critical STOP banner (100000) so a STOP-not-confirmed
    // alert always wins the top slot; offset down a touch to clear the status bar.
    node.style.cssText =
      "position:fixed;left:8px;right:8px;top:8px;z-index:9000;" +
      "display:none;align-items:center;justify-content:center;gap:12px;" +
      "padding:8px 14px;border-radius:10px;" +
      "font:600 13px/1.3 system-ui,-apple-system,sans-serif;text-align:center;" +
      "color:#fff;background:rgba(180,83,9,.95);box-shadow:0 2px 10px rgba(0,0,0,.4);";
    const label = document.createElement("span");
    label.id = "role-banner-label";
    const btn = document.createElement("button");
    btn.id = "role-banner-take";
    btn.type = "button";
    btn.textContent = "Take helm";
    btn.style.cssText =
      "flex:none;padding:6px 12px;border:0;border-radius:8px;cursor:pointer;" +
      "font:700 13px system-ui,-apple-system,sans-serif;color:#7c2d12;background:#fde68a;";
    btn.addEventListener("click", function () { VA.takeHelm(); });
    node.appendChild(label);
    node.appendChild(btn);
    (document.body || document.documentElement).appendChild(node);
    return node;
  }

  function render(info) {
    const role = info.role;
    const clients = info.clients || 1;
    const node = el();
    const label = document.getElementById("role-banner-label");
    const btn = document.getElementById("role-banner-take");
    // Dim non-STOP controls for observers as a hint (server is the authority).
    document.body.classList.toggle("va-observer", role !== "helm");

    if (role !== "helm") {
      // Observer: prominent banner + Take Helm button.
      if (label) {
        label.textContent = info.readonly
          ? "Demo — read-only view (controls disabled)"
          : (info.helmPresent ? "Observing — another helm is connected"
                              : "Observing — no helm connected");
      }
      if (btn) btn.style.display = info.readonly ? "none" : "";
      node.style.background = "rgba(180,83,9,.95)";
      node.style.display = "flex";
      return;
    }
    // This client is the helm.
    if (clients > 1) {
      // Quiet informational hint; no Take Helm button (already helm).
      if (label) label.textContent = clients + " connected · you have the helm";
      if (btn) btn.style.display = "none";
      node.style.background = "rgba(30,64,110,.92)";
      node.style.display = "flex";
    } else {
      node.style.display = "none";  // helm + alone: nothing to show
    }
  }

  VA.onRole(render);
})();
