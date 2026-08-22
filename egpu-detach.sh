#!/usr/bin/env bash
set -Eeuo pipefail

if [[ ${EUID} -ne 0 ]]; then
    echo "Run this script as root." >&2
    exit 1
fi

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
KWIN_ENV="/etc/environment.d/10kwin-egpu.conf"
SAFE_MARKER="/run/egpu-safe-to-unplug"
PENDING_MARKER="/run/egpu-nvidia-hotplug-pending"
LATE_MARKER="/run/egpu-nvidia-late-loaded"
GEN4_ACTIVE_MARKER="/run/egpu-gen4-test-active"
DETACH_BLOCK_MARKER="/run/egpu-nvidia-detach-block"
LOCK_FILE="/run/egpu-nvidia-transition.lock"
dm_stopped=0
had_kwin_env=0
nvidia_unloaded=0
pci_branch_removed=0

# shellcheck source=egpu-pci-lib.sh
source "${SCRIPT_DIR}/egpu-pci-lib.sh"

exec 9>"${LOCK_FILE}"
flock -x 9
previous_kwin_env="$(mktemp /run/10kwin-egpu.previous.XXXXXX)"

restart_display_manager_on_error() {
    rc=$?
    trap - EXIT
    if (( rc != 0 )); then
        rm -f -- "${DETACH_BLOCK_MARKER}"
        if (( ! nvidia_unloaded )); then
            if (( had_kwin_env )); then
                install -D -m 0644 "${previous_kwin_env}" "${KWIN_ENV}"
            else
                rm -f -- "${KWIN_ENV}"
            fi
        fi
    fi
    rm -f -- "${previous_kwin_env}"
    if (( dm_stopped && (! nvidia_unloaded || pci_branch_removed) )); then
        systemctl start display-manager.service || true
    elif (( dm_stopped && nvidia_unloaded && ! pci_branch_removed )); then
        echo "NVIDIA is unloaded but its PCI branch was not removed; leaving the display manager stopped. Reboot without unplugging." >&2
    fi
    if (( rc != 0 )); then
        echo "eGPU detach failed; do not unplug the USB4 cable." >&2
    fi
    exit "${rc}"
}
trap restart_display_manager_on_error EXIT

if [[ -r ${KWIN_ENV} ]]; then
    cp -- "${KWIN_ENV}" "${previous_kwin_env}"
    had_kwin_env=1
fi

rm -f -- \
    "${SAFE_MARKER}" \
    "${PENDING_MARKER}" \
    "${LATE_MARKER}" \
    "${GEN4_ACTIVE_MARKER}"

if ! resolve_egpu_gpu; then
    echo "The NVIDIA eGPU is already absent."
    exit 0
fi
resolve_egpu_topology

if [[ -r /sys/module/nvidia_drm/parameters/fbdev ]]; then
    drm_fbdev="$(< /sys/module/nvidia_drm/parameters/fbdev)"
    if [[ ${drm_fbdev} != N && ${drm_fbdev} != 0 ]]; then
        echo "Safe detach requires nvidia_drm.fbdev=0, but the live module reports ${drm_fbdev}." >&2
        echo "Install the updated profile and reboot before trying again." >&2
        exit 1
    fi
fi

# Capture the configured dock endpoint before stopping the desktop. It must
# remain present after removing the sibling RTX branch.
dock_nic=""
if [[ ${HP_DOCK_SUPPORT} == 1 ]] && hp_dock_router_present; then
    dock_nic="$(find_unique_pci_device "${HP_DOCK_NIC_VENDOR}" "${HP_DOCK_NIC_DEVICE}" 2>/dev/null || true)"
    [[ -n ${dock_nic} ]] || {
        echo "The configured ${DOCK_DISPLAY_NAME} NIC is absent; refusing eGPU-only PCI removal." >&2
        exit 1
    }
fi

# Prevent a PCI rescan or a delayed udev add event from reloading NVIDIA after
# teardown but before the user physically unplugs. A deliberate manual
# hot-attach clears this latch; /run also clears it on reboot.
install -D -m 0644 /dev/null "${DETACH_BLOCK_MARKER}"

igpu_card="$(find "/sys/bus/pci/devices/${IGPU}/drm" -mindepth 1 -maxdepth 1 -name 'card[0-9]*' -printf '/dev/dri/%f\n' | head -n 1)"
egpu_card="$(find "/sys/bus/pci/devices/${GPU}/drm" -mindepth 1 -maxdepth 1 -name 'card[0-9]*' -printf '/dev/dri/%f\n' | head -n 1)"
egpu_render="$(find "/sys/bus/pci/devices/${GPU}/drm" -mindepth 1 -maxdepth 1 -name 'renderD[0-9]*' -printf '/dev/dri/%f\n' | head -n 1)"
if [[ -z ${igpu_card} || ! -c ${igpu_card} ]]; then
    echo "Could not resolve the AMD DRM card; refusing to stop the display manager." >&2
    exit 1
