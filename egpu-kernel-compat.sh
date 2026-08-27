#!/usr/bin/env bash

# Linux 7.2 changed how the PCI core normalizes empty hot-plug bridge windows
# after this project's controlled TH5P4 remove/rescan.  Older kernels keep the
# locally programmed windows without a global realloc request and must retain
# that already validated behavior.
EGPU_PCI_REALLOC_MIN_KERNEL="7.2.0"
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

egpu_kernel_requires_pci_realloc() {
    local version

    version=$(egpu_kernel_release_version "${1:-$(uname -r)}") || return 2
    egpu_version_at_least "${version}" "${EGPU_PCI_REALLOC_MIN_KERNEL}"
}

egpu_kernel_compat_mode() {
    if egpu_kernel_requires_pci_realloc "${1:-$(uname -r)}"; then
        printf '%s\n' realloc
    else
        case $? in
            1) printf '%s\n' legacy ;;
            *) return 2 ;;
        esac
    fi
}

egpu_cmdline_has_arg() {
    local cmdline=$1 arg=$2
    [[ " ${cmdline} " == *" ${arg} "* ]]
}

egpu_managed_pci_realloc_action() {
    local release=$1 cmdline=$2 managed=${3:-0}
    local mode

    [[ ${managed} == 0 || ${managed} == 1 ]] || return 2
    mode=$(egpu_kernel_compat_mode "${release}") || return 2
    if [[ ${mode} == realloc ]]; then
        if egpu_cmdline_has_arg "${cmdline}" "${EGPU_PCI_REALLOC_KARG}"; then
            printf '%s\n' keep
        else
            printf '%s\n' add
        fi
    elif [[ ${managed} == 1 ]]; then
        printf '%s\n' remove
    else
        printf '%s\n' keep
    fi
}
