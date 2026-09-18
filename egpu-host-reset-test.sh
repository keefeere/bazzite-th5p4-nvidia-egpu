#!/usr/bin/env bash
# Stage only a host_reset experiment; never suspend, reboot or unload drivers.
set -Eeuo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/egpu-kernel-compat.sh"
action=${1:-status}
[[ $# -le 1 ]] || { echo "Usage: $0 [status|arm|arm-nodock|cancel]" >&2; exit 2; }
case ${action} in
    status)
        printf 'Live host_reset: '
        cat /sys/module/thunderbolt/parameters/host_reset
        printf 'Live experiment: '
        if egpu_host_reset_test_active "$(</proc/cmdline)" "$(cat /sys/module/thunderbolt/parameters/host_reset)"; then
            echo active
        else
            echo inactive
        fi
        echo 'Next-boot arguments:'
        rpm-ostree kargs
        exit 0 ;;
    arm|arm-nodock|cancel) ;;
    *) echo "Usage: $0 [status|arm|arm-nodock|cancel]" >&2; exit 2 ;;
esac
[[ ${EUID} == 0 ]] || { echo 'Run with sudo.' >&2; exit 1; }
kargs=$(rpm-ostree kargs)
if [[ ${action} == cancel ]]; then
    rpm-ostree status --json | jq -e '.transaction == null' >/dev/null || {
        echo 'Another transaction is active; wait for it before cancellation.' >&2; exit 1;
    }
    if ! egpu_cmdline_has_arg "${kargs}" "${EGPU_TB_HOST_RESET_TEST_KARG}"; then
        rpm-ostree status --json | jq -e \
            '([.deployments[] | select(.staged == true)] | length == 0)' >/dev/null || {
            echo 'An unmarked deployment is still staged; recovery is not finalized. Do not test sleep.' >&2
            exit 1
        }
        echo 'No marked experiment is staged; nothing removed.'
        exit 0
    fi
    rpm-ostree kargs --delete-if-present="${EGPU_TB_HOST_RESET_KARG}" \
        --delete-if-present="${EGPU_TB_HOST_RESET_TEST_KARG}" \
        --delete-if-present="${EGPU_TB_HOST_RESET_NODOCK_KARG}"
    # A failed suspend requires a hard power-cycle, which never reaches the
    # shutdown-time ostree-finalize-staged.service. Finalize the bootloader
    # entry now so that the recovery deployment survives that exact failure.
    ostree admin finalize-staged
    next_kargs=$(rpm-ostree kargs)
    for token in ${next_kargs}; do
        case ${token} in
            thunderbolt.host_reset=*|egpu.host_reset_test=*|egpu.host_reset_nodock=*)
                echo "Recovery finalization retained an experimental argument: ${token}" >&2
                exit 1
                ;;
        esac
    done
    rpm-ostree status --json | jq -e \
        '.transaction == null and ([.deployments[] | select(.staged == true)] | length == 0)' >/dev/null || {
        echo 'Recovery deployment was not fully finalized; do not run the suspend test.' >&2
        exit 1
    }
    echo 'Normal USB4 reset policy finalized for the next boot. Current boot is unchanged.'
    echo 'The normal-policy entry is finalized; bootloader default/selection must still choose it.'
    exit 0
fi
[[ $(egpu_kernel_compat_mode "$(uname -r)") == hotplug-size ]] || {
    echo 'This experiment is only for the 7.2+ path; legacy behavior is unchanged.' >&2; exit 1;
}
# Do not combine an OS update or another pending deployment with this A/B.
rpm-ostree status --json | jq -e \
    '.transaction == null and .deployments[0].booted == true and ([.deployments[] | select(.staged == true)] | length == 0)' >/dev/null || {
    echo 'Pending transaction/deployment exists; resolve it before this isolated A/B.' >&2; exit 1;
}
for token in ${kargs}; do
    case ${token} in
        thunderbolt.host_reset=*|egpu.host_reset_test=*|egpu.host_reset_nodock=*)
            echo "Existing reset policy/experiment: ${token}; refusing to replace it." >&2; exit 1 ;;
    esac
done
[[ -r /etc/egpu-nvidia/hardware.conf ]] || { echo 'Install the eGPU stack first.' >&2; exit 1; }
# Preserve installed copies, including any local edits, before updating only
# these diagnostic-aware helpers. Do not run the full graphics installer.
backup=$(mktemp -d /var/lib/egpu-host-reset-test-backup.XXXXXXXX)
existing_files=(egpu-kernel-compat.sh egpu-cold-hp-dynamic-rebar.sh verify-egpu-install.sh
                egpu-boot.sh egpu-local-reserve-verify.sh)
new_files=(egpu-host-reset-nodock.sh egpu-existing-resources.py)
for file in "${existing_files[@]}"; do
    [[ -f /etc/egpu-nvidia/${file} && -f ${SCRIPT_DIR}/${file} ]] || {
        echo "Missing installed/source helper ${file}; refusing." >&2; exit 1;
    }
    bash -n "${SCRIPT_DIR}/${file}"
    cp -a -- "/etc/egpu-nvidia/${file}" "${backup}/${file}"
done
for file in "${new_files[@]}"; do
    [[ -f ${SCRIPT_DIR}/${file} ]] || { echo "Missing ${file}" >&2; exit 1; }
    if [[ -e /etc/egpu-nvidia/${file} ]]; then
        cp -a -- "/etc/egpu-nvidia/${file}" "${backup}/${file}"
    fi
done
# Compile in memory, without creating __pycache__ in the installed directory.
python3 -c 'import ast,sys; ast.parse(open(sys.argv[1]).read())' "${SCRIPT_DIR}/egpu-existing-resources.py"
bash -n "${SCRIPT_DIR}/egpu-host-reset-nodock.sh"
for file in "${existing_files[@]}" "${new_files[@]}"; do
    install -m 0755 "${SCRIPT_DIR}/${file}" "/etc/egpu-nvidia/${file}"
    restorecon "/etc/egpu-nvidia/${file}"
done
echo "Original helpers saved in ${backup}"
karg_changes=(--append-if-missing="${EGPU_TB_HOST_RESET_KARG}"
              --append-if-missing="${EGPU_TB_HOST_RESET_TEST_KARG}")
if [[ ${action} == arm-nodock ]]; then
    karg_changes+=(--append-if-missing="${EGPU_TB_HOST_RESET_NODOCK_KARG}")
fi
rpm-ostree kargs "${karg_changes[@]}"
if [[ ${action} == arm-nodock ]]; then
    echo 'No-dock A/B staged. Power off XAX, disconnect HP from TH5P4, then boot with powered TH5P4 + RTX already attached.'
    echo 'Only existing valid full-size GPU resources will be accepted; no PCI rescan or ReBAR resize.'
    echo 'All three empty downstream PCIe hotplug ports will be quarantined. Do not attach HP in this boot.'
else
    echo 'A/B staged. Reboot with the same enclosure/dock chain already powered and attached.'
fi
echo 'Sleep guard is unchanged. Verify graphics first; do not request sleep yet.'
echo 'This is NOT an automatic one-shot: after boot, run this script with cancel to stage the normal policy before testing sleep.'
echo 'If boot fails, choose the previous deployment in the boot menu.'
