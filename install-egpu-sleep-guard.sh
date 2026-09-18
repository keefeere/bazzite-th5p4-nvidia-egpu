#!/usr/bin/env bash
set -Eeuo pipefail
[[ ${EUID} == 0 ]] || { echo 'Run with sudo.' >&2; exit 1; }
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
[[ -r /etc/egpu-nvidia/egpu-pci-lib.sh && -r /etc/egpu-nvidia/hardware.conf ]] || {
    echo 'Install the eGPU stack first.' >&2; exit 1;
}
# Use the installed parser/profile, without reinstalling the graphics stack.
source /etc/egpu-nvidia/egpu-pci-lib.sh
for action in suspend hibernate hybrid-sleep suspend-then-hibernate; do
    destination="/etc/systemd/system/systemd-${action}.service.d/90-egpu-sleep-guard.conf"
    if [[ -e ${destination} ]] &&
       ! cmp -s "${SCRIPT_DIR}/egpu-sleep-guard.conf" "${destination}" &&
       ! cmp -s <(sed 's@egpu-sleep-guard.sh %n$@egpu-sleep-guard.sh@' "${SCRIPT_DIR}/egpu-sleep-guard.conf") "${destination}"; then
        echo "Refusing to overwrite a different guard: ${destination}" >&2; exit 1
    fi
done
install -m 0755 "${SCRIPT_DIR}/egpu-sleep-guard.sh" /etc/egpu-nvidia/egpu-sleep-guard.sh
restorecon /etc/egpu-nvidia/egpu-sleep-guard.sh
for action in suspend hibernate hybrid-sleep suspend-then-hibernate; do
    destination="/etc/systemd/system/systemd-${action}.service.d/90-egpu-sleep-guard.conf"
    install -D -m 0644 "${SCRIPT_DIR}/egpu-sleep-guard.conf" "${destination}"
    restorecon "$(dirname "${destination}")" "${destination}"
done
systemctl daemon-reload
for action in suspend hibernate hybrid-sleep suspend-then-hibernate; do
    systemctl show "systemd-${action}.service" -p ExecStartPre --value |
        grep -Fq '/etc/egpu-nvidia/egpu-sleep-guard.sh'
done
echo 'eGPU sleep guard installed and active. No reboot needed; no sleep requested.'
result=0
/usr/bin/bash /etc/egpu-nvidia/egpu-sleep-guard.sh || result=$?
[[ ${result} -le 1 ]] || exit "${result}"
