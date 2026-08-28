#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
APPLIED_MARKER="/run/egpu-local-reserve-applied"
DYNAMIC_REBAR_MARKER_TEXT="TH5P4 late-hotplug dynamic ReBAR repair is active."
NO_DOCK_COLD_REBAR_MARKER_TEXT="TH5P4 no-dock cold-boot dynamic ReBAR repair is active."
dynamic_rebar_mode=${EGPU_ALLOW_DYNAMIC_REBAR:-0}
if [[ -s ${APPLIED_MARKER} ]] &&
   { grep -Fqx -- "${DYNAMIC_REBAR_MARKER_TEXT}" "${APPLIED_MARKER}" ||
     grep -Fqx -- "${NO_DOCK_COLD_REBAR_MARKER_TEXT}" "${APPLIED_MARKER}"; }; then
    dynamic_rebar_mode=1
fi

# shellcheck source=egpu-pci-lib.sh
source "${SCRIPT_DIR}/egpu-pci-lib.sh"
# shellcheck source=egpu-kernel-compat.sh
source "${SCRIPT_DIR}/egpu-kernel-compat.sh"

kernel_compat_mode=$(egpu_kernel_compat_mode "$(uname -r)") || {
    echo "LOCAL TH5P4 RESERVE VERIFY FAILED: unsupported kernel release $(uname -r)" >&2
    exit 1
}
if [[ ${kernel_compat_mode} == hotplug-size ]]; then
    required_p1_mmio=${EGPU_PCI_HOTPLUG_MMIO_BYTES}
    required_p1_pref=${EGPU_PCI_HOTPLUG_PREF_BYTES}
else
    required_p1_mmio=${TARGET_MMIO_BYTES}
    required_p1_pref=${TARGET_PREF_BYTES}
fi
required_p1_io=${MIN_IO_BYTES}
if ((dynamic_rebar_mode)); then
    # The kernel-managed late ReBAR transition distributes the 16 KiB parent
    # I/O aperture evenly across the three empty ports. PCIe hot-add on the HP
    # port is quarantined in this mode, so only the bridge's own 4 KiB window
    # is required until reboot restores the full early-boot reservation.
    required_p1_io=4096
fi

die() {
    echo "LOCAL TH5P4 RESERVE VERIFY FAILED: $*" >&2
    exit 1
}

read_window() {
    local bdf=$1
    local line=$2
    local __start=$3
    local __end=$4
    local __flags=$5
    local raw_start raw_end raw_flags

    read -r raw_start raw_end raw_flags < <(sed -n "${line}p" "/sys/bus/pci/devices/${bdf}/resource")
    printf -v "${__start}" '%d' "${raw_start}"
    printf -v "${__end}" '%d' "${raw_end}"
    printf -v "${__flags}" '%d' "${raw_flags}"
}

expect_bus_range() {
    local bdf=$1
    local expected_secondary=$2
    local expected_subordinate=$3
    local secondary subordinate

    secondary=$(<"/sys/bus/pci/devices/${bdf}/secondary_bus_number")
    subordinate=$(<"/sys/bus/pci/devices/${bdf}/subordinate_bus_number")
    ((secondary == expected_secondary && subordinate == expected_subordinate)) ||
        die "${bdf} has bus $(printf '%02x-%02x' "${secondary}" "${subordinate}"), expected $(printf '%02x-%02x' "${expected_secondary}" "${expected_subordinate}")"
}

expect_window() {
    local bdf=$1
    local line=$2
    local expected_start=$3
    local expected_end=$4
    local label=$5
    local start end flags

    read_window "${bdf}" "${line}" start end flags
    ((flags != 0 && start == expected_start && end == expected_end)) ||
        die "${bdf} ${label} is $(printf '0x%x-0x%x' "${start}" "${end}"), expected $(printf '0x%x-0x%x' "${expected_start}" "${expected_end}")"
}

expect_disabled_window() {
    local bdf=$1
    local line=$2
    local label=$3
    local start end flags

    read_window "${bdf}" "${line}" start end flags
    ((flags == 0)) || die "${bdf} unexpectedly owns a ${label} window"
}

window_size() {
    local start=$1
    local end=$2
    echo $((end - start + 1))
}

