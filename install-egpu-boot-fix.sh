#!/usr/bin/env bash
set -Eeuo pipefail

if [[ ${EUID} -ne 0 ]]; then
    echo "Run this installer as root (sudo)." >&2
    exit 1
fi

SOURCE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="/etc/egpu-nvidia"
SERVICE="egpu-nvidia-boot.service"
KWIN_ENV="/etc/environment.d/10kwin-egpu.conf"
POLKIT_RULE="/etc/polkit-1/rules.d/49-egpu-nvidia.rules"
PLASMOID_ID="com.keefeere.egpu"

profile_source="${EGPU_PROFILE_SOURCE:-}"
if [[ -n ${profile_source} ]]; then
    [[ -r ${profile_source} ]] || {
        echo "Requested hardware profile is not readable: ${profile_source}" >&2
        exit 1
    }
elif [[ -r ${INSTALL_DIR}/hardware.conf ]]; then
    profile_source="${INSTALL_DIR}/hardware.conf"
else
    profile_source="${SOURCE_DIR}/hardware.conf"
fi

# This is intentionally read-only and runs before the first filesystem change.
EGPU_CONFIG_FILE="${profile_source}" "${SOURCE_DIR}/egpu-config-preflight.sh"

# Validate the candidate before it can replace the installed profile.
EGPU_CONFIG_FILE="${profile_source}"
# shellcheck source=egpu-pci-lib.sh
source "${SOURCE_DIR}/egpu-pci-lib.sh"
install -d -m 0755 "${INSTALL_DIR}"
if [[ ${profile_source} != "${INSTALL_DIR}/hardware.conf" ]]; then
    install -m 0644 "${profile_source}" "${INSTALL_DIR}/hardware.conf"
fi

desktop_passwd="$(getent passwd "${DESKTOP_USER}" || true)"
if [[ -z ${desktop_passwd} ]]; then
    echo "Could not resolve configured desktop user ${DESKTOP_USER}; refusing to install the tray widget." >&2
    exit 1
fi
IFS=: read -r desktop_user _ desktop_uid desktop_gid _ desktop_home _ <<< "${desktop_passwd}"
if [[ ${desktop_user} != "${DESKTOP_USER}" || ${desktop_uid} != "${DESKTOP_UID}" ]]; then
    echo "Configured desktop identity ${DESKTOP_USER}:${DESKTOP_UID} does not match ${desktop_user}:${desktop_uid}." >&2
    exit 1
fi
PLASMOID_DIR="${desktop_home}/.local/share/plasma/plasmoids/${PLASMOID_ID}"

# Remove the retired authorization-cycling experiment.  The replacement keeps
# the HP USB4 router alive and rebuilds only its PCI subtree.
rm -f -- "${INSTALL_DIR}/allow-cold-hp-cycle"
install -m 0755 \
    "${SOURCE_DIR}/egpu-stage-gen3.sh" \
    "${SOURCE_DIR}/egpu-load-nvidia-gen3.sh" \
    "${SOURCE_DIR}/egpu-boot.sh" \
    "${SOURCE_DIR}/egpu-quarantine.sh" \
    "${SOURCE_DIR}/egpu-detach.sh" \
    "${SOURCE_DIR}/egpu-hot-attach.sh" \
    "${SOURCE_DIR}/egpu-physical-unplug.sh" \
    "${SOURCE_DIR}/egpu-tray-status.sh" \
    "${SOURCE_DIR}/egpu-config-preflight.sh" \
    "${SOURCE_DIR}/egpu-hardware-report.sh" \
    "${SOURCE_DIR}/enable-egpu-gen4.sh" \
    "${SOURCE_DIR}/disable-egpu-gen4.sh" \
    "${SOURCE_DIR}/verify-egpu-install.sh" \
    "${SOURCE_DIR}/egpu-local-reserve-preflight.sh" \
    "${SOURCE_DIR}/egpu-local-reserve-apply.sh" \
    "${SOURCE_DIR}/egpu-cold-hp-pci-rebuild.sh" \
    "${SOURCE_DIR}/egpu-local-reserve-verify.sh" \
    "${SOURCE_DIR}/egpu-cold-attached-hp-verify.sh" \
    "${SOURCE_DIR}/egpu-local-reserve-accept-live.sh" \
    "${INSTALL_DIR}/"
