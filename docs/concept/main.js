/* VANCHOR-NG concept film — STAGE 1: core scene.
 * Deterministic clock, holographic-instrument look. No network, no post-processing,
 * no shadow maps — all glow is additive sprites / emissive shells.
 * QA hooks: window.__seek(s), window.__beat(n), window.__xray(v).
 */
import * as THREE from 'three';
import { OrbitControls } from './vendor/OrbitControls.js';

// Vendored fonts (Chakra Petch + JetBrains Mono woff2, zero network requests):
// wait briefly for them so the PCB canvas textures are drawn with the real
// mono face. Times out fast — the page must never hang on typography.
try {
  await Promise.race([
    Promise.all([
      document.fonts.load('700 84px "JetBrains Mono"'),
      document.fonts.load('500 30px "JetBrains Mono"'),
      document.fonts.load('600 40px "Chakra Petch"'),
    ]),
    new Promise((r) => setTimeout(r, 1500)),
  ]);
} catch { /* system fallbacks are fine */ }

// ---------------------------------------------------------------- palette ---
const C = {
  bg0: 0x05080d, bg1: 0x071018,
  cyan: 0x2ff3ff, teal: 0x2ce8b0, amber: 0xffc24d, text: 0xeaf2fb,
  navyDeep: 0x06121e, subsurf0: 0x0a2a33,
};
const CYAN = new THREE.Color(C.cyan), TEAL = new THREE.Color(C.teal);

// ------------------------------------------------------------- timeline -----
// Beat durations from the spec. 00–01 cold open (plays once), 02–08 loop (61 s).
const BEAT_DUR = [6, 12, 8, 10, 7, 8, 10, 10, 8];
const BEAT_START = BEAT_DUR.reduce((a, d) => (a.push(a[a.length - 1] + d), a), [0]).slice(0, -1);
const COLD_OPEN = BEAT_START[2];            // 18
const LOOP_DUR = BEAT_DUR.slice(2).reduce((a, b) => a + b, 0); // 61

function timeInfo(globalT) {
  let beat, localT, loopT = 0;
  if (globalT < COLD_OPEN) {
    beat = globalT < BEAT_START[1] ? 0 : 1;
    localT = globalT - BEAT_START[beat];
  } else {
    loopT = (globalT - COLD_OPEN) % LOOP_DUR;
    beat = 8;
    for (let i = 2; i < 9; i++) {
      const b0 = BEAT_START[i] - COLD_OPEN;
      if (loopT >= b0) beat = i; else break;
    }
    localT = loopT - (BEAT_START[beat] - COLD_OPEN);
  }
  return { beat, localT, loopT };
}

// -------------------------------------------------------------- renderer ----
const app = document.getElementById('app');
const isPhone = /Android|iPhone|iPad|Mobile/i.test(navigator.userAgent);
let renderer;
try {
  renderer = new THREE.WebGLRenderer({ antialias: true, powerPreference: 'high-performance' });
  if (!renderer.getContext()) throw new Error('no context');
} catch (e) {
  document.getElementById('fallback').classList.add('on');
  throw e;
}
const DPR_CAP = isPhone ? 1.5 : 2;
let dpr = Math.min(window.devicePixelRatio || 1, DPR_CAP);
renderer.setPixelRatio(dpr);
renderer.setSize(innerWidth, innerHeight);
renderer.toneMapping = THREE.ACESFilmicToneMapping;
renderer.toneMappingExposure = 1.05;
renderer.outputColorSpace = THREE.SRGBColorSpace;
app.appendChild(renderer.domElement);

const scene = new THREE.Scene();
scene.fog = new THREE.FogExp2(C.bg1, 0.016);

const camera = new THREE.PerspectiveCamera(42, innerWidth / innerHeight, 0.1, 600);
camera.position.set(7.4, 3.1, 9.2);
const camTarget = new THREE.Vector3(0.45, 0.55, 0);
camera.lookAt(camTarget);
const BASE_FOV = 42;
function fitFov() {           // widen on portrait screens so the hero still frames
  const a = camera.aspect;
  camera.fov = THREE.MathUtils.clamp(BASE_FOV * (1 + 0.55 * Math.max(0, 1.45 - a)), BASE_FOV, 68);
  camera.updateProjectionMatrix();
}
fitFov();

const controls = new OrbitControls(camera, renderer.domElement);
controls.target.copy(camTarget);
controls.enableDamping = true;
controls.dampingFactor = 0.08;
controls.minDistance = 2.2;
controls.maxDistance = 45;
controls.maxPolarAngle = Math.PI / 2 - 0.05; // stay above the waterline
controls.update();

addEventListener('resize', () => {
  camera.aspect = innerWidth / innerHeight;
  fitFov();
  renderer.setSize(innerWidth, innerHeight);
  repaintHeld();
});
// If the GL context bounces (mobile tab juggling, headless QA), a held frame
// would otherwise stay black — repaint it once the context returns.
renderer.domElement.addEventListener('webglcontextrestored', () => setTimeout(repaintHeld, 0));

// ------------------------------------------------------------ tex helpers ---
function canvasTex(w, h, draw) {
  const cv = document.createElement('canvas');
  cv.width = w; cv.height = h;
  draw(cv.getContext('2d'), w, h);
  const t = new THREE.CanvasTexture(cv);
  t.colorSpace = THREE.SRGBColorSpace;
  return t;
}
function glowTexture(inner = 'rgba(255,255,255,1)', mid = 'rgba(255,255,255,0.28)') {
  return canvasTex(128, 128, (ctx) => {
    const g = ctx.createRadialGradient(64, 64, 0, 64, 64, 64);
    g.addColorStop(0, inner); g.addColorStop(0.35, mid); g.addColorStop(1, 'rgba(255,255,255,0)');
    ctx.fillStyle = g; ctx.fillRect(0, 0, 128, 128);
  });
}
const glowTex = glowTexture();
function addGlowSprite(parent, color, scale, pos, opacity = 0.8) {
  const m = new THREE.SpriteMaterial({
    map: glowTex, color, transparent: true, opacity,
    blending: THREE.AdditiveBlending, depthWrite: false,
  });
  const s = new THREE.Sprite(m);
  s.scale.setScalar(scale);
  s.position.copy(pos);
  s.renderOrder = 6;
  parent.add(s);
  return s;
}

// ------------------------------------------------------------------ sky -----
// Dithered vertical gradient + thin cyan horizon glow, on a BackSide sphere.
const skyTex = canvasTex(64, 512, (ctx, w, h) => {
  const img = ctx.createImageData(w, h);
  const top = [5, 8, 13], bot = [7, 16, 24];
  for (let y = 0; y < h; y++) {
    const v = y / (h - 1);                         // 0 top → 1 bottom
    // horizon band sits at v ≈ .5 (sphere equator)
    const hb = Math.exp(-Math.pow((v - 0.5) / 0.028, 2)) * 0.55
             + Math.exp(-Math.pow((v - 0.5) / 0.10, 2)) * 0.16;
    for (let x = 0; x < w; x++) {
      const i = (y * w + x) * 4;
      const dith = (Math.random() - 0.5) * 3;      // ordered-ish dither: kill banding
      img.data[i]     = top[0] + (bot[0] - top[0]) * v + hb * 30 + dith;
      img.data[i + 1] = top[1] + (bot[1] - top[1]) * v + hb * 120 + dith;
      img.data[i + 2] = top[2] + (bot[2] - top[2]) * v + hb * 140 + dith;
      img.data[i + 3] = 255;
    }
  }
  ctx.putImageData(img, 0, 0);
});
skyTex.needsUpdate = true;
const sky = new THREE.Mesh(
  new THREE.SphereGeometry(280, 32, 24),
  new THREE.MeshBasicMaterial({ map: skyTex, side: THREE.BackSide, fog: false, depthWrite: false })
);
scene.add(sky);

// --------------------------------------------------------------- lights -----
const SUN_DIR = new THREE.Vector3(-0.62, 0.16, -0.60).normalize(); // low on horizon
const key = new THREE.DirectionalLight(0xcfe6ff, 3.0);
key.position.copy(SUN_DIR).multiplyScalar(60);
scene.add(key);
// cool fill from the camera side so the hull reads as a product shot, not a cutout
const fill = new THREE.DirectionalLight(0x8fb8d8, 1.6);
fill.position.set(30, 14, 26);
scene.add(fill);
const hemi = new THREE.HemisphereLight(0x16283e, 0x0b3a34, 0.9);
scene.add(hemi);

// PMREM environment from a throwaway gradient + glint scene (no HDR files).
{
  const pmrem = new THREE.PMREMGenerator(renderer);
  const env = new THREE.Scene();
  const envSky = new THREE.Mesh(
    new THREE.SphereGeometry(50, 16, 12),
    new THREE.MeshBasicMaterial({ side: THREE.BackSide, fog: false })
  );
  envSky.material.map = canvasTex(32, 128, (ctx, w, h) => {
    const g = ctx.createLinearGradient(0, 0, 0, h);
    g.addColorStop(0.0, '#070d16');
    g.addColorStop(0.46, '#0c1d2c');
    g.addColorStop(0.52, '#1a5a66');
    g.addColorStop(0.58, '#0a2a33');
    g.addColorStop(1.0, '#041017');
    ctx.fillStyle = g; ctx.fillRect(0, 0, w, h);
  });
  env.add(envSky);
  const glint = new THREE.Mesh(
    new THREE.SphereGeometry(4, 8, 8),
    new THREE.MeshBasicMaterial({ color: 0xbfe4ff, fog: false })
  );
  glint.position.copy(SUN_DIR).multiplyScalar(40);
  env.add(glint);
  scene.environment = pmrem.fromScene(env, 0.035).texture;
  pmrem.dispose();
  envSky.material.map.dispose(); envSky.geometry.dispose(); glint.geometry.dispose();
}

// ---------------------------------------------------------------- water -----
const WATER_SEGS = isPhone ? 96 : 120;
const waterUniforms = {
  uTime:      { value: 0 },
  uSunDir:    { value: SUN_DIR.clone() },
  uWindDir:   { value: new THREE.Vector2(0.83, 0.55) },
  uWindSkew:  { value: 0.35 },
  uAnchorPos: { value: new THREE.Vector2(0.0, 0.0) },
  uGridLock:  { value: 0.75 },
  uGridBase:  { value: 0.16 },
  uTetherA:   { value: new THREE.Vector2(3.2, 0) },
  uTetherB:   { value: new THREE.Vector2(0, 0) },
  uTetherGlow:{ value: 0.0 },
  uBowPos:    { value: new THREE.Vector2(3.28, 0) },   // motor shaft, world xz
  uThrust:    { value: 0.0 },                           // 0..1, widens the window
  uFogColor:  { value: new THREE.Color(C.bg1) },
  uFogDensity:{ value: scene.fog.density },
};
const waterMat = new THREE.ShaderMaterial({
  uniforms: waterUniforms,
  transparent: true,
  depthWrite: false,
  vertexShader: /* glsl */`
    uniform float uTime;
    varying vec3 vWorld;
    varying vec3 vN;
    varying float vH;

    void wave(vec2 p, vec2 dir, float amp, float len, float speed, float q,
              inout float h, inout vec2 disp, inout vec2 grad) {
      float k = 6.2831853 / len;
      float ph = k * dot(dir, p) - speed * uTime;
      h += amp * sin(ph);
      disp += dir * (q * amp * cos(ph));
      grad += amp * k * dir * cos(ph);
    }

    void main() {
      vec3 pos = position;
      vec2 p = pos.xz;
      float h = 0.0; vec2 disp = vec2(0.0); vec2 grad = vec2(0.0);
      wave(p, normalize(vec2( 1.0,  0.30)), 0.070, 9.5, 0.90, 0.55, h, disp, grad);
      wave(p, normalize(vec2(-0.55, 1.0 )), 0.042, 4.6, 1.25, 0.45, h, disp, grad);
      wave(p, normalize(vec2( 0.80, -0.62)), 0.020, 2.1, 1.90, 0.30, h, disp, grad);
      pos.y += h;
      pos.xz += disp;
      vH = h;
      vN = normalize(vec3(-grad.x, 1.0, -grad.y));
      vec4 wp = modelMatrix * vec4(pos, 1.0);
      vWorld = wp.xyz;
      gl_Position = projectionMatrix * viewMatrix * wp;
    }
  `,
  fragmentShader: /* glsl */`
    precision highp float;
    uniform float uTime;
    uniform vec3  uSunDir;
    uniform vec2  uWindDir;
    uniform float uWindSkew;
    uniform vec2  uAnchorPos;
    uniform float uGridLock;
    uniform float uGridBase;
    uniform vec2  uTetherA;
    uniform vec2  uTetherB;
    uniform float uTetherGlow;
    uniform vec2  uBowPos;
    uniform float uThrust;
    uniform vec3  uFogColor;
    uniform float uFogDensity;
    varying vec3 vWorld;
    varying vec3 vN;
    varying float vH;

    const vec3 CYAN = vec3(0.184, 0.953, 1.0);
    const vec3 TEAL = vec3(0.173, 0.910, 0.690);

    float hash(vec2 p){ return fract(sin(dot(p, vec2(127.1, 311.7))) * 43758.5453); }
    float vnoise(vec2 p){
      vec2 i = floor(p), f = fract(p);
      f = f * f * (3.0 - 2.0 * f);
      return mix(mix(hash(i), hash(i + vec2(1, 0)), f.x),
                 mix(hash(i + vec2(0, 1)), hash(i + vec2(1, 1)), f.x), f.y);
    }
    float gridLine(vec2 c, float cell){
      vec2 g = abs(fract(c / cell) - 0.5) * cell;
      vec2 w = fwidth(c) * 0.9 + 0.012;
      vec2 l = 1.0 - smoothstep(vec2(0.0), w, g);
      // fade lines out where the pixel footprint gets large (grazing angles)
      float atten = exp(-max(w.x, w.y) * 13.0);
      return max(l.x, l.y) * atten;
    }
    float segDist(vec2 p, vec2 a, vec2 b){
      vec2 ab = b - a;
      float t = clamp(dot(p - a, ab) / max(dot(ab, ab), 1e-5), 0.0, 1.0);
      return length(p - (a + ab * t));
    }

    void main() {
      vec3 V = normalize(cameraPosition - vWorld);
      // micro normal detail for sparkle
      vec2 mp = vWorld.xz * 2.6 + vec2(uTime * 0.22, -uTime * 0.17);
      vec3 n = normalize(vN + vec3(vnoise(mp) - 0.5, 0.0, vnoise(mp + 17.7) - 0.5) * 0.10);

      float ndv = max(dot(n, V), 0.0);
      float fres = 0.028 + 0.972 * pow(1.0 - ndv, 5.0);

      // procedural sky reflection (matches skyShell)
      vec3 R = reflect(-V, n);
      float ry = clamp(R.y, 0.0, 1.0);
      vec3 zen = vec3(0.016, 0.027, 0.045);
      vec3 hor = vec3(0.030, 0.096, 0.128);
      vec3 skyc = mix(hor, zen, smoothstep(0.0, 0.42, ry));
      skyc += CYAN * 0.10 * exp(-ry * 18.0);              // horizon glow band

      // body colour: deep navy + teal subsurface lift on wave crests facing the light
      vec3 deep = vec3(0.020, 0.058, 0.104);
      float lift = clamp(vH * 3.2 + 0.30, 0.0, 1.0) * clamp(dot(n, uSunDir) * 0.5 + 0.5, 0.0, 1.0);
      vec3 body = mix(deep, vec3(0.031, 0.118, 0.152), lift);
      body += TEAL * 0.028 * lift;

      vec3 col = mix(body, skyc, fres);

      // key-light glint: one sharp + one broad sheen
      vec3 H = normalize(V + uSunDir);
      float ndh = max(dot(n, H), 0.0);
      col += vec3(0.81, 0.90, 1.0) * (pow(ndh, 520.0) * 1.15 + pow(ndh, 60.0) * 0.045);

      // cheap caustic shimmer
      float ca = vnoise(vWorld.xz * 0.75 + uTime * 0.10) * vnoise(vWorld.xz * 1.45 - uTime * 0.13);
      col += TEAL * pow(ca, 2.0) * 0.055;

      // ---- cyan reticle grid: skews downwind, LOCKS around the anchor ------
      float dA = length(vWorld.xz - uAnchorPos);
      float lockF = uGridLock * exp(-dA / 14.0);
      vec2 drift = uWindDir * (sin(uTime * 0.07) * 1.6 + uTime * 0.0) * uWindSkew;
      vec2 gc = vWorld.xz - drift * (1.0 - lockF);        // locked grid stays put
      float g = gridLine(gc, 1.5) * 0.16 + gridLine(gc, 7.5) * 0.45;
      float camFade = exp(-length(vWorld - cameraPosition) * 0.052);
      float gridI = g * (uGridBase + lockF * 0.55) * camFade;
      // lock ring + soft bloom at the anchor
      gridI += exp(-abs(dA - 3.5) * 7.0) * lockF * 0.22 * camFade;
      gridI += exp(-dA * 1.6) * lockF * 0.08;
      // grid dims slightly where the surface is most transparent so it never
      // tattoos the submerged pod/prop
      col += CYAN * gridI * (0.55 + 0.45 * fres);

      // tether light smear dancing on the ripples
      if (uTetherGlow > 0.001) {
        float dT = segDist(vWorld.xz + n.xz * 1.4, uTetherA, uTetherB);
        col += CYAN * uTetherGlow * exp(-dT * 2.4) * (0.5 + 0.5 * ca);
      }

      // FogExp2-matched dissolve + plane-edge fade
      float dist = length(vWorld - cameraPosition);
      float fog = 1.0 - exp(-uFogDensity * uFogDensity * dist * dist);
      col = mix(col, uFogColor, fog);
      float edge = 1.0 - smoothstep(66.0, 94.0, length(vWorld.xz));

      // ordered-ish dither to kill banding on the dark gradient
      col += (hash(gl_FragCoord.xy) - 0.5) / 160.0;

      // transparent water: near-glancing angles stay opaque/reflective (the
      // glint + horizon body survive), looking down near the boat the surface
      // opens to ~0.4 alpha so the submerged pod + prop read through it
      float alpha = mix(0.44, 0.95, fres) * edge;
      // instrument window: the surface thins a little more around the motor
      // head so the steerable head + prop stay readable even at thrust —
      // gentle now that the whole surface is transparent (the surface must
      // never vanish entirely at the beat-06 waterline camera)
      float dBow = length(vWorld.xz - uBowPos);
      alpha *= 1.0 - (0.16 + 0.20 * uThrust) * exp(-dBow * dBow / (1.4 + 5.0 * uThrust));
      gl_FragColor = vec4(col, alpha);
    }
  `,
});
const waterGeo = new THREE.PlaneGeometry(200, 200, WATER_SEGS, WATER_SEGS);
waterGeo.rotateX(-Math.PI / 2);
const water = new THREE.Mesh(waterGeo, waterMat);
water.renderOrder = 2;
scene.add(water);

