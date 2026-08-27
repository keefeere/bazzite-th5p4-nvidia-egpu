#!/usr/bin/env bash
set -Eeuo pipefail

SOURCE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="/etc/egpu-nvidia"
PCI_REALLOC_OWNER_MARKER="${INSTALL_DIR}/managed-pci-realloc"

# shellcheck source=egpu-kernel-compat.sh
source "${SOURCE_DIR}/egpu-kernel-compat.sh"

profile_source=""
case ${1:-} in
    "") ;;
    --config)
        [[ $# == 2 ]] || { echo "Usage: $0 [--config FILE]" >&2; exit 2; }
        profile_source=$2
        ;;
    -h|--help)
        echo "Usage: $0 [--config FILE]"
        echo "Without --config, an existing /etc/egpu-nvidia/hardware.conf is preserved; otherwise the bundled profile is installed."
        exit 0
        ;;
    *) echo "Usage: $0 [--config FILE]" >&2; exit 2 ;;
esac
if [[ ${EUID} -ne 0 ]]; then
    echo "Run this installer as root (sudo)." >&2
    exit 1
fi
if [[ -n ${profile_source} ]]; then
    [[ -r ${profile_source} ]] || { echo "Hardware profile is not readable: ${profile_source}" >&2; exit 1; }
    export EGPU_PROFILE_SOURCE="$(readlink -f "${profile_source}")"
fi

candidate_profile="${EGPU_PROFILE_SOURCE:-}"
if [[ -z ${candidate_profile} ]]; then
    if [[ -r ${INSTALL_DIR}/hardware.conf ]]; then
        candidate_profile="${INSTALL_DIR}/hardware.conf"
    else
        candidate_profile="${SOURCE_DIR}/hardware.conf"
    fi
fi
EGPU_CONFIG_FILE="${candidate_profile}" "${SOURCE_DIR}/egpu-config-preflight.sh"

reject_global_pci_experiment() {
    local args

    args=" $(</proc/cmdline) "
    if [[ ${args} == *' pci=assign-busses'* ||
          ${args} == *'hpbussize='* ||
          ${args} == *'hpmmiosize='* ||
          ${args} == *'hpmmioprefsize='* ]]; then
        echo "A retired global PCI reservation argument is active." >&2
        echo "Rollback that deployment before installing the final local TH5P4 solution." >&2
        exit 1
    fi

    args=" $(rpm-ostree kargs 2>/dev/null || true) "
    if [[ ${args} == *' pci=assign-busses'* ||
          ${args} == *'hpbussize='* ||
          ${args} == *'hpmmiosize='* ||
          ${args} == *'hpmmioprefsize='* ]]; then
        echo "A retired global PCI reservation argument is staged in rpm-ostree." >&2
        echo "Run remove-pci-hotplug-reserve.sh first." >&2
        exit 1
    fi
}

regenerate_initramfs_if_needed() {
    local changed=$1
    local enabled
    local arg
    local -a current_args=()
    local -a command=(rpm-ostree initramfs --enable)

    enabled="$(rpm-ostree status --json | jq -r '.deployments[] | select(.booted == true) | .["regenerate-initramfs"] // false')"
    mapfile -t current_args < <(
        rpm-ostree status --json |
            jq -r '.deployments[] | select(.booted == true) | .["initramfs-args"][]?'
    )

    if [[ ${enabled} == true && ${changed} == 0 ]]; then
        echo "Local initramfs regeneration is already enabled; its tested configuration is unchanged."
        return
    fi

    for arg in "${current_args[@]}"; do
        command+=("--arg=${arg}")
    done
    "${command[@]}"
}

reject_global_pci_experiment

initramfs_config_changed=0
for source in 99-nvidia.conf zz-egpu-delay.conf; do
    if ! cmp -s -- "${SOURCE_DIR}/${source}" "/etc/dracut.conf.d/${source}"; then
        initramfs_config_changed=1
    fi
done

install -D -m 0644 "${SOURCE_DIR}/99-nvidia.conf" "/etc/dracut.conf.d/99-nvidia.conf"
install -D -m 0644 "${SOURCE_DIR}/zz-egpu-delay.conf" "/etc/dracut.conf.d/zz-egpu-delay.conf"
install -D -m 0644 "${SOURCE_DIR}/99-egpu-delay-nvidia.conf" "/etc/modprobe.d/99-egpu-delay-nvidia.conf"

