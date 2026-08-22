#!/usr/bin/env bash
set -Eeuo pipefail

KARG='pci=assign-busses,realloc=on,hpbussize=6,hpmmiosize=8M,hpmmioprefsize=8M'
GPU_ID='0x10de:0x2c05'
TH5P4_ID='0x8086:0x5786'
HP_DOCK_NAME='HP Thunderbolt Dock G4'

# hpbussize is global.  With this dock the observed cold topology contains
# two ordinary and three hot-plug downstream bridges.  At hpbussize=6 that
# needs about 22 bus numbers below the TH5P4 port.  assign-busses lets the PCI
# scanner distribute the root port's available range among the empty TH5P4
# ports instead of preserving the firmware's one-bus windows.  The three
# nested hot-plug branches likewise need roughly 24 MiB of each MMIO type;
# require 32 MiB to keep alignment/headroom out of the live NVIDIA window.
MIN_BUSES=22
MIN_MMIO=$((32 * 1024 * 1024))
MIN_PREF=$((32 * 1024 * 1024))

die() {
    echo "NOT SAFE TO HOT-PLUG THE HP DOCK: $*" >&2
    exit 1
}

size_of_window() {
    local device=$1
    local line=$2
    local start end flags

    read -r start end flags < <(sed -n "${line}p" "/sys/bus/pci/devices/${device}/resource")
    if [[ -z ${start:-} || ${flags:-0x0} == 0x0000000000000000 || ${end} -lt ${start} ]]; then
        echo 0
    else
        echo $((end - start + 1))
    fi
}

human_mib() {
    awk -v bytes="$1" 'BEGIN { printf "%.0f MiB", bytes / 1048576 }'
}

if [[ " $(</proc/cmdline) " != *" ${KARG} "* ]]; then
    die "the new kernel argument is not active; install it and reboot first"
fi

for name_file in /sys/bus/thunderbolt/devices/*/device_name; do
    [[ -r ${name_file} ]] || continue
    if [[ $(<"${name_file}") == "${HP_DOCK_NAME}" ]]; then
        die "HP Dock G4 is already present; reboot once with it disconnected"
    fi
done

gpu=''
for sysfs in /sys/bus/pci/devices/0000:*; do
    [[ -r ${sysfs}/vendor && -r ${sysfs}/device ]] || continue
    if [[ $(<"${sysfs}/vendor"):$(<"${sysfs}/device") == "${GPU_ID}" ]]; then
        gpu=${sysfs##*/}
        break
    fi
done
[[ -n ${gpu} ]] || die "RTX 5070 Ti PCI endpoint was not found"

gpu_path="$(readlink -f "/sys/bus/pci/devices/${gpu}")"
gpu_port_path="$(dirname -- "${gpu_path}")"
hub_path="$(dirname -- "${gpu_port_path}")"
gpu_port=${gpu_port_path##*/}

[[ -r ${gpu_port_path}/vendor && -r ${gpu_port_path}/device ]] ||
    die "could not resolve the RTX downstream bridge"
[[ $(<"${gpu_port_path}/vendor"):$(<"${gpu_port_path}/device") == "${TH5P4_ID}" ]] ||
    die "the RTX parent is not the expected TH5P4 bridge"

printf 'RTX endpoint: %s (downstream bridge %s)\n' "${gpu}" "${gpu_port}"
printf '%-14s %-10s %-10s %-12s %-12s %s\n' \
    'TH5P4 port' 'secondary' 'subordinate' 'bus reserve' 'MMIO' 'MMIO_PREF'

safe_ports=()
seen_ports=0
for port_path in "${hub_path}"/0000:*; do
    [[ -d ${port_path} && -r ${port_path}/vendor && -r ${port_path}/device ]] || continue
    [[ $(<"${port_path}/vendor"):$(<"${port_path}/device") == "${TH5P4_ID}" ]] || continue

    port=${port_path##*/}
    [[ ${port} != "${gpu_port}" ]] || continue
    [[ -r ${port_path}/secondary_bus_number && -r ${port_path}/subordinate_bus_number ]] || continue
    ((seen_ports += 1))

    secondary=$(<"${port_path}/secondary_bus_number")
    subordinate=$(<"${port_path}/subordinate_bus_number")
    buses=$((subordinate - secondary + 1))

    # PCI bridge windows are resource indices 14 (non-prefetchable MMIO) and
    # 15 (prefetchable MMIO), i.e. lines 15 and 16 in the sysfs resource file.
    mmio=$(size_of_window "${port}" 15)
    pref=$(size_of_window "${port}" 16)

    printf '%-14s 0x%02x       0x%02x       %-12s %-12s %s\n' \
        "${port}" "${secondary}" "${subordinate}" "${buses}" \
        "$(human_mib "${mmio}")" "$(human_mib "${pref}")"

    # An actually empty downstream bridge has no PCI children below it.
    has_child=0
    for child in "${port_path}"/0000:*; do
        [[ -e ${child} ]] && has_child=1
    done

    if (( ! has_child && buses >= MIN_BUSES && mmio >= MIN_MMIO && pref >= MIN_PREF )); then
        safe_ports+=("${port}")
    fi
done

((seen_ports > 0)) || die "no spare TH5P4 downstream bridges were found"
if ((${#safe_ports[@]} == 0)); then
    die "no empty TH5P4 port has at least ${MIN_BUSES} buses + 32 MiB MMIO + 32 MiB MMIO_PREF reserved"
fi

echo
echo "Reservation check passed for empty bridge(s): ${safe_ports[*]}"
echo "Keep unsaved work closed for the first isolated hot-plug test."
echo "After plugging in the HP dock, immediately check journal and nvidia-smi."
