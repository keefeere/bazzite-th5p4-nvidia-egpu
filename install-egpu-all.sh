#!/usr/bin/env bash
set -Eeuo pipefail

SOURCE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="/etc/egpu-nvidia"
PCI_REALLOC_OWNER_MARKER="${INSTALL_DIR}/managed-pci-realloc"
PCI_HOTPLUG_OWNER_MARKER="${INSTALL_DIR}/managed-pci-hotplug-size"

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

kernel_compat_mode=$(egpu_kernel_compat_mode "$(uname -r)") || {
    echo "Unsupported kernel release format: $(uname -r)" >&2
    exit 1
}

reject_unsupported_pci_arguments() {
    local active staged label args token option
    local -a options

    active="$(</proc/cmdline)"
    staged="$(rpm-ostree kargs 2>/dev/null || true)"
    for label in active staged; do
        if [[ ${label} == active ]]; then
            args=${active}
        else
            args=${staged}
        fi
        for token in ${args}; do
            [[ ${token} == pci=* ]] || continue
            if [[ ${kernel_compat_mode} == hotplug-size &&
                  ${token} == "${EGPU_PCI_HOTPLUG_KARG}" ]]; then
                continue
            fi
            IFS=, read -r -a options <<< "${token#pci=}"
            for option in "${options[@]}"; do
                case ${option} in
                    assign-busses|hpbussize=*|hpiosize=*|hpmemsize=*|hpmmiosize=*|hpmmioprefsize=*)
                        echo "Unsupported PCI reservation option is ${label}: ${option}" >&2
                        echo "Only ${EGPU_PCI_HOTPLUG_KARG} is accepted on Linux ${EGPU_PCI_COMPAT_MIN_KERNEL}+." >&2
                        exit 1
                        ;;
                esac
            done
        done
    done

    # The stack-owned realloc experiment is allowed only long enough for this
    # installer to migrate it out of the staged deployment. A user-owned copy
    # remains a hard conflict because it repacks the TH5P4 hierarchy at boot.
    if egpu_cmdline_has_arg "${staged}" "${EGPU_PCI_REALLOC_KARG}" &&
       [[ ! -e ${PCI_REALLOC_OWNER_MARKER} ]]; then
        echo "User-managed ${EGPU_PCI_REALLOC_KARG} is staged and conflicts with the local TH5P4 flow." >&2
        exit 1
    fi
    if egpu_cmdline_has_arg "${active}" "${EGPU_PCI_REALLOC_KARG}" &&
       [[ ! -e ${PCI_REALLOC_OWNER_MARKER} ]] &&
       { egpu_cmdline_has_arg "${staged}" "${EGPU_PCI_REALLOC_KARG}" ||
         ! egpu_cmdline_has_arg "${staged}" "${EGPU_PCI_HOTPLUG_KARG}"; }; then
        echo "Active ${EGPU_PCI_REALLOC_KARG} is not a recognized pending migration." >&2
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

reject_unsupported_pci_arguments

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
# needs the measured 32 MiB hot-plug memory policy because its rewritten PCI
# allocator no longer preserves oversized empty bridge windows. Older kernels
# deliberately keep the previously validated flow byte-for-byte unchanged.
current_kargs=" $(rpm-ostree kargs) "
kargs_command=(rpm-ostree kargs)
for arg in thunderbolt.host_reset=0 thunderbolt.clx=0; do
    if [[ ${current_kargs} != *" ${arg} "* ]]; then
        kargs_command+=("--append-if-missing=${arg}")
    fi
done
managed_pci_realloc=0
[[ -e ${PCI_REALLOC_OWNER_MARKER} ]] && managed_pci_realloc=1
manage_pci_realloc=$(egpu_managed_realloc_migration_action \
    "${current_kargs}" "${managed_pci_realloc}") || {
    echo "Could not select the rejected realloc migration action." >&2
    exit 1
}
case ${manage_pci_realloc} in
    remove)
        kargs_command+=("--delete-if-present=${EGPU_PCI_REALLOC_KARG}")
        ;;
    keep|cleanup) ;;
    *) echo "Invalid realloc migration action: ${manage_pci_realloc}" >&2; exit 1 ;;
esac

managed_pci_hotplug=0
[[ -e ${PCI_HOTPLUG_OWNER_MARKER} ]] && managed_pci_hotplug=1
manage_pci_hotplug=$(egpu_managed_hotplug_size_action \
    "$(uname -r)" "${current_kargs}" "${managed_pci_hotplug}") || {
    echo "Could not select the kernel PCI hot-plug sizing action." >&2
    exit 1
}
case ${manage_pci_hotplug} in
    add)
        kargs_command+=("--append-if-missing=${EGPU_PCI_HOTPLUG_KARG}")
        ;;
    remove)
        kargs_command+=("--delete-if-present=${EGPU_PCI_HOTPLUG_KARG}")
        ;;
    keep|cleanup) ;;
    *) echo "Invalid hot-plug sizing action: ${manage_pci_hotplug}" >&2; exit 1 ;;
esac
if ((${#kargs_command[@]} > 2)); then
    "${kargs_command[@]}"
else
    echo "The tested kernel arguments are already staged for ${kernel_compat_mode} PCI compatibility mode."
fi
if [[ ${manage_pci_realloc} == remove || ${manage_pci_realloc} == cleanup ]]; then
    rm -f -- "${PCI_REALLOC_OWNER_MARKER}"
    echo "Removed the rejected stack-managed ${EGPU_PCI_REALLOC_KARG}."
fi
case ${manage_pci_hotplug} in
    add)
        install -D -m 0644 /dev/null "${PCI_HOTPLUG_OWNER_MARKER}"
        echo "Staged ${EGPU_PCI_HOTPLUG_KARG} for Linux ${EGPU_PCI_COMPAT_MIN_KERNEL}+; this stack owns the exact argument."
        ;;
    remove|cleanup)
        rm -f -- "${PCI_HOTPLUG_OWNER_MARKER}"
        echo "Removed the stack-managed hot-plug sizing argument; the legacy flow remains unchanged."
        ;;
    keep)
        if [[ ${kernel_compat_mode} == hotplug-size ]]; then
            if [[ -e ${PCI_HOTPLUG_OWNER_MARKER} ]]; then
                echo "Stack-managed ${EGPU_PCI_HOTPLUG_KARG} is already staged."
            else
                echo "Existing user-managed ${EGPU_PCI_HOTPLUG_KARG} satisfies the Linux 7.2+ compatibility mode."
            fi
        else
            echo "Using the unchanged legacy PCI flow on kernel $(uname -r)."
        fi
        ;;
esac
if egpu_cmdline_has_arg "$(</proc/cmdline)" "${EGPU_PCI_REALLOC_KARG}"; then
    if ! egpu_cmdline_has_arg "$(rpm-ostree kargs)" "${EGPU_PCI_REALLOC_KARG}"; then
        echo "The rejected realloc mode is still active only in this boot; reboot into the staged replacement before verification."
    else
        echo "WARNING: ${EGPU_PCI_REALLOC_KARG} remains staged unexpectedly." >&2
    fi
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
