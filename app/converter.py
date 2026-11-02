"""Convert a camera-specific look profile (.dcp) so it works on other cameras.

The look's own colour tables and tone curve are re-expressed relative to Adobe
Standard, then laid over each target camera's Adobe Standard calibration.
"""
from __future__ import annotations

import difflib
import os
import sys
import re
import struct
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .dcp import read_dcp
from .pipeline import ACR3, XYZ_TO_PP, Profile, lab
from .transfer import LOOK_DIMS, Transferred, build_look, fit_look

if sys.platform == "darwin":
    ADOBE_STANDARD_DIR = Path("/Library/Application Support/Adobe/CameraRaw/CameraProfiles/Adobe Standard")
    USER_PROFILES_DIR = Path.home() / "Library/Application Support/Adobe/CameraRaw/CameraProfiles"
else:
    ADOBE_STANDARD_DIR = Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "Adobe/CameraRaw/CameraProfiles/Adobe Standard"
    USER_PROFILES_DIR = Path(os.environ.get("APPDATA", "")) / "Adobe/CameraRaw/CameraProfiles"
INSTALL_DIR = USER_PROFILES_DIR / "Generated Profiles"

BYTE, ASCII, LONG, FLOAT = 1, 2, 4, 11
_STD_RE = re.compile(r"^(?P<model>.+?) Adobe Standard(?P<v2> v2)?\.dcp$", re.I)


# ---- finding profiles ----------------------------------------------------------

def adobe_standard_cameras() -> dict[str, Path]:
    """Camera model -> its Adobe Standard profile (the original version where both exist)."""
    found: dict[str, Path] = {}
    if not ADOBE_STANDARD_DIR.is_dir():
        return found
    for p in sorted(ADOBE_STANDARD_DIR.glob("*.dcp")):
        m = _STD_RE.match(p.name)
        if not m:
            continue
        model = m.group("model")
        if model not in found or not m.group("v2"):
            found[model] = p
    return found


def installed_profiles() -> dict[tuple[str, str], Path]:
    """(camera, profile name) -> file, for profiles installed outside our own folder (e.g. the original
    maker's). Read from inside each file: names like "400H⁺¹" don't match their file names."""
    found: dict[tuple[str, str], Path] = {}
    if not USER_PROFILES_DIR.is_dir():
        return found
    for root, _dirs, files in os.walk(USER_PROFILES_DIR):
        if Path(root) == INSTALL_DIR:
            continue
        for f in files:
            if f.lower().endswith(".dcp"):
                try:
                    info = read_dcp(Path(root) / f)
                except Exception:
                    continue
                found.setdefault((info.get("UniqueCameraModel"), info.get("ProfileName")), Path(root) / f)
    return found


# VSCO tuned its per-camera versions in batches. Within a brand's latest batch (2014-18) the versions
# agree within about 1 dE, and rebuilt from any of them a newer body lands within about 1 dE too; an
# older batch lands 3-5 dE away. Measured against VSCO's own versions, Film 03-06 colour looks.
LATEST_BATCH = {
    "canon": {"Canon EOS R", "Canon EOS 5D Mark IV", "Canon EOS 6D Mark II", "Canon EOS 80D", "Canon EOS 77D",
              "Canon EOS 800D", "Canon EOS 200D", "Canon EOS M5", "Canon EOS M6", "Canon EOS M100",
              "Canon EOS-1D X Mark II", "Canon EOS 7D Mark II", "Canon EOS 750D", "Canon EOS 760D", "Canon EOS 1300D",
              "Canon EOS 1500D", "Canon PowerShot G3 X", "Canon PowerShot G7 X", "Canon PowerShot G7 X Mark II",
              "Canon PowerShot G9 X"},
    "nikon": {"Nikon Z 6", "Nikon D850", "Nikon D750", "Nikon D5", "Nikon D500", "Nikon D7500", "Nikon D5600",
              "Nikon D5500", "Nikon D3500"},
    "sony": {"Sony ILCE-7M3", "Sony ILCE-7RM3", "Sony ILCE-9", "Sony ILCE-7RM2", "Sony ILCE-6300", "Sony ILCE-6500",
             "Sony DSC-RX100M6", "Sony DSC-RX1RM2", "Sony ILCA-99M2", "Sony ILCA-68"},
    "fujifilm": {"Fujifilm X-T3", "Fujifilm X-H1", "Fujifilm X-T2", "Fujifilm X-T20", "Fujifilm X-Pro2", "Fujifilm X-E3",
                 "Fujifilm X100F", "Fujifilm GFX 50S", "Fujifilm X-E2S", "Fujifilm X70", "Fujifilm X30",
                 "Fujifilm X-A2", "Fujifilm X-A3", "Fujifilm X-A10"},
}


def closest_source(target: str, cameras: list[str]) -> str:
    """Default pick of which version of a look to convert from: same brand, the brand's latest batch
    where the look has versions in it, then the nearest model name. The user can choose instead."""
    brand = target.split()[0].lower()
    same = [c for c in cameras if c.split()[0].lower() == brand] or cameras
    same = [c for c in same if c in LATEST_BATCH.get(brand, ())] or same
    t = target.lower()
    num = lambda s: int((re.findall(r"\d+", s.split(maxsplit=1)[-1]) or [0])[0])
    return max(same, key=lambda c: (len(os.path.commonprefix([c.lower(), t])),
                                    round(difflib.SequenceMatcher(None, c.lower(), t).ratio(), 3),
                                    -abs(num(c) - num(target)), c))


