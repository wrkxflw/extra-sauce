"""DNG/ACR camera-profile pipeline model: HueSatMap -> LookTable -> tone curve, in ProPhoto linear."""
from pathlib import Path

import numpy as np

from .dcp import read_dcp

HERE = Path(__file__).resolve().parent
ACR3 = np.loadtxt(HERE / "acr3_curve.txt")   # Adobe's default tone curve, used by profiles that carry none
# EXIF LightSource codes -> colour temperature (K)
TEMPS = {1: 5500.0, 2: 4150.0, 3: 2850.0, 4: 5500.0, 9: 5500.0, 10: 6500.0, 11: 7500.0, 12: 6430.0,
         13: 5000.0, 14: 4150.0, 15: 3525.0, 17: 2856.0, 18: 4874.0, 19: 6774.0, 20: 5503.0,
         21: 6504.0, 22: 7504.0, 23: 5003.0, 24: 3200.0}
D50 = np.array([0.9642, 1.0, 0.8249])


def rgb2hsv(rgb):
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    v = rgb.max(-1)
    mn = rgb.min(-1)
    gap = v - mn
    s = np.where(v > 0, gap / np.where(v > 0, v, 1), 0)
    safe = np.where(gap > 0, gap, 1)
    h = np.where(r == v, (g - b) / safe, np.where(g == v, 2 + (b - r) / safe, 4 + (r - g) / safe))
    h = np.where(gap > 0, h % 6, 0)
    return h, s, v


def hsv2rgb(h, s, v):
    h = h % 6
    i = np.floor(h).astype(int)
    f = h - i
    p, q, t = v * (1 - s), v * (1 - s * f), v * (1 - s * (1 - f))
    choices = [(v, t, p), (q, v, p), (p, v, t), (p, q, v), (t, p, v), (v, p, q)]
    out = np.zeros(h.shape + (3,))
    for k, (a, b, c) in enumerate(choices):
        m = i == k
        out[m] = np.stack([a[m], b[m], c[m]], -1)
    return out


def hsd_apply(table, dims, h, s, v, srgb_gamma=False):
    """table shape (vals, hues, sats, 3) of (hue_shift_deg, sat_scale, val_scale)."""
    nh, ns, nv = dims
    hs = h * (nh / 6.0)
    ss = s * (ns - 1)
    venc = v
    if srgb_gamma:
        venc = np.where(v <= 0.0031308, v * 12.92, 1.055 * np.power(np.clip(v, 0, None), 1 / 2.4) - 0.055)
    vs = venc * (nv - 1) if nv > 1 else np.zeros_like(v)
    h0 = np.clip(np.floor(hs).astype(int), 0, nh - 1)
    h1 = (h0 + 1) % nh
    s0 = np.clip(np.floor(ss).astype(int), 0, ns - 2)
    v0 = np.clip(np.floor(vs).astype(int), 0, max(nv - 2, 0))
    v1 = np.minimum(v0 + 1, nv - 1)
    hf, sf, vf = hs - h0, ss - s0, vs - v0
    if nv == 1:
        vf = np.zeros_like(vf)
    acc = 0
    for vi, vw in ((v0, 1 - vf), (v1, vf)):
        for hi, hw in ((h0, 1 - hf), (h1, hf)):
            for si, sw in ((s0, 1 - sf), (s0 + 1, sf)):
                acc = acc + (vw * hw * sw)[..., None] * table[vi, hi, si]
    hue_shift, sat_scale, val_scale = acc[..., 0], acc[..., 1], acc[..., 2]
    h = h + hue_shift * 6.0 / 360.0
    s = s * sat_scale
    if srgb_gamma:
        ve = venc * val_scale
        v = np.where(ve <= 0.04045, ve / 12.92, np.power(np.clip((ve + 0.055) / 1.055, 0, None), 2.4))
    else:
        v = v * val_scale
    return h, s, v


def rgb_tone(rgb, curve_x, curve_y):
    """DNG RefBaselineRGBTone: curve on max & min, middle interpolated (hue-preserving)."""
    rgb = np.clip(rgb, 0, 1)
    f = lambda x: np.interp(x, curve_x, curve_y)
    order = np.argsort(rgb, -1)
    lo = np.take_along_axis(rgb, order[..., :1], -1)[..., 0]
    mid = np.take_along_axis(rgb, order[..., 1:2], -1)[..., 0]
    hi = np.take_along_axis(rgb, order[..., 2:], -1)[..., 0]
    flo, fhi = f(lo), f(hi)
    gap = hi - lo
    fmid = np.where(gap > 0, flo + (fhi - flo) * (mid - lo) / np.where(gap > 0, gap, 1), fhi)
    out = np.empty_like(rgb)
    np.put_along_axis(out, order[..., :1], flo[..., None], -1)
    np.put_along_axis(out, order[..., 1:2], fmid[..., None], -1)
    np.put_along_axis(out, order[..., 2:], fhi[..., None], -1)
    return out