install -m 0644 \
    "${SOURCE_DIR}/egpu-pci-lib.sh" \
    "${SOURCE_DIR}/egpu-kernel-compat.sh" \
    "${SOURCE_DIR}/nvidia-base-only.conf" \
    "${INSTALL_DIR}/"
install -m 0644 \
    "${SOURCE_DIR}/${SERVICE}" \
    "${SOURCE_DIR}/egpu-nvidia-quarantine.service" \
    "${SOURCE_DIR}/egpu-nvidia-detach.service" \
    "${SOURCE_DIR}/egpu-nvidia-hot-attach.service" \
    "/etc/systemd/system/"
install -D -m 0644 \
    "${SOURCE_DIR}/99-egpu-delay-nvidia.conf" \
    "/etc/modprobe.d/99-egpu-delay-nvidia.conf"

tmp_udev="$(mktemp /run/99-egpu-nvidia.rules.XXXXXX)"
tmp_polkit="$(mktemp /run/49-egpu-nvidia.rules.XXXXXX)"
trap 'rm -f -- "${tmp_udev}" "${tmp_polkit}"' EXIT
sed \
    -e "s|@EGPU_VENDOR@|${EGPU_VENDOR}|g" \
    -e "s|@EGPU_DEVICE@|${EGPU_DEVICE}|g" \
    -e "s|@EGPU_AUDIO_VENDOR@|${EGPU_AUDIO_VENDOR}|g" \
    -e "s|@EGPU_AUDIO_DEVICE@|${EGPU_AUDIO_DEVICE}|g" \
    -e "s|@TH5P4_TB_VENDOR@|${TH5P4_TB_VENDOR}|g" \
    -e "s|@TH5P4_TB_DEVICE@|${TH5P4_TB_DEVICE}|g" \
    -e "s|@TH5P4_TB_NAME@|${TH5P4_TB_NAME}|g" \
    "${SOURCE_DIR}/99-egpu-nvidia.rules.in" > "${tmp_udev}"
sed \
    -e "s|@DESKTOP_USER@|${DESKTOP_USER}|g" \
    "${SOURCE_DIR}/49-egpu-nvidia.rules.in" > "${tmp_polkit}"
install -D -m 0644 "${tmp_udev}" "/etc/udev/rules.d/99-egpu-nvidia.rules"
install -D -m 0644 "${tmp_polkit}" "${POLKIT_RULE}"
rm -f -- "${tmp_udev}" "${tmp_polkit}"
trap - EXIT

# The widget itself is user data so it survives rpm-ostree deployments without
# modifying immutable /usr.  It is marked as a Hardware System Tray applet and
# will be discovered on the next Plasma login.
install -d -m 0755 -o "${desktop_uid}" -g "${desktop_gid}" \
    "${PLASMOID_DIR}/contents/ui"
install -m 0644 -o "${desktop_uid}" -g "${desktop_gid}" \
    "${SOURCE_DIR}/plasmoid/${PLASMOID_ID}/metadata.json" \
    "${PLASMOID_DIR}/metadata.json"
install -m 0644 -o "${desktop_uid}" -g "${desktop_gid}" \
    "${SOURCE_DIR}/plasmoid/${PLASMOID_ID}/contents/ui/main.qml" \
    "${PLASMOID_DIR}/contents/ui/main.qml"

# Resolve both GPUs by their exact PCI IDs so installed configuration does not
# depend on firmware BDFs such as 03:00 or 64:00.
live_topology=0
if resolve_egpu_topology 2>/dev/null; then
    live_topology=1
fi

# An unbound live endpoint without the runtime reserve marker is the known
# post-cable-reconnect state. Do not offer a risky live ReBAR/bridge rebuild;
# make the required recovery explicit in the tray immediately after install.
if (( live_topology )) && ! grep -q '^nvidia ' /proc/modules &&
   [[ -e ${INSTALL_DIR}/enable-local-reserve &&
      ! -s /run/egpu-local-reserve-applied ]]; then
    install -D -m 0644 /dev/null /run/egpu-nvidia-reboot-required
