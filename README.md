# Ender-2 × Claude — PrintBridge

Control a **Creality Ender-2** 3D printer from **Claude** (desktop *and* mobile app), from anywhere.

Ask Claude in plain language — *"find a phone stand on Printables, slice it at 0.2 mm with 20 % infill and print it"* —
and it searches for the model, downloads it, checks it fits the bed, slices it with **your own Cura profile**,
uploads the G-code to the printer and starts the print **only after you confirm**.

```
 Claude (desktop / mobile)
        │  MCP
        ▼
 PrintBridge MCP server (Python, on the PC)
   ├─ download_model  → Printables / Thingiverse / direct STL
   ├─ slice_model     → CuraEngine + your Cura machine profile
   └─ upload / start  → HTTP + WebSocket
        │
        ▼   (LAN or Tailscale when away from home)
 ESP32-S3 (N16R8) running ESP3D 3.x  ── UART ──  Ender-2 (Marlin)
```

## What's in this repo

| Folder | What it is |
|---|---|
| `firmware/` | **ESP32-S3 firmware** (ESP3D + built-in Tailscale): full flash image and OTA app image, with flashing guide. |
| `printbridge/` | The MCP server + CLI (Python). Talks to ESP3D, slices with CuraEngine, downloads models. |
| `install.ps1` / `install.bat` | One-click Windows installer: Python, venv, config and Claude Desktop registration. |
| `cura-plugin/ESP3DPrinter/` | A Cura plugin: **"Print via Wi-Fi"** button + Monitor tab (temperatures, progress, pause/resume/cancel) for an ESP3D printer. |
| `cura-profile/` | Ender-2 machine settings used for slicing. |
| `examples/` | A 20 mm calibration cube for a first test. |
| `config.example.json` | Copy to `config.json` and set your printer address. |

## Hardware

- Creality Ender-2 (Marlin firmware)
- ESP32-S3 board (N16R8) flashed with **ESP3D 3.x**, connected to the printer's serial port
- A Windows PC with Cura installed (PrintBridge uses Cura's bundled CuraEngine and your Cura profile)
- **Tailscale** on the PC/network so the printer is reachable when you're away from home

## MCP tools

| Tool | Does |
|---|---|
| `printer_status` | Online state, temperatures, current job, network/Tailscale info |
| `cura_profile` | Shows the Cura profile that will be used (layer height, infill, material, temps, build volume) |
| `download_model` | Downloads a model from a Printables/Thingiverse page or direct link |
| `inspect_model` | Size, volume, and whether it fits the 150 × 150 × 200 mm bed |
| `slice_model` | Slices with CuraEngine (layer height, infill, supports, adhesion, scale, rotation, raw Cura keys) |
| `upload_gcode` | Uploads G-code to the ESP3D storage |
| `start_print` | Starts a print — **requires `confirm=True`, only after the user approves** |
| `print_progress` / `control_print` | Progress, pause / resume / cancel |
| `send_gcode` | Single G-code command (blocked while printing unless forced) |
| `list_printer_files` / `delete_printer_file` | Manage files on the board |

## Setup

### 0. Flash the ESP32-S3

```bash
esptool --chip esp32s3 --baud 921600 write-flash 0x0 firmware/ESP3D_Tailscale_PrintBridge_FULL_0x0.bin
```
Then set Wi-Fi and the Tailscale auth key. See [`firmware/README.md`](firmware/README.md).

### 1. PrintBridge (MCP server)

**Quick way:** run `install.bat` (or right-click `install.ps1` → *Run with PowerShell*). It installs everything
into `%USERPROFILE%\PrintBridge`, asks for the printer address and registers the server in Claude Desktop.

**Manual way:**

```powershell
git clone https://github.com/ibrahimmmm14/ender2-claude-printbridge.git
cd ender2-claude-printbridge
python -m venv venv
venv\Scripts\pip install -r requirements.txt

mkdir $HOME\PrintBridge
copy config.example.json $HOME\PrintBridge\config.json   # then edit printer_url
```

`config.json` is looked up at `~/PrintBridge/config.json`, or wherever the `PRINTBRIDGE_CONFIG` environment variable points.
Leave `cura_install` / `cura_config` empty to auto-detect Cura.

Quick test from the command line:

```powershell
venv\Scripts\python -m printbridge.cli status
venv\Scripts\python -m printbridge.cli slice examples\cube_20mm.stl
```

### 2. Register it in Claude Desktop

Add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "printbridge": {
      "command": "C:\\path\\to\\ender2-claude-printbridge\\venv\\Scripts\\python.exe",
      "args": ["-m", "printbridge.server"],
      "env": { "PYTHONPATH": "C:\\path\\to\\ender2-claude-printbridge" }
    }
  }
}
```

Restart Claude Desktop. With the PC on and Claude Desktop running, the same tools are available when you talk to Claude from the mobile app.

### 3. Cura plugin (optional)

Copy `cura-plugin/ESP3DPrinter` into your Cura plugins folder
(e.g. `%APPDATA%\cura\5.x\plugins\ESP3DPrinter\ESP3DPrinter`), restart Cura, and set the printer address
(LAN IP or Tailscale address) under *Extensions → ESP3D Wi-Fi Printer*.

## Safety

- Claude can never start a print without explicit confirmation (`start_print` refuses without `confirm=True`).
- Manual G-code is blocked while a file is streaming, so it can't break the print.
- Watch the first layer, and don't leave the printer unattended for long.

## License

MIT — see `LICENSE`.

Built by **Ibrahim Moawad** with Claude.
