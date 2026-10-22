"""
cli.py - Command line access to PrintBridge (same operations as the MCP tools).

  python -m printbridge.cli status
  python -m printbridge.cli profile
  python -m printbridge.cli download <url>
  python -m printbridge.cli inspect <model>
  python -m printbridge.cli slice <model> [--layer 0.2] [--infill 20] [--supports] [--adhesion brim]
                                          [--scale 100] [--rz 0] [--lay-flat] [--set key=value ...]
  python -m printbridge.cli upload <gcode>
  python -m printbridge.cli print <remote_file> --yes
  python -m printbridge.cli progress | pause | resume | cancel
  python -m printbridge.cli gcode "M105"
  python -m printbridge.cli files | delete <name>
  python -m printbridge.cli config [key=value ...]
"""
from __future__ import annotations

import argparse
import json
import sys

from . import core


def main(argv=None):
    ap = argparse.ArgumentParser(prog="printbridge")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    sub.add_parser("profile")
    p = sub.add_parser("download"); p.add_argument("url")
    p = sub.add_parser("inspect"); p.add_argument("model")
    p = sub.add_parser("slice")
    p.add_argument("model"); p.add_argument("--layer", type=float); p.add_argument("--infill", type=float)
    p.add_argument("--supports", action="store_true", default=None); p.add_argument("--no-supports", dest="supports", action="store_false")
    p.add_argument("--adhesion"); p.add_argument("--scale", type=float, default=100.0)
    p.add_argument("--rx", type=float, default=0); p.add_argument("--ry", type=float, default=0); p.add_argument("--rz", type=float, default=0)
    p.add_argument("--lay-flat", action="store_true"); p.add_argument("--set", action="append", default=[])
    p = sub.add_parser("upload"); p.add_argument("gcode"); p.add_argument("--name")
    p = sub.add_parser("print"); p.add_argument("remote_file"); p.add_argument("--yes", action="store_true")
    for a in ("progress", "pause", "resume", "cancel", "files"):
        sub.add_parser(a)
    p = sub.add_parser("gcode"); p.add_argument("line"); p.add_argument("--force", action="store_true")
    p = sub.add_parser("delete"); p.add_argument("name")
    p = sub.add_parser("config"); p.add_argument("pairs", nargs="*")
    a = ap.parse_args(argv)
    try:
        r = _run(a)
    except Exception as e:  # friendly error instead of a traceback
        r = {"error": "%s: %s" % (type(e).__name__, e)}
    if isinstance(r, int):
        return r
    print(json.dumps(r, indent=2, ensure_ascii=False, default=str))
    return 1 if isinstance(r, dict) and r.get("error") else 0


def _run(a):

    if a.cmd == "status":
        r = core.printer_status()
    elif a.cmd == "profile":
        r = core.cura_profile()
    elif a.cmd == "download":
        r = core.download_model(a.url)
    elif a.cmd == "inspect":
        r = core.inspect_model(a.model)
    elif a.cmd == "slice":
        extra = {}
        for kv in a.set:
            k, _, v = kv.partition("=")
            extra[k.strip()] = v.strip()
        r = core.slice_model(a.model, a.layer, a.infill, a.supports, a.adhesion, a.scale, a.rx, a.ry, a.rz,
                             a.lay_flat, extra or None)
    elif a.cmd == "upload":
        r = core.upload_gcode(a.gcode, a.name)
    elif a.cmd == "print":
        if not a.yes:
            return {"error": "Refusing to start without --yes (confirm the print first)."}
        r = core.start_print(a.remote_file)
    elif a.cmd == "progress":
        r = core.print_progress()
    elif a.cmd in ("pause", "resume", "cancel"):
        r = core.control_print(a.cmd)
    elif a.cmd == "gcode":
        r = core.send_gcode(a.line, a.force)
    elif a.cmd == "files":
        r = core.list_printer_files()
    elif a.cmd == "delete":
        r = core.delete_printer_file(a.name)
    elif a.cmd == "config":
        cfg = core.load_config()
        for kv in a.pairs:
            k, _, v = kv.partition("=")
            cfg[k] = int(v) if v.isdigit() else v
        if a.pairs:
            core.save_config(cfg)
        r = cfg
    return r


if __name__ == "__main__":
    sys.exit(main())
