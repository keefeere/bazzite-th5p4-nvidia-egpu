#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

# shellcheck source=egpu-pci-lib.sh
source "${SCRIPT_DIR}/egpu-pci-lib.sh"

MIN_BUSES=${MIN_BUSES_PER_PORT}
MIN_IO=${MIN_IO_BYTES}
MIN_MMIO=${MIN_MMIO_BYTES}
MIN_PREF=${MIN_PREF_BYTES}
TARGET_MMIO=${TARGET_MMIO_BYTES}
TARGET_PREF=${TARGET_PREF_BYTES}

die() {
    echo "LOCAL RESERVE PREFLIGHT FAILED: $*" >&2
    exit 1
}

read_bridge_window() {
    local bdf=$1
    local line=$2
    local __start=$3
    local __end=$4
    local __flags=$5
    local start end flags

    read -r start end flags < <(sed -n "${line}p" "/sys/bus/pci/devices/${bdf}/resource")
    printf -v "${__start}" '%d' "${start}"
    printf -v "${__end}" '%d' "${end}"
    printf -v "${__flags}" '%d' "${flags}"
}

align_up() {
    local value=$1
    local alignment=$2
    echo $((((value + alignment - 1) / alignment) * alignment))
}

human_size() {
    local bytes=$1
    if ((bytes >= 1024 * 1024 * 1024)); then
        awk -v bytes="${bytes}" 'BEGIN { printf "%.3f GiB", bytes / 1073741824 }'
    elif ((bytes >= 1024 * 1024)); then
        awk -v bytes="${bytes}" 'BEGIN { printf "%.0f MiB", bytes / 1048576 }'
    else
        awk -v bytes="${bytes}" 'BEGIN { printf "%.0f KiB", bytes / 1024 }'
    fi
}

resolve_egpu_topology
validate_expected_topology || die "live BDF layout differs from the configured profile"

if [[ " $(</proc/cmdline) " == *' pci=assign-busses'* ]]; then
    die "pci=assign-busses is active; cold-boot the clean rollback deployment first"
fi

if [[ ${HP_DOCK_SUPPORT} == 1 ]] && hp_dock_router_present; then
    dock_dir="$(find_hp_dock_router_dir)"
    dock_authorized=$(cat -- "${dock_dir}/authorized" 2>/dev/null || true)
    if [[ ${dock_authorized} == 0 ]]; then
        echo "Note: ${DOCK_DISPLAY_NAME} is physically present but its PCIe tunnel is not authorized."
    elif [[ ${dock_authorized} == 1 && ${EGPU_COLD_HP_PCI_REBUILD:-0} == 1 ]]; then
        echo "Note: ${DOCK_DISPLAY_NAME} router remains authorized; its validated PCI subtree was removed without resetting USB4."
    else
        die "${DOCK_DISPLAY_NAME} is already attached and its PCIe tunnel is authorized"
    fi
fi

bridge_bus_prefix="${BRIDGE%:*}"
PORTS=(
    "${bridge_bus_prefix}:01.0"
    "${bridge_bus_prefix}:02.0"
    "${bridge_bus_prefix}:03.0"
)

