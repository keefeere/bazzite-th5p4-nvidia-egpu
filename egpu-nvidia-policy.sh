#!/usr/bin/env bash

# Keep the controlled NVIDIA loader aligned with policy shipped by the active
# immutable deployment.  The eGPU stack deliberately uses `modprobe -C` to
# avoid Bazzite's automatic post-softdeps, which also means host `options`
# lines must be mirrored explicitly at load time.
EGPU_NVIDIA_CONTIGUOUS_POLICY='NVreg_RegistryDwords=RMDisableNoncontigAlloc=1'
EGPU_NVIDIA_POLICY_SKIP_ONCE=${EGPU_NVIDIA_POLICY_SKIP_ONCE:-/etc/egpu-nvidia/skip-contiguous-policy-once}
EGPU_NVIDIA_POLICY_SKIP_ACTIVE=${EGPU_NVIDIA_POLICY_SKIP_ACTIVE:-/run/egpu-nvidia-contiguous-policy-skipped}

egpu_nvidia_stream_has_contiguous_policy() {
    awk -v policy="${EGPU_NVIDIA_CONTIGUOUS_POLICY}" '
        $1 == "options" && $2 == "nvidia" {
            for (field = 3; field <= NF; field++) {
                if ($field == policy) {
                    found = 1
                }
            }
        }
        END { exit(found ? 0 : 1) }
    '
}

egpu_nvidia_host_has_contiguous_policy() {
    modprobe --showconfig 2>/dev/null |
        egpu_nvidia_stream_has_contiguous_policy
}

egpu_nvidia_live_has_contiguous_policy() {
    local params_file=${1:-/proc/driver/nvidia/params}

    [[ -r ${params_file} ]] || return 1
    awk -v policy="${EGPU_NVIDIA_CONTIGUOUS_POLICY#*=}" '
        /^RegistryDwords:/ && index($0, policy) { found = 1 }
        END { exit(found ? 0 : 1) }
    ' "${params_file}"
}

# Consume the persistent latch once, then retain the result in /run for every
# controlled loader retry in the same boot.  The /run marker disappears on the
# following reboot, restoring the deployment policy automatically.
egpu_nvidia_skip_contiguous_policy_this_boot() {
    if [[ -e ${EGPU_NVIDIA_POLICY_SKIP_ACTIVE} ]]; then
        return 0
    fi
    if [[ -e ${EGPU_NVIDIA_POLICY_SKIP_ONCE} ]]; then
        rm -f -- "${EGPU_NVIDIA_POLICY_SKIP_ONCE}"
        install -D -m 0644 /dev/null "${EGPU_NVIDIA_POLICY_SKIP_ACTIVE}"
        return 0
    fi
    return 1
}