fi

# Preserve an NVIDIA-first setting only when NVIDIA DRM is live. Installing an
# update during an AMD-only or quarantined session must not create a stale DRM
# card path for the next login.
if (( live_topology )) && grep -q '^nvidia_drm ' /proc/modules &&
   [[ -d "/sys/bus/pci/devices/${GPU}/drm" && -d "/sys/bus/pci/devices/${IGPU}/drm" ]]; then
    egpu_card="$(find "/sys/bus/pci/devices/${GPU}/drm" -mindepth 1 -maxdepth 1 -name 'card[0-9]*' -printf '/dev/dri/%f\n' | head -n 1)"
    igpu_card="$(find "/sys/bus/pci/devices/${IGPU}/drm" -mindepth 1 -maxdepth 1 -name 'card[0-9]*' -printf '/dev/dri/%f\n' | head -n 1)"
    if [[ -n ${egpu_card} && -c ${egpu_card} && -n ${igpu_card} && -c ${igpu_card} ]]; then
        tmp_env="$(mktemp /run/10kwin-egpu-install.XXXXXX)"
        printf '%s\n' \
            '# Generated by install-egpu-boot-fix.sh from the live DRM topology.' \
            "KWIN_DRM_DEVICES=${egpu_card}:${igpu_card}" \
            > "${tmp_env}"
        install -D -m 0644 "${tmp_env}" "${KWIN_ENV}"
        rm -f -- "${tmp_env}"
    else
        rm -f -- "${KWIN_ENV}"
    fi
else
    rm -f -- "${KWIN_ENV}"
fi
rm -f -- "/etc/environment.d/10kwin-egpu-test.conf"

systemctl daemon-reload
udevadm control --reload-rules
# Seed the exact-enclosure udev database tag for a router that was already
# connected before this installer update. This synthetic change does not
# alter authorization, PCI resources or the live NVIDIA stack.
if th5p4_router_present; then
    th5p4_router="$(find_th5p4_router_dir)"
    # Wait only for this synthetic event. A global `udevadm settle` can time
    # out behind unrelated long-running input/device jobs and must never turn
    # successful installation into a false failure. A coldplug on the next
    # boot will seed the same tag even if this best-effort refresh fails.
    if ! udevadm trigger --action=change --settle "${th5p4_router}"; then
        echo "Warning: the live ${ENCLOSURE_DISPLAY_NAME} udev tag refresh timed out; next boot will seed it normally." >&2
    fi
fi
systemctl reset-failed \
    egpu-nvidia-detach.service \
    egpu-nvidia-hot-attach.service \
    egpu-nvidia-quarantine.service \
    2>/dev/null || true
systemctl enable "${SERVICE}"

# Applying the installer during a working NVIDIA session must not reload the
# driver or retrain the link. It is nevertheless safe to enforce the two power
# attributes that the installed udev rule will own on future events.
if (( live_topology )); then
    for device in "${GPU}" "${AUDIO}"; do
        sysfs="/sys/bus/pci/devices/${device}"
        [[ -w "${sysfs}/power/control" ]] && echo on > "${sysfs}/power/control"
        [[ -w "${sysfs}/d3cold_allowed" ]] && echo 0 > "${sysfs}/d3cold_allowed"
    done
fi

echo "Installed ${SERVICE} for profile: ${PROFILE_NAME}"
echo "It stages ${EGPU_DISPLAY_NAME} after ${ENCLOSURE_DISPLAY_NAME} appears and uses Gen3 unless the guarded Gen4 latch is armed."
if [[ ${HP_DOCK_SUPPORT} == 1 ]]; then
    echo "A cold-attached ${DOCK_DISPLAY_NAME} keeps its USB4 router authorized while only its exact PCI subtree is rebuilt."
fi
echo "Thunderbolt deauthorization is never used. The KWin eGPU pin is removed automatically when the eGPU is absent."
echo "Installed the NVIDIA eGPU System Tray widget and its exact detach/reattach Polkit rule."
