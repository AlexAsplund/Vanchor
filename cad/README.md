# cad/ — handoff

Parametric, code-driven CAD for Vanchor-NG hardware. Everything regenerates from
source; STEP/STL/PNG outputs live under `cad/out/` (and can be deleted + rebuilt).

## Toolchain (important — this machine is aarch64)
- **build123d** is the CAD kernel (OpenCASCADE/OCP), in the project `.venv`. It
  exports **STEP + STL**.
- **CadQuery does NOT work here**: PyPI only ships OCP **7.9** wheels for py3.12,
  which no CadQuery release supports. `enclosure.py` is an old CadQuery demo kept
  for reference — **it will not run**. Port it to build123d if needed.
- **OpenSCAD**: no usable binary (no root; only an x86_64 AppImage exists, won't
  run on aarch64).
- A stub `site-packages/nlopt.py` exists so leftover CadQuery can import; harmless.
- PNG "print-screens" render headless via **matplotlib mplot3d** from STL
  (`render.py`) — preview quality, **no true hidden-surface removal** (far walls
  ghost through). For clean filled views/sections, open the STEP in FreeCAD/Fusion/
  an online viewer and apply a section plane.

Setup from scratch (if the venv is rebuilt):
`pip install build123d numpy-stl matplotlib`

## Files
| File | What |
|---|---|
| `steering.py` | **Main deliverable.** Parametric steering/azimuth gearbox (v2). All dims in the `P` dataclass. `python cad/steering.py` → STEP+STL+assembly. |
| `steering_BOM.md` | Bill of materials, cheap/reliable part choices, build + firmware/integration notes. |
| `steering_REVIEW.md` | 10-iteration design review (10×10 checklist, v1 flaws → v2 fixes → what remains). |
| `make_renders.py` | Per-part + assembled + exploded PNGs → `out/renders/`. |
| `make_section.py` | Centre-plane cross-section PNG → `out/renders/section.png`. |
| `render.py` | STL → shaded multi-view PNG helper. |
| `enclosure.py` | CadQuery electronics-box demo — **does not run here** (see above). |

Run renders from inside `cad/` (they `import steering`):
`cd cad && python make_renders.py && python make_section.py`

## Design state — steering gearbox (v2)
A closed-loop "servo" that swings the trolling motor. The 1" shaft slides into a
rotating **turret** socket (top split-clamp grips it); a quiet, self-locking **12 V
worm gearmotor** (mounted under the housing) drives a **pinion → ring gear** (4:1);
an **AS5600** magnetic absolute encoder reads turret angle for feedback + cable-
wrap limit; sealed housing (gasket+bolts, O-ring on the turret, weep, breather).
Current size: module 2, ~**202×142×44 mm**. Parts: `pinion, ring_gear,
turret_holder, housing_bottom, housing_top`.

Key params to tune in `P`: `module`/`ring_teeth` (size vs torque), `shaft_dia`,
`bearing_*`, `motor_*`, `enc_gap`, seal/clearance fields.

## Open items (from the review — need a real prototype)
- Boat-mount **flange/feet** (not yet modelled).
- **Motor-mount slots** for backlash/mesh adjustment.
- True **lip seal on an SS sleeve** for real IP rating (today: splash/rain only).
- **Involute** gears in nylon for production (current teeth are simplified).
- Manual **declutch** / hard end-stop; finalize fuse/H-bridge spec.
- Torque/FEA validation; coupon-tuned print fits.
- Optional: shrink the box via module 1.5 or a 2-stage reduction.

## Status
Geometry builds and exports cleanly; renders are current. Not yet
prototyped/printed. No automated tests cover CAD (geometry is validated visually).
