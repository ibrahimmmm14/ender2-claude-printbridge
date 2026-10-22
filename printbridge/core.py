"""
core.py - PrintBridge operations shared by the MCP server and the CLI.
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import Any, Dict, Optional

from .esp3d import ESP3D, ESP3DError
from .models import download_model as _download
from .slicer import Slicer, inspect_model as _inspect

HOME = os.path.join(os.path.expanduser("~"), "PrintBridge")
CONFIG_PATH = os.environ.get("PRINTBRIDGE_CONFIG", os.path.join(HOME, "config.json"))

DEFAULT_CONFIG = {
    "printer_url": "http://esp3d.local",
    "printer_ws_port": 8282,
    "esp3d_user": "admin",
    "esp3d_password": "",
    "cura_install": "",
    "cura_config": "",
    "cura_machine": "",
    "workdir": HOME,
}


def load_config() -> Dict[str, Any]:
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, encoding="utf-8-sig") as f:
            cfg.update(json.load(f))
    return cfg


def save_config(cfg: Dict[str, Any]):
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


def _dirs(cfg) -> Dict[str, str]:
    base = cfg.get("workdir") or HOME
    d = {"models": os.path.join(base, "models"), "gcode": os.path.join(base, "gcode")}
    for p in d.values():
        os.makedirs(p, exist_ok=True)
    return d


def printer(cfg=None) -> ESP3D:
    cfg = cfg or load_config()
    return ESP3D(cfg["printer_url"], int(cfg.get("printer_ws_port", 8282)), cfg.get("esp3d_user", ""),
                 cfg.get("esp3d_password", ""))


def slicer(cfg=None) -> Slicer:
    cfg = cfg or load_config()
    return Slicer(cfg.get("cura_install") or None, cfg.get("cura_config") or None, cfg.get("cura_machine") or None)


# ---------------------------------------------------------------- operations

def printer_status() -> Dict[str, Any]:
    p = printer()
    out: Dict[str, Any] = {"printer_url": p.url}
    try:
        info = p.info()
        keep = ("FW ver", "tailscale", "ip", "signal", "SSID", "targetfw", "serial", "free mem", "FS usage")
        out["esp3d"] = {k: v for k, v in info.items() if k in keep} if isinstance(info, dict) else info
    except Exception as e:
        return {"printer_url": p.url, "online": False, "error": str(e)}
    out["online"] = True
    try:
        out["job"] = p.stream_status()
    except Exception as e:
        out["job"] = {"error": str(e)}
    try:
        busy = str(out["job"].get("status", "")).lower() in ("processing", "pause")
        out["temperatures"] = p.temperatures(passive=busy)
    except Exception as e:
        out["temperatures"] = {"error": str(e)}
    return out


def cura_profile() -> Dict[str, Any]:
    return slicer().profile_summary()


def download_model(url: str) -> Dict[str, Any]:
    cfg = load_config()
    folder = os.path.join(_dirs(cfg)["models"], time.strftime("%Y%m%d-%H%M%S"))
    res = _download(url, folder)
    res["models"] = []
    for f in res["files"]:
        try:
            res["models"].append(_inspect(f))
        except Exception as e:
            res["models"].append({"file": f, "error": str(e)})
    return res


def inspect_model(path: str) -> Dict[str, Any]:
    try:
        s = slicer().machine().summary()
        bed = tuple(float(x) for x in s["build_volume"])
    except Exception:
        bed = (150.0, 150.0, 200.0)
    return _inspect(path, bed)


def slice_model(path: str, layer_height: Optional[float] = None, infill_percent: Optional[float] = None,
                supports: Optional[bool] = None, adhesion: Optional[str] = None, scale_percent: float = 100.0,
                rotate_x: float = 0, rotate_y: float = 0, rotate_z: float = 0, lay_flat: bool = False,
                extra_settings: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    cfg = load_config()
    opts: Dict[str, Any] = {}
    if layer_height:
        opts["layer_height"] = float(layer_height)
    if infill_percent is not None:
        opts["infill"] = float(infill_percent)
    if supports is not None:
        opts["supports"] = bool(supports)
    if adhesion:
        opts["adhesion"] = adhesion
    if extra_settings:
        opts.update(extra_settings)
    base = re.sub(r"[^A-Za-z0-9_-]", "_", os.path.splitext(os.path.basename(path))[0])[:22] or "model"
    out = os.path.join(_dirs(cfg)["gcode"], base + ".gcode")
    return slicer(cfg).slice(path, out, opts, scale_percent, (rotate_x, rotate_y, rotate_z), lay_flat)


def compact_gcode(src: str) -> str:
    """Copy of the G-code without comments / blank lines (smaller upload, same moves)."""
    dst = os.path.splitext(src)[0] + ".min.gcode"
    with open(src, "r", encoding="utf-8", errors="ignore") as fi, open(dst, "w", encoding="utf-8", newline="\n") as fo:
        for i, line in enumerate(fi):
            if i < 20 and line.startswith(";"):  # keep the small Cura header (time, filament, bounds)
                fo.write(line)
                continue
            code = line.split(";", 1)[0].rstrip()
            if code:
                fo.write(code + "\n")
    return dst


def upload_gcode(gcode_path: str, remote_name: Optional[str] = None, compact: bool = True) -> Dict[str, Any]:
    p = printer()
    original = os.path.getsize(gcode_path)
    if compact and not gcode_path.endswith(".min.gcode"):
        gcode_path_up = compact_gcode(gcode_path)
        remote_name = remote_name or os.path.basename(gcode_path)
        gcode_path = gcode_path_up
    size = os.path.getsize(gcode_path)
    try:
        listing = p.list_files("/")
        total, used = _bytes(listing.get("total")), _bytes(listing.get("used"))
        if total and used is not None and size > (total - used) - 16384:
            return {"ok": False, "error": "Not enough space on the ESP32 (%d KB free, file %d KB). "
                                          "Delete old files with delete_printer_file." % ((total - used) // 1024, size // 1024)}
    except ESP3DError:
        raise
    except Exception:
        pass
    res = p.upload(gcode_path, remote_name)
    return {"ok": True, "remote_file": res.get("uploaded_as"), "size_kb": size // 1024,
            "original_size_kb": original // 1024}


def _bytes(txt) -> Optional[int]:
    if txt is None:
        return None
    m = re.match(r"\s*([\d.]+)\s*([KMG]?B)?", str(txt), re.I)
    if not m:
        return None
    mult = {"B": 1, "KB": 1024, "MB": 1024 ** 2, "GB": 1024 ** 3}.get((m.group(2) or "B").upper(), 1)
    return int(float(m.group(1)) * mult)


def start_print(remote_file: str) -> Dict[str, Any]:
    p = printer()
    st = p.stream_status()
    if str(st.get("status", "")).lower().startswith("processing"):
        return {"ok": False, "error": "A print is already running", "job": st}
    ans = p.start_print(remote_file)
    time.sleep(1.5)
    return {"ok": True, "answer": ans, "job": p.stream_status()}


def print_progress() -> Dict[str, Any]:
    p = printer()
    out = {"job": p.stream_status()}
    try:
        busy = str(out["job"].get("status", "")).lower() in ("processing", "pause")
        out["temperatures"] = p.temperatures(passive=busy)
    except Exception as e:
        out["temperatures"] = {"error": str(e)}
    return out


def control_print(action: str) -> Dict[str, Any]:
    action = action.lower()
    p = printer()
    if action not in ("pause", "resume", "cancel"):
        return {"ok": False, "error": "action must be pause, resume or cancel"}
    ans = p.stream_action("ABORT" if action == "cancel" else action)
    if action == "cancel":
        # stop heaters / fan, lift and park like Cura's end g-code would
        for g in ("M104 S0", "M140 S0", "M107", "G91", "G1 Z10 F600", "G90", "M84"):
            try:
                p.send_gcode(g)
            except Exception:
                pass
    return {"ok": True, "answer": ans}


def send_gcode(gcode: str, force: bool = False) -> Dict[str, Any]:
    p = printer()
    if p.is_printing() and not force:
        return {"ok": False, "error": "A print is streaming: sending G-code now can disturb it. Use pause/cancel, "
                                      "or force=True for an emergency command (e.g. M112)."}
    lines = p.query(gcode, wait_s=5.0)
    return {"sent": gcode, "printer_answer": lines[-20:]}


def list_printer_files() -> Dict[str, Any]:
    return printer().list_files("/")


def delete_printer_file(name: str) -> Dict[str, Any]:
    return printer().delete_file(name.lstrip("/"), "/")
