#!/usr/bin/env bash
set -Eeuo pipefail

if [[ ${EUID} -ne 0 ]]; then
    echo "Run this helper as root (sudo)." >&2
    exit 1
fi

rm -f -- \
    /etc/egpu-nvidia/use-gen4 \
    /etc/egpu-nvidia/try-gen4-once

echo "Persistent Gen4 and the next-boot request are disabled."
echo "The current live link is unchanged; the next NVIDIA boot uses stable Gen3 x4."