// ------------------------------------------------------------- materials ----
const xrayState = { v: 0 };
const structureMats = [];   // mats that fade out in X-ray
function structureMat(opts) {
  const m = new THREE.MeshStandardMaterial(opts);
  structureMats.push(m);
  return m;
}
const hullMat   = structureMat({ color: 0x33567c, roughness: 0.5, metalness: 0.15, envMapIntensity: 1.0, side: THREE.DoubleSide });
const deckMat   = structureMat({ color: 0x1b2c40, roughness: 0.88, metalness: 0.05, envMapIntensity: 0.5 });
const rimMat    = structureMat({ color: 0x3d5a78, roughness: 0.4, metalness: 0.55, envMapIntensity: 1.0 });
const consoleMat= structureMat({ color: 0x243c55, roughness: 0.55, metalness: 0.2, envMapIntensity: 0.7 });
const seatMat   = structureMat({ color: 0x2b3f52, roughness: 0.85, metalness: 0.0 });
const metalMat  = new THREE.MeshStandardMaterial({ color: 0x55606c, roughness: 0.33, metalness: 0.85, envMapIntensity: 1.2 });
const darkPlastic = new THREE.MeshStandardMaterial({ color: 0x27313d, roughness: 0.5, metalness: 0.25, envMapIntensity: 0.8 });
// electronics detail materials — must read even in the dim hull interior
const brightMetal = new THREE.MeshStandardMaterial({ color: 0xaebbc7, roughness: 0.26, metalness: 0.9, envMapIntensity: 1.6 });
const icMat = new THREE.MeshStandardMaterial({ color: 0x151a21, roughness: 0.42, metalness: 0.15, envMapIntensity: 0.9 });
const pcbEdgeMat = new THREE.MeshStandardMaterial({ color: 0x16242f, roughness: 0.55, metalness: 0.1, emissive: 0x0d2431, emissiveIntensity: 0.5 });

// emissive cyan waterline stripe, injected in world space so it hugs the sea
const stripeU = { value: 0.20 };
hullMat.onBeforeCompile = (sh) => {
  sh.uniforms.uStripeY = stripeU;
  sh.vertexShader = sh.vertexShader
    .replace('#include <common>', '#include <common>\nvarying vec3 vWPos;')
    .replace('#include <worldpos_vertex>',
      '#include <worldpos_vertex>\nvWPos = (modelMatrix * vec4(transformed, 1.0)).xyz;');
  sh.fragmentShader = sh.fragmentShader
    .replace('#include <common>', '#include <common>\nvarying vec3 vWPos;\nuniform float uStripeY;')
    .replace('#include <emissivemap_fragment>',
      `#include <emissivemap_fragment>
       float stripe = smoothstep(0.022, 0.008, abs(vWPos.y - uStripeY));
       totalEmissiveRadiance += vec3(0.184, 0.953, 1.0) * stripe * 0.30;`);
};

// Underwater depth tint: geometry below the waterline shifts teal/dark by
// world Y — a cheap "it is UNDER the water" read now that the surface is
// transparent (no refraction/render-targets; alpha + tint is the budget).
function addUnderwaterTint(mat, tag) {
  const prev = mat.onBeforeCompile;
  mat.onBeforeCompile = (sh) => {
    if (prev) prev(sh);
    sh.vertexShader = sh.vertexShader
      .replace('#include <common>', '#include <common>\nvarying float vUwY;')
      .replace('#include <worldpos_vertex>',
        '#include <worldpos_vertex>\nvUwY = (modelMatrix * vec4(transformed, 1.0)).y;');
    sh.fragmentShader = sh.fragmentShader
      .replace('#include <common>', '#include <common>\nvarying float vUwY;')
      .replace('#include <fog_fragment>',
        `#include <fog_fragment>
         float uw = 1.0 - smoothstep(-0.55, 0.03, vUwY);
         gl_FragColor.rgb = mix(gl_FragColor.rgb,
           gl_FragColor.rgb * vec3(0.52, 0.84, 0.82) + vec3(0.016, 0.075, 0.082), uw * 0.9);`);
  };
  mat.customProgramCacheKey = () => `uw-${tag}`;
}
addUnderwaterTint(hullMat, 'hull-stripe');
addUnderwaterTint(metalMat, 'metal');
addUnderwaterTint(darkPlastic, 'plastic');

// ------------------------------------------------------------ hull loft -----
// Parametric loft: half-beam / sheer / keel curves, t: 0 = stern → 1 = bow (+X).
const HULL = { x0: -2.6, len: 5.8, halfBeam: 0.95 };
function hullCurves(t) {
  const tb = 0.42;
  let w;
  if (t < tb) w = HULL.halfBeam * (0.86 + 0.14 * THREE.MathUtils.smoothstep(t, 0, tb));
  else w = HULL.halfBeam * Math.pow(Math.cos(((t - tb) / (1 - tb)) * Math.PI / 2), 0.72);
  const s = 0.50 + 0.42 * Math.pow(t, 2.8) + 0.06 * Math.pow(1 - t, 2.2) - 0.055 * Math.sin(Math.PI * t);
  let k = -(0.16 + 0.24 * Math.pow(Math.sin(Math.PI * Math.pow(t, 0.9)), 0.8));
  k *= 1 - THREE.MathUtils.smoothstep(t, 0.8, 1.0) * 0.55;
  return { w, s, k, x: HULL.x0 + HULL.len * t };
}
function sectionPoint(t, v) {          // v ∈ [-1, 1]: -1 port sheer, 0 keel, +1 stbd sheer
  const { w, s, k, x } = hullCurves(t);
  const u = Math.abs(v);
  const z = Math.sign(v) * w * Math.pow(Math.sin(u * Math.PI / 2), 0.85) * (1 + 0.08 * u * u * u);
  const y = k + (s - k) * Math.pow(u, 1.55);
  return new THREE.Vector3(x, y, z);
}
function buildHullGeometry(NS = 48, NP = 20) {
  const cols = 2 * NP + 1;
  const pos = [], uv = [], idx = [];
  for (let i = 0; i <= NS; i++) {
    const t = i / NS;
    for (let j = 0; j < cols; j++) {
      const v = (j / (cols - 1)) * 2 - 1;
      const p = sectionPoint(t, v);
      pos.push(p.x, p.y, p.z);
      uv.push(t, j / (cols - 1));
    }
  }
  for (let i = 0; i < NS; i++) for (let j = 0; j < cols - 1; j++) {
    const a = i * cols + j, b = a + cols;
    idx.push(a, a + 1, b, b, a + 1, b + 1);
  }
  const g = new THREE.BufferGeometry();
  g.setAttribute('position', new THREE.Float32BufferAttribute(pos, 3));
  g.setAttribute('uv', new THREE.Float32BufferAttribute(uv, 2));
  g.setIndex(idx);
  g.computeVertexNormals();
  return g;
}
function buildTransomGeometry(NP = 20) {
  const cols = 2 * NP + 1;
  const pts = [];
  for (let j = 0; j < cols; j++) pts.push(sectionPoint(0, (j / (cols - 1)) * 2 - 1));
  const c = hullCurves(0);
  const center = new THREE.Vector3(c.x, (c.k + c.s) * 0.5, 0);
  const pos = [center.x, center.y, center.z];
  for (const p of pts) pos.push(p.x, p.y, p.z);
  const idx = [];
  for (let j = 0; j < cols - 1; j++) idx.push(0, j + 1, j + 2);
  const g = new THREE.BufferGeometry();
  g.setAttribute('position', new THREE.Float32BufferAttribute(pos, 3));
  g.setIndex(idx);
  g.computeVertexNormals();
  return g;
}
function deckHalfWidthAt(t, yDeck) {
  const { w, s, k } = hullCurves(t);
  const u = Math.pow(THREE.MathUtils.clamp((yDeck - k) / (s - k), 0, 1), 1 / 1.55);
  return w * Math.pow(Math.sin(u * Math.PI / 2), 0.85) * (1 + 0.08 * u ** 3) * 0.985;
}
function buildDeckGeometry(NS = 40, y = null, t0 = 0.005, t1 = 0.985, inset = 1.0) {
  // y === null → follow the sheer (legacy full deck); fixed y → flat sole/platform
  const pos = [], idx = [];
  for (let i = 0; i <= NS; i++) {
    const t = t0 + (t1 - t0) * (i / NS);
    const { s, x } = hullCurves(t);
    const yy = y === null ? s - 0.11 : y;
    const hw = deckHalfWidthAt(t, yy) * inset;
    pos.push(x, yy, -hw, x, yy, hw);
  }
  for (let i = 0; i < NS; i++) {
    const a = i * 2;
    idx.push(a, a + 1, a + 2, a + 1, a + 3, a + 2);
  }
  const g = new THREE.BufferGeometry();
  g.setAttribute('position', new THREE.Float32BufferAttribute(pos, 3));
  g.setIndex(idx);
  g.computeVertexNormals();
  return g;
}

const boat = new THREE.Group();
scene.add(boat);

const hullGeo = buildHullGeometry();
const hullSolid = new THREE.Mesh(hullGeo, hullMat);
boat.add(hullSolid);
const transom = new THREE.Mesh(buildTransomGeometry(), hullMat);
boat.add(transom);
// open skiff: a low flat sole (floorboards) instead of a raised deck, plus a
// small bow casting platform that carries the thrust driver
const SOLE_Y = 0.17;
const sole = new THREE.Mesh(buildDeckGeometry(44, SOLE_Y, 0.02, 0.93, 0.97), deckMat);
boat.add(sole);
const bowPlatform = new THREE.Mesh(buildDeckGeometry(12, 0.42, 0.755, 0.975, 0.98), consoleMat);
boat.add(bowPlatform);

// gunwale rim: tube along the sheer line (port stern → bow → stbd stern)
{
  const pts = [];
  const N = 30;
  for (let i = 0; i <= N; i++) pts.push(sectionPoint(i / N, -1));
  for (let i = N - 1; i >= 0; i--) pts.push(sectionPoint(i / N, 1));
  const curve = new THREE.CatmullRomCurve3(pts, true, 'catmullrom', 0.1);
  const rim = new THREE.Mesh(new THREE.TubeGeometry(curve, 140, 0.035, 8, true), rimMat);
  boat.add(rim);
}

// ---------------------------------------------------- open tiller skiff -----
// Plain jon-style layout: bench thwarts, a static stern tiller outboard
// (decorative — the contrast with the smart bow motor), and a compact helm
// enclosure beside the stern bench where the battery lives.
const benchMat = structureMat({ color: 0x2e4256, roughness: 0.78, metalness: 0.12, envMapIntensity: 0.5 });
function bench(t, y = 0.36) {
  const { x } = hullCurves(t);
  const hw = deckHalfWidthAt(t, y) * 0.96;
  const g = new THREE.Group();
  const board = new THREE.Mesh(new THREE.BoxGeometry(0.32, 0.05, hw * 2), benchMat);
  board.position.set(x, y - 0.025, 0);
  g.add(board);
  for (const s of [-1, 1]) {   // simple knee brackets under the ends
    const knee = new THREE.Mesh(new THREE.BoxGeometry(0.26, 0.10, 0.03), seatMat);
    knee.position.set(x, y - 0.10, s * (hw - 0.10));
    g.add(knee);
  }
  return g;
}
boat.add(bench(0.115), bench(0.42), bench(0.675));

// helm enclosure (the board's new home, stern) + battery box under the bench
const helmMount = new THREE.Group();
{
  const encl = new THREE.Mesh(new THREE.BoxGeometry(0.40, 0.15, 0.30), consoleMat);
  encl.position.y = 0.075;
  const lip = new THREE.Mesh(new THREE.BoxGeometry(0.42, 0.022, 0.32), rimMat);
  lip.position.y = 0.148;
  helmMount.add(encl, lip);
  const batt = new THREE.Mesh(new THREE.BoxGeometry(0.30, 0.15, 0.20), seatMat);
  batt.position.set(-0.36, 0.075, 0.03);
  const battCapP = new THREE.Mesh(new THREE.CylinderGeometry(0.02, 0.02, 0.03, 8), darkPlastic);
  battCapP.position.set(-0.44, 0.16, 0.08);
  const battCapN = battCapP.clone();
  battCapN.position.set(-0.28, 0.16, 0.08);
  helmMount.add(batt, battCapP, battCapN);
}
helmMount.position.set(-1.46, SOLE_Y, 0.12);
boat.add(helmMount);

// static tiller outboard on the transom — simple fishing boat, honest steering
const tillerMat = structureMat({ color: 0x22303e, roughness: 0.55, metalness: 0.25, envMapIntensity: 0.7 });
{
  const tiller = new THREE.Group();
  const clamp = new THREE.Mesh(new THREE.BoxGeometry(0.12, 0.17, 0.15), tillerMat);
  clamp.position.set(-0.02, 0.47, 0);
  const cowl = new THREE.Mesh(new THREE.BoxGeometry(0.32, 0.21, 0.19), tillerMat);
  cowl.position.set(-0.22, 0.60, 0);
  const cowlCap = new THREE.Mesh(new THREE.BoxGeometry(0.27, 0.055, 0.155), seatMat);
  cowlCap.position.set(-0.22, 0.732, 0);
  const leg = new THREE.Mesh(new THREE.CylinderGeometry(0.032, 0.038, 0.56, 10), metalMat);
  leg.position.set(-0.22, 0.21, 0);
  const lower = new THREE.Mesh(new THREE.CapsuleGeometry(0.045, 0.14, 4, 10), darkPlastic);
  lower.rotation.z = Math.PI / 2;
  lower.position.set(-0.24, -0.075, 0);
  const skeg = new THREE.Mesh(new THREE.BoxGeometry(0.10, 0.09, 0.012), metalMat);
  skeg.position.set(-0.27, -0.15, 0);
  const tProp = new THREE.Group();                    // static, never running
  const tHub = new THREE.Mesh(new THREE.CylinderGeometry(0.018, 0.022, 0.05, 8), metalMat);
  tHub.rotation.z = Math.PI / 2;
  tProp.add(tHub);
  for (const a of [0.9, 0.9 + Math.PI]) {
    const bl = new THREE.Mesh(new THREE.BoxGeometry(0.012, 0.10, 0.045), metalMat);
    bl.rotation.x = a;
    bl.position.y = 0;
    tProp.add(bl);
  }
  tProp.position.set(-0.345, -0.075, 0);
  // tiller arm angled forward over the stern bench, grip to port
  const arm = new THREE.Mesh(new THREE.CylinderGeometry(0.015, 0.019, 0.52, 8), tillerMat);
  arm.rotation.z = -Math.PI / 2 - 0.13;
  arm.rotation.y = 0.24;
  arm.position.set(0.17, 0.585, -0.06);
  const grip = new THREE.Mesh(new THREE.CylinderGeometry(0.024, 0.024, 0.14, 10), seatMat);
  grip.rotation.z = -Math.PI / 2 - 0.13;
  grip.rotation.y = 0.24;
  grip.position.set(0.415, 0.552, -0.122);
  tiller.add(clamp, cowl, cowlCap, leg, lower, skeg, tProp, arm, grip);
  tiller.position.set(-2.60, 0, 0);
  boat.add(tiller);
}

// ------------------------------------------------------ PCBs (the boards) ---
function seeded(seed) { let a = seed; return () => (a = (a * 1664525 + 1013904223) >>> 0, a / 4294967296); }
const MONO = '"JetBrains Mono", ui-monospace, Menlo, Consolas, monospace';

