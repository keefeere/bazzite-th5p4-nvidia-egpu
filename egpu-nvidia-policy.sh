#!/usr/bin/env bash

# Keep the controlled NVIDIA loader aligned with policy shipped by the active
# immutable deployment.  The eGPU stack deliberately uses `modprobe -C` to
# avoid Bazzite's automatic post-softdeps, which also means host `options`
# lines must be mirrored explicitly at load time.
EGPU_NVIDIA_CONTIGUOUS_POLICY='NVreg_RegistryDwords=RMDisableNoncontigAlloc=1'

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
