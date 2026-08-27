#!/usr/bin/env bash
set -Eeuo pipefail

if [[ ${EUID} -ne 0 ]]; then
    echo "Run this script as root." >&2
    exit 1
fi

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
KWIN_ENV="/etc/environment.d/10kwin-egpu.conf"
LOCK_FILE="/run/egpu-nvidia-transition.lock"
SAFE_MARKER="/run/egpu-safe-to-unplug"
DETACH_BLOCK_MARKER="/run/egpu-nvidia-detach-block"
LOCAL_RESERVE_MARKER="/run/egpu-local-reserve-applied"
REBOOT_MARKER="/run/egpu-nvidia-reboot-required"
dm_stopped=0
pci_repair_started=0

# shellcheck source=egpu-cardwire-compat.sh
source "${SCRIPT_DIR}/egpu-cardwire-compat.sh"

restart_display_manager_on_error() {
    rc=$?
    trap - EXIT
    if (( rc != 0 && pci_repair_started )); then
        install -D -m 0644 /dev/null "${REBOOT_MARKER}"
        echo "The controlled PCI reattach repair failed; NVIDIA remains blocked. Reboot with the enclosure connected." >&2
    fi
    if (( rc != 0 && dm_stopped )); then
        systemctl start display-manager.service || true
        echo "eGPU activation failed; restored the login screen." >&2
    fi
    egpu_resume_cardwire || true
    exit "${rc}"
}
trap restart_display_manager_on_error EXIT

# shellcheck source=egpu-pci-lib.sh
source "${SCRIPT_DIR}/egpu-pci-lib.sh"
# shellcheck source=egpu-kernel-compat.sh
source "${SCRIPT_DIR}/egpu-kernel-compat.sh"

exec 9>"${LOCK_FILE}"
flock -x 9

# Cardwire reacts to DRM/PCI hotplug and can open NVIDIA nodes while this
# script is still rebuilding and validating the exact branch. Pause it for the
# whole transition; its previous active/inactive state is restored on every
# exit path.
egpu_pause_cardwire

stop_desktop_for_transition() {
    local session_id session_uid session_class session_type
    local -a graphical_sessions=()

    (( dm_stopped == 0 )) || return 0
    while read -r session_id session_uid _; do
        [[ ${session_uid} == "${DESKTOP_UID}" ]] || continue
        session_class="$(loginctl show-session "${session_id}" -p Class --value 2>/dev/null || true)"
        session_type="$(loginctl show-session "${session_id}" -p Type --value 2>/dev/null || true)"
        if [[ ${session_class} == user && (${session_type} == wayland || ${session_type} == x11) ]]; then
            graphical_sessions+=("${session_id}")
        fi
    done < <(loginctl list-sessions --no-legend 2>/dev/null || true)

    systemctl --user --machine="${DESKTOP_USER}@.host" \
        stop --no-block graphical-session.target plasma-workspace.target || true
    systemctl stop display-manager.service
    dm_stopped=1
    for session_id in "${graphical_sessions[@]}"; do
        loginctl terminate-session "${session_id}" || true
    done

    # A fresh user manager is required: startplasma otherwise imports the old
    # AMD-only KWIN_DRM_DEVICES value back over the generated NVIDIA-first one.
    loginctl terminate-user "${DESKTOP_UID}" || true
    systemctl stop "user@${DESKTOP_UID}.service" || true
    for _ in {1..50}; do
        systemctl is-active --quiet "user@${DESKTOP_UID}.service" || break
        sleep 0.2
    done
    if systemctl is-active --quiet "user@${DESKTOP_UID}.service"; then
        echo "The old desktop user manager is still active; refusing an ambiguous KWin restart." >&2
        return 1
    fi
}

