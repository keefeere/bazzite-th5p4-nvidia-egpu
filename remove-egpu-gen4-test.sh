#!/usr/bin/env bash
set -Eeuo pipefail

if [[ ${EUID} -ne 0 ]]; then
    echo "Run this rollback as root (sudo)." >&2
    exit 1
fi

rm -f -- /etc/egpu-nvidia/try-gen4-once
echo "Cancelled the pending Gen4 transition. The next boot uses stable Gen3."
if [[ -e /etc/egpu-nvidia/use-gen4 ]]; then
    echo "The persistent Gen4 preference remains enabled, but it will not re-arm until another successful Gen4 boot."
fi
