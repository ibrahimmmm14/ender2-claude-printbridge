"""
esp3d.py - Talk to an ESP3D 3.x board (HTTP API + data websocket).
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from typing import Any, Dict, List, Optional

import requests

try:
    import websocket  # websocket-client
except ImportError:  # pragma: no cover
    websocket = None


class ESP3DError(RuntimeError):
    pass


class ESP3D:
    def __init__(self, url: str, ws_port: int = 8282, user: str = "", password: str = "", timeout: float = 10.0):
        if not url.startswith("http"):
            url = "http://" + url
        self.url = url.rstrip("/")
        self.host = re.sub(r"^https?://", "", self.url).split("/")[0].split(":")[0]
        self.ws_port = ws_port
        self.timeout = timeout
        self.s = requests.Session()
        self.user, self.password = user, password
        if password:
            self.login()

    # ------------------------------------------------------------------ http
    def login(self):
        r = self.s.get(self.url + "/login", params={"USER": self.user or "admin", "PASSWORD": self.password,
                                                     "SUBMIT": "yes"}, timeout=self.timeout)
        if r.status_code == 401:
            raise ESP3DError("ESP3D login failed (wrong password)")

    def _get(self, endpoint: str, params: Dict[str, Any]) -> requests.Response:
        try:
            r = self.s.get(self.url + endpoint, params=params, timeout=self.timeout)
        except requests.ConnectionError:
            raise ESP3DError("Cannot reach the printer at %s (is the ESP32 on, and is the address / Tailscale "
                             "connection right?)" % self.url)
        if r.status_code == 401 and self.password:
            self.login()
            r = self.s.get(self.url + endpoint, params=params, timeout=self.timeout)
        if r.status_code == 401:
            raise ESP3DError("ESP3D needs a password (set esp3d_password in config.json)")
        return r

    def command(self, cmd: str) -> str:
        """Send an ESP3D [ESPxxx] command or a G-code line. Returns the HTTP answer text."""
        return self._get("/command", {"cmd": cmd}).text

    def info(self) -> Dict[str, Any]:
        txt = self.command("[ESP420]json")
        try:
            data = json.loads(txt)
            items = data.get("data", data)
            if isinstance(items, list):
                return {d.get("id"): d.get("value") for d in items if isinstance(d, dict)}
            return items
        except ValueError:
            return {"raw": txt}

    def list_files(self, path: str = "/") -> Dict[str, Any]:
        r = self._get("/files", {"path": path})
        try:
            return r.json()
        except ValueError:
            raise ESP3DError("Unexpected answer listing files: %s" % r.text[:200])

    def delete_file(self, name: str, path: str = "/") -> Dict[str, Any]:
        r = self._get("/files", {"path": path, "action": "delete", "filename": name})
        try:
            return r.json()
        except ValueError:
            return {"raw": r.text}

    def upload(self, local_path: str, remote_name: Optional[str] = None, path: str = "/",
               progress_cb=None) -> Dict[str, Any]:
        name = remote_name or os.path.basename(local_path)
        name = re.sub(r"[^A-Za-z0-9._-]", "_", name)
        if len(name) > 31:  # LittleFS name limit
            base, ext = os.path.splitext(name)
            name = base[: 31 - len(ext)] + ext
        size = os.path.getsize(local_path)
        with open(local_path, "rb") as f:
            files = {"myfiles": (name, f, "application/octet-stream")}
            data = {"path": path, name + "S": str(size)}
            r = self.s.post(self.url + "/files", data=data, files=files, timeout=max(60, size / 20000))
        if r.status_code == 401:
            raise ESP3DError("ESP3D needs a password")
        try:
            res = r.json()
        except ValueError:
            res = {"raw": r.text[:300]}
        if r.status_code >= 400 or str(res.get("status", "ok")).lower().startswith("error"):
            raise ESP3DError("Upload failed (%s): %s" % (r.status_code, res))
        res["uploaded_as"] = path.rstrip("/") + "/" + name
        return res

    # --------------------------------------------------------- streaming
    def start_print(self, remote_file: str) -> str:
        if not remote_file.startswith("/"):
            remote_file = "/" + remote_file
        ans = self.command("[ESP700]" + remote_file + " json")
        try:
            data = json.loads(ans)
            if data.get("status") != "ok":
                raise ESP3DError("Cannot start print: %s" % data.get("data"))
            return str(data.get("data"))
        except ValueError:
            if "error" in ans.lower() or "progress" in ans.lower():
                raise ESP3DError(ans)
            return ans

    def stream_status(self) -> Dict[str, Any]:
        ans = self.command("[ESP701]json")
        try:
            data = json.loads(ans)
            d = data.get("data", data)
            if isinstance(d, str):
                try:
                    d = json.loads(d)
                except ValueError:
                    d = {"status": d}
            if isinstance(d, dict) and d.get("total") and d.get("processed"):
                try:
                    d["percent"] = round(100.0 * float(d["processed"]) / float(d["total"]), 1)
                except (ValueError, ZeroDivisionError):
                    pass
            return d
        except ValueError:
            return {"status": ans.strip()}

    def stream_action(self, action: str) -> str:
        return self.command("[ESP701]action=" + action.upper())

    # --------------------------------------------------------- printer I/O
    def query(self, gcode: str, wait_s: float = 3.0, until: str = "ok") -> List[str]:
        """Send G-code through the data websocket and collect the printer's answer lines."""
        if websocket is None:
            raise ESP3DError("Missing package websocket-client")
        lines: List[str] = []
        url = "ws://%s:%d/" % (self.host, self.ws_port)
        try:
            ws = websocket.create_connection(url, timeout=wait_s, subprotocols=["esp3d-v1"])
        except websocket.WebSocketBadStatusException:
            raise
        except (websocket.WebSocketException, OSError) as e:
            if isinstance(e, ConnectionRefusedError):
                raise ESP3DError("Printer data websocket (port %d) refused the connection" % self.ws_port)
            ws = websocket.create_connection(url, timeout=wait_s)
        try:
            buf = ""
            if gcode.strip():
                ws.send(gcode.strip() + "\n")
            end = time.time() + wait_s
            while time.time() < end:
                try:
                    msg = ws.recv()
                except Exception:
                    break
                if isinstance(msg, bytes):
                    msg = msg.decode("utf-8", "ignore")
                buf += msg
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    line = line.strip()
                    if line:
                        lines.append(line)
                if until and any(l.startswith(until) for l in lines):
                    break
            if buf.strip():
                lines.append(buf.strip())
        finally:
            ws.close()
        return lines

    def is_printing(self) -> bool:
        try:
            return str(self.stream_status().get("status", "")).lower() in ("processing", "pause")
        except Exception:
            return False

    def temperatures(self, passive: bool = False) -> Dict[str, Any]:
        """M105 report. passive=True only listens (never sends anything) - used while a print streams,
        because an extra 'ok' from the printer would desynchronise ESP3D's streaming."""
        lines = self.query("" if passive else "M105", wait_s=4.0, until="" if passive else "ok")
        for line in reversed(lines):
            m = re.search(r"T:\s*([\d.]+)\s*/\s*([\d.]+)", line)
            if m:
                out = {"hotend": float(m.group(1)), "hotend_target": float(m.group(2))}
                b = re.search(r"B:\s*([\d.]+)\s*/\s*([\d.]+)", line)
                if b:
                    out.update({"bed": float(b.group(1)), "bed_target": float(b.group(2))})
                return out
        if passive:
            return {"note": "temperatures are not polled while printing (it would disturb the stream)"}
        return {"error": "no temperature report", "raw": lines[-5:]}

    def send_gcode(self, gcode: str) -> str:
        return self.command(gcode)
