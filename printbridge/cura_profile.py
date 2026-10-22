"""
cura_profile.py - Resolve a Cura machine profile exactly like the Cura GUI does.

Reads the user's Cura configuration (machine instance, extruder stack,
user / quality_changes / intent / quality / material / variant /
definition_changes / definition containers), evaluates every setting formula
with Cura's own rules (GlobalStack / ExtruderStack / SettingFunction
semantics), expands start/end g-code tokens like StartSliceJob, and returns
the flat global + extruder setting dicts that Cura would send to CuraEngine.
"""
from __future__ import annotations

import ast
import base64  # noqa: F401  (available to formulas, like in Uranium)
import builtins
import configparser
import glob
import hashlib  # noqa: F401
import json
import math  # noqa: F401
import os
import re
import sys
import time
import urllib.parse
import uuid  # noqa: F401
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional, Tuple

# --------------------------------------------------------------------------
# Locating Cura
# --------------------------------------------------------------------------


def _version_key(path: str) -> Tuple:
    m = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", os.path.basename(path.rstrip("/\\")))
    if not m:
        return (0, 0, 0)
    return tuple(int(x or 0) for x in m.groups())


def find_cura_install() -> Optional[str]:
    """Return the Cura install dir (contains CuraEngine and share/cura/resources)."""
    env = os.environ.get("CURA_INSTALL_DIR")
    if env and os.path.isdir(env):
        return env
    candidates: List[str] = []
    if sys.platform.startswith("win"):
        for base in filter(None, [os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"),
                                  os.environ.get("LOCALAPPDATA") and os.path.join(os.environ["LOCALAPPDATA"], "Programs")]):
            candidates += glob.glob(os.path.join(base, "UltiMaker Cura*"))
            candidates += glob.glob(os.path.join(base, "Ultimaker Cura*"))
    elif sys.platform == "darwin":
        candidates += glob.glob("/Applications/UltiMaker Cura*.app/Contents/MacOS")
        candidates += glob.glob("/Applications/Ultimaker Cura*.app/Contents/MacOS")
    else:
        candidates += glob.glob(os.path.expanduser("~/squashfs-root"))
    candidates = [c for c in candidates if os.path.isdir(c)]
    if not candidates:
        return None
    return sorted(candidates, key=_version_key)[-1]


def find_resources_dir(install_dir: str) -> str:
    for rel in ["share/cura/resources", "resources", "../Resources/share/cura/resources", "../Resources/resources"]:
        p = os.path.normpath(os.path.join(install_dir, rel))
        if os.path.isdir(os.path.join(p, "definitions")):
            return p
    raise FileNotFoundError("Cura resources folder not found under %s" % install_dir)


def find_cura_engine(install_dir: str) -> str:
    env = os.environ.get("CURA_ENGINE")
    if env and os.path.exists(env):
        return env
    for name in ["CuraEngine.exe", "CuraEngine"]:
        for rel in ["", "bin", "../Resources", "../Resources/bin"]:
            p = os.path.normpath(os.path.join(install_dir, rel, name))
            if os.path.isfile(p):
                return p
    raise FileNotFoundError("CuraEngine not found under %s" % install_dir)


def find_cura_config_dir() -> Optional[str]:
    """Latest Cura user configuration directory (machine_instances etc.)."""
    env = os.environ.get("CURA_CONFIG_DIR")
    if env and os.path.isdir(env):
        return env
    roots = []
    if sys.platform.startswith("win"):
        roots.append(os.path.join(os.environ.get("APPDATA", ""), "cura"))
    elif sys.platform == "darwin":
        roots.append(os.path.expanduser("~/Library/Application Support/cura"))
    else:
        roots.append(os.path.expanduser("~/.local/share/cura"))
        roots.append(os.path.expanduser("~/.config/cura"))
    found = []
    for r in roots:
        for d in glob.glob(os.path.join(r, "*")):
            if os.path.isdir(os.path.join(d, "machine_instances")):
                found.append(d)
    if not found:
        return None
    return sorted(found, key=_version_key)[-1]


# --------------------------------------------------------------------------
# Containers
# --------------------------------------------------------------------------

_SAFE_BUILTINS = {
    "abs", "all", "any", "bool", "dict", "enumerate", "filter", "float", "int", "len", "list", "map",
    "max", "min", "range", "round", "set", "sorted", "str", "sum", "tuple", "zip", "isinstance",
    "True", "False", "None", "next", "reversed", "divmod", "pow", "ValueError", "TypeError",
}


class Formula:
    """A setting formula, equivalent to UM.Settings.SettingFunction."""

    _cache: Dict[str, Any] = {}

    def __init__(self, code: str):
        self.code = code.strip()
        try:
            tree = ast.parse(self.code, "<setting>", "eval")
            self.compiled = compile(tree, "<setting>", "eval")
            names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
            # function arguments that are string literals are setting keys too,
            # but those are fetched by the operator functions themselves.
            self.names = {n for n in names if n not in OPERATORS and n not in _FORMULA_GLOBALS}
        except SyntaxError:
            self.compiled = None
            self.names = set()

    def __repr__(self):
        return "=" + self.code


