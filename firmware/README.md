# ESP32-S3 firmware — ESP3D + Tailscale

Firmware for the ESP32-S3 (N16R8, 16 MB flash) that sits between the Ender-2 and the network.
It runs **ESP3D 3.x** (web UI, HTTP API, file storage and G-code streaming to Marlin) and has a built-in
**Tailscale** client (WireGuard), so the printer can be reached securely from anywhere, with no port forwarding.

| File | Use |
|---|---|
| `ESP3D_Tailscale_PrintBridge_FULL_0x0.bin` | Full 16 MB flash image (bootloader + partition table + app + file system). Flash at **0x0** on a new board. |
| `ESP3D_Tailscale_PrintBridge_app_ota.bin` | Application only. Use it to update a board that already runs this firmware (ESP3D web update / OTA). |

SHA-256:
```
f6426320a30219c38160d040b2f432ce48aec169e3e0fdaa73213c713b3cbe96  ESP3D_Tailscale_PrintBridge_FULL_0x0.bin
50e00869449f3be37e9af7c696f291080825dbee7a3c88f6d09d5fd1f876986f  ESP3D_Tailscale_PrintBridge_app_ota.bin
```

The image has **no Wi-Fi credentials and no Tailscale key** in it. You enter them after flashing.

## Flash

```bash
pip install esptool
esptool --chip esp32s3 --baud 921600 write-flash 0x0 ESP3D_Tailscale_PrintBridge_FULL_0x0.bin
```
(On older esptool versions use `esptool.py` and `write_flash`.) If the port isn't detected, hold **BOOT**, tap **RST**, then release **BOOT**.

## First setup

1. After a reboot the board starts an ESP3D access point. Connect to it and open the web UI.
2. Set your Wi-Fi network (ESP3D commands `[ESP100]` SSID / `[ESP101]` password, or the web UI settings).
3. **Tailscale:** in the Tailscale admin console go to *Settings → Keys → Generate auth key* (reusable).
   Then open `http://<board-ip>/tailscale` and paste the key, or send `[ESP760]KEY=tskey-auth-...`.
   The board appears in your tailnet with a `100.x.y.z` address.
4. Wire the board's UART to the printer's serial port (TX → RX, RX → TX, GND → GND) and match the printer's baud rate.
5. Use the LAN IP or the Tailscale address as `printer_url` in PrintBridge's `config.json` and in the Cura plugin.

## Partition layout

| Name | Offset | Size |
|---|---|---|
| nvs | 0x9000 | 20 KB |
| otadata | 0xE000 | 8 KB |
| app0 | 0x10000 | 3 MB |
| app1 | 0x310000 | 3 MB |
| spiffs | 0x610000 | 9.9 MB |
| coredump | 0xFF0000 | 64 KB |

Built on ESP-IDF 5.1 / Arduino-ESP32 core.
