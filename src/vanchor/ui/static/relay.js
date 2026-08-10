/* Client fetch relay (#147): the boat's Pi usually has no internet, but THIS
 * device (phone/tablet) does. When server code needs an online resource (the
 * route planner's OpenStreetMap water data, map tiles, ...) it broadcasts
 * {type:"fetch_request", id, url, method, headers?, body_b64?} over the
 * telemetry WS; we fetch it on the boat's behalf and POST the bytes back to
 * /api/relay/<id>. Cache-first: a tile we already hold in the offline
 * IndexedDB cache is answered from there without touching the internet.
 * Failures are reported back (the server surfaces a clear error) AND toasted
 * here so the operator sees that the online fetch could not be completed. */
(function () {
  "use strict";
  if (!window.VA || !VA.onFetchRequest) return;

  function b64ToBytes(b64) {
    const bin = atob(b64);
    const out = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
    return out;
  }

  async function postResult(id, ok, payload) {
    try {
      await fetch("/api/relay/" + encodeURIComponent(id) + "?ok=" + (ok ? 1 : 0), {
        method: "POST",
        headers: { "Content-Type": "application/octet-stream" },
        body: payload,
      });
    } catch (e) { /* boat link dropped mid-relay — nothing more we can do */ }
  }

  async function handle(req) {
    const id = req.id, url = req.url;
    if (!id || !url) return;
    // 1) Cache-first: the offline tile cache may already hold this exact URL
    //    (only ever populated with GET-able tiles, so skip for POST bodies).
    if ((req.method || "GET") === "GET" && VA.tileCache) {
      try {
        const hit = await VA.tileCache.get(url);
        if (hit) {
          await postResult(id, true, hit);   // Blob posts as-is
          return;
        }
      } catch (e) { /* cache miss path below */ }
    }
    // 2) Fetch on the boat's behalf with this device's internet.
    try {
      const opts = { method: req.method || "GET" };
      if (req.headers) opts.headers = req.headers;
      if (req.body_b64) opts.body = b64ToBytes(req.body_b64);
      const resp = await fetch(url, opts);
      if (!resp.ok) throw new Error("HTTP " + resp.status + " from " + new URL(url).hostname);
      const blob = await resp.blob();
      await postResult(id, true, blob);
      // Opportunistically warm the tile cache for GETs so the NEXT relay of the
      // same URL (or the map itself) is answered offline.
      if (opts.method === "GET" && VA.tileCache) {
        try { VA.tileCache.put(url, blob); } catch (e) { /* best-effort */ }
      }
    } catch (e) {
      const msg = "This device could not fetch " + url + " (" + (e && e.message || e) + ")";
      await postResult(id, false, msg);
      // The operator must SEE that the boat needed online data and this device
      // couldn't provide it (offline-fetch failures were previously silent).
      if (VA.toast) VA.toast("⚠ Online fetch failed: " + (e && e.message || e), { ttl: 6000 });
      if (VA.logLine) VA.logLine("relay: " + msg);
    }
  }

  VA.onFetchRequest((req) => { handle(req); });
})();
