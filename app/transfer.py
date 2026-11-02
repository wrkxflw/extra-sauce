"""Transfer a VSCO-style look from one camera to another via Adobe Standard.

new_profile(dst) = dst matrices -> dst Adobe Standard HueSatMap (calibration)
                   -> T (new LookTable) -> source look's tone curve
where T = srcLook . srcHSM . (srcStdHSM)^-1 is sampled on an HSV grid,
then refined by fitting it directly against the look's own output (fit_look).
"""
import numpy as np
from .pipeline import PP_TO_XYZ, XYZ_TO_PP, hsd_apply, hsv2rgb, rgb2hsv, rgb_tone

LOOK_DIMS = (90, 25, 16)   # hues, sats, vals


def stage_hsm(p, x):
    h, s, v = rgb2hsv(np.clip(x, 0, None))
    h, s, v = hsd_apply(p.hsm, p.hsm_dims, h, s, v, p.hsm_srgb)
    return hsv2rgb(h, np.clip(s, 0, 1), np.clip(v, 0, None))


def stage_look(p, x):
    if p.look is None:
        return x
    h, s, v = rgb2hsv(np.clip(x, 0, 1))
    h, s, v = hsd_apply(p.look, p.look_dims, h, s, v, p.look_srgb)
    return hsv2rgb(h, np.clip(s, 0, 1), np.clip(v, 0, 1))


def invert_stage(f, target, iters=60):
    x = target.copy()
    eps = 1e-5
    for _ in range(iters):
        fx = f(x)
        r = fx - target
        J = np.empty(x.shape + (3,))
        for k in range(3):
            dx = np.zeros(3); dx[k] = eps
            J[..., :, k] = (f(x + dx) - fx) / eps
        J += np.eye(3) * 1e-7
        step = np.linalg.solve(J, -r[..., None])[..., 0]
        x = np.clip(x + np.clip(step, -0.05, 0.05), 0, 1)
    return x, np.abs(f(x) - target).max(-1)


def srgb_dec(e):
    return np.where(e <= 0.04045, e / 12.92, ((e + 0.055) / 1.055) ** 2.4)


def srgb_enc(v):
    v = np.clip(v, 0, None)
    return np.where(v <= 0.0031308, v * 12.92, 1.055 * v ** (1 / 2.4) - 0.055)


def build_look(src_look, src_std, dims=LOOK_DIMS):
    nh, ns, nv = dims
    H, Sg, Vg = np.meshgrid(np.arange(nh) * 6.0 / nh, np.linspace(0, 1, ns),
                            srgb_dec(np.linspace(0, 1, nv)), indexing="ij")
    Vg = np.maximum(Vg, 1e-4)
    g = hsv2rgb(H, Sg, Vg)                                   # post-calibration colour
    x, resid = invert_stage(lambda c: stage_hsm(src_std, c), g)
    # if the look uses different camera matrices from Adobe Standard, carry that difference too
    m = XYZ_TO_PP @ src_look.fm @ np.linalg.inv(src_std.fm) @ PP_TO_XYZ
    x = np.clip(x @ m.T, 0, None)
    y = stage_look(src_look, stage_hsm(src_look, x))          # what the look makes of it
    yh, ys, yv = rgb2hsv(y)
    dh = ((yh - H + 3) % 6 - 3) * 60.0                         # degrees
    sat_scale = np.where(Sg > 1e-6, ys / np.maximum(Sg, 1e-6), 1.0)
    val_scale = srgb_enc(yv) / srgb_enc(Vg)                   # sRGB-encoded table scales the encoded value
    # s=0 column: hue undefined -> copy the neighbouring sat column's average so the table is smooth
    dh[:, 0] = 0.0
    sat_scale[:, 0] = sat_scale[:, 1]
    table = np.stack([dh, sat_scale, val_scale], -1)          # (h, s, v, 3)
    table = fill_unreachable(table, resid < 1e-3)
    return np.transpose(table, (2, 0, 1, 3)).astype(np.float32), resid   # (v, h, s, 3)


def fill_unreachable(table, valid, iters=3000):
    """Nodes Adobe Standard can't produce get a smooth extension of their neighbours (hue wraps)."""
    table = table.copy()
    bad = ~valid
    for _ in range(iters):
        p = np.pad(table, ((0, 0), (1, 1), (1, 1), (0, 0)), mode="edge")
        p = np.concatenate([p[-1:], p, p[:1]], axis=0)
        avg = (p[2:, 1:-1, 1:-1] + p[:-2, 1:-1, 1:-1] + p[1:-1, 2:, 1:-1]
               + p[1:-1, :-2, 1:-1] + p[1:-1, 1:-1, 2:] + p[1:-1, 1:-1, :-2]) / 6
        table[bad] = avg[bad]
    return table