// Face art: navy soldermask (never black), glowing cyan traces, silkscreen
// courtyards that match the 3D component blocks, and a BIG centered title
// that must read at the beat-04/06 camera distance. The emissive map carries
// a faint full-face wash so the board reads even in the unlit hull interior.
function boardTextures(title, seed, fat, prints, subs, band) {
  const W = 1024, H = 672, rnd = seeded(seed);
  const mapCv = document.createElement('canvas'); mapCv.width = W; mapCv.height = H;
  const emCv = document.createElement('canvas'); emCv.width = W; emCv.height = H;
  const m = mapCv.getContext('2d'), e = emCv.getContext('2d');
  m.fillStyle = '#13202e'; m.fillRect(0, 0, W, H);
  const pour = m.createLinearGradient(0, 0, W, H);
  pour.addColorStop(0, 'rgba(52,130,160,0.14)');
  pour.addColorStop(0.55, 'rgba(8,14,22,0.0)');
  pour.addColorStop(1, 'rgba(30,90,110,0.10)');
  m.fillStyle = pour; m.fillRect(0, 0, W, H);
  e.fillStyle = '#000000'; e.fillRect(0, 0, W, H);
  e.fillStyle = 'rgba(16,36,50,0.60)'; e.fillRect(0, 0, W, H);   // holographic backlight
  // trace routes (45° manhattan), visible on the map AND glowing on emissive
  const route = () => {
    let x = 40 + rnd() * (W - 80), y = 40 + rnd() * (H - 80);
    const p = [[x, y]];
    for (let s = 0; s < 3 + rnd() * 3; s++) {
      const dir = Math.floor(rnd() * 8) * Math.PI / 4;
      const d = 60 + rnd() * 200;
      x = THREE.MathUtils.clamp(x + Math.cos(dir) * d, 26, W - 26);
      y = THREE.MathUtils.clamp(y + Math.sin(dir) * d, 26, H - 26);
      p.push([x, y]);
    }
    return p;
  };
  for (let i = 0; i < (fat ? 18 : 28); i++) {
    const p = route(), lw = fat && i < 5 ? 20 : 4.2;
    for (const [ctx, style, w2] of [[m, '#265a6e', lw], [e, `rgba(47,214,255,${fat && i < 5 ? 0.5 : 0.75})`, lw]]) {
      ctx.strokeStyle = style; ctx.lineWidth = w2; ctx.lineJoin = 'round'; ctx.lineCap = 'round';
      ctx.beginPath(); ctx.moveTo(p[0][0], p[0][1]);
      for (let j = 1; j < p.length; j++) ctx.lineTo(p[j][0], p[j][1]);
      ctx.stroke();
    }
    e.fillStyle = 'rgba(47,214,255,0.9)';
    for (const [px, py] of p) { e.beginPath(); e.arc(px, py, lw * 0.55 + 2.5, 0, 7); e.fill(); }
  }
  // silkscreen perimeter + corner mounting holes
  m.strokeStyle = 'rgba(234,242,251,0.6)'; m.lineWidth = 4;
  m.strokeRect(12, 12, W - 24, H - 24);
  for (const [hx, hy] of [[34, 34], [W - 34, 34], [34, H - 34], [W - 34, H - 34]]) {
    m.fillStyle = '#070b10'; m.beginPath(); m.arc(hx, hy, 15, 0, 7); m.fill();
    m.strokeStyle = 'rgba(234,242,251,0.6)'; m.lineWidth = 3;
    m.beginPath(); m.arc(hx, hy, 19, 0, 7); m.stroke();
  }
  // component courtyards (dashed silkscreen) + reference designators,
  // aligned with the 3D blocks: u = (x/w+0.5)*W, v = (z/d+0.5)*H
  const uOf = (x, bw) => (x / bw + 0.5) * W, vOf = (z, bd) => (z / bd + 0.5) * H;
  m.setLineDash([10, 7]);
  for (const pr of prints) {
    const u0 = uOf(pr.x - pr.w / 2, pr.bw), v0 = vOf(pr.z - pr.d / 2, pr.bd);
    const u1 = uOf(pr.x + pr.w / 2, pr.bw), v1 = vOf(pr.z + pr.d / 2, pr.bd);
    m.strokeStyle = 'rgba(234,242,251,0.55)'; m.lineWidth = 3;
    m.strokeRect(u0, v0, u1 - u0, v1 - v0);
    m.setLineDash([]);
    m.font = `600 26px ${MONO}`; m.fillStyle = 'rgba(234,242,251,0.75)';
    m.textAlign = 'left'; m.textBaseline = 'bottom';
    m.fillText(pr.ref, u0 + 2, v0 - 4);
    m.setLineDash([10, 7]);
  }
  m.setLineDash([]);
  // clear band + BIG centered silkscreen title — the hero read. `band` narrows
  // the strip (e.g. clear of the driver's terminal block so 'v1.1' never hides
  // behind it at close cameras); the font auto-fits the strip.
  const bandH = 118;
  const bx0 = band ? band[0] * W : 20, bx1 = band ? band[1] * W : W - 20;
  m.fillStyle = 'rgba(9,15,22,0.92)'; m.fillRect(bx0, H / 2 - bandH / 2, bx1 - bx0, bandH);
  m.strokeStyle = 'rgba(47,214,255,0.35)'; m.lineWidth = 2;
  m.strokeRect(bx0, H / 2 - bandH / 2, bx1 - bx0, bandH);
  e.fillStyle = 'rgba(0,0,0,0.85)'; e.fillRect(bx0, H / 2 - bandH / 2, bx1 - bx0, bandH);
  let tPx = title.length > 18 ? 76 : 84;
  m.font = `700 ${tPx}px ${MONO}`;
  while (tPx > 40 && m.measureText(title).width > bx1 - bx0 - 30) {
    tPx -= 2; m.font = `700 ${tPx}px ${MONO}`;
  }
  const tFont = m.font, tCx = (bx0 + bx1) / 2;
  m.textAlign = 'center'; m.textBaseline = 'middle';
  m.fillStyle = '#eaf2fb';
  m.fillText(title, tCx, H / 2 + 2);
  e.textAlign = 'center'; e.textBaseline = 'middle';
  e.fillStyle = 'rgba(230,245,252,0.85)'; e.font = tFont;   // self-lit silkscreen
  e.fillText(title, tCx, H / 2 + 2);
  // connector labels along the bottom edge
  m.textAlign = 'left'; m.textBaseline = 'alphabetic';
  m.font = `500 30px ${MONO}`; m.fillStyle = 'rgba(234,242,251,0.85)';
  e.textAlign = 'left'; e.textBaseline = 'alphabetic';
  e.font = m.font; e.fillStyle = 'rgba(230,245,252,0.4)';
  subs.forEach((s2, i) => {
    m.fillText(s2, 48 + i * ((W - 96) / subs.length), H - 40);
    e.fillText(s2, 48 + i * ((W - 96) / subs.length), H - 40);
  });
  const mk = (cv) => { const t = new THREE.CanvasTexture(cv); t.colorSpace = THREE.SRGBColorSpace; t.anisotropy = 8; return t; };
  return { map: mk(mapCv), emissive: mk(emCv) };
}
const boardFaceMats = [];
function makeBoard({ title, seed, w, d, fat, prints, subs, band }) {
  const g = new THREE.Group();
  for (const pr of prints) { pr.bw = w; pr.bd = d; }
  const tex = boardTextures(title, seed, fat, prints, subs, band);
  const body = new THREE.Mesh(new THREE.BoxGeometry(w, 0.016, d), pcbEdgeMat);
  g.add(body);
  const faceMat = new THREE.MeshStandardMaterial({
    map: tex.map, emissiveMap: tex.emissive, emissive: 0xffffff, emissiveIntensity: 1.0,
    roughness: 0.45, metalness: 0.1,
  });
  boardFaceMats.push(faceMat);
  const face = new THREE.Mesh(new THREE.PlaneGeometry(w, d), faceMat);
  face.rotation.x = -Math.PI / 2;
  face.position.y = 0.0086;
  g.add(face);
  return g;
}

// labelled component tops (IC packages) — off-white part number, self-lit
const compLabelMats = [];
function labelTex(label, sub) {
  return canvasTex(256, 176, (ctx, w, h) => {
    ctx.fillStyle = '#0c1117'; ctx.fillRect(0, 0, w, h);
    ctx.fillStyle = 'rgba(234,242,251,0.92)';
    ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
    ctx.font = `700 ${label.length > 6 ? 46 : 56}px ${MONO}`;
    ctx.fillText(label, w / 2, sub ? h * 0.40 : h * 0.5);
    if (sub) {
      ctx.font = `500 30px ${MONO}`;
      ctx.fillStyle = 'rgba(234,242,251,0.6)';
      ctx.fillText(sub, w / 2, h * 0.72);
    }
    ctx.fillStyle = 'rgba(234,242,251,0.7)';
    ctx.beginPath(); ctx.arc(24, 24, 8, 0, 7); ctx.fill();   // pin-1 dot
  });
}
function chip({ w, h, d, label, sub, tab }) {
  const g = new THREE.Group();
  g.add(new THREE.Mesh(new THREE.BoxGeometry(w, h, d), icMat));
  const t = labelTex(label, sub);
  const mat = new THREE.MeshStandardMaterial({
    map: t, emissiveMap: t, emissive: 0xffffff, emissiveIntensity: 0.5,
    roughness: 0.5, metalness: 0.1,
  });
  compLabelMats.push(mat);
  const top = new THREE.Mesh(new THREE.PlaneGeometry(w * 0.94, d * 0.94), mat);
  top.rotation.x = -Math.PI / 2;
  top.position.y = h / 2 + 0.0006;
  g.add(top);
  if (tab) {   // exposed metal tab, power-package style
    const tb = new THREE.Mesh(new THREE.BoxGeometry(w * 0.86, h * 0.4, 0.007), brightMetal);
    tb.position.set(0, h * 0.12, -d / 2 - 0.0035);
    g.add(tb);
  }
  return g;
}

// HELM BOARD — vanchor-helm v4.2 (Pi/Pico carrier). Mounted on the stern
// helm enclosure beside the battery, face tilted up toward the bow.
const helmBoard = makeBoard({
  title: 'VANCHOR-HELM v4.2', seed: 42, w: 0.34, d: 0.22, fat: false,
  subs: ['GPS', 'IMU', 'PWM OUT', 'SERVO'],
  prints: [
    { x: -0.06, z: -0.068, w: 0.112, d: 0.068, ref: 'U1' },
    { x: 0.05, z: -0.07, w: 0.064, d: 0.032, ref: 'U2' },
    { x: 0.125, z: -0.062, w: 0.052, d: 0.052, ref: 'RF1' },
    { x: 0.152, z: 0.035, w: 0.026, d: 0.148, ref: 'J1' },
  ],
});
{
  const pi = chip({ w: 0.105, h: 0.02, d: 0.062, label: 'PI CM4', sub: 'CARRIER' });
  pi.position.set(-0.06, 0.018, -0.068);
  const pico = chip({ w: 0.058, h: 0.012, d: 0.026, label: 'PICO' });
  pico.position.set(0.05, 0.014, -0.07);
  const rf = new THREE.Mesh(new THREE.BoxGeometry(0.045, 0.013, 0.045), brightMetal);
  rf.position.set(0.125, 0.014, -0.062);
  const hdr = new THREE.Mesh(new THREE.BoxGeometry(0.02, 0.02, 0.14), icMat);
  hdr.position.set(0.152, 0.017, 0.035);
  helmBoard.add(pi, pico, rf, hdr);
  addGlowSprite(helmBoard, C.cyan, 0.07, new THREE.Vector3(-0.145, 0.02, 0.09), 0.9); // status LED
  helmBoard.position.set(0.02, 0.21, 0);   // atop the stern enclosure lid
  helmBoard.rotation.z = -0.22;            // face tipped toward the bow camera
  helmBoard.rotation.x = 0.38;             // …and rolled to the beat-01 dolly side
}
helmMount.add(helmBoard);

// THRUST DRIVER — vanchor-thrust v1.1 (BTN8982 H-bridge + heatsink), bow locker.
// Mounted on a tilted bulkhead tray so beat-06 cameras see the face, not an edge.
const thrustDriver = makeBoard({
  title: 'VANCHOR-THRUST v1.1', seed: 11, w: 0.40, d: 0.26, fat: true,
  band: [0.10, 0.79],     // title strip ends clear of the X1 terminal block
  subs: ['PWM IN', 'SENSE OUT', 'BAT +', 'MOTOR'],
  prints: [
    { x: -0.02, z: -0.075, w: 0.19, d: 0.102, ref: 'HS1' },
    { x: -0.045, z: 0.062, w: 0.066, d: 0.05, ref: 'U1' },
    { x: 0.035, z: 0.062, w: 0.066, d: 0.05, ref: 'U2' },
    { x: 0.117, z: 0.062, w: 0.062, d: 0.05, ref: 'C1·C2' },
    { x: 0.166, z: -0.03, w: 0.054, d: 0.118, ref: 'X1' },
    { x: -0.182, z: 0.0, w: 0.032, d: 0.124, ref: 'J1' },
  ],
});
let trayFrameMat;
{
  // 2× BTN8982 half-bridge power packages, metal tabs toward the heatsink
  const u1 = chip({ w: 0.062, h: 0.015, d: 0.046, label: 'BTN8982', sub: 'H-BRIDGE', tab: true });
  u1.position.set(-0.045, 0.016, 0.062);
  const u2 = chip({ w: 0.062, h: 0.015, d: 0.046, label: 'BTN8982', sub: 'H-BRIDGE', tab: true });
  u2.position.set(0.035, 0.016, 0.062);
  // finned aluminium heatsink across the port half
  const hsG = new THREE.Group();
  hsG.add(new THREE.Mesh(new THREE.BoxGeometry(0.185, 0.01, 0.098), brightMetal));
  for (let i = 0; i < 9; i++) {
    const fin = new THREE.Mesh(new THREE.BoxGeometry(0.185, 0.05, 0.0045), brightMetal);
    fin.position.set(0, 0.028, -0.0465 + i * 0.0116);
    hsG.add(fin);
  }
  hsG.position.set(-0.02, 0.014, -0.075);
  // heavy screw-terminal block at the bow edge (the two motor cables land here)
  const term = new THREE.Group();
  const termBody = new THREE.Mesh(new THREE.BoxGeometry(0.05, 0.034, 0.114), icMat);
  term.add(termBody);
  for (const zz of [-0.03, 0.03]) {
    const screw = new THREE.Mesh(new THREE.CylinderGeometry(0.012, 0.012, 0.008, 12), brightMetal);
    screw.position.set(0, 0.021, zz);
    term.add(screw);
  }
  term.position.set(0.166, 0.024, -0.03);
  // 8-pin ribbon header at the stern edge
  const hdr = new THREE.Mesh(new THREE.BoxGeometry(0.028, 0.02, 0.118), icMat);
  hdr.position.set(-0.182, 0.017, 0);
  const hdrLip = new THREE.Mesh(new THREE.BoxGeometry(0.006, 0.012, 0.104), brightMetal);
  hdrLip.position.set(-0.194, 0.019, 0);
  // 2× bulk electrolytics
  const capG = new THREE.Group();
  for (const xx of [0.102, 0.134]) {
    const can = new THREE.Mesh(new THREE.CylinderGeometry(0.0135, 0.0135, 0.036, 12), brightMetal);
    can.position.set(xx, 0.026, 0.062);
    const sleeve = new THREE.Mesh(new THREE.CylinderGeometry(0.0142, 0.0142, 0.026, 12), icMat);
    sleeve.position.set(xx, 0.022, 0.062);
    capG.add(can, sleeve);
  }
  thrustDriver.add(u1, u2, hsG, term, hdr, hdrLip, capG);
  addGlowSprite(thrustDriver, C.teal, 0.06, new THREE.Vector3(0.175, 0.02, 0.108), 0.8); // status LED
  addGlowSprite(thrustDriver, C.cyan, 0.5, new THREE.Vector3(0.0, 0.12, 0), 0.16);       // locker backlight pool
  // mounting tray + emissive edge frame (reads as an installed unit in X-ray)
  const tray = new THREE.Mesh(new THREE.BoxGeometry(0.46, 0.008, 0.31), darkPlastic);
  tray.position.y = -0.013;
  thrustDriver.add(tray);
  trayFrameMat = new THREE.LineBasicMaterial({
    color: C.cyan, transparent: true, opacity: 0,
    blending: THREE.AdditiveBlending, depthWrite: false,
  });
  const fw2 = 0.23, fd2 = 0.155;
  const frame = new THREE.LineLoop(new THREE.BufferGeometry().setFromPoints([
    new THREE.Vector3(-fw2, 0, -fd2), new THREE.Vector3(fw2, 0, -fd2),
    new THREE.Vector3(fw2, 0, fd2), new THREE.Vector3(-fw2, 0, fd2),
  ]), trayFrameMat);
  frame.position.y = -0.008;
  frame.renderOrder = 5;
  thrustDriver.add(frame);
  thrustDriver.position.set(2.0, 0.46, 0);
  thrustDriver.rotation.z = 0.38;   // face tilted up-stern toward the beat-06 camera
}
boat.add(thrustDriver);

// ----------------------------------------------------------------- cables ---
function offsetCurvePoints(curve, side, lift, n = 64) {
  const pts = [];
  const up = new THREE.Vector3(0, 1, 0);
  for (let i = 0; i <= n; i++) {
    const t = i / n;
    const p = curve.getPoint(t);
    const tan = curve.getTangent(t);
    const s = new THREE.Vector3().crossVectors(up, tan).normalize();
    pts.push(p.clone().addScaledVector(s, side).add(new THREE.Vector3(0, lift, 0)));
  }
  return new THREE.CatmullRomCurve3(pts);
}
function mergedTubes(curves, r, mat, segs = 48) {
  const geos = curves.map((c) => new THREE.TubeGeometry(c, segs, r, 6, false));
  // manual merge (no BufferGeometryUtils vendored)
  let vtx = 0, itx = 0;
  for (const g of geos) { vtx += g.attributes.position.count; itx += g.index.count; }
  const pos = new Float32Array(vtx * 3), nor = new Float32Array(vtx * 3);
  const idx = new Uint32Array(itx);
  let vo = 0, io = 0;
  for (const g of geos) {
    pos.set(g.attributes.position.array, vo * 3);
    nor.set(g.attributes.normal.array, vo * 3);
    const gi = g.index.array;
    for (let i = 0; i < gi.length; i++) idx[io + i] = gi[i] + vo;
    vo += g.attributes.position.count; io += gi.length;
    g.dispose();
  }
  const g = new THREE.BufferGeometry();
  g.setAttribute('position', new THREE.BufferAttribute(pos, 3));
  g.setAttribute('normal', new THREE.BufferAttribute(nor, 3));
  g.setIndex(new THREE.BufferAttribute(idx, 1));
  return new THREE.Mesh(g, mat);
}
// Cable grade: dark jackets with a restrained BLUE-cyan glow — the command
// path stays cyan (never the grass-green of an over-driven teal/cyan emissive)
// and adjacent ribbon wires alternate light/dark so the 8 conductors count.
const CABLE_GLOW = 0x2fd4ff;
const cableMatA = new THREE.MeshStandardMaterial({ color: 0x39506a, roughness: 0.55, metalness: 0.1, envMapIntensity: 0.5, emissive: CABLE_GLOW, emissiveIntensity: 0.10 });
const cableMatB = new THREE.MeshStandardMaterial({ color: 0x11161e, roughness: 0.6, metalness: 0.1, envMapIntensity: 0.4, emissive: CABLE_GLOW, emissiveIntensity: 0.04 });
const heavyMat = new THREE.MeshStandardMaterial({ color: 0x171d25, roughness: 0.45, metalness: 0.2, envMapIntensity: 0.6, emissive: CABLE_GLOW, emissiveIntensity: 0.0 });

// 8-wire ribbon: stern helm → bow thrust driver (PWM forward, current-sense
// back) — the full-length signal path, run along the starboard hull side;
// lands on the J1 header at the stern edge of the tilted driver board
const cable8Path = new THREE.CatmullRomCurve3([
  new THREE.Vector3(-1.30, 0.335, 0.16),
  new THREE.Vector3(-1.02, 0.20, 0.42),
  new THREE.Vector3(-0.30, 0.155, 0.54),
  new THREE.Vector3(0.60, 0.155, 0.54),
  new THREE.Vector3(1.32, 0.20, 0.40),
  new THREE.Vector3(1.70, 0.335, 0.10),
  new THREE.Vector3(1.83, 0.412, 0.0),
]);
const cable8Curves = [];   // all 8 conductor curves, for the pulse system
{
  const even = [], odd = [];
  for (let i = 0; i < 8; i++) {
    const off = (i - 3.5) * 0.013;
    const c = offsetCurvePoints(cable8Path, off, 0);
    cable8Curves.push(c);
    (i % 2 ? odd : even).push(c);
  }
  boat.add(mergedTubes(even, 0.0045, cableMatA), mergedTubes(odd, 0.0045, cableMatB));
}
// thin servo lead: stern helm → bow steering collar, along the port side
const servoLeadPath = new THREE.CatmullRomCurve3([
  new THREE.Vector3(-1.34, 0.335, 0.02),
  new THREE.Vector3(-0.95, 0.19, -0.38),
  new THREE.Vector3(0.20, 0.155, -0.50),
  new THREE.Vector3(1.50, 0.20, -0.42),
  new THREE.Vector3(2.50, 0.56, -0.22),
  new THREE.Vector3(2.95, 0.84, -0.06),
  new THREE.Vector3(3.22, 0.92, 0.0),
]);
boat.add(mergedTubes([servoLeadPath], 0.006, cableMatA, 64));
// two heavy power cables: driver X1 terminals → motor head
const motorCablePaths = [1, -1].map((s) => new THREE.CatmullRomCurve3([
  new THREE.Vector3(2.14, 0.545, -0.03 + 0.06 * (s > 0 ? 1 : 0)),
  new THREE.Vector3(2.5, 0.60, 0.07 * s),
  new THREE.Vector3(2.9, 0.84, 0.06 * s),
  new THREE.Vector3(3.18, 0.97, 0.035 * s),
  new THREE.Vector3(3.28, 1.0, 0.02 * s),
]));
boat.add(mergedTubes(motorCablePaths, 0.016, heavyMat, 48));

