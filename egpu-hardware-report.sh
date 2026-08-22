#!/usr/bin/env bash
set -Eeuo pipefail

printf '%s\n' \
    "eGPU hardware discovery report" \
    "Generated: $(date --iso-8601=seconds)" \
    "Host: $(hostname)" \
    "Kernel: $(uname -r)" \
    "User: $(id -un):$(id -u)" \
    ""

echo "Display/audio PCI candidates:"
for sysfs in /sys/bus/pci/devices/0000:*; do
    [[ -r ${sysfs}/class && -r ${sysfs}/vendor && -r ${sysfs}/device ]] || continue
    class="$(<"${sysfs}/class")"
    case ${class} in
        0x030000|0x030200|0x040300)
            bdf=${sysfs##*/}
            driver="$(basename -- "$(readlink -f "${sysfs}/driver" 2>/dev/null)" 2>/dev/null || true)"
            printf '  %s class=%s id=%s:%s driver=%s path=%s\n' \
                "${bdf}" "${class}" "$(<"${sysfs}/vendor")" "$(<"${sysfs}/device")" \
                "${driver:-unbound}" "$(readlink -f "${sysfs}")"
            ;;
    esac
done

echo
echo "Thunderbolt/USB4 routers:"
router_count=0
for router in /sys/bus/thunderbolt/devices/*; do
    [[ -r ${router}/device_name ]] || continue
    router_count=$((router_count + 1))
    printf '  %s name=%q vendor=%s device=%s authorized=%s\n' \
        "${router##*/}" "$(<"${router}/device_name")" \
        "$(cat -- "${router}/vendor" 2>/dev/null || echo unknown)" \
        "$(cat -- "${router}/device" 2>/dev/null || echo unknown)" \
        "$(cat -- "${router}/authorized" 2>/dev/null || echo n/a)"
done
((router_count > 0)) || echo "  none"

echo
echo "USB devices at SuperSpeed or faster:"
usb_count=0
for usb in /sys/bus/usb/devices/*; do
    [[ -r ${usb}/idVendor && -r ${usb}/idProduct && -r ${usb}/speed ]] || continue
    speed="$(<"${usb}/speed")"
    [[ ${speed} =~ ^[0-9]+$ ]] || continue
    ((speed >= 5000)) || continue
    usb_count=$((usb_count + 1))
    printf '  %s id=%s:%s speed=%sM product=%q\n' \
        "${usb##*/}" "$(<"${usb}/idVendor")" "$(<"${usb}/idProduct")" "${speed}" \
        "$(cat -- "${usb}/product" 2>/dev/null || echo unknown)"
done
((usb_count > 0)) || echo "  none"

echo
echo "PCI tree:"
lspci -Dtvnn 2>/dev/null

echo
echo "Full PCI IDs and active drivers:"
lspci -Dnnk 2>/dev/null
