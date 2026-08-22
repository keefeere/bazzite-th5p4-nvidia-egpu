#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

# shellcheck source=egpu-pci-lib.sh
source "${SCRIPT_DIR}/egpu-pci-lib.sh"

die() {
    echo "CONFIG PREFLIGHT FAILED: $*" >&2
    exit 1
}

required_commands=(
    bash cmp find flock fuser getent install jq loginctl lspci modprobe
    nvidia-modprobe nvidia-smi readlink rpm-ostree runuser sed setpci
    systemctl udevadm
)
for command_name in "${required_commands[@]}"; do
    command -v "${command_name}" >/dev/null || die "required command is missing: ${command_name}"
done

desktop_record="$(getent passwd "${DESKTOP_USER}" || true)"
[[ -n ${desktop_record} ]] || die "desktop user ${DESKTOP_USER} does not exist"
IFS=: read -r actual_user _ actual_uid _ _ actual_home _ <<< "${desktop_record}"
[[ ${actual_user} == "${DESKTOP_USER}" && ${actual_uid} == "${DESKTOP_UID}" && -d ${actual_home} ]] ||
    die "configured desktop identity ${DESKTOP_USER}:${DESKTOP_UID} does not match the local account"

((ROOT_BUS_SECONDARY < UPSTREAM_BUS_SECONDARY &&
  UPSTREAM_BUS_SECONDARY < GPU_BUS_SECONDARY &&
  GPU_BUS_SECONDARY < PORT1_BUS_START && PORT1_BUS_START <= PORT1_BUS_END &&
  PORT1_BUS_END < PORT2_BUS_START && PORT2_BUS_START <= PORT2_BUS_END &&
  PORT2_BUS_END < PORT3_BUS_START && PORT3_BUS_START <= PORT3_BUS_END &&
  PORT3_BUS_END == ROOT_BUS_SUBORDINATE)) || die "configured PCI bus ranges overlap or leave an invalid tail"

((ROOT_IO_START == GPU_IO_START && GPU_IO_END < ROOT_IO_END &&
  ROOT_MMIO_START == GPU_MMIO_START && GPU_MMIO_END < ROOT_MMIO_END &&
  ROOT_PREF_START == GPU_PREF_START && GPU_PREF_END < ROOT_PREF_END)) ||
    die "configured GPU bridge windows are not the first children of the root windows"

IGPU="$(find_unique_pci_device "${IGPU_VENDOR}" "${IGPU_DEVICE}" 2>/dev/null || true)"
[[ -n ${IGPU} ]] || die "configured iGPU ${IGPU_VENDOR}:${IGPU_DEVICE} is absent or ambiguous"
[[ ${IGPU} == "${EXPECTED_IGPU_BDF}" ]] || die "iGPU is ${IGPU}, profile expects ${EXPECTED_IGPU_BDF}"

if resolve_egpu_gpu 2>/dev/null; then
    resolve_egpu_topology
    validate_expected_topology || die "live eGPU topology differs from the profile"
    hardware_state="live eGPU topology matches the profile"
elif th5p4_router_present; then
    hardware_state="${ENCLOSURE_DISPLAY_NAME} router is present; eGPU endpoint is not currently enumerated"
else
    hardware_state="eGPU is absent; static profile validation only"
fi

printf '%s\n' \
    "CONFIG PREFLIGHT PASSED." \
    "  Profile: ${PROFILE_NAME}" \
    "  Config:  ${EGPU_CONFIG_FILE}" \
    "  Desktop: ${DESKTOP_USER}:${DESKTOP_UID}" \
    "  State:   ${hardware_state}"
