#!/usr/bin/env bash
set -Eeuo pipefail

if [[ ${EUID} -ne 0 ]]; then
    echo "Run this rollback as root (sudo)." >&2
    exit 1
fi

rm -f -- /etc/egpu-nvidia/enable-local-reserve

echo "Disabled the experimental TH5P4-only local bridge reservation."
echo "The stable Gen3 NVIDIA boot fix remains installed."
echo "If the reservation is active in this boot, a cold reboot restores firmware bridge configuration."