expect_inside() {
    local parent_start=$1
    local parent_end=$2
    local child_start=$3
    local child_end=$4
    local label=$5

    ((child_start >= parent_start && child_end <= parent_end)) ||
        die "${label} is outside its parent bridge window"
}

expect_before() {
    local left_end=$1
    local right_start=$2
    local label=$3

    ((left_end < right_start)) || die "${label} windows overlap or are out of order"
}

expect_disjoint() {
    local left_start=$1
    local left_end=$2
    local right_start=$3
    local right_end=$4
    local label=$5

    ((left_end < right_start || right_end < left_start)) ||
        die "${label} windows overlap"
}

expect_inside_either() {
    local first_start=$1
    local first_end=$2
    local second_start=$3
    local second_end=$4
    local child_start=$5
    local child_end=$6
    local label=$7

    ((child_start >= first_start && child_end <= first_end)) ||
        ((child_start >= second_start && child_end <= second_end)) ||
        die "${label} is outside both parent MMIO apertures"
}

resolve_egpu_topology
validate_expected_topology || die "live BDF layout differs from the configured profile"

bridge_bus_prefix="${BRIDGE%:*}"
PORT1="${bridge_bus_prefix}:01.0"
PORT2="${bridge_bus_prefix}:02.0"
PORT3="${bridge_bus_prefix}:03.0"

for port in "${PORT1}" "${PORT2}" "${PORT3}"; do
    [[ $(pci_id_at "${port}" 2>/dev/null || true) == "${TH5P4_VENDOR}:${TH5P4_DEVICE}" ]] ||
        die "expected TH5P4 bridge ${port} is absent"
done

root_subordinate=$(<"/sys/bus/pci/devices/${ROOT_PORT}/subordinate_bus_number")
((root_subordinate == ROOT_BUS_SUBORDINATE)) || die "USB4 root subordinate bus differs from the profile"

expect_bus_range "${UPSTREAM}" "${UPSTREAM_BUS_SECONDARY}" "${ROOT_BUS_SUBORDINATE}"
expect_bus_range "${BRIDGE}" "${GPU_BUS_SECONDARY}" "${GPU_BUS_SECONDARY}"
expect_bus_range "${PORT1}" "${PORT1_BUS_START}" "${PORT1_BUS_END}"
expect_bus_range "${PORT2}" "${PORT2_BUS_START}" "${PORT2_BUS_END}"
expect_bus_range "${PORT3}" "${PORT3_BUS_START}" "${PORT3_BUS_END}"

# sysfs resource lines 14/15/16: I/O, MMIO, MMIO_PREF.  Linux is allowed to
# normalize the values programmed before rescan: it grows the RTX bridge for
# its VF BARs and gives empty hot-plug bridges small optional windows. Verify
# containment, capacity and non-overlap instead of requiring the raw writes to
# survive byte-for-byte.
expect_window "${ROOT_PORT}" 14 "${ROOT_IO_START}" "${ROOT_IO_END}" "I/O"
expect_window "${ROOT_PORT}" 15 "${ROOT_MMIO_START}" "${ROOT_MMIO_END}" "MMIO"
if (( ! dynamic_rebar_mode )); then
    expect_window "${ROOT_PORT}" 16 "${ROOT_PREF_START}" "${ROOT_PREF_END}" "MMIO_PREF"
fi

for spec in \
    "root_io ${ROOT_PORT} 14" "root_mem ${ROOT_PORT} 15" "root_pref ${ROOT_PORT} 16" \
    "up_io ${UPSTREAM} 14" "up_mem ${UPSTREAM} 15" "up_pref ${UPSTREAM} 16" \
    "gpu_io ${BRIDGE} 14" "gpu_mem ${BRIDGE} 15" "gpu_pref ${BRIDGE} 16" \
    "p1_io ${PORT1} 14" "p1_mem ${PORT1} 15" "p1_pref ${PORT1} 16" \
    "p2_io ${PORT2} 14" "p2_mem ${PORT2} 15" "p2_pref ${PORT2} 16" \
    "p3_io ${PORT3} 14" "p3_mem ${PORT3} 15" "p3_pref ${PORT3} 16"; do
    read -r prefix bdf line <<< "${spec}"
    read_window "${bdf}" "${line}" "${prefix}_start" "${prefix}_end" "${prefix}_flags"
done

