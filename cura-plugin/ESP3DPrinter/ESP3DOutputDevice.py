"""Cura output device for a Marlin printer behind an ESP3D 3.x board.

- "Print via Wi-Fi" uploads the sliced G-code to the ESP3D flash (/files),
  then asks before starting the print ([ESP700]).
- Monitor tab: stream progress from [ESP701]json (an ESP3D command, safe while printing)
  and temperatures heard passively on the data websocket.
- Manual controls (temperatures, jog, home) are only sent while nothing is printing,
  because an extra "ok" from the printer would break ESP3D's file streaming.
"""
import io
import json
import os
import re
import time
from urllib.parse import quote

from PyQt6.QtNetwork import QNetworkReply, QNetworkRequest

from UM.Logger import Logger
from UM.Message import Message
from UM.PluginRegistry import PluginRegistry
from UM.i18n import i18nCatalog

from cura.CuraApplication import CuraApplication
from cura.PrinterOutput.NetworkedPrinterOutputDevice import NetworkedPrinterOutputDevice
from cura.PrinterOutput.PrinterOutputDevice import ConnectionState
from cura.PrinterOutput.Models.PrinterOutputModel import PrinterOutputModel
from cura.PrinterOutput.Models.PrintJobOutputModel import PrintJobOutputModel
from cura.PrinterOutput.GenericOutputController import GenericOutputController

from .ESP3DWebSocket import ESP3DWebSocket

catalog = i18nCatalog("cura")


def _safe_name(name):
    name = re.sub(r"[^A-Za-z0-9._-]", "_", name)
    if not name.lower().endswith(".gcode"):
        name += ".gcode"
    if len(name) > 31:  # ESP3D LittleFS name limit
        base, ext = os.path.splitext(name)
        name = base[: 31 - len(ext)] + ext
    return name


