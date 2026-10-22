import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.3

import UM 1.5 as UM
import Cura 1.0 as Cura

UM.Dialog
{
    id: dialog
    title: "ESP3D Wi-Fi printer"
    minimumWidth: UM.Theme.getSize("default_margin").width * 40
    minimumHeight: UM.Theme.getSize("default_margin").height * 22
    width: minimumWidth
    height: minimumHeight

    ColumnLayout
    {
        anchors.fill: parent
        spacing: UM.Theme.getSize("default_margin").height

        UM.Label
        {
            Layout.fillWidth: true
            wrapMode: Text.WordWrap
            text: "Printer address on your network (or its Tailscale address when you are away from home)."
        }

        UM.Label { text: "Address (IP or host name)" }
        UM.TextField
        {
            id: addressField
            Layout.fillWidth: true
            text: manager.address
            placeholderText: "192.168.0.10"
        }

        UM.Label { text: "Websocket port" }
        UM.TextField
        {
            id: portField
            Layout.fillWidth: true
            text: manager.wsPort
            validator: IntValidator { bottom: 1; top: 65535 }
        }

        UM.Label { text: "ESP3D password (leave empty if none)" }
        UM.TextField
        {
            id: passwordField
            Layout.fillWidth: true
            text: manager.password
            echoMode: TextInput.Password
        }

        UM.CheckBox
        {
            id: tempReport
            checked: manager.autoTempReport
            text: "Live temperatures (asks Marlin to report every 3 s)"
        }

        UM.Label
        {
            Layout.fillWidth: true
            wrapMode: Text.WordWrap
            color: UM.Theme.getColor("text_inactive")
            text: manager.status
        }

        Item { Layout.fillHeight: true }
    }

    rightButtons: [
        Cura.SecondaryButton
        {
            text: "Test connection"
            onClicked: manager.testConnection(addressField.text)
        },
        Cura.PrimaryButton
        {
            text: "Save"
            onClicked:
            {
                manager.save(addressField.text, portField.text, passwordField.text, tempReport.checked)
                dialog.close()
            }
        }
    ]
}
