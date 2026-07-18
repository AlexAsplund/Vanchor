/* Vanchor-NG — armed-state banner (WP6 item 19).
 *
 * A full-width strip below the topbar (above any alarm strips) that shows the
 * current "tap to…" arming state for go-to, add-waypoints, and measure. One
 * arming at a time: show() while visible fires the previous onCancel first.
 *
 * API:
 *   VA.armbar.show({ text, done?, onDone?, onCancel? })
 *   VA.armbar.update(text)
 *   VA.armbar.hide()
 *
 * Alarms (task-1 .sbanner strips) STACK ABOVE because safety-banners sits
 * higher in z-order; armbar is below the alarm region but above the map.
 */
"use strict";

(function () {
  let _onCancel = null;
  let _onDone = null;

  const banner = document.getElementById("arm-banner");
  const textEl = document.getElementById("arm-banner-text");
  const doneBtn = document.getElementById("arm-banner-done");
  const cancelBtn = document.getElementById("arm-banner-cancel");

  if (!banner) return;

  function hide() {
    banner.classList.add("hidden");
    _onCancel = null; _onDone = null;
  }

  function show(opts) {
    opts = opts || {};
    const text = opts.text || "";
    const done = opts.done !== false;
    const onDone = opts.onDone || null;
    const onCancel = opts.onCancel || null;
    // Cancel any previous arming without re-triggering
    if (!banner.classList.contains("hidden") && typeof _onCancel === "function") {
      try { _onCancel(); } catch (_) {}
    }
    _onDone = onDone;
    _onCancel = onCancel;
    if (textEl) textEl.textContent = text;
    if (doneBtn) doneBtn.classList.toggle("hidden", !done);
    banner.classList.remove("hidden");
  }

  function update(text) {
    if (textEl) textEl.textContent = text || "";
  }

  if (doneBtn) doneBtn.addEventListener("click", function() {
    const fn = _onDone;
    hide();
    if (typeof fn === "function") try { fn(); } catch (_) {}
  });

  if (cancelBtn) cancelBtn.addEventListener("click", function() {
    const fn = _onCancel;
    hide();
    if (typeof fn === "function") try { fn(); } catch (_) {}
  });

  VA.armbar = { show: show, update: update, hide: hide };
})();