class Container:
    def __init__(self, cid: str, kind: str):
        self.id = cid
        self.kind = kind  # user, quality_changes, intent, quality, material, variant, definition_changes, definition
        self.values: Dict[str, Any] = {}
        self.metadata: Dict[str, Any] = {}
        self.name = cid

    def get(self, key: str, prop: str):
        if prop == "value":
            return self.values.get(key)
        return None

    def has_value(self, key):
        return key in self.values


class Definition(Container):
    """Merged definition chain (e.g. creality_ender2 -> creality_base -> fdmprinter)."""

    def __init__(self, cid: str):
        super().__init__(cid, "definition")
        self.props: Dict[str, Dict[str, Any]] = {}
        self.order: List[str] = []

    def get(self, key: str, prop: str):
        p = self.props.get(key)
        if p is None:
            return None
        if prop == "value":
            v = p.get("value")
            return v if v is not None else p.get("default_value", 0)
        if prop in p:
            return p[prop]
        return _PROP_DEFAULTS.get(prop)


# Uranium / Cura defaults for setting properties that a definition does not set
_PROP_DEFAULTS = {
    "settable_per_mesh": True,
    "settable_per_extruder": True,
    "settable_per_meshgroup": True,
    "settable_globally": True,
    "limit_to_extruder": "-1",
    "resolve": None,
    "enabled": True,
    "default_value": 0,
}


def _convert_typed(raw: str, stype: Optional[str]):
    """Convert a string from an .inst.cfg into a typed value (Uranium semantics)."""
    if isinstance(raw, str) and raw.startswith("="):
        return Formula(raw[1:])
    if stype in ("float",):
        try:
            return float(raw)
        except ValueError:
            return Formula(raw)
    if stype in ("int",):
        try:
            return int(raw)
        except ValueError:
            try:
                return int(float(raw))
            except ValueError:
                return Formula(raw)
    if stype == "bool":
        return str(raw).strip().lower() in ("true", "yes", "1", "on")
    if stype in ("polygon", "polygons", "[int]", "vec3"):
        try:
            return ast.literal_eval(raw)
        except Exception:
            return raw
    return raw


