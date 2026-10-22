"""
slicer.py - Prepare a model and slice it with the CuraEngine bundled with Cura,
using the user's resolved Cura profile.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
from typing import Any, Dict, List, Optional

import numpy as np
import trimesh

from .cura_profile import CuraMachine, find_cura_config_dir, find_cura_engine, find_cura_install, find_resources_dir

# --------------------------------------------------------------------------
# Mesh preparation
# --------------------------------------------------------------------------


def load_mesh(path: str) -> trimesh.Trimesh:
    mesh = trimesh.load(path, force="mesh")
    if isinstance(mesh, trimesh.Scene):
        mesh = mesh.dump(concatenate=True)
    if mesh.is_empty:
        raise ValueError("The model file contains no geometry: %s" % path)
    return mesh


def inspect_model(path: str, bed=(150.0, 150.0, 200.0)) -> Dict[str, Any]:
    """Size, volume and whether the model fits the bed (as-is and after lay-flat)."""
    mesh = load_mesh(path)
    ext = mesh.bounding_box.extents
    info = {
        "file": path,
        "size_mm": [round(float(x), 2) for x in ext],
        "volume_cm3": round(float(abs(mesh.volume)) / 1000.0, 2) if mesh.is_volume else None,
        "watertight": bool(mesh.is_watertight),
        "triangles": int(len(mesh.faces)),
        "fits_bed": bool(ext[0] <= bed[0] - 4 and ext[1] <= bed[1] - 4 and ext[2] <= bed[2]),
        "bed_mm": list(bed),
    }
    if not info["fits_bed"]:
        s = min((bed[0] - 4) / ext[0], (bed[1] - 4) / ext[1], bed[2] / ext[2])
        info["max_scale_percent_to_fit"] = round(s * 100.0, 1)
    return info


def prepare_mesh(path: str, out_stl: str, bed=(150.0, 150.0, 200.0), scale_percent: float = 100.0,
                 rotate_deg=(0.0, 0.0, 0.0), lay_flat: bool = False, center_is_zero: bool = False) -> Dict[str, Any]:
    """Scale / rotate / lay flat, put on the bed (z=0) and center it, write a binary STL."""
    mesh = load_mesh(path)
    if scale_percent and abs(scale_percent - 100.0) > 1e-6:
        mesh.apply_scale(scale_percent / 100.0)
    rx, ry, rz = [float(a) for a in rotate_deg]
    for angle, axis in ((rx, [1, 0, 0]), (ry, [0, 1, 0]), (rz, [0, 0, 1])):
        if angle:
            mesh.apply_transform(trimesh.transformations.rotation_matrix(np.radians(angle), axis))
    if lay_flat:
        # pick the stable pose with the highest probability (largest flat face on the bed)
        try:
            transforms, probs = trimesh.poses.compute_stable_poses(mesh, n_samples=1)
            if len(transforms):
                mesh.apply_transform(transforms[int(np.argmax(probs))])
        except Exception:
            pass
    bmin, bmax = mesh.bounds
    center = (bmin + bmax) / 2.0
    # CuraEngine expects mesh coordinates relative to the bed center (like the Cura
    # front-end sends them); it adds machine_width/2, machine_depth/2 itself when
    # machine_center_is_zero is false.
    mesh.apply_translation([-center[0], -center[1], -bmin[2]])
    mesh.export(out_stl)
    ext = mesh.bounding_box.extents
    return {"size_mm": [round(float(x), 2) for x in ext],
            "fits_bed": bool(ext[0] <= bed[0] and ext[1] <= bed[1] and ext[2] <= bed[2])}


# --------------------------------------------------------------------------
# Slicing
# --------------------------------------------------------------------------

# Friendly option names -> Cura setting keys
OPTION_MAP = {
    "layer_height": "layer_height",
    "infill": "infill_sparse_density",
    "infill_pattern": "infill_pattern",
    "walls": "wall_line_count",
    "supports": "support_enable",
    "support_type": "support_type",  # buildplate / everywhere
    "adhesion": "adhesion_type",  # skirt / brim / raft / none
    "print_temperature": "material_print_temperature",
    "bed_temperature": "material_bed_temperature",
    "print_speed": "speed_print",
    "fan": "cool_fan_speed",
}


def _write_flat_definition(path: str, settings: Dict[str, str], name: str):
    data = {
        "version": 2,
        "name": name,
        "metadata": {},
        "settings": {
            "resolved": {
                "label": "resolved",
                "type": "category",
                "children": {k: {"label": k, "type": "str", "default_value": v} for k, v in settings.items()},
            }
        },
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)


def _engine_cmd(engine: str) -> List[str]:
    wrapper = os.environ.get("CURA_ENGINE_WRAPPER")
    return [wrapper] if wrapper else [engine]


class Slicer:
    def __init__(self, cura_install: Optional[str] = None, cura_config: Optional[str] = None,
                 machine: Optional[str] = None):
        self.install = cura_install or find_cura_install()
        if not self.install:
            raise FileNotFoundError("Cura installation not found. Set cura_install in config.json")
        self.resources = find_resources_dir(self.install)
        self.engine = find_cura_engine(self.install)
        self.config_dir = cura_config if cura_config is not None else find_cura_config_dir()
        self.machine_name = machine

    def machine(self) -> CuraMachine:
        return CuraMachine(self.resources, self.config_dir, self.machine_name)

    def profile_summary(self) -> Dict[str, Any]:
        m = self.machine()
        s = m.summary()
        s["cura_install"] = self.install
        s["cura_config"] = self.config_dir or "(not found - using Cura defaults for the printer)"
        return s

    def slice(self, model_path: str, out_gcode: str, options: Optional[Dict[str, Any]] = None,
              scale_percent: float = 100.0, rotate_deg=(0, 0, 0), lay_flat: bool = False,
              timeout_s: int = 900) -> Dict[str, Any]:
        options = dict(options or {})
        overrides: Dict[str, Any] = {}
        for k, v in options.items():
            key = OPTION_MAP.get(k, k)
            if key == "support_enable" and isinstance(v, str):
                v = v.lower() in ("1", "true", "yes", "on")
            overrides[key] = v
        machine = self.machine()
        global_s, extruders = machine.resolve(overrides)
        bed = (float(global_s["machine_width"]), float(global_s["machine_depth"]), float(global_s["machine_height"]))
        center_zero = global_s.get("machine_center_is_zero", "False") == "True"

        work = tempfile.mkdtemp(prefix="printbridge_")
        stl = os.path.join(work, "model.stl")
        placed = prepare_mesh(model_path, stl, bed, scale_percent, rotate_deg, lay_flat, center_zero)
        if not placed["fits_bed"]:
            raise ValueError("Model %s mm does not fit the %s mm build volume. Scale it down."
                             % (placed["size_mm"], list(bed)))
        gdef = os.path.join(work, "global.def.json")
        _write_flat_definition(gdef, global_s, "global")
        cmd = _engine_cmd(self.engine) + ["slice", "-p", "-j", gdef]
        for nr, es in sorted(extruders.items()):
            edef = os.path.join(work, "extruder%s.def.json" % nr)
            _write_flat_definition(edef, es, "extruder" + nr)
            cmd += ["-e" + nr, "-j", edef]
        cmd += ["-l", stl, "-o", out_gcode]
        t0 = time.time()
        creation = 0x08000000 if sys.platform.startswith("win") else 0  # CREATE_NO_WINDOW
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s, creationflags=creation)
        if proc.returncode != 0 or not os.path.exists(out_gcode) or os.path.getsize(out_gcode) < 100:
            tail = (proc.stderr or proc.stdout or "")[-2000:]
            raise RuntimeError("CuraEngine failed (code %s): %s" % (proc.returncode, tail))
        info = parse_gcode_info(out_gcode, global_s, extruders.get("0", {}))
        b = info.get("bounds")
        if b and (b[0] < -1 or b[1] < -1 or b[3] > bed[0] + 1 or b[4] > bed[1] + 1 or b[5] > bed[2] + 0.5):
            os.remove(out_gcode)
            raise RuntimeError("Safety check failed: toolpath %s is outside the build volume %s" % (b, list(bed)))
        fix_header(out_gcode, info)
        info.update({
            "gcode": out_gcode,
            "size_bytes": os.path.getsize(out_gcode),
            "slice_seconds": round(time.time() - t0, 1),
            "model_size_mm": placed["size_mm"],
            "settings": {
                "layer_height": global_s.get("layer_height"),
                "infill_percent": extruders.get("0", {}).get("infill_sparse_density"),
                "supports": global_s.get("support_enable"),
                "adhesion": global_s.get("adhesion_type"),
                "print_temperature": extruders.get("0", {}).get("material_print_temperature"),
                "bed_temperature": extruders.get("0", {}).get("material_bed_temperature") or global_s.get("material_bed_temperature"),
                "material": extruders.get("0", {}).get("material_type", ""),
                "profile": machine.summary().get("custom_profile") or machine.summary().get("quality"),
            },
        })
        return info


def parse_gcode_info(path: str, global_s: Dict[str, str], ext_s: Dict[str, str]) -> Dict[str, Any]:
    """Read print time / filament from the g-code (CuraEngine header or ;TIME_ELAPSED)."""
    time_s = None
    filament_m = None
    layers = None
    last_elapsed = None
    max_e = 0.0
    lo = [1e9, 1e9, 1e9]
    hi = [-1e9, -1e9, -1e9]
    cur = [0.0, 0.0, 0.0]
    started = False
    coord = re.compile(r"\b([XYZ])(-?[\d.]+)")
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        for i, line in enumerate(f):
            if line.startswith(";LAYER:0"):
                started = True
            if started and (line.startswith("G1 ") or line.startswith("G0 ")):
                for ax, val in coord.findall(line.split(";")[0]):
                    try:
                        cur["XYZ".index(ax)] = float(val)
                    except ValueError:
                        pass
                if line.startswith("G1 ") and " E" in line:
                    for k in range(3):
                        lo[k] = min(lo[k], cur[k])
                        hi[k] = max(hi[k], cur[k])
            if line.startswith(";TIME:"):
                try:
                    time_s = int(float(line[6:].strip()))
                except ValueError:
                    pass
            elif line.startswith(";Filament used:"):
                m = re.findall(r"[\d.]+", line)
                if m:
                    filament_m = float(m[0])
            elif line.startswith(";LAYER_COUNT:"):
                layers = int(line.split(":")[1])
            elif line.startswith(";TIME_ELAPSED:"):
                last_elapsed = float(line.split(":")[1])
            elif line.startswith("G1") or line.startswith("G0"):
                m = re.search(r"\bE(-?[\d.]+)", line)
                if m:
                    try:
                        max_e = max(max_e, float(m.group(1)))
                    except ValueError:
                        pass
    if (time_s is None or time_s <= 0 or time_s == 6666) and last_elapsed:
        time_s = int(last_elapsed)
    if not filament_m and max_e:
        filament_m = max_e / 1000.0  # absolute E with resets is approximate
    diameter = float(ext_s.get("material_diameter", global_s.get("material_diameter", 1.75)) or 1.75)
    grams = None
    if filament_m:
        area = 3.14159265 * (diameter / 2.0) ** 2  # mm2
        grams = round(filament_m * 1000.0 * area / 1000.0 * 1.24, 1)  # PLA density 1.24 g/cm3
    return {
        "print_time_s": time_s,
        "print_time": time.strftime("%H:%M", time.gmtime(time_s)) if time_s else None,
        "filament_m": round(filament_m, 2) if filament_m else None,
        "filament_g_pla": grams,
        "layers": layers,
        "bounds": [round(lo[0], 2), round(lo[1], 2), round(lo[2], 2), round(hi[0], 2), round(hi[1], 2), round(hi[2], 2)]
        if hi[0] > -1e9 else None,
    }


def fix_header(path: str, info: Dict[str, Any]):
    """Replace the CuraEngine placeholder header values (;TIME:6666 etc.) like Cura does."""
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        head = f.read(2048)
        rest = f.read()
    b = info.get("bounds") or [0, 0, 0, 0, 0, 0]
    rep = {
        r";TIME:6666": ";TIME:%d" % (info.get("print_time_s") or 0),
        r";Filament used: 0m": ";Filament used: %.5fm" % (info.get("filament_m") or 0),
        r";MINX:[^\n]*": ";MINX:%.3f" % b[0], r";MINY:[^\n]*": ";MINY:%.3f" % b[1], r";MINZ:[^\n]*": ";MINZ:%.3f" % b[2],
        r";MAXX:[^\n]*": ";MAXX:%.3f" % b[3], r";MAXY:[^\n]*": ";MAXY:%.3f" % b[4], r";MAXZ:[^\n]*": ";MAXZ:%.3f" % b[5],
    }
    for pat, val in rep.items():
        head = re.sub(pat, val, head, count=1)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(head)
        f.write(rest)
