import json
import os
from urllib.parse import quote

from PyQt6.QtCore import QObject, QUrl, pyqtProperty, pyqtSignal, pyqtSlot
from PyQt6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest

from UM.Extension import Extension
from cura.CuraApplication import CuraApplication

from . import ESP3DOutputDevicePlugin as P


class ESP3DSettingsBackend(QObject):
    changed = pyqtSignal()

    def __init__(self, plugin, parent=None):
        super().__init__(parent)
        self._plugin = plugin
        self._status = ""
        self._nam = None

    def _prefs(self):
        return CuraApplication.getInstance().getPreferences()

    @pyqtProperty(str, notify=changed)
    def address(self):
        return self._prefs().getValue(P.PREF_ADDRESS) or ""

    @pyqtProperty(str, notify=changed)
    def wsPort(self):
        return str(self._prefs().getValue(P.PREF_WS_PORT) or 8282)

    @pyqtProperty(str, notify=changed)
    def password(self):
        return self._prefs().getValue(P.PREF_PASSWORD) or ""

    @pyqtProperty(bool, notify=changed)
    def autoTempReport(self):
        return bool(self._prefs().getValue(P.PREF_TEMP_REPORT))

    @pyqtProperty(str, notify=changed)
    def status(self):
        return self._status

    def _setStatus(self, text):
        self._status = text
        self.changed.emit()

    @pyqtSlot(str, str, str, bool)
    def save(self, address, ws_port, password, auto_temp_report):
        prefs = self._prefs()
        prefs.setValue(P.PREF_ADDRESS, address.strip())
        try:
            prefs.setValue(P.PREF_WS_PORT, int(ws_port))
        except ValueError:
            prefs.setValue(P.PREF_WS_PORT, 8282)
        prefs.setValue(P.PREF_PASSWORD, password)
        prefs.setValue(P.PREF_TEMP_REPORT, bool(auto_temp_report))
        self._plugin.reconnect()
        self.changed.emit()

    @pyqtSlot(str)
    def testConnection(self, address):
        address = address.strip()
        if not address:
            self._setStatus("Enter an address first.")
            return
        self._setStatus("Testing %s ..." % address)
        if self._nam is None:
            self._nam = QNetworkAccessManager()
        req = QNetworkRequest(QUrl("http://%s/command?cmd=%s" % (address, quote("[ESP420]json", safe=""))))
        req.setTransferTimeout(5000)
        reply = self._nam.get(req)
        reply.finished.connect(lambda: self._onTest(reply))

    def _onTest(self, reply):
        if reply.error() != QNetworkReply.NetworkError.NoError:
            self._setStatus("No answer: %s" % reply.errorString())
            reply.deleteLater()
            return
        text = bytes(reply.readAll()).decode("utf-8", "ignore")
        reply.deleteLater()
        info = {}
        try:
            data = json.loads(text)
            items = data.get("data", data)
            if isinstance(items, list):
                info = {d.get("id"): d.get("value") for d in items if isinstance(d, dict)}
        except ValueError:
            pass
        if info:
            self._setStatus("Connected: ESP3D %s, Wi-Fi %s (%s)" % (info.get("FW ver", "?"), info.get("SSID", "?"),
                                                                    info.get("signal", "?")))
        else:
            self._setStatus("The address answered, but it does not look like ESP3D: %s" % text[:80])


class ESP3DSettingsExtension(Extension):
    def __init__(self, plugin):
        super().__init__()
        self._plugin = plugin
        self._backend = None
        self._dialog = None
        self.setMenuName("ESP3D Wi-Fi")
        self.addMenuItem("Printer settings...", self.showSettings)

    def showSettings(self):
        if self._backend is None:
            self._backend = ESP3DSettingsBackend(self._plugin)
        if self._dialog is None:
            path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ESP3DSettings.qml")
            self._dialog = CuraApplication.getInstance().createQmlComponent(path, {"manager": self._backend})
        if self._dialog:
            self._backend.changed.emit()
            self._dialog.show()
