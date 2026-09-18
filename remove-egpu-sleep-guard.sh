#!/usr/bin/env bash
set -Eeuo pipefail
[[ ${EUID} == 0 ]] || { echo 'Run with sudo.' >&2; exit 1; }
for action in suspend hibernate hybrid-sleep suspend-then-hibernate; do
    rm -f -- "/etc/systemd/system/systemd-${action}.service.d/90-egpu-sleep-guard.conf"
done
systemctl daemon-reload
echo 'Removed the eGPU sleep guard drop-ins. Sleep is no longer blocked by this guard.'
