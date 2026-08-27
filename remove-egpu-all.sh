#!/usr/bin/env bash
set -Eeuo pipefail

if [[ ${EUID} -ne 0 ]]; then
    echo "Run this rollback as root (sudo)." >&2
    exit 1
fi

SOURCE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PLASMOID_ID="com.keefeere.egpu"
INITRAMFS_DRIVER_LIST="nvidia nvidia_drm nvidia_modeset nvidia_peermem nvidia_uvm"
managed_pci_realloc=0
[[ -e /etc/egpu-nvidia/managed-pci-realloc ]] && managed_pci_realloc=1

desktop_home=""
# Rollback must remain possible even if an edited installed profile is broken.
if source "${SOURCE_DIR}/egpu-pci-lib.sh" 2>/dev/null; then
    desktop_home="$(getent passwd "${DESKTOP_UID}" | cut -d: -f6)"
elif [[ -n ${SUDO_USER:-} && ${SUDO_USER} != root ]]; then
    desktop_home="$(getent passwd "${SUDO_USER}" | cut -d: -f6)"
fi

systemctl disable --now egpu-nvidia-boot.service 2>/dev/null || true
systemctl unmask ublue-nvctk-cdi.service 2>/dev/null || true

rm -f -- \
    /etc/systemd/system/egpu-nvidia-boot.service \
    /etc/systemd/system/egpu-nvidia-quarantine.service \
    /etc/systemd/system/egpu-nvidia-detach.service \
    /etc/systemd/system/egpu-nvidia-hot-attach.service \
    /etc/udev/rules.d/99-egpu-nvidia.rules \
    /etc/polkit-1/rules.d/49-egpu-nvidia.rules \
    /etc/modprobe.d/99-egpu-delay-nvidia.conf \
    /etc/environment.d/10kwin-egpu.conf \
    /etc/environment.d/10kwin-egpu-test.conf \
    /etc/dracut.conf.d/99-nvidia.conf \
    /etc/dracut.conf.d/zz-egpu-delay.conf

rm -rf -- /etc/egpu-nvidia
if [[ -n ${desktop_home} ]]; then
    rm -rf -- "${desktop_home}/.local/share/plasma/plasmoids/${PLASMOID_ID}"
fi

systemctl daemon-reload
udevadm control --reload-rules

# Preserve every pre-existing dracut argument (notably the encrypted-root
# configuration) while removing only this setup's exact NVIDIA omission.
mapfile -t current_args < <(
    rpm-ostree status --json |
        jq -r '.deployments[] | select(.booted == true) | .["initramfs-args"][]?'
)
filtered_args=()
for (( index = 0; index < ${#current_args[@]}; index++ )); do
    if [[ ${current_args[index]} == "--omit-drivers" &&
          ${current_args[index + 1]:-} == "${INITRAMFS_DRIVER_LIST}" ]]; then
        index=$((index + 1))
        continue
    fi
    filtered_args+=("${current_args[index]}")
done

initramfs_command=(rpm-ostree initramfs --enable)
for arg in "${filtered_args[@]}"; do
    initramfs_command+=("--arg=${arg}")
done
"${initramfs_command[@]}"

kargs_command=(
    rpm-ostree kargs
    --delete-if-present="thunderbolt.host_reset=0"
    --delete-if-present="thunderbolt.clx=0"
)
if (( managed_pci_realloc )); then
    kargs_command+=(--delete-if-present="pci=realloc=on")
fi
"${kargs_command[@]}"

echo
echo "Removed the custom eGPU services, policy, widget and module/initramfs overrides."
echo "The running NVIDIA stack and PCI topology were not touched."
echo "Reboot into the staged deployment to return to Bazzite's stock NVIDIA behavior."
echo "Source files remain available at ${SOURCE_DIR}."