// ---------------------------------------------- bow trolling motor + servo --
const bowArm = new THREE.Group();
bowArm.position.set(2.55, 0.695, 0);
boat.add(bowArm);
{
  // machined mount: base plate with bolt heads, tapered arm with a top rib,
  // an underside gusset at the root and a pivot boss under the collar —
  // beat 05/06 cameras dwell here, so it earns real detailing
  const plate = new THREE.Mesh(new THREE.BoxGeometry(0.46, 0.04, 0.26), darkPlastic);
  plate.position.set(0.05, 0, 0);
  bowArm.add(plate);
  for (const [bx, bz] of [[-0.13, -0.09], [0.23, -0.09], [-0.13, 0.09], [0.23, 0.09]]) {
    const bolt = new THREE.Mesh(new THREE.CylinderGeometry(0.014, 0.016, 0.014, 8), brightMetal);
    bolt.position.set(bx, 0.026, bz);
    bowArm.add(bolt);
  }
  const arm = new THREE.Mesh(new THREE.BoxGeometry(0.62, 0.05, 0.10), metalMat);
  arm.position.set(0.42, 0.05, 0);
  const armRib = new THREE.Mesh(new THREE.BoxGeometry(0.56, 0.016, 0.032), brightMetal);
  armRib.position.set(0.40, 0.083, 0);
  // root gusset: triangular web under the arm
  const gShape = new THREE.Shape();
  gShape.moveTo(0, 0); gShape.lineTo(0.24, 0); gShape.lineTo(0, -0.12); gShape.closePath();
  const gusset = new THREE.Mesh(new THREE.ExtrudeGeometry(gShape, { depth: 0.022, bevelEnabled: false }), metalMat);
  gusset.position.set(0.14, 0.028, -0.011);
  const boss = new THREE.Mesh(new THREE.CylinderGeometry(0.075, 0.088, 0.05, 16), metalMat);
  boss.position.set(0.73, 0.055, 0);
  const bossRing = new THREE.Mesh(new THREE.TorusGeometry(0.079, 0.005, 6, 24), brightMetal);
  bossRing.rotation.x = Math.PI / 2;
  bossRing.position.set(0.73, 0.082, 0);
  bowArm.add(arm, armRib, gusset, boss, bossRing);
}
const steeringGroup = new THREE.Group();      // rotation.y = azimuth
steeringGroup.position.set(0.73, 0.1, 0);     // world ≈ (3.28, 0.96, 0)
bowArm.add(steeringGroup);
{
  // shaft: collar top down into the water
  const shaft = new THREE.Mesh(new THREE.CylinderGeometry(0.042, 0.042, 1.5, 14), metalMat);
  shaft.position.y = -0.48;
  steeringGroup.add(shaft);

  // steering servo collar unit at the shaft top — machined split-clamp look:
  // flange + bolt circle, turned ribs, clamp blocks, strain-relief for the lead
  const collar = new THREE.Mesh(new THREE.CylinderGeometry(0.085, 0.095, 0.17, 20), darkPlastic);
  collar.position.y = 0.14;
  const flange = new THREE.Mesh(new THREE.CylinderGeometry(0.108, 0.112, 0.024, 20), metalMat);
  flange.position.y = 0.062;
  steeringGroup.add(flange);
  for (let i = 0; i < 6; i++) {
    const a = (i / 6) * Math.PI * 2 + 0.26;
    const bolt = new THREE.Mesh(new THREE.CylinderGeometry(0.009, 0.010, 0.012, 6), brightMetal);
    bolt.position.set(Math.cos(a) * 0.092, 0.078, Math.sin(a) * 0.092);
    steeringGroup.add(bolt);
  }
  for (const ry of [0.115, 0.155, 0.195]) {       // turned collar ribs
    const rib = new THREE.Mesh(new THREE.TorusGeometry(0.0895, 0.0035, 6, 24), metalMat);
    rib.rotation.x = Math.PI / 2;
    rib.position.y = ry;
    steeringGroup.add(rib);
  }
  for (const s of [-1, 1]) {                      // split-clamp blocks + bolts
    const block = new THREE.Mesh(new THREE.BoxGeometry(0.032, 0.055, 0.026), metalMat);
    block.position.set(0.089 * s, 0.135, 0.052 * s);
    block.rotation.y = s * 0.55;
    steeringGroup.add(block);
    const cb = new THREE.Mesh(new THREE.CylinderGeometry(0.007, 0.007, 0.062, 6), brightMetal);
    cb.position.copy(block.position);
    steeringGroup.add(cb);
  }
  const servoBox = new THREE.Mesh(new THREE.BoxGeometry(0.17, 0.11, 0.12), darkPlastic);
  servoBox.position.set(-0.125, 0.12, 0);
  for (let i = 0; i < 4; i++) {                   // servo housing cooling ribs
    const fin = new THREE.Mesh(new THREE.BoxGeometry(0.135, 0.008, 0.128), icMat);
    fin.position.set(-0.135, 0.084 + i * 0.024, 0);
    steeringGroup.add(fin);
  }
  const strainRelief = new THREE.Mesh(new THREE.CylinderGeometry(0.011, 0.015, 0.05, 8), icMat);
  strainRelief.rotation.z = Math.PI / 2 - 0.35;
  strainRelief.position.set(-0.21, 0.085, 0);
  steeringGroup.add(strainRelief);
  const collarRing = new THREE.Mesh(
    new THREE.TorusGeometry(0.098, 0.006, 8, 32),
    new THREE.MeshBasicMaterial({ color: 0x1899a8, transparent: true, opacity: 0.7, blending: THREE.AdditiveBlending, depthWrite: false })
  );
  collarRing.rotation.x = Math.PI / 2;
  collarRing.position.y = 0.065;
  collarRing.renderOrder = 6;
  // protractor arc (dim in stage 1 — it ignites in beat 05)
  const protractor = new THREE.Mesh(
    new THREE.RingGeometry(0.15, 0.168, 48, 1, -Math.PI / 3, Math.PI * 2 / 3),
    new THREE.MeshBasicMaterial({ color: 0x17747f, transparent: true, opacity: 0.35, blending: THREE.AdditiveBlending, depthWrite: false, side: THREE.DoubleSide })
  );
  protractor.rotation.x = -Math.PI / 2;
  protractor.position.y = 0.235;
  protractor.renderOrder = 6;
  steeringGroup.add(collar, servoBox, collarRing, protractor);
  // faint waterline halo where the shaft pierces the surface
  addGlowSprite(steeringGroup, C.cyan, 0.28, new THREE.Vector3(0, -0.83, 0), 0.2);
}
// head pod (just under the surface), nose forward (+X), prop astern
const headGroup = new THREE.Group();
headGroup.position.y = -1.18;                 // world ≈ y -0.35
steeringGroup.add(headGroup);
const propGroup = new THREE.Group();
const motionDiscs = [];                       // counter-rotating additive discs
const bladeMat = metalMat.clone();            // blades get their own mat so they
bladeMat.transparent = true;                  // can cross-fade into the discs
bladeMat.emissive = new THREE.Color(0x155a60); // faint self-light: the prop must
bladeMat.emissiveIntensity = 0.7;              // read through the water surface
addUnderwaterTint(bladeMat, 'blade');         // clone doesn't carry onBeforeCompile
{
  const pod = new THREE.Mesh(new THREE.CapsuleGeometry(0.095, 0.42, 6, 16), metalMat);
  pod.rotation.z = Math.PI / 2;
  const fin = new THREE.Mesh(new THREE.BoxGeometry(0.18, 0.10, 0.014), metalMat);
  fin.position.set(-0.14, -0.11, 0);
  const accent = new THREE.Mesh(
    new THREE.TorusGeometry(0.102, 0.009, 8, 32),
    new THREE.MeshBasicMaterial({ color: 0x2adbe9, transparent: true, opacity: 1.0, blending: THREE.AdditiveBlending, depthWrite: false })
  );
  accent.rotation.y = Math.PI / 2;
  accent.position.x = 0.15;
  accent.renderOrder = 1;                     // under water: draw before the surface
  headGroup.add(pod, fin, accent);
  addGlowSprite(headGroup, C.cyan, 0.3, new THREE.Vector3(0.15, 0, 0), 0.5).renderOrder = 1;

  // prop: 3 twisted blades + hub + (dormant) motion discs
  propGroup.position.x = -0.30;
  const hub = new THREE.Mesh(new THREE.CylinderGeometry(0.022, 0.028, 0.06, 10), metalMat);
  hub.rotation.z = Math.PI / 2;
  propGroup.add(hub);
  const bladeShape = new THREE.Shape();
  bladeShape.moveTo(0, 0.015);
  bladeShape.quadraticCurveTo(0.075, 0.055, 0.135, 0.02);
  bladeShape.quadraticCurveTo(0.15, 0.0, 0.135, -0.02);
  bladeShape.quadraticCurveTo(0.075, -0.045, 0, -0.015);
  bladeShape.closePath();
  const bladeGeo = new THREE.ExtrudeGeometry(bladeShape, { depth: 0.006, bevelEnabled: false });
  for (let i = 0; i < 3; i++) {
    const b = new THREE.Mesh(bladeGeo, bladeMat);
    const holder = new THREE.Group();
    b.rotation.y = 0.5;                       // blade pitch
    b.position.y = 0.02;
    holder.add(b);
    holder.rotation.x = (i / 3) * Math.PI * 2;
    propGroup.add(holder);
  }
  // motion-blur disc texture: radial falloff × 3-fold angular smear, so the
  // counter-rotation actually reads instead of a flat circle
  const discTex = canvasTex(128, 128, (ctx, w, h) => {
    const img = ctx.createImageData(w, h);
    for (let y = 0; y < h; y++) for (let x = 0; x < w; x++) {
      const dx = (x - 64) / 64, dy = (y - 64) / 64;
      const r = Math.hypot(dx, dy), th = Math.atan2(dy, dx);
      const rad = Math.max(0, 1 - r) * Math.min(1, r * 6);
      const swirl = 0.45 + 0.55 * (0.5 + 0.5 * Math.sin(3 * th + r * 5.0));
      const a = 255 * Math.pow(rad, 1.3) * swirl;
      const i = (y * w + x) * 4;
      img.data[i] = img.data[i + 1] = img.data[i + 2] = 255;
      img.data[i + 3] = a;
    }
    ctx.putImageData(img, 0, 0);
  });
  const discMat = new THREE.MeshBasicMaterial({ map: discTex, color: C.teal, transparent: true, opacity: 0, blending: THREE.AdditiveBlending, depthWrite: false, side: THREE.DoubleSide });
  for (const s of [1, -1]) {
    const disc = new THREE.Mesh(new THREE.CircleGeometry(0.16, 24), discMat.clone());
    disc.rotation.y = Math.PI / 2;
    disc.position.x = -0.03 * s;
    disc.renderOrder = 1;
    motionDiscs.push(disc);
    propGroup.add(disc);
  }
  headGroup.add(propGroup);
  // soft teal backlight at the hub so the blades silhouette under water
  addGlowSprite(headGroup, C.teal, 0.36, new THREE.Vector3(-0.34, 0, 0), 0.3).renderOrder = 1;
}

// -------------------------------------------------------- X-ray hull mode ---
// glass shell + fresnel rim
const xrayShellMat = new THREE.ShaderMaterial({
  uniforms: { uOn: { value: 0 } },
  transparent: true, depthWrite: false,
  vertexShader: /* glsl */`
    varying vec3 vN; varying vec3 vW;
    void main() {
      vN = normalize(mat3(modelMatrix) * normal);
      vec4 wp = modelMatrix * vec4(position, 1.0);
      vW = wp.xyz;
      gl_Position = projectionMatrix * viewMatrix * wp;
    }
  `,
  fragmentShader: /* glsl */`
    uniform float uOn;
    varying vec3 vN; varying vec3 vW;
    void main() {
      vec3 V = normalize(cameraPosition - vW);
      float fres = pow(1.0 - abs(dot(normalize(vN), V)), 2.2);
      vec3 col = mix(vec3(0.055, 0.13, 0.17), vec3(0.184, 0.953, 1.0), fres * 0.9);
      float a = uOn * (0.22 + 0.75 * fres);
      gl_FragColor = vec4(col, a);
    }
  `,
});
const hullXray = new THREE.Mesh(hullGeo, xrayShellMat);
hullXray.renderOrder = 4;
hullXray.scale.setScalar(1.002);
boat.add(hullXray);

// blueprint seam lines (sheer, keel, stem, a few stations)
const seamMats = [];
function seamLine(points, baseOpacity = 0.55) {
  const g = new THREE.BufferGeometry().setFromPoints(points);
  const m = new THREE.LineBasicMaterial({
    color: C.cyan, transparent: true, opacity: 0,
    blending: THREE.AdditiveBlending, depthWrite: false,
  });
  m.userData.base = baseOpacity;
  seamMats.push(m);
  const l = new THREE.Line(g, m);
  l.renderOrder = 5;
  boat.add(l);
  return l;
}
{
  const N = 40, port = [], stbd = [], keel = [];
  for (let i = 0; i <= N; i++) {
    const t = i / N;
    port.push(sectionPoint(t, -1)); stbd.push(sectionPoint(t, 1)); keel.push(sectionPoint(t, 0));
  }
  seamLine(port, 0.85); seamLine(stbd, 0.85); seamLine(keel, 0.6);
  for (const t of [0.0, 0.15, 0.35, 0.55, 0.75, 0.9]) {
    const ring = [];
    for (let j = 0; j <= 24; j++) ring.push(sectionPoint(t, (j / 24) * 2 - 1));
    seamLine(ring, t === 0 ? 0.5 : 0.3);
  }
}

window.__xray = function (v) {
  v = THREE.MathUtils.clamp(v ?? 0, 0, 1);
  xrayState.v = v;
  const solid = v < 0.001;
  for (const m of structureMats) {
    m.transparent = !solid;
    m.opacity = solid ? 1 : Math.pow(1 - v, 1.6);
    m.depthWrite = solid || v < 0.55;
    m.needsUpdate = false;
  }
  xrayShellMat.uniforms.uOn.value = v;
  for (const m of seamMats) m.opacity = v * m.userData.base;
  // featured electronics lift while the hull is ghosted — restrained, so the
  // ribbon reads as 8 countable wires (cyan command path), never a glowing hose
  cableMatA.emissiveIntensity = 0.10 + v * 0.24;
  cableMatB.emissiveIntensity = 0.04 + v * 0.12;
  heavyMat.emissiveIntensity = v * 0.06;   // heavy DC cables stay dark rubber
  for (const m of boardFaceMats) m.emissiveIntensity = 1.0 + v * 0.55;
  for (const m of compLabelMats) m.emissiveIntensity = 0.5 + v * 0.5;
  trayFrameMat.opacity = v * 0.85;
};

// ------------------------------------------------------- contact shadow -----
let contactShadow;
{
  const shadowTex = canvasTex(256, 128, (ctx, w, h) => {
    const g = ctx.createRadialGradient(w / 2, h / 2, 6, w / 2, h / 2, w / 2);
    g.addColorStop(0, 'rgba(1,3,6,0.5)');
    g.addColorStop(0.55, 'rgba(1,3,6,0.22)');
    g.addColorStop(1, 'rgba(1,3,6,0)');
    ctx.fillStyle = g; ctx.fillRect(0, 0, w, h);
  });
  const shadow = new THREE.Mesh(
    new THREE.PlaneGeometry(6.8, 2.5),
    new THREE.MeshBasicMaterial({ map: shadowTex, transparent: true, depthWrite: false, fog: false })
  );
  shadow.rotation.x = -Math.PI / 2;
  shadow.position.set(0.3, 0.165, 0);
  shadow.renderOrder = 3;
  scene.add(shadow);
  contactShadow = shadow;
}


// ============================================================================
// STAGE 2 — STORY & ANIMATION
// One deterministic clock. simState is a pure function of time (scrub-safe,
// frame-rate independent); beats own ONLY camera, captions and overlays.
// COLD_OPEN (beats 00–01, 18 s, once) → LOOP (beats 02–08, 61 s, seamless).
// ============================================================================

const RM = matchMedia('(prefers-reduced-motion: reduce)').matches;
const MOTION = RM ? 0.3 : 1;
const UP = new THREE.Vector3(0, 1, 0);
const AMBER = new THREE.Color(C.amber);

// ------------------------------------------------------------- easing -------
const clamp01 = (x) => THREE.MathUtils.clamp(x, 0, 1);
const lerp = THREE.MathUtils.lerp;
const smooth = (a, b, x) => THREE.MathUtils.smoothstep(x, a, b);
const easeIO = (u) => (u <= 0 ? 0 : u >= 1 ? 1 : u < 0.5 ? 4 * u * u * u : 1 - Math.pow(-2 * u + 2, 3) / 2);
const easeOutCubic = (u) => 1 - Math.pow(1 - clamp01(u), 3);
function easeOutBack(u, k = 1.2) { u = clamp01(u); const c3 = k + 1; return 1 + c3 * Math.pow(u - 1, 3) + k * Math.pow(u - 1, 2); }
const frac = (x) => x - Math.floor(x);
const d2r = THREE.MathUtils.degToRad, r2d = THREE.MathUtils.radToDeg;
const gauss = (x, w) => Math.exp(-(x * x) / (w * w));

function track(keys) {              // scalar keyframes, cubic-eased per segment
  const n = keys.length;
  return (t) => {
    if (t <= keys[0][0]) return keys[0][1];
    if (t >= keys[n - 1][0]) return keys[n - 1][1];
    for (let i = 1; i < n; i++) if (t < keys[i][0]) {
      const a = keys[i - 1], b = keys[i];
      return lerp(a[1], b[1], easeIO((t - a[0]) / (b[0] - a[0])));
    }
    return keys[n - 1][1];
  };
}
function vecTrack(keys) {           // [t, x, y, z]
  const n = keys.length;
  return (t, out) => {
    if (t <= keys[0][0]) { const k = keys[0]; return out.set(k[1], k[2], k[3]); }
    if (t >= keys[n - 1][0]) { const k = keys[n - 1]; return out.set(k[1], k[2], k[3]); }
    for (let i = 1; i < n; i++) if (t < keys[i][0]) {
      const a = keys[i - 1], b = keys[i];
      const u = easeIO((t - a[0]) / (b[0] - a[0]));
      return out.set(lerp(a[1], b[1], u), lerp(a[2], b[2], u), lerp(a[3], b[3], u));
    }
    return out;
  };
}

