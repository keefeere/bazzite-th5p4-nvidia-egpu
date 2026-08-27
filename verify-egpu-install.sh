#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
failures=0
warnings=0

# shellcheck source=egpu-pci-lib.sh
source "${SCRIPT_DIR}/egpu-pci-lib.sh"
# shellcheck source=egpu-kernel-compat.sh
source "${SCRIPT_DIR}/egpu-kernel-compat.sh"

pass() {
    printf 'PASS  %s\n' "$1"
}

warn() {
    printf 'WARN  %s\n' "$1"
    warnings=$((warnings + 1))
}

fail() {
    printf 'FAIL  %s\n' "$1" >&2
    failures=$((failures + 1))
}

check_file() {
    local path=$1
    local description=$2

    if [[ -e ${path} ]]; then
        pass "${description}"
    else
        fail "${description}: missing ${path}"
    fi
}

printf '%s\n' 'NVIDIA eGPU final-stack verification'
pass "hardware profile: ${PROFILE_NAME}"

cmdline=" $(</proc/cmdline) "
kernel_release=$(uname -r)
if kernel_compat_mode=$(egpu_kernel_compat_mode "${kernel_release}"); then
    pass "kernel ${kernel_release} selected ${kernel_compat_mode} PCI compatibility mode"
else
    fail "unsupported kernel release format: ${kernel_release}"
    kernel_compat_mode=unknown
fi

if [[ ${kernel_compat_mode} == hotplug-size ]]; then
    if egpu_cmdline_has_arg "${cmdline}" "${EGPU_PCI_REALLOC_KARG}"; then
        fail "rejected ${EGPU_PCI_REALLOC_KARG} is active and globally repacks TH5P4"
    else
        pass "rejected PCI realloc compatibility mode is inactive"
    fi
    if egpu_cmdline_has_arg "${cmdline}" "${EGPU_PCI_HOTPLUG_KARG}"; then
        pass "Linux ${EGPU_PCI_COMPAT_MIN_KERNEL}+ hot-plug sizing argument ${EGPU_PCI_HOTPLUG_KARG} is active"
        if [[ -e /etc/egpu-nvidia/managed-pci-hotplug-size ]]; then
            pass "version-aware installer owns the exact hot-plug sizing argument"
        else
            pass "pre-existing user-managed hot-plug sizing argument is preserved"
        fi
    else
        fail "Linux ${EGPU_PCI_COMPAT_MIN_KERNEL}+ requires active ${EGPU_PCI_HOTPLUG_KARG}; rerun the installer and reboot"
    fi
elif [[ ${kernel_compat_mode} == legacy ]]; then
    pass "pre-${EGPU_PCI_COMPAT_MIN_KERNEL} kernel retains the unchanged legacy PCI flow"
    if [[ -e /etc/egpu-nvidia/managed-pci-hotplug-size ]]; then
        fail "stale stack-managed ${EGPU_PCI_HOTPLUG_KARG} marker on a legacy kernel; rerun the installer"
    elif egpu_cmdline_has_arg "${cmdline}" "${EGPU_PCI_HOTPLUG_KARG}"; then
        fail "legacy kernel is using the new-kernel ${EGPU_PCI_HOTPLUG_KARG}; rerun the installer"
    fi
    if [[ -e /etc/egpu-nvidia/managed-pci-realloc ]]; then
        fail "stale stack-managed ${EGPU_PCI_REALLOC_KARG} marker on a legacy kernel; rerun the installer"
    elif egpu_cmdline_has_arg "${cmdline}" "${EGPU_PCI_REALLOC_KARG}"; then
        warn "user-managed ${EGPU_PCI_REALLOC_KARG} is active but is not required by the validated legacy flow"
    fi
fi

if egpu_cmdline_has_pci_option "${cmdline}" assign-busses; then
    fail "global PCI bus numbering is active"
else
    pass "firmware PCI numbering is preserved"
fi
if egpu_cmdline_has_pci_option "${cmdline}" hpbussize ||
   egpu_cmdline_has_pci_option "${cmdline}" hpiosize ||
   egpu_cmdline_has_pci_option "${cmdline}" hpmemsize; then
    fail "unsupported global PCI hot-plug reservation arguments are active"
