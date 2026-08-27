#!/usr/bin/env bash
set -Eeuo pipefail

if [[ ${EUID} -ne 0 ]]; then
    echo "Run this script as root (sudo)." >&2
    exit 1
fi

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
STAGE_SCRIPT="${SCRIPT_DIR}/egpu-stage-gen3.sh"
TARGET_GENERATION="${EGPU_PCIE_GENERATION:-3}"
MODPROBE_CONFIG="${SCRIPT_DIR}/nvidia-base-only.conf"
NVIDIA_POLICY_LIB="${SCRIPT_DIR}/egpu-nvidia-policy.sh"
PENDING_MARKER="/run/egpu-nvidia-hotplug-pending"
LATE_MARKER="/run/egpu-nvidia-late-loaded"

# shellcheck source=egpu-pci-lib.sh
source "${SCRIPT_DIR}/egpu-pci-lib.sh"
# shellcheck source=egpu-nvidia-policy.sh
source "${NVIDIA_POLICY_LIB}"
resolve_egpu_topology

case "${TARGET_GENERATION}" in
    3) expected_speed="8.0 GT/s PCIe"; generation_label="Gen3" ;;
    4) expected_speed="16.0 GT/s PCIe"; generation_label="Gen4" ;;
    *) echo "Unsupported EGPU_PCIE_GENERATION=${TARGET_GENERATION}." >&2; exit 2 ;;
esac

trap 'rc=$?; echo "eGPU driver staging failed at line ${LINENO} (exit ${rc}). Do not unplug the eGPU until the loaded-module state has been checked." >&2; exit "${rc}"' ERR

for required_file in "${STAGE_SCRIPT}" "${MODPROBE_CONFIG}" "${NVIDIA_POLICY_LIB}"; do
    if [[ ! -r ${required_file} ]]; then
        echo "Required file is missing: ${required_file}" >&2
        exit 1
    fi
done

if grep -q '^nvidia ' /proc/modules; then
    echo "The NVIDIA core module is already loaded; refusing to restage a live GPU." >&2
    exit 1
fi

echo "[1/8] Fixing the eGPU PCIe link at ${generation_label} x4..."
"${STAGE_SCRIPT}"

echo "[2/8] Loading the NVIDIA core module only..."
nvidia_module_options=()
if egpu_nvidia_host_has_contiguous_policy; then
    nvidia_module_options+=("${EGPU_NVIDIA_CONTIGUOUS_POLICY}")
    echo "Mirroring the active Bazzite contiguous-allocation policy for Gamescope scanout."
fi
modprobe -C "${MODPROBE_CONFIG}" nvidia "${nvidia_module_options[@]}"

# Bazzite's NVIDIA udev rule changes this back to auto when the driver binds.
# Override it after the bind so the eGPU cannot enter runtime D3.
echo on > "/sys/bus/pci/devices/${GPU}/power/control"
echo 0 > "/sys/bus/pci/devices/${GPU}/d3cold_allowed"

echo "[3/8] Loading UVM and creating the core/UVM device nodes..."
modprobe -C "${MODPROBE_CONFIG}" nvidia_uvm
nvidia-modprobe -c 0 -c 255
nvidia-modprobe -u -c 0 -c 1

echo "[4/8] Starting NVIDIA persistence before the display stack..."
systemctl start nvidia-persistenced.service
systemctl is-active --quiet nvidia-persistenced.service

echo "[5/8] Loading NVIDIA modeset..."
modprobe -C "${MODPROBE_CONFIG}" nvidia_modeset
nvidia-modprobe -m

echo "[6/8] Loading NVIDIA DRM/KMS..."
modprobe -C "${MODPROBE_CONFIG}" nvidia_drm modeset=1 fbdev="${NVIDIA_DRM_FBDEV}"

# Keep the full PCIe path pinned after all driver/udev activity.
for device in "${ROOT_PORT}" "${UPSTREAM}" "${BRIDGE}" "${GPU}" "${AUDIO}"; do
    sysfs="/sys/bus/pci/devices/${device}"
    [[ -w "${sysfs}/power/control" ]] && echo on > "${sysfs}/power/control"
    [[ -w "${sysfs}/d3cold_allowed" ]] && echo 0 > "${sysfs}/d3cold_allowed"
done

echo "[7/8] Binding NVIDIA HDMI/DP audio after GPU initialization..."
if [[ ! -L "/sys/bus/pci/devices/${AUDIO}/driver" ]]; then
    echo "${AUDIO}" > /sys/bus/pci/drivers/snd_hda_intel/bind
fi
audio_driver="$(basename "$(readlink "/sys/bus/pci/devices/${AUDIO}/driver")")"
if [[ ${audio_driver} != "snd_hda_intel" ]]; then
    echo "NVIDIA HDMI/DP audio did not bind to snd_hda_intel." >&2
    exit 1
fi
echo on > "/sys/bus/pci/devices/${AUDIO}/power/control"
echo 0 > "/sys/bus/pci/devices/${AUDIO}/d3cold_allowed"

# snd_hda_intel completes part of the HDMI codec probe asynchronously after
# bind returns and can re-enable runtime PM after the immediate write above.
# Reassert D0 after that short probe window so the audio function cannot pull
# the tunneled eGPU path into a low-power transition later.
sleep 0.5
echo on > "/sys/bus/pci/devices/${GPU}/power/control"
echo 0 > "/sys/bus/pci/devices/${GPU}/d3cold_allowed"
echo on > "/sys/bus/pci/devices/${AUDIO}/power/control"
echo 0 > "/sys/bus/pci/devices/${AUDIO}/d3cold_allowed"

echo "[8/8] Verifying the live state..."
speed="$(<"/sys/bus/pci/devices/${GPU}/current_link_speed")"
width="$(<"/sys/bus/pci/devices/${GPU}/current_link_width")"
if [[ ${speed} != "${expected_speed}" || ${width} != "4" ]]; then
    echo "The GPU link changed unexpectedly: ${speed} x${width}." >&2
    exit 1
fi

for module in nvidia nvidia_uvm nvidia_modeset nvidia_drm; do
    if ! grep -q "^${module} " /proc/modules; then
        echo "Expected module ${module} is not loaded." >&2
        exit 1
    fi
done

nvidia-smi -L
rm -f -- "${PENDING_MARKER}" "${LATE_MARKER}"
echo "NVIDIA eGPU is ready at fixed ${generation_label} x4 with late-bound HDMI/DP audio."