class Resources:
    def __init__(self, resources_dir: str, config_dir: Optional[str]):
        self.resources_dir = resources_dir
        self.config_dir = config_dir
        self._def_cache: Dict[str, Dict] = {}
        self._inst_index: Dict[str, str] = {}
        self._material_index: Dict[str, str] = {}
        search = [resources_dir] + ([config_dir] if config_dir else [])
        for base in search:
            for path in glob.glob(os.path.join(base, "**", "*.inst.cfg"), recursive=True):
                cid = urllib.parse.unquote_plus(os.path.basename(path)[: -len(".inst.cfg")])
                self._inst_index[cid] = path
            for path in glob.glob(os.path.join(base, "**", "*.xml.fdm_material"), recursive=True):
                cid = urllib.parse.unquote_plus(os.path.basename(path)[: -len(".xml.fdm_material")])
                self._material_index[cid] = path
        # user-defined definitions (custom printers) live in config_dir/definitions
        self._def_dirs = [os.path.join(resources_dir, "definitions"), os.path.join(resources_dir, "extruders")]
        if config_dir:
            self._def_dirs += [os.path.join(config_dir, "definitions")]
        pmap = os.path.join(resources_dir, "texts", "product_to_id.json")
        self.product_to_id = {}
        if os.path.exists(pmap):
            contents = ""
            with open(pmap, encoding="utf-8") as f:
                for line in f:
                    contents += line if "#" not in line else "".join(line.replace("#", str(n)) for n in range(1, 12))
            try:
                self.product_to_id = json.loads(contents)
            except Exception:
                self.product_to_id = {}

    # -- definitions ------------------------------------------------------
    def _load_def_json(self, did: str) -> Dict:
        if did in self._def_cache:
            return self._def_cache[did]
        for d in self._def_dirs:
            p = os.path.join(d, did + ".def.json")
            if os.path.exists(p):
                with open(p, encoding="utf-8") as f:
                    data = json.load(f)
                self._def_cache[did] = data
                return data
        raise FileNotFoundError("Definition %s not found" % did)

    def load_definition(self, did: str) -> Definition:
        chain = []
        cur = did
        while cur:
            data = self._load_def_json(cur)
            chain.append(data)
            cur = data.get("inherits")
        definition = Definition(did)
        meta: Dict[str, Any] = {}
        for data in reversed(chain):  # base first
            meta.update(data.get("metadata", {}))
            if "settings" in data:
                self._walk_settings(data["settings"], definition)
            for key, over in data.get("overrides", {}).items():
                if key not in definition.props:
                    definition.props[key] = {}
                    definition.order.append(key)
                p = definition.props[key]
                # an override of value removes nothing; default_value override keeps value unless replaced
                p.update(over)
        definition.metadata = meta
        definition.name = chain[0].get("name", did)
        # parse formulas
        for key, p in definition.props.items():
            for fprop in ("value", "resolve", "limit_to_extruder", "enabled", "settable_per_extruder"):
                if fprop in p and isinstance(p[fprop], str) and fprop in ("value", "resolve", "limit_to_extruder"):
                    p[fprop] = Formula(p[fprop])
        return definition

    def _walk_settings(self, settings: Dict, definition: Definition, parent_settable=None):
        for key, p in settings.items():
            entry = {k: v for k, v in p.items() if k != "children"}
            if key in definition.props:
                definition.props[key].update(entry)
            else:
                definition.props[key] = entry
                definition.order.append(key)
            if "children" in p:
                self._walk_settings(p["children"], definition)

    # -- instance containers ---------------------------------------------
    def load_instance(self, cid: str, kind: str, types: Dict[str, str]) -> Optional[Container]:
        if not cid or cid.startswith("empty"):
            return None
        if kind == "material":
            return self.load_material(cid, types)
        path = self._inst_index.get(cid)
        if not path:
            return None
        return self.read_inst_cfg(path, cid, kind, types)

    @staticmethod
    def read_inst_cfg(path: str, cid: str, kind: str, types: Dict[str, str]) -> Container:
        cp = configparser.ConfigParser(interpolation=None, strict=False)
        cp.optionxform = str
        with open(path, encoding="utf-8") as f:
            cp.read_file(f)
        c = Container(cid, kind)
        if cp.has_section("general"):
            c.name = cp.get("general", "name", fallback=cid)
            c.metadata["definition"] = cp.get("general", "definition", fallback="")
        if cp.has_section("metadata"):
            c.metadata.update(dict(cp.items("metadata")))
        if cp.has_section("values"):
            for k, v in cp.items("values"):
                c.values[k] = _convert_typed(v, types.get(k))
        return c

    def load_material(self, cid: str, types: Dict[str, str], machine_def_id: str = "", variant_name: str = "") -> Optional[Container]:
        path = self._material_index.get(cid)
        base = cid
        if not path:
            # derived ids look like "<base>_<machine>[_<hotend>]"; find the longest known prefix
            best = ""
            for known in self._material_index:
                if cid.startswith(known) and len(known) > len(best):
                    best = known
            if not best:
                return None
            path = self._material_index[best]
            base = best
        ns = {"um": "http://www.ultimaker.com/material", "cura": "http://www.ultimaker.com/cura"}
        tree = ET.parse(path)
        root = tree.getroot()
        c = Container(cid, "material")
        name_el = root.find("./um:metadata/um:name", ns)
        if name_el is not None:
            c.metadata["brand"] = (name_el.findtext("um:brand", "", ns) or "").strip()
            c.metadata["material"] = (name_el.findtext("um:material", "", ns) or "").strip()
            c.metadata["name"] = (name_el.findtext("um:label", "", ns) or "").strip() or c.metadata["material"]
        c.metadata["GUID"] = (root.findtext("./um:metadata/um:GUID", "", ns) or "").strip()
        c.metadata["base_file"] = base
        c.name = c.metadata.get("name", cid)
        values: Dict[str, Any] = {}
        diam = root.findtext("./um:properties/um:diameter", None, ns)
        if diam:
            values["material_diameter"] = diam
        for tag, key in (("GUID", "material_guid"), ("material", "material_type"), ("brand", "material_brand")):
            v = c.metadata.get(tag)
            if v:
                values[key] = v

        def read_settings(node):
            out = {}
            for s in node.findall("um:setting", ns):
                k = s.get("key")
                if k == "processing temperature graph":
                    pts = [[float(p.get("flow")), float(p.get("temperature"))] for p in s.findall("um:point", ns)]
                    out[MATERIAL_SETTING_MAP[k]] = str(pts)
                elif k in MATERIAL_SETTING_MAP and s.text is not None:
                    out[MATERIAL_SETTING_MAP[k]] = s.text.strip()
            for s in node.findall("cura:setting", ns):
                v = (s.text or "").strip()
                if v.lower() == "yes":
                    v = True
                elif v.lower() == "no":
                    v = False
                out[s.get("key")] = v
            return out

        settings_node = root.find("um:settings", ns)
        if settings_node is not None:
            values.update(read_settings(settings_node))
            # machine specific overrides (+ hotend specific)
            if machine_def_id:
                for machine in settings_node.findall("um:machine", ns):
                    ids = []
                    for ident in machine.findall("um:machine_identifier", ns):
                        prod = ident.get("product", "")
                        ids += self.product_to_id.get(prod, [prod.lower().replace(" ", "_")])
                    if machine_def_id in ids:
                        values.update(read_settings(machine))
                        for hotend in machine.findall("um:hotend", ns):
                            if variant_name and hotend.get("id") == variant_name:
                                values.update(read_settings(hotend))
        for k, v in values.items():
            c.values[k] = v if not isinstance(v, str) else _convert_typed(v, types.get(k))
        return c