for port in "${PORTS[@]}"; do
    [[ $(pci_id_at "${port}" 2>/dev/null || true) == "${TH5P4_VENDOR}:${TH5P4_DEVICE}" ]] ||
        die "expected an empty TH5P4 downstream bridge at ${port}"
    for child in "/sys/bus/pci/devices/${port}"/0000:*; do
        child_name=${child##*/}
        if [[ -e ${child} && ${child_name} =~ ^0000:[[:xdigit:]]{2}:[[:xdigit:]]{2}\.[0-7]$ ]]; then
            die "${port} is not empty (${child_name} is present)"
        fi
    done
done

root_secondary=$(<"/sys/bus/pci/devices/${ROOT_PORT}/secondary_bus_number")
root_subordinate=$(<"/sys/bus/pci/devices/${ROOT_PORT}/subordinate_bus_number")
upstream_secondary=$(<"/sys/bus/pci/devices/${UPSTREAM}/secondary_bus_number")
gpu_secondary=$(<"/sys/bus/pci/devices/${BRIDGE}/secondary_bus_number")

((root_secondary == ROOT_BUS_SECONDARY)) || die "USB4 root secondary bus differs from the profile"
((root_subordinate == ROOT_BUS_SUBORDINATE)) || die "USB4 root subordinate bus differs from the profile"
((upstream_secondary == UPSTREAM_BUS_SECONDARY)) || die "enclosure upstream secondary bus differs from the profile"
((gpu_secondary == GPU_BUS_SECONDARY)) || die "eGPU branch secondary bus differs from the profile"
((root_subordinate >= GPU_BUS_SECONDARY + MIN_BUSES * ${#PORTS[@]})) ||
    die "the root port does not contain enough free bus numbers"

# Nothing else may already use the range that firmware owns under this root
# port but leaves unenumerated behind the TH5P4 (07 through root subordinate).
for sysfs in /sys/bus/pci/devices/0000:*; do
    bdf=${sysfs##*/}
    bus_hex=${bdf#0000:}
    bus_hex=${bus_hex%%:*}
    bus=$((16#${bus_hex}))
    if ((bus >= FIRMWARE_UNUSED_FIRST_BUS && bus <= root_subordinate)); then
        die "bus $(printf '%02x' "${bus}") is unexpectedly occupied by ${bdf}"
    fi
done

# For these PCI bridges, sysfs resource lines 14, 15 and 16 are I/O,
# non-prefetchable MMIO and prefetchable MMIO respectively.
read_bridge_window "${ROOT_PORT}" 14 root_io_start root_io_end root_io_flags
read_bridge_window "${ROOT_PORT}" 15 root_mem_start root_mem_end root_mem_flags
read_bridge_window "${ROOT_PORT}" 16 root_pref_start root_pref_end root_pref_flags
read_bridge_window "${BRIDGE}" 14 gpu_io_start gpu_io_end gpu_io_flags
read_bridge_window "${BRIDGE}" 15 gpu_mem_start gpu_mem_end gpu_mem_flags
read_bridge_window "${BRIDGE}" 16 gpu_pref_start gpu_pref_end gpu_pref_flags

for flags in root_io_flags root_mem_flags root_pref_flags gpu_io_flags gpu_mem_flags gpu_pref_flags; do
    ((${!flags} != 0)) || die "required bridge window ${flags%_flags} is disabled"
done

# This is intentionally machine-specific. Refuse to derive a new write plan if
# firmware, a kernel update or another device has changed the layout that was
# measured on this XAX. A fresh design review is safer than adapting silently.
((root_io_start == ROOT_IO_START && root_io_end == ROOT_IO_END)) ||
    die "USB4 root I/O window differs from the configured profile"
((root_mem_start == ROOT_MMIO_START && root_mem_end == ROOT_MMIO_END)) ||
    die "USB4 root MMIO window differs from the configured profile"
((root_pref_start == ROOT_PREF_START && root_pref_end == ROOT_PREF_END)) ||
    die "USB4 root MMIO_PREF window differs from the configured profile"
((gpu_io_start == GPU_IO_START && gpu_io_end == GPU_IO_END)) ||
    die "eGPU branch I/O window differs from the configured profile"
((gpu_mem_start == GPU_MMIO_START && gpu_mem_end == GPU_MMIO_END)) ||
    die "eGPU branch MMIO window differs from the configured profile"
((gpu_pref_start == GPU_PREF_START && gpu_pref_end == GPU_PREF_END)) ||
    die "eGPU branch MMIO_PREF window differs from the configured profile"

((gpu_io_start == root_io_start && gpu_io_end < root_io_end)) ||
    die "RTX I/O window is not the first child of the AMD root window"
((gpu_mem_start == root_mem_start && gpu_mem_end < root_mem_end)) ||
    die "RTX MMIO window is not the first child of the AMD root window"
((gpu_pref_start == root_pref_start && gpu_pref_end < root_pref_end)) ||
    die "RTX MMIO_PREF window is not the first child of the AMD root window"

reserve_io_start=$(align_up "$((gpu_io_end + 1))" 4096)
reserve_io_end=${root_io_end}
reserve_mem_start=$(align_up "$((gpu_mem_end + 1))" $((16 * 1024 * 1024)))
reserve_mem_end=$((reserve_mem_start + TARGET_MMIO - 1))
reserve_pref_start=$(align_up "$((gpu_pref_end + 1))" $((32 * 1024 * 1024)))
reserve_pref_end=$((reserve_pref_start + TARGET_PREF - 1))

((reserve_mem_end <= root_mem_end)) || die "the proposed 128 MiB MMIO window does not fit"
((reserve_pref_end <= root_pref_end)) || die "the proposed 1 GiB MMIO_PREF window does not fit"

reserve_io_size=$((reserve_io_end - reserve_io_start + 1))
reserve_mem_size=$((reserve_mem_end - reserve_mem_start + 1))
reserve_pref_size=$((reserve_pref_end - reserve_pref_start + 1))

((reserve_io_size >= MIN_IO)) || die "only $(human_size "${reserve_io_size}") I/O is free"
((reserve_mem_size >= MIN_MMIO)) || die "only $(human_size "${reserve_mem_size}") MMIO is free"
((reserve_pref_size >= MIN_PREF)) || die "only $(human_size "${reserve_pref_size}") MMIO_PREF is free"

first_free_bus=$((gpu_secondary + 1))
available_buses=$((root_subordinate - first_free_bus + 1))
buses_per_port=$((available_buses / ${#PORTS[@]}))
((buses_per_port >= MIN_BUSES)) || die "only ${buses_per_port} buses are available per TH5P4 port"
((first_free_bus == PORT1_BUS_START &&
  first_free_bus + buses_per_port - 1 == PORT1_BUS_END &&
  first_free_bus + buses_per_port == PORT2_BUS_START &&
  first_free_bus + buses_per_port * 2 - 1 == PORT2_BUS_END &&
  first_free_bus + buses_per_port * 2 == PORT3_BUS_START &&
  root_subordinate == PORT3_BUS_END)) || die "derived port ranges differ from the configured profile"

printf '%s\n' \
    "LOCAL TH5P4 RESERVE PREFLIGHT PASSED (read-only; nothing was changed)." \
    "" \
    "Validated path:" \
    "  AMD root port: ${ROOT_PORT} [bus $(printf '%02x' "${root_secondary}")-$(printf '%02x' "${root_subordinate}")]" \
    "  TH5P4 upstream: ${UPSTREAM}" \
    "  RTX branch:    ${BRIDGE} -> ${GPU}" \
    "" \
    "Proposed bus map after a controlled TH5P4-only remove/rescan:"

bus_start=${first_free_bus}
for port in "${PORTS[@]}"; do
    bus_end=$((bus_start + buses_per_port - 1))
    printf '  %s -> [bus %02x-%02x] (%d buses)\n' \
        "${port}" "${bus_start}" "${bus_end}" "${buses_per_port}"
    bus_start=$((bus_end + 1))
done

printf '%s\n' \
    "" \
    "Proposed windows for the HP Dock port ${PORTS[0]}:" \
    "  I/O:       $(printf '0x%x-0x%x' "${reserve_io_start}" "${reserve_io_end}") ($(human_size "${reserve_io_size}"))" \
    "  MMIO:      $(printf '0x%x-0x%x' "${reserve_mem_start}" "${reserve_mem_end}") ($(human_size "${reserve_mem_size}"))" \
    "  MMIO_PREF: $(printf '0x%x-0x%x' "${reserve_pref_start}" "${reserve_pref_end}") ($(human_size "${reserve_pref_size}"))" \
    "" \
    "The next phase may program only the TH5P4 bridges, remove/rescan that PCIe subtree," \
    "and verify the kernel resource tree before NVIDIA is allowed to bind."
