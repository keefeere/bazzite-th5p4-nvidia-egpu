#!/usr/bin/env bash
set -Eeuo pipefail

if [[ ${EUID} -ne 0 ]]; then
    echo "Run this script as root." >&2
    exit 1
fi

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
LOAD_SCRIPT="${SCRIPT_DIR}/egpu-load-nvidia-gen3.sh"
LOCAL_RESERVE_SCRIPT="${SCRIPT_DIR}/egpu-local-reserve-apply.sh"
LOCAL_COLD_HP_REBUILD="${SCRIPT_DIR}/egpu-cold-hp-pci-rebuild.sh"
LOCAL_COLD_DYNAMIC_REBAR="${SCRIPT_DIR}/egpu-cold-hp-dynamic-rebar.sh"
LOCAL_RESERVE_VERIFY="${SCRIPT_DIR}/egpu-local-reserve-verify.sh"
LOCAL_COLD_DOCK_VERIFY="${SCRIPT_DIR}/egpu-cold-attached-hp-verify.sh"
LOCAL_RESERVE_ENABLE="/etc/egpu-nvidia/enable-local-reserve"
LOCAL_RESERVE_MARKER="/run/egpu-local-reserve-applied"
LOCAL_RESERVE_FAILURE_MARKER="/run/egpu-local-reserve-failed"
GEN4_ONCE_REQUEST="/etc/egpu-nvidia/try-gen4-once"
GEN4_PERSISTENT="/etc/egpu-nvidia/use-gen4"
GEN4_ACTIVE_MARKER="/run/egpu-gen4-test-active"
KWIN_ENV="/etc/environment.d/10kwin-egpu.conf"
TEST_ENV="/etc/environment.d/10kwin-egpu-test.conf"
LOCK_FILE="/run/egpu-nvidia-transition.lock"
PENDING_MARKER="/run/egpu-nvidia-hotplug-pending"

# shellcheck source=egpu-pci-lib.sh
source "${SCRIPT_DIR}/egpu-pci-lib.sh"
# shellcheck source=egpu-kernel-compat.sh
source "${SCRIPT_DIR}/egpu-kernel-compat.sh"

# A cold-attached downstream dock delayed the exact RTX endpoint until the
# same timestamp at which the former 5-second guard expired. This wait is used
# only after the configured TH5P4 router is already present, so it does not
# delay an ordinary AMD-only boot. Profiles may select a reviewed 1..30 value.
ENDPOINT_WAIT_SECONDS=${EGPU_ENDPOINT_WAIT_SECONDS:-12}
[[ ${ENDPOINT_WAIT_SECONDS} =~ ^[1-9][0-9]*$ ]] &&
    ((ENDPOINT_WAIT_SECONDS <= 30)) || {
        echo "Invalid EGPU_ENDPOINT_WAIT_SECONDS=${ENDPOINT_WAIT_SECONDS}; expected 1..30." >&2
        exit 1
    }
ENDPOINT_WAIT_STEPS=$((ENDPOINT_WAIT_SECONDS * 5))

refresh_gpu_bdf() {
    GPU="$(find_unique_pci_device "${EGPU_VENDOR}" "${EGPU_DEVICE}" 2>/dev/null || true)"
    [[ -n ${GPU} ]]
}

acquire_transition_lock() {
    if [[ ${EGPU_TRANSITION_LOCK_HELD:-0} == 1 ]]; then
        return
    fi

    exec 9>"${LOCK_FILE}"
    flock -x 9
}

expected_router_present() {
    th5p4_router_present
}

