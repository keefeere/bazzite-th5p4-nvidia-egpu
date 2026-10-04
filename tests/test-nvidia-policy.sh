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

procfs_tmp=$(mktemp -d)
EGPU_NVIDIA_PROCFS_PM_ONCE="${procfs_tmp}/once"
EGPU_NVIDIA_PROCFS_PM_ACTIVE="${procfs_tmp}/active"
EGPU_NVIDIA_PROCFS_PM_BOOT_ID="${procfs_tmp}/boot-id"
EGPU_NVIDIA_PROCFS_PM_OWNER=$(id -u)
printf '%s\n' 'test-boot-id' > "${EGPU_NVIDIA_PROCFS_PM_BOOT_ID}"
if egpu_nvidia_procfs_pm_this_boot; then
    printf 'FAIL  unarmed procfs mode was selected\n' >&2
    failures=$((failures + 1))
else
    result=$?
    if (( result == 1 )); then
        printf 'PASS  unarmed procfs mode leaves notifier mode unchanged\n'
    else
        printf 'FAIL  unarmed procfs mode returned %d\n' "${result}" >&2
        failures=$((failures + 1))
    fi
fi
printf '%s\n' 'procfs-pm-v1' > "${EGPU_NVIDIA_PROCFS_PM_ONCE}"
chmod 0600 "${EGPU_NVIDIA_PROCFS_PM_ONCE}"
if egpu_nvidia_procfs_pm_this_boot &&
   [[ ! -e ${EGPU_NVIDIA_PROCFS_PM_ONCE} &&
      $(cat "${EGPU_NVIDIA_PROCFS_PM_ACTIVE}") == test-boot-id ]]; then
    printf 'PASS  procfs A/B latch consumed into this boot only\n'
else
    printf 'FAIL  procfs A/B latch was not consumed correctly\n' >&2
    failures=$((failures + 1))
fi
if egpu_nvidia_procfs_pm_this_boot; then
    printf 'PASS  procfs A/B mode survives controlled loader retries in this boot\n'
else
    printf 'FAIL  procfs A/B boot marker was not honored\n' >&2
    failures=$((failures + 1))
fi
printf '%s\n' 'other-boot' > "${EGPU_NVIDIA_PROCFS_PM_ACTIVE}"
if egpu_nvidia_procfs_pm_this_boot; then
    printf 'FAIL  stale procfs A/B marker was accepted\n' >&2
    failures=$((failures + 1))
else
    result=$?
    if (( result == 2 )); then
        printf 'PASS  stale procfs A/B marker fails closed\n'
    else
        printf 'FAIL  stale procfs A/B marker returned %d\n' "${result}" >&2
        failures=$((failures + 1))
    fi
fi
rm -- "${EGPU_NVIDIA_PROCFS_PM_ACTIVE}"
printf '%s\n' 'wrong-version' > "${EGPU_NVIDIA_PROCFS_PM_ONCE}"
chmod 0600 "${EGPU_NVIDIA_PROCFS_PM_ONCE}"
if egpu_nvidia_procfs_pm_this_boot; then
    printf 'FAIL  malformed procfs A/B latch was accepted\n' >&2
    failures=$((failures + 1))
else
    result=$?
    if (( result == 2 )) && [[ -e ${EGPU_NVIDIA_PROCFS_PM_ONCE} &&
                               ! -e ${EGPU_NVIDIA_PROCFS_PM_ACTIVE} ]]; then
        printf 'PASS  malformed procfs A/B latch is rejected without consumption\n'
    else
        printf 'FAIL  malformed procfs A/B latch returned %d or changed state\n' "${result}" >&2
        failures=$((failures + 1))
    fi
fi
rm -- "${EGPU_NVIDIA_PROCFS_PM_ONCE}"
ln -s -- "${EGPU_NVIDIA_PROCFS_PM_BOOT_ID}" "${EGPU_NVIDIA_PROCFS_PM_ONCE}"
if egpu_nvidia_procfs_pm_this_boot; then
    printf 'FAIL  symlink procfs A/B latch was accepted\n' >&2
    failures=$((failures + 1))
else
    result=$?
    if (( result == 2 )) && [[ -L ${EGPU_NVIDIA_PROCFS_PM_ONCE} ]]; then
        printf 'PASS  symlink procfs A/B latch is rejected without following it\n'
    else
        printf 'FAIL  symlink procfs A/B latch returned %d or changed state\n' "${result}" >&2
        failures=$((failures + 1))
    fi
fi
rm -rf -- "${procfs_tmp}"

default_options=$(sed -n '/^options nvidia /p' "${REPO_DIR}/nvidia-base-only.conf")
procfs_options=$(sed -n '/^options nvidia /p' "${REPO_DIR}/nvidia-base-only-procfs-pm.conf")
if [[ ${default_options} == *NVreg_UseKernelSuspendNotifiers=1* &&
      ${procfs_options} == "${default_options/NVreg_UseKernelSuspendNotifiers=1/NVreg_UseKernelSuspendNotifiers=0}" ]]; then
    printf 'PASS  procfs modprobe config changes only the NVIDIA notifier flag\n'
else
    printf 'FAIL  procfs modprobe config changed another NVIDIA option\n' >&2
    failures=$((failures + 1))
fi

guard_hook='argv[]=/usr/bin/bash /etc/egpu-nvidia/egpu-sleep-guard.sh systemd-suspend.service ; ignore_errors=no'
pre_hook='argv[]=/usr/bin/python3 /etc/egpu-nvidia/egpu-nvidia-procfs-pm.py pre ; ignore_errors=no'
post_hook='argv[]=/usr/bin/python3 /etc/egpu-nvidia/egpu-nvidia-procfs-pm.py post ; ignore_errors=no'
mock_pre="${guard_hook} ; ${pre_hook}"
mock_post="${post_hook}"
systemctl() {
    if [[ $* == *ExecStartPre* ]]; then
        printf '%s\n' "${mock_pre}"
    else
        printf '%s\n' "${mock_post}"
    fi
}
if egpu_nvidia_procfs_pm_hooks_loaded; then
    printf 'PASS  mandatory guard/pre/post hooks in correct order are accepted\n'
else
    printf 'FAIL  correctly ordered procfs PM hooks were rejected\n' >&2
    failures=$((failures + 1))
fi
mock_pre="${pre_hook} ; ${guard_hook}"
if egpu_nvidia_procfs_pm_hooks_loaded; then
    printf 'FAIL  procfs PM pre-hook before the sleep guard was accepted\n' >&2
    failures=$((failures + 1))
else
    printf 'PASS  procfs PM pre-hook before the sleep guard is rejected\n'
fi
mock_pre="${guard_hook} ; ${pre_hook/ignore_errors=no/ignore_errors=yes}"
if egpu_nvidia_procfs_pm_hooks_loaded; then
    printf 'FAIL  ignored procfs PM pre-hook was accepted\n' >&2
    failures=$((failures + 1))
else
    printf 'PASS  ignored procfs PM pre-hook is rejected\n'
fi
mock_pre="${guard_hook} ; ${pre_hook}"
mock_post="${post_hook/ignore_errors=no/ignore_errors=yes}"
if egpu_nvidia_procfs_pm_hooks_loaded; then
    printf 'FAIL  ignored procfs PM resume-hook was accepted\n' >&2
    failures=$((failures + 1))
else
    printf 'PASS  ignored procfs PM resume-hook is rejected\n'
fi

printf '\nNVIDIA policy tests complete: %d failure(s).\n' "${failures}"
(( failures == 0 ))
