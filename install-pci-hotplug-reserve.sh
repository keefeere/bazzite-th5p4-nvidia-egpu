#!/usr/bin/env bash
set -Eeuo pipefail

if [[ ${EUID} -ne 0 ]]; then
    echo "Run this installer as root (sudo)." >&2
    exit 1
fi

cat >&2 <<'EOF'
This experiment is retired and will not be installed.

Observed results on the ASUS ROG Xbox Ally X:
- without assign-busses, the empty TH5P4 ports retained only one bus each;
- with assign-busses, amdgpu could not locate the iGPU BIOS ROM and failed
  initialization, leaving the internal panel on simple-framebuffer at
  1366x768@60;
- the reassigned TH5P4 ports still had only 6 buses and 8 MiB MMIO windows,
  below the verified HP Dock G4 requirement.

Use cold attachment for HP Dock G4 and keep the stable Gen3 NVIDIA boot fix.
EOF
exit 1
