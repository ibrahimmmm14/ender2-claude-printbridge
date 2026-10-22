"""
server.py - PrintBridge MCP server: lets Claude find, slice (with your Cura
profile) and print 3D models on an ESP3D-connected printer.

Run: python -m printbridge.server
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from mcp.server.fastmcp import FastMCP

from . import core

mcp = FastMCP(
    "printbridge",
    instructions=(
        "Tools for a Creality Ender-2 3D printer driven by ESP3D, sliced with the user's own Cura profile. "
        "Workflow: find a model on the web (Printables/Thingiverse) -> download_model -> check size with "
        "inspect_model -> slice_model -> show the user the summary (time, filament, settings, size) -> "
        "upload_gcode -> ONLY after the user explicitly confirms, start_print(confirm=True). "
        "Never start a print without the user's explicit OK in the conversation. Remind the user that someone "
        "should watch the first layer and that the printer must not be left unattended for long."
    ),
)


@mcp.tool()
def printer_status() -> Dict[str, Any]:
    """Printer / ESP3D status: online, current job progress, hotend and bed temperatures, Tailscale address."""
    return core.printer_status()


@mcp.tool()
def cura_profile() -> Dict[str, Any]:
    """Show the Cura machine profile that will be used for slicing (custom profile, layer height, infill,
    material, temperatures, build volume)."""
    return core.cura_profile()


@mcp.tool()
def download_model(url: str) -> Dict[str, Any]:
    """Download a 3D model from a URL (Printables or Thingiverse model page, or a direct .stl/.3mf/.obj/.zip
    link) to the PC. Returns local file paths with size and fit-on-bed information."""
    return core.download_model(url)


@mcp.tool()
def inspect_model(path: str) -> Dict[str, Any]:
    """Size (mm), volume and whether a local model file fits the printer's build volume."""
    return core.inspect_model(path)


@mcp.tool()
def slice_model(path: str, layer_height: Optional[float] = None, infill_percent: Optional[float] = None,
                supports: Optional[bool] = None, adhesion: Optional[str] = None, scale_percent: float = 100.0,
                rotate_x: float = 0, rotate_y: float = 0, rotate_z: float = 0, lay_flat: bool = False,
                extra_settings: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Slice a local STL/3MF/OBJ with CuraEngine using the user's Cura profile. Leave options empty to use the
    profile as-is. adhesion: skirt | brim | raft | none. extra_settings: raw Cura setting keys
    (e.g. {"wall_line_count": 3}). Returns print time, filament, layers and the G-code path."""
    return core.slice_model(path, layer_height, infill_percent, supports, adhesion, scale_percent,
                            rotate_x, rotate_y, rotate_z, lay_flat, extra_settings)


@mcp.tool()
def upload_gcode(gcode_path: str, remote_name: Optional[str] = None) -> Dict[str, Any]:
    """Upload a G-code file to the ESP3D board (does not start printing)."""
    return core.upload_gcode(gcode_path, remote_name)


@mcp.tool()
def start_print(remote_file: str, confirm: bool = False) -> Dict[str, Any]:
    """Start printing a G-code file already uploaded to ESP3D. Requires confirm=True, which you may only set
    after the user explicitly approved this exact print in the conversation."""
    if not confirm:
        return {"ok": False, "error": "Ask the user to confirm the print first, then call again with confirm=True."}
    return core.start_print(remote_file)


@mcp.tool()
def print_progress() -> Dict[str, Any]:
    """Progress of the running print (bytes processed / percent) and current temperatures."""
    return core.print_progress()


@mcp.tool()
def control_print(action: str) -> Dict[str, Any]:
    """Pause, resume or cancel the running print. action: pause | resume | cancel (cancel also turns heaters off)."""
    return core.control_print(action)


@mcp.tool()
def send_gcode(gcode: str, force: bool = False) -> Dict[str, Any]:
    """Send one G-code command to the printer and return its answer (e.g. M105, G28, M104 S0).
    Refused while a print streams unless force=True (only for emergencies such as M112)."""
    return core.send_gcode(gcode, force)


@mcp.tool()
def list_printer_files() -> Dict[str, Any]:
    """List G-code files stored on the ESP3D board with free space."""
    return core.list_printer_files()


@mcp.tool()
def delete_printer_file(name: str) -> Dict[str, Any]:
    """Delete a file from the ESP3D board storage."""
    return core.delete_printer_file(name)


def main():
    mcp.run()


if __name__ == "__main__":
    main()