# These two arguments prevent controller resets/low-power transitions that
# were correlated with the original USB4/eGPU instability. Linux 7.2+ also
# needs its standard PCI realloc pass enabled so the controlled TH5P4 rescan
# retains the locally programmed bridge windows. Older kernels deliberately
# keep the previously validated no-realloc path.
kernel_compat_mode=$(egpu_kernel_compat_mode "$(uname -r)") || {
    echo "Unsupported kernel release format: $(uname -r)" >&2
    exit 1
}
current_kargs=" $(rpm-ostree kargs) "
kargs_command=(rpm-ostree kargs)
manage_pci_realloc=""
for arg in thunderbolt.host_reset=0 thunderbolt.clx=0; do
    if [[ ${current_kargs} != *" ${arg} "* ]]; then
        kargs_command+=("--append-if-missing=${arg}")
    fi
done
managed_pci_realloc=0
[[ -e ${PCI_REALLOC_OWNER_MARKER} ]] && managed_pci_realloc=1
case $(egpu_managed_pci_realloc_action "$(uname -r)" "${current_kargs}" "${managed_pci_realloc}") in
    add)
        kargs_command+=("--append-if-missing=${EGPU_PCI_REALLOC_KARG}")
        manage_pci_realloc=add
        ;;
    remove)
        kargs_command+=("--delete-if-present=${EGPU_PCI_REALLOC_KARG}")
        manage_pci_realloc=remove
        ;;
    keep) ;;
    *) echo "Could not select a kernel PCI compatibility action." >&2; exit 1 ;;
esac
if ((${#kargs_command[@]} > 2)); then
    "${kargs_command[@]}"
else
    echo "The tested kernel arguments are already staged for ${kernel_compat_mode} PCI compatibility mode."
fi
if [[ ${manage_pci_realloc} == add ]]; then
    install -D -m 0644 /dev/null "${PCI_REALLOC_OWNER_MARKER}"
    echo "Staged ${EGPU_PCI_REALLOC_KARG} for Linux ${EGPU_PCI_REALLOC_MIN_KERNEL}+; this stack owns the exact argument."
elif [[ ${manage_pci_realloc} == remove ]]; then
    rm -f -- "${PCI_REALLOC_OWNER_MARKER}"
    echo "Removed the stack-managed ${EGPU_PCI_REALLOC_KARG}; the pre-${EGPU_PCI_REALLOC_MIN_KERNEL} flow remains unchanged."
elif [[ ${kernel_compat_mode} == realloc ]]; then
    if [[ -e ${PCI_REALLOC_OWNER_MARKER} ]]; then
        echo "Stack-managed ${EGPU_PCI_REALLOC_KARG} is already staged for Linux ${EGPU_PCI_REALLOC_MIN_KERNEL}+."
    else
        echo "Existing user-managed ${EGPU_PCI_REALLOC_KARG} satisfies Linux ${EGPU_PCI_REALLOC_MIN_KERNEL}+ compatibility."
    fi
else
    echo "Using the unchanged legacy PCI flow on kernel $(uname -r)."
fi

regenerate_initramfs_if_needed "${initramfs_config_changed}"

"${SOURCE_DIR}/install-egpu-boot-fix.sh"
# Load the installed, validated profile for the remaining messages/actions.
EGPU_CONFIG_FILE="${INSTALL_DIR}/hardware.conf"
# shellcheck source=egpu-pci-lib.sh
source "${SOURCE_DIR}/egpu-pci-lib.sh"
install -D -m 0644 /dev/null "${INSTALL_DIR}/enable-local-reserve"
"${SOURCE_DIR}/enable-egpu-gen4.sh"
systemctl mask --now ublue-nvctk-cdi.service

echo
echo "Final NVIDIA eGPU stack installed for: ${PROFILE_NAME}"
echo "  - local ${ENCLOSURE_DISPLAY_NAME} bus/window reservation: enabled"
if [[ ${HP_DOCK_SUPPORT} == 1 ]]; then
    echo "  - cold-attached ${DOCK_DISPLAY_NAME} PCI-only rebuild: enabled"
fi
echo "  - guarded persistent PCIe Gen4 x4: armed"
echo "  - NVIDIA-first KWin order and safe-detach widget: installed"
echo "  - automatic NVIDIA loading and ublue-nvctk-cdi: blocked"
echo "  - kernel PCI compatibility mode: ${kernel_compat_mode}"
echo
echo "No global PCI bus renumbering is used. Thunderbolt authorization is never cycled."
echo "Use a warm reboot with the validated chain left powered and connected."
echo "After login: ${INSTALL_DIR}/verify-egpu-install.sh"
echo "Full rollback: ${SOURCE_DIR}/remove-egpu-all.sh"