def _corners(h, s, v, dims):
    """Flat node indices (N, 8) and trilinear weights (N, 8), matching hsd_apply with sRGB value encoding."""
    nh, ns, nv = dims
    hs, ss, vs = h * (nh / 6.0), s * (ns - 1), srgb_enc(v) * (nv - 1)
    h0 = np.clip(np.floor(hs).astype(int), 0, nh - 1)
    h1 = (h0 + 1) % nh
    s0 = np.clip(np.floor(ss).astype(int), 0, ns - 2)
    v0 = np.clip(np.floor(vs).astype(int), 0, nv - 2)
    hf, sf, vf = hs - h0, ss - s0, vs - v0
    idx, w = [], []
    for vi, vw in ((v0, 1 - vf), (v0 + 1, vf)):
        for hi, hw in ((h0, 1 - hf), (h1, hf)):
            for si, sw in ((s0, 1 - sf), (s0 + 1, sf)):
                idx.append((vi * nh + hi) * ns + si)
                w.append(vw * hw * sw)
    return np.stack(idx, -1), np.clip(np.stack(w, -1), 0, None)


def fit_look(src_look, src_std, table, dims=LOOK_DIMS, n=200000, iters=80, damp=2.0, seed=11):
    """Refine the extracted table so Adobe Standard + table reproduces the look's own output.

    The look stage's input (Adobe Standard's matrices + calibration) and the colour it should produce
    (the original profile before its tone curve) are both known for each sample, so every sample asks for a
    hue shift / sat scale / val scale at its spot in the table. Those are a sparse linear least-squares
    problem in the table nodes, solved by iterative back-projection. `damp` keeps nodes the samples barely
    reach close to the extracted table, so cameras that land there aren't thrown off.
    """
    cam = np.random.default_rng(seed).random((n, 3)) ** 2.2
    pl, ps = cam @ (XYZ_TO_PP @ src_look.fm).T, cam @ (XYZ_TO_PP @ src_std.fm).T
    sc = max(pl.max(), ps.max(), 1.0)
    want = np.clip(src_look.render(pl / sc, tone=False), 0, 1)
    h, s, v = rgb2hsv(np.clip(stage_hsm(src_std, ps / sc), 0, 1))
    th, ts, tv = rgb2hsv(want)
    keep = v > 1e-4
    h, s, v, th, ts, tv = h[keep], s[keep], v[keep], th[keep], ts[keep], tv[keep]
    req = np.stack([((th - h + 3) % 6 - 3) * 60.0,
                    np.where(s > 1e-3, ts / np.maximum(s, 1e-3), 1.0),
                    srgb_enc(tv) / np.maximum(srgb_enc(v), 1e-6)], -1)
    imp = np.stack([s * v, s * np.sqrt(v), np.ones_like(v)], -1)   # hue only shows with chroma, sat with brightness
    idx, w = _corners(h, s, v, dims)
    nh, ns, nv = dims
    T = table.reshape(-1, 3).astype(np.float64)                  # (v, h, s) order, flattened
    wsum = np.zeros_like(T)
    for k in range(3):
        np.add.at(wsum[:, k], idx.ravel(), (w * imp[:, k:k + 1]).ravel())
    lam = damp * np.array([np.median(wsum[wsum[:, k] > 0, k]) for k in range(3)])
    for _ in range(iters):
        r = req - np.einsum("nk,nkc->nc", w, T[idx])
        r[:, 0] = (r[:, 0] + 180) % 360 - 180
        upd = np.zeros_like(T)
        for k in range(3):
            np.add.at(upd[:, k], idx.ravel(), (w * (imp[:, k] * r[:, k])[:, None]).ravel())
        T += upd / (wsum + lam)
        T[:, 1:] = np.clip(T[:, 1:], 0.0, 4.0)
    return T.reshape(nv, nh, ns, 3).astype(np.float32)


class Transferred:
    """Simulated rendering of the generated profile for camera `dst_std`."""

    def __init__(self, dst_std, look_table, tone_x, tone_y, dims=LOOK_DIMS):
        self.std, self.look, self.dims = dst_std, look_table, dims
        self.cx, self.cy = tone_x, tone_y
        self.fm = dst_std.fm

    def render(self, x):
        x = stage_hsm(self.std, x)
        h, s, v = rgb2hsv(np.clip(x, 0, 1))
        h, s, v = hsd_apply(self.look, self.dims, h, s, v, srgb_gamma=True)
        x = hsv2rgb(h, np.clip(s, 0, 1), np.clip(v, 0, 1))
        return rgb_tone(x, self.cx, self.cy)
