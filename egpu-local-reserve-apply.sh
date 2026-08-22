#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PREFLIGHT="${SCRIPT_DIR}/egpu-local-reserve-preflight.sh"
VERIFY="${SCRIPT_DIR}/egpu-local-reserve-verify.sh"
COLD_HP_VERIFY="${SCRIPT_DIR}/egpu-cold-attached-hp-verify.sh"
APPLIED_MARKER="/run/egpu-local-reserve-applied"
BEFORE_STATE="/run/egpu-local-reserve-before.txt"
dry_run=0

if [[ ${1:-} == "--dry-run" ]]; then
    dry_run=1
elif (($#)); then
    echo "Usage: $0 [--dry-run]" >&2
    exit 2
fi

if (( ! dry_run )) && [[ ${EUID} -ne 0 ]]; then
    echo "Run this script as root." >&2
    exit 1
fi

if (( ! dry_run )) && [[ ${EGPU_LOCAL_RESERVE_BOOT_CONTEXT:-0} != 1 ]]; then
    echo "Refusing live PCI surgery outside the controlled pre-NVIDIA boot path." >&2
    echo "Use --dry-run for a read-only check." >&2
    exit 1
fi

if (( ! dry_run )) && systemctl is-active --quiet display-manager.service; then
    echo "Refusing local PCI bridge changes after the display manager has started." >&2
    exit 1
fi

if grep -q '^nvidia' /proc/modules; then
    if (( dry_run )); then
        echo "Note: NVIDIA is live; dry-run remains read-only."
    else
        echo "NVIDIA is already loaded; refusing TH5P4 remove/rescan." >&2
        exit 1
    fi
fi

# Executing the preflight in this shell leaves the validated topology and
# calculated reserve boundaries available below.
# shellcheck source=egpu-local-reserve-preflight.sh
source "${PREFLIGHT}"

PORT1=${PORTS[0]}
PORT2=${PORTS[1]}
PORT3=${PORTS[2]}

bus1_start=${PORT1_BUS_START}
bus1_end=${PORT1_BUS_END}
bus2_start=${PORT2_BUS_START}
bus2_end=${PORT2_BUS_END}
bus3_start=${PORT3_BUS_START}
bus3_end=${PORT3_BUS_END}

printf '%s\n' \
    "" \
    "Planned config-space changes:" \
    "  ${UPSTREAM}: secondary=$(printf '%02x' "${UPSTREAM_BUS_SECONDARY}"), subordinate=$(printf '%02x' "${ROOT_BUS_SUBORDINATE}")" \
    "  ${PORT1}: buses $(printf '%02x-%02x' "${bus1_start}" "${bus1_end}"); windows $(printf '%x-%x, %x-%x, %x-%x' "${reserve_io_start}" "${reserve_io_end}" "${reserve_mem_start}" "${reserve_mem_end}" "${reserve_pref_start}" "${reserve_pref_end}")" \
    "  ${PORT2}: buses $(printf '%02x-%02x' "${bus2_start}" "${bus2_end}"); address windows disabled" \
    "  ${PORT3}: buses $(printf '%02x-%02x' "${bus3_start}" "${bus3_end}"); address windows disabled"

if (( dry_run )); then
    echo "DRY RUN COMPLETE: no PCI config, driver, sysfs or runtime state was changed."
    exit 0
fi

rm -f -- "${APPLIED_MARKER}" "${BEFORE_STATE}"

original_gpu=${GPU}
original_audio=${AUDIO}
original_bridge=${BRIDGE}
original_upstream=${UPSTREAM}
original_root=${ROOT_PORT}
original_igpu=${IGPU}

gpu_driver="$(basename "$(readlink "/sys/bus/pci/devices/${GPU}/driver" 2>/dev/null)" 2>/dev/null || true)"
[[ -z ${gpu_driver} ]] || {
    echo "RTX is already bound to ${gpu_driver}; refusing remove/rescan." >&2
    exit 1
}

audio_driver="$(basename "$(readlink "/sys/bus/pci/devices/${AUDIO}/driver" 2>/dev/null)" 2>/dev/null || true)"
if [[ ${audio_driver} == "snd_hda_intel" ]]; then
    echo "${AUDIO}" > /sys/bus/pci/drivers/snd_hda_intel/unbind
elif [[ -n ${audio_driver} ]]; then
    echo "RTX audio is unexpectedly bound to ${audio_driver}." >&2
    exit 1
fi

for device in "${ROOT_PORT}" "${UPSTREAM}" "${BRIDGE}" "${PORT1}" "${PORT2}" "${PORT3}" "${GPU}" "${AUDIO}"; do
    sysfs="/sys/bus/pci/devices/${device}"
    [[ -w ${sysfs}/power/control ]] && echo on > "${sysfs}/power/control"
    [[ -w ${sysfs}/d3cold_allowed ]] && echo 0 > "${sysfs}/d3cold_allowed"
done

{
    echo "Original bridge config before local TH5P4 reservation:"
    for bdf in "${ROOT_PORT}" "${UPSTREAM}" "${BRIDGE}" "${PORT1}" "${PORT2}" "${PORT3}"; do
        printf '%s ' "${bdf}"
        setpci -s "${bdf#0000:}" \
            PRIMARY_BUS.b SECONDARY_BUS.b SUBORDINATE_BUS.b \
            IO_BASE.b IO_LIMIT.b IO_BASE_UPPER16.w IO_LIMIT_UPPER16.w \
            MEMORY_BASE.w MEMORY_LIMIT.w \
            PREF_MEMORY_BASE.w PREF_MEMORY_LIMIT.w \
            PREF_BASE_UPPER32.l PREF_LIMIT_UPPER32.l | paste -sd' ' -
    done
} > "${BEFORE_STATE}"

write_bus_range() {
    local bdf=$1
    local secondary=$2
    local subordinate=$3

    setpci -s "${bdf#0000:}" \
        SECONDARY_BUS.b="$(printf '%02x' "${secondary}")" \
        SUBORDINATE_BUS.b="$(printf '%02x' "${subordinate}")"
}

write_io_window() {
    local bdf=$1
    local start=$2
    local end=$3
    local base_lo limit_lo base_hi limit_hi

    base_lo=$(printf '%02x' "$((((start >> 8) & 0xf0) | 1))")
    limit_lo=$(printf '%02x' "$((((end >> 8) & 0xf0) | 1))")
    base_hi=$(printf '%04x' "$((start >> 16))")
    limit_hi=$(printf '%04x' "$((end >> 16))")
    setpci -s "${bdf#0000:}" IO_BASE_UPPER16.l=0000ffff
    setpci -s "${bdf#0000:}" IO_BASE.b="${base_lo}" IO_LIMIT.b="${limit_lo}"
    setpci -s "${bdf#0000:}" IO_BASE_UPPER16.w="${base_hi}" IO_LIMIT_UPPER16.w="${limit_hi}"
}

write_mem_window() {
    local bdf=$1
    local start=$2
    local end=$3
    setpci -s "${bdf#0000:}" \
        MEMORY_BASE.w="$(printf '%04x' "$(((start >> 16) & 0xfff0))")" \
        MEMORY_LIMIT.w="$(printf '%04x' "$(((end >> 16) & 0xfff0))")"
}

write_pref_window() {
    local bdf=$1
    local start=$2
    local end=$3
    local base_lo limit_lo base_hi limit_hi

    base_lo=$(printf '%04x' "$((((start >> 16) & 0xfff0) | 1))")
    limit_lo=$(printf '%04x' "$((((end >> 16) & 0xfff0) | 1))")
    base_hi=$(printf '%08x' "$((start >> 32))")
    limit_hi=$(printf '%08x' "$((end >> 32))")
    setpci -s "${bdf#0000:}" PREF_LIMIT_UPPER32.l=00000000
    setpci -s "${bdf#0000:}" PREF_MEMORY_BASE.w="${base_lo}" PREF_MEMORY_LIMIT.w="${limit_lo}"
    setpci -s "${bdf#0000:}" PREF_BASE_UPPER32.l="${base_hi}" PREF_LIMIT_UPPER32.l="${limit_hi}"
}

disable_windows() {
    local bdf=$1
    setpci -s "${bdf#0000:}" IO_BASE_UPPER16.l=00000000 IO_BASE.b=f1 IO_LIMIT.b=01
    setpci -s "${bdf#0000:}" MEMORY_BASE.w=fff0 MEMORY_LIMIT.w=0000
    setpci -s "${bdf#0000:}" \
        PREF_LIMIT_UPPER32.l=00000000 \
        PREF_MEMORY_BASE.w=fff1 PREF_MEMORY_LIMIT.w=0001 \
        PREF_BASE_UPPER32.l=00000000 PREF_LIMIT_UPPER32.l=00000000
}

echo "Programming only the TH5P4 bridge hierarchy..."
write_bus_range "${PORT1}" "${bus1_start}" "${bus1_end}"
write_bus_range "${PORT2}" "${bus2_start}" "${bus2_end}"
write_bus_range "${PORT3}" "${bus3_start}" "${bus3_end}"
write_bus_range "${UPSTREAM}" "${UPSTREAM_BUS_SECONDARY}" "${ROOT_BUS_SUBORDINATE}"

write_io_window "${PORT1}" "${reserve_io_start}" "${reserve_io_end}"
write_mem_window "${PORT1}" "${reserve_mem_start}" "${reserve_mem_end}"
write_pref_window "${PORT1}" "${reserve_pref_start}" "${reserve_pref_end}"
disable_windows "${PORT2}"
disable_windows "${PORT3}"

write_io_window "${UPSTREAM}" "${root_io_start}" "${reserve_io_end}"
write_mem_window "${UPSTREAM}" "${root_mem_start}" "${reserve_mem_end}"
write_pref_window "${UPSTREAM}" "${root_pref_start}" "${reserve_pref_end}"

rescan_on_error() {
    rc=$?
    trap - ERR
    if [[ ! -d /sys/bus/pci/devices/${original_upstream} ]]; then
        echo "TH5P4 disappeared during a failed transition; attempting one PCI rescan." >&2
        echo 1 > /sys/bus/pci/rescan || true
    fi
    echo "Local TH5P4 reservation failed; NVIDIA remains blocked. Cold reboot restores firmware bridge configuration." >&2
    exit "${rc}"
}
trap rescan_on_error ERR

echo "Removing the driver-free TH5P4 PCIe subtree from the kernel model..."
echo 1 > "/sys/bus/pci/devices/${UPSTREAM}/remove"

for _ in {1..20}; do
    [[ ! -d /sys/bus/pci/devices/${original_upstream} ]] && break
    sleep 0.05
done
[[ ! -d /sys/bus/pci/devices/${original_upstream} ]]

echo "Rescanning PCI so Linux imports the locally programmed bridge ranges..."
echo 1 > /sys/bus/pci/rescan

for _ in {1..50}; do
    [[ -d /sys/bus/pci/devices/${original_gpu} ]] && break
    sleep 0.1
done
[[ -d /sys/bus/pci/devices/${original_gpu} ]]
udevadm settle --timeout=10

resolve_egpu_topology
[[ ${GPU} == "${original_gpu}" && ${AUDIO} == "${original_audio}" &&
   ${BRIDGE} == "${original_bridge}" && ${UPSTREAM} == "${original_upstream}" &&
   ${ROOT_PORT} == "${original_root}" && ${IGPU} == "${original_igpu}" ]]

if [[ ${EGPU_COLD_HP_PCI_REBUILD:-0} == 1 ]]; then
    # The authorized USB4 router and its PCIe tunnel were intentionally kept
    # alive.  Wait briefly for the exact I225 endpoint to return after the
    # TH5P4 rescan, then validate every bridge, BAR and parent window.
    for _ in {1..50}; do
        find_unique_pci_device "${HP_DOCK_NIC_VENDOR}" "${HP_DOCK_NIC_DEVICE}" >/dev/null 2>&1 && break
        sleep 0.1
    done
    "${COLD_HP_VERIFY}"
else
    "${VERIFY}"
fi
if grep -q '^nvidia' /proc/modules; then
    echo "NVIDIA loaded unexpectedly during the local bridge transition." >&2
    exit 1
fi

printf '%s\n' \
    "TH5P4-only bus/MMIO reservation is active." \
    "Validated before the NVIDIA driver was allowed to bind." \
    > "${APPLIED_MARKER}"
if [[ ${EGPU_COLD_HP_PCI_REBUILD:-0} == 1 ]]; then
    echo "Cold-attached HP PCI subtree was rebuilt without Thunderbolt deauthorization." >> "${APPLIED_MARKER}"
fi

trap - ERR
cat "${APPLIED_MARKER}"
