"""A tiny tanh-MLP policy for station-keeping (the anchor function).

Pure numpy: a forward pass is a handful of small matrix multiplies, so it runs
in microseconds on a Raspberry Pi with no ML runtime. The weights are a single
flat vector so Evolution Strategies can perturb/update them directly, and the
whole policy serialises to a small JSON file for deployment.
"""

from __future__ import annotations

import json

import numpy as np


class TinyPolicy:
    """obs -> [thrust, steering], both in [-1, 1] (a MotorCommand)."""

    def __init__(self, sizes=(8, 24, 16, 2), params=None, rng=None):
        self.sizes = tuple(sizes)
        # Parameter layout: (W, b) per layer, flattened in order.
        self._shapes = []
        for a, b in zip(self.sizes[:-1], self.sizes[1:]):
            self._shapes.append((a, b))  # weight
            self._shapes.append((b,))    # bias
        self.n_params = int(sum(int(np.prod(s)) for s in self._shapes))
        if params is not None:
            self.set_params(np.asarray(params, dtype=np.float64))
        else:
            rng = rng or np.random.default_rng()
            # Small init keeps the initial policy gentle (near-zero output).
            self.set_params(rng.standard_normal(self.n_params) * 0.1)

    def set_params(self, flat: np.ndarray) -> None:
        self._theta = np.asarray(flat, dtype=np.float64).copy()
        self._layers = []
        i = 0
        for k in range(0, len(self._shapes), 2):
            wsh, bsh = self._shapes[k], self._shapes[k + 1]
            wn, bn = int(np.prod(wsh)), int(np.prod(bsh))
            W = self._theta[i:i + wn].reshape(wsh); i += wn
            b = self._theta[i:i + bn].reshape(bsh); i += bn
            self._layers.append((W, b))

    def get_params(self) -> np.ndarray:
        return self._theta.copy()

    def forward(self, obs) -> np.ndarray:
        x = np.asarray(obs, dtype=np.float64)
        last = len(self._layers) - 1
        for k, (W, b) in enumerate(self._layers):
            x = x @ W + b
            if k < last:
                x = np.tanh(x)
        return np.tanh(x)  # [thrust, steering] in [-1, 1]

    def save(self, path: str, meta: dict | None = None) -> None:
        """Serialise to JSON. ``meta`` adds extra metadata keys (e.g. the
        ``steer_sign`` polarity convention the runtime reads); ``sizes`` and
        ``params`` always win over colliding meta keys."""
        d = dict(meta or {})
        d.update({"sizes": list(self.sizes), "params": self._theta.tolist()})
        json.dump(d, open(path, "w"))

    @classmethod
    def load(cls, path: str) -> "TinyPolicy":
        d = json.load(open(path))
        return cls(sizes=tuple(d["sizes"]), params=np.array(d["params"]))


OBS_DIM = 8
ACT_DIM = 2