MATERIAL_SETTING_MAP = {
    "print temperature": "default_material_print_temperature",
    "heated bed temperature": "default_material_bed_temperature",
    "standby temperature": "material_standby_temperature",
    "processing temperature graph": "material_flow_temp_graph",
    "print cooling": "cool_fan_speed",
    "retraction amount": "retraction_amount",
    "retraction speed": "retraction_speed",
    "adhesion tendency": "material_adhesion_tendency",
    "surface energy": "material_surface_energy",
    "build volume temperature": "build_volume_temperature",
    "anti ooze retract position": "material_anti_ooze_retracted_position",
    "anti ooze retract speed": "material_anti_ooze_retraction_speed",
    "break preparation position": "material_break_preparation_retracted_position",
    "break preparation speed": "material_break_preparation_speed",
    "break preparation temperature": "material_break_preparation_temperature",
    "break position": "material_break_retracted_position",
    "flush purge speed": "material_flush_purge_speed",
    "flush purge length": "material_flush_purge_length",
    "end of filament purge speed": "material_end_of_filament_purge_speed",
    "end of filament purge length": "material_end_of_filament_purge_length",
    "maximum park duration": "material_maximum_park_duration",
    "no load move factor": "material_no_load_move_factor",
    "break speed": "material_break_speed",
    "break temperature": "material_break_temperature",
}

# --------------------------------------------------------------------------
# Stacks (GlobalStack / ExtruderStack semantics)
# --------------------------------------------------------------------------

ORDER = ["user", "quality_changes", "intent", "quality", "material", "variant", "definition_changes", "definition"]


class Stack:
    def __init__(self, sid: str, containers: List[Container], is_global: bool, position: str = ""):
        self.id = sid
        self.containers = containers  # top -> bottom; last is Definition
        self.is_global = is_global
        self.position = position
        self.next: Optional["Stack"] = None
        self.extruders: Dict[str, "Stack"] = {}
        self._cache: Dict[str, Any] = {}
        self._resolving: set = set()
        self.metadata: Dict[str, Any] = {}

    @property
    def definition(self) -> Definition:
        return self.containers[-1]  # type: ignore

    def container(self, kind: str) -> Optional[Container]:
        for c in self.containers:
            if c.kind == kind:
                return c
        return None

    def has_user_value(self, key: str) -> bool:
        for kind in ("user", "quality_changes"):
            c = self.container(kind)
            if c is not None and c.has_value(key):
                return True
        return False

    def all_keys(self) -> List[str]:
        d = self.definition
        if not self.is_global and self.next is not None:
            keys = list(self.next.definition.order)
            for k in d.order:
                if k not in self.next.definition.props:
                    keys.append(k)
        else:
            keys = list(d.order)
        return [k for k in keys if self._prop_any(k, "type") != "category"]

    def _prop_any(self, key, prop):
        v = self.definition.get(key, prop)
        if v is None and self.next is not None:
            v = self.next.definition.get(key, prop)
        return v

    # Uranium ContainerStack.getRawProperty: own containers, then the next stack's
    # containers (raw, without GlobalStack resolve/limit_to_extruder logic)
    def _raw(self, key: str, prop: str, start_index: int = 0):
        containers = self.containers[start_index:] if start_index < len(self.containers) else []
        if prop not in ("value", "state", "validationState"):
            v = containers[-1].get(key, prop) if containers else None
            if v is not None:
                return v
        else:
            for c in containers:
                v = c.get(key, prop)
                if v is not None:
                    return v
        if self.next is not None:
            return self.next._raw(key, prop, start_index)
        return None

    # Uranium ContainerStack.getProperty: raw value, formulas evaluated with this stack
    def _base_get(self, key: str, prop: str, start_index: int = 0):
        v = self._raw(key, prop, start_index)
        if isinstance(v, Formula):
            return evaluate(v, self)
        return v

    def get(self, key: str, prop: str = "value", start_index: int = 0):
        if prop == "value" and start_index == 0 and key in self._cache:
            return self._cache[key]
        result = self._get(key, prop, start_index)
        if prop == "value" and start_index == 0:
            self._cache[key] = result
        return result

    def _get(self, key: str, prop: str, start_index: int):
        if self.is_global:
            # GlobalStack.getProperty
            if key not in self.definition.props:
                return None
            if prop == "value" and self.definition.get(key, "resolve") is not None and key not in self._resolving \
                    and not self.has_user_value(key):
                self._resolving.add(key)
                try:
                    resolve = self._base_get(key, "resolve", start_index)
                finally:
                    self._resolving.discard(key)
                if resolve is not None:
                    return resolve
            lte = self._base_get(key, "limit_to_extruder", start_index)
            if lte is not None:
                lte = str(lte)
                if lte != "-1" and lte in self.extruders and self._base_get(key, "settable_per_extruder"):
                    r = self.extruders[lte].get(key, prop)
                    if r is not None:
                        return r
            return self._base_get(key, prop, start_index)
        # ExtruderStack.getProperty
        if not self._base_get(key, "settable_per_extruder"):
            return self.next.get(key, prop) if self.next else None
        lte = self._base_get(key, "limit_to_extruder")
        if lte is not None and str(lte) != "-1" and str(lte) != str(self.position) and self.next is not None:
            other = self.next.extruders.get(str(lte))
            if other is not None and other is not self:
                r = other.get(key, prop)
                if r is not None:
                    return r
        return self._base_get(key, prop, start_index)