elif { egpu_cmdline_has_pci_option "${cmdline}" hpmmiosize ||
       egpu_cmdline_has_pci_option "${cmdline}" hpmmioprefsize; } &&
     ! { [[ ${kernel_compat_mode} == hotplug-size ]] &&
         egpu_cmdline_has_arg "${cmdline}" "${EGPU_PCI_HOTPLUG_KARG}"; }; then
    fail "unrecognized PCI hot-plug memory sizing argument is active"
else
    pass "no unsupported PCI hot-plug sizing arguments are active"
fi

for arg in thunderbolt.host_reset=0 thunderbolt.clx=0; do
    if [[ ${cmdline} == *" ${arg} "* ]]; then
        pass "kernel argument ${arg}"
    else
        warn "kernel argument ${arg} is staged but not active yet; reboot required"
    fi
done

check_file /etc/egpu-nvidia/enable-local-reserve "local ${ENCLOSURE_DISPLAY_NAME} reservation enabled"
check_file /etc/egpu-nvidia/hardware.conf "versioned hardware profile installed"
check_file /etc/egpu-nvidia/use-gen4 "persistent Gen4 preference enabled"
check_file /etc/modprobe.d/99-egpu-delay-nvidia.conf "automatic NVIDIA module loading blocked"
check_file /etc/dracut.conf.d/zz-egpu-delay.conf "NVIDIA omitted from locally generated initramfs"

if [[ -r /sys/module/nvidia_drm/parameters/fbdev ]]; then
    drm_fbdev="$(< /sys/module/nvidia_drm/parameters/fbdev)"
    if [[ ${drm_fbdev} == N || ${drm_fbdev} == 0 ]]; then
        pass "NVIDIA DRM fbdev is disabled; the iGPU owns the fallback console"
    else
        fail "NVIDIA DRM fbdev is ${drm_fbdev}; safe module unload requires a reboot with fbdev=0"
    fi
fi

if [[ -e /etc/egpu-nvidia/try-gen4-once ]]; then
    pass "Gen4 safety latch armed for the next boot"
elif [[ -e /run/egpu-gen4-test-active ]]; then
    pass "Gen4 safety latch consumed by the current boot"
else
    warn "Gen4 safety latch is not armed; the next NVIDIA boot will use Gen3"
fi

if systemctl is-enabled --quiet egpu-nvidia-boot.service; then
    pass "early eGPU boot service enabled"
else
    fail "egpu-nvidia-boot.service is not enabled"
fi

if [[ $(systemctl is-enabled ublue-nvctk-cdi.service 2>/dev/null || true) == masked ]]; then
    pass "ublue-nvctk-cdi.service masked"
else
    fail "ublue-nvctk-cdi.service is not masked"
fi

modprobe_rule="$(modprobe --showconfig 2>/dev/null | sed -n '/^install nvidia \/usr\/bin\/false\($\| \)/p' | head -n 1)"
if [[ -n ${modprobe_rule} ]]; then
    pass "uncontrolled modprobe nvidia is denied"
else
    fail "modprobe nvidia is not routed to /usr/bin/false"
fi