for flags in up_io_flags up_mem_flags up_pref_flags \
             gpu_io_flags gpu_mem_flags gpu_pref_flags \
             p1_io_flags p1_mem_flags p1_pref_flags; do
    ((${!flags} != 0)) || die "required window ${flags%_flags} is disabled"
done

expect_inside "${root_io_start}" "${root_io_end}" "${up_io_start}" "${up_io_end}" "TH5P4 I/O"
expect_inside "${root_mem_start}" "${root_mem_end}" "${up_mem_start}" "${up_mem_end}" "TH5P4 MMIO"
expect_inside "${root_pref_start}" "${root_pref_end}" "${up_pref_start}" "${up_pref_end}" "TH5P4 MMIO_PREF"

if ((dynamic_rebar_mode)); then
    ((root_pref_flags != 0 &&
      $(window_size "${root_pref_start}" "${root_pref_end}") >= EGPU_BAR1_SIZE + EGPU_BAR3_SIZE)) ||
        die "dynamic root MMIO_PREF cannot contain the complete RTX ReBAR aperture"
fi

expect_inside "${up_io_start}" "${up_io_end}" "${gpu_io_start}" "${gpu_io_end}" "RTX I/O"
expect_inside "${up_mem_start}" "${up_mem_end}" "${gpu_mem_start}" "${gpu_mem_end}" "RTX MMIO"
expect_inside "${up_pref_start}" "${up_pref_end}" "${gpu_pref_start}" "${gpu_pref_end}" "RTX MMIO_PREF"
expect_inside "${up_io_start}" "${up_io_end}" "${p1_io_start}" "${p1_io_end}" "HP-port I/O"
expect_inside "${up_mem_start}" "${up_mem_end}" "${p1_mem_start}" "${p1_mem_end}" "HP-port MMIO"
if ((dynamic_rebar_mode)); then
    # The 7.2 allocator may place an empty child's prefetchable aperture inside
    # the parent's ordinary MMIO window while keeping RTX ReBAR in MMIO_PREF.
    expect_inside_either \
        "${up_mem_start}" "${up_mem_end}" "${up_pref_start}" "${up_pref_end}" \
        "${p1_pref_start}" "${p1_pref_end}" "HP-port MMIO_PREF"
else
    expect_inside "${up_pref_start}" "${up_pref_end}" "${p1_pref_start}" "${p1_pref_end}" "HP-port MMIO_PREF"
fi

(( $(window_size "${p1_io_start}" "${p1_io_end}") >= required_p1_io )) ||
    die "dock-port I/O reserve is smaller than the ${kernel_compat_mode} runtime requirement"
(( $(window_size "${p1_mem_start}" "${p1_mem_end}") >= required_p1_mmio )) ||
    die "dock-port MMIO reserve is smaller than the ${kernel_compat_mode} requirement"
(( $(window_size "${p1_pref_start}" "${p1_pref_end}") >= required_p1_pref )) ||
    die "dock-port MMIO_PREF reserve is smaller than the ${kernel_compat_mode} requirement"

expect_before "${gpu_io_end}" "${p1_io_start}" "RTX/HP-port I/O"
expect_before "${gpu_mem_end}" "${p1_mem_start}" "RTX/HP-port MMIO"
if ((dynamic_rebar_mode)); then
    expect_disjoint "${gpu_pref_start}" "${gpu_pref_end}" \
        "${p1_pref_start}" "${p1_pref_end}" "RTX/HP-port MMIO_PREF"
else
    expect_before "${gpu_pref_end}" "${p1_pref_start}" "RTX/HP-port MMIO_PREF"
fi

# On legacy kernels the two unused ports remain disabled or receive Linux's
# 2 MiB defaults. Linux 7.2+ deliberately gives every empty hot-plug port the
# measured 32 MiB minimum because its allocator no longer retains port1's
# pre-programmed 128 MiB/1 GiB window.
if ((dynamic_rebar_mode)); then
    for prefix in p2_io p3_io; do
        flags_name="${prefix}_flags"
        start_name="${prefix}_start"
        end_name="${prefix}_end"
        ((${!flags_name} != 0)) || die "${prefix} dynamic I/O window is disabled"
        (( $(window_size "${!start_name}" "${!end_name}") >= 4096 )) ||
            die "${prefix} dynamic I/O window is smaller than 4 KiB"
        expect_inside "${up_io_start}" "${up_io_end}" \
            "${!start_name}" "${!end_name}" "${prefix} dynamic I/O"
    done
    expect_before "${p1_io_end}" "${p2_io_start}" "HP-port/port2 I/O"
    expect_before "${p2_io_end}" "${p3_io_start}" "port2/port3 I/O"