pci_branch_has_descendants() {
    local ancestor=$1
    local sysfs bdf

    for sysfs in /sys/bus/pci/devices/0000:*; do
        bdf=${sysfs##*/}
        [[ ${bdf} == "${ancestor}" ]] && continue
        if is_pci_descendant_of "${bdf}" "${ancestor}"; then
            return 0
        fi
    done
    return 1
}

validate_gpu_branch_children() {
    local bridge=$1
    local sysfs bdf

    for sysfs in /sys/bus/pci/devices/0000:*; do
        bdf=${sysfs##*/}
        [[ ${bdf} == "${bridge}" ]] && continue
        if is_pci_descendant_of "${bdf}" "${bridge}"; then
            case ${bdf} in
                "${EXPECTED_GPU_BDF}"|"${EXPECTED_AUDIO_BDF}") ;;
                *)
                    echo "Unexpected PCI descendant ${bdf} below the released RTX port ${bridge}." >&2
                    return 1
                    ;;
            esac
        fi
    done
}

repair_hotplug_size_reattach() {
    local upstream=${EXPECTED_TH5P4_UPSTREAM_BDF}
    local gpu_bridge=${EXPECTED_GPU_BRIDGE_BDF}
    local bridge_prefix=${EXPECTED_GPU_BRIDGE_BDF%:*}
    local dock_port="${bridge_prefix}:01.0"
    local spare_port2="${bridge_prefix}:02.0"
    local spare_port3="${bridge_prefix}:03.0"
    local port dock_nic audio_driver

    [[ -s ${SAFE_MARKER} && -e ${DETACH_BLOCK_MARKER} ]] || {
        echo "The same-cable detach latch is absent; refusing a live PCI repair." >&2
        return 1
    }
    [[ -s ${LOCAL_RESERVE_MARKER} ]] || {
        echo "The validated runtime PCI reserve marker is absent." >&2
        return 1
    }
    if grep -q '^nvidia' /proc/modules; then
        echo "NVIDIA is unexpectedly loaded during a released-branch repair." >&2
        return 1
    fi
    th5p4_router_present || {
        echo "${ENCLOSURE_DISPLAY_NAME} is physically disconnected; reconnect it and reboot to restore PCI resources." >&2
        return 1
    }
    [[ $(pci_id_at "${upstream}" 2>/dev/null || true) == "${TH5P4_VENDOR}:${TH5P4_DEVICE}" ]] || {
        echo "Refusing reattach: the configured ${ENCLOSURE_DISPLAY_NAME} upstream bridge is absent." >&2
        return 1
    }

    for port in "${dock_port}" "${spare_port2}" "${spare_port3}"; do
        [[ $(pci_id_at "${port}" 2>/dev/null || true) == "${TH5P4_VENDOR}:${TH5P4_DEVICE}" ]] || {
            echo "Refusing reattach: expected ${ENCLOSURE_DISPLAY_NAME} port ${port} is absent or changed." >&2
            return 1
        }
    done
    for port in "${spare_port2}" "${spare_port3}"; do
        if pci_branch_has_descendants "${port}"; then
            echo "Refusing reattach: nominally empty ${ENCLOSURE_DISPLAY_NAME} port ${port} has a descendant." >&2
            return 1
        fi
    done

    if [[ -d /sys/bus/pci/devices/${gpu_bridge} ]]; then
        [[ $(pci_id_at "${gpu_bridge}" 2>/dev/null || true) == "${TH5P4_VENDOR}:${TH5P4_DEVICE}" ]] || {
            echo "The returned RTX bridge ${gpu_bridge} has an unexpected PCI ID." >&2
            return 1
        }
        resolve_egpu_topology
        validate_expected_topology
        validate_gpu_branch_children "${gpu_bridge}"
        audio_driver="$(basename -- "$(readlink -f "/sys/bus/pci/devices/${EXPECTED_AUDIO_BDF}/driver" 2>/dev/null || true)")"
        if [[ ${audio_driver} == snd_hda_intel ]]; then
            echo "${EXPECTED_AUDIO_BDF}" > /sys/bus/pci/drivers/snd_hda_intel/unbind
        fi
    fi

    if [[ ${HP_DOCK_SUPPORT} == 1 ]] && hp_dock_router_present; then
        dock_nic="$(find_unique_pci_device "${HP_DOCK_NIC_VENDOR}" "${HP_DOCK_NIC_DEVICE}" 2>/dev/null || true)"
        [[ -n ${dock_nic} &&
           $(<"/sys/bus/pci/devices/${dock_nic}/subsystem_vendor") == "${HP_DOCK_NIC_SUBSYSTEM_VENDOR}" &&
           $(<"/sys/bus/pci/devices/${dock_nic}/subsystem_device") == "${HP_DOCK_NIC_SUBSYSTEM_DEVICE}" ]] || {
            echo "The configured ${DOCK_DISPLAY_NAME} NIC changed before the RTX-only repair." >&2
            return 1
        }
        is_pci_descendant_of "${dock_nic}" "${dock_port}" || {
            echo "The configured ${DOCK_DISPLAY_NAME} NIC is outside its validated port." >&2
            return 1
        }
    fi

    pci_repair_started=1
    echo "Recycling only the released RTX port and two validated empty ${ENCLOSURE_DISPLAY_NAME} ports..."
    if [[ -d /sys/bus/pci/devices/${gpu_bridge} ]]; then
        echo 1 > "/sys/bus/pci/devices/${gpu_bridge}/remove"
    fi
    echo 1 > "/sys/bus/pci/devices/${spare_port2}/remove"
    echo 1 > "/sys/bus/pci/devices/${spare_port3}/remove"

    for _ in {1..50}; do
        [[ ! -d /sys/bus/pci/devices/${gpu_bridge} &&
           ! -d /sys/bus/pci/devices/${spare_port2} &&
           ! -d /sys/bus/pci/devices/${spare_port3} ]] && break
        sleep 0.1
    done
    [[ ! -d /sys/bus/pci/devices/${gpu_bridge} &&
       ! -d /sys/bus/pci/devices/${spare_port2} &&
       ! -d /sys/bus/pci/devices/${spare_port3} ]]

    echo "Rescanning the validated ${ENCLOSURE_DISPLAY_NAME} ports with RTX first..."
    echo 1 > "/sys/bus/pci/devices/${upstream}/rescan"
    for _ in {1..50}; do
        [[ -d /sys/bus/pci/devices/${EXPECTED_GPU_BDF} &&
           -d /sys/bus/pci/devices/${spare_port2} &&
           -d /sys/bus/pci/devices/${spare_port3} ]] && break
        sleep 0.1
    done
    [[ -d /sys/bus/pci/devices/${EXPECTED_GPU_BDF} &&
       -d /sys/bus/pci/devices/${spare_port2} &&
       -d /sys/bus/pci/devices/${spare_port3} ]]

    resolve_egpu_topology
    validate_expected_topology
    audio_driver="$(basename -- "$(readlink -f "/sys/bus/pci/devices/${AUDIO}/driver" 2>/dev/null || true)")"
    if [[ ${audio_driver} == snd_hda_intel ]]; then
        echo "${AUDIO}" > /sys/bus/pci/drivers/snd_hda_intel/unbind
    fi
    if [[ ${HP_DOCK_SUPPORT} == 1 ]] && hp_dock_router_present; then
        "${SCRIPT_DIR}/egpu-cold-attached-hp-verify.sh"
    else
        "${SCRIPT_DIR}/egpu-local-reserve-verify.sh"
    fi
    if grep -q '^nvidia' /proc/modules; then
        echo "NVIDIA loaded unexpectedly during the controlled RTX-port repair." >&2
        return 1
    fi

    pci_repair_started=0
    rm -f -- "${REBOOT_MARKER}"
    echo "The Linux 7.2+ same-cable RTX port repair passed."
}