// ------------------------------------------------- wind (seeded per cycle) --
const windCache = new Map();
function windOf(cycle) {
  let w = windCache.get(cycle);
  if (!w) {
    const rnd = seeded(((cycle + 3) * 2654435761 ^ 0x9e3779b9) >>> 0);
    rnd(); rnd();
    const sgn = rnd() < 0.5 ? -1 : 1;
    const ang = sgn * d2r(28 + 52 * rnd());     // drift heads bow-diagonal
    w = { ang, dir: new THREE.Vector2(Math.cos(ang), Math.sin(ang)) };
    windCache.set(cycle, w);
  }
  return w;
}

// ----------------------------------------------------- simState tracks ------
// All in loop time lt ∈ [0, 61): b02 0–8, b03 8–18, b04 18–25, b05 25–33,
// b06 33–43, b07 43–53, b08 53–61.
const driftAt = track([[0, 0], [8.6, 0], [10.2, 0.4], [13.5, 1.7], [18, 3.2],
  [22, 3.45], [25, 3.6], [29, 3.78], [33, 3.85], [35.4, 3.9], [38.5, 3.15],
  [41.5, 2.0], [44.5, 1.0], [46.8, 0.32], [48.2, 0.02], [49.2, -0.13],
  [50.6, 0.015], [51.8, 0]]);
const thrustAt = track([[35.2, 0], [36.4, 22], [37.6, 38], [40, 37], [43.5, 34],
  [45.5, 22], [47.6, 8], [49.4, 0],
  [55.4, 0], [56.0, 5], [56.7, 0], [58.9, 0], [59.4, 4], [60.0, 0]]); // b08 puffs
const azMixAt = track([[26.5, 0], [28.4, 0.62], [30.4, 1.05], [31.5, 1],
  [51.5, 1], [53.5, 0.85], [57, 0]]);
const xrayLoopAt = track([[18.05, 0], [18.9, 0.92], [23.4, 0.92], [25.0, 0],
  [33.05, 0], [33.9, 0.88], [38.2, 0.88], [41.2, 0.30], [44.0, 0],
  [54, 0], [55.8, 0.22], [59, 0.22], [60.7, 0]]);
const xrayColdAt = track([[6.6, 0], [8.2, 0.95], [15.6, 0.95], [17.7, 0]]);
const gridLock0At = track([[0, 0.12], [1.1, 0.15], [3.0, 1]]);
const windSkewAt = track([[0, 0.12], [7, 0.12], [9.5, 0.5], [43, 0.5], [48, 0.15], [61, 0.12]]);
const streakActAt = track([[0, 0.02], [6, 0.12], [8, 0.4], [10, 1], [43, 1], [49, 0.4], [55, 0.18], [59, 0.02], [61, 0.02]]);

// prop spin: integral of spin rate over the loop, tabulated once (pure lookup)
const SPIN_STEP = 0.05, SPIN_IDLE = 1.6;
const spinTable = [0];
{
  const n = Math.round(LOOP_DUR / SPIN_STEP);
  for (let i = 1; i <= n; i++) {
    const tm = (i - 0.5) * SPIN_STEP;
    spinTable.push(spinTable[i - 1] + (SPIN_IDLE + thrustAt(tm) * 0.55) * SPIN_STEP);
  }
}
function propAngleAt(gt) {
  if (gt < COLD_OPEN) return SPIN_IDLE * gt;
  const cyc = Math.floor((gt - COLD_OPEN) / LOOP_DUR);
  const lt = gt - COLD_OPEN - cyc * LOOP_DUR;
  const x = clamp01(lt / LOOP_DUR) * (spinTable.length - 1);
  const i = Math.min(Math.floor(x), spinTable.length - 2);
  return SPIN_IDLE * COLD_OPEN + cyc * spinTable[spinTable.length - 1]
       + lerp(spinTable[i], spinTable[i + 1], x - i);
}

const ANCHOR = new THREE.Vector3(3.4, 0.02, 0);

function offsetAt(lt, cycle, out) {
  const d = driftAt(lt);
  const { dir } = windOf(cycle);
  const wob = 0.28 * Math.sin(lt * 0.34 + 1.1) * clamp01(Math.abs(d) / 3.9);
  return out.set(dir.x * d - dir.y * wob, dir.y * d + dir.x * wob);
}

const SIM = {
  gt: 0, lt: 0, cycle: 0, cold: true,
  off: new THREE.Vector2(), windDir: new THREE.Vector2(1, 0), windAng: 0,
  drift: 0, dispDrift: 0, tension: 0,
  azDeg: 0, azTargetDeg: 0, thrust: 0,
  gridLock: 0.12, xray: 0, anchorOn: 0, lockPulse: 0,
};
function simAt(gt) {
  SIM.gt = gt;
  SIM.cold = gt < COLD_OPEN;
  if (SIM.cold) { SIM.cycle = 0; SIM.lt = 0; }
  else {
    SIM.cycle = Math.floor((gt - COLD_OPEN) / LOOP_DUR);
    SIM.lt = gt - COLD_OPEN - SIM.cycle * LOOP_DUR;
  }
  const lt = SIM.lt;
  const w0 = windOf(SIM.cycle), w1 = windOf(SIM.cycle + 1);
  const wb = SIM.cold ? 0 : smooth(59.4, 61, lt);       // re-randomize at seam
  let dA = w1.ang - w0.ang;
  dA = Math.atan2(Math.sin(dA), Math.cos(dA));
  SIM.windAng = w0.ang + dA * wb;
  SIM.windDir.set(Math.cos(SIM.windAng), Math.sin(SIM.windAng));
  if (SIM.cold) { SIM.off.set(0, 0); SIM.drift = 0; }
  else { offsetAt(lt, SIM.cycle, SIM.off); SIM.drift = driftAt(lt); }
  SIM.dispDrift = Math.max(0, SIM.drift);
  SIM.tension = clamp01(SIM.dispDrift / 3.9);
  SIM.azTargetDeg = ((r2d(w0.ang) + 180 + 540) % 360) - 180;
  const micro = (1.6 * Math.sin(gt * 0.53) + 0.7 * Math.sin(gt * 1.71)) * MOTION;
  SIM.azDeg = SIM.cold ? micro * 2 : SIM.azTargetDeg * azMixAt(lt) + micro;
  SIM.thrust = SIM.cold ? 0 : thrustAt(lt);
  SIM.xray = SIM.cold ? xrayColdAt(gt) : xrayLoopAt(lt);
  SIM.gridLock = SIM.cold ? 0.12 : (SIM.cycle === 0 ? gridLock0At(lt) : 1);
  SIM.anchorOn = SIM.cold ? 0 : (SIM.cycle === 0 ? smooth(0.5, 1.3, lt) : 1);
  SIM.lockPulse = SIM.cold ? 0 : smooth(48.3, 48.65, lt) * (1 - smooth(51, 52.6, lt));
  return SIM;
}

// ============================================================ scene: story ==
// ------------------------------------------------------- anchor + reticle ---
function flatRing(rIn, rOut, segs, color, opacity, mat) {
  const m = new THREE.Mesh(
    new THREE.RingGeometry(rIn, rOut, segs),
    mat || new THREE.MeshBasicMaterial({ color, transparent: true, opacity, blending: THREE.AdditiveBlending, depthWrite: false, side: THREE.DoubleSide, fog: false })
  );
  m.rotation.x = -Math.PI / 2;
  m.renderOrder = 6;
  return m;
}
const anchorGroup = new THREE.Group();
anchorGroup.position.copy(ANCHOR);
scene.add(anchorGroup);
const retRing = flatRing(0.44, 0.52, 64, C.cyan, 0.9);
const retRing2 = flatRing(0.62, 0.638, 64, C.cyan, 0.35, retRing.material);
const retTicks = new THREE.Group();
for (let i = 0; i < 4; i++) {
  const arm = new THREE.Group();
  const tick = new THREE.Mesh(new THREE.PlaneGeometry(0.26, 0.04), retRing.material);
  tick.rotation.x = -Math.PI / 2;
  tick.position.x = 0.82;
  arm.rotation.y = (i / 4) * Math.PI * 2;
  arm.add(tick);
  retTicks.add(arm);
}
const holdCircle = flatRing(3.42, 3.5, 96, C.cyan, 0.16);
const retGlow = addGlowSprite(anchorGroup, C.cyan, 1.7, new THREE.Vector3(0, 0.06, 0), 0.45);
const stampRing = flatRing(0.44, 0.52, 48, C.cyan, 0);     // descending ghost
const rippleRing = flatRing(0.96, 1.0, 64, C.cyan, 0);     // stamp ripple
anchorGroup.add(retRing, retRing2, retTicks, holdCircle, stampRing, rippleRing);

// ping rings (beat 04 GPS pings + beat 07 LOCK flash)
const PINGS = [{ t: 19.6, teal: false }, { t: 22.6, teal: false }, { t: 48.35, teal: true }];
const pingMeshes = PINGS.map(() => {
  const r = flatRing(0.98, 1.02, 64, C.cyan, 0);
  anchorGroup.add(r);
  return r;
});

// ------------------------------------------------------- HOLDLINE tether ----
const tetherMat = new THREE.MeshBasicMaterial({ color: C.cyan, transparent: true, opacity: 0.8, blending: THREE.AdditiveBlending, depthWrite: false, fog: false });
const tetherCurve = new THREE.QuadraticBezierCurve3(new THREE.Vector3(), new THREE.Vector3(), new THREE.Vector3());
let tetherGeoCur = new THREE.TubeGeometry(tetherCurve, 40, 0.015, 6, false);
const tether = new THREE.Mesh(tetherGeoCur, tetherMat);
tether.renderOrder = 6;
tether.frustumCulled = false;
scene.add(tether);
const tetherGlowA = addGlowSprite(scene, C.cyan, 0.5, new THREE.Vector3(), 0.5);  // eyelet
const tetherGlowB = addGlowSprite(scene, C.cyan, 0.7, new THREE.Vector3(), 0.5);  // reticle

// --------------------------------------- drift vector + thrust cone (anti) --
function arrowMesh(color, shaftR, headR) {
  const g = new THREE.Group();
  const mat = new THREE.MeshBasicMaterial({ color, transparent: true, opacity: 0.6, blending: THREE.AdditiveBlending, depthWrite: false, fog: false });
  const shaft = new THREE.Mesh(new THREE.CylinderGeometry(shaftR, shaftR, 1, 8), mat);
  const head = new THREE.Mesh(new THREE.ConeGeometry(headR, 0.26, 12), mat);
  g.add(shaft, head);
  g.renderOrder = 6;
  g.userData = { shaft, head, mat };
  return g;
}
const driftArrow = arrowMesh(C.amber, 0.026, 0.09);
scene.add(driftArrow);
const driftArrowGlow = addGlowSprite(scene, C.amber, 0.9, new THREE.Vector3(), 0.30);

const thrustConeWrap = new THREE.Group();       // +Y of wrap = head nose (+X)
thrustConeWrap.rotation.z = -Math.PI / 2;
thrustConeWrap.position.set(-0.28, -0.11, 0);   // hub-rooted at the visible prop
// Directional-cone shader: fresnel rim keeps the silhouette crisp edge-on,
// longitudinal flow lines stream hub→exit so the push direction reads from
// ANY camera (beat 06's waterline truck included) — never a flat smear.
const thrustConeMat = new THREE.ShaderMaterial({
  uniforms: {
    uTime: { value: 0 },
    uAlpha: { value: 0 },
    uColor: { value: new THREE.Color(C.teal) },
  },
  transparent: true, depthWrite: false,
  blending: THREE.AdditiveBlending, side: THREE.DoubleSide,
  vertexShader: /* glsl */`
    varying vec2 vUv; varying vec3 vN; varying vec3 vW;
    void main() {
      vUv = uv;
      vN = normalize(mat3(modelMatrix) * normal);
      vec4 wp = modelMatrix * vec4(position, 1.0);
      vW = wp.xyz;
      gl_Position = projectionMatrix * viewMatrix * wp;
    }
  `,
  fragmentShader: /* glsl */`
    uniform float uTime; uniform float uAlpha; uniform vec3 uColor;
    varying vec2 vUv; varying vec3 vN; varying vec3 vW;
    void main() {
      if (uAlpha < 0.004) discard;
      vec3 V = normalize(cameraPosition - vW);
      float rim = pow(1.0 - abs(dot(normalize(vN), V)), 1.7);        // edge highlight
      // 7 longitudinal flow lines, pulsing hub (v=1) → exit (v=0)
      float lines = smoothstep(0.45, 0.92, 0.5 + 0.5 * sin(vUv.x * 43.98));
      float flow = 0.45 + 0.55 * (0.5 + 0.5 * sin((vUv.y * 2.6 + uTime * 1.9) * 6.2831853));
      float axial = smoothstep(0.02, 0.30, vUv.y);                   // soft exit fade
      float a = uAlpha * (0.26 + 0.75 * rim + 0.85 * lines * flow) * axial;
      gl_FragColor = vec4(uColor * a, a);
    }
  `,
});
const thrustCone = new THREE.Mesh(new THREE.ConeGeometry(0.17, 1, 32, 1, true), thrustConeMat);
thrustCone.rotation.z = Math.PI;                // apex at the hub
thrustCone.renderOrder = 1;
thrustConeWrap.add(thrustCone);
headGroup.add(thrustConeWrap);

// ------------------------------------------------------------ wake rings ----
const WAKE_RATE = 0.55, WAKE_LIFE = 2.4;
const wakeRings = [];
for (let i = 0; i < 6; i++) {
  const r = flatRing(0.93, 1.0, 40, C.teal, 0);
  r.visible = false;
  scene.add(r);
  wakeRings.push(r);
}
const _wOff = new THREE.Vector2();
function loopAzRadAt(lt, cycle) {               // scripted azimuth at loop time
  const tgt = ((r2d(windOf(cycle).ang) + 180 + 540) % 360) - 180;
  return d2r(tgt * azMixAt(lt));
}
function propXZAt(lt, cycle, out) {             // world xz of the prop hub
  offsetAt(lt, cycle, _wOff);
  const az = loopAzRadAt(lt, cycle);
  return out.set(3.28 + _wOff.x - Math.cos(az) * 0.30, _wOff.y - Math.sin(az) * 0.30);
}

// ------------------------------------------------------------ breadcrumbs ---
const CRUMB_N = 12, CRUMB_DT = 1.2;
const crumbs = new THREE.InstancedMesh(
  new THREE.SphereGeometry(0.055, 8, 6),
  new THREE.MeshBasicMaterial({ color: C.cyan, transparent: true, opacity: 0.55, blending: THREE.AdditiveBlending, depthWrite: false, fog: false }),
  CRUMB_N
);
crumbs.instanceMatrix.setUsage(THREE.DynamicDrawUsage);
crumbs.renderOrder = 6;
crumbs.frustumCulled = false;
scene.add(crumbs);

// ------------------------------------------------------------ wind streaks --
const STREAK_N = 30;
const streakGeo = new THREE.PlaneGeometry(1.15, 0.045);
streakGeo.rotateX(-Math.PI / 2);
const streakMat = new THREE.MeshBasicMaterial({ color: C.teal, transparent: true, opacity: 0.0, blending: THREE.AdditiveBlending, depthWrite: false });
const streaks = new THREE.InstancedMesh(streakGeo, streakMat, STREAK_N);
streaks.instanceMatrix.setUsage(THREE.DynamicDrawUsage);
streaks.renderOrder = 5;
streaks.frustumCulled = false;
scene.add(streaks);
const streakBase = [];
{
  const rnd = seeded(777);
  for (let i = 0; i < STREAK_N; i++)
    streakBase.push({ x: (rnd() - 0.5) * 44, z: (rnd() - 0.5) * 44, sp: 1.5 + rnd() * 1.6, sc: 0.7 + rnd() * 1.1 });
}

// --------------------------------------------------- servo azimuth grammar --
// Protractor arc + converging ticks live on the bowArm (they measure azimuth,
// so they must NOT rotate with the steering group).
const servoHud = new THREE.Group();
servoHud.position.set(0.73, 0.36, 0);
bowArm.add(servoHud);
const servoHudMat = new THREE.MeshBasicMaterial({ color: C.cyan, transparent: true, opacity: 0, blending: THREE.AdditiveBlending, depthWrite: false, side: THREE.DoubleSide, fog: false });
// Ticks use NORMAL blending so they read as cyan instrument marks — additive
// planes silhouetted against the horizon-glow sky washed to solid white.
const servoTickLiveMat = new THREE.MeshBasicMaterial({ color: C.cyan, transparent: true, opacity: 0, blending: THREE.NormalBlending, depthWrite: false, side: THREE.DoubleSide, fog: false });
const servoTickGhostMat = servoTickLiveMat.clone();
const protractorBase = flatRing(0.155, 0.163, 64, C.cyan, 0, servoHudMat);   // faint full circle
let sweepArc = new THREE.Mesh(new THREE.RingGeometry(0.15, 0.175, 32, 1, 0, 0.01), servoHudMat);
sweepArc.rotation.x = -Math.PI / 2;
sweepArc.renderOrder = 6;
// ghost (target) vs live (heading) ticks: thin RADIAL spokes crossing the
// protractor ring, flat on its plane — never free planes that can catch the sky.
const ghostTick = new THREE.Mesh(new THREE.RingGeometry(0.150, 0.202, 2, 1, -0.019, 0.038), servoTickGhostMat);
ghostTick.rotation.x = -Math.PI / 2;
ghostTick.renderOrder = 6;
const liveTick = new THREE.Mesh(new THREE.RingGeometry(0.148, 0.212, 2, 1, -0.025, 0.05), servoTickLiveMat);
liveTick.rotation.x = -Math.PI / 2;
liveTick.position.y = 0.004;
liveTick.renderOrder = 6;
// marker: FLAT needle triangle on the protractor plane, tip pointing outward
// (a 3D cone here silhouettes as a solid quad against the sky — never again).
const _triShape = new THREE.Shape();
_triShape.moveTo(0.046, 0); _triShape.lineTo(0, 0.015); _triShape.lineTo(0, -0.015);
const markerTriGeo = new THREE.ShapeGeometry(_triShape);
markerTriGeo.rotateX(-Math.PI / 2);            // lie in the XZ plane, tip +x
const markerTri = new THREE.Mesh(markerTriGeo, servoTickLiveMat);
markerTri.renderOrder = 6;
servoHud.add(protractorBase, sweepArc, ghostTick, liveTick, markerTri);

