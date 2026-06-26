"""Render STL meshes to shaded PNG previews (headless, matplotlib).

Engineering-preview quality, not photoreal -- enough to eyeball a part / assembly
from several angles ("print screens"). For clean technical line drawings we also
use CadQuery's SVG projection exporter elsewhere.
"""

from __future__ import annotations

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from mpl_toolkits.mplot3d.art3d import Poly3DCollection
from stl import mesh as stlmesh


def _set_equal(ax, verts):
    mins = verts.reshape(-1, 3).min(axis=0)
    maxs = verts.reshape(-1, 3).max(axis=0)
    c = (mins + maxs) / 2
    r = (maxs - mins).max() / 2
    ax.set_xlim(c[0] - r, c[0] + r)
    ax.set_ylim(c[1] - r, c[1] + r)
    ax.set_zlim(c[2] - r, c[2] + r)


def render(stl_paths, png_path, *, views=None, color="#9fb3c8", title=None):
    """Render one or more STL files together into a single PNG with sub-views."""
    if isinstance(stl_paths, (str, bytes)):
        stl_paths = [(stl_paths, color)]
    elif stl_paths and isinstance(stl_paths[0], str):
        stl_paths = [(p, color) for p in stl_paths]

    meshes = [(stlmesh.Mesh.from_file(p), c) for p, c in stl_paths]
    all_v = np.concatenate([m.vectors.reshape(-1, 3) for m, _ in meshes])
    views = views or [(28, -55), (0, -90), (90, -90)]

    fig = plt.figure(figsize=(5 * len(views), 5))
    if title:
        fig.suptitle(title)
    for i, (elev, azim) in enumerate(views, 1):
        ax = fig.add_subplot(1, len(views), i, projection="3d")
        for m, c in meshes:
            poly = Poly3DCollection(m.vectors, alpha=1.0)
            poly.set_facecolor(c)
            poly.set_edgecolor((0, 0, 0, 0.12))
            poly.set_linewidth(0.15)
            ax.add_collection3d(poly)
        _set_equal(ax, all_v)
        ax.view_init(elev=elev, azim=azim)
        ax.set_box_aspect((1, 1, 1))
        ax.set_axis_off()
    fig.tight_layout()
    fig.savefig(png_path, dpi=130, bbox_inches="tight")
    plt.close(fig)
    return png_path
