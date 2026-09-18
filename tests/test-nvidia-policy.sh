#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname -- "${SCRIPT_DIR}")"
# shellcheck source=../egpu-nvidia-policy.sh
source "${REPO_DIR}/egpu-nvidia-policy.sh"

failures=0

expect_stream_match() {
    local input=$1 description=$2
    if printf '%s\n' "${input}" | egpu_nvidia_stream_has_contiguous_policy; then
        printf 'PASS  %s\n' "${description}"
    else
        printf 'FAIL  %s\n' "${description}" >&2
        failures=$((failures + 1))
    fi
}

expect_stream_miss() {
    local input=$1 description=$2
    if printf '%s\n' "${input}" | egpu_nvidia_stream_has_contiguous_policy; then
        printf 'FAIL  %s\n' "${description}" >&2
        failures=$((failures + 1))
    else
        printf 'PASS  %s\n' "${description}"
    fi
}

expect_live_match() {
    local input=$1 description=$2
    local params
    params=$(mktemp)
    printf '%s\n' "${input}" > "${params}"
    if egpu_nvidia_live_has_contiguous_policy "${params}"; then
        printf 'PASS  %s\n' "${description}"
    else
        printf 'FAIL  %s\n' "${description}" >&2
        failures=$((failures + 1))
    fi
    rm -f -- "${params}"
}

expect_live_miss() {
    local input=$1 description=$2
    local params
    params=$(mktemp)
    printf '%s\n' "${input}" > "${params}"
    if egpu_nvidia_live_has_contiguous_policy "${params}"; then
        printf 'FAIL  %s\n' "${description}" >&2
        failures=$((failures + 1))
    else
        printf 'PASS  %s\n' "${description}"
    fi
    rm -f -- "${params}"
}

expect_stream_match \
    'options nvidia NVreg_RegistryDwords=RMDisableNoncontigAlloc=1' \
    'exact Bazzite policy is detected'
expect_stream_match \
    'options nvidia NVreg_UseKernelSuspendNotifiers=1 NVreg_RegistryDwords=RMDisableNoncontigAlloc=1 NVreg_PreserveVideoMemoryAllocations=1' \
    'policy is detected among other NVIDIA options'
expect_stream_miss \
    'options nvidia NVreg_RegistryDwords=SomethingElse=1' \
    'unrelated RegistryDwords policy is ignored'
expect_stream_miss \
    'options nvidia_drm NVreg_RegistryDwords=RMDisableNoncontigAlloc=1' \
    'policy on the wrong module is ignored'

expect_live_match \
    'RegistryDwords: "RMDisableNoncontigAlloc=1"' \
    'quoted live RegistryDwords policy is detected'
expect_live_match \
    'RegistryDwords: "Other=1;RMDisableNoncontigAlloc=1"' \
    'live policy is detected among other registry dwords'
expect_live_miss \
    'RegistryDwords: ""' \
    'empty live RegistryDwords is rejected'

policy_tmp=$(mktemp -d)
EGPU_NVIDIA_POLICY_SKIP_ONCE="${policy_tmp}/etc/skip-once"
EGPU_NVIDIA_POLICY_SKIP_ACTIVE="${policy_tmp}/run/skip-active"
install -D -m 0644 /dev/null "${EGPU_NVIDIA_POLICY_SKIP_ONCE}"
if egpu_nvidia_skip_contiguous_policy_this_boot &&
   [[ ! -e ${EGPU_NVIDIA_POLICY_SKIP_ONCE} && -e ${EGPU_NVIDIA_POLICY_SKIP_ACTIVE} ]]; then
    printf 'PASS  one-shot policy latch is consumed into the boot marker\n'
else
    printf 'FAIL  one-shot policy latch was not consumed correctly\n' >&2
    failures=$((failures + 1))
fi
if egpu_nvidia_skip_contiguous_policy_this_boot; then
    printf 'PASS  boot marker keeps policy omitted for controlled loader retries\n'
else
    printf 'FAIL  boot marker did not retain the diagnostic policy\n' >&2
    failures=$((failures + 1))
fi
rm -rf -- "${policy_tmp}"

printf '\nNVIDIA policy tests complete: %d failure(s).\n' "${failures}"
(( failures == 0 ))