// ---------------------------------------------------------- signal pulses ---
// Each channel is ONE THREE.Points cloud (1 draw call): per-point brightness is
// baked into vertex colors (additive blending), hidden points parked at y −999.
const pulseGroup = new THREE.Group();
boat.add(pulseGroup);                          // cable curves are boat-local
function pulseChannel({ curves, per, color, size, rate, reverse = false, stagger = 0.37, tex = glowTex }) {
  const n = curves.length * per;
  const geo = new THREE.BufferGeometry();
  geo.setAttribute('position', new THREE.BufferAttribute(new Float32Array(n * 3), 3).setUsage(THREE.DynamicDrawUsage));
  geo.setAttribute('color', new THREE.BufferAttribute(new Float32Array(n * 3), 3).setUsage(THREE.DynamicDrawUsage));
  const mat = new THREE.PointsMaterial({
    map: tex, size: size * 2.6, vertexColors: true, transparent: true,
    blending: THREE.AdditiveBlending, depthWrite: false, sizeAttenuation: true,
  });
  const pts = new THREE.Points(geo, mat);
  pts.frustumCulled = false;
  pts.renderOrder = 7;
  pulseGroup.add(pts);
  return { curves, per, rate, reverse, stagger, geo, base: new THREE.Color(color) };
}
const _pp = new THREE.Vector3();
function updateChannel(ch, gt, env) {
  const { curves, per, rate, reverse, stagger, geo, base } = ch;
  const pos = geo.attributes.position, col = geo.attributes.color;
  let s = 0;
  for (let c = 0; c < curves.length; c++) for (let i = 0; i < per; i++, s++) {
    let o = 0;
    if (env && env.a > 0.015) {
      const ph = frac(gt * rate + i / per + c * stagger);
      if (!(ph > env.f || ph < env.g)) {
        curves[c].getPoint(reverse ? 1 - ph : ph, _pp);
        o = env.a * (0.42 + 0.38 * Math.sin(ph * Math.PI));
      }
    }
    if (o > 0) {
      pos.setXYZ(s, _pp.x, _pp.y, _pp.z);
      col.setXYZ(s, base.r * o, base.g * o, base.b * o);
    } else {
      pos.setXYZ(s, 0, -999, 0);
      col.setXYZ(s, 0, 0, 0);
    }
  }
  pos.needsUpdate = col.needsUpdate = true;
}
// signal front: pulses only exist BEHIND the traveling onset — the electronics
// visibly cause the mechanics (arrive-before-effect).
function envWin(lt, on, off, travel, amp = 1) {
  if (lt < on || lt > off + travel) return null;
  const a = amp * smooth(on, on + 0.4, lt) * (1 - smooth(off, off + travel, lt));
  return { a, f: clamp01((lt - on) / travel), g: lt > off ? clamp01((lt - off) / travel) : 0 };
}
function envMax(...list) { return list.reduce((m, x) => (!x ? m : (!m || x.a > m.a ? x : m)), null); }
const ENV_IDLE = { a: 0.16, f: 1, g: 0 };

// GPS antenna puck + short feed to the helm board
boat.updateMatrixWorld(true);
const helmLocal = new THREE.Vector3();
helmBoard.getWorldPosition(helmLocal);          // boat is at origin here
const puckPos = helmLocal.clone().add(new THREE.Vector3(-0.38, 0.23, -0.42)); // stern bench mast
{
  const mast = new THREE.Mesh(new THREE.CylinderGeometry(0.007, 0.007, 0.23, 6), darkPlastic);
  mast.position.copy(puckPos).y -= 0.115;
  const puck = new THREE.Mesh(new THREE.CylinderGeometry(0.03, 0.036, 0.022, 12), darkPlastic);
  puck.position.copy(puckPos);
  boat.add(mast, puck);
  addGlowSprite(boat, C.cyan, 0.045, puckPos.clone().add(new THREE.Vector3(0, 0.02, 0)), 0.6);
}
const gpsPath = new THREE.CatmullRomCurve3([
  puckPos.clone(),
  puckPos.clone().lerp(helmLocal, 0.5).add(new THREE.Vector3(0, 0.05, 0)),
  helmLocal.clone().add(new THREE.Vector3(-0.06, 0.03, -0.03)),
]);

const chGps   = pulseChannel({ curves: [gpsPath], per: 3, color: C.cyan, size: 0.045, rate: 1.5 });
const chServo = pulseChannel({ curves: [servoLeadPath], per: 5, color: C.cyan, size: 0.042, rate: 0.85 });
const chPwm   = pulseChannel({ curves: cable8Curves, per: 2, color: C.cyan, size: 0.03, rate: 1.05, stagger: 0.09 });
const chSense = pulseChannel({ curves: [cable8Curves[1], cable8Curves[6]], per: 3, color: C.teal, size: 0.032, rate: 0.75, reverse: true });
const softGlowTex = glowTexture('rgba(255,255,255,0.72)', 'rgba(255,255,255,0.13)');
const chPower = pulseChannel({ curves: motorCablePaths, per: 2, color: 0x63e8c8, size: 0.034, rate: 0.5, stagger: 0.5, tex: softGlowTex });

// =========================================================== apply simState =
const _m4 = new THREE.Matrix4(), _q = new THREE.Quaternion(), _s3 = new THREE.Vector3();
const _v3a = new THREE.Vector3(), _v3b = new THREE.Vector3(), _v3c = new THREE.Vector3();
const _xz = new THREE.Vector2();

function applySim(sim) {
  const { gt, lt } = sim;
  waterUniforms.uTime.value = gt;

  // ---- boat pose (offset + breathing bob) ----
  const bobY = (0.045 * Math.sin(gt * 0.9) + 0.022 * Math.sin(gt * 1.63 + 1.2)) * MOTION;
  boat.position.set(sim.off.x, -0.02 + bobY, sim.off.y);
  boat.rotation.z = 0.013 * Math.sin(gt * 0.8 + 0.5) * MOTION;
  boat.rotation.x = 0.009 * Math.sin(gt * 0.57 + 2.0) * MOTION;
  stripeU.value = 0.155 + bobY * 0.9;
  contactShadow.position.set(0.3 + sim.off.x, 0.165, sim.off.y);

  // ---- servo azimuth + prop ----
  const azRad = d2r(sim.azDeg);
  steeringGroup.rotation.y = -azRad;
  const spin = propAngleAt(gt);
  propGroup.rotation.x = spin;
  const rate = SPIN_IDLE + sim.thrust * 0.55;
  const discF = smooth(6, 15, rate);                     // blades → motion disc
  motionDiscs[0].rotation.x = spin * 0.5;
  motionDiscs[1].rotation.x = -spin * 0.35;
  for (const d of motionDiscs) d.material.opacity = 0.55 * discF;
  bladeMat.opacity = 1 - 0.72 * discF;
  boat.updateMatrixWorld(true);

  // ---- water ----
  waterUniforms.uWindDir.value.copy(sim.windDir);
  waterUniforms.uWindSkew.value = sim.cold ? 0.12 : windSkewAt(lt);
  waterUniforms.uGridLock.value = sim.gridLock * 0.9;
  waterUniforms.uAnchorPos.value.set(ANCHOR.x, ANCHOR.z);
  const stampPulse = sim.cold ? 0 : smooth(0.9, 1.4, lt) * (1 - smooth(2.6, 3.6, lt));
  waterUniforms.uGridBase.value = 0.16 + 0.20 * stampPulse + 0.10 * sim.lockPulse;
  waterUniforms.uThrust.value = clamp01(sim.thrust / 38);
  steeringGroup.getWorldPosition(_v3a);
  waterUniforms.uBowPos.value.set(_v3a.x, _v3a.z);

  // ---- X-ray + electronics glow ----
  window.__xray(sim.xray);
  const shimmer = sim.cold ? 0 : smooth(18.3, 19.2, lt) * (1 - smooth(24, 25, lt));
  boardFaceMats[0].emissiveIntensity += shimmer * (0.5 + 0.5 * Math.sin(lt * 9.0)) * 0.9;
  const powerAct = sim.cold ? 0 : smooth(34.2, 35.2, lt) * (1 - smooth(44.5, 46, lt));
  boardFaceMats[1].emissiveIntensity += powerAct * (0.35 + 0.25 * Math.sin(lt * 7.0));
  heavyMat.emissiveIntensity = sim.xray * 0.06 + powerAct * 0.22;

  // ---- HOLDLINE tether ----
  const drop = sim.cold ? 0 : (sim.cycle === 0 ? smooth(2.3, 3.9, lt) : 1);
  tether.visible = drop > 0.02;
  tetherGlowA.visible = tetherGlowB.visible = tether.visible;
  boat.localToWorld(_v3a.set(2.92, 0.55, 0));            // bow eyelet
  _v3b.copy(ANCHOR);
  const bend = sim.tension * 0.95;
  _v3c.copy(_v3a).add(_v3b).multiplyScalar(0.5);
  _v3c.x += sim.windDir.x * bend; _v3c.z += sim.windDir.y * bend;
  _v3c.y -= 0.16 * (1 - sim.tension);
  tetherCurve.v0.copy(_v3a); tetherCurve.v1.copy(_v3c); tetherCurve.v2.copy(_v3b);
  const g = new THREE.TubeGeometry(tetherCurve, 40, 0.013 + 0.007 * (1 - sim.tension), 6, false);
  g.setDrawRange(0, Math.floor(40 * drop) * 36);
  tether.geometry.dispose();
  tether.geometry = g;
  tetherMat.color.copy(CYAN).lerp(AMBER, sim.tension * 0.95);
  tetherMat.opacity = (0.55 + 0.3 * sim.tension) * drop;
  tetherGlowA.position.copy(_v3a);
  tetherGlowB.position.copy(_v3b).y += 0.05;
  tetherGlowA.material.color.copy(tetherMat.color);
  tetherGlowA.material.opacity = 0.4 * drop;
  waterUniforms.uTetherA.value.set(_v3a.x, _v3a.z);
  waterUniforms.uTetherB.value.set(_v3b.x, _v3b.z);
  waterUniforms.uTetherGlow.value = 0.28 * drop * (0.75 + 0.25 * Math.sin(gt * 2.1));

  // ---- reticle ----
  anchorGroup.visible = sim.anchorOn > 0.01;
  retRing.material.opacity = 0.85 * sim.anchorOn;
  retRing.material.color.copy(CYAN).lerp(TEAL, sim.lockPulse);
  retGlow.material.color.copy(retRing.material.color);
  retGlow.material.opacity = 0.35 * sim.anchorOn + 0.45 * sim.lockPulse;
  retRing.scale.setScalar(1 + 0.035 * Math.sin(gt * 2.0) + 0.10 * sim.lockPulse);
  retRing2.scale.copy(retRing.scale);
  retTicks.rotation.y = gt * 0.12;
  const breathe = sim.cold ? 0 : 0.05 * Math.sin(gt * 1.25) * smooth(52, 55, lt) * (1 - smooth(59.2, 61, lt));
  holdCircle.material.opacity = (0.10 + 0.08 * sim.gridLock) * sim.anchorOn + breathe;
  // per-cycle stamp: ghost ring descends + ripple (a re-affirmation on later loops)
  const su = sim.cold ? 0 : clamp01((lt - 0.35) / 1.0);
  stampRing.position.y = lerp(2.4, 0.02, easeIO(su));
  stampRing.material.opacity = (su <= 0 || su >= 1) ? 0 : 0.7 * Math.sin(su * Math.PI);
  const ru = sim.cold ? 0 : clamp01((lt - 1.35) / 1.3);
  rippleRing.scale.setScalar(lerp(0.4, 4.2, easeOutCubic(ru)));
  rippleRing.material.opacity = (ru <= 0 || ru >= 1) ? 0 : 0.5 * (1 - ru);
  // ping rings (beat-04 fixes + LOCK flash)
  for (let i = 0; i < PINGS.length; i++) {
    const age = lt - PINGS[i].t;
    const m = pingMeshes[i];
    if (sim.cold || age <= 0 || age > 1.8) { m.visible = false; continue; }
    m.visible = true;
    m.scale.setScalar(0.4 + age * 3.4);
    m.material.opacity = 0.55 * (1 - age / 1.8);
    m.material.color.set(PINGS[i].teal ? C.teal : C.cyan);
  }

  // ---- drift vector (amber) ----
  _xz.set(3.05 + sim.off.x - ANCHOR.x, sim.off.y - ANCHOR.z);
  const dLen = _xz.length() - 0.55;
  const dvOn = smooth(0.35, 0.9, dLen) * sim.anchorOn * (1 - sim.lockPulse);
  driftArrow.visible = driftArrowGlow.visible = dvOn > 0.02;
  if (driftArrow.visible) {
    _v3a.set(_xz.x, 0, _xz.y).normalize();
    driftArrow.quaternion.setFromUnitVectors(UP, _v3a);
    driftArrow.position.set(ANCHOR.x, 0.07, ANCHOR.z);
    const L = Math.max(dLen, 0.05);
    const { shaft, head, mat } = driftArrow.userData;
    shaft.scale.y = Math.max(L - 0.26, 0.01);
    shaft.position.y = (L - 0.26) / 2;
    head.position.y = L - 0.13;
    mat.opacity = 0.62 * dvOn;
    driftArrowGlow.position.set(ANCHOR.x + _v3a.x * L, 0.12, ANCHOR.z + _v3a.z * L);
    driftArrowGlow.material.opacity = 0.28 * dvOn;
  }

  // ---- thrust cone (teal, anti-parallel to drift) + wake + ESC ----
  const thrustF = sim.thrust / 38;
  thrustConeMat.uniforms.uAlpha.value = 0.78 * clamp01(thrustF);
  thrustConeMat.uniforms.uTime.value = gt;
  thrustCone.visible = thrustF > 0.02;
  const coneL = 0.5 + 1.25 * thrustF;
  thrustCone.scale.set(1, coneL, 1);
  thrustCone.position.y = coneL / 2;
  for (let i = 0; i < wakeRings.length; i++) {
    const r = wakeRings[i];
    if (sim.cold) { r.visible = false; continue; }
    const k = Math.floor(lt / WAKE_RATE) - i;
    const sT = k * WAKE_RATE;
    const age = lt - sT;
    const th = sT >= 0 ? thrustAt(sT) : 0;
    if (k < 0 || age > WAKE_LIFE || th < 6) { r.visible = false; continue; }
    propXZAt(sT, sim.cycle, _xz);
    r.visible = true;
    // rings drift backward (away from home) as they age
    const back = 0.45 + age * 0.5;
    const azT = loopAzRadAt(sT, sim.cycle);
    r.position.set(_xz.x - Math.cos(azT) * back, 0.03, _xz.y - Math.sin(azT) * back);
    r.scale.setScalar(0.30 + age * 0.62);
    r.material.opacity = 0.16 * (1 - age / WAKE_LIFE) * (th / 38);
  }

  // ---- breadcrumbs (GPS fix trail) ----
  const k0 = Math.floor(lt / CRUMB_DT);
  for (let i = 0; i < CRUMB_N; i++) {
    const sT = (k0 - i) * CRUMB_DT;
    let sc = 0;
    if (!sim.cold && sT >= 9 && sT <= lt) {
      const age = lt - sT;
      // trail fades out entirely once the boat re-locks (seam-safe)
      sc = clamp01(1 - age / (CRUMB_N * CRUMB_DT)) * 0.9 * (1 - smooth(50, 53.5, lt));
      offsetAt(sT, sim.cycle, _wOff);
      _v3a.set(3.05 + _wOff.x, 0.05, _wOff.y);
    } else _v3a.set(0, -10, 0);
    sc = Math.max(sc, 0.0001);
    _m4.compose(_v3a, _q.identity(), _s3.set(sc, sc * 0.28, sc));   // hug the water
    crumbs.setMatrixAt(i, _m4);
  }
  crumbs.instanceMatrix.needsUpdate = true;

  // ---- wind streaks ----
  streakMat.opacity = 0.22 * (sim.cold ? 0.15 : streakActAt(lt)) * MOTION + 0.008;
  const cAng = -sim.windAng;
  for (let i = 0; i < STREAK_N; i++) {
    const b = streakBase[i];
    const travel = sim.gt * b.sp;
    let x = b.x + sim.windDir.x * travel, z = b.z + sim.windDir.y * travel;
    x = ((x - 2 + 22) % 44 + 44) % 44 - 22 + 2;         // wrap around action area
    z = ((z + 22) % 44 + 44) % 44 - 22;
    _m4.compose(_v3a.set(x, 0.05, z), _q.setFromAxisAngle(UP, cAng), _s3.set(b.sc, 1, 1));
    streaks.setMatrixAt(i, _m4);
  }
  streaks.instanceMatrix.needsUpdate = true;

  // ---- servo protractor grammar (ignites in beat 05) ----
  const sOn = sim.cold ? 0 : smooth(25.7, 26.4, lt) * (1 - smooth(32.2, 33.4, lt));
  servoHudMat.opacity = 0.5 * sOn;
  servoTickLiveMat.opacity = 0.85 * sOn;
  servoTickGhostMat.opacity = 0.5 * sOn;
  servoHud.visible = sOn > 0.02;
  if (servoHud.visible) {
    const azT = d2r(SIM.azTargetDeg);
    const minA = Math.min(0, azRad), maxA = Math.max(0, azRad);
    sweepArc.geometry.dispose();
    sweepArc.geometry = new THREE.RingGeometry(0.148, 0.176, 40, 1, -maxA, Math.max(maxA - minA, 0.02));
    ghostTick.rotation.z = -azT;              // arcs slide along the protractor
    liveTick.rotation.z = -azRad;
    markerTri.position.set(Math.cos(azRad) * 0.098, 0.002, Math.sin(azRad) * 0.098);
    markerTri.rotation.y = -azRad;             // needle sweeps flat on the dial
  }

  // ---- 8-WIRE callout: lift the ribbon out of the dark while pointed at ----
  const ribHot = sim.cold ? smooth(10.15, 10.6, gt) * (1 - smooth(11.9, 12.35, gt)) : 0;
  cableMatA.emissiveIntensity = 0.10 + 0.50 * ribHot;
  cableMatB.emissiveIntensity = 0.04 + 0.26 * ribHot;

  // ---- signal pulse channels ----
  if (sim.cold) {
    const idle = gt > 7 && gt < 17.5 ? ENV_IDLE : null;   // beat-01 dim idle life
    updateChannel(chGps, gt, idle);
    updateChannel(chServo, gt, idle);
    updateChannel(chPwm, gt, idle);
    updateChannel(chSense, gt, null);
    updateChannel(chPower, gt, idle && { a: 0.1, f: 1, g: 0 });
  } else {
    updateChannel(chGps, gt, envWin(lt, 9, 55, 0.5));
    updateChannel(chServo, gt, envMax(envWin(lt, 25.4, 30.6, 1.1), envWin(lt, 31, 47, 1.1, 0.22)));
    updateChannel(chPwm, gt, envMax(envWin(lt, 33.2, 45.3, 0.9), envWin(lt, 53.6, 60.0, 0.9, 0.3)));
    updateChannel(chSense, gt, envWin(lt, 35.0, 46.2, 0.9, 0.8));
    updateChannel(chPower, gt, envMax(envWin(lt, 34.4, 45.6, 0.8, 0.8), envWin(lt, 54.2, 60.2, 0.8, 0.28)));
  }
}