def safe_filename(s: str) -> str:
    return re.sub(r'[<>:"/\\|?*]', "_", s)


# ---- the conversion ---------------------------------------------------------------

@dataclass
class LookSource:
    path: Path
    name: str
    camera: str
    table: np.ndarray
    tone: np.ndarray            # flat x,y pairs
    accuracy_mean: float
    accuracy_p95: float
    filled_pct: float

    @property
    def summary(self) -> str:
        return f"{self.name}  (made for {self.camera})"


class ConversionError(Exception):
    pass


def prepare(look_path: Path, cameras: dict[str, Path] | None = None) -> LookSource:
    """Analyse one look profile against Adobe Standard for the camera it was made for."""
    cameras = cameras if cameras is not None else adobe_standard_cameras()
    info = read_dcp(look_path)
    name, camera = info.get("ProfileName"), info.get("UniqueCameraModel")
    if not name or not camera:
        raise ConversionError(f"{look_path.name}: not a camera profile (no name or camera model inside).")
    if camera not in cameras:
        raise ConversionError(f"{look_path.name}: no Adobe Standard profile for {camera} on this computer, "
                              "so there's nothing to measure the look against.")
    look, std = Profile(look_path), Profile(cameras[camera])
    table, resid = build_look(look, std)
    table = fit_look(look, std, table)
    tone = info.get("ProfileToneCurve")
    if tone is None:   # profile relies on Adobe's default curve: write it out explicitly
        tone = np.stack([np.linspace(0, 1, len(ACR3)), ACR3], -1).ravel()
    mean, p95 = _self_check(look, std, table, look.cx, look.cy)
    return LookSource(look_path, name, camera, table, np.asarray(tone, dtype=float), mean, p95,
                      100 * float((resid >= 1e-3).mean()))


def _self_check(look, std, table, cx, cy, n=40000):
    """Rebuild the look for its own camera and measure how far it lands from the original (dE76)."""
    rng = np.random.default_rng(7)
    cam = rng.random((n, 3)) ** 2.2
    pp_look, pp_std = cam @ (XYZ_TO_PP @ look.fm).T, cam @ (XYZ_TO_PP @ std.fm).T
    s = max(pp_look.max(), pp_std.max(), 1.0)
    want = lab(look.render(pp_look / s))
    got = lab(Transferred(std, table, cx, cy).render(pp_std / s))
    de = np.linalg.norm(got - want, axis=-1)
    return float(de.mean()), float(np.percentile(de, 95))


def write_profile(src: LookSource, target_std: Path, out_path: Path, profile_name: str | None = None) -> None:
    entries = _raw_entries(target_std)
    nh, ns, nv = LOOK_DIMS
    entries[50936] = _ascii(profile_name or src.name)
    entries[50942] = _ascii(f"Converted from {src.name} ({src.camera}) over Adobe Standard")
    entries[50941] = (LONG, 1, struct.pack("<I", 0))                     # embed policy: allow copying
    entries[50981] = (LONG, 3, struct.pack("<3I", nh, ns, nv))
    entries[50982] = (FLOAT, src.table.size, src.table.astype("<f4").tobytes())
    entries[51108] = (LONG, 1, struct.pack("<I", 1))                     # look table uses sRGB value encoding
    tc = src.tone.astype("<f4")
    entries[50940] = (FLOAT, tc.size, tc.tobytes())
    # exposure offset: some packs set one per camera alongside the tone curve (e.g. -0.25 EV); it sits
    # outside the colour tables, so carry it over with the curve
    beo = _raw_entries(src.path).get(51109)
    if beo:
        entries[51109] = beo
    else:
        entries.pop(51109, None)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    _write_dcp(out_path, entries)


# ---- DCP file writing -------------------------------------------------------------

_SIZES = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8}


def _raw_entries(path: Path) -> dict:
    b = path.read_bytes()
    if b[:4] != b"IIRC":
        raise ConversionError(f"{path.name} is not a DCP file.")
    off = struct.unpack_from("<I", b, 4)[0]
    out = {}
    for i in range(struct.unpack_from("<H", b, off)[0]):
        tag, typ, cnt, val = struct.unpack_from("<HHII", b, off + 2 + 12 * i)
        size = _SIZES[typ] * cnt
        p = off + 2 + 12 * i + 8 if size <= 4 else val
        out[tag] = (typ, cnt, b[p:p + size])
    return out


def _ascii(s: str):
    """A text tag. Non-ASCII names go out as UTF-8 bytes, the way the originals store them, so presets
    asking for the exact name still find the converted profile."""
    if s.isascii():
        data = s.encode("ascii") + b"\0"
        return (ASCII, len(data), data)
    data = s.encode("utf-8") + b"\0"
    return (BYTE, len(data), data)


def _write_dcp(path: Path, entries: dict) -> None:
    tags = sorted(entries)
    data_off = 8 + 2 + 12 * len(tags) + 4
    ifd, blob = bytearray(struct.pack("<H", len(tags))), bytearray()
    for tag in tags:
        typ, cnt, data = entries[tag]
        if len(data) <= 4:
            ifd += struct.pack("<HHI", tag, typ, cnt) + data.ljust(4, b"\0")
        else:
            if (data_off + len(blob)) % 2:
                blob += b"\0"
            ifd += struct.pack("<HHII", tag, typ, cnt, data_off + len(blob))
            blob += data
    ifd += struct.pack("<I", 0)
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(b"IIRC" + struct.pack("<I", 8) + ifd + blob)
    os.replace(tmp, path)