# --------------------------------------------------------------------------
# Formula evaluation (SettingFunction + CuraFormulaFunctions)
# --------------------------------------------------------------------------

_current_global: List[Stack] = []


def _global() -> Stack:
    return _current_global[-1]


def _extruder(pos) -> Optional[Stack]:
    g = _global()
    try:
        pos = int(pos)
    except (TypeError, ValueError):
        pos = 0
    if pos == -1:
        pos = 0
    return g.extruders.get(str(pos))


def op_extruderValue(extruder_position, key):
    e = _extruder(extruder_position)
    if e is None:
        return _global().get(key)
    v = e.get(key)
    return v


def op_extruderValues(key):
    g = _global()
    out = []
    for pos in sorted(g.extruders, key=int):
        e = g.extruders[pos]
        if not e.get("extruder_enabled"):  # disabled extruders are skipped
            continue
        v = e.get(key)
        if v is None:
            v = g.get(key)
        out.append(v)
    if not out:
        out.append(g.get(key))
    return out


def op_anyExtruderWithMaterial(filter_key):
    g = _global()
    for pos in sorted(g.extruders, key=int):
        e = g.extruders[pos]
        mat = e.container("material")
        if mat is not None and str(mat.metadata.get(filter_key, "")).lower() in ("true", "1", "yes"):
            return str(pos)
    return "0"


def op_anyExtruderNrWithOrDefault(filter_key):
    g = _global()
    for pos in sorted(g.extruders, key=int):
        if g.extruders[pos].get(filter_key):
            return str(pos)
    return "0"


def op_resolveOrValue(key):
    return _global().get(key)


def op_defaultExtruderPosition():
    return "0"


def op_valueFromContainer(key, container_index):
    return _global().get(key, start_index=int(container_index))


def op_extruderValueFromContainer(extruder_position, key, container_index):
    e = _extruder(extruder_position)
    if e is None:
        return None
    return e.get(key, start_index=int(container_index))


OPERATORS = {
    "extruderValue": op_extruderValue,
    "extruderValues": op_extruderValues,
    "anyExtruderWithMaterial": op_anyExtruderWithMaterial,
    "anyExtruderNrWithOrDefault": op_anyExtruderNrWithOrDefault,
    "resolveOrValue": op_resolveOrValue,
    "defaultExtruderPosition": op_defaultExtruderPosition,
    "valueFromContainer": op_valueFromContainer,
    "extruderValueFromContainer": op_extruderValueFromContainer,
}

_FORMULA_GLOBALS: Dict[str, Any] = {
    "math": math, "base64": base64, "hashlib": hashlib, "uuid": uuid,
    "__builtins__": {k: getattr(builtins, k) for k in _SAFE_BUILTINS if hasattr(builtins, k)},
}
_FORMULA_GLOBALS.update(OPERATORS)

_eval_depth = [0]


def evaluate(f: Formula, provider: Stack, extra: Optional[Dict[str, Any]] = None):
    if f.compiled is None:
        return None
    _eval_depth[0] += 1
    try:
        if _eval_depth[0] > 200:
            return 0
        local = {}
        for name in f.names:
            if extra and name in extra:
                continue
            v = provider.get(name)
            if v is not None:
                local[name] = v
        if extra:
            local.update(extra)
        try:
            return eval(f.compiled, dict(_FORMULA_GLOBALS), local)
        except Exception:
            return 0  # Uranium: "Settings may be used in calculations and they need a value"
    finally:
        _eval_depth[0] -= 1


# --------------------------------------------------------------------------
# Start/End g-code token expansion (GcodeStartEndFormatter)
# --------------------------------------------------------------------------

_INSTR = re.compile(r"{(?P<condition>if|else|elif|endif)?\s*(?P<expression>[^{}]*?)\s*(?:,\s*(?P<extruder_nr_expr>[^{}]*))?\s*}(?P<end_of_line>\n?)")
_POST_SLICE = ["filament_cost", "print_time", "filament_amount", "filament_weight", "jobname"]


