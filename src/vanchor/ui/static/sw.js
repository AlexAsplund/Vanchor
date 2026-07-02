/* Vanchor-NG service worker — makes the app shell load fully offline on the
 * boat (#82).
 *
 * Strategy
 * --------
 *  - PRECACHE the whole app shell on install: the document ("/" + index.html),
 *    every /static/*.js, style.css, the vendored libraries (Leaflet + uPlot +
 *    self-hosted fonts), the manifest and the icons. These never change without
 *    a redeploy, so a cache-first strategy serves them instantly and offline.
 *  - cache-first for same-origin shell + vendor assets.
 *  - BYPASS the SW entirely (network-only, no caching) for:
 *      * /api/*           — live runtime state/commands
 *      * /ws              — the telemetry WebSocket (never goes through fetch)
 *      * cross-origin map tile hosts — these are already cached in IndexedDB by
 *        offline.js; double-caching them here would break that and blow storage.
 *  - Navigation requests fall back to the cached index.html when offline, so a
 *    fresh load with zero internet still boots the app.
 *  - Old caches are deleted on activate via a versioned cache name.
 */
"use strict";

const VERSION = "vanchor-shell-v55";
const CACHE = VERSION;

// The app shell. Kept in sync with index.html's <link>/<script> tags. "/" and
// "/index.html" both resolve to the same document; we precache both so a direct
// navigation or an offline fallback both hit the cache.
const SHELL = [
  "/",
  "/index.html",
  "/manifest.webmanifest",
  "/static/style.css",
  // Vendored libraries (self-hosted for guaranteed offline).
  "/static/vendor/leaflet/leaflet.css",
  "/static/vendor/leaflet/leaflet.js",
  "/static/vendor/leaflet/images/marker-icon.png",
  "/static/vendor/leaflet/images/marker-icon-2x.png",
  "/static/vendor/leaflet/images/marker-shadow.png",
  "/static/vendor/leaflet/images/layers.png",
  "/static/vendor/leaflet/images/layers-2x.png",
  "/static/vendor/uplot/uPlot.min.css",
  "/static/vendor/uplot/uPlot.iife.min.js",
  "/static/vendor/fonts/fonts.css",
  "/static/vendor/fonts/ChakraPetch-400.woff2",
  "/static/vendor/fonts/ChakraPetch-500.woff2",
  "/static/vendor/fonts/ChakraPetch-600.woff2",
  "/static/vendor/fonts/ChakraPetch-700.woff2",
  "/static/vendor/fonts/Inter-400.woff2",
  "/static/vendor/fonts/Inter-500.woff2",
  "/static/vendor/fonts/Inter-600.woff2",
  "/static/vendor/fonts/Inter-700.woff2",
  "/static/vendor/fonts/Inter-800.woff2",
  "/static/vendor/fonts/JetBrainsMono-400.woff2",
  "/static/vendor/fonts/JetBrainsMono-500.woff2",
  "/static/vendor/fonts/JetBrainsMono-700.woff2",
  // App icons.
  "/static/icons/icon-192.png",
  "/static/icons/icon-512.png",
  "/static/icons/icon-maskable-512.png",
  "/static/icons/apple-touch-icon.png",
  // App scripts (every <script src="/static/*.js"> in index.html).
  "/static/core.js",
  "/static/map-core.js",
  "/static/map-boaticon.js",
  "/static/map-boat.js",
  "/static/map-anchor.js",
  "/static/map-track.js",
  "/static/map-waypoints.js",
  "/static/map-depth.js",
  "/static/hudframe.js",
  "/static/hud.js",
  "/static/roles.js",
  "/static/steering.js",
  "/static/wizard.js",
  "/static/boat.js",
  "/static/debug.js",
  "/static/appcore.js",
  "/static/health.js",
  "/static/controls.js",
  "/static/route.js",
  "/static/settings.js",
  "/static/menu.js",
  "/static/charts.js",
  "/static/remote.js",
  "/static/markers.js",
  "/static/routing.js",
  "/static/survey.js",
  "/static/island.js",
  "/static/offline.js",
  "/static/layout.js",
  "/static/routestore.js",
  "/static/navctl.js",
  "/static/catch.js",
  "/static/analytics.js",
  "/static/trips.js",
  "/static/gpscal.js",
  "/static/devices.js",
  "/static/backup.js",
  "/static/guided.js",
  "/static/work-area.js",
  "/static/safety.js",
  "/static/motorplace.js",
  "/static/selectboat.js",
  "/static/teleport.js",
  "/static/alerts.js",
  "/static/logs.js",
  "/static/audit.js",
  "/static/measure.js",
  "/static/mobile.js",
  "/static/wakelock.js",
];

// Map-tile hosts handled by offline.js's IndexedDB cache — the SW must not
// touch them.
const TILE_HOSTS = [
  "basemaps.cartocdn.com",
  "server.arcgisonline.com",
  "tile.opentopomap.org",
  "tiles.openseamap.org",
  "tile.openstreetmap.org",
  "fonts.googleapis.com",
  "fonts.gstatic.com",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE).then((cache) =>
      // Tolerate individual misses so one bad URL can't wedge the whole install.
      Promise.all(
        SHELL.map((url) =>
          cache.add(new Request(url, { cache: "reload" })).catch(() => {})
        )
      )
    ).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

function isTileHost(url) {
  return TILE_HOSTS.some((h) => url.hostname === h || url.hostname.endsWith("." + h));
}

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return; // commands etc. — network only

  const url = new URL(req.url);

  // Bypass: live API + websocket + tile hosts -> network only, never cached.
  if (url.origin === self.location.origin) {
    if (url.pathname.startsWith("/api/") || url.pathname === "/ws") return;
  }
  if (isTileHost(url)) return;

  // Same-origin shell + navigations: NETWORK-FIRST. The app is served by the
  // boat's own Pi, which is reachable whenever you're actually controlling the
  // boat, so we always prefer FRESH assets (no stale UI lingering after an
  // update — the bug that made app changes not show up) and fall back to the
  // precached copy only when the server is momentarily unreachable. The IndexedDB
  // tile cache + the bypassed /api keep the map + live data working offline.
  if (url.origin === self.location.origin || req.mode === "navigate") {
    event.respondWith(
      // cache:"reload" bypasses the HTTP cache so network-first means *network*,
      // not a heuristically-cached stale copy (the reason a CSS/JS change could
      // still not show up even though the server already had the new file).
      fetch(req, { cache: "reload" })
        .then((resp) => {
          if (resp && resp.ok && resp.type === "basic") {
            const copy = resp.clone();
            caches.open(CACHE).then((cache) => cache.put(req, copy));
          }
          return resp;
        })
        .catch(() =>
          caches.match(req).then(
            (cached) => cached || (req.mode === "navigate" ? caches.match("/index.html") : undefined)
          )
        )
    );
  }
});
