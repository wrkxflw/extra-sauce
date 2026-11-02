"""Extra Sauce window: an HTML interface (app/web/index.html) in a native WebView2 window.

The page calls the Api methods below through pywebview's js_api; long jobs run on a
worker thread and report back by calling window.vtg.* in the page.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import traceback
from pathlib import Path

import webview
from webview.dom import DOMEventHandler

from . import accuracy, chart, converter
from .dcp import read_dcp
from .pipeline import Profile

APP_NAME = "Extra Sauce"
MAC = sys.platform == "darwin"
_SETTINGS_ROOT = Path.home() / "Library/Application Support" if MAC else Path(os.environ.get("APPDATA", Path.home()))
SETTINGS_PATH = _SETTINGS_ROOT / APP_NAME / "settings.json"
DEFAULT_COPY_DIR = Path.home() / "Desktop" / "Extra Sauce Profiles"
DEFAULT_LUT_DIR = Path.home() / "Desktop" / "Extra Sauce LUTs"
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp"}


def _web_dir() -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    return base / "app" / "web"


OLD_NAME = "One To Go"      # the app's earlier name: carry its settings over once


def _load_settings() -> dict:
    path = SETTINGS_PATH
    if not path.exists():
        path = SETTINGS_PATH.parent.parent / OLD_NAME / "settings.json"
    try:
        s = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    for key, new in (("copy_dir", DEFAULT_COPY_DIR), ("lut_dir", DEFAULT_LUT_DIR)):
        if s.get(key) and Path(s[key]).name.startswith(OLD_NAME):
            s[key] = str(new)
    return s


class Api:
    # Everything stored on the instance is underscore-prefixed: pywebview exposes public attributes
    # to the page and walks into them, and walking the Window object can hang startup.
    def __init__(self):
        self._window = None
        self._settings = _load_settings()
        self._cameras = converter.adobe_standard_cameras()
        self._looks: dict[str, dict] = {}       # id -> {id, name, versions: {camera: path}}
        self._next_id = 0
        self._exports: dict[str, Path] = {}     # id -> chart export path
        self._sources: dict[str, converter.LookSource] = {}   # "look id|camera" -> analysed version, for its map
        self._page = "cams"
        self._busy = False

    # ---- plumbing ---------------------------------------------------------------------
    def _js(self, fn: str, *args):
        if self._window:
            self._window.evaluate_js(f"window.vtg.{fn}({', '.join(json.dumps(a) for a in args)})")

    def _save(self):
        try:
            SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
            SETTINGS_PATH.write_text(json.dumps(self._settings, indent=2), encoding="utf-8")
        except OSError:
            pass

    def init(self):
        s = self._settings
        return {
            "cameras": sorted(self._cameras, key=str.lower),
            "standardDir": str(converter.ADOBE_STANDARD_DIR),
            "installDir": str(converter.INSTALL_DIR),
            "platform": sys.platform,
            "settings": {
                "install": s.get("install", True), "copy": s.get("copy", True),
                "copyDir": s.get("copy_dir", str(DEFAULT_COPY_DIR)), "skip": s.get("skip_existing", True),
                "targets": s.get("targets", []), "lutDir": s.get("lut_dir", str(DEFAULT_LUT_DIR)),
                "page": s.get("page", "cams"),
            },
        }

    def set_page(self, page):
        self._page = page
        self._settings["page"] = page
        self._save()

    def save_settings(self, values):
        mapping = {"install": "install", "copy": "copy", "copyDir": "copy_dir", "skip": "skip_existing",
                   "targets": "targets", "lutDir": "lut_dir"}
        for k, v in values.items():
            if k in mapping:
                self._settings[mapping[k]] = v
        self._save()

    def open_folder(self, path):
        p = Path(path)
        p.mkdir(parents=True, exist_ok=True)
        if MAC:
            subprocess.run(["open", str(p)], check=False)
        else:
            os.startfile(p)

    def browse_folder(self, start=""):
        r = self._window.create_file_dialog(webview.FileDialog.FOLDER, directory=start or str(Path.home()))
        return r[0] if r else None

    # ---- camera profiles --------------------------------------------------------------
    def pick_profiles(self):
        r = self._window.create_file_dialog(webview.FileDialog.OPEN, allow_multiple=True,
                                           directory=str(converter.USER_PROFILES_DIR),
                                           file_types=("Camera profiles (*.dcp)",))
        return self.add_profiles(list(r or []))

    def pick_profile_folder(self):
        r = self._window.create_file_dialog(webview.FileDialog.FOLDER, directory=str(converter.USER_PROFILES_DIR))
        return self.add_profiles(list(r or []))

    def add_profiles(self, raw):
        files = []
        for p in raw:
            path = Path(p)
            if path.is_dir():
                files += sorted(q for q in path.rglob("*") if q.suffix.lower() == ".dcp")
            elif path.suffix.lower() == ".dcp":
                files.append(path)
        # one entry per look; every camera's version of it is kept, since which version to convert
        # from matters (see converter.closest_source)
        by_name = {l["name"]: l for l in self._looks.values()}
        added, updated, versions, same, bad = [], {}, 0, 0, 0
        for f in files:
            try:
                info = read_dcp(f)
            except Exception:
                bad += 1
                continue
            name, cam = info.get("ProfileName"), info.get("UniqueCameraModel")
            if not name or not cam:
                bad += 1
                continue
            look = by_name.get(name)
            if look is None:
                look = {"id": f"l{self._next_id}", "name": name, "versions": {}}
                self._next_id += 1
                self._looks[look["id"]] = by_name[name] = look
                added.append(look)
            if cam in look["versions"]:
                same += 1
                continue
            look["versions"][cam] = str(f)
            versions += 1
            if look not in added:
                updated[look["id"]] = look
        return {"added": [self._public(l) for l in added], "updated": [self._public(l) for l in updated.values()],
                "versions": versions, "dupes": same, "bad": bad, "seen": len(files)}

    def _public(self, look):
        cams = sorted(look["versions"], key=str.lower)
        return {"id": look["id"], "name": look["name"], "cameras": cams,
                "ok": any(c in self._cameras for c in cams)}

    def _pick(self, look, target, source):
        """Which camera's version of the look to convert from for this target."""
        usable = [c for c in look["versions"] if c in self._cameras]
        return source if source in usable else converter.closest_source(target, usable)

    def remove_look(self, lid):
        self._looks.pop(lid, None)

    def accuracy_map(self, key):
        """Map data for a look version analysed in this session (see accuracy.py)."""
        src = self._sources.get(key)
        if src is None:
            return None
        look, std = Profile(src.path), Profile(self._cameras[src.camera])
        return {"name": src.name, "camera": src.camera,
                **accuracy.map_data(look, std, src.table, look.cx, look.cy)}

    def convert(self, ids, targets, install, copy_dir, skip, source=""):
        if self._busy:
            return False
        looks = [self._looks[i] for i in ids if i in self._looks and self._public(self._looks[i])["ok"]]
        self.save_settings({"install": install, "copy": bool(copy_dir), "skip": skip, "targets": targets,
                            **({"copyDir": copy_dir} if copy_dir else {})})
        self._busy = True
        threading.Thread(target=self._convert, args=(looks, targets, install, copy_dir, skip, source), daemon=True).start()
        return True

    def _convert(self, looks, targets, install, copy_dir, skip, source):
        made = skipped = failed = 0
        plans = [{t: self._pick(l, t, source) for t in targets if t not in l["versions"]} for l in looks]
        total = max(1, sum(len(set(p.values())) + len(targets) for p in plans))
        step = 0
        copy_path = Path(copy_dir) if copy_dir else None
        try:
            for look, plan in zip(looks, plans):
                srcs = {}
                for cam in sorted(set(plan.values()), key=str.lower):
                    self._js("log", "info", f"Analysing {look['name']}", f"the version made for {cam}")
                    try:
                        src = srcs[cam] = converter.prepare(Path(look["versions"][cam]), self._cameras)
                    except Exception as e:
                        self._js("log", "error", f"Couldn't analyse {look['name']} ({cam})", str(e))
                        failed += 1
                    else:
                        grade = "excellent" if src.accuracy_mean < 1.5 else "good" if src.accuracy_mean < 3 else "rough"
                        key = f"{look['id']}|{cam}"
                        self._sources[key] = src
                        self._js("log", "check", f"Accuracy check: {src.accuracy_mean:.1f} ΔE",
                                 f"95% of colours within {src.accuracy_p95:.1f} ΔE · {grade}", key)
                    step += 1
                    self._js("progress", step / total)
                for cam in targets:
                    fname = converter.safe_filename(f"{cam} {look['name']}.dcp")
                    src = srcs.get(plan.get(cam))
                    if cam in look["versions"]:
                        self._js("log", "skip", cam, "the pack already has a version for this camera")
                        skipped += 1
                    elif src is None:
                        failed += 1
                    elif skip and (existing := converter.existing_profile(cam, src.name)):
                        self._js("log", "skip", cam, f"already has it ({existing.parent.name})")
                        skipped += 1
                    else:
                        try:
                            if install:
                                converter.write_profile(src, self._cameras[cam], converter.INSTALL_DIR / fname)
                            if copy_path:
                                converter.write_profile(src, self._cameras[cam], copy_path / fname)
                            self._js("log", "ok", cam, f"{src.name} · from the {src.camera} version")
                            made += 1
                        except Exception as e:
                            self._js("log", "error", cam, str(e))
                            failed += 1
                    step += 1
                    self._js("progress", step / total)
            self._js("done", {"made": made, "skipped": skipped, "failed": failed, "install": install,
                              "installDir": str(converter.INSTALL_DIR),
            "platform": sys.platform, "copyDir": copy_dir or ""})
        except Exception:
            self._js("log", "error", "Unexpected error", traceback.format_exc())
            self._js("done", {"made": made, "skipped": skipped, "failed": failed + 1, "install": install,
                              "installDir": "", "copyDir": ""})
        finally:
            self._busy = False

    # ---- phone presets (LUTs) ---------------------------------------------------------
    def save_chart(self):
        d = self._window.create_file_dialog(webview.FileDialog.FOLDER, directory=str(Path.home() / "Desktop"))
        if not d:
            return None
        return [str(p) for p in chart.save_chart(Path(d[0]))]

    def pick_exports(self):
        r = self._window.create_file_dialog(webview.FileDialog.OPEN, allow_multiple=True,
                                           file_types=("Images (*.jpg;*.jpeg;*.png;*.tif;*.tiff;*.webp)",))
        return self.add_exports(list(r or []))

    def add_exports(self, raw):
        added = []
        for p in raw:
            path = Path(p)
            items = sorted(path.iterdir()) if path.is_dir() else [path]
            for f in items:
                if f.suffix.lower() in IMAGE_EXTS and f not in self._exports.values():
                    eid = f"e{len(self._exports)}_{abs(hash(str(f))) % 10**6}"
                    self._exports[eid] = f
                    added.append({"id": eid, "name": f.stem, "file": f.name})
        return added

    def remove_export(self, eid):
        self._exports.pop(eid, None)

    def make_luts(self, items, out_dir):
        """items: [{id, name}] -> converts each export, reporting via vtg.lutResult."""
        self.save_settings({"lutDir": out_dir})

        def work():
            for it in items:
                f = self._exports.get(it["id"])
                if not f:
                    continue
                try:
                    out, reading = chart.convert(f, Path(out_dir), it.get("name") or f.stem)
                    self._js("lutResult", it["id"], "warn" if reading.warnings else "ok", out.name,
                             reading.warnings + reading.notes)
                except chart.ChartError as e:
                    self._js("lutResult", it["id"], "error", "", [f"Couldn't read it: {e}"])
                except Exception as e:
                    self._js("lutResult", it["id"], "error", "", [str(e)])
            self._js("lutsDone")
        threading.Thread(target=work, daemon=True).start()
        return True


def _bind_drop(window, api: Api):
    def on_drop(e):
        files = (e.get("dataTransfer") or {}).get("files") or []
        paths = [f.get("pywebviewFullPath") for f in files if f.get("pywebviewFullPath")]
        if not paths:
            return
        if api._page == "luts":
            api._js("exportsAdded", api.add_exports(paths))
        else:
            api._js("looksAdded", api.add_profiles(paths))

    window.dom.document.events.dragover += DOMEventHandler(lambda e: None, True, True)
    window.dom.document.events.drop += DOMEventHandler(on_drop, True, True)


def main():
    api = Api()
    window = webview.create_window(
        APP_NAME, str(_web_dir() / "index.html"), js_api=api,
        width=1180, height=820, min_size=(960, 680), background_color="#FFFFFF")
    api._window = window
    # macOS takes the Dock icon from the app bundle; Windows needs it passed in
    icon = None if MAC else str(_web_dir().parent / "assets" / "icon.ico")
    webview.start(_bind_drop, (window, api), icon=icon)
