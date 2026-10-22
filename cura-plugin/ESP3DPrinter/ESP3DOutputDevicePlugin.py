from UM.Logger import Logger
from UM.OutputDevice.OutputDevicePlugin import OutputDevicePlugin
from cura.CuraApplication import CuraApplication

from .ESP3DOutputDevice import ESP3DOutputDevice

PREF_ADDRESS = "esp3d/address"
PREF_WS_PORT = "esp3d/ws_port"
PREF_PASSWORD = "esp3d/password"
PREF_TEMP_REPORT = "esp3d/auto_temp_report"
DEVICE_ID = "esp3d_wifi_printer"


class ESP3DOutputDevicePlugin(OutputDevicePlugin):
    def __init__(self):
        super().__init__()
        prefs = CuraApplication.getInstance().getPreferences()
        prefs.addPreference(PREF_ADDRESS, "192.168.0.10")
        prefs.addPreference(PREF_WS_PORT, 8282)
        prefs.addPreference(PREF_PASSWORD, "")
        prefs.addPreference(PREF_TEMP_REPORT, True)
        self._device = None

    def start(self):
        self._createDevice()

    def stop(self):
        self._removeDevice()

    def reconnect(self):
        self._removeDevice()
        self._createDevice()

    def _createDevice(self):
        prefs = CuraApplication.getInstance().getPreferences()
        address = (prefs.getValue(PREF_ADDRESS) or "").strip()
        if not address:
            return
        try:
            ws_port = int(prefs.getValue(PREF_WS_PORT) or 8282)
        except (TypeError, ValueError):
            ws_port = 8282
        self._device = ESP3DOutputDevice(DEVICE_ID, address, ws_port=ws_port,
                                         password=prefs.getValue(PREF_PASSWORD) or "",
                                         auto_temp_report=bool(prefs.getValue(PREF_TEMP_REPORT)))
        self.getOutputDeviceManager().addOutputDevice(self._device)
        self._device.connect()
        Logger.log("i", "ESP3D printer added at %s", address)

    def _removeDevice(self):
        if self._device is not None:
            try:
                self._device.close()
            finally:
                self.getOutputDeviceManager().removeOutputDevice(self._device.getId())
                self._device = None
