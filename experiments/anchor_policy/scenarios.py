"""Scenario randomisation -- "every scenario thinkable" for station-keeping.

Each scenario randomises the environment (wind, current, gusts, slow weather
wander), the boat (mass, hull character, thruster mount, motor power) and the
starting condition (offset from the anchor, heading, initial drift). A scenario
is a plain dict derived deterministically from a single integer seed, so a
generation can score every candidate policy on the *same* batch of scenarios
(common random numbers) for a fair, low-variance comparison.
"""

from __future__ import annotations

import math

import numpy as np

_MOUNTS = {"bow": 1.7, "stern": -1.7, "center": 0.0}


def sample_scenario(seed: int) -> dict:
    rng = np.random.default_rng(seed)

    # ~20% of scenarios are calm, so the policy keeps a strong "just hold still"
    # signal; the rest span up to a stiff blow and a real current.
    # ~15% are "at rest on the mark, calm": the boat starts ON the anchor, dead
    # still, no disturbance -- so the policy explicitly learns to IDLE at zero
    # (the case the v2 policy mis-extrapolated to full reverse, the live failure).
    rest = rng.random() < 0.15
    calm = rest or rng.random() < 0.2
    # Cap wind at ~9 m/s (a stiff ~18 kn): beyond that a small trolling boat
    # can't physically hold station, and those unwinnable scenarios only cap the
    # metrics + add gradient noise.
    wind = 0.0 if (rest or (calm and rng.random() < 0.5)) else rng.uniform(0.0, 9.0)
    cur = 0.0 if calm else rng.uniform(0.0, 1.2)
    mount = _MOUNTS[rng.choice(["bow", "stern", "center"], p=[0.6, 0.2, 0.2])]

    return {
        "seed": int(seed),
        # environment
        "wind_speed": float(wind),
        "wind_dir": float(rng.uniform(0.0, 360.0)),
        "current_speed": float(cur),
        "current_dir": float(rng.uniform(0.0, 360.0)),
        "gust": float(0.0 if calm else rng.uniform(0.0, 0.35) * max(wind, 1.0)),
        "gust_tau": float(rng.uniform(3.0, 8.0)),
        "wind_var": float(0.0 if calm else rng.uniform(0.0, 0.5)),
        "cur_var": float(0.0 if calm else rng.uniform(0.0, 0.4)),
        # boat (generalise across the fleet the README advertises)
        "mass": float(rng.uniform(200.0, 400.0)),
        "hull_tracking": float(rng.uniform(0.35, 2.5)),
        "thruster_x_m": float(mount),
        "max_thrust_n": float(rng.uniform(210.0, 300.0)),
        # start condition
        "start_dist": float(rng.uniform(0.0, 1.5) if rest else rng.uniform(0.0, 12.0)),
        "start_bearing": float(rng.uniform(0.0, 2 * math.pi)),
        "heading": float(rng.uniform(0.0, 360.0)),
        "u0": float(0.0 if rest else rng.uniform(-0.3, 0.6)),
        "v0": float(0.0 if rest else rng.uniform(-0.3, 0.3)),
    }


def scenario_batch(gen_seed: int, k: int) -> list[dict]:
    """K scenarios for one generation -- derived from the generation seed so all
    candidates in the generation face the identical batch."""
    return [sample_scenario((gen_seed * 1_000_003 + i) & 0x7FFFFFFF) for i in range(k)]


# A fixed, held-out validation set (never used for the ES gradient) so the
# logged learning curve and "best so far" checkpoint reflect true generalisation.
_VALIDATION_BASE = 0x5A1D0000


def validation_batch(k: int = 64) -> list[dict]:
    return [sample_scenario(_VALIDATION_BASE + i) for i in range(k)]
