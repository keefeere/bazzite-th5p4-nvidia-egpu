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
expect_equal realloc "$(egpu_kernel_compat_mode 7.2.0-ogc6.1.fc44.x86_64)" \
    "new Bazzite kernel selects realloc mode"
expect_equal realloc "$(egpu_kernel_compat_mode 8.0.0-test)" \
    "future kernel remains on guarded realloc mode"

expect_equal keep "$(egpu_managed_pci_realloc_action 6.17.7 'quiet splash' 0)" \
    "old kernel leaves its no-realloc flow untouched"
expect_equal keep "$(egpu_managed_pci_realloc_action 6.17.7 'quiet pci=realloc=on' 0)" \
    "old kernel preserves a user-owned realloc argument"
expect_equal remove "$(egpu_managed_pci_realloc_action 6.17.7 'quiet pci=realloc=on' 1)" \
    "old kernel removes only the stack-owned realloc argument"
expect_equal add "$(egpu_managed_pci_realloc_action 7.2.0 'quiet splash' 0)" \
    "new kernel adds its required realloc argument"
expect_equal keep "$(egpu_managed_pci_realloc_action 7.2.0 'quiet pci=realloc=on' 0)" \
    "new kernel preserves a pre-existing user-owned argument"
expect_equal keep "$(egpu_managed_pci_realloc_action 7.2.0 'quiet pci=realloc=on' 1)" \
    "new kernel keeps the stack-owned argument"

if egpu_cmdline_has_arg 'quiet pci=realloc=on splash' 'pci=realloc=on' &&
   ! egpu_cmdline_has_arg 'quiet foo=pci=realloc=onward splash' 'pci=realloc=on'; then
    printf 'PASS  exact kernel argument matching\n'
else
    printf 'FAIL  exact kernel argument matching\n' >&2
    failures=$((failures + 1))
fi

printf '\nKernel compatibility tests complete: %d failure(s).\n' "${failures}"
(( failures == 0 ))