class Profile:
    def __init__(self, path, temp=5500.0):
        d = read_dcp(path)
        self.d = d
        mix = 1.0
        if "CalibrationIlluminant2" in d and "ColorMatrix2" in d:
            t1 = TEMPS.get(int(d.get("CalibrationIlluminant1", 17)), 2856.0)
            t2 = TEMPS.get(int(d["CalibrationIlluminant2"]), 6504.0)
            if t1 != t2:
                mix = float(np.clip((1 / temp - 1 / t2) / (1 / t1 - 1 / t2), 0, 1))
        if "HueSatMapData1" in d:
            nh, ns, nv = (int(x) for x in d["HueSatMapDims"])
            m1 = d["HueSatMapData1"].reshape(nv, nh, ns, 3)
            m2 = d.get("HueSatMapData2", d["HueSatMapData1"]).reshape(nv, nh, ns, 3)
            self.hsm = mix * m1 + (1 - mix) * m2
        else:   # no calibration table: identity map
            nh, ns, nv = 6, 2, 1
            self.hsm = np.tile(np.array([0.0, 1.0, 1.0]), (nv, nh, ns, 1))
        self.hsm_dims = (nh, ns, nv)
        self.hsm_srgb = int(d.get("HueSatMapEncoding", 0)) == 1
        self.look = None
        if "LookTableData" in d:
            lh, ls, lv = (int(x) for x in d["LookTableDims"])
            self.look = d["LookTableData"].reshape(lv, lh, ls, 3)
            self.look_dims = (lh, ls, lv)
            self.look_srgb = int(d.get("LookTableEncoding", 0)) == 1
        if "ProfileToneCurve" in d:
            tc = d["ProfileToneCurve"].reshape(-1, 2)
            self.cx, self.cy = tc[:, 0], tc[:, 1]
        else:
            self.cx, self.cy = np.linspace(0, 1, len(ACR3)), ACR3
        if "ForwardMatrix1" in d:
            fm1 = d["ForwardMatrix1"].reshape(3, 3)
            fm2 = d.get("ForwardMatrix2", d["ForwardMatrix1"]).reshape(3, 3)
            self.fm = mix * fm1 + (1 - mix) * fm2
        else:   # colour-matrix-only profile: approximate camera->XYZ(D50) with neutral -> D50 white
            cm = np.linalg.inv(d["ColorMatrix1"].reshape(3, 3))
            self.fm = cm * (D50 / (cm @ np.ones(3)))[:, None]

    def render(self, pp_lin, tone=True):
        """pp_lin: ProPhoto linear (post camera matrix), returns ProPhoto linear output."""
        h, s, v = rgb2hsv(np.clip(pp_lin, 0, None))
        h, s, v = hsd_apply(self.hsm, self.hsm_dims, h, s, v, self.hsm_srgb)
        x = np.clip(hsv2rgb(h, np.clip(s, 0, 1), np.clip(v, 0, None)), 0, None)
        if self.look is not None:
            xc = np.clip(x, 0, 1)
            h, s, v = rgb2hsv(xc)
            h, s, v = hsd_apply(self.look, self.look_dims, h, s, v, self.look_srgb)
            x = hsv2rgb(h, np.clip(s, 0, 1), np.clip(v, 0, 1))
        if tone:
            x = rgb_tone(x, self.cx, self.cy)
        return x


# XYZ(D50) -> ProPhoto linear
XYZ_TO_PP = np.linalg.inv(np.array([[0.7976749, 0.1351917, 0.0313534],
                                     [0.2880402, 0.7118741, 0.0000857],
                                     [0.0000000, 0.0000000, 0.8252100]]))
PP_TO_XYZ = np.linalg.inv(XYZ_TO_PP)
XYZ_TO_SRGB_D50 = np.array([[3.1338561, -1.6168667, -0.4906146],
                            [-0.9787684, 1.9161415, 0.0334540],
                            [0.0719453, -0.2289914, 1.4052427]])


def pp_encode(lin):
    return np.power(np.clip(lin, 0, 1), 1 / 1.8)


def pp_decode(enc):
    return np.power(np.clip(enc, 0, 1), 1.8)


def srgb_encode(lin):
    lin = np.clip(lin, 0, 1)
    return np.where(lin <= 0.0031308, lin * 12.92, 1.055 * np.power(lin, 1 / 2.4) - 0.055)


def srgb_decode(enc):
    enc = np.clip(enc, 0, 1)
    return np.where(enc <= 0.04045, enc / 12.92, np.power((enc + 0.055) / 1.055, 2.4))


def lab(pp_lin):
    """ProPhoto linear -> CIELAB (D50)."""
    xyz = np.clip(pp_lin, 0, None) @ PP_TO_XYZ.T / np.array([0.9642, 1.0, 0.8249])
    f = np.where(xyz > 216 / 24389, np.cbrt(xyz), (24389 / 27 * xyz + 16) / 116)
    return np.stack([116 * f[..., 1] - 16, 500 * (f[..., 0] - f[..., 1]), 200 * (f[..., 1] - f[..., 2])], -1)
