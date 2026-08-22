#!/usr/bin/env bash
set -Eeuo pipefail

if [[ ${EUID} -ne 0 ]]; then
    echo "Run this helper as root (sudo)." >&2
    exit 1
fi

LOCAL_RESERVE_ENABLE="/etc/egpu-nvidia/enable-local-reserve"
PERSISTENT_REQUEST="/etc/egpu-nvidia/use-gen4"
NEXT_BOOT_REQUEST="/etc/egpu-nvidia/try-gen4-once"

if [[ ! -e ${LOCAL_RESERVE_ENABLE} ]]; then
    echo "The verified TH5P4 local reservation is not enabled; refusing persistent Gen4." >&2
    exit 1
fi

install -D -m 0644 /dev/null "${PERSISTENT_REQUEST}"
install -D -m 0644 /dev/null "${NEXT_BOOT_REQUEST}"

echo "Guarded persistent Gen4 is enabled and armed for the next early NVIDIA boot."
echo "A successful Gen4 boot re-arms the following boot."
echo "A failed Gen4 transition consumes the latch, so the next boot falls back to Gen3."