# Safe detach removes the exact GPU bridge while deliberately leaving the
# TH5P4 router and USB tunnel alive. Linux 7.2+ can give the RTX I/O window to
# an empty hot-plug port during a child-only rescan. Recycle only those exact
# empty siblings so deterministic BDF scan order gives the RTX port its window
# first. Older kernels retain the previously validated child-only rescan.
if [[ -s ${SAFE_MARKER} ]]; then
    th5p4_router_present || {
        echo "${ENCLOSURE_DISPLAY_NAME} is physically disconnected; reconnect it and reboot to restore PCI resources." >&2
        exit 1
    }
    upstream_id="$(pci_id_at "${EXPECTED_TH5P4_UPSTREAM_BDF}" 2>/dev/null || true)"
    if [[ ${upstream_id} != "${TH5P4_VENDOR}:${TH5P4_DEVICE}" ]]; then
        echo "Refusing reattach: expected ${ENCLOSURE_DISPLAY_NAME} upstream ${EXPECTED_TH5P4_UPSTREAM_BDF}, found ${upstream_id:-nothing}." >&2
        exit 1
    fi
    kernel_compat_mode=$(egpu_kernel_compat_mode "$(uname -r)")
    if [[ ${kernel_compat_mode} == hotplug-size ]]; then
        repair_hotplug_size_reattach
    elif ! resolve_egpu_gpu; then
        echo "Rescanning the released ${ENCLOSURE_DISPLAY_NAME} GPU port..."
        echo 1 > "/sys/bus/pci/devices/${EXPECTED_TH5P4_UPSTREAM_BDF}/rescan"
        for _ in {1..25}; do
            resolve_egpu_gpu && break
            sleep 0.2
        done
        resolve_egpu_gpu || {
            echo "${EGPU_DISPLAY_NAME} did not return after the validated upstream rescan." >&2
            exit 1
        }
    fi
