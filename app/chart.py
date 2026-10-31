"""Copy a phone-app preset (VSCO etc.) into a 3D LUT using a colour chart.

The chart holds every combination of 25 levels of R, G and B as 12px patches inside a
mid-grey border. Run it through the preset, and each patch's new colour is one entry of
the LUT. The border shows whether the preset also does something spatial (vignette,
grain), which a LUT can't hold.
"""
from __future__ import annotations

import io
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from PIL import Image, ImageCms

LEVELS, PATCH, BORDER, SIDE = 25, 12, 24, 125          # 25^3 = 125 x 125 patches
SIZE = SIDE * PATCH + 2 * BORDER                         # 1548 px square
BORDER_GREY = 128
CUBE_SIZE = 33


def make_chart() -> Image.Image:
    lv = np.round(np.linspace(0, 255, LEVELS)).astype(np.uint8)
    r, g, b = np.meshgrid(lv, lv, lv, indexing="ij")
    grid = np.stack([r, g, b], -1).reshape(SIDE, SIDE, 3)        # red changes slowest
    out = np.full((SIZE, SIZE, 3), BORDER_GREY, np.uint8)
    out[BORDER:-BORDER, BORDER:-BORDER] = np.repeat(np.repeat(grid, PATCH, 0), PATCH, 1)
    return Image.fromarray(out)


def save_chart(folder: Path) -> list[Path]:
    folder.mkdir(parents=True, exist_ok=True)
    im = make_chart()
    icc = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    paths = [folder / "Preset chart.png", folder / "Preset chart.jpg"]
    im.save(paths[0], icc_profile=icc)
    im.save(paths[1], quality=100, subsampling=0, icc_profile=icc)
    return paths


@dataclass
class ChartReading:
    lut: np.ndarray                     # (25,25,25,3) floats 0-1, indexed [r,g,b]
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


class ChartError(Exception):
    pass


def _to_srgb(im: Image.Image) -> tuple[Image.Image, str | None]:
    """Convert a tagged non-sRGB export (e.g. iPhone Display P3) to sRGB."""
    icc = im.info.get("icc_profile")
    im = im.convert("RGB")
    if not icc:
        return im, None
    try:
        src = ImageCms.ImageCmsProfile(io.BytesIO(icc))
        name = ImageCms.getProfileDescription(src).strip()
    except Exception:
        return im, None
    if "srgb" in name.lower().replace(" ", ""):
        return im, None
    dst = ImageCms.createProfile("sRGB")
    return ImageCms.profileToProfile(im, src, dst, renderingIntent=ImageCms.Intent.RELATIVE_COLORIMETRIC), name


def read_export(path: Path) -> ChartReading:
    im = Image.open(path)
    w, h = im.size
    if abs(w - h) > max(w, h) * 0.01:
        raise ChartError(f"it's {w}x{h}, but the chart is square. Was it cropped? Export the whole chart.")
    im, converted_from = _to_srgb(im)
    a = np.asarray(im).astype(float) / 255
    scale = w / SIZE
    notes, warnings = [], []
    if converted_from:
        notes.append(f"converted from {converted_from} to sRGB")
    if abs(scale - 1) > 0.01:
        notes.append(f"exported at {w}px instead of {SIZE}px; scaled to match")
    if scale < 0.6:
        warnings.append("the export is quite small, so patches are blurred together. Export at full size if you can.")

    # sample the middle half of each patch
    half = max(1, int(PATCH * scale * 0.25))
    centres = (BORDER + (np.arange(SIDE) + 0.5) * PATCH) * scale
    lut = np.empty((SIDE, SIDE, 3))
    for i, cy in enumerate(centres):
        y = int(round(cy))
        rows = a[y - half:y + half]
        for j, cx in enumerate(centres):
            x = int(round(cx))
            lut[i, j] = np.median(rows[:, x - half:x + half].reshape(-1, 3), 0)
    lut = lut.reshape(LEVELS, LEVELS, LEVELS, 3)

    # border: should be one even colour all round if the preset is purely a colour mapping
    b = max(2, int(BORDER * scale * 0.25))
    mid = int(BORDER * scale / 2)
    spots = [(mid, mid), (mid, w // 2), (mid, w - mid), (h // 2, mid), (h // 2, w - mid),
             (h - mid, mid), (h - mid, w // 2), (h - mid, w - mid)]
    means = np.array([a[y - b:y + b, x - b:x + b].reshape(-1, 3).mean(0) for y, x in spots]) * 255
    spread = float(np.ptp(means, axis=0).max())
    noise = float(np.mean([a[y - b:y + b, x - b:x + b].reshape(-1, 3).std(0).max() for y, x in spots]) * 255)
    if spread > 4:
        warnings.append(f"the border isn't even (varies by {spread:.0f}/255), so the preset probably adds a "
                        "vignette or light leak. The LUT holds the colour only; add the vignette with Lightroom's sliders.")
    if noise > 6:
        warnings.append("the border is grainy, so the preset probably adds grain. The LUT ignores grain; "
                        "use Lightroom's Grain slider for that.")
    ident = np.stack(np.meshgrid(*[np.linspace(0, 1, LEVELS)] * 3, indexing="ij"), -1)
    if np.abs(lut - ident).mean() * 255 < 1.5:
        warnings.append("this looks almost identical to the blank chart. Was the preset applied?")
    return ChartReading(lut, warnings, notes)


def _cubic_weights(n_in: int, n_out: int) -> np.ndarray:
    """Catmull-Rom resampling matrix (n_out x n_in) on the unit interval."""
    W = np.zeros((n_out, n_in))
    for o, t in enumerate(np.linspace(0, n_in - 1, n_out)):
        i = min(int(np.floor(t)), n_in - 2)
        f = t - i
        ws = [(-f ** 3 + 2 * f ** 2 - f) / 2, (3 * f ** 3 - 5 * f ** 2 + 2) / 2,
              (-3 * f ** 3 + 4 * f ** 2 + f) / 2, (f ** 3 - f ** 2) / 2]
        for k, wgt in zip(range(i - 1, i + 3), ws):
            W[o, min(max(k, 0), n_in - 1)] += wgt
    return W


def resample(lut: np.ndarray, n: int = CUBE_SIZE) -> np.ndarray:
    W = _cubic_weights(lut.shape[0], n)
    out = np.einsum("ai,ijkc->ajkc", W, lut)
    out = np.einsum("bj,ajkc->abkc", W, out)
    out = np.einsum("ck,abkd->abcd", W, out)
    return np.clip(out, 0, 1)


def write_cube(path: Path, lut: np.ndarray, title: str) -> None:
    n = lut.shape[0]
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f'TITLE "{title}"', f"LUT_3D_SIZE {n}", "DOMAIN_MIN 0 0 0", "DOMAIN_MAX 1 1 1"]
    flat = lut.transpose(2, 1, 0, 3).reshape(-1, 3)          # .cube order: red changes fastest
    lines += ["%.6f %.6f %.6f" % tuple(v) for v in flat]
    path.write_text("\n".join(lines) + "\n", encoding="ascii")


def convert(path: Path, out_dir: Path, name: str | None = None) -> tuple[Path, ChartReading]:
    reading = read_export(path)
    name = name or path.stem
    out = out_dir / f"{name}.cube"
    write_cube(out, resample(reading.lut), name)
    return out, reading
