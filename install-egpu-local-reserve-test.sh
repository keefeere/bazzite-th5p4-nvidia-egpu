#!/usr/bin/env bash
set -Eeuo pipefail

if [[ ${EUID} -ne 0 ]]; then
    echo "Run this installer as root (sudo)." >&2
    exit 1
fi

SOURCE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ENABLE_FILE="/etc/egpu-nvidia/enable-local-reserve"

"${SOURCE_DIR}/egpu-local-reserve-apply.sh" --dry-run
"${SOURCE_DIR}/install-egpu-boot-fix.sh"
install -D -m 0644 /dev/null "${ENABLE_FILE}"

echo
echo "Enabled the experimental TH5P4-only local bridge reservation."
echo "No kernel argument was added and the running PCI topology was not changed."
echo "The next cold boot may keep TH5P4 + RTX + HP Dock G4 physically connected."
echo "Only the exact HP PCI subtree is removed/rebuilt; its Thunderbolt router is never deauthorized."
echo "If any topology or resource check fails, NVIDIA stays blocked and AMD remains the fallback."
echo "Rollback: ${SOURCE_DIR}/remove-egpu-local-reserve-test.sh"
