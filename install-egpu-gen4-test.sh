#!/usr/bin/env bash
set -Eeuo pipefail

if [[ ${EUID} -ne 0 ]]; then
    echo "Run this installer as root (sudo)." >&2
    exit 1
fi

SOURCE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REQUEST="/etc/egpu-nvidia/try-gen4-once"
LOCAL_RESERVE_ENABLE="/etc/egpu-nvidia/enable-local-reserve"

if [[ ! -e ${LOCAL_RESERVE_ENABLE} ]]; then
    echo "The tested TH5P4 local-reservation boot path is not enabled; refusing to arm Gen4." >&2
    echo "Install the local reservation experiment first." >&2
    exit 1
fi

"${SOURCE_DIR}/install-egpu-boot-fix.sh"
install -D -m 0644 /dev/null "${REQUEST}"

echo
echo "Armed a one-shot PCIe Gen4 x4 eGPU test for the next NVIDIA boot."
echo "The request is consumed before retraining, so any following boot returns to stable Gen3 automatically."
echo "Use a warm reboot with the validated TH5P4 + RTX + HP Dock chain left powered and connected."
echo "The early PCI-only HP rebuild runs before the guarded Gen4 retrain."
echo "Cancel before reboot: ${SOURCE_DIR}/remove-egpu-gen4-test.sh"
