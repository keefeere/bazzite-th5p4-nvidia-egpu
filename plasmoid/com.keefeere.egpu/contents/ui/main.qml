/*
 * SPDX-License-Identifier: GPL-2.0-or-later
 */

pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Layouts

import org.kde.kirigami as Kirigami
import org.kde.plasma.components as PlasmaComponents3
import org.kde.plasma.core as PlasmaCore
import org.kde.plasma.extras as PlasmaExtras
import org.kde.plasma.plasma5support as Plasma5Support
import org.kde.plasma.plasmoid

PlasmoidItem {
    id: root

    readonly property bool ukrainian: Qt.locale().name.toLowerCase().startsWith("uk")
    readonly property string statusCommand: "/etc/egpu-nvidia/egpu-tray-status.sh --language " + (ukrainian ? "uk" : "en")
    readonly property string detachCommand: "/usr/bin/systemctl start egpu-nvidia-detach.service"
    readonly property string attachCommand: "/usr/bin/systemctl start egpu-nvidia-hot-attach.service"

    property string egpuState: "unknown"

    property string stateTitle: localized("Checking eGPU…", "Перевіряємо eGPU…")
    property string stateDetail: ""
    property string confirmationAction: ""
    property string commandError: ""

    readonly property bool canDetach: egpuState === "ready"
    readonly property bool canAttach: egpuState === "safe" || egpuState === "reattach" || egpuState === "present"
    readonly property bool isBusy: egpuState === "detaching" || egpuState === "attaching" || egpuState === "initializing"

    Plasmoid.icon: (egpuState === "safe" || egpuState === "unplugged") ? "media-eject" :
        egpuState === "error" ? "data-error" :
        (egpuState === "stuck" || egpuState === "reboot") ? "dialog-warning" : "video-display"
    Plasmoid.status: (egpuState === "absent" || egpuState === "unplugged")
        ? PlasmaCore.Types.PassiveStatus : PlasmaCore.Types.ActiveStatus
    Plasmoid.busy: isBusy

    toolTipMainText: stateTitle
    toolTipSubText: stateDetail

    function localized(english, ukrainianText) {
        return ukrainian ? ukrainianText : english;
    }

    function refreshStatus() {
        if (!statusSource.connectedSources.includes(statusCommand)) {
            statusSource.connectSource(statusCommand);
        }
    }

    function applyStatus(output) {
        const line = output.trim().split("\n")[0];
        const fields = line.split("\t");
        if (fields.length < 3) {
            egpuState = "error";
            stateTitle = localized("Could not read eGPU status", "Не вдалося прочитати стан eGPU");
            stateDetail = line.length > 0 ? line : localized("Empty response from the status helper.", "Порожня відповідь status helper.");
            return;
        }

        egpuState = fields[0];
        stateTitle = fields[1];
        stateDetail = fields.slice(2).join(" ");
        if (egpuState !== "ready") {
            if (egpuState !== "safe" && egpuState !== "reattach" && egpuState !== "present") {
                confirmationAction = "";
            }
        }
        if (egpuState !== "error") {
            commandError = "";
        }
    }

    function beginDetach() {
        confirmationAction = "";
        commandError = "";
        egpuState = "detaching";
        stateTitle = localized("Detaching…", "Від’єднання…");
        stateDetail = localized(
            "The session will end. After signing in again, wait for permission to unplug the cable.",
            "Сеанс буде завершено; після входу дочекайся дозволу від’єднати кабель."
        );
        detachSource.connectSource(detachCommand);
    }

    function beginAttach() {
        confirmationAction = "";
        commandError = "";
        egpuState = "attaching";
        stateTitle = localized("Connecting eGPU…", "Підключення eGPU…");
        stateDetail = localized(
            "The session will end while NVIDIA is initialized safely at Gen3.",
            "Сеанс буде завершено, поки NVIDIA безпечно ініціалізується у Gen3."
        );
        detachSource.connectSource(attachCommand);
    }

    Plasma5Support.DataSource {
        id: statusSource
        engine: "executable"
        connectedSources: []

        onNewData: function(sourceName, data) {
            disconnectSource(sourceName);
            const stdout = data["stdout"] ?? "";
            const stderr = data["stderr"] ?? "";
            if (stdout.length > 0) {
                root.applyStatus(stdout);
            } else {
                root.egpuState = "error";
                root.stateTitle = root.localized("The status helper did not respond", "Status helper не відповів");
                root.stateDetail = stderr.length > 0 ? stderr.trim() : root.localized("Unknown error.", "Невідома помилка.");
            }
        }
    }

    Plasma5Support.DataSource {
        id: detachSource
        engine: "executable"
        connectedSources: []

        onNewData: function(sourceName, data) {
            disconnectSource(sourceName);
            const exitCode = data["exit code"] ?? data["exitCode"] ?? 0;
            const stderr = data["stderr"] ?? "";
            if (exitCode !== 0 || stderr.length > 0) {
                root.commandError = stderr.trim().length > 0 ? stderr.trim() :
                    root.localized("systemctl exited with code ", "systemctl завершився з кодом ") + exitCode;
            }
            root.refreshStatus();
        }
    }

    Timer {
        interval: 1500
        repeat: true
        running: true
        triggeredOnStart: true
        onTriggered: root.refreshStatus()
    }

    compactRepresentation: Item {
        Layout.minimumWidth: Kirigami.Units.iconSizes.small
        Layout.minimumHeight: Kirigami.Units.iconSizes.small

        Kirigami.Icon {
            anchors.fill: parent
            source: Plasmoid.icon
            opacity: root.egpuState === "absent" ? 0.55 : 1.0
        }

        MouseArea {
            anchors.fill: parent
            hoverEnabled: true
            onClicked: root.expanded = !root.expanded
        }
    }

    fullRepresentation: PlasmaExtras.Representation {
        Layout.minimumWidth: Kirigami.Units.gridUnit * 19
        Layout.minimumHeight: contentColumn.implicitHeight + Kirigami.Units.gridUnit * 2
        collapseMarginsHint: true

        contentItem: ColumnLayout {
            id: contentColumn
            spacing: Kirigami.Units.largeSpacing

            Item {
                Layout.fillWidth: true
                implicitHeight: Kirigami.Units.gridUnit * 4

                Kirigami.Icon {
                    anchors.centerIn: parent
                    width: Kirigami.Units.iconSizes.huge
                    height: width
                    source: Plasmoid.icon
                }
            }

            PlasmaComponents3.Label {
                Layout.fillWidth: true
                horizontalAlignment: Text.AlignHCenter
                text: root.stateTitle
                font.bold: true
                font.pointSize: Kirigami.Theme.defaultFont.pointSize * 1.15
                wrapMode: Text.Wrap
            }

            PlasmaComponents3.Label {
                Layout.fillWidth: true
                horizontalAlignment: Text.AlignHCenter
                text: root.stateDetail
                opacity: 0.75
                wrapMode: Text.Wrap
            }

            PlasmaComponents3.Label {
                Layout.fillWidth: true
                visible: root.commandError.length > 0
                text: root.commandError
                color: Kirigami.Theme.negativeTextColor
                wrapMode: Text.Wrap
            }

            PlasmaComponents3.Label {
                Layout.fillWidth: true
                visible: root.confirmationAction.length > 0
                text: root.confirmationAction === "detach"
                    ? root.localized(
                        "All applications using NVIDIA will be closed and the current graphical session will end.",
                        "Усі програми на NVIDIA буде закрито, а поточний графічний сеанс завершено."
                    )
                    : root.localized(
                        "The current graphical session will end so NVIDIA can become the primary GPU.",
                        "Поточний графічний сеанс буде завершено для запуску NVIDIA як основної GPU."
                    )
                color: Kirigami.Theme.neutralTextColor
                wrapMode: Text.Wrap
            }

            RowLayout {
                Layout.fillWidth: true
                spacing: Kirigami.Units.smallSpacing

                PlasmaComponents3.Button {
                    Layout.fillWidth: true
                    visible: root.confirmationAction.length === 0 && root.canDetach
                    enabled: root.canDetach
                    icon.name: "media-eject"
                    text: root.localized("Safely detach", "Безпечно від’єднати")
                    onClicked: root.confirmationAction = "detach"
                }

                PlasmaComponents3.Button {
                    Layout.fillWidth: true
                    visible: root.confirmationAction.length === 0 && root.canAttach
                    enabled: root.canAttach
                    icon.name: "network-connect"
                    text: root.localized("Connect eGPU", "Підключити eGPU")
                    onClicked: root.confirmationAction = "attach"
                }

                PlasmaComponents3.Button {
                    Layout.fillWidth: true
                    visible: root.confirmationAction.length > 0
                    icon.name: "dialog-ok"
                    text: root.confirmationAction === "detach"
                        ? root.localized("Yes, detach", "Так, від’єднати")
                        : root.localized("Yes, connect", "Так, підключити")
                    onClicked: root.confirmationAction === "detach" ? root.beginDetach() : root.beginAttach()
                }

                PlasmaComponents3.Button {
                    visible: root.confirmationAction.length > 0
                    icon.name: "dialog-cancel"
                    text: root.localized("Cancel", "Скасувати")
                    onClicked: root.confirmationAction = ""
                }
            }
        }
    }
}
