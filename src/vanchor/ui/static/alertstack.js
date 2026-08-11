/* Vanchor-NG — safety-banner stack coordinator (#166).
 *
 * When several alerts fire at once the strips in #safety-banners stack
 * vertically and eat the phone screen. This coordinator (Proposal A in
 * docs/alerts-presentation.md) keeps the highest-priority active banner
 * full-size and collapses every other active banner into ONE slim labelled
 * overflow strip directly beneath it:
 *
 *     ▲ GPS LOST · SHALLOW · +2 more ▾
 *
 * Tap the strip to expand back to today's full stack (so every action button
 * is one tap away); tap it again — or wait 10 s of no interaction inside the
 * container — to recollapse.
 *
 * It NEVER touches the existing show/hide logic (safety.js / health.js /
 * core.js / alerts.js): banners are rehomed VISUALLY via a `sb-compacted`
 * class, never removed. CSS `order` stays the single source of priority truth.
 * Recompute is idempotent so the MutationObserver converges instead of looping.
 */
"use strict";

(function () {
  const container = document.getElementById("safety-banners");
  const strip = document.getElementById("sbanner-overflow-strip");
  if (!container || !strip) return;

  // ---- short-label map (by banner id) --------------------------------------
  // Every collapsed ALARM-class banner shows its label token (never elided);
  // warn-class banners fold into the "+N more" count.
  const LABELS = {
    "health-fault-banner": "FAULT",
    "mob-banner": "MOB",
    "anchor-alarm-banner": "DRAG ALARM",
    "health-fix-lost-banner": "GPS LOST",
    "shallow-banner": "SHALLOW",
    "health-hdg-stale-banner": "COMPASS",
    "batt-crit-banner": "BATT CRIT",
    "batt-warn-banner": "BATT LOW",
    "link-banner": "LINK LOST",
    "health-depth-stale-banner": "DEPTH",
    "rtl-banner": "BATT RTL",
    "auto-apb-banner": "EXT AUTOPILOT",
    "gov-banner": "GOVERNOR",
  };

  // Alarm-class = a banner that is safety-floor critical (red). Warn-class =
  // amber advisory. Derived from the existing severity classes so CSS stays
  // the single source of truth.
  function isAlarm(el) {
    return (
      el.classList.contains("sbanner-alarm") ||
      el.classList.contains("sbanner-mob") ||
      el.classList.contains("sbanner-batt-crit")
    );
  }
  // Short label for one banner. Special-cases the shallow/no-go strip (its
  // message text distinguishes the two), and falls back to the banner's own
  // message text for unknown future ids.
  function labelFor(el) {
    const id = el.id;
    if (id === "shallow-banner") {
      const msgEl = document.getElementById("shallow-banner-msg");
      const txt = (msgEl && msgEl.textContent) || "";
      const up = txt.trim().toUpperCase();
      const hasNogo = up.indexOf("NO-GO") >= 0;
      const hasShallow = up.indexOf("SHALLOW") >= 0;
      if (hasNogo && hasShallow) return "SHALLOW/NO-GO";
      if (up.startsWith("NO-GO")) return "NO-GO";
      return "SHALLOW";
    }
    if (LABELS[id]) return LABELS[id];
    // Unknown future id: first ~12 chars of the strip's own text, uppercased.
    const src =
      el.querySelector(".sb-msg") || el.querySelector(".sb-title");
    const txt = (src && src.textContent ? src.textContent : "").trim();
    if (txt) return txt.slice(0, 12).toUpperCase();
    return "ALERT";
  }

  // ---- state ----------------------------------------------------------------
  let expanded = false;
  let mutating = false; // re-entry guard for our own DOM writes
  let recollapseTimer = null;

  function activeBanners() {
    const out = [];
    for (const el of container.children) {
      if (el === strip) continue;
      if (!el.classList.contains("sbanner")) continue;
      if (el.classList.contains("hidden")) continue;
      out.push(el);
    }
    // CSS `order` is the priority source of truth (lower = higher priority).
    out.sort(function (a, b) {
      const oa = parseInt(getComputedStyle(a).order, 10) || 0;
      const ob = parseInt(getComputedStyle(b).order, 10) || 0;
      return oa - ob;
    });
    return out;
  }

  // Build the desired overflow-strip content string for the compacted set.
  function buildStripText(compacted) {
    const alarmLabels = [];
    let warnCount = 0;
    for (const el of compacted) {
      if (isAlarm(el)) alarmLabels.push(labelFor(el));
      else warnCount += 1; // warn-class (incl. anything not alarm)
    }
    let text = alarmLabels.join(" · ");
    if (warnCount > 0) {
      const plus = "+" + warnCount + " more";
      text = text ? text + " · " + plus : plus;
    }
    return text;
  }

  // Set a class on an element only if it needs changing (idempotent).
  function setClass(el, cls, want) {
    if (el.classList.contains(cls) !== want) {
      el.classList.toggle(cls, want);
      return true;
    }
    return false;
  }
  function setAttr(el, name, val) {
    if (el.getAttribute(name) !== val) {
      el.setAttribute(name, val);
      return true;
    }
    return false;
  }
  function setText(el, txt) {
    if (el.textContent !== txt) {
      el.textContent = txt;
      return true;
    }
    return false;
  }

  // ---- recompute (idempotent) ----------------------------------------------
  function recompute() {
    if (mutating) return;
    const active = activeBanners();
    mutating = true;
    try {
      recomputeInner(active);
    } finally {
      mutating = false;
    }
  }

  function recomputeInner(active) {
    // 0 or 1 active: no compaction at all, strip hidden. Pixel-identical to
    // today. Also drop out of expanded mode if we were in it.
    if (active.length <= 1) {
      // Clear any compaction anywhere (including on hidden banners).
      for (const el of container.children) {
        if (el === strip) continue;
        setClass(el, "sb-compacted", false);
      }
      setClass(strip, "hidden", true);
      setText(strip, "");
      strip.removeAttribute("data-sev");
      setAttr(strip, "aria-expanded", "false");
      if (expanded) {
        expanded = false;
        clearRecollapse();
      }
      return;
    }

    const top = active[0];
    const rest = active.slice(1);

    // Top banner never compacted.
    setClass(top, "sb-compacted", false);
    if (expanded) {
      // Full stack visible; strip becomes the collapse bar.
      for (const el of rest) setClass(el, "sb-compacted", false);
      renderStrip(rest); // colour/sev from the set that WOULD be collapsed
      setText(strip, "▴ collapse");
      setAttr(strip, "aria-expanded", "true");
      setAttr(strip, "aria-label", "Collapse alerts");
      setClass(strip, "hidden", false);
    } else {
      // Collapsed: hide all but the top, render the overflow strip.
      for (const el of rest) setClass(el, "sb-compacted", true);
      // Any hidden banner should not carry a stale compaction class.
      for (const el of container.children) {
        if (el === strip || el === top) continue;
        if (rest.indexOf(el) >= 0) continue;
        setClass(el, "sb-compacted", false);
      }
      renderStrip(rest);
      setText(strip, buildStripText(rest) + " ▾");
      setAttr(strip, "aria-expanded", "false");
      setAttr(
        strip,
        "aria-label",
        rest.length + " more alert" + (rest.length === 1 ? "" : "s") + ", tap to expand"
      );
      setClass(strip, "hidden", false);
    }
  }

  // Set the strip severity attribute from the compacted set.
  function renderStrip(compacted) {
    let sev = "warn";
    for (const el of compacted) {
      if (isAlarm(el)) { sev = "alarm"; break; }
    }
    setAttr(strip, "data-sev", sev);
  }

  // ---- expand / collapse ----------------------------------------------------
  function clearRecollapse() {
    if (recollapseTimer !== null) {
      clearTimeout(recollapseTimer);
      recollapseTimer = null;
    }
  }
  function armRecollapse() {
    clearRecollapse();
    recollapseTimer = setTimeout(function () {
      recollapseTimer = null;
      if (expanded) {
        expanded = false;
        recompute();
      }
    }, 10000);
  }

  strip.addEventListener("click", function () {
    if (expanded) {
      expanded = false;
      clearRecollapse();
      recompute();
    } else {
      expanded = true;
      recompute();
      armRecollapse();
    }
  });

  // Any pointer interaction inside the container while expanded resets the
  // 10 s idle timer, so a user mid-interaction is never yanked back.
  container.addEventListener("pointerdown", function () {
    if (expanded) armRecollapse();
  });

  // ---- observe --------------------------------------------------------------
  const observer = new MutationObserver(function () {
    recompute();
  });
  observer.observe(container, {
    attributes: true,
    attributeFilter: ["class"],
    childList: true,
    subtree: true,
  });

  // Initial pass (banners may already be present / health.js prepends async).
  recompute();
})();