// ================================================================= camera ===
const HERO_POS = new THREE.Vector3(7.4, 3.1, 9.2);
const HERO_TGT = new THREE.Vector3(0.45, 0.55, 0);
const _pose = { pos: new THREE.Vector3(), tgt: new THREE.Vector3(), fov: 42 };
const _poseT = { pos: new THREE.Vector3(), tgt: new THREE.Vector3(), fov: 42 };
const _poseT2 = { pos: new THREE.Vector3(), tgt: new THREE.Vector3(), fov: 42 };
function lerpPose(a, b, k, out) {
  out.pos.lerpVectors(a.pos, b.pos, k);
  out.tgt.lerpVectors(a.tgt, b.tgt, k);
  out.fov = lerp(a.fov, b.fov, k);
}

function poseB00(u, out) {
  const k = easeIO(smooth(2.0, 6.0, u));
  out.pos.set(lerp(1.4, 7.05, k), lerp(17.5, 4.0, k), lerp(2.4, 9.25, k));
  out.tgt.copy(HERO_TGT);
  out.fov = 42;
}
const b01Path = new THREE.CatmullRomCurve3([
  new THREE.Vector3(-5.2, 1.55, 3.3),
  new THREE.Vector3(-2.6, 1.38, 3.5),
  new THREE.Vector3(0.8, 1.05, 4.2),
  new THREE.Vector3(3.0, 0.92, 4.0),
  new THREE.Vector3(5.6, 0.75, 2.9),
]);
const b01Tgt = vecTrack([                       // stern→bow hardware tour:
  [0.0, -2.45, 0.55, 0],                        // tiller stern
  [2.2, -1.44, 0.40, 0.12],                     // helm enclosure by the battery
  [4.6, 0.10, 0.20, 0.42],                      // ribbon running the hull side
  [6.6, 2.0, 0.5, 0],                           // thrust driver, bow platform
  [8.8, 3.24, 0.9, 0],                          // steering collar
  [10.6, 3.3, 0.4, 0],
  [12.0, 3.3, 0.2, 0],
]);
function poseB01(u, out) {
  const k = clamp01(u / 12);
  const kk = 0.72 * k + 0.28 * (k * k * (3 - 2 * k));
  b01Path.getPoint(kk, out.pos);
  b01Tgt(u, out.tgt);
  out.fov = 38;
  if (u < 2.4) {                                // flow in from the b00 reveal
    poseB00(6, _poseT);
    lerpPose(_poseT, { pos: out.pos.clone(), tgt: out.tgt.clone(), fov: 38 }, easeIO(u / 2.4), out);
  }
}
function heroOrbit(u8, out) {                   // beat 02: 8° dolly-orbit push-in
  out.pos.copy(HERO_POS).sub(HERO_TGT)
    .applyAxisAngle(UP, d2r(-8) * u8)
    .multiplyScalar(1 - 0.07 * easeIO(u8))
    .add(HERO_TGT);
  out.tgt.copy(HERO_TGT);
  out.fov = 42;
}
function poseB02(localT, cycle, out) {
  heroOrbit(localT / 8, out);
  if (cycle === 0) {                            // hand-off from the cold open
    const w = easeIO(clamp01(localT / 2.8));
    if (w < 1) {
      poseB01(12, _poseT);
      out.pos.lerpVectors(_poseT.pos, out.pos, w);
      out.tgt.lerpVectors(_poseT.tgt, out.tgt, w);
      out.fov = lerp(38, out.fov, w);
    }
  }
}
function poseB03(localT, sim, out) {
  _v3a.set(sim.off.x, 0, sim.off.y);
  out.pos.set(11.4, 6.2, 9.6).addScaledVector(_v3a, 0.45);
  out.tgt.set(2.3, 0.15, 0).addScaledVector(_v3a, 0.55);
  const g = sim.gt;                             // faint handheld sway
  out.pos.x += 0.10 * Math.sin(g * 1.21) * MOTION;
  out.pos.y += 0.05 * Math.sin(g * 1.63 + 2.0) * MOTION;
  out.pos.z += 0.08 * Math.sin(g * 0.93 + 4.0) * MOTION;
  out.fov = 40;
}
function poseB04(localT, out) {
  helmBoard.getWorldPosition(_v3b);
  const a = -0.30 + d2r(10) * (localT / 7);     // 10° parallax orbit, bow-on
  out.pos.copy(_v3b).add(_v3c.set(Math.cos(a) * 0.56, 0.52, Math.sin(a) * 0.56));
  out.tgt.copy(_v3b);
  out.fov = 32;
}
function poseB05(localT, sim, out) {
  // eased fast descent: continues from the b04 board close-up down to the water
  steeringGroup.getWorldPosition(_v3b);
  out.pos.set(5.01 + sim.off.x, 0.95, -1.94 + sim.off.y);  // port bow quarter
  out.pos.x += 0.05 * Math.sin(sim.gt * 0.7) * MOTION + localT * 0.022;
  out.pos.z -= localT * 0.016;                              // parallax drift
  out.tgt.copy(_v3b);
  out.tgt.y -= 0.52;             // drop the eyeline: collar top-frame, the
  out.tgt.x -= 0.10;             // submerged pod/prop enter the lower third
  out.fov = 36;                  // through the transparent surface
  const k = easeIO(clamp01(localT / 2.6));
  if (k < 1) {
    poseB04(7, _poseT);
    out.pos.lerpVectors(_poseT.pos, out.pos, k);
    // arc the move over the sheer so the blend never grazes the hull side
    out.pos.y += 0.60 * Math.sin(Math.PI * k);
    out.tgt.lerpVectors(_poseT.tgt, out.tgt, k);
    out.fov = lerp(_poseT.fov, out.fov, k);
  }
}
function poseB06(localT, sim, out) {
  const u = clamp01(localT / 10);
  _v3a.set(sim.windDir.x, 0, sim.windDir.y);
  _v3c.set(-sim.windDir.y, 0, sim.windDir.x);
  out.pos.copy(ANCHOR)
    .addScaledVector(_v3a, 3.1 - 1.7 * u)
    .addScaledVector(_v3c, 2.5 - 0.7 * u);
  out.pos.y = 0.48;
  headGroup.getWorldPosition(_v3b);
  out.tgt.copy(_v3b).addScaledVector(_v3a, 0.35).addScaledVector(_v3c, 0.30);
  out.tgt.y = Math.min(out.tgt.y + 0.15, 0.1);
  const g = sim.gt;                             // subtle energy wobble
  out.pos.x += (0.05 * Math.sin(g * 2.3) + 0.03 * Math.sin(g * 3.7)) * MOTION;
  out.pos.y += 0.03 * Math.sin(g * 2.9 + 1.0) * MOTION;
  out.fov = 34;
  const k = easeIO(clamp01(localT / 1.7));      // flow in from the b05 tuck
  if (k < 1) {
    poseB05(8, sim, _poseT2);
    out.pos.lerpVectors(_poseT2.pos, out.pos, k);
    out.tgt.lerpVectors(_poseT2.tgt, out.tgt, k);
    out.fov = lerp(_poseT2.fov, out.fov, k);
  }
}
function poseB07(localT, sim, out) {
  poseB06(10, sim, _poseT);
  const k = easeOutBack(clamp01(localT / 7), 0.65);   // decelerating crane + settle
  out.pos.lerpVectors(_poseT.pos, HERO_POS, k);
  out.tgt.lerpVectors(_poseT.tgt, HERO_TGT, k);
  out.fov = lerp(_poseT.fov, 42, clamp01(k));
}
function poseB08(localT, out) {
  const u = clamp01(localT / 8);
  const ang = d2r(20) * Math.sin(Math.PI * u);        // out and seamlessly back
  out.pos.copy(HERO_POS).sub(HERO_TGT).applyAxisAngle(UP, ang).add(HERO_TGT);
  out.pos.y += 0.35 * Math.sin(Math.PI * u);
  out.tgt.copy(HERO_TGT);
  out.fov = 42;
}

function director(ti, sim) {
  const { beat, localT } = ti;
  const P = _pose;
  switch (beat) {
    case 0: poseB00(localT, P); break;
    case 1: poseB01(localT, P); break;
    case 2: poseB02(localT, sim.cycle, P); break;
    case 3: poseB03(localT, sim, P); break;
    case 4: poseB04(localT, P); break;
    case 5: poseB05(localT, sim, P); break;
    case 6: poseB06(localT, sim, P); break;
    case 7: poseB07(localT, sim, P); break;
    default: poseB08(localT, P); break;
  }
  // constant low-amplitude breathing bob from the reveal onward
  const bob = 0.03 * MOTION * (P.fov / 42) * smooth(2.5, 5.5, sim.gt);
  P.pos.y += bob * Math.sin(sim.gt * 0.83 + 0.7);
  camera.position.copy(P.pos);
  camera.lookAt(P.tgt);
  const fov = THREE.MathUtils.clamp(P.fov * (1 + 0.55 * Math.max(0, 1.45 - camera.aspect)), P.fov, 72);
  if (Math.abs(camera.fov - fov) > 1e-3) { camera.fov = fov; camera.updateProjectionMatrix(); }
  // fresh matrices so DOM projection (chips/callouts) matches THIS frame
  camera.updateMatrixWorld(true);
  camera.matrixWorldInverse.copy(camera.matrixWorld).invert();
}

// ================================================================ DOM layer =
const $ = (id) => document.getElementById(id);
const capEl = $('cap'), capKick = $('capKick'), capBody = $('capBody');
const beatIdxEl = $('beatIdx'), wipeEl = $('wipe'), wordmarkEl = $('wordmark');
const logoOverlay = $('logoOverlay'), lgRing = $('lgRing'), lgTicks = $('lgTicks'), lgAnchor = $('lgAnchor');
const calloutEl = $('callout'), leaderSvg = $('leader');
const chipDrift = $('chipDrift'), chipAz = $('chipAz'), chipEsc = $('chipEsc'),
      chipLock = $('chipLock'), panelTel = $('panelTel'), hudEl = $('hud');
const escLabel = chipEsc.querySelector('span'), escBar = chipEsc.querySelector('.bar i');

const CAPS = [
  ['00 · MARK', 'VANCHOR-NG · The virtual anchor'],
  ['01 · THE HARDWARE', 'One brain. One muscle. The helm board computes; the thrust driver delivers.'],
  ['02 · ANCHOR SET', 'Drop a virtual anchor. No chain — just a GPS point the boat is told to hold.'],
  ['03 · DRIFT', 'Wind and current don’t care about your anchor. Metre by metre, the drift builds.'],
  ['04 · THE HELM DECIDES', 'The helm board reads the GPS ten times a second. It knows how far off — and which way.'],
  ['05 · AIM — SERVO', 'The helm drives the steering servo — swinging the motor to point straight at home.'],
  ['06 · THRUST — DRIVER', 'PWM to the thrust driver, power to the prop — a measured pulse, never more. Current-sense reports back.'],
  ['07 · BACK ON THE MARK', 'Zero drift. Locked. One full correction — and the loop never stops running.'],
  ['08 · IT JUST HOLDS', 'Set it and fish. Station-keeping, quietly, forever. → github'],
];
let capBeatShown = -1;

// beat-01 leader-line callouts (boat-local anchor points)
// 8-WIRE anchor: sample the point ON the ribbon curve nearest the mid-hull
// stretch the beat-01 dolly looks at (x ≈ 0.10) so the leader dot sits on
// the cable run, never floating above it
const _cable8Mid = (() => {
  let best = cable8Path.getPoint(0.5).clone(), bd = Infinity;
  for (let i = 0; i <= 48; i++) {
    const p = cable8Path.getPoint(i / 48);
    const d = Math.abs(p.x - 0.10);
    if (d < bd) { bd = d; best.copy(p); }
  }
  return best;
})();
const CALLOUTS = [
  { t0: 1.4, t1: 4.0, label: 'HELM BOARD', sub: 'vanchor-helm v4.2 · Pi/Pico carrier — the brain, by the battery', p: new THREE.Vector3(-1.42, 0.42, 0.12) },
  { t0: 4.2, t1: 6.3, label: '8-WIRE CABLE', sub: 'stern to bow · PWM out, current-sense telemetry back', p: _cable8Mid },
  { t0: 6.5, t1: 8.5, label: 'THRUST DRIVER', sub: 'vanchor-thrust v1.1 · BTN8982 H-bridge + heatsink', p: new THREE.Vector3(2.0, 0.56, 0) },
  { t0: 8.7, t1: 10.3, label: 'STEERING SERVO', sub: 'thin servo lead, driven straight from the helm', p: new THREE.Vector3(3.18, 0.98, 0) },
  { t0: 10.5, t1: 11.8, label: 'TROLLING MOTOR', sub: 'bow-mounted · steerable head + prop, fed by two heavy cables', p: new THREE.Vector3(3.32, 0.3, 0) },
];

// loop-HUD ring: SENSE · DECIDE · AIM · THRUST
const HUD_NODES = [
  { label: 'SENSE', ang: -90, ig: 9.5 },
  { label: 'DECIDE', ang: 0, ig: 19.2 },
  { label: 'AIM', ang: 90, ig: 26.8 },
  { label: 'THRUST', ang: 180, ig: 35.3 },
];
const HUD_ARCS = [                                // arc i: node i → node i+1
  { t: 19.2, dur: 0.7 }, { t: 26.8, dur: 0.7 }, { t: 35.3, dur: 0.7 },
  { t: 47.6, dur: 1.8 },                          // the loop closes → re-lock
];
let hudNodeEls = [], hudArcEls = [], hudBaseEl = null;
{
  const cx = 85, cy = 78, r = 40;
  const pt = (aDeg) => [cx + r * Math.cos(d2r(aDeg)), cy + r * Math.sin(d2r(aDeg))];
  let svg = `<svg viewBox="0 0 170 150" width="100%" height="100%" style="overflow:visible">`;
  svg += `<circle id="hudBase" cx="${cx}" cy="${cy}" r="${r}" fill="none" stroke="rgba(47,243,255,.14)" stroke-width="1.5"/>`;
  for (let i = 0; i < 4; i++) {
    const [x0, y0] = pt(HUD_NODES[i].ang), [x1, y1] = pt(HUD_NODES[(i + 1) % 4].ang);
    svg += `<path class="hudArc" d="M ${x0} ${y0} A ${r} ${r} 0 0 1 ${x1} ${y1}" fill="none" stroke="#2ff3ff" stroke-width="2" pathLength="100" stroke-dasharray="100" stroke-dashoffset="100"/>`;
  }
  for (const n of HUD_NODES) {
    const [x, y] = pt(n.ang);
    const lx = cx + (r + 14) * Math.cos(d2r(n.ang)), ly = cy + (r + 14) * Math.sin(d2r(n.ang));
    const anchor = Math.abs(n.ang) === 90 ? 'middle' : (n.ang === 0 ? 'start' : 'end');
    svg += `<g class="hudNode"><circle cx="${x}" cy="${y}" r="8" fill="rgba(47,243,255,.16)"/>` +
           `<circle cx="${x}" cy="${y}" r="3.4" fill="#2ff3ff"/>` +
           `<text x="${lx}" y="${ly + 3}" text-anchor="${anchor}">${n.label}</text></g>`;
  }
  svg += `</svg>`;
  hudEl.innerHTML = svg;
  hudNodeEls = [...hudEl.querySelectorAll('.hudNode')];
  hudArcEls = [...hudEl.querySelectorAll('.hudArc')];
  hudBaseEl = hudEl.querySelector('#hudBase');
}

const _prj = new THREE.Vector3();
function toScreen(w) {
  _prj.copy(w).project(camera);
  if (_prj.z > 1) return null;
  return { x: (_prj.x * 0.5 + 0.5) * innerWidth, y: (-_prj.y * 0.5 + 0.5) * innerHeight };
}
function placeChip(el, world, dx, dy, show) {
  if (!show) { el.style.display = 'none'; return; }
  const s = toScreen(world);
  if (!s) { el.style.display = 'none'; return; }
  if (s.x < -30 || s.x > innerWidth + 30 || s.y < -30 || s.y > innerHeight + 30) {
    el.style.display = 'none';                       // anchor truly off-screen
    return;
  }
  el.style.display = 'block';
  const x = Math.min(Math.max(8, s.x + dx), innerWidth - el.offsetWidth - 8);
  const y = Math.min(Math.max(8, s.y + dy), innerHeight - el.offsetHeight - 8);
  el.style.left = `${x.toFixed(1)}px`;
  el.style.top = `${y.toFixed(1)}px`;
}