fi
if [[ -z ${egpu_card} || ! -c ${egpu_card} ]]; then
    echo "Could not resolve the NVIDIA DRM card; refusing to stop the display manager." >&2
    exit 1
fi
if [[ -z ${egpu_render} || ! -c ${egpu_render} ]]; then
    echo "Could not resolve the NVIDIA DRM render node; refusing to stop the display manager." >&2
    exit 1
fi

tmp_env="$(mktemp /run/10kwin-internal.conf.XXXXXX)"
printf '%s\n' \
    '# Generated for a safe NVIDIA eGPU detach.' \
    "KWIN_DRM_DEVICES=${igpu_card}" \
    > "${tmp_env}"
install -D -m 0644 "${tmp_env}" "${KWIN_ENV}"
rm -f -- "${tmp_env}"

echo "Stopping the graphical session so KWin releases the NVIDIA DRM device..."

# Remember the actual graphical login scopes. terminate-user proved racy on
# this machine and could return while the Wayland session was still active.
graphical_sessions=()
while read -r session_id session_uid _; do
    [[ ${session_uid} == "${DESKTOP_UID}" ]] || continue
    session_class="$(loginctl show-session "${session_id}" -p Class --value 2>/dev/null || true)"
    session_type="$(loginctl show-session "${session_id}" -p Type --value 2>/dev/null || true)"
    if [[ ${session_class} == user && (${session_type} == wayland || ${session_type} == x11) ]]; then
        graphical_sessions+=("${session_id}")
    fi
done < <(loginctl list-sessions --no-legend 2>/dev/null || true)

# SDDM does not own the Plasma processes on current Bazzite releases: KWin,
# plasmashell and applications are user-systemd units. Ask that manager to
# stop the graphical session first. --no-block is intentional because this
# system service must remain alive while the tray process disappears.
systemctl --user --machine="${DESKTOP_USER}@.host" \
    stop --no-block graphical-session.target plasma-workspace.target || true

systemctl stop display-manager.service
dm_stopped=1

# End the concrete login scope(s), not the persistent user manager. Other
# user services may still exist, so exact GPU-node holders are handled below.
for session_id in "${graphical_sessions[@]}"; do
    loginctl terminate-session "${session_id}" || true
done
systemctl stop nvidia-persistenced.service

gpu_nodes=(
    "${egpu_card}"
    "${egpu_render}"
    /dev/nvidia0
    /dev/nvidiactl
    /dev/nvidia-modeset
    /dev/nvidia-uvm
    /dev/nvidia-uvm-tools
)

nodes_are_busy() {
    local node
    for node in "${gpu_nodes[@]}"; do
        if [[ -e ${node} ]] && fuser -s "${node}" 2>/dev/null; then
            return 0
        fi
    done
    return 1
}

# Give Plasma and ordinary applications a bounded graceful-exit window.
for _ in {1..50}; do
    if ! nodes_are_busy; then
        break
    fi
    sleep 0.2
done