fi

# A full cable removal destroys the programmed PCI bridge state and can return
# the RTX with a 256 MiB BAR1 instead of the validated 16 GiB BAR1. Rebuilding
# nested TH5P4/HP resources and resizing ReBAR live is deliberately unsupported:
# keep the AMD session intact and require the proven early-boot path.
reserve_valid=1
if [[ -e /etc/egpu-nvidia/enable-local-reserve ]]; then
    if [[ ! -s /run/egpu-local-reserve-applied ]]; then
        reserve_valid=0
    elif hp_dock_router_present; then
        "${SCRIPT_DIR}/egpu-cold-attached-hp-verify.sh" >/dev/null 2>&1 || reserve_valid=0
    else
        "${SCRIPT_DIR}/egpu-local-reserve-verify.sh" >/dev/null 2>&1 || reserve_valid=0
    fi
fi

if (( ! reserve_valid )); then
    install -D -m 0644 /dev/null /run/egpu-nvidia-reboot-required
    rm -f -- /run/egpu-nvidia-hotplug-pending /run/egpu-nvidia-late-loaded
    echo "The live TH5P4 PCI reserve is absent or stale after a physical reconnect."
    echo "Reboot with the enclosure connected; NVIDIA was intentionally left unloaded."
    exit 0
fi

rm -f -- \
    "${SAFE_MARKER}" \
    "${DETACH_BLOCK_MARKER}" \
    "${REBOOT_MARKER}" \
    /run/egpu-nvidia-late-loaded \
    /run/egpu-nvidia-hotplug-pending

EGPU_TRANSITION_LOCK_HELD=1 EGPU_FORCE_GEN3=1 \
    "${SCRIPT_DIR}/egpu-boot.sh"

kwin_devices="$(sed -n 's/^KWIN_DRM_DEVICES=//p' "${KWIN_ENV}")"
if [[ -z ${kwin_devices} ]]; then
    echo "The generated KWin GPU order is missing; refusing to restart the session." >&2
    exit 1
fi

echo "Restarting the graphical session with NVIDIA as KWin primary..."
stop_desktop_for_transition
systemctl start display-manager.service
dm_stopped=0

egpu_resume_cardwire || true

trap - EXIT
echo "The NVIDIA eGPU was hot-attached at Gen3 x4."
