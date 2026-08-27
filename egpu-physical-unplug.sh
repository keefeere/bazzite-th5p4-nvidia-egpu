#!/usr/bin/env bash
set -Eeuo pipefail

# This short helper is called only by the udev remove event of the exact TH5P4
# router tagged by the installed hardware profile. An unexpected cable removal
# has no detach latch to clear and is deliberately left untouched.
if [[ -s /run/egpu-safe-to-unplug && -e /run/egpu-nvidia-detach-block ]]; then
    rm -f -- \
        /run/egpu-safe-to-unplug \
        /run/egpu-nvidia-detach-block \
        /run/egpu-nvidia-hotplug-pending \
        /run/egpu-nvidia-late-loaded \
        /run/egpu-local-reserve-applied \
        /run/egpu-pciehp-quarantined \
        /run/egpu-local-reserve-before.txt \
        /run/egpu-gen4-test-active
    install -D -m 0644 /dev/null /run/egpu-nvidia-reboot-required
    logger -t egpu-nvidia "The safely released TH5P4 was physically unplugged; a reboot is required before NVIDIA can be used again."
fi
