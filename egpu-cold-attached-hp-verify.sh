#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

# shellcheck source=egpu-pci-lib.sh
source "${SCRIPT_DIR}/egpu-pci-lib.sh"

die() {
    echo "COLD-ATTACHED HP DOCK VERIFY FAILED: $*" >&2
    exit 1
}

read_window() {
    local bdf=$1
    local line=$2
    local __start=$3
    local __end=$4
    local __flags=$5
    local raw_start raw_end raw_flags

    read -r raw_start raw_end raw_flags < <(sed -n "${line}p" "/sys/bus/pci/devices/${bdf}/resource")
    printf -v "${__start}" '%d' "${raw_start}"
    printf -v "${__end}" '%d' "${raw_end}"
    printf -v "${__flags}" '%d' "${raw_flags}"
}

read_bus_range() {
    local bdf=$1
    local __secondary=$2
    local __subordinate=$3

    printf -v "${__secondary}" '%d' "$(<"/sys/bus/pci/devices/${bdf}/secondary_bus_number")"
    printf -v "${__subordinate}" '%d' "$(<"/sys/bus/pci/devices/${bdf}/subordinate_bus_number")"
}

expect_window_inside() {
    local parent=$1
    local child=$2
    local line=$3
    local label=$4
    local parent_start parent_end parent_flags child_start child_end child_flags

    read_window "${parent}" "${line}" parent_start parent_end parent_flags
    read_window "${child}" "${line}" child_start child_end child_flags
    ((parent_flags != 0 && child_flags != 0)) ||
        die "${label} bridge window is disabled"
    ((child_start >= parent_start && child_end <= parent_end)) ||
        die "${label} window is outside its parent"
}

expect_bar() {
    local bdf=$1
    local line=$2
    local expected_size=$3
    local label=$4
    local start end flags size

    read_window "${bdf}" "${line}" start end flags
    ((flags != 0 && start != 0 && end >= start)) ||
        die "${bdf} ${label} is not assigned"
    size=$((end - start + 1))
    ((size == expected_size)) ||
        die "${bdf} ${label} has size ${size}, expected ${expected_size}"
}

expect_bar_inside() {
    local endpoint=$1
    local bar_line=$2
    local bridge=$3
    local bridge_line=$4
    local label=$5
    local bar_start bar_end bar_flags win_start win_end win_flags

    read_window "${endpoint}" "${bar_line}" bar_start bar_end bar_flags
    read_window "${bridge}" "${bridge_line}" win_start win_end win_flags
    ((bar_flags != 0 && win_flags != 0 && bar_start >= win_start && bar_end <= win_end)) ||
        die "${label} is outside ${bridge}"
}

expect_pci_id() {
    local bdf=$1
    local expected=$2
    local label=$3
    local actual

    actual="$(pci_id_at "${bdf}" 2>/dev/null || true)"
    [[ ${actual} == "${expected}" ]] ||
        die "${label} ${bdf} is ${actual:-absent}, expected ${expected}"
}

is_descendant_of() {
    local child=$1
    local ancestor=$2
    local child_path ancestor_path

    child_path="$(readlink -f "/sys/bus/pci/devices/${child}")"
    ancestor_path="$(readlink -f "/sys/bus/pci/devices/${ancestor}")"
    [[ ${child_path} == "${ancestor_path}/"* ]]
}

resolve_egpu_topology

[[ " $(</proc/cmdline) " != *' pci=assign-busses'* ]] ||
    die "pci=assign-busses is active"
[[ ${HP_DOCK_SUPPORT} == 1 ]] || die "downstream dock support is disabled in the profile"
hp_dock_router_present || die "the exact ${DOCK_DISPLAY_NAME} router is absent"
validate_expected_topology || die "the configured AMD/enclosure/eGPU BDF layout changed"

bridge_bus_prefix="${BRIDGE%:*}"
PORT1="${bridge_bus_prefix}:01.0"
PORT2="${bridge_bus_prefix}:02.0"
PORT3="${bridge_bus_prefix}:03.0"
for port in "${PORT1}" "${PORT2}" "${PORT3}"; do
    expect_pci_id "${port}" "${TH5P4_VENDOR}:${TH5P4_DEVICE}" "TH5P4 downstream bridge"
done

