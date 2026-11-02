"""Data for the accuracy map: where on the colour wheel a converted look differs from the original.

Uses the same 40,000 colours as the self-check. Everything is returned as plain lists for the page
to draw; nothing here renders.
"""
from __future__ import annotations

import numpy as np

from .pipeline import XYZ_TO_PP, XYZ_TO_SRGB_D50, lab
from .transfer import Transferred

# RGB -> XYZ (D50) for the gamuts the map can outline
GAMUTS = {
    "srgb": np.linalg.inv(XYZ_TO_SRGB_D50),
    # Display P3 (D65), Bradford-adapted to D50
    "p3": np.array([[1.0478112, 0.0228866, -0.0501270], [0.0295424, 0.9904844, -0.0170491], [-0.0092345, 0.0150436, 0.7521316]])
          @ np.array([[0.4865709, 0.2656677, 0.1982173], [0.2289746, 0.6917385, 0.0792869], [0.0, 0.0451134, 1.0439444]]),
    "adobe": np.array([[0.6097559, 0.2052401, 0.1492240], [0.3111242, 0.6256560, 0.0632197], [0.0194811, 0.0608902, 0.7448387]]),
}
BANDS = [(0, 35), (35, 70), (70, 101)]           # shadows, midtones, highlights (Lab lightness)
_EDGE_BANDS = [(15, 35), (35, 70), (70, 95)]     # lightness range used to draw each band's outline
_gamut_labs = {}


def _gamut_lab(name):
    """Lab of a 48^3 grid filling the gamut (cached)."""
    if name not in _gamut_labs:
        g = np.stack(np.meshgrid(*[np.linspace(0, 1, 48)] * 3, indexing="ij"), -1).reshape(-1, 3)
        lin = g ** 2.2 if name == "adobe" else np.where(g <= 0.04045, g / 12.92, ((g + 0.055) / 1.055) ** 2.4)
        _gamut_labs[name] = lab(lin @ (XYZ_TO_PP @ GAMUTS[name]).T)
    return _gamut_labs[name]


def _edge(name, lo, hi):
    """Widest chroma per 5-degree hue for the gamut's colours in a lightness range."""
    gl = _gamut_lab(name)
    m = (gl[:, 0] >= lo) & (gl[:, 0] < hi)
    h = np.degrees(np.arctan2(gl[m, 2], gl[m, 1])) % 360
    e = np.zeros(72)
    np.maximum.at(e, (h // 5).astype(int), np.hypot(gl[m, 1], gl[m, 2]))
    return np.median([np.roll(e, -1), e, np.roll(e, 1)], axis=0)      # drop single-bin notches


def _inside(name, Lab):
    fy = (Lab[:, 0] + 16) / 116
    fx, fz = fy + Lab[:, 1] / 500, fy - Lab[:, 2] / 200
    finv = lambda q: np.where(q > 6 / 29, q ** 3, 3 * (6 / 29) ** 2 * (q - 4 / 29))
    xyz = np.stack([finv(fx) * 0.96422, finv(fy), finv(fz) * 0.82521], -1)
    rgb = xyz @ np.linalg.inv(GAMUTS[name]).T
    return np.all((rgb > -0.01) & (rgb < 1.01), -1)


def _stats(d):
    return [round(float(d.mean()), 2), round(float(np.percentile(d, 95)), 2)] if len(d) else [0, 0]


def map_data(look, std, table, cx, cy, n=40000):
    rng = np.random.default_rng(7)
    cam = rng.random((n, 3)) ** 2.2
    pl, ps = cam @ (XYZ_TO_PP @ look.fm).T, cam @ (XYZ_TO_PP @ std.fm).T
    s = max(pl.max(), ps.max(), 1.0)
    want = lab(look.render(pl / s))
    got = lab(Transferred(std, table, cx, cy).render(ps / s))
    de = np.linalg.norm(got - want, axis=-1)
    inside = {g: _inside(g, want) for g in GAMUTS}
    L = want[:, 0]

    bands = []
    both_srgb = inside["srgb"] & _inside("srgb", got)
    for (lo, hi), (elo, ehi) in zip(BANDS, _EDGE_BANDS):
        sel = (L >= lo) & (L < hi)
        cand = np.where(sel & both_srgb)[0]
        worst = int(cand[np.argmax(de[cand])]) if len(cand) else None
        bands.append({
            "stats": _stats(de[sel]),
            "max": round(float(de[sel].max()), 1) if sel.any() else 0,
            "edges": {g: np.round(_edge(g, elo, ehi), 1).tolist() for g in GAMUTS},
            "strip": None if worst is None else {"in": np.round(want[worst], 2).tolist(), "out": np.round(got[worst], 2).tolist(),
                                                 "de": round(float(de[worst]), 1)},
        })
    top = np.argsort(de)[-n // 10:]
    drift = top[np.linspace(0, len(top) - 1, 400).astype(int)]
    mask = sum(inside[g].astype(int) << k for k, g in enumerate(GAMUTS))
    return {
        "stats": _stats(de),
        "bands": bands,
        "rim": float(np.ceil(np.hypot(want[:, 1], want[:, 2]).max() / 10) * 10 + 5),
        # every test colour, flattened: L, a, b, dE, gamut bits (1 sRGB, 2 P3, 4 Adobe RGB); the page works out
        # the inside-the-outline figures itself, since they follow the toggles
        "dots": np.column_stack([np.round(want, 1), np.round(de, 2), mask]).ravel().tolist(),
        "drift": np.column_stack([np.round(want[drift, 1:], 1), np.round(got[drift, 1:], 1), np.round(de[drift], 2)]).ravel().tolist(),
    }