def expand_gcode(text: str, all_settings: Dict[str, Dict[str, Any]], stacks: Dict[str, Stack], default_extruder_nr) -> str:
    OUT, TRUE, FALSE, DONE = 1, 3, 2, 4
    state = OUT
    result = ""
    remaining = text or ""

    def emit(s):
        return s if state in (OUT, TRUE) else ""

    while remaining:
        m = _INSTR.search(remaining)
        if not m:
            result += emit(remaining)
            break
        s, e = m.span()
        if s > 0:
            result += emit(remaining[:s])
        remaining = remaining[e:]
        cond, expr, ext_expr, eol = m.group("condition"), m.group("expression"), m.group("extruder_nr_expr"), m.group("end_of_line")
        if expr in _POST_SLICE:
            result += "{%s}" % expr
            continue
        extruder_nr = str(default_extruder_nr)
        instr = 1  # skip
        if cond is None:
            if state in (OUT, TRUE):
                instr = 3
        else:
            if cond == "if":
                instr = 2
            else:
                if state == TRUE:
                    state = DONE
                if cond == "elif":
                    instr = 2 if state == FALSE else 1
                elif cond == "else":
                    instr = 1
                    if state == FALSE:
                        state = TRUE
                elif cond == "endif":
                    instr = 1
                    state = OUT
        if instr >= 2 and ext_expr is not None:
            extruder_nr = str(evaluate(Formula(ext_expr), stacks["-1"], all_settings["-1"]))
        if instr >= 2:
            extra = dict(all_settings.get(extruder_nr, all_settings["-1"]))
            stack = stacks.get(extruder_nr, stacks["-1"])
            value = evaluate(Formula(expr), stack, extra)
            if instr == 2:
                state = TRUE if value else FALSE
            else:
                result += str(value) + (eol or "")
    return result


# --------------------------------------------------------------------------
# Building the machine from the user's Cura configuration
# --------------------------------------------------------------------------


def _read_stack_cfg(path: str) -> Tuple[Dict[str, str], Dict[str, str], List[str]]:
    cp = configparser.ConfigParser(interpolation=None, strict=False)
    cp.optionxform = str
    with open(path, encoding="utf-8") as f:
        cp.read_file(f)
    general = dict(cp.items("general")) if cp.has_section("general") else {}
    meta = dict(cp.items("metadata")) if cp.has_section("metadata") else {}
    containers = []
    if cp.has_section("containers"):
        items = sorted(cp.items("containers"), key=lambda kv: int(kv[0]))
        containers = [v for _, v in items]
    return general, meta, containers


def list_machines(config_dir: str) -> List[Dict[str, str]]:
    out = []
    for path in glob.glob(os.path.join(config_dir, "machine_instances", "*.global.cfg")):
        general, meta, containers = _read_stack_cfg(path)
        out.append({"name": general.get("name", ""), "id": general.get("id", ""),
                    "definition": containers[-1] if containers else "", "path": path})
    return out


def active_machine_name(config_dir: str) -> Optional[str]:
    cfg = os.path.join(config_dir, "cura.cfg")
    if not os.path.exists(cfg):
        return None
    cp = configparser.ConfigParser(interpolation=None, strict=False)
    try:
        cp.read(cfg, encoding="utf-8")
        return cp.get("cura", "active_machine", fallback=None)
    except Exception:
        return None