NIC="$(find_unique_pci_device "${HP_DOCK_NIC_VENDOR}" "${HP_DOCK_NIC_DEVICE}" 2>/dev/null || true)"
[[ -n ${NIC} ]] || die "the HP Dock I225-LMvP endpoint is absent or ambiguous"
[[ $(<"/sys/bus/pci/devices/${NIC}/subsystem_vendor") == "${HP_DOCK_NIC_SUBSYSTEM_VENDOR}" &&
   $(<"/sys/bus/pci/devices/${NIC}/subsystem_device") == "${HP_DOCK_NIC_SUBSYSTEM_DEVICE}" ]] ||
    die "${NIC} is not the validated HP Dock G4 NIC subsystem"
is_descendant_of "${NIC}" "${PORT1}" || die "${NIC} is not below the TH5P4 HP port"

nic_path="$(readlink -f "/sys/bus/pci/devices/${NIC}")"
NIC_BRIDGE="$(basename -- "$(dirname -- "${nic_path}")")"
HP_SWITCH="$(basename -- "$(dirname -- "$(dirname -- "${nic_path}")")")"
expect_pci_id "${NIC_BRIDGE}" "${HP_DOCK_BRIDGE_VENDOR}:${HP_DOCK_BRIDGE_DEVICE}" "HP NIC bridge"
expect_pci_id "${HP_SWITCH}" "${HP_DOCK_BRIDGE_VENDOR}:${HP_DOCK_BRIDGE_DEVICE}" "HP upstream bridge"
is_descendant_of "${HP_SWITCH}" "${PORT1}" || die "HP switch is outside the validated TH5P4 port"

hp_internal_bus="${NIC_BRIDGE%:*}"
read -r -a hp_internal_functions <<< "${HP_DOCK_INTERNAL_BRIDGE_FUNCTIONS}"
for function in "${hp_internal_functions[@]}"; do
    expect_pci_id "${hp_internal_bus}:${function}" \
        "${HP_DOCK_BRIDGE_VENDOR}:${HP_DOCK_BRIDGE_DEVICE}" "HP internal bridge"
done

