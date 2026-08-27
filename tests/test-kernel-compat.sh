#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname -- "${SCRIPT_DIR}")"
# shellcheck source=../egpu-kernel-compat.sh
source "${REPO_DIR}/egpu-kernel-compat.sh"

failures=0

expect_equal() {
    local expected=$1 actual=$2 description=$3
    if [[ ${actual} == "${expected}" ]]; then
        printf 'PASS  %s\n' "${description}"
    else
        printf 'FAIL  %s: expected %q, got %q\n' "${description}" "${expected}" "${actual}" >&2
        failures=$((failures + 1))
    fi
}

expect_failure() {
    local description=$1
    shift
    if "$@" >/dev/null 2>&1; then
        printf 'FAIL  %s: command unexpectedly succeeded\n' "${description}" >&2
        failures=$((failures + 1))
    else
        printf 'PASS  %s\n' "${description}"
    fi
}

expect_equal 6.17.7 "$(egpu_kernel_release_version 6.17.7-ba29.fc43.x86_64)" \
    "Bazzite 6.17 release parsing"
expect_equal 7.2.0 "$(egpu_kernel_release_version 7.2.0-ogc6.1.fc44.x86_64)" \
    "Bazzite 7.2 release parsing"
expect_equal 8.0.0 "$(egpu_kernel_release_version 8.0)" \
    "two-component future release parsing"
expect_failure "invalid release is rejected" egpu_kernel_release_version rolling

expect_equal legacy "$(egpu_kernel_compat_mode 6.17.7-ba29.fc43.x86_64)" \
    "old validated kernel selects legacy mode"
expect_equal legacy "$(egpu_kernel_compat_mode 7.1.99-test)" \
    "last pre-threshold kernel selects legacy mode"
expect_equal hotplug-size "$(egpu_kernel_compat_mode 7.2.0-ogc6.1.fc44.x86_64)" \
    "new Bazzite kernel selects hot-plug sizing mode"
expect_equal hotplug-size "$(egpu_kernel_compat_mode 8.0.0-test)" \
    "future kernel remains on guarded hot-plug sizing mode"

expect_equal $'[Unit]\nBefore=bolt.service' "$(egpu_boot_order_dropin legacy)" \
    "old kernel keeps eGPU staging before boltd"
expect_equal $'[Unit]\nWants=bolt.service\nAfter=bolt.service' \
    "$(egpu_boot_order_dropin hotplug-size)" \
    "new kernel authorizes the replacement USB4 tunnel before eGPU staging"
expect_failure "unknown boot-order mode is rejected" \
    egpu_boot_order_dropin unsupported

expect_equal add "$(egpu_managed_host_reset_action 6.17.7 'quiet splash')" \
    "old kernel adds its validated host-reset workaround"
expect_equal keep "$(egpu_managed_host_reset_action 6.17.7 'quiet thunderbolt.host_reset=0')" \
    "old kernel keeps its validated host-reset workaround"
expect_equal remove "$(egpu_managed_host_reset_action 7.2.0 'quiet thunderbolt.host_reset=0')" \
    "new kernel removes the legacy host-reset workaround"
expect_equal keep "$(egpu_managed_host_reset_action 7.2.0 'quiet splash')" \
    "new kernel keeps the upstream host-router reset default"

new_karg='pci=hpmmiosize=32M,hpmmioprefsize=32M'
expect_equal keep "$(egpu_managed_hotplug_size_action 6.17.7 'quiet splash' 0)" \
    "old kernel leaves its validated flow untouched"
expect_equal remove "$(egpu_managed_hotplug_size_action 6.17.7 "quiet ${new_karg}" 1)" \
    "old kernel removes only the stack-owned new sizing argument"
expect_equal cleanup "$(egpu_managed_hotplug_size_action 6.17.7 'quiet splash' 1)" \
    "old kernel cleans a stale sizing ownership marker"
expect_equal add "$(egpu_managed_hotplug_size_action 7.2.0 'quiet splash' 0)" \
    "new kernel adds its required sizing argument"
expect_equal keep "$(egpu_managed_hotplug_size_action 7.2.0 "quiet ${new_karg}" 0)" \
    "new kernel preserves a pre-existing user-owned sizing argument"
expect_equal keep "$(egpu_managed_hotplug_size_action 7.2.0 "quiet ${new_karg}" 1)" \
    "new kernel keeps the stack-owned sizing argument"

expect_equal remove "$(egpu_managed_realloc_migration_action 'quiet pci=realloc=on' 1)" \
    "rejected stack-owned realloc argument is removed"
expect_equal cleanup "$(egpu_managed_realloc_migration_action 'quiet splash' 1)" \
    "stale realloc ownership marker is cleaned"
expect_equal keep "$(egpu_managed_realloc_migration_action 'quiet pci=realloc=on' 0)" \
    "user-owned realloc remains outside automatic deletion"

if egpu_cmdline_has_arg 'quiet pci=realloc=on splash' 'pci=realloc=on' &&
   ! egpu_cmdline_has_arg 'quiet foo=pci=realloc=onward splash' 'pci=realloc=on'; then
    printf 'PASS  exact kernel argument matching\n'
else
    printf 'FAIL  exact kernel argument matching\n' >&2
    failures=$((failures + 1))
fi

if egpu_cmdline_has_pci_option "quiet ${new_karg}" hpmmiosize &&
   egpu_cmdline_has_pci_option "quiet ${new_karg}" hpmmioprefsize &&
   ! egpu_cmdline_has_pci_option "quiet ${new_karg}" hpbussize; then
    printf 'PASS  combined PCI option parsing\n'
else
    printf 'FAIL  combined PCI option parsing\n' >&2
    failures=$((failures + 1))
fi

printf '\nKernel compatibility tests complete: %d failure(s).\n' "${failures}"
(( failures == 0 ))
