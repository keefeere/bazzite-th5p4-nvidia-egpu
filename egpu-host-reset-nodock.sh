#!/usr/bin/env bash
set -Eeuo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/egpu-pci-lib.sh"
source "${SCRIPT_DIR}/egpu-kernel-compat.sh"
mode=${1:---check}
[[ $# -le 1 && ( ${mode} == --prepare || ${mode} == --verify || ${mode} == --check ) ]] || exit 2
die() { echo "NO-DOCK HOST RESET TEST FAILED: $*" >&2; exit 1; }
[[ $(egpu_kernel_compat_mode "$(uname -r)") == hotplug-size ]] || die 'requires Linux 7.2+'
cmdline=$(</proc/cmdline)
egpu_cmdline_has_arg "${cmdline}" "${EGPU_PCI_HOTPLUG_KARG}" || die 'missing unchanged hotplug-size argument'
! egpu_cmdline_has_pci_option "${cmdline}" realloc || die 'realloc not allowed'
! egpu_cmdline_has_pci_option "${cmdline}" assign-busses || die 'global renumbering not allowed'
egpu_host_reset_nodock_test_active "$(</proc/cmdline)" \
    "$(cat /sys/module/thunderbolt/parameters/host_reset 2>/dev/null || true)" || die 'no exact active no-dock A/B boot'
hp_dock_router_present && die 'HP must be physically disconnected for this experiment'
resolve_egpu_topology
validate_expected_topology || die 'identity/ancestry/BDF differs from profile'
ports=("${BRIDGE%:*}:01.0" "${BRIDGE%:*}:02.0" "${BRIDGE%:*}:03.0")
if [[ ${mode} == --prepare ]]; then
    [[ ${EUID} == 0 && ${EGPU_LOCAL_RESERVE_BOOT_CONTEXT:-0} == 1 ]] || die 'early boot only'
    ! systemctl is-active --quiet display-manager.service || die 'display manager active'
    ! grep -q '^nvidia' /proc/modules || die 'NVIDIA already loaded'
    [[ ! -L /sys/bus/pci/devices/${GPU}/driver ]] || die 'GPU driver already bound'
fi
python3 "${SCRIPT_DIR}/egpu-existing-resources.py" \
    --root "${ROOT_PORT}" --upstream "${UPSTREAM}" --bridge "${BRIDGE}" \
    --gpu "${GPU}" --audio "${AUDIO}" --bridge-id "${TH5P4_VENDOR}:${TH5P4_DEVICE}" \
    --bar-sizes "${EGPU_BAR0_SIZE}" "${EGPU_BAR1_SIZE}" "${EGPU_BAR3_SIZE}" \
        "${EGPU_BAR5_SIZE}" "${EGPU_AUDIO_BAR0_SIZE}"
# Keep all downstream PCIe ports unavailable: these are existing allocations,
# NOT hot-add reservations. No rescan, bridge programming or BAR resizing.
for port in "${ports[@]}"; do
    service="${port}:pcie204"
    path="/sys/bus/pci_express/devices/${service}"
    [[ -d ${path} ]] || die "missing PCIe hotplug service ${service}"
    if [[ -L ${path}/driver ]]; then
        [[ $(basename -- "$(readlink -f "${path}/driver")") == pciehp ]] || die 'unexpected port service driver'
        [[ ${mode} != --verify ]] || die "${port} is not quarantined"
    fi
done
if [[ ${mode} == --prepare ]]; then
    for port in "${ports[@]}"; do
        service="${port}:pcie204"
        if [[ -L /sys/bus/pci_express/devices/${service}/driver ]]; then
            echo "${service}" > /sys/bus/pci_express/drivers/pciehp/unbind
        fi
        [[ ! -L /sys/bus/pci_express/devices/${service}/driver ]] || die "${port} quarantine failed"
    done
    # Recheck after the only mutation (hotplug-service quarantine).
    "${BASH_SOURCE[0]}" --verify
    printf '%s\n' 'TH5P4 no-dock host_reset A/B: existing PCI resources verified; no reserve/reallocation performed.' > /run/egpu-local-reserve-applied
    printf '%s\n' 'No-dock host_reset A/B: all three empty TH5P4 PCIe hotplug ports quarantined until reboot. Do not attach HP.' > /run/egpu-pciehp-quarantined
fi