# A browser or restored application can live outside the login scope and keep
# a render node open. Safe detach necessarily ends such GPU clients. Kill only
# processes that hold an exact eGPU device node, after validating ownership.
if nodes_are_busy; then
    declare -A holder_set=()
    for node in "${gpu_nodes[@]}"; do
        [[ -e ${node} ]] || continue
        for pid in $(fuser "${node}" 2>/dev/null || true); do
            [[ ${pid} =~ ^[0-9]+$ ]] && holder_set["${pid}"]=1
        done
    done

    for pid in "${!holder_set[@]}"; do
        [[ -r /proc/${pid}/status ]] || continue
        read -r _ real_uid _ < <(grep '^Uid:' "/proc/${pid}/status")
        if [[ ${real_uid} != "${DESKTOP_UID}" ]]; then
            echo "Refusing to terminate NVIDIA holder PID ${pid}: UID ${real_uid}, expected ${DESKTOP_UID}." >&2
            exit 1
        fi
    done

    if (( ${#holder_set[@]} )); then
        echo "Ending remaining user processes that hold the NVIDIA device..."
        kill -TERM "${!holder_set[@]}" 2>/dev/null || true
    fi
    for _ in {1..25}; do
        if ! nodes_are_busy; then
            break
        fi
        sleep 0.2
    done
fi

# TERM-resistant clients are force-ended only after the orderly session stop
# and only after repeating ownership validation for their current live PIDs.
if nodes_are_busy; then
    declare -A stubborn_holder_set=()
    for node in "${gpu_nodes[@]}"; do
        [[ -e ${node} ]] || continue
        for pid in $(fuser "${node}" 2>/dev/null || true); do
            [[ ${pid} =~ ^[0-9]+$ ]] && stubborn_holder_set["${pid}"]=1
        done
    done
    for pid in "${!stubborn_holder_set[@]}"; do
        [[ -r /proc/${pid}/status ]] || continue
        read -r _ real_uid _ < <(grep '^Uid:' "/proc/${pid}/status")
        if [[ ${real_uid} != "${DESKTOP_UID}" ]]; then
            echo "Refusing to kill NVIDIA holder PID ${pid}: UID ${real_uid}, expected ${DESKTOP_UID}." >&2
            exit 1
        fi
    done
    if (( ${#stubborn_holder_set[@]} )); then
        echo "Force-ending unresponsive user NVIDIA clients..."
        kill -KILL "${!stubborn_holder_set[@]}" 2>/dev/null || true
        sleep 1
    fi
fi

if nodes_are_busy; then
    echo "One or more NVIDIA device nodes are still in use:" >&2
    for node in "${gpu_nodes[@]}"; do
        [[ -e ${node} ]] && fuser -v "${node}" >&2 || true
    done
    exit 1
fi

if [[ -L "/sys/bus/pci/devices/${AUDIO}/driver" ]] &&
   [[ $(basename "$(readlink "/sys/bus/pci/devices/${AUDIO}/driver")") == "snd_hda_intel" ]]; then
    echo "${AUDIO}" > /sys/bus/pci/drivers/snd_hda_intel/unbind
fi

modprobe -r nvidia_drm nvidia_modeset nvidia_uvm nvidia
if grep -q '^nvidia' /proc/modules; then
    echo "One or more NVIDIA modules remain loaded." >&2
    grep '^nvidia' /proc/modules >&2
    exit 1
fi
nvidia_unloaded=1

# A driverless but still enumerated RTX remains visible to desktop power
# helpers and leaves the board in an unmanaged fail-safe state. Remove only
# the already-validated GPU branch from Linux's PCI model. The enclosure
# upstream and every sibling branch (including the downstream dock) stay
# authorized and enumerated until the user physically unplugs the USB4 cable.
echo "Removing the released ${EGPU_DISPLAY_NAME} PCI branch from the kernel model..."
[[ -d /sys/bus/pci/devices/${BRIDGE} ]] || {
    echo "The validated eGPU bridge ${BRIDGE} disappeared before PCI removal." >&2
    exit 1
}
echo 1 > "/sys/bus/pci/devices/${BRIDGE}/remove"

for _ in {1..25}; do
    [[ ! -e /sys/bus/pci/devices/${GPU} &&
       ! -e /sys/bus/pci/devices/${AUDIO} &&
       ! -e /sys/bus/pci/devices/${BRIDGE} ]] && break
    sleep 0.2
done
if [[ -e /sys/bus/pci/devices/${GPU} ||
      -e /sys/bus/pci/devices/${AUDIO} ||
      -e /sys/bus/pci/devices/${BRIDGE} ]]; then
    echo "The exact eGPU PCI branch did not disappear; refusing safe-unplug status." >&2
    exit 1
fi
if ! [[ -d /sys/bus/pci/devices/${UPSTREAM} ]]; then
    echo "The enclosure upstream bridge disappeared unexpectedly." >&2
    exit 1
fi
if [[ -n ${dock_nic} ]]; then
    if [[ ! -d /sys/bus/pci/devices/${dock_nic} ]] || ! hp_dock_router_present; then
        echo "${DOCK_DISPLAY_NAME} changed during eGPU-only PCI removal." >&2
        exit 1
    fi
fi
pci_branch_removed=1

echo "Starting the AMD-only login screen..."
systemctl start display-manager.service
systemctl is-active --quiet display-manager.service
dm_stopped=0

printf '%s\n' \
    "NVIDIA modules are unloaded, HDMI/DP audio is unbound, and the exact eGPU PCI branch is removed." \
    "It is now safe to unplug the eGPU USB4 cable." \
    > "${SAFE_MARKER}"

trap - EXIT
rm -f -- "${previous_kwin_env}"
cat "${SAFE_MARKER}"