hp_pci_subtree_present() {
    local port1 sysfs bdf

    port1="${BRIDGE%:*}:01.0"
    for sysfs in /sys/bus/pci/devices/0000:*; do
        bdf=${sysfs##*/}
        [[ ${bdf} == "${port1}" ]] && continue
        if is_pci_descendant_of "${bdf}" "${port1}"; then
            return 0
        fi
    done
    return 1
}

record_local_reserve_failure() {
    local stage=$1
    local rc=$2

    printf '%s\n' \
        "The early local ${ENCLOSURE_DISPLAY_NAME} PCI reserve failed during: ${stage}." \
        "Kernel: $(uname -r); compatibility mode: $(egpu_kernel_compat_mode "$(uname -r)" 2>/dev/null || echo unknown)." \
        "NVIDIA stayed blocked so ${IGPU_DISPLAY_NAME} remains the fallback. Check egpu-nvidia-boot.service." \
        > "${LOCAL_RESERVE_FAILURE_MARKER}"
    return "${rc}"
}

run_local_reserve_step() {
    local stage=$1
    local rc
    shift

    if "$@"; then
        return 0
    else
        rc=$?
        record_local_reserve_failure "${stage}" "${rc}"
    fi
}

acquire_transition_lock
rm -f -- "${LOCAL_RESERVE_FAILURE_MARKER}"

# Never carry a stale eGPU-primary setting into an AMD-only login.
rm -f -- "${KWIN_ENV}" "${TEST_ENV}"

# A connected TH5P4 can enumerate its USB router during the normal udev
# coldplug while its PCIe tunnel and NVIDIA endpoint appear much later. Wait
# briefly for the exact router first; only a matching enclosure earns the long
# endpoint wait, so an AMD-only boot is not delayed by a full minute.
if ! refresh_gpu_bdf; then
    for _ in {1..15}; do
        expected_router_present && break
        sleep 0.2
    done
fi

if ! refresh_gpu_bdf && ! expected_router_present; then
    echo "${ENCLOSURE_DISPLAY_NAME} is absent; leaving KWin on ${IGPU_DISPLAY_NAME}."
    exit 0
fi

if ! refresh_gpu_bdf; then
    echo "${ENCLOSURE_DISPLAY_NAME} is present; waiting up to ${ENDPOINT_WAIT_SECONDS} seconds for its PCIe tunnel and ${EGPU_DISPLAY_NAME} endpoint..."
    for (( attempt = 0; attempt < ENDPOINT_WAIT_STEPS; attempt++ )); do
        refresh_gpu_bdf && break
        sleep 0.2
    done
fi

if ! refresh_gpu_bdf; then
    echo "${ENCLOSURE_DISPLAY_NAME} stayed present but its ${EGPU_DISPLAY_NAME} endpoint did not appear within ${ENDPOINT_WAIT_SECONDS} seconds." >&2
    echo "Leaving KWin on ${IGPU_DISPLAY_NAME}; a later endpoint will remain driverless until a cold boot with the enclosure attached." >&2
    exit 0
fi

resolve_egpu_topology

if [[ -e ${LOCAL_RESERVE_ENABLE} ]]; then
    kernel_compat_mode=$(egpu_kernel_compat_mode "$(uname -r)") || {
        record_local_reserve_failure "kernel release parsing" 1 || true
        echo "Unsupported kernel release format: $(uname -r)" >&2
        exit 1
    }
    if [[ ${kernel_compat_mode} == hotplug-size ]]; then
        if egpu_cmdline_has_arg "$(</proc/cmdline)" "${EGPU_PCI_REALLOC_KARG}"; then
            record_local_reserve_failure \
                "kernel compatibility preflight (rejected ${EGPU_PCI_REALLOC_KARG})" 1 || true
            echo "The rejected ${EGPU_PCI_REALLOC_KARG} mode globally repacks TH5P4 on Linux 7.2+." >&2
            echo "Rerun install-egpu-all.sh and reboot into its staged deployment." >&2
            exit 1
        fi
        if ! egpu_cmdline_has_arg "$(</proc/cmdline)" "${EGPU_PCI_HOTPLUG_KARG}"; then
            record_local_reserve_failure \
                "kernel compatibility preflight (missing ${EGPU_PCI_HOTPLUG_KARG})" 1 || true
            echo "Linux ${EGPU_PCI_COMPAT_MIN_KERNEL}+ requires the exact ${EGPU_PCI_HOTPLUG_KARG} compatibility argument for the controlled TH5P4 rescan." >&2
            echo "Rerun install-egpu-all.sh and reboot into its staged deployment." >&2
            exit 1
        fi
    elif egpu_cmdline_has_arg "$(</proc/cmdline)" "${EGPU_PCI_HOTPLUG_KARG}"; then
        record_local_reserve_failure \
            "legacy compatibility preflight (stale ${EGPU_PCI_HOTPLUG_KARG})" 1 || true
        echo "The legacy kernel must retain the previously validated no-hotplug-sizing flow." >&2
        echo "Rerun install-egpu-all.sh and reboot into its staged deployment." >&2
        exit 1
    fi

    if [[ -s ${LOCAL_RESERVE_MARKER} ]]; then
        if hp_dock_router_present; then
            run_local_reserve_step "cold-attached dock verification" "${LOCAL_COLD_DOCK_VERIFY}"
        else
            run_local_reserve_step "local reserve verification" "${LOCAL_RESERVE_VERIFY}"
        fi
    elif grep -q '^nvidia ' /proc/modules; then
        echo "NVIDIA is live without a verified local-reserve or cold-dock marker." >&2
        exit 1
    else
        # Let a physically cold-attached downstream router become visible
        # before deciding that the hot-add reservation path is appropriate.
        for _ in {1..5}; do
            hp_dock_router_present && break
            sleep 0.2
        done
        if hp_dock_router_present; then
            if hp_pci_subtree_present; then
                if [[ ${kernel_compat_mode} == hotplug-size ]]; then
                    run_local_reserve_step \
                        "cold-attached dock dynamic ReBAR repair" \
                        env EGPU_DYNAMIC_REBAR_BOOT_CONTEXT=1 "${LOCAL_COLD_DYNAMIC_REBAR}"
                else
                    run_local_reserve_step "cold-attached dock PCI rebuild" "${LOCAL_COLD_HP_REBUILD}"
                fi
            else
                hp_router="$(find_hp_dock_router_dir)"
                if [[ $(cat -- "${hp_router}/authorized" 2>/dev/null || true) == 1 ]]; then
                    # The router/tunnel exists but Linux has no imported HP PCI
                    # child yet.  Preserve authorization and let apply's single
                    # rescan import it into the prepared windows.
                    run_local_reserve_step \
                        "local reserve with cold-attached dock import" \
                        env EGPU_COLD_HP_PCI_REBUILD=1 "${LOCAL_RESERVE_SCRIPT}"
                else
                    # A connected but unauthorized HP router has no PCIe
                    # tunnel.  Reserve the empty port; boltd can hot-add it
                    # after this early boot service has completed.
                    run_local_reserve_step "local reserve with unauthorized dock" "${LOCAL_RESERVE_SCRIPT}"
                fi
            fi
        else
            if [[ ${kernel_compat_mode} == hotplug-size ]]; then
                run_local_reserve_step \
                    "no-dock cold-boot dynamic ReBAR repair" \
                    env EGPU_DYNAMIC_REBAR_BOOT_CONTEXT=1 "${LOCAL_COLD_DYNAMIC_REBAR}"
            else
                run_local_reserve_step "local reserve apply" "${LOCAL_RESERVE_SCRIPT}"
            fi
        fi
        resolve_egpu_topology
    fi
    rm -f -- "${LOCAL_RESERVE_FAILURE_MARKER}"
fi

link_generation=3
if [[ ${EGPU_FORCE_GEN3:-0} == 1 ]]; then
    echo "A controlled hot-attach requested conservative Gen3."
elif [[ -e ${GEN4_ACTIVE_MARKER} &&
      ${EGPU_LOCAL_RESERVE_BOOT_CONTEXT:-0} == 1 ]]; then
    link_generation=4
elif ! grep -q '^nvidia ' /proc/modules &&
     [[ -e ${GEN4_ONCE_REQUEST} ]] &&
     [[ ${EGPU_LOCAL_RESERVE_BOOT_CONTEXT:-0} == 1 ]] &&
     ! systemctl is-active --quiet display-manager.service; then
    # Consume before retraining. A reset or failed boot therefore falls back to
    # the stable Gen3 path automatically on the next boot. Restrict this to the
    # early boot service so a pending test can never become a live hot-plug
    # retrain after Plasma has started.
    rm -f -- "${GEN4_ONCE_REQUEST}"
    printf '%s\n' "Guarded Gen4 x4 request is active for this boot." > "${GEN4_ACTIVE_MARKER}"
    link_generation=4
elif [[ -e ${GEN4_ONCE_REQUEST} ]]; then
    echo "Gen4 request left pending: this is not a safe early-boot transition."
elif [[ -e ${GEN4_ACTIVE_MARKER} ]]; then
    echo "Ignoring the current-boot Gen4 marker outside the guarded early-boot path; using Gen3."
fi

if grep -q '^nvidia ' /proc/modules; then
    if ! grep -q '^nvidia_drm ' /proc/modules; then
        echo "NVIDIA core is loaded without NVIDIA DRM; refusing a partial-stack takeover." >&2
        exit 1
    fi
    echo "NVIDIA is already initialized; refreshing the KWin GPU order only."
else
    EGPU_PCIE_GENERATION="${link_generation}" "${LOAD_SCRIPT}"
fi

rm -f -- "${PENDING_MARKER}"

egpu_card="$(find "/sys/bus/pci/devices/${GPU}/drm" -mindepth 1 -maxdepth 1 -name 'card[0-9]*' -printf '/dev/dri/%f\n' | head -n 1)"
igpu_card="$(find "/sys/bus/pci/devices/${IGPU}/drm" -mindepth 1 -maxdepth 1 -name 'card[0-9]*' -printf '/dev/dri/%f\n' | head -n 1)"

if [[ -z ${egpu_card} || ! -c ${egpu_card} ]]; then
    echo "Could not resolve the NVIDIA DRM card after loading the driver." >&2
    exit 1
fi
if [[ -z ${igpu_card} || ! -c ${igpu_card} ]]; then
    echo "Could not resolve the AMD DRM card." >&2
    exit 1
fi

tmp_env="$(mktemp /run/10kwin-egpu.conf.XXXXXX)"
trap 'rm -f -- "${tmp_env}"' EXIT
printf '%s\n' \
    '# Generated by egpu-nvidia-boot.service; do not edit.' \
    "KWIN_DRM_DEVICES=${egpu_card}:${igpu_card}" \
    > "${tmp_env}"
install -D -m 0644 "${tmp_env}" "${KWIN_ENV}"

# Persistent Gen4 is deliberately implemented as a success latch rather than
# an unconditional default.  The request was consumed before retraining; only
# a completely successful early Gen4 initialization may arm the next boot.
# If a Gen4 boot hangs or fails, the next boot therefore returns to Gen3 and
# stays there until the administrator explicitly arms Gen4 again.
if [[ ${link_generation} == 4 &&
      -e ${GEN4_PERSISTENT} &&
      ${EGPU_LOCAL_RESERVE_BOOT_CONTEXT:-0} == 1 ]]; then
    install -D -m 0644 /dev/null "${GEN4_ONCE_REQUEST}"
    echo "Persistent Gen4 preference re-armed after a successful Gen4 boot."
fi

echo "KWin primary GPU configured: ${egpu_card}; fallback: ${igpu_card}."
