/*
 * Node harness for the USGS 3DEP bathymetry basemap unit tests.
 *
 * Loads the REAL map-core.js and offline.js in a vm sandbox with just-enough
 * `L` / `window` / `document` / `localStorage` stubs, so the tests exercise the
 * actual shipped code (not a copy). Emits a JSON report on stdout that the
 * Python test (test_usgs_bathymetry.py) asserts against.
 *
 * Run: node tests/usgs_bathymetry_harness.mjs <static-dir>
 */
import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";

const STATIC = process.argv[2];
if (!STATIC) {
  console.error("usage: node usgs_bathymetry_harness.mjs <static-dir>");
  process.exit(2);
}

// A no-op Leaflet stub. L.tileLayer returns a plain object whose getTileUrl can
// be overridden (which is exactly what map-core does for the WMS basemap). The
// chainable methods return the layer so `.addTo(map)` etc. don't throw.
function makeLayer() {
  const layer = {
    options: {},
    getTileUrl() { return ""; },
    addTo() { return layer; },
    on() { return layer; },
    setZIndex() { return layer; },
    redraw() { return layer; },
  };
  return layer;
}
// A chainable no-op object: every method returns itself, every property read
// returns another chainable. Stands in for a Leaflet map / control / pane so
// map-core.js's boot code (setView, on, addLayer, createPane, ...) never throws.
function chainable() {
  const target = function () { return proxy; };
  const proxy = new Proxy(target, {
    get(_t, prop) {
      if (prop === Symbol.toPrimitive) return () => "";
      if (prop === "then") return undefined; // not a thenable
      return proxy;
    },
    apply() { return proxy; },
    set() { return true; },
  });
  return proxy;
}
// A callable factory that also answers ANY sub-property (e.g. L.control.zoom,
// L.control.layers, L.tileLayer.wms) with another callable factory returning a
// chainable. This absorbs every Leaflet entry point map-core.js touches at boot
// without us enumerating each one.
function factory(makeResult) {
  const fn = function () { return makeResult(...arguments); };
  return new Proxy(fn, {
    get(t, prop) {
      if (prop in t) return t[prop];
      return factory(makeResult);
    },
    apply(_t, _this, args) { return makeResult(...args); },
  });
}
// A class-like stub whose .extend() returns another extendable class and whose
// instances are chainable — covers L.GridLayer.extend({...}), L.Control.extend,
// `new (L.GridLayer.extend(...))()`, etc.
function klass() {
  function Ctor() { return chainable(); }
  Ctor.extend = () => klass();
  Ctor.include = () => Ctor;
  return new Proxy(Ctor, {
    construct() { return chainable(); },
    get(t, prop) { if (prop in t) return t[prop]; return factory(() => chainable()); },
  });
}

const Lbase = {
  tileLayer: factory((url, opts) => { const l = makeLayer(); l.options = opts || {}; l._url = url; return l; }),
  map: factory(() => chainable()),
  control: factory(() => chainable()),
  layerGroup: factory(() => chainable()),
  featureGroup: factory(() => chainable()),
  GridLayer: klass(),
  Control: klass(),
  Layer: klass(),
  Class: klass(),
  DomUtil: { create() { return { classList: { add() {}, remove() {} }, style: {} }; } },
  Util: { template(str, data) { return str.replace(/\{ *([\w_-]+) *\}/g, (_, k) => data[k]); } },
};
// Fallback: any unspecified L.* becomes a permissive factory so boot-time calls
// (L.latLng, L.point, L.icon, ...) never throw.
const L = new Proxy(Lbase, {
  get(t, prop) {
    if (prop in t) return t[prop];
    if (typeof prop === "symbol") return undefined;
    return factory(() => chainable());
  },
});

