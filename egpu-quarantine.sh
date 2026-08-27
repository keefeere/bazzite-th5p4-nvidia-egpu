#!/usr/bin/env bash
set -Eeuo pipefail

if [[ ${EUID} -ne 0 ]]; then
    echo "Run this script as root." >&2
    exit 1
fi

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
BOOT_SCRIPT="${SCRIPT_DIR}/egpu-boot.sh"
LOCK_FILE="/run/egpu-nvidia-transition.lock"
PENDING_MARKER="/run/egpu-nvidia-hotplug-pending"
LATE_MARKER="/run/egpu-nvidia-late-loaded"
KWIN_ENV="/etc/environment.d/10kwin-egpu.conf"
DETACH_BLOCK_MARKER="/run/egpu-nvidia-detach-block"
LOCAL_RESERVE_FAILURE_MARKER="/run/egpu-local-reserve-failed"
RELEASED_UNPLUGGED_MARKER="/run/egpu-nvidia-released-unplugged"

# shellcheck source=egpu-pci-lib.sh
source "${SCRIPT_DIR}/egpu-pci-lib.sh"

exec 9>"${LOCK_FILE}"
flock -x 9

# A cable removed after a successful safe detach is a completed one-way
# transition on this Linux 7.2 profile. Never reinterpret its later add event
# as a repairable first hot-plug: the released bridge state belongs to the
# previous tunnel and must be rebuilt during the next early boot.
if [[ -s ${RELEASED_UNPLUGGED_MARKER} ]]; then
    install -D -m 0644 /dev/null /run/egpu-nvidia-reboot-required
    printf '%s\n' \
        "The eGPU was safely unplugged earlier in this boot." \
        "Same-boot physical reattach is unsupported; reboot with the enclosure attached." \
        > "${PENDING_MARKER}"
    rm -f -- "${LATE_MARKER}"
    cat "${PENDING_MARKER}"
    exit 0
fi

if [[ -e ${DETACH_BLOCK_MARKER} ]]; then
    echo "Safe-detach is latched; refusing automatic NVIDIA reload until a deliberate hot-attach or reboot."
    exit 0
fi

if ! resolve_egpu_gpu; then
    rm -f -- "${PENDING_MARKER}" "${LATE_MARKER}"
    echo "The late NVIDIA endpoint disappeared before it could be initialized."
    exit 0
fi

resolve_egpu_topology

# The HDA add event also queues this unit when the endpoint was present early.
# In that normal path the boot service has already initialized the complete
# stack by the time our After= ordering releases us; do not label it as late.
if grep -q '^nvidia_drm ' /proc/modules; then
    rm -f -- "${PENDING_MARKER}" "${LATE_MARKER}"
    echo "The normal boot path already initialized NVIDIA; no asynchronous action is needed."
    exit 0
fi

# A physical enclosure hot-plug can enumerate the RTX with only a small BAR1.
# Rebuilding the local reserve requires a deliberately experimental graphical
# transition, so never perform it from an automatic udev event or advertise it
# as the normal tray flow. The production recovery is an early boot.
reserve_needs_manual_rebuild=0
if [[ -e /etc/egpu-nvidia/enable-local-reserve ]]; then
    if [[ ! -s /run/egpu-local-reserve-applied ]]; then
        reserve_needs_manual_rebuild=1
    elif hp_dock_router_present; then
        "${SCRIPT_DIR}/egpu-cold-attached-hp-verify.sh" >/dev/null 2>&1 || reserve_needs_manual_rebuild=1
    else
        "${SCRIPT_DIR}/egpu-local-reserve-verify.sh" >/dev/null 2>&1 || reserve_needs_manual_rebuild=1
    fi
fi
if (( reserve_needs_manual_rebuild )); then
    install -D -m 0644 /dev/null /run/egpu-nvidia-reboot-required
    if [[ -s ${LOCAL_RESERVE_FAILURE_MARKER} ]]; then
        {
            cat -- "${LOCAL_RESERVE_FAILURE_MARKER}"
            echo "This was an early reserve failure, not a physical cable reconnect."
            echo "Correct the kernel compatibility state, then reboot with the enclosure attached."
        } > "${PENDING_MARKER}"
    else
        printf '%s\n' \
            "The NVIDIA eGPU appeared after the graphical boot path." \
            "Production activation requires a reboot with the enclosure attached; the current AMD session remains untouched." \
            > "${PENDING_MARKER}"
    fi
    rm -f -- "${LATE_MARKER}"
    cat "${PENDING_MARKER}"
    exit 0
fi

echo "A late NVIDIA endpoint appeared after the graphical boot path."
echo "Staging and loading it asynchronously without restarting the graphical session..."
EGPU_TRANSITION_LOCK_HELD=1 "${BOOT_SCRIPT}"

kwin_devices="$(sed -n 's/^KWIN_DRM_DEVICES=//p' "${KWIN_ENV}")"
if [[ -z ${kwin_devices} ]]; then
    echo "The generated KWin GPU order is missing after late initialization." >&2
    exit 1
fi

# A logged-in user's systemd manager can outlive Plasma sessions. Seed the
# NVIDIA-first order for the next voluntary login, but never terminate the
# current AMD session from an automatic udev event.
desktop_user="${DESKTOP_USER}"
if [[ -n ${desktop_user} && -S /run/user/${DESKTOP_UID}/bus ]]; then
    runuser -u "${desktop_user}" -- \
        env XDG_RUNTIME_DIR="/run/user/${DESKTOP_UID}" \
        systemctl --user set-environment \
        "KWIN_DRM_DEVICES=${kwin_devices}" || true
fi

printf '%s\n' \
    "The late NVIDIA eGPU is loaded at fixed Gen3 x4 without interrupting the AMD session." \
    "External outputs may hot-appear; log out when convenient to make NVIDIA the KWin primary GPU." \
    > "${LATE_MARKER}"
rm -f -- "${PENDING_MARKER}"

cat "${LATE_MARKER}"
