#!/usr/bin/env bash
set -Eeuo pipefail

if [[ ${EUID} -ne 0 ]]; then
    echo "Run this script as root." >&2
    exit 1
fi

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
APPLIED_MARKER="/run/egpu-local-reserve-applied"
REBOOT_MARKER="/run/egpu-nvidia-reboot-required"
ports_removed=0

# shellcheck source=egpu-pci-lib.sh
source "${SCRIPT_DIR}/egpu-pci-lib.sh"
# shellcheck source=egpu-kernel-compat.sh
source "${SCRIPT_DIR}/egpu-kernel-compat.sh"

die() {
    echo "COLD HP DYNAMIC REBAR FAILED: $*" >&2
    exit 1
}

pci_resource_size() {
    local bdf=$1 resource_number=$2
    local start end flags

    read -r start end flags < <(
        sed -n "$((resource_number + 1))p" "/sys/bus/pci/devices/${bdf}/resource"
    )
    ((flags != 0 && end >= start)) || return 1
    printf '%d\n' "$((end - start + 1))"
}

pci_branch_has_descendants() {
    local ancestor=$1 sysfs bdf

    for sysfs in /sys/bus/pci/devices/0000:*; do
        bdf=${sysfs##*/}
        [[ ${bdf} == "${ancestor}" ]] && continue
        is_pci_descendant_of "${bdf}" "${ancestor}" && return 0
    done
    return 1
}

recover_on_error() {
    local rc=$?

    trap - EXIT
    install -D -m 0644 /dev/null "${REBOOT_MARKER}" || true
    if ((ports_removed)) && [[ -w /sys/bus/pci/devices/${EXPECTED_TH5P4_UPSTREAM_BDF}/rescan ]]; then
        echo "Rescanning the TH5P4 upstream after an interrupted empty-port transition..." >&2
        echo 1 > "/sys/bus/pci/devices/${EXPECTED_TH5P4_UPSTREAM_BDF}/rescan" || true
    fi
    exit "${rc}"
}
trap recover_on_error EXIT

[[ ${EGPU_DYNAMIC_REBAR_BOOT_CONTEXT:-0} == 1 ]] ||
    die "refusing PCI mutation outside the controlled pre-NVIDIA boot path"
[[ $(egpu_kernel_compat_mode "$(uname -r)") == hotplug-size ]] ||
    die "this path is restricted to Linux ${EGPU_PCI_COMPAT_MIN_KERNEL}+"
systemctl is-active --quiet display-manager.service &&
    die "the display manager is already active"
grep -q '^nvidia ' /proc/modules && die "NVIDIA is already loaded"
[[ -r /sys/module/thunderbolt/parameters/host_reset &&
   $(< /sys/module/thunderbolt/parameters/host_reset) == Y ]] ||
    die "the tested upstream USB4 host reset is not active"

resolve_egpu_topology
validate_expected_topology || die "live BDF layout differs from the configured profile"
[[ ${HP_DOCK_SUPPORT} == 1 ]] || die "downstream dock support is disabled"
hp_dock_router_present || die "the exact ${DOCK_DISPLAY_NAME} router is absent"

# This read-only helper validates the exact HP router, bridge count, NIC
# identity and ancestry. It deliberately performs no removal in dry-run mode.
"${SCRIPT_DIR}/egpu-cold-hp-pci-rebuild.sh" --dry-run

bridge_prefix=${EXPECTED_GPU_BRIDGE_BDF%:*}
PORT2="${bridge_prefix}:02.0"
PORT3="${bridge_prefix}:03.0"
for port in "${PORT2}" "${PORT3}"; do
    [[ $(pci_id_at "${port}" 2>/dev/null || true) == "${TH5P4_VENDOR}:${TH5P4_DEVICE}" ]] ||
        die "expected empty TH5P4 sibling ${port} is absent or changed"
    ! pci_branch_has_descendants "${port}" ||
        die "expected empty TH5P4 sibling ${port} has a descendant"
done

[[ ! -L /sys/bus/pci/devices/${EXPECTED_GPU_BDF}/driver ]] ||
    die "the RTX function unexpectedly has a driver"
[[ $(pci_resource_size "${EXPECTED_GPU_BDF}" 0) == "${EGPU_BAR0_SIZE}" &&
   $(pci_resource_size "${EXPECTED_GPU_BDF}" 3) == "${EGPU_BAR3_SIZE}" &&
   $(pci_resource_size "${EXPECTED_GPU_BDF}" 5) == "${EGPU_BAR5_SIZE}" &&
   $(pci_resource_size "${EXPECTED_AUDIO_BDF}" 0) == "${EGPU_AUDIO_BAR0_SIZE}" ]] ||
    die "the fixed RTX BAR layout differs from the profile"

read -r root_io_start root_io_end root_io_flags < <(
    sed -n '14p' "/sys/bus/pci/devices/${EXPECTED_USB4_ROOT_PORT_BDF}/resource"
)
((root_io_flags != 0 && root_io_end >= root_io_start &&
  root_io_end - root_io_start + 1 >= 32 * 1024)) ||
    die "the reassigned USB4 root I/O aperture is smaller than 32 KiB"
read -r root_mmio_start root_mmio_end root_mmio_flags < <(
    sed -n '15p' "/sys/bus/pci/devices/${EXPECTED_USB4_ROOT_PORT_BDF}/resource"
)
read -r root_pref_start root_pref_end root_pref_flags < <(
    sed -n '16p' "/sys/bus/pci/devices/${EXPECTED_USB4_ROOT_PORT_BDF}/resource"
)
((root_mmio_flags != 0 && root_mmio_start == ROOT_MMIO_START &&
  root_mmio_end == ROOT_MMIO_END)) ||
    die "the USB4 root MMIO aperture differs from the profile"
((root_pref_flags != 0 && root_pref_start == ROOT_PREF_START &&
  root_pref_end == ROOT_PREF_END)) ||
    die "the USB4 root MMIO_PREF aperture differs from the profile"

upstream_secondary=$(< "/sys/bus/pci/devices/${EXPECTED_TH5P4_UPSTREAM_BDF}/secondary_bus_number")
upstream_subordinate=$(< "/sys/bus/pci/devices/${EXPECTED_TH5P4_UPSTREAM_BDF}/subordinate_bus_number")
gpu_secondary=$(< "/sys/bus/pci/devices/${EXPECTED_GPU_BRIDGE_BDF}/secondary_bus_number")
((upstream_secondary == UPSTREAM_BUS_SECONDARY &&
  upstream_subordinate == ROOT_BUS_SUBORDINATE &&
  gpu_secondary == GPU_BUS_SECONDARY)) ||
    die "the TH5P4 bus ranges differ from the profile"

bar1_size=$(pci_resource_size "${EXPECTED_GPU_BDF}" 1)
if [[ ${bar1_size} == "${EGPU_BAR1_SIZE}" ]]; then
    echo "The complete 16 GiB RTX BAR1 is already present; validating the preserved HP branch..."
    "${SCRIPT_DIR}/egpu-cold-attached-hp-verify.sh"
elif [[ ${bar1_size} == $((256 * 1024 * 1024)) ]]; then
    resize_mask=$(< "/sys/bus/pci/devices/${EXPECTED_GPU_BDF}/resource1_resize")
    (((16#${resize_mask}) & (1 << 14))) ||
        die "the RTX does not advertise a 16 GiB BAR1 size"

    audio_driver="$(basename -- "$(readlink -f "/sys/bus/pci/devices/${EXPECTED_AUDIO_BDF}/driver" 2>/dev/null || true)")"
    if [[ ${audio_driver} == snd_hda_intel ]]; then
        echo "${EXPECTED_AUDIO_BDF}" > /sys/bus/pci/drivers/snd_hda_intel/unbind
    elif [[ -n ${audio_driver} ]]; then
        die "RTX audio is unexpectedly bound to ${audio_driver}"
    fi

    echo "Preserving ${DOCK_DISPLAY_NAME} and releasing only two empty TH5P4 sibling ports..."
    echo 1 > "/sys/bus/pci/devices/${PORT2}/remove"
    echo 1 > "/sys/bus/pci/devices/${PORT3}/remove"
    ports_removed=1
    for _ in {1..50}; do
        [[ ! -d /sys/bus/pci/devices/${PORT2} &&
           ! -d /sys/bus/pci/devices/${PORT3} ]] && break
        sleep 0.1
    done
    [[ ! -d /sys/bus/pci/devices/${PORT2} &&
       ! -d /sys/bus/pci/devices/${PORT3} ]] ||
        die "the empty sibling ports did not leave the PCI model"

    echo "Requesting 16 GiB RTX BAR1 through Linux resource1_resize..."
    echo 14 > "/sys/bus/pci/devices/${EXPECTED_GPU_BDF}/resource1_resize"
    [[ $(pci_resource_size "${EXPECTED_GPU_BDF}" 1) == "${EGPU_BAR1_SIZE}" ]] ||
        die "the kernel did not assign the required 16 GiB RTX BAR1"

    echo "Returning the two empty TH5P4 sibling ports..."
    echo 1 > "/sys/bus/pci/devices/${EXPECTED_TH5P4_UPSTREAM_BDF}/rescan"
    for _ in {1..50}; do
        [[ -d /sys/bus/pci/devices/${PORT2} &&
           -d /sys/bus/pci/devices/${PORT3} ]] && break
        sleep 0.1
    done
    [[ -d /sys/bus/pci/devices/${PORT2} &&
       -d /sys/bus/pci/devices/${PORT3} ]] ||
        die "the empty sibling ports did not return"
    ports_removed=0

    resolve_egpu_topology
    validate_expected_topology || die "the exact topology changed after ReBAR resize"
    "${SCRIPT_DIR}/egpu-cold-attached-hp-verify.sh"
else
    die "RTX BAR1 is neither the validated 256 MiB input nor 16 GiB result"
fi

printf '%s\n' \
    "TH5P4 cold-dock dynamic ReBAR repair is active." \
    "The kernel resized RTX BAR1 to 16 GiB while preserving the validated HP PCI branch." \
    > "${APPLIED_MARKER}"
rm -f -- "${REBOOT_MARKER}"
trap - EXIT
echo "The guarded cold-dock ReBAR transition passed before NVIDIA was allowed to bind."
