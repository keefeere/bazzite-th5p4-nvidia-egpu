#!/usr/bin/env bash
set -Eeuo pipefail

if [[ ${EUID} -ne 0 ]]; then
    echo "Run this script as root." >&2
    exit 1
fi

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
KWIN_ENV="/etc/environment.d/10kwin-egpu.conf"
LOCK_FILE="/run/egpu-nvidia-transition.lock"
dm_stopped=0

restart_display_manager_on_error() {
    rc=$?
    trap - EXIT
    if (( rc != 0 && dm_stopped )); then
        systemctl start display-manager.service || true
        echo "eGPU activation failed; restored the login screen." >&2
    fi
    exit "${rc}"
}
trap restart_display_manager_on_error EXIT

# shellcheck source=egpu-pci-lib.sh
source "${SCRIPT_DIR}/egpu-pci-lib.sh"

exec 9>"${LOCK_FILE}"
flock -x 9

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

# Safe detach removes the exact GPU bridge while deliberately leaving the
# TH5P4 router and USB tunnel alive. If the cable is still attached, recreate
# only that child branch before clearing the latch. The expected upstream BDF
# and PCI ID are profile-validated so this cannot rescan an arbitrary bridge.
if [[ -s /run/egpu-safe-to-unplug ]] && ! resolve_egpu_gpu; then
    th5p4_router_present || {
        echo "${ENCLOSURE_DISPLAY_NAME} is physically disconnected; reconnect it and reboot to restore PCI resources." >&2
        exit 1
    }
    upstream_id="$(pci_id_at "${EXPECTED_TH5P4_UPSTREAM_BDF}" 2>/dev/null || true)"
    if [[ ${upstream_id} != "${TH5P4_VENDOR}:${TH5P4_DEVICE}" ]]; then
        echo "Refusing reattach: expected ${ENCLOSURE_DISPLAY_NAME} upstream ${EXPECTED_TH5P4_UPSTREAM_BDF}, found ${upstream_id:-nothing}." >&2
        exit 1
    fi
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
    /run/egpu-safe-to-unplug \
    /run/egpu-nvidia-detach-block \
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

trap - EXIT
echo "The NVIDIA eGPU was hot-attached at Gen3 x4."