else
    expect_disabled_window "${PORT2}" 14 "I/O"
    expect_disabled_window "${PORT3}" 14 "I/O"
fi
for prefix in p2_mem p2_pref p3_mem p3_pref; do
    flags_name="${prefix}_flags"
    start_name="${prefix}_start"
    end_name="${prefix}_end"
    if ((${!flags_name} != 0)); then
        size=$(window_size "${!start_name}" "${!end_name}")
        if [[ ${kernel_compat_mode} == hotplug-size ]]; then
            ((size >= EGPU_PCI_HOTPLUG_MMIO_BYTES)) ||
                die "${prefix} is smaller than the Linux 7.2+ hot-plug minimum"
        else
            ((size <= 2 * 1024 * 1024)) || die "${prefix} optional window exceeds 2 MiB"
        fi
    elif [[ ${kernel_compat_mode} == hotplug-size ]]; then
        die "${prefix} hot-plug window is disabled on Linux 7.2+"
    fi
done

if ((p2_mem_flags != 0)); then
    expect_inside "${up_mem_start}" "${up_mem_end}" "${p2_mem_start}" "${p2_mem_end}" "port2 MMIO"
    expect_before "${p1_mem_end}" "${p2_mem_start}" "HP-port/port2 MMIO"
fi
if ((p3_mem_flags != 0)); then
    expect_inside "${up_mem_start}" "${up_mem_end}" "${p3_mem_start}" "${p3_mem_end}" "port3 MMIO"
    ((p2_mem_flags != 0)) && expect_before "${p2_mem_end}" "${p3_mem_start}" "port2/port3 MMIO"
fi
if ((p2_pref_flags != 0)); then
    if ((dynamic_rebar_mode)); then
        expect_inside_either \
            "${up_mem_start}" "${up_mem_end}" "${up_pref_start}" "${up_pref_end}" \
            "${p2_pref_start}" "${p2_pref_end}" "port2 MMIO_PREF"
    else
        expect_inside "${up_pref_start}" "${up_pref_end}" "${p2_pref_start}" "${p2_pref_end}" "port2 MMIO_PREF"
    fi
    expect_before "${p1_pref_end}" "${p2_pref_start}" "HP-port/port2 MMIO_PREF"
fi
if ((p3_pref_flags != 0)); then
    if ((dynamic_rebar_mode)); then
        expect_inside_either \
            "${up_mem_start}" "${up_mem_end}" "${up_pref_start}" "${up_pref_end}" \
            "${p3_pref_start}" "${p3_pref_end}" "port3 MMIO_PREF"
    else
        expect_inside "${up_pref_start}" "${up_pref_end}" "${p3_pref_start}" "${p3_pref_end}" "port3 MMIO_PREF"
    fi
    ((p2_pref_flags != 0)) && expect_before "${p2_pref_end}" "${p3_pref_start}" "port2/port3 MMIO_PREF"
fi

# No endpoint may already occupy the reserve before the HP Dock test.
for sysfs in /sys/bus/pci/devices/0000:*; do
    bdf=${sysfs##*/}
    bus_hex=${bdf#0000:}
    bus_hex=${bus_hex%%:*}
    bus=$((16#${bus_hex}))
    ((bus < PORT1_BUS_START || bus > ROOT_BUS_SUBORDINATE)) || die "reserved bus $(printf '%02x' "${bus}") is occupied by ${bdf}"
done

printf '%s\n' \
    "LOCAL TH5P4 RESERVE VERIFY PASSED." \
    "  Dock port ${PORT1}: buses $(printf '%02x-%02x' "${PORT1_BUS_START}" "${PORT1_BUS_END}"), ${kernel_compat_mode} I/O/MMIO/MMIO_PREF reserves are present." \
    "  ${IGPU_DISPLAY_NAME} remains ${IGPU}; ${EGPU_DISPLAY_NAME} remains ${GPU}."

if [[ -s ${APPLIED_MARKER} ]]; then
    echo "  Runtime marker: ${APPLIED_MARKER}"
fi
