#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
APPLY="${SCRIPT_DIR}/egpu-local-reserve-apply.sh"

# shellcheck source=egpu-pci-lib.sh
source "${SCRIPT_DIR}/egpu-pci-lib.sh"

dry_run=0
if [[ ${1:-} == "--dry-run" ]]; then
    dry_run=1
elif (($#)); then
    echo "Usage: $0 [--dry-run]" >&2
    exit 2
fi

die() {
    echo "COLD HP PCI REBUILD FAILED: $*" >&2
    exit 1
}

if (( ! dry_run )) && [[ ${EUID} -ne 0 ]]; then
    die "run this script as root"
fi
if (( ! dry_run )) && [[ ${EGPU_LOCAL_RESERVE_BOOT_CONTEXT:-0} != 1 ]]; then
    die "refusing PCI removal outside the controlled pre-NVIDIA boot path"
fi
if (( ! dry_run )) && systemctl is-active --quiet display-manager.service; then
    die "the display manager is already active"
fi
if (( ! dry_run )) && grep -q '^nvidia' /proc/modules; then
    die "NVIDIA is already loaded"
fi

resolve_egpu_topology
validate_expected_topology || die "live BDF layout differs from the configured profile"
[[ ${HP_DOCK_SUPPORT} == 1 ]] || die "downstream dock support is disabled in the profile"
hp_router="$(find_hp_dock_router_dir)" || die "the exact ${DOCK_DISPLAY_NAME} router is absent or ambiguous"
[[ $(cat -- "${hp_router}/authorized" 2>/dev/null || true) == 1 ]] ||
    die "the dock router is not in the expected authorized cold-boot state"

bridge_bus_prefix="${BRIDGE%:*}"
PORT1="${bridge_bus_prefix}:01.0"
PORT2="${bridge_bus_prefix}:02.0"
PORT3="${bridge_bus_prefix}:03.0"

for port in "${PORT1}" "${PORT2}" "${PORT3}"; do
    [[ $(pci_id_at "${port}" 2>/dev/null || true) == "${TH5P4_VENDOR}:${TH5P4_DEVICE}" ]] ||
        die "expected TH5P4 downstream bridge ${port} is absent"
done

NIC="$(find_unique_pci_device "${HP_DOCK_NIC_VENDOR}" "${HP_DOCK_NIC_DEVICE}" 2>/dev/null || true)"
[[ -n ${NIC} ]] || die "the exact HP I225 endpoint is absent or ambiguous"
[[ $(<"/sys/bus/pci/devices/${NIC}/subsystem_vendor") == "${HP_DOCK_NIC_SUBSYSTEM_VENDOR}" &&
   $(<"/sys/bus/pci/devices/${NIC}/subsystem_device") == "${HP_DOCK_NIC_SUBSYSTEM_DEVICE}" ]] ||
    die "${NIC} is not the validated HP Dock G4 NIC subsystem"
is_pci_descendant_of "${NIC}" "${PORT1}" || die "${NIC} is not below ${PORT1}"

# Accept exactly the measured HP Dock G4 PCI tree: one upstream and five
# downstream Goshen Ridge bridges plus the single HP I225 endpoint.  Rejecting
# any additional endpoint is essential because the next write removes this
# complete subtree from Linux's PCI model.
descendants=()
hp_bridges=()
for sysfs in /sys/bus/pci/devices/0000:*; do
    bdf=${sysfs##*/}
    [[ ${bdf} == "${PORT1}" ]] && continue
    if is_pci_descendant_of "${bdf}" "${PORT1}"; then
        descendants+=("${bdf}")
        id="$(pci_id_at "${bdf}" 2>/dev/null || true)"
        if [[ ${id} == "${HP_DOCK_BRIDGE_VENDOR}:${HP_DOCK_BRIDGE_DEVICE}" ]]; then
            hp_bridges+=("${bdf}")
        elif [[ ${bdf} != "${NIC}" ]]; then
            die "unexpected HP-port PCI function ${bdf} (${id:-unknown})"
        fi
    fi
done

((${#descendants[@]} == HP_DOCK_EXPECTED_DESCENDANTS)) ||
    die "expected ${HP_DOCK_EXPECTED_DESCENDANTS} dock PCI descendants below ${PORT1}, found ${#descendants[@]}: ${descendants[*]:-none}"
((${#hp_bridges[@]} == HP_DOCK_EXPECTED_BRIDGES)) ||
    die "expected ${HP_DOCK_EXPECTED_BRIDGES} dock bridges, found ${#hp_bridges[@]}"

HP_SWITCH=""
for bridge in "${hp_bridges[@]}"; do
    parent="$(basename -- "$(dirname -- "$(readlink -f "/sys/bus/pci/devices/${bridge}")")")"
    if [[ ${parent} == "${PORT1}" ]]; then
        [[ -z ${HP_SWITCH} ]] || die "more than one direct PCI child exists below ${PORT1}"
        HP_SWITCH=${bridge}
    fi
done
[[ -n ${HP_SWITCH} ]] || die "could not resolve the HP upstream bridge directly below ${PORT1}"

for port in "${PORT2}" "${PORT3}"; do
    for sysfs in /sys/bus/pci/devices/0000:*; do
        bdf=${sysfs##*/}
        [[ ${bdf} == "${port}" ]] && continue
        ! is_pci_descendant_of "${bdf}" "${port}" ||
            die "unused TH5P4 port ${port} contains ${bdf}"
    done
done

printf '%s\n' \
    "Validated the exact cold-attached ${DOCK_DISPLAY_NAME} PCI subtree." \
    "Removing ${HP_SWITCH} and its validated descendants from Linux's PCI model only..." \
    "The Thunderbolt router remains authorized and is not reset."

if (( dry_run )); then
    printf '%s\n' \
        "DRY RUN COMPLETE: ${HP_SWITCH} and all ${HP_DOCK_EXPECTED_DESCENDANTS} exact descendants passed validation." \
        "No driver, PCI device, bridge register or Thunderbolt authorization state was changed."
    exit 0
fi

echo 1 > "/sys/bus/pci/devices/${HP_SWITCH}/remove"
for _ in {1..40}; do
    [[ ! -d /sys/bus/pci/devices/${HP_SWITCH} ]] && break
    sleep 0.05
done
[[ ! -d /sys/bus/pci/devices/${HP_SWITCH} ]] || die "${HP_SWITCH} did not leave the PCI model"

for bdf in "${descendants[@]}"; do
    [[ ! -d /sys/bus/pci/devices/${bdf} ]] || die "HP descendant ${bdf} remained after removal"
done

# Reuse the already proven TH5P4-only bridge programming.  Its rescan now
# imports both RTX and the still-connected HP tunnel into the reserved ranges.
EGPU_COLD_HP_PCI_REBUILD=1 "${APPLY}"