class ESP3DOutputDevice(NetworkedPrinterOutputDevice):
    def __init__(self, device_id, address, ws_port=8282, password="", auto_temp_report=True, parent=None):
        super().__init__(device_id=device_id, address=address, properties={}, parent=parent)
        self._address = address
        self._ws_port = ws_port
        self._password = password
        self._auto_temp_report = auto_temp_report
        self._name = "ESP3D (%s)" % address

        self.setPriority(5)
        self.setName(self._name)
        self.setShortDescription(catalog.i18nc("@action:button", "Print via Wi-Fi"))
        self.setDescription(catalog.i18nc("@properties:tooltip", "Send to the printer over Wi-Fi (ESP3D)"))
        self.setIconName("print")
        self.setConnectionText(catalog.i18nc("@info:status", "Connecting to ESP3D at %s") % address)
        self._monitor_view_qml_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "MonitorItem.qml")

        self._controller = _ESP3DController(self)
        self._ws = ESP3DWebSocket(address, ws_port, on_log=lambda m: Logger.log("d", m))

        self._stream = {"status": "unknown"}
        self._job_times = {}          # remote file name -> estimated seconds (from Cura)
        self._upload_message = None
        self._uploading = False
        self._logged_in = False
        self._temp_report_sent = False
        self._poll_busy = False
        self._last_poll = 0.0

    # ---------------------------------------------------------- connection
    def connect(self):
        super().connect()
        self.setConnectionState(ConnectionState.Connecting)
        if not self._printers:
            printer = PrinterOutputModel(output_controller=self._controller, number_of_extruders=1)
            printer.updateName("Ender-2 (ESP3D)")
            printer.updateKey(self._id)
            printer.updateState("idle")
            self._printers = [printer]
            self.printersChanged.emit()
        if self._password and not self._logged_in:
            self.get("/login?USER=admin&PASSWORD=%s&SUBMIT=yes" % quote(self._password), self._onLogin)
        self._ws.start()
        self._poll()

    def close(self):
        self._ws.stop()
        super().close()

    def _onLogin(self, reply):
        status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
        self._logged_in = status != 401
        if not self._logged_in:
            Message(catalog.i18nc("@info", "ESP3D login failed. Check the password in Extensions > ESP3D Wi-Fi."),
                    title="ESP3D", message_type=Message.MessageType.ERROR).show()

    def _command_url(self, cmd):
        return "/command?cmd=" + quote(cmd, safe="")

    def _update(self):
        try:
            super()._update()
        except Exception:
            pass
        if time.time() - self._last_poll >= 2.5:
            self._poll()
        self._applyTemperatures()

    # ------------------------------------------------------------- polling
    def _poll(self):
        if self._uploading:
            return
        if self._poll_busy:
            # Cura never calls back when the board does not answer at all
            if time.time() - self._last_poll < 10:
                return
            self.setConnectionState(ConnectionState.Closed)
            self.setConnectionText(catalog.i18nc("@info:status", "Cannot reach ESP3D at %s") % self._address)
        self._poll_busy = True
        self._last_poll = time.time()
        self.get(self._command_url("[ESP701]json"), self._onStreamStatus)

    def _onStreamStatus(self, reply):
        self._poll_busy = False
        if reply.error() != QNetworkReply.NetworkError.NoError:
            self.setConnectionState(ConnectionState.Closed)
            self.setConnectionText(catalog.i18nc("@info:status", "Cannot reach ESP3D at %s") % self._address)
            return
        raw = bytes(reply.readAll()).decode("utf-8", "ignore")
        self._stream = self._parseStream(raw)
        self.setConnectionState(ConnectionState.Connected)
        self.setConnectionText(catalog.i18nc("@info:status", "Connected to ESP3D at %s") % self._address)
        self._setAcceptsCommands(True)
        self._applyStream()
        self._maybeEnableTemperatureReport()

    @staticmethod
    def _parseStream(raw):
        try:
            data = json.loads(raw)
        except ValueError:
            return {"status": raw.strip()}
        d = data.get("data", data) if isinstance(data, dict) else data
        if isinstance(d, str):
            try:
                d = json.loads(d)
            except ValueError:
                d = {"status": d}
        if not isinstance(d, dict):
            return {"status": str(d)}
        try:
            if d.get("total") and d.get("processed") is not None:
                d["percent"] = 100.0 * float(d["processed"]) / float(d["total"])
        except (ValueError, ZeroDivisionError):
            pass
        return d

    def isPrinting(self):
        return str(self._stream.get("status", "")).lower() in ("processing", "pause", "paused")

    def _applyStream(self):
        if not self._printers:
            return
        printer = self._printers[0]
        status = str(self._stream.get("status", "")).lower()
        if status in ("processing", "pause", "paused"):
            state = "printing" if status == "processing" else "paused"
            printer.updateState(state)
            name = os.path.basename(str(self._stream.get("name", "")))
            job = printer.activePrintJob
            if job is None or job.name != name:
                job = PrintJobOutputModel(output_controller=self._controller, key=name, name=name)
                job.updateAssignedPrinter(printer)
                printer.updateActivePrintJob(job)
            job.updateState(state)
            pct = float(self._stream.get("percent", 0.0) or 0.0)
            total = self._job_times.get(name)
            if total:
                job.updateTimeTotal(int(total))
                job.updateTimeElapsed(int(total * pct / 100.0))
            else:  # unknown estimate: show progress only
                job.updateTimeTotal(100)
                job.updateTimeElapsed(int(pct))
        else:
            if printer.activePrintJob is not None:
                printer.activePrintJob.updateState("wait_cleanup")
                printer.updateActivePrintJob(None)
            printer.updateState("idle")

    def _maybeEnableTemperatureReport(self):
        # Ask Marlin to push temperatures every 3 s (M155), once, and only while idle.
        if self._auto_temp_report and not self._temp_report_sent and not self.isPrinting():
            self._temp_report_sent = True
            self.get(self._command_url("M155 S3"), lambda r: None)

    def _applyTemperatures(self):
        if not self._printers:
            return
        t = self._ws.latestTemperatures()
        if not t:
            return
        printer = self._printers[0]
        ext = printer.extruders[0]
        ext.updateHotendTemperature(t["hotend"])
        ext.updateTargetHotendTemperature(t["hotend_target"])
        if t.get("bed") is not None:
            printer.updateBedTemperature(t["bed"])
            printer.updateTargetBedTemperature(t["bed_target"])

    # ------------------------------------------------------------ commands
    def sendCommand(self, command):
        if self.isPrinting():
            Message(catalog.i18nc("@info", "Manual commands are disabled while a print is running "
                                           "(they would interrupt the Wi-Fi stream)."),
                    title=catalog.i18nc("@info:title", "ESP3D"),
                    message_type=Message.MessageType.WARNING).show()
            return
        self.get(self._command_url(command), lambda r: None)

    def pausePrint(self):
        self.get(self._command_url("[ESP701]action=PAUSE"), lambda r: self._poll())

    def resumePrint(self):
        self.get(self._command_url("[ESP701]action=RESUME"), lambda r: self._poll())

    def cancelPrint(self):
        def heaters_off(_reply):
            # stream is gone now, so plain G-code is safe
            for cmd in ("M104 S0", "M140 S0", "M106 S0"):
                self.get(self._command_url(cmd), lambda r: None)
            self._poll()
        self.get(self._command_url("[ESP701]action=ABORT"), heaters_off)

    # -------------------------------------------------------------- upload
    def requestWrite(self, nodes, file_name=None, limit_mimetypes=False, file_handler=None,
                     filter_by_machine=False, **kwargs):
        if self._uploading:
            Message(catalog.i18nc("@info", "An upload is already in progress."),
                    title="ESP3D").show()
            return
        if self.isPrinting():
            Message(catalog.i18nc("@info", "The printer is printing. Wait until it finishes before uploading."),
                    title="ESP3D", message_type=Message.MessageType.WARNING).show()
            return

        writer = PluginRegistry.getInstance().getPluginObject("GCodeWriter")
        stream = io.StringIO()
        if not writer.write(stream, None):
            Message(catalog.i18nc("@info", "Could not get the G-code. Slice the model first."),
                    title="ESP3D", message_type=Message.MessageType.ERROR).show()
            return
        gcode = stream.getvalue().encode("utf-8")

        app = CuraApplication.getInstance()
        job_name = app.getPrintInformation().jobName or "print"
        remote = _safe_name(job_name)
        m = re.search(r"^;TIME:(\d+)", stream.getvalue()[:2000], re.M)
        if m:
            self._job_times[remote] = int(m.group(1))

        self._uploading = True
        self._remote_name = remote
        self._upload_message = Message(catalog.i18nc("@info:status", "Uploading %s to the printer...") % remote,
                                       title="ESP3D", progress=0, lifetime=0, dismissable=False)
        self._upload_message.show()

        parts = [
            self.createFormPart('name="path"', b"/"),
            self.createFormPart('name="%sS"' % remote, str(len(gcode)).encode()),
            self.createFormPart('name="myfiles"; filename="%s"' % remote, gcode, "application/octet-stream"),
        ]
        self.postFormWithParts("/files", parts, on_finished=self._onUploadFinished,
                               on_progress=self._onUploadProgress)

    def _onUploadProgress(self, sent, total):
        if self._upload_message and total > 0:
            self._upload_message.setProgress(int(100 * sent / total))

    def _onUploadFinished(self, reply):
        self._uploading = False
        if self._upload_message:
            self._upload_message.hide()
            self._upload_message = None
        body = bytes(reply.readAll()).decode("utf-8", "ignore")
        failed = reply.error() != QNetworkReply.NetworkError.NoError or '"error' in body.lower()
        if failed:
            Logger.log("e", "ESP3D upload failed: %s %s", reply.errorString(), body[:300])
            Message(catalog.i18nc("@info", "Upload failed: %s") % (reply.errorString() or body[:200]),
                    title="ESP3D", message_type=Message.MessageType.ERROR).show()
            return
        msg = Message(catalog.i18nc("@info", "%s is on the printer. Start printing now?") % self._remote_name,
                      title="ESP3D", lifetime=0)
        msg.addAction("start", catalog.i18nc("@action:button", "Start print"), "", "")
        msg.addAction("later", catalog.i18nc("@action:button", "Not now"), "", "",
                      button_style=Message.ActionButtonStyle.SECONDARY)
        msg.actionTriggered.connect(self._onUploadedAction)
        msg.show()

    def _onUploadedAction(self, message, action):
        message.hide()
        if action == "start":
            self.startPrint(self._remote_name)

    def startPrint(self, remote_name):
        def started(reply):
            body = bytes(reply.readAll()).decode("utf-8", "ignore")
            if reply.error() != QNetworkReply.NetworkError.NoError or "error" in body.lower():
                Message(catalog.i18nc("@info", "Could not start the print: %s") % body[:200],
                        title="ESP3D", message_type=Message.MessageType.ERROR).show()
            else:
                Message(catalog.i18nc("@info", "Printing %s. Follow it in the Monitor tab.") % remote_name,
                        title="ESP3D").show()
                CuraApplication.getInstance().getController().setActiveStage("MonitorStage")
            self._poll()
        self.get(self._command_url("[ESP700]/%s json" % remote_name), started)


class _ESP3DController(GenericOutputController):
    """Same as Cura's USB controller, but G-code goes through ESP3D HTTP."""

    def __init__(self, device):
        super().__init__(device)
        self.can_pre_heat_hotends = True
        self.can_pre_heat_bed = True
        self.can_control_manually = True
        self.can_send_raw_gcode = True
        self.can_update_firmware = False