// A permissive Proxy stands in for the DOM / browser globals map-core.js and
// offline.js touch at load time. Reads return another no-op proxy; calls are
// no-ops. This lets both IIFEs run to the point where they publish their VA.*
// exports without a real DOM.
function noopProxy() {
  const fn = function () { return proxy; };
  const proxy = new Proxy(fn, {
    get(_t, prop) {
      if (prop === Symbol.toPrimitive) return () => "";
      if (prop === "length") return 0;
      return proxy;
    },
    apply() { return proxy; },
    set() { return true; },
  });
  return proxy;
}

const store = new Map();
const localStorage = {
  getItem(k) { return store.has(k) ? store.get(k) : null; },
  setItem(k, v) { store.set(k, String(v)); },
  removeItem(k) { store.delete(k); },
};

const win = {};
const VA = {};
win.VA = VA;
win.L = L;
win.localStorage = localStorage;
win.addEventListener = () => {};
win.dispatchEvent = () => {};
win.matchMedia = () => ({ matches: false, addEventListener() {}, addListener() {} });
win.location = { href: "http://localhost:8000/", protocol: "http:", host: "localhost:8000" };
win.innerWidth = 1024;
win.innerHeight = 768;
win.CustomEvent = function () {};
win.setTimeout = () => 0;
win.clearTimeout = () => {};
win.setInterval = () => 0;
win.indexedDB = undefined; // offline.js degrades gracefully when absent

const sandbox = {
  window: win,
  VA, // core.js sets a bare global VA = (window.VA = window.VA || {})
  L,
  document: noopProxy(),
  localStorage,
  navigator: { onLine: true, serviceWorker: undefined },
  console,
  fetch: () => Promise.resolve({ ok: false }),
  setTimeout: () => 0,
  clearTimeout: () => {},
  setInterval: () => 0,
  CustomEvent: function () {},
  Image: function () { return {}; },
  URL: { createObjectURL: () => "", revokeObjectURL: () => {} },
};
sandbox.globalThis = sandbox;
vm.createContext(sandbox);

function run(file) {
  const src = fs.readFileSync(path.join(STATIC, file), "utf-8");
  vm.runInContext(src, sandbox, { filename: file });
}

// core.js establishes the global VA; but running the whole thing is fragile, so
// we pre-seed VA (mirrors core.js's single effect that map-core/offline rely on).
run("map-core.js");
run("offline.js");

// ---- gather results ----
const wmsUrl = VA._wmsTileUrl(
  VA._usgs3dep.endpoint,
  { layers: VA._usgs3dep.layer, styles: VA._usgs3dep.style, format: "image/png", transparent: false, version: "1.3.0" },
  12, 1160, 1512
);

// The live basemap layer's getTileUrl (the override) for the same tile.
const liveLayer = VA._baseLayers["USGS Topobathy (US)"];
const liveUrl = liveLayer.getTileUrl({ z: 12, x: 1160, y: 1512 });

// The offline template (function form) for the same tile.
const tmpl = VA._baseTemplates["USGS Topobathy (US)"];
const offlineTmplUrl = typeof tmpl === "function" ? tmpl(12, 1160, 1512) : null;

// The offline tileUrl() resolver over both a string and the function template.
const stringResolved = VA._offline.tileUrl(
  "https://a.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}.png", 5, 3, 7
);
const fnResolved = VA._offline.tileUrl(tmpl, 12, 1160, 1512);

// enumerateTiles / countTiles agreement on a small bbox.
const bbox = [-94.40, 47.14, -94.38, 47.16];
const enumerated = VA._offline.enumerateTiles(bbox, 12, 13).length;
const counted = VA._offline.countTiles(bbox, 12, 13);

const report = {
  wmsUrl,
  liveUrl,
  offlineTmplUrl,
  stringResolved,
  fnResolved,
  templateIsFunction: typeof tmpl === "function",
  baseNativeMax: VA._baseNativeMax["USGS Topobathy (US)"],
  usgs3dep: VA._usgs3dep,
  hasBasemap: Boolean(VA._baseLayers["USGS Topobathy (US)"]),
  enumerated,
  counted,
};
process.stdout.write(JSON.stringify(report));