class CuraMachine:
    """A fully resolved Cura machine (global stack + extruder stacks)."""

    def __init__(self, resources_dir: str, config_dir: Optional[str], machine: Optional[str] = None,
                 definition_id: str = "creality_ender2"):
        self.res = Resources(resources_dir, config_dir)
        self.config_dir = config_dir
        self.global_stack: Stack
        self.machine_name = ""
        global_path = None
        if config_dir:
            machines = list_machines(config_dir)
            wanted = machine or active_machine_name(config_dir)
            for m in machines:
                if wanted and (m["name"] == wanted or m["id"] == wanted):
                    global_path = m["path"]
            if global_path is None:
                for m in machines:
                    if m["definition"] == definition_id:
                        global_path = m["path"]
                        break
            if global_path is None and len(machines) == 1:
                global_path = machines[0]["path"]
        if global_path:
            self._build_from_cfg(global_path)
        else:
            self._build_default(definition_id)

    # .. helpers
    def _types(self, definition: Definition) -> Dict[str, str]:
        return {k: p.get("type") for k, p in definition.props.items()}

    def _make_stack(self, sid, ids: List[str], is_global, definition: Definition, machine_def: str, variant_name: str = ""):
        types = self._types(definition) if is_global else {**self._types(self._global_def), **self._types(definition)}
        containers: List[Container] = []
        if not hasattr(self, "missing"):
            self.missing = []
        kinds = ORDER
        for kind, cid in zip(kinds, ids[:-1]):
            if kind == "material":
                c = self.res.load_material(cid, types, machine_def, variant_name) if cid and not cid.startswith("empty") else None
            else:
                c = self.res.load_instance(cid, kind, types)
            if c is not None:
                c.kind = kind
                containers.append(c)
            elif cid and not cid.startswith("empty") and kind not in ("user", "definition_changes"):
                self.missing.append("%s '%s' not found" % (kind, cid))
        containers.append(definition)
        return Stack(sid, containers, is_global)

    def _build_from_cfg(self, global_path: str):
        general, meta, ids = _read_stack_cfg(global_path)
        self.machine_name = general.get("name", "")
        def_id = ids[-1]
        self._global_def = self.res.load_definition(def_id)
        self.global_stack = self._make_stack(general.get("id", def_id), ids, True, self._global_def, def_id)
        self.global_stack.metadata = meta
        # extruders
        for path in glob.glob(os.path.join(self.config_dir, "extruders", "*.extruder.cfg")):
            eg, em, eids = _read_stack_cfg(path)
            if em.get("machine") not in (general.get("id"), general.get("name")):
                continue
            pos = em.get("position", "0")
            edef = self.res.load_definition(eids[-1])
            variant_name = ""
            vc = self.res.load_instance(eids[5], "variant", {}) if len(eids) > 5 else None
            if vc is not None:
                variant_name = vc.name
            st = self._make_stack(eg.get("id", "extruder" + pos), eids, False, edef, def_id, variant_name)
            st.position = pos
            st.metadata = em
            st.next = self.global_stack
            self.global_stack.extruders[pos] = st
        if not self.global_stack.extruders:
            self._add_default_extruder(def_id)

    def _build_default(self, def_id: str):
        """No Cura config found: Cura defaults for this printer (standard quality, preferred material)."""
        self.machine_name = def_id
        self._global_def = self.res.load_definition(def_id)
        meta = self._global_def.metadata
        qtype = meta.get("preferred_quality_type", "normal")
        qdef = meta.get("quality_definition", def_id)
        gq = None
        for cid, path in self.res._inst_index.items():
            if "/quality/" in path.replace("\\", "/"):
                c = Resources.read_inst_cfg(path, cid, "quality", {})
                if c.metadata.get("definition") == qdef and c.metadata.get("global_quality") in ("True", "true") \
                        and c.metadata.get("quality_type") == qtype:
                    gq = cid
        ids = ["", "", "", gq or "", "", "", "", def_id]
        self.global_stack = self._make_stack(def_id, ids, True, self._global_def, def_id)
        self._add_default_extruder(def_id, qtype, qdef)

    def _add_default_extruder(self, def_id, qtype=None, qdef=None):
        meta = self._global_def.metadata
        trains = meta.get("machine_extruder_trains", {"0": "fdmextruder"})
        qtype = qtype or meta.get("preferred_quality_type", "normal")
        qdef = qdef or meta.get("quality_definition", def_id)
        variant_name = meta.get("preferred_variant_name", "0.4mm Nozzle")
        mat_base = meta.get("preferred_material", "generic_pla")
        for pos, edef_id in trains.items():
            edef = self.res.load_definition(edef_id)
            # variant
            vid = ""
            for cid, path in self.res._inst_index.items():
                if "/variants/" in path.replace("\\", "/"):
                    c = Resources.read_inst_cfg(path, cid, "variant", {})
                    if c.metadata.get("definition") == def_id and c.name == variant_name:
                        vid = cid
            # material: 1.75 mm variant when the printer uses 1.75 filament
            diameter = self._global_def.get("material_diameter", "value")
            mid = mat_base + ("_175" if diameter and float(diameter) < 2.5 else "")
            # quality for material + variant
            qid = ""
            for cid, path in self.res._inst_index.items():
                if "/quality/" in path.replace("\\", "/"):
                    c = Resources.read_inst_cfg(path, cid, "quality", {})
                    if c.metadata.get("definition") == qdef and c.metadata.get("quality_type") == qtype and \
                            c.metadata.get("material") == mat_base and c.metadata.get("variant", variant_name) == variant_name:
                        qid = cid
            ids = ["", "", "", qid, mid, vid, "", edef_id]
            st = self._make_stack("extruder" + pos, ids, False, edef, def_id, variant_name)
            st.position = pos
            st.next = self.global_stack
            self.global_stack.extruders[pos] = st

    # .. public
    def summary(self) -> Dict[str, Any]:
        g = self.global_stack
        e = g.extruders.get("0")

        def name_of(st, kind):
            c = st.container(kind) if st else None
            return c.name if c else ""
        return {
            "machine": self.machine_name,
            "definition": g.definition.id,
            "quality": name_of(e, "quality") or name_of(g, "quality"),
            "custom_profile": name_of(g, "quality_changes") or name_of(e, "quality_changes"),
            "material": name_of(e, "material"),
            "nozzle": name_of(e, "variant"),
            "layer_height": self.value("layer_height"),
            "infill": self.value("infill_sparse_density", extruder=True),
            "print_temperature": self.value("material_print_temperature", extruder=True),
            "bed_temperature": self.value("material_bed_temperature", extruder=True) or self.value("material_bed_temperature"),
            "heated_bed": self.value("machine_heated_bed"),
            "build_volume": [self.value("machine_width"), self.value("machine_depth"), self.value("machine_height")],
            "support": self.value("support_enable"),
            "adhesion": self.value("adhesion_type"),
            "warnings": list(getattr(self, "missing", [])),
        }

    def value(self, key: str, extruder: bool = False):
        _current_global.append(self.global_stack)
        try:
            if extruder and "0" in self.global_stack.extruders:
                return self.global_stack.extruders["0"].get(key)
            return self.global_stack.get(key)
        finally:
            _current_global.pop()

    def resolve(self, overrides: Optional[Dict[str, Any]] = None) -> Tuple[Dict[str, str], Dict[str, Dict[str, str]]]:
        """Return (global_settings, {extruder_nr: settings}) as strings, like StartSliceJob."""
        g = self.global_stack
        if overrides:
            types = self._types(self._global_def)
            uc = g.container("user")
            if uc is None:
                uc = Container("cli_user", "user")
                g.containers.insert(0, uc)
            for k, v in overrides.items():
                uc.values[k] = _convert_typed(str(v), types.get(k)) if isinstance(v, str) else v
            for e in g.extruders.values():
                euc = e.container("user")
                if euc is None:
                    euc = Container("cli_user_e", "user")
                    e.containers.insert(0, euc)
                for k, v in overrides.items():
                    euc.values[k] = _convert_typed(str(v), types.get(k)) if isinstance(v, str) else v
            for st in [g] + list(g.extruders.values()):
                st._cache.clear()
        _current_global.append(g)
        try:
            def tokens(st: Stack) -> Dict[str, Any]:
                res = {}
                for key in st.all_keys():
                    res[key] = st.get(key)
                mat = None
                for s in ([st] + ([st.extruders.get("0")] if st.is_global else [])):
                    if s is not None and s.container("material") is not None:
                        mat = s.container("material")
                        break
                res["material_id"] = mat.metadata.get("base_file", "") if mat else ""
                res["material_type"] = mat.metadata.get("material", "") if mat else ""
                res["material_name"] = mat.metadata.get("name", "") if mat else ""
                res["material_brand"] = mat.metadata.get("brand", "") if mat else ""
                q = st.container("quality")
                qc = st.container("quality_changes")
                res["quality_name"] = q.name if q else ""
                res["quality_changes_name"] = qc.name if qc else None
                res["print_bed_temperature"] = res.get("material_bed_temperature")
                res["print_temperature"] = res.get("material_print_temperature")
                res["travel_speed"] = res.get("speed_travel")
                res["time"] = time.strftime("%H:%M:%S")
                res["date"] = time.strftime("%d-%m-%Y")
                res["day"] = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"][int(time.strftime("%w"))]
                res["initial_extruder_nr"] = 0
                return res

            all_settings = {"-1": tokens(g)}
            stacks = {"-1": g}
            for pos, e in g.extruders.items():
                nr = str(e.get("extruder_nr"))
                all_settings[nr] = tokens(e)
                stacks[nr] = e

            gs = dict(all_settings["-1"])
            start = re.sub(r";.+?(\n|$)", "\n", str(gs.get("machine_start_gcode", "")))
            if gs.get("material_bed_temp_prepend"):
                pat = r"\{(%s)(,\s?\w+)?\}" % "|".join(["material_bed_temperature", "material_bed_temperature_layer_0"])
                gs["material_bed_temp_prepend"] = re.search(pat, start) is None
            if gs.get("material_print_temp_prepend"):
                pat = r"\{(%s)(,\s?\w+)?\}" % "|".join(["material_print_temperature", "material_print_temperature_layer_0",
                                                          "default_material_print_temperature", "material_initial_print_temperature",
                                                          "material_final_print_temperature", "material_standby_temperature", "print_temperature"])
                gs["material_print_temp_prepend"] = re.search(pat, start) is None
            gs["machine_start_gcode"] = expand_gcode(str(gs.get("machine_start_gcode", "")), all_settings, stacks, 0)
            gs["machine_end_gcode"] = expand_gcode(str(gs.get("machine_end_gcode", "")), all_settings, stacks, 0)
            gs["nozzle_offsetting_for_disallowed_areas"] = True

            ext_out: Dict[str, Dict[str, str]] = {}
            gdef = g.definition
            for pos, e in g.extruders.items():
                nr = str(e.get("extruder_nr"))
                s = dict(all_settings[nr])
                mat = e.container("material")
                s["material_guid"] = mat.metadata.get("GUID", "") if mat else ""
                for k in ("machine_extruder_prestart_code", "machine_extruder_start_code", "machine_extruder_end_code"):
                    if k in s:
                        s[k] = expand_gcode(str(s[k] or ""), all_settings, stacks, nr)
                out = {}
                for k, v in s.items():
                    if not gdef.get(k, "settable_per_extruder") and not e.definition.get(k, "settable_per_extruder"):
                        continue
                    out[k] = _to_engine_str(v)
                ext_out[nr] = out
            return {k: _to_engine_str(v) for k, v in gs.items()}, ext_out
        finally:
            _current_global.pop()


def _to_engine_str(v) -> str:
    return str(v)