function updateDOM(ti, sim) {
  const { beat, localT } = ti;
  const lt = sim.lt, gt = sim.gt;

  // ---- captions ----
  if (capBeatShown !== beat) {
    capKick.textContent = CAPS[beat][0];
    capBody.textContent = CAPS[beat][1];
    beatIdxEl.textContent = `${String(beat).padStart(2, '0')} / 08`;
    capBeatShown = beat;
  }
  const dur = BEAT_DUR[beat];
  const inW = beat === 0 ? smooth(1.3, 2.2, localT) : smooth(0.3, 1.0, localT);
  const capOp = inW * (1 - smooth(dur - 0.85, dur - 0.2, localT));
  capEl.style.opacity = capOp.toFixed(3);
  capEl.style.transform = `translateX(-50%) translateY(${((1 - inW) * 8).toFixed(2)}px)`;
  beatIdxEl.style.opacity = (0.85 * smooth(2.5, 4, gt)).toFixed(3);

  // ---- beat-00 logo draw-on ----
  if (beat === 0) {
    logoOverlay.style.display = 'grid';
    logoOverlay.style.opacity = (1 - smooth(2.9, 4.4, localT)).toFixed(3);
    lgRing.setAttribute('stroke-dashoffset', (100 * (1 - smooth(0.2, 1.9, localT))).toFixed(2));
    lgTicks.setAttribute('opacity', smooth(1.55, 2.05, localT).toFixed(3));
    lgAnchor.setAttribute('stroke-dashoffset', (100 * (1 - smooth(1.0, 2.7, localT))).toFixed(2));
  } else logoOverlay.style.display = 'none';

  // ---- scanline wipe (hard cuts into beats 03 and 04) ----
  let wOp = 0, w0 = 0, w1 = 0;
  if (!sim.cold) for (const cut of [8, 18]) {
    const dt = lt - cut;
    if (Math.abs(dt) <= 0.13) {
      const p = (dt + 0.13) / 0.26;
      wOp = 1;
      w0 = p < 0.5 ? 0 : (p - 0.5) * 2 * 100;
      w1 = p < 0.5 ? p * 2 * 100 : 100;
    }
  }
  wipeEl.style.opacity = wOp;
  wipeEl.style.setProperty('--w0', w0.toFixed(2));
  wipeEl.style.setProperty('--w1', w1.toFixed(2));

  // ---- beat-01 callouts + leader line ----
  let call = null;
  if (beat === 1) for (const c of CALLOUTS) if (localT >= c.t0 && localT <= c.t1) { call = c; break; }
  if (call) {
    boat.localToWorld(_v3a.copy(call.p));
    const s = toScreen(_v3a);
    if (s) {
      const cOp = smooth(call.t0, call.t0 + 0.35, localT) * (1 - smooth(call.t1 - 0.3, call.t1, localT));
      calloutEl.style.display = 'block';
      calloutEl.style.opacity = cOp.toFixed(3);
      calloutEl.querySelector('.cl-label').textContent = call.label;
      calloutEl.querySelector('.cl-sub').textContent = call.sub;
      const cx2 = Math.min(Math.max(s.x + 60, 20), innerWidth - 270);
      const cy2 = Math.max(s.y - 110, 18);
      calloutEl.style.left = `${cx2}px`;
      calloutEl.style.top = `${cy2}px`;
      leaderSvg.innerHTML = `<line x1="${s.x}" y1="${s.y}" x2="${cx2 + 8}" y2="${cy2 + 40}" stroke="rgba(47,243,255,.6)" stroke-width="1"/>` +
        `<circle cx="${s.x}" cy="${s.y}" r="3" fill="none" stroke="#2ff3ff" stroke-width="1.2" opacity="${cOp}"/>`;
      leaderSvg.style.opacity = cOp.toFixed(3);
    } else { calloutEl.style.display = 'none'; leaderSvg.innerHTML = ''; }
  } else { calloutEl.style.display = 'none'; leaderSvg.innerHTML = ''; }

  // ---- loop-HUD ring ---- (fades in DURING beat 02, per spec)
  const hudOp = sim.cold ? 0 : (sim.cycle === 0 ? smooth(2.2, 4.2, lt) : 1);
  hudEl.style.opacity = (hudOp * 0.95).toFixed(3);
  if (hudOp > 0) {
    const fadeBack = 1 - smooth(54, 58, lt);
    for (let i = 0; i < 4; i++) {
      const n = HUD_NODES[i];
      const litRaw = smooth(n.ig, n.ig + 0.5, lt) * fadeBack;
      const pulse = gauss(lt - n.ig - 0.3, 0.55);
      hudNodeEls[i].style.opacity = (0.30 + 0.70 * litRaw + 0.4 * pulse * fadeBack).toFixed(3);
      const a = HUD_ARCS[i];
      const fill = smooth(a.t, a.t + a.dur, lt) * fadeBack;
      hudArcEls[i].setAttribute('stroke-dashoffset', (100 * (1 - fill)).toFixed(2));
      hudArcEls[i].setAttribute('opacity', (0.85 * fill).toFixed(3));
    }
    const closePulse = smooth(49.4, 49.8, lt) * (1 - smooth(51, 52.5, lt));
    hudBaseEl.setAttribute('stroke', `rgba(47,243,255,${(0.14 + 0.4 * closePulse).toFixed(3)})`);
  }

  // ---- telemetry chips ----
  // DRIFT
  const driftShow = !sim.cold && lt > 9.2 && lt < 48.35 && beat !== 4 && beat !== 5;
  if (driftShow) {
    _v3a.set(3.05 + sim.off.x, 0.2, sim.off.y).lerp(_v3b.copy(ANCHOR), 0.45);
    chipDrift.textContent = `DRIFT ${sim.dispDrift.toFixed(1)} m`;
    const hot = sim.tension > 0.25;
    chipDrift.style.color = hot ? '#ffc24d' : '#2ff3ff';
    chipDrift.style.borderColor = hot ? 'rgba(255,194,77,.45)' : 'rgba(47,243,255,.4)';
    placeChip(chipDrift, _v3a, 14, -34, true);
    chipDrift.style.opacity = (smooth(9.2, 9.9, lt) * (1 - smooth(47.6, 48.3, lt))).toFixed(3);
  } else chipDrift.style.display = 'none';
  // beat-04 telemetry panel (drift, bearing, sparkline of recent fixes)
  if (beat === 4 && !sim.cold) {
    helmBoard.getWorldPosition(_v3a);
    const brg = ((r2d(Math.atan2(ANCHOR.z - sim.off.y, ANCHOR.x - (3.05 + sim.off.x))) + 360) % 360) | 0;
    let pts = '';
    for (let i = 0; i <= 24; i++) {
      const v = Math.max(0, driftAt(lt - 6 + i * 0.25));
      pts += `${(i * 4).toFixed(0)},${(20 - v * 4.6).toFixed(1)} `;
    }
    panelTel.innerHTML =
      `<span style="color:#ffc24d">DRIFT ${sim.dispDrift.toFixed(2)} m</span><br>` +
      `<span style="color:#2ff3ff">BRG ${String(brg).padStart(3, '0')}° · FIX 10 Hz</span>` +
      `<svg width="96" height="22" viewBox="0 0 96 22"><polyline points="${pts}" fill="none" stroke="#2ff3ff" stroke-width="1.3" opacity=".9"/></svg>`;
    placeChip(panelTel, _v3a, 120, -140, true);
    panelTel.style.opacity = (smooth(18.9, 19.6, lt) * (1 - smooth(24.2, 24.9, lt))).toFixed(3);
  } else panelTel.style.display = 'none';
  // AZ (beat 05)
  if (!sim.cold && lt > 26.0 && lt < 32.6) {
    steeringGroup.getWorldPosition(_v3a);
    _v3a.y += 0.35;
    const azShow = ((Math.round(SIM.azDeg) % 360) + 360) % 360;
    chipAz.textContent = `AZ ${String(azShow).padStart(3, '0')}°`;
    placeChip(chipAz, _v3a, 26, -40, true);
    chipAz.style.color = '#2ff3ff';
    chipAz.style.opacity = (smooth(26.0, 26.6, lt) * (1 - smooth(32.0, 32.6, lt))).toFixed(3);
  } else chipAz.style.display = 'none';
  // ESC (beat 06) — anchored to the motor shaft at the waterline so the bar
  // sits INSIDE beat 06's low waterline framing (the driver board itself is
  // behind the hull there)
  if (!sim.cold && lt > 33.8 && lt < 46) {
    escLabel.textContent = `ESC · ${Math.round(sim.thrust)}%`;
    escBar.style.width = `${(sim.thrust / 38 * 100).toFixed(1)}%`;
    if (innerHeight > innerWidth) {
      // portrait: the world anchor can sit off the left frame edge, so pin
      // the readout top-center instead of losing it to the off-screen rule
      chipEsc.style.display = 'block';
      chipEsc.style.left = `${Math.max(8, (innerWidth - chipEsc.offsetWidth) * 0.5).toFixed(1)}px`;
      chipEsc.style.top = `${Math.round(innerHeight * 0.12)}px`;
    } else {
      steeringGroup.getWorldPosition(_v3a);
      _v3a.y = 0.42;
      placeChip(chipEsc, _v3a, 30, -56, true);
    }
    chipEsc.style.opacity = (smooth(33.8, 34.5, lt) * (1 - smooth(45.2, 46, lt))).toFixed(3);
  } else chipEsc.style.display = 'none';
  // LOCK (beat 07)
  if (sim.lockPulse > 0.01) {
    _v3a.copy(ANCHOR).setY(0.25);
    placeChip(chipLock, _v3a, -24, -30, true);
    chipLock.style.opacity = sim.lockPulse.toFixed(3);
    chipLock.style.transform = `scale(${(0.9 + 0.15 * sim.lockPulse).toFixed(3)})`;
  } else chipLock.style.display = 'none';

  // ---- beat-08 wordmark ----
  const wm = beat === 8 ? smooth(2.2, 3.4, localT) * (1 - smooth(5.6, 6.8, localT)) : 0;
  wordmarkEl.style.opacity = wm.toFixed(3);

  // ---- scrub rail (loop beats 02–08) ----
  railEl.style.opacity = sim.cold ? '0' : '0.9';
  if (!sim.cold) for (let i = 2; i <= 8; i++) {
    const b0 = BEAT_START[i] - COLD_OPEN;
    const f = clamp01((lt - b0) / BEAT_DUR[i]);
    railSegs[i - 2].style.transform = `scaleX(${f.toFixed(3)})`;
  }

  // ---- debug ----
  if (DEBUG) {
    debugEl.textContent =
      `BEAT ${String(beat).padStart(2, '0')}  local ${localT.toFixed(2)}s\n` +
      `global ${gt.toFixed(2)}s  loop ${lt.toFixed(2)}s  cyc ${sim.cycle}\n` +
      `drift ${sim.drift.toFixed(2)}m  az ${sim.azDeg.toFixed(1)}°  thr ${sim.thrust.toFixed(0)}%\n` +
      `xray ${sim.xray.toFixed(2)}  wind ${r2d(sim.windAng).toFixed(0)}°  dpr ${dpr.toFixed(2)}`;
  }
}

// ===================================================== modes + housekeeping =
let explore = false;
const modeBtn = $('modeToggle');
const introEl = $('intro'), hintEl = $('hint'), railEl = $('rail');
const IS_TOUCH = matchMedia('(pointer: coarse)').matches;
controls.enabled = false;

function updateHint() {
  hintEl.textContent = explore
    ? 'DRAG TO ORBIT · SCROLL TO ZOOM'
    : RM ? 'TAP TO ADVANCE'
    : IS_TOUCH ? 'TAP THE SCENE TO EXPLORE' : 'EXPLORE = FREE ORBIT';
}
function setExplore(on) {
  explore = on;
  modeBtn.textContent = explore ? 'STORY' : 'EXPLORE';
  document.body.classList.toggle('explore', explore);
  controls.enabled = explore;
  if (explore) {
    // during the cold open there is only void — hand explore the loop diorama
    if (!RM && clockState.mode === 'play' && currentTime() < COLD_OPEN) {
      clockState.t0 = performance.now() / 1000;
      clockState.offset = COLD_OPEN + 5;
      _pose.pos.copy(HERO_POS); _pose.tgt.copy(HERO_TGT);
      camera.position.copy(HERO_POS);
      camera.lookAt(HERO_TGT);
    }
    controls.target.copy(_pose.tgt);              // zero-pop hand-off
  } else if (RM) rmShow(rmBeat);
  updateHint();
}
modeBtn.addEventListener('click', () => setExplore(!explore));

// ------------------------------------------------------ deterministic time --
const clockState = { mode: 'play', t0: performance.now() / 1000, offset: 0, held: 0 };
function repaintHeld() {
  try {
    if (clockState.mode === 'hold' && !explore) { update(clockState.held); renderer.render(scene, camera); }
  } catch { /* pre-init events */ }
}
function currentTime() {
  return clockState.mode === 'play'
    ? performance.now() / 1000 - clockState.t0 + clockState.offset
    : clockState.held;
}
window.__seek = function (t) {
  dismissIntro('silent');
  clockState.mode = 'hold';
  clockState.held = Math.max(0, +t || 0);
  update(clockState.held);
  renderer.render(scene, camera);
  return clockState.held;
};
window.__beat = function (n) {
  n = Math.max(0, Math.min(8, n | 0));
  return window.__seek(BEAT_START[n]);
};
window.__play = function () {
  dismissIntro('silent');
  clockState.t0 = performance.now() / 1000;
  clockState.offset = clockState.held;
  clockState.mode = 'play';
};

// ------------------------------------------- intro overlay + reduced motion --
// The page loads holding frame 0 behind the intro card; dismissing starts the
// film (or a static-beat slideshow under prefers-reduced-motion).
let introDone = false, introTimer = 0;
let rmBeat = 0;
const RM_TIMES = [3.5, 5.2, 5, 6, 3.5, 6.5, 5, 6, 3.2];  // hero moment per beat
function rmShow(n) {
  rmBeat = ((n % 9) + 9) % 9;
  clockState.mode = 'hold';
  clockState.held = BEAT_START[rmBeat] + RM_TIMES[rmBeat];
  update(clockState.held);
  renderer.render(scene, camera);
}
function dismissIntro(mode) {
  if (introDone) { return; }
  introDone = true;
  clearTimeout(introTimer);
  document.body.classList.remove('intro');
  if (mode === 'silent') {
    introEl.style.display = 'none';                 // QA hooks: instant, no fade
  } else {
    introEl.classList.add('off');
    if (mode === 'explore') {
      clockState.held = COLD_OPEN + 5;              // anchored diorama, sim alive
      if (RM) rmShow(2); else { clockState.t0 = performance.now() / 1000; clockState.offset = clockState.held; clockState.mode = 'play'; }
      _pose.pos.copy(HERO_POS); _pose.tgt.copy(HERO_TGT);   // hero framing hand-off
      camera.position.copy(HERO_POS);
      camera.lookAt(HERO_TGT);
      setExplore(true);
    } else if (RM) {
      rmShow(0);
    } else {
      clockState.t0 = performance.now() / 1000;     // roll the film from 0
      clockState.offset = 0;
      clockState.mode = 'play';
    }
  }
  updateHint();
}
$('introStory').addEventListener('click', () => dismissIntro('story'));
$('introExplore').addEventListener('click', () => dismissIntro('explore'));

// jump the running film to a loop beat (scrub rail)
function jumpToBeat(n) {
  dismissIntro('silent');
  if (RM) { rmShow(n); return; }
  clockState.mode = 'hold';
  clockState.held = BEAT_START[n];
  window.__play();
}

// 7-segment tap-to-scrub rail for the loop beats 02–08
const railSegs = [];
for (let i = 2; i <= 8; i++) {
  const seg = document.createElement('span');
  seg.appendChild(document.createElement('i'));
  seg.addEventListener('click', () => jumpToBeat(i));
  railEl.appendChild(seg);
  railSegs.push(seg.firstChild);
}

// tap = advance (reduced motion) or toggle explore/story (touch devices)
{
  let px = 0, py = 0, pt = 0, pid = -1;
  renderer.domElement.addEventListener('pointerdown', (e) => {
    px = e.clientX; py = e.clientY; pt = performance.now(); pid = e.pointerId;
  });
  renderer.domElement.addEventListener('pointerup', (e) => {
    if (e.pointerId !== pid) return;
    const dt = performance.now() - pt;
    const dd = Math.hypot(e.clientX - px, e.clientY - py);
    if (dt > 450 || dd > 12 || !introDone) return;
    if (RM && !explore) rmShow(rmBeat + 1);
    else if (e.pointerType === 'touch') setExplore(!explore);
  });
}
// QA-only camera setter: [px,py,pz, tx,ty,tz, fov?]
window.__setCam = function (a) {
  camera.position.set(a[0], a[1], a[2]);
  controls.target.set(a[3], a[4], a[5]);
  if (a.length > 6) { camera.fov = a[6]; camera.updateProjectionMatrix(); }
  camera.lookAt(controls.target);
  renderer.render(scene, camera);
};

// debug readout (?debug=1)
const debugEl = document.getElementById('debug');
const DEBUG = new URLSearchParams(location.search).get('debug') === '1';
if (DEBUG) {
  document.body.classList.add('debug');
  window.__scene = scene; window.__driver = thrustDriver; window.__helm = helmBoard;
  window.__sim = SIM; window.__camera = camera; window.__renderer = renderer;
}
window.__BEAT_START = BEAT_START.slice();

// ------------------------------------------------------------- update -------
function update(t) {
  const sim = simAt(t);
  const ti = timeInfo(t);
  applySim(sim);
  if (!explore) director(ti, sim);
  updateDOM(ti, sim);
}

// adaptive DPR: step down if frames run long
let frameAcc = 0, frameN = 0;
let lastNow = performance.now();
function watchPerf(now) {
  frameAcc += now - lastNow; lastNow = now;
  if (++frameN >= 90) {
    if (frameAcc / frameN > 26 && dpr > 1.25) {
      dpr = Math.max(1.25, dpr - 0.25);
      renderer.setPixelRatio(dpr);
    }
    frameAcc = 0; frameN = 0;
  }
}

// pause the deterministic clock while hidden / off-screen
let hidden = false, offscreen = false, pausedAt = 0;
function setPaused(p) {
  if (p) pausedAt = currentTime();
  else if (clockState.mode === 'play') {
    clockState.t0 = performance.now() / 1000;
    clockState.offset = pausedAt;
  }
}
document.addEventListener('visibilitychange', () => {
  const was = hidden || offscreen;
  hidden = document.hidden;
  const is = hidden || offscreen;
  if (was !== is) setPaused(is);
});
new IntersectionObserver((entries) => {
  const was = hidden || offscreen;
  offscreen = !entries[0].isIntersecting;
  const is = hidden || offscreen;
  if (was !== is) setPaused(is);
}).observe(renderer.domElement);

renderer.setAnimationLoop((now) => {
  if (hidden || offscreen) return;
  watchPerf(now);
  if (explore) controls.update();
  if (clockState.mode === 'play') {
    update(currentTime());
    renderer.render(scene, camera);
  } else if (explore) {
    renderer.render(scene, camera);
  }
});

// full teardown so a SPA-style unload leaks nothing
addEventListener('pagehide', () => {
  renderer.setAnimationLoop(null);
  scene.traverse((o) => {
    o.geometry?.dispose?.();
    const m = o.material;
    if (m) (Array.isArray(m) ? m : [m]).forEach((x) => { x.map?.dispose(); x.emissiveMap?.dispose(); x.dispose(); });
  });
  controls.dispose();
  renderer.dispose();
}, { once: true });

// hold frame 0 under the intro card; the card auto-dismisses into the story
document.body.classList.add('intro');
clockState.mode = 'hold';
clockState.held = 0;
update(0);
renderer.render(scene, camera);
updateHint();
introTimer = setTimeout(() => dismissIntro('story'), 6500);
