#!/usr/bin/env bash

# Linux 7.2 stopped preserving the pre-programmed size of empty PCI bridge
# memory windows during this project's controlled TH5P4 remove/rescan.  Bus
# ranges still survive, but the allocator uses the standard hot-plug sizing
# policy instead.  Request the measured 32 MiB minimum on those kernels only.
#
# Older kernels retain the already validated local-window flow unchanged.
EGPU_PCI_COMPAT_MIN_KERNEL="7.2.0"
EGPU_PCI_HOTPLUG_KARG="pci=hpmmiosize=32M,hpmmioprefsize=32M"
EGPU_PCI_HOTPLUG_MMIO_BYTES=$((32 * 1024 * 1024))
EGPU_PCI_HOTPLUG_PREF_BYTES=$((32 * 1024 * 1024))
EGPU_TB_HOST_RESET_KARG="thunderbolt.host_reset=0"

# Migration-only: an earlier, rejected 7.2 experiment used this argument.
# It globally repacked the TH5P4 hierarchy at boot and must never be required.
EGPU_PCI_REALLOC_KARG="pci=realloc=on"

egpu_kernel_release_version() {
    local release=${1:-$(uname -r)}
    local major minor patch extra

    release=${release%%-*}
    IFS=. read -r major minor patch extra <<< "${release}"
    [[ -z ${extra:-} && ${major:-} =~ ^[0-9]+$ &&
       ${minor:-} =~ ^[0-9]+$ && ${patch:-0} =~ ^[0-9]+$ ]] || return 1
    printf '%d.%d.%d\n' "${major}" "${minor}" "${patch:-0}"
}

egpu_version_at_least() {
    local current=$1 minimum=$2
    local current_major current_minor current_patch
    local minimum_major minimum_minor minimum_patch

    IFS=. read -r current_major current_minor current_patch <<< "${current}"
    IFS=. read -r minimum_major minimum_minor minimum_patch <<< "${minimum}"
    for value in \
        "${current_major}" "${current_minor}" "${current_patch}" \
        "${minimum_major}" "${minimum_minor}" "${minimum_patch}"; do
        [[ ${value} =~ ^[0-9]+$ ]] || return 2
    done

    (( current_major > minimum_major )) && return 0
    (( current_major < minimum_major )) && return 1
    (( current_minor > minimum_minor )) && return 0
    (( current_minor < minimum_minor )) && return 1
    (( current_patch >= minimum_patch ))
}

egpu_kernel_requires_hotplug_sizing() {
    local version

    version=$(egpu_kernel_release_version "${1:-$(uname -r)}") || return 2
    egpu_version_at_least "${version}" "${EGPU_PCI_COMPAT_MIN_KERNEL}"
}

egpu_kernel_compat_mode() {
    if egpu_kernel_requires_hotplug_sizing "${1:-$(uname -r)}"; then
        printf '%s\n' hotplug-size
    else
        case $? in
            1) printf '%s\n' legacy ;;
            *) return 2 ;;
        esac
    fi
}

egpu_boot_order_dropin() {
    local mode=$1

    case ${mode} in
        hotplug-size)
            printf '%s\n' \
                '[Unit]' \
                'Wants=bolt.service' \
                'After=bolt.service'
            ;;
        legacy)
            printf '%s\n' \
                '[Unit]' \
                'Before=bolt.service'
            ;;
        *) return 2 ;;
    esac
}

# The original 6.17 deployment needed host_reset=0 for stable cold-attached
# operation. On the tested 7.2 AMD USB4 stack it prevents a freshly booted
# host router from bringing a first hot-plugged TH5P4 to TB_PORT_UP. Keep the
# old-kernel behavior unchanged and return to the upstream default reset on
# 7.2+.
egpu_managed_host_reset_action() {
    local release=$1 cmdline=$2
    local mode

    mode=$(egpu_kernel_compat_mode "${release}") || return 2
    if [[ ${mode} == legacy ]]; then
        if egpu_cmdline_has_arg "${cmdline}" "${EGPU_TB_HOST_RESET_KARG}"; then
            printf '%s\n' keep
        else
            printf '%s\n' add
        fi
    elif egpu_cmdline_has_arg "${cmdline}" "${EGPU_TB_HOST_RESET_KARG}"; then
        printf '%s\n' remove
    else
        printf '%s\n' keep
    fi
}

egpu_cmdline_has_arg() {
    local cmdline=$1 arg=$2
    [[ " ${cmdline} " == *" ${arg} "* ]]
}

egpu_cmdline_has_pci_option() {
    local cmdline=$1 wanted=$2 token option
    local -a options

    for token in ${cmdline}; do
        [[ ${token} == pci=* ]] || continue
        IFS=, read -r -a options <<< "${token#pci=}"
        for option in "${options[@]}"; do
            [[ ${option} == "${wanted}" || ${option} == "${wanted}="* ]] && return 0
        done
    done
    return 1
}

egpu_managed_hotplug_size_action() {
    local release=$1 cmdline=$2 managed=${3:-0}
    local mode

    [[ ${managed} == 0 || ${managed} == 1 ]] || return 2
    mode=$(egpu_kernel_compat_mode "${release}") || return 2
    if [[ ${mode} == hotplug-size ]]; then
        if egpu_cmdline_has_arg "${cmdline}" "${EGPU_PCI_HOTPLUG_KARG}"; then
            printf '%s\n' keep
        else
            printf '%s\n' add
        fi
    elif [[ ${managed} == 1 ]]; then
        if egpu_cmdline_has_arg "${cmdline}" "${EGPU_PCI_HOTPLUG_KARG}"; then
            printf '%s\n' remove
        else
            printf '%s\n' cleanup
        fi
    else
        printf '%s\n' keep
    fi
}

egpu_managed_realloc_migration_action() {
    local cmdline=$1 managed=${2:-0}

    [[ ${managed} == 0 || ${managed} == 1 ]] || return 2
    if [[ ${managed} == 1 ]]; then
        if egpu_cmdline_has_arg "${cmdline}" "${EGPU_PCI_REALLOC_KARG}"; then
            printf '%s\n' remove
        else
            printf '%s\n' cleanup
        fi
    else
        printf '%s\n' keep
    fi
}