if resolve_egpu_topology 2>/dev/null; then
    pass "exact ${EGPU_DISPLAY_NAME}/${ENCLOSURE_DISPLAY_NAME}/${IGPU_DISPLAY_NAME} PCI topology resolved"
    speed="$(<"/sys/bus/pci/devices/${GPU}/current_link_speed")"
    width="$(<"/sys/bus/pci/devices/${GPU}/current_link_width")"
    if [[ ${speed} == "16.0 GT/s PCIe" && ${width} == 4 ]]; then
        pass "${EGPU_DISPLAY_NAME} link is PCIe Gen4 x4"
    elif [[ ${speed} == "8.0 GT/s PCIe" && ${width} == 4 ]]; then
        warn "${EGPU_DISPLAY_NAME} link is safe fallback PCIe Gen3 x4"
    else
        fail "unexpected RTX link: ${speed} x${width}"
    fi

    for module in nvidia nvidia_uvm nvidia_modeset nvidia_drm; do
        if grep -q "^${module} " /proc/modules; then
            pass "kernel module ${module} loaded"
        else
            fail "kernel module ${module} is not loaded"
        fi
    done

    if command -v nvidia-smi >/dev/null && nvidia-smi -L >/dev/null 2>&1; then
        pass "NVIDIA userspace can talk to the RTX"
    else
        fail "nvidia-smi cannot access the RTX"
    fi

    if hp_dock_router_present; then
        topology_verify="${SCRIPT_DIR}/egpu-cold-attached-hp-verify.sh"
        if topology_output="$("${topology_verify}" 2>&1)"; then
            pass "local reserve and complete ${DOCK_DISPLAY_NAME} PCI subtree verified"
        else
            fail "HP/local-reserve verifier: ${topology_output##*$'\n'}"
        fi

        NIC="$(find_unique_pci_device "${HP_DOCK_NIC_VENDOR}" "${HP_DOCK_NIC_DEVICE}" 2>/dev/null || true)"
        nic_interface=""
        if [[ -n ${NIC} ]]; then
            for interface_dir in /sys/class/net/*; do
                [[ -e ${interface_dir}/device ]] || continue
                if [[ $(basename -- "$(readlink -f "${interface_dir}/device")") == "${NIC}" ]]; then
                    nic_interface="${interface_dir##*/}"
                    break
                fi
            done
        fi
        if [[ -n ${nic_interface} ]]; then
            nic_speed="$(cat -- "/sys/class/net/${nic_interface}/speed" 2>/dev/null || true)"
            if [[ ${nic_speed} == 1000 ]]; then
                pass "${DOCK_DISPLAY_NAME} Ethernet ${nic_interface} linked at 1000 Mb/s"
            else
                warn "${DOCK_DISPLAY_NAME} Ethernet ${nic_interface} reports ${nic_speed:-unknown} Mb/s"
            fi
        else
            warn "${DOCK_DISPLAY_NAME} Ethernet interface was not resolved"
        fi

        displaylink_speed=""
        for usb_dir in /sys/bus/usb/devices/*; do
            [[ -r ${usb_dir}/idVendor && -r ${usb_dir}/idProduct ]] || continue
            if [[ $(<"${usb_dir}/idVendor") == "${HP_DOCK_DISPLAYLINK_VENDOR}" &&
                  $(<"${usb_dir}/idProduct") == "${HP_DOCK_DISPLAYLINK_PRODUCT}" ]]; then
                displaylink_speed="$(cat -- "${usb_dir}/speed" 2>/dev/null || true)"
                break
            fi
        done
        if [[ ${displaylink_speed} =~ ^[0-9]+$ ]] && ((displaylink_speed >= 5000)); then
            pass "configured USB display adapter is on the USB3 tunnel at ${displaylink_speed} Mb/s"
        elif [[ -n ${displaylink_speed} ]]; then
            warn "configured USB display adapter is present but reports only ${displaylink_speed} Mb/s"
        else
            warn "configured USB display adapter is absent; ${DOCK_DISPLAY_NAME} USB3 endpoint check skipped"
        fi
    else
        topology_verify="${SCRIPT_DIR}/egpu-local-reserve-verify.sh"
        if topology_output="$("${topology_verify}" 2>&1)"; then
            pass "local ${ENCLOSURE_DISPLAY_NAME} reservation verified"
        else
            fail "local-reserve verifier: ${topology_output##*$'\n'}"
        fi
    fi
else
    if th5p4_usb_diagnostic_present && ! th5p4_router_present; then
        fail "${ENCLOSURE_DISPLAY_NAME} USB diagnostic is present but its USB4/PCIe router is absent; power-cycle the enclosure/eGPU"
    else
        warn "${EGPU_DISPLAY_NAME} is absent; live GPU checks skipped"
    fi
fi

tray_status="$("${SCRIPT_DIR}/egpu-tray-status.sh" 2>&1 || true)"
if [[ -n ${tray_status} ]]; then
    pass "tray status helper: ${tray_status//$'\t'/ — }"
else
    fail "tray status helper returned no state"
fi

printf '\nVerification complete: %d failure(s), %d warning(s).\n' "${failures}" "${warnings}"
(( failures == 0 ))
