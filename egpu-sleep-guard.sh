#!/usr/bin/env bash
# ExecStartPre gate; direct invocation is read-only. A diagnostic service
# invocation may consume its explicitly armed one-shot exception.
set -Eeuo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/egpu-pci-lib.sh"
[[ $# -le 1 ]] || exit 2
sleep_unit=${1:-check}
case ${sleep_unit} in
    check|systemd-suspend.service|systemd-hibernate.service|systemd-hybrid-sleep.service|systemd-suspend-then-hibernate.service) ;;
    *) echo 'eGPU sleep guard: unknown invocation; refusing sleep.' >&2; exit 2 ;;
esac

# Alternate tree is for hardware-free tests only; systemd supplies no override.
sysfs=${EGPU_SLEEP_SYSFS:-/sys}
if [[ ${sysfs} == /sys ]]; then
    procfs=/proc
    runtime=/run
    pm_test=/sys/power/pm_test
    marker_owner=0
else
    # Alternate paths and owner are accepted only with the fake sysfs tree.
    procfs=${EGPU_SLEEP_PROCFS:-/proc}
    runtime=${EGPU_SLEEP_RUNTIME:-/run}
    pm_test=${EGPU_SLEEP_PM_TEST:-/sys/power/pm_test}
    marker_owner=${EGPU_SLEEP_MARKER_OWNER:-0}
fi
platform_once=${runtime}/egpu-sleep-guard-platform-once
for bus in pci thunderbolt usb; do
    if [[ ! -d ${sysfs}/bus/${bus}/devices ]]; then
        echo "eGPU sleep guard: cannot inspect ${bus}; refusing sleep." >&2
        exit 2
    fi
done

consume_platform_once() {
    [[ -e ${platform_once} || -L ${platform_once} ]] || return 1
    if [[ -L ${platform_once} || ! -f ${platform_once} ]]; then
        echo "eGPU sleep guard: invalid one-shot marker; refusing sleep." >&2
        return 2
    fi

    local owner mode boot_id active line token exact name_count=0 exact_count=0
    owner=$(stat -c %u -- "${platform_once}") || return 2
    mode=$(stat -c %a -- "${platform_once}") || return 2
    mapfile -t line < "${platform_once}" || return 2
    boot_id=$(cat "${procfs}/sys/kernel/random/boot_id" 2>/dev/null) || return 2
    active=$(sed -n 's/.*\[\([^]]*\)\].*/\1/p' "${pm_test}" 2>/dev/null) || return 2

    for exact in thunderbolt.host_reset=0 egpu.host_reset_test=1 egpu.host_reset_nodock=1; do
        name_count=0
        exact_count=0
        while read -r token; do
            [[ ${token%%=*} == "${exact%%=*}" ]] && ((name_count += 1))
            [[ ${token} == "${exact}" ]] && ((exact_count += 1))
        done < <(tr ' ' '\n' < "${procfs}/cmdline")
        if ((name_count != 1 || exact_count != 1)); then
            rm -f -- "${platform_once}"
            echo "eGPU sleep guard: one-shot marker does not match the exact host_reset experiment; refusing sleep." >&2
            return 2
        fi
    done

    if [[ ${owner} != "${marker_owner}" || ${mode} != 600 || ${#line[@]} != 2 ||
          ${line[0]} != "${boot_id}" || ${line[1]} != platform || ${active} != platform ||
          $(cat "${sysfs}/module/thunderbolt/parameters/host_reset" 2>/dev/null) != N ]]; then
        rm -f -- "${platform_once}"
        echo "eGPU sleep guard: invalid or stale one-shot platform marker; refusing sleep." >&2
        return 2
    fi

    # Consume before systemd-sleep starts. A retry is blocked even if this test hangs.
    rm -- "${platform_once}"
    echo "eGPU sleep guard: consumed the one-shot host_reset=0 platform diagnostic; allowing this invocation only."
    return 0
}

if [[ ${sleep_unit} == systemd-suspend.service && ( -e ${platform_once} || -L ${platform_once} ) ]]; then
    # Serialise consumption so two invocations cannot both use one exception.
    umask 077
    exec {guard_lock}>"${runtime}/egpu-sleep-guard.lock"
    flock -x "${guard_lock}"
    consume_platform_once
    exit $?
fi

matches() {
    local directory=$1 first=$2 first_value=$3 second=$4 second_value=$5 a b
    a=$(cat "${directory}/${first}" 2>/dev/null) || return 1
    b=$(cat "${directory}/${second}" 2>/dev/null) || return 1
    [[ ${a} == "${first_value}" && ${b} == "${second_value}" ]]
}
block() {
    echo "Sleep blocked: ${EGPU_DISPLAY_NAME}/${ENCLOSURE_DISPLAY_NAME} is connected ($1). Safely detach and physically disconnect the enclosure before sleep." >&2
    exit 1
}

for device in "${sysfs}"/bus/pci/devices/*; do
    if matches "${device}" vendor "${EGPU_VENDOR}" device "${EGPU_DEVICE}" ||
       matches "${device}" vendor "${TH5P4_VENDOR}" device "${TH5P4_DEVICE}"; then
        block "PCI ${device##*/}"
    fi
done
for device in "${sysfs}"/bus/thunderbolt/devices/*; do
    if matches "${device}" vendor "${TH5P4_TB_VENDOR}" device "${TH5P4_TB_DEVICE}"; then
        block "USB4 ${device##*/}"
    fi
done
# A stalled enclosure can expose only its USB diagnostic device.
for device in "${sysfs}"/bus/usb/devices/*; do
    if matches "${device}" idVendor "${TH5P4_USB_DIAG_VENDOR}" idProduct "${TH5P4_USB_DIAG_PRODUCT}"; then
        block "USB diagnostic ${device##*/}"
    fi
done
echo "eGPU sleep guard: enclosure absent; sleep allowed."
