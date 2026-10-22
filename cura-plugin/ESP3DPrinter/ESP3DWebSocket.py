"""Minimal, listen-only websocket client for the ESP3D 3.x data socket (port 8282).

It never sends G-code: an extra "ok" from the printer would desynchronise ESP3D's
file streaming. It only collects what the printer prints (temperature reports etc.)
and keeps the latest temperatures for the Qt thread to read.
Standard library only, so it works inside Cura's bundled Python.
"""
import base64
import os
import re
import socket
import struct
import threading
import time

_T_RE = re.compile(r"T:\s*(-?[\d.]+)\s*/\s*(-?[\d.]+)")
_B_RE = re.compile(r"B:\s*(-?[\d.]+)\s*/\s*(-?[\d.]+)")


class _Kicked(Exception):
    pass


class ESP3DWebSocket:
    def __init__(self, host, port=8282, on_log=None):
        self.host = host
        self.port = int(port)
        self._on_log = on_log
        self._stop = threading.Event()
        self._thread = None
        self._sock = None
        self._lock = threading.Lock()
        self._temps = None          # dict hotend, hotend_target, bed, bed_target
        self._temps_time = 0.0
        self.connected = False
        self._my_id = None

    # ---------------------------------------------------------------- public
    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="ESP3DWebSocket", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        s = self._sock
        if s is not None:
            try:
                s.close()
            except OSError:
                pass

    def latestTemperatures(self, max_age=15.0):
        with self._lock:
            if self._temps and time.time() - self._temps_time <= max_age:
                return dict(self._temps)
        return None

    # -------------------------------------------------------------- internals
    def _log(self, msg):
        if self._on_log:
            try:
                self._on_log(msg)
            except Exception:
                pass

    def _run(self):
        delay = 2.0
        while not self._stop.is_set():
            try:
                self._connect()
                delay = 2.0
                self._loop()
            except _Kicked as e:
                # Someone else (web UI, PrintBridge) needs the socket: leave it to them for a while
                self._log("ESP3D websocket: %s" % e)
                delay = 60.0
            except Exception as e:  # network errors, server closed...
                if not self._stop.is_set():
                    self._log("ESP3D websocket: %s" % e)
            finally:
                self.connected = False
                if self._sock is not None:
                    try:
                        self._sock.close()
                    except OSError:
                        pass
                    self._sock = None
            self._stop.wait(delay)
            delay = min(delay * 2, 60.0)

    def _handshake(self, protocol):
        s = socket.create_connection((self.host, self.port), timeout=5)
        key = base64.b64encode(os.urandom(16)).decode()
        req = ("GET / HTTP/1.1\r\nHost: %s:%d\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
               "Sec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n" % (self.host, self.port, key))
        if protocol:
            req += "Sec-WebSocket-Protocol: %s\r\n" % protocol
        s.sendall((req + "\r\n").encode())
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = s.recv(1024)
            if not chunk:
                s.close()
                raise ConnectionError("handshake closed")
            data += chunk
            if len(data) > 8192:
                s.close()
                raise ConnectionError("handshake too long")
        head, rest = data.split(b"\r\n\r\n", 1)
        if b" 101" not in head.split(b"\r\n", 1)[0]:
            s.close()
            raise ConnectionError("handshake refused: %r" % head.split(b"\r\n", 1)[0])
        return s, rest

    def _connect(self):
        try:
            s, rest = self._handshake("esp3d-v1")
        except (ConnectionError, OSError):
            s, rest = self._handshake(None)
        s.settimeout(1.0)
        self._sock = s
        self._buf = rest
        self._line = ""
        self.connected = True
        self._my_id = None
        self._log("ESP3D websocket connected")

    def _recv_exact(self, n):
        while len(self._buf) < n:
            if self._stop.is_set():
                raise ConnectionError("stopped")
            try:
                chunk = self._sock.recv(4096)
            except socket.timeout:
                continue
            if not chunk:
                raise ConnectionError("closed by printer")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def _send_frame(self, opcode, payload=b""):
        mask = os.urandom(4)
        header = bytes([0x80 | opcode])
        n = len(payload)
        if n < 126:
            header += bytes([0x80 | n])
        elif n < 65536:
            header += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            header += bytes([0x80 | 127]) + struct.pack(">Q", n)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self._sock.sendall(header + mask + masked)

    def _loop(self):
        while not self._stop.is_set():
            b1, b2 = self._recv_exact(2)
            opcode = b1 & 0x0F
            length = b2 & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._recv_exact(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._recv_exact(8))[0]
            mask = self._recv_exact(4) if b2 & 0x80 else None
            payload = self._recv_exact(length) if length else b""
            if mask:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            if opcode == 0x8:
                raise ConnectionError("closed by printer")
            if opcode == 0x9:
                self._send_frame(0xA, payload)
                continue
            if opcode in (0x0, 0x1, 0x2):
                self._feed(payload.decode("utf-8", "ignore"), is_text=(opcode == 0x1))

    def _feed(self, text, is_text):
        if is_text:
            # ESP3D control messages: currentID:<n>, activeID:<n>, PING:..., etc.
            for msg in text.splitlines():
                if msg.startswith("currentID:"):
                    self._my_id = msg.split(":", 1)[1].strip()
                elif msg.startswith("activeID:"):
                    active = msg.split(":", 1)[1].strip()
                    if self._my_id is not None and active != self._my_id:
                        # another client took the socket (web UI, PrintBridge); back off
                        raise _Kicked("another client is using the printer socket")
            # Some firmwares also send serial output as text frames
        self._line += text
        while "\n" in self._line:
            line, self._line = self._line.split("\n", 1)
            self._parse_line(line.strip())
        if len(self._line) > 4096:
            self._line = ""

    def _parse_line(self, line):
        m = _T_RE.search(line)
        if not m:
            return
        temps = {"hotend": float(m.group(1)), "hotend_target": float(m.group(2))}
        b = _B_RE.search(line)
        if b:
            temps["bed"] = float(b.group(1))
            temps["bed_target"] = float(b.group(2))
        with self._lock:
            if self._temps and "bed" not in temps:
                temps["bed"] = self._temps.get("bed")
                temps["bed_target"] = self._temps.get("bed_target")
            self._temps = temps
            self._temps_time = time.time()
