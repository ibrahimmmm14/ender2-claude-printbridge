from . import ESP3DOutputDevicePlugin
from . import ESP3DSettings


def getMetaData():
    return {}


def register(app):
    plugin = ESP3DOutputDevicePlugin.ESP3DOutputDevicePlugin()
    return {
        "output_device": plugin,
        "extension": ESP3DSettings.ESP3DSettingsExtension(plugin),
    }
