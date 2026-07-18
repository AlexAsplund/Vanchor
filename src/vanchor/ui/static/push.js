/* Vanchor-NG — Web Push opt-in (adoption #7).
 * Settings -> Sound & touch -> Notifications card: permission request,
 * PushManager subscribe/unsubscribe against /api/push/*, test button.
 * Requires a secure context + a server with the `push` extra; both
 * unavailability cases show an explanation instead of the controls. */
"use strict";
(function () {
  const $ = (id) => document.getElementById(id);
  const card = $("push-card");
  if (!card) return;

  const supported = window.isSecureContext &&
    "serviceWorker" in navigator && "PushManager" in window &&
    "Notification" in window;

  // b64url -> Uint8Array for applicationServerKey (standard converter):
  function b64ToBytes(s) {
    const pad = "=".repeat((4 - (s.length % 4)) % 4);
    const raw = atob((s + pad).replace(/-/g, "+").replace(/_/g, "/"));
    return Uint8Array.from(raw, (c) => c.charCodeAt(0));
  }

  function setBadge(text) {
    const el = $("push-state");
    if (el) el.textContent = text;
  }

  function setStatus(text) {
    const el = $("push-status");
    if (el) el.textContent = text;
  }

  function showUnavailable(msg) {
    const unavEl = $("push-unavailable");
    if (unavEl) {
      // Map pip-speak server reasons to plain fishing language.
      let plain = msg;
      let raw = null;
      if (/pywebpush|py_vapid|push extra/i.test(msg)) {
        plain = "The boat is missing its notifications add-on. " +
                "Install `vanchor-ng[push]` on the boat, then come back here.";
        raw = msg;
      }
      unavEl.innerHTML = "";
      const p = document.createElement("p");
      p.textContent = plain;
      unavEl.appendChild(p);
      if (raw) {
        const det = document.createElement("details");
        det.className = "mini";
        const sum = document.createElement("summary");
        sum.textContent = "Details";
        const rawEl = document.createElement("span");
        rawEl.id = "push-unavailable-raw";
        rawEl.textContent = raw;
        det.appendChild(sum);
        det.appendChild(rawEl);
        unavEl.appendChild(det);
      }
      unavEl.classList.remove("hidden");
    }
    const ctrl = $("push-controls");
    if (ctrl) ctrl.classList.add("hidden");
    setBadge("unavailable");
    _updatePushLink(false);
  }

  function showControls() {
    const el = $("push-unavailable");
    if (el) el.classList.add("hidden");
    const ctrl = $("push-controls");
    if (ctrl) ctrl.classList.remove("hidden");
  }

  // ---- anchor-panel push cross-link (item 35) -----------------------------
  // Shown when push is supported but not yet subscribed so the user can
  // navigate from the anchor panel to the push settings card in one tap.
  function _updatePushLink(subscribed) {
    const link = $("aa-push-link");
    if (!link) return;
    // Show only when push is supported-but-unsubscribed.
    const show = supported && !subscribed;
    link.classList.toggle("hidden", !show);
  }
  // Wire the cross-link: tap → open Menu → Phone notifications card.
  const pushLink = $("aa-push-link");
  if (pushLink) {
    pushLink.addEventListener("click", function () {
      try {
        const settingsBtn = document.getElementById("settings-open");
        if (settingsBtn) settingsBtn.click();
        setTimeout(function () {
          if (VA.menu) VA.menu.showCategory("feedback");
          const pushCard = $("push-card");
          if (pushCard) {
            pushCard.open = true;
            pushCard.scrollIntoView({ behavior: "smooth", block: "start" });
          }
        }, 120);
      } catch (e) { /* ignore */ }
    });
  }

  function setCount(n) {
    const el = $("push-count");
    if (el) el.textContent = n;
  }

  let probed = false;
  let serverPubKey = null;

  async function probe() {
    if (!supported) {
      // Determine why: insecure context or missing browser support.
      if (!window.isSecureContext) {
        const httpsUrl = "https://" + location.hostname + ":8443";
        showUnavailable(
          "Notifications need HTTPS. Open " + httpsUrl +
          " and trust the boat’s certificate on this device, then enable here."
        );
      } else {
        showUnavailable("This browser does not support Web Push (PushManager missing).");
      }
      return;
    }

    let s;
    try {
      s = await VA.getJSON("/api/push/status");
    } catch (e) {
      showUnavailable("Could not reach the server: " + e.message);
      return;
    }

    if (!s.available) {
      showUnavailable(s.reason || "Push extra not installed on the boat.");
      return;
    }
    if (!s.enabled) {
      showUnavailable("Web Push is disabled in the boat’s config (push.enabled: false).");
      return;
    }

    showControls();
    setCount(s.subscriptions);

    // Reflect this browser's subscription state.
    const cb = $("push-enable");
    try {
      const reg = await navigator.serviceWorker.ready;
      const sub = await reg.pushManager.getSubscription();
      const granted = Notification.permission === "granted";
      const isSubbed = !!(sub && granted);
      if (cb) cb.checked = isSubbed;
      setBadge(isSubbed ? "on" : "off");
      _updatePushLink(isSubbed);
      // Re-sync: push our subscription to the server (idempotent upsert).
      if (sub && granted) {
        try {
          await VA.postJSON("/api/push/subscribe", {
            subscription: sub.toJSON(),
            ua: navigator.userAgent,
          });
        } catch (_) { /* non-fatal */ }
      }
    } catch (e) {
      if (cb) cb.checked = false;
      setBadge("off");
    }
  }

  // Lazy probe: only on first open of the details card.
  card.addEventListener("toggle", function onToggle() {
    if (!card.open) return;
    if (probed) return;
    probed = true;
    probe();
  });

  // Enable checkbox handler.
  const cbEl = $("push-enable");
  if (cbEl) {
    cbEl.addEventListener("change", async function () {
      if (!this.checked) {
        // Disable: unsubscribe.
        try {
          const reg = await navigator.serviceWorker.ready;
          const sub = await reg.pushManager.getSubscription();
          if (sub) {
            await sub.unsubscribe();
            await VA.postJSON("/api/push/unsubscribe", { endpoint: sub.endpoint });
          }
          setStatus("Disabled on this device.");
          setBadge("off");
          setCount(0);
          _updatePushLink(false);
        } catch (e) {
          setStatus("Error disabling: " + e.message);
          this.checked = true;
        }
        return;
      }

      // Enable: request permission -> get pubkey -> subscribe.
      try {
        const perm = await Notification.requestPermission();
        if (perm !== "granted") {
          this.checked = false;
          setStatus("Permission denied — allow notifications for this site in browser settings.");
          return;
        }

        const pk = await VA.getJSON("/api/push/pubkey");
        if (!pk.ok) {
          this.checked = false;
          setStatus(pk.error || "Could not get the VAPID public key from the boat.");
          return;
        }
        serverPubKey = pk.public_key;

        const reg = await navigator.serviceWorker.ready;
        const sub = await reg.pushManager.subscribe({
          userVisibleOnly: true,
          applicationServerKey: b64ToBytes(serverPubKey),
        });

        const r = await VA.postJSON("/api/push/subscribe", {
          subscription: sub.toJSON(),
          ua: navigator.userAgent,
        });
        setCount(r.count || "?");
        setStatus("Notifications enabled on this device.");
        setBadge("on");
        _updatePushLink(true);
      } catch (e) {
        this.checked = false;
        setStatus("Error: " + e.message);
        setBadge("off");
      }
    });
  }

  // Test button handler.
  const testBtn = $("push-test");
  if (testBtn) {
    testBtn.addEventListener("click", async function () {
      setStatus("Sending…");
      try {
        const r = await VA.postJSON("/api/push/test", {});
        if (r.ok) {
          setStatus(
            "Sent to " + r.sent + " device(s)" +
            (r.failed ? ", " + r.failed + " failed" : "") +
            ". The notification may take a few seconds to appear."
          );
        } else {
          setStatus(r.error || "failed (check that you have subscribed first)");
        }
      } catch (e) {
        setStatus("Error: " + e.message);
      }
    });
  }
})();
