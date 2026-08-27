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
    printf '%s\n' \
        "The eGPU cable was removed after a successful safe detach." \
        "Same-boot physical reattach is unsupported; reboot with the enclosure attached." \
        > /run/egpu-nvidia-released-unplugged
    install -D -m 0644 /dev/null /run/egpu-nvidia-reboot-required
    logger -t egpu-nvidia "Safe detach completed and TH5P4 was physically unplugged; same-boot reattach is unsupported, so the next NVIDIA use requires a reboot."
fi
