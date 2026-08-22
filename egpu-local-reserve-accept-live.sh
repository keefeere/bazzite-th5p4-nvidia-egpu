#!/usr/bin/env bash
set -Eeuo pipefail

if [[ ${EUID} -ne 0 ]]; then
    echo "Run this recovery as root (sudo)." >&2
    exit 1
fi

SOURCE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ENABLE_FILE="/etc/egpu-nvidia/enable-local-reserve"
MARKER="/run/egpu-local-reserve-applied"

# shellcheck source=egpu-pci-lib.sh
source "${SOURCE_DIR}/egpu-pci-lib.sh"

[[ -e ${ENABLE_FILE} ]] || {
    echo "The local-reserve experiment is not enabled." >&2
    exit 1
}
if grep -q '^nvidia' /proc/modules; then
    echo "NVIDIA is already loaded; refusing the failed-boot recovery path." >&2
    exit 1
fi

# Install the corrected invariant-based verifier and marker-aware boot path.
"${SOURCE_DIR}/install-egpu-boot-fix.sh"
"${SOURCE_DIR}/egpu-local-reserve-verify.sh"

printf '%s\n' \
    "TH5P4-only bus/MMIO reservation is active." \
    "Accepted after Linux safely normalized the child bridge windows." \
    > "${MARKER}"

systemctl reset-failed egpu-nvidia-boot.service egpu-nvidia-quarantine.service
systemctl start egpu-nvidia-boot.service
systemctl is-active --quiet egpu-nvidia-boot.service
nvidia-smi -L

kwin_devices="$(sed -n 's/^KWIN_DRM_DEVICES=//p' /etc/environment.d/10kwin-egpu.conf)"
if [[ -n ${kwin_devices} && -S /run/user/${DESKTOP_UID}/bus ]]; then
    runuser -u "${DESKTOP_USER}" -- \
        env XDG_RUNTIME_DIR="/run/user/${DESKTOP_UID}" \
        systemctl --user set-environment "KWIN_DRM_DEVICES=${kwin_devices}"
fi

echo "NVIDIA is loaded. Log out and back in if external outputs do not appear automatically."
