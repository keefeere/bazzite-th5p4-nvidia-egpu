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

    // GPU service-roles profiles (Cardwire). Reading the current profile needs no privilege;
    // applying one starts an exact polkit-whitelisted system unit.
    readonly property string profileReadCommand: "/usr/bin/busctl --system get-property org.opengamingcollective.cardwire /org/opengamingcollective/cardwire org.opengamingcollective.cardwire.ServiceRoles CurrentProfile"
    readonly property var profiles: [
        { id: "gaming-nvidia", icon: "applications-games", label: localized("Gaming", "Ігри"),
          hint: localized("NVIDIA renders and drives displays; every app may use it.", "NVIDIA рендерить і виводить зображення; усі програми можуть її використовувати.") },
        { id: "work-nvidia", icon: "preferences-desktop-display", label: localized("Work: NVIDIA displays", "Робота: дисплеї NVIDIA"),
          hint: localized("AMD renders; NVIDIA only drives displays and runs allowed compute (llama).", "AMD рендерить; NVIDIA лише виводить зображення та виконує дозволені обчислення (llama).") },
        { id: "work-igpu", icon: "cpu", label: localized("Work: iGPU displays", "Робота: дисплеї iGPU"),
          hint: localized("AMD renders and displays; NVIDIA only runs allowed compute.", "AMD рендерить і виводить зображення; NVIDIA лише для дозволених обчислень.") }
    ]
    // The work profiles take NVIDIA away from every NEW program; that is only safe when the
    // compositor renders on the AMD GPU (otherwise new windows, even this widget, cannot be drawn).
    readonly property string kwinVendorCommand: "/usr/bin/sh -c 'd=$(/usr/bin/systemctl --user show-environment | /usr/bin/sed -n s/^KWIN_DRM_DEVICES=//p | /usr/bin/cut -d: -f1); /usr/bin/cat /sys/class/drm/$(/usr/bin/basename \"$d\")/device/vendor'"
    readonly property string desiredCommand: "/usr/bin/cat /var/lib/egpu-nvidia-service-roles/desired-profile"
    readonly property string logoutCommand: "/usr/bin/busctl --user call org.kde.LogoutPrompt /LogoutPrompt org.kde.LogoutPrompt promptLogout"
    readonly property string lastErrorCommand: "/usr/bin/cat /run/egpu-service-roles-last-error"
    property bool kwinOnAmd: false
    property string desiredProfile: ""
    // The choice the user made (remembered across restarts); falls back to what is active now.
    readonly property string chosenProfile: desiredProfile.length > 0 ? desiredProfile : currentProfile
    // KWin picks its render GPU at session start, so a work profile (AMD renders) and Gaming
    // (NVIDIA renders) each need ONE session restart after a switch.
    readonly property bool reloginPending: chosenProfile.length > 0 &&
        ((chosenProfile !== "gaming-nvidia" && !kwinOnAmd) || (chosenProfile === "gaming-nvidia" && kwinOnAmd))
    property string currentProfile: ""
    readonly property string chosenProfileLabel: {
        for (let i = 0; i < profiles.length; ++i) {
            if (profiles[i].id === chosenProfile) {
                return profiles[i].label;
            }
        }
        return chosenProfile;
    }
    property string pendingProfile: ""
    property string profileError: ""

    property string egpuState: "unknown"

    property string stateTitle: localized("Checking eGPU…", "Перевіряємо eGPU…")
    property string stateDetail: ""
    property string confirmationAction: ""
    property string commandError: ""

    readonly property bool canDetach: egpuState === "ready"
    readonly property bool canAttach: egpuState === "safe" || egpuState === "reattach" || egpuState === "present" || egpuState === "hotplug"
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
            if (egpuState !== "safe" && egpuState !== "reattach" && egpuState !== "present" && egpuState !== "hotplug") {
                confirmationAction = "";
            }
        }
        if (egpuState !== "error") {
            commandError = "";
        }
    }

    function refreshProfile() {
        if (!profileSource.connectedSources.includes(profileReadCommand)) {
            profileSource.connectSource(profileReadCommand);
        }
    }

    function refreshKwin() {
        if (!kwinSource.connectedSources.includes(kwinVendorCommand)) {
            kwinSource.connectSource(kwinVendorCommand);
        }
    }

    function refreshDesired() {
        if (!desiredSource.connectedSources.includes(desiredCommand)) {
            desiredSource.connectSource(desiredCommand);
        }
    }

    function applyProfile(output) {
        const match = output.trim().match(/^s "([a-z0-9_-]*)"$/);
        currentProfile = match ? match[1] : "";
    }

    function beginProfile(profileId) {
        confirmationAction = "";
        profileError = "";
        pendingProfile = profileId;
        profileApplySource.connectSource("/usr/bin/systemctl start egpu-service-roles-profile@" + profileId + ".service");
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
        id: profileSource
        engine: "executable"
        connectedSources: []

        onNewData: function(sourceName, data) {
            disconnectSource(sourceName);
            // An error (interface absent) simply hides the profile section.
            root.applyProfile((data["exit code"] ?? 1) === 0 ? (data["stdout"] ?? "") : "");
        }
    }

    Plasma5Support.DataSource {
        id: kwinSource
        engine: "executable"
        connectedSources: []

        onNewData: function(sourceName, data) {
            disconnectSource(sourceName);
            root.kwinOnAmd = (data["stdout"] ?? "").trim() === "0x1002";
        }
    }

    Plasma5Support.DataSource {
        id: desiredSource
        engine: "executable"
        connectedSources: []

        onNewData: function(sourceName, data) {
            disconnectSource(sourceName);
            const value = (data["stdout"] ?? "").trim();
            root.desiredProfile = /^[a-z0-9_-]{1,32}$/.test(value) ? value : "";
        }
    }

    Plasma5Support.DataSource {
        id: logoutSource
        engine: "executable"
        connectedSources: []

        onNewData: function(sourceName, data) {
            disconnectSource(sourceName);
        }
    }

    Plasma5Support.DataSource {
        id: lastErrorSource
        engine: "executable"
        connectedSources: []

        onNewData: function(sourceName, data) {
            disconnectSource(sourceName);
            const reason = (data["stdout"] ?? "").trim();
            if (reason.length > 0) {
                root.profileError = reason;
            }
        }
    }

    Plasma5Support.DataSource {
        id: profileApplySource
        engine: "executable"
        connectedSources: []

        onNewData: function(sourceName, data) {
            disconnectSource(sourceName);
            const exitCode = data["exit code"] ?? data["exitCode"] ?? 0;
            if (exitCode !== 0) {
                // The control tool leaves the reason in a world-readable file.
                root.profileError = root.localized("The profile was not applied.", "Профіль не застосовано.");
                lastErrorSource.connectSource(root.lastErrorCommand);
            }
            root.pendingProfile = "";
            root.refreshProfile();
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
        onTriggered: { root.refreshStatus(); root.refreshProfile(); root.refreshKwin(); root.refreshDesired(); }
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

            Kirigami.Separator {
                Layout.fillWidth: true
                visible: root.currentProfile.length > 0
            }

            PlasmaComponents3.Label {
                Layout.fillWidth: true
                visible: root.currentProfile.length > 0
                text: root.localized("GPU profile", "Профіль GPU")
                font.bold: true
            }

            PlasmaComponents3.Label {
                Layout.fillWidth: true
                visible: root.currentProfile.length > 0
                text: root.localized("Chosen: ", "Обрано: ") + root.chosenProfileLabel
                color: Kirigami.Theme.positiveTextColor
                wrapMode: Text.Wrap
            }

            Repeater {
                model: root.currentProfile.length > 0 ? root.profiles : []

                delegate: PlasmaComponents3.Button {
                    required property var modelData
                    Layout.fillWidth: true
                    icon.name: modelData.icon
                    readonly property bool active: root.chosenProfile === modelData.id
                    text: (active ? "✓ " : "") + modelData.label
                    font.bold: active
                    highlighted: active
                    enabled: root.pendingProfile.length === 0 && !active
                    PlasmaComponents3.ToolTip.text: modelData.hint
                    PlasmaComponents3.ToolTip.visible: hovered
                    onClicked: root.beginProfile(modelData.id)
                }
            }

            PlasmaComponents3.Label {
                Layout.fillWidth: true
                visible: root.currentProfile.length > 0 && root.reloginPending
                text: root.chosenProfile === "gaming-nvidia"
                    ? root.localized(
                        "Saved. Restart the session so the desktop renders on NVIDIA again.",
                        "Збережено. Перезапусти сеанс, щоб робочий стіл знову рендерився на NVIDIA.")
                    : root.localized(
                        "Saved. Restart the session: the desktop will render on the AMD GPU, then the profile is applied automatically.",
                        "Збережено. Перезапусти сеанс: робочий стіл рендеритиметься на AMD, після чого профіль застосується автоматично.")
                color: Kirigami.Theme.neutralTextColor
                wrapMode: Text.Wrap
            }

            PlasmaComponents3.Button {
                Layout.fillWidth: true
                visible: root.currentProfile.length > 0 && root.reloginPending
                icon.name: "system-log-out"
                text: root.localized("Restart session…", "Перезапустити сеанс…")
                onClicked: logoutSource.connectSource(root.logoutCommand)
            }

            PlasmaComponents3.Label {
                Layout.fillWidth: true
                visible: root.currentProfile.length > 0 && root.currentProfile !== "gaming-nvidia" && !root.reloginPending
                text: root.localized(
                    "In a work profile, new programs cannot use NVIDIA; running apps, the compositor and llama are unaffected.",
                    "У робочому профілі нові програми не можуть використовувати NVIDIA; запущені програми, компoзитор і llama не зачіпаються.")
                opacity: 0.75
                wrapMode: Text.Wrap
            }

            PlasmaComponents3.Label {
                Layout.fillWidth: true
                visible: root.profileError.length > 0
                text: root.profileError
                color: Kirigami.Theme.negativeTextColor
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