# Reject an unexpected endpoint anywhere below the HP port. The current dock
# contains only its five Goshen Ridge bridges and exact HP I225 endpoint.
for sysfs in /sys/bus/pci/devices/0000:*; do
    bdf=${sysfs##*/}
    [[ ${bdf} == "${PORT1}" ]] && continue
    if is_descendant_of "${bdf}" "${PORT1}"; then
        id="$(pci_id_at "${bdf}" 2>/dev/null || true)"
        [[ ${id} == "${HP_DOCK_BRIDGE_VENDOR}:${HP_DOCK_BRIDGE_DEVICE}" ||
           ${bdf} == "${NIC}" ]] ||
            die "unexpected HP-port PCI function ${bdf} (${id:-unknown})"
    fi
done

# The other TH5P4 ports must still be empty.
for port in "${PORT2}" "${PORT3}"; do
    for sysfs in /sys/bus/pci/devices/0000:*; do
        bdf=${sysfs##*/}
        [[ ${bdf} == "${port}" ]] && continue
        is_descendant_of "${bdf}" "${port}" &&
            die "unused TH5P4 port ${port} contains ${bdf}"
    done
done

read_bus_range "${ROOT_PORT}" root_secondary root_subordinate
read_bus_range "${UPSTREAM}" up_secondary up_subordinate
read_bus_range "${BRIDGE}" gpu_secondary gpu_subordinate
read_bus_range "${PORT1}" p1_secondary p1_subordinate
read_bus_range "${PORT2}" p2_secondary p2_subordinate
read_bus_range "${PORT3}" p3_secondary p3_subordinate

((root_secondary == ROOT_BUS_SECONDARY && root_subordinate == ROOT_BUS_SUBORDINATE)) ||
    die "USB4 root bus range differs from the profile"
((up_secondary == UPSTREAM_BUS_SECONDARY && up_subordinate <= root_subordinate)) ||
    die "enclosure upstream range is invalid"
((gpu_secondary == GPU_BUS_SECONDARY && gpu_subordinate == GPU_BUS_SECONDARY)) ||
    die "eGPU branch is not the configured isolated bus"
((p1_secondary == PORT1_BUS_START && p1_subordinate == PORT1_BUS_END &&
   p2_secondary == PORT2_BUS_START && p2_subordinate == PORT2_BUS_END &&
   p3_secondary == PORT3_BUS_START && p3_subordinate == PORT3_BUS_END &&
   p1_secondary <= p1_subordinate &&
   p1_subordinate < p2_secondary && p2_secondary <= p2_subordinate &&
   p2_subordinate < p3_secondary && p3_secondary <= p3_subordinate &&
   p3_subordinate == up_subordinate)) ||
    die "TH5P4 downstream bus ranges overlap or are incomplete"

nic_bus_hex=${NIC#0000:}
nic_bus_hex=${nic_bus_hex%%:*}
nic_bus=$((16#${nic_bus_hex}))
((nic_bus >= p1_secondary && nic_bus <= p1_subordinate)) ||
    die "HP NIC bus is outside the HP-port range"

# Required physical-function BARs. Failed optional VF BAR allocation is not a
# blocker; these are the resources the NVIDIA and igc drivers actually use.
expect_bar "${GPU}" 1 "${EGPU_BAR0_SIZE}" "BAR0"
expect_bar "${GPU}" 2 "${EGPU_BAR1_SIZE}" "BAR1"
expect_bar "${GPU}" 4 "${EGPU_BAR3_SIZE}" "BAR3"
expect_bar "${GPU}" 6 "${EGPU_BAR5_SIZE}" "BAR5"
expect_bar "${AUDIO}" 1 "${EGPU_AUDIO_BAR0_SIZE}" "audio BAR0"
expect_bar "${NIC}" 1 "${HP_NIC_BAR0_SIZE}" "NIC BAR0"
expect_bar "${NIC}" 4 "${HP_NIC_BAR3_SIZE}" "NIC BAR3"

expect_bar_inside "${GPU}" 1 "${BRIDGE}" 15 "RTX BAR0"
expect_bar_inside "${GPU}" 2 "${BRIDGE}" 16 "RTX BAR1"
expect_bar_inside "${GPU}" 4 "${BRIDGE}" 16 "RTX BAR3"
expect_bar_inside "${GPU}" 6 "${BRIDGE}" 14 "RTX BAR5"
expect_bar_inside "${AUDIO}" 1 "${BRIDGE}" 15 "RTX audio BAR0"
expect_window_inside "${UPSTREAM}" "${BRIDGE}" 14 "RTX I/O"
expect_window_inside "${UPSTREAM}" "${BRIDGE}" 15 "RTX MMIO"
expect_window_inside "${UPSTREAM}" "${BRIDGE}" 16 "RTX MMIO_PREF"
expect_window_inside "${ROOT_PORT}" "${UPSTREAM}" 14 "TH5P4 I/O"
expect_window_inside "${ROOT_PORT}" "${UPSTREAM}" 15 "TH5P4 MMIO"
expect_window_inside "${ROOT_PORT}" "${UPSTREAM}" 16 "TH5P4 MMIO_PREF"

expect_bar_inside "${NIC}" 1 "${NIC_BRIDGE}" 15 "HP NIC BAR0"
expect_bar_inside "${NIC}" 4 "${NIC_BRIDGE}" 15 "HP NIC BAR3"
expect_window_inside "${HP_SWITCH}" "${NIC_BRIDGE}" 15 "HP NIC bridge MMIO"
expect_window_inside "${PORT1}" "${HP_SWITCH}" 15 "HP switch MMIO"
expect_window_inside "${UPSTREAM}" "${PORT1}" 15 "HP-port MMIO"

printf '%s\n' \
    "COLD-ATTACHED HP DOCK VERIFY PASSED." \
    "  ${IGPU_DISPLAY_NAME} and ${EGPU_DISPLAY_NAME} BDFs are unchanged; eGPU physical BARs are assigned." \
    "  ${DOCK_DISPLAY_NAME} PCI tree is complete at ${PORT1}; exact NIC endpoint ${NIC} is usable."
printf '  Firmware bus map is non-overlapping: HP %02x-%02x, unused %02x-%02x and %02x-%02x.\n' \
    "${p1_secondary}" "${p1_subordinate}" \
    "${p2_secondary}" "${p2_subordinate}" \
    "${p3_secondary}" "${p3_subordinate}"
