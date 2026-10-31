"""Minimal DCP (DNG Camera Profile) reader."""
import struct
import numpy as np

TAGS = {
    50708: "UniqueCameraModel", 50721: "ColorMatrix1", 50722: "ColorMatrix2",
    50778: "CalibrationIlluminant1", 50779: "CalibrationIlluminant2",
    50936: "ProfileName", 50937: "HueSatMapDims", 50938: "HueSatMapData1",
    50939: "HueSatMapData2", 50940: "ProfileToneCurve", 50941: "ProfileEmbedPolicy",
    50942: "ProfileCopyright", 50964: "ForwardMatrix1", 50965: "ForwardMatrix2",
    50981: "LookTableDims", 50982: "LookTableData", 51107: "HueSatMapEncoding",
    51108: "LookTableEncoding", 51109: "BaselineExposureOffset", 51110: "DefaultBlackRender",
    52552: "ProfileGroupName",
}
SIZES = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8}


def read_dcp(path):
    b = open(path, "rb").read()
    assert b[:4] == b"IIRC", b[:4]
    off = struct.unpack_from("<I", b, 4)[0]
    n = struct.unpack_from("<H", b, off)[0]
    out = {}
    for i in range(n):
        tag, typ, cnt, val = struct.unpack_from("<HHII", b, off + 2 + 12 * i)
        size = SIZES[typ] * cnt
        p = off + 2 + 12 * i + 8 if size <= 4 else val
        if typ == 2:
            v = b[p:p + cnt].split(b"\0")[0].decode("latin1")
        elif typ == 3:
            v = np.frombuffer(b, "<u2", cnt, p)
        elif typ in (4, 9):
            v = np.frombuffer(b, "<u4" if typ == 4 else "<i4", cnt, p)
        elif typ in (5, 10):
            r = np.frombuffer(b, "<u4" if typ == 5 else "<i4", cnt * 2, p).astype(float)
            v = r[0::2] / r[1::2]
        elif typ == 11:
            v = np.frombuffer(b, "<f4", cnt, p).astype(float)
        elif typ == 12:
            v = np.frombuffer(b, "<f8", cnt, p)
        else:
            v = b[p:p + size]
        if not isinstance(v, str) and len(v) == 1:
            v = v[0]
        out[TAGS.get(tag, tag)] = v
    return out


def summarize(d):
    for k, v in d.items():
        if isinstance(v, str) or np.isscalar(v):
            print(f"  {k}: {v}")
        else:
            a = np.asarray(v)
            print(f"  {k}: len={a.size}" + (f" {np.round(a, 4).tolist()}" if a.size <= 12 else ""))
