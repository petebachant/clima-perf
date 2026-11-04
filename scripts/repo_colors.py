"""One color per CliMA repo, shared by every figure.

A repo's color depends only on its name, never on its rank in a given
chart, so it reads the same in every figure and doesn't shift when the
ranking changes. The highest-profile repos have pinned colors; every other
tracked repo gets the next palette color in the order of
``clima_repos.CLIMA_REPOS``, and any other name a stable color from its
hash.
"""

from __future__ import annotations

import zlib

import plotly.colors as pc

from clima_repos import CLIMA_REPOS

PINNED_COLORS = {
    "Oceananigans": "#1F77B4",  # blue
    "ClimaAtmos": "#EDB120",  # yellow
    "ClimaCore": "#D62728",  # red
    "ClimaLand": "#2CA02C",  # green
}
# Lumped-together remainder, e.g., "Other"
OTHER_COLOR = "#a8a7a1"

# The pinned hues are removed so nothing else reuses them
_PALETTE = [
    c
    for c in (
        list(pc.qualitative.Plotly)
        + list(pc.qualitative.D3)
        + list(pc.qualitative.Light24)
        + list(pc.qualitative.Dark24)
    )
    if c.upper() not in {v.upper() for v in PINNED_COLORS.values()}
]
_ASSIGNED = {
    name: _PALETTE[i % len(_PALETTE)]
    for i, name in enumerate(n for n in CLIMA_REPOS if n not in PINNED_COLORS)
}


def repo_color(name: str) -> str:
    """The color for repo or package ``name``, with or without ``.jl``."""
    name = name.removesuffix(".jl")
    if name in PINNED_COLORS:
        return PINNED_COLORS[name]
    if name in _ASSIGNED:
        return _ASSIGNED[name]
    return _PALETTE[zlib.crc32(name.encode()) % len(_PALETTE)]
