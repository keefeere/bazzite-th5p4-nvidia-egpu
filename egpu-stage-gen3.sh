#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
    echo "Run this script as root (sudo)." >&2
    exit 1
fi

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
TARGET_GENERATION="${EGPU_PCIE_GENERATION:-3}"

case "${TARGET_GENERATION}" in
    3)
        target_register=0x23
        expected_speed="8.0 GT/s PCIe"
        generation_label="Gen3"
        ;;
    4)
        target_register=0x24
        expected_speed="16.0 GT/s PCIe"
        generation_label="Gen4"
        ;;
    *)
        echo "Unsupported EGPU_PCIE_GENERATION=${TARGET_GENERATION}; expected 3 or 4." >&2
        exit 2
        ;;
esac
# shellcheck source=egpu-pci-lib.sh
source "${SCRIPT_DIR}/egpu-pci-lib.sh"
resolve_egpu_topology

if grep -q '^nvidia ' /proc/modules; then
    echo "The NVIDIA module is already loaded; refusing to retrain a live GPU." >&2
    exit 1
fi

gpu_driver="$(basename "$(readlink "/sys/bus/pci/devices/${GPU}/driver" 2>/dev/null)" 2>/dev/null || true)"
if [[ -n ${gpu_driver} ]]; then
    echo "The NVIDIA GPU is already bound to ${gpu_driver}; refusing to retrain it." >&2
    exit 1
fi

audio_driver="$(basename "$(readlink "/sys/bus/pci/devices/${AUDIO}/driver" 2>/dev/null)" 2>/dev/null || true)"
if [[ ${audio_driver} == "snd_hda_intel" ]]; then
    echo "${AUDIO}" > /sys/bus/pci/drivers/snd_hda_intel/unbind
fi

echo "Pinning the USB4/PCIe path in D0 and disabling D3cold..."
for device in "${ROOT_PORT}" "${UPSTREAM}" "${BRIDGE}" "${GPU}" "${AUDIO}"; do
    sysfs="/sys/bus/pci/devices/${device}"
    [[ -w "${sysfs}/power/control" ]] && echo on > "${sysfs}/power/control"
    [[ -w "${sysfs}/d3cold_allowed" ]] && echo 0 > "${sysfs}/d3cold_allowed"
done

echo "Before:"
lspci -s "${BRIDGE#0000:}" -vv | grep -E 'LnkCtl:|LnkSta:|LnkCtl2:'
lspci -s "${GPU#0000:}" -vv | grep -E 'LnkCtl:|LnkSta:|LnkCtl2:'

# Link Control 2: preserve every unrelated bit, set Target Link Speed in bits
# 3:0 and Hardware Autonomous Speed Disable in bit 5.
target_hex=$(printf '%04x' "${target_register}")
setpci -s "${GPU#0000:}" CAP_EXP+30.w="${target_hex}":002f
setpci -s "${BRIDGE#0000:}" CAP_EXP+30.w="${target_hex}":002f

# Request retraining from the downstream bridge (Link Control, bit 5).
setpci -s "${BRIDGE#0000:}" CAP_EXP+10.w=0020:0020

for _ in {1..20}; do
    speed="$(<"/sys/bus/pci/devices/${GPU}/current_link_speed")"
    width="$(<"/sys/bus/pci/devices/${GPU}/current_link_width")"
    if [[ ${speed} == "${expected_speed}" && ${width} == "4" ]]; then
        break
    fi
    sleep 0.1
done

echo "After:"
lspci -s "${BRIDGE#0000:}" -vv | grep -E 'LnkCtl:|LnkSta:|LnkCtl2:'
lspci -s "${GPU#0000:}" -vv | grep -E 'LnkCtl:|LnkSta:|LnkCtl2:'

speed="$(<"/sys/bus/pci/devices/${GPU}/current_link_speed")"
width="$(<"/sys/bus/pci/devices/${GPU}/current_link_width")"
if [[ ${speed} != "${expected_speed}" || ${width} != "4" ]]; then
    echo "Link did not settle at ${generation_label} x4 (reported ${speed} x${width})." >&2
    exit 1
fi

echo "eGPU link is staged at fixed ${generation_label} x4; NVIDIA remains unloaded."
