#!/usr/bin/env bash
set -Eeuo pipefail

if [[ ${EUID} -ne 0 ]]; then
    echo "Run this rollback as root (sudo)." >&2
    exit 1
fi

OLD_KARG='pci=realloc=on,hpbussize=6,hpmmiosize=8M,hpmmioprefsize=8M'
KARG='pci=assign-busses,realloc=on,hpbussize=6,hpmmiosize=8M,hpmmioprefsize=8M'

rpm-ostree kargs \
    --delete-if-present="${OLD_KARG}" \
    --delete-if-present="${KARG}"

echo "Removed all experimental PCI hot-plug reservation arguments from the next deployment."
echo "The Gen3 NVIDIA boot fix is unchanged. Power off and cold-boot to restore the normal AMD display path."
