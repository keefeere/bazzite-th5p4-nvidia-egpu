#!/usr/bin/env bash
set -Eeuo pipefail

if [[ ${EUID} -ne 0 ]]; then
    echo "Run this script as root (sudo)." >&2
    exit 1
fi

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=egpu-nvidia-policy.sh
source "${SCRIPT_DIR}/egpu-nvidia-policy.sh"

case "${1:-arm}" in
    arm)
        install -D -m 0644 /dev/null "${EGPU_NVIDIA_POLICY_SKIP_ONCE}"
        echo "Armed one NVIDIA allocation-policy A/B boot."
        echo "The next controlled NVIDIA load will omit RMDisableNoncontigAlloc=1."
        echo "The latch is consumed before module load; the following reboot restores the Bazzite policy automatically."
        echo "This does not change the NVIDIA module already loaded in the current boot."
        ;;
    cancel)
        rm -f -- "${EGPU_NVIDIA_POLICY_SKIP_ONCE}"
        echo "Cancelled the pending NVIDIA allocation-policy A/B boot."
        ;;
    *)
        echo "Usage: $0 [arm|cancel]" >&2
        exit 2
        ;;
esac
