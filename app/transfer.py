"""Transfer a VSCO-style look from one camera to another via Adobe Standard.

new_profile(dst) = dst matrices -> dst Adobe Standard HueSatMap (calibration)
                   -> T (new LookTable) -> source look's tone curve
where T = srcLook . srcHSM . (srcStdHSM)^-1 is sampled on an HSV grid.
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
