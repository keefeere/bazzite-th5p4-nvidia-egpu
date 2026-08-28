#!/usr/bin/env bash

# Load one explicit machine profile. Installed scripts use /etc; source-tree
# diagnostics fall back to the adjacent profile. EGPU_CONFIG_FILE can point to
# a candidate profile for read-only validation before installation.
EGPU_LIB_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
EGPU_CONFIG_FILE="${EGPU_CONFIG_FILE:-/etc/egpu-nvidia/hardware.conf}"
if [[ ! -r ${EGPU_CONFIG_FILE} ]]; then
    EGPU_CONFIG_FILE="${EGPU_LIB_DIR}/hardware.conf"
fi
if [[ ! -r ${EGPU_CONFIG_FILE} ]]; then
    echo "eGPU hardware profile is missing: ${EGPU_CONFIG_FILE}" >&2
    return 1 2>/dev/null || exit 1
fi
load_egpu_config() {
    local line key raw value line_number=0
    local -A seen=()

    while IFS= read -r line || [[ -n ${line} ]]; do
        line_number=$((line_number + 1))
        line=${line%$'\r'}
        [[ ${line} =~ ^[[:space:]]*$ || ${line} =~ ^[[:space:]]*# ]] && continue
        if [[ ! ${line} =~ ^([A-Z][A-Z0-9_]*)=(.*)$ ]]; then
            echo "Invalid eGPU hardware profile ${EGPU_CONFIG_FILE}:${line_number}: expected KEY=value" >&2
            return 1
        fi
        key=${BASH_REMATCH[1]}
        raw=${BASH_REMATCH[2]}
        if [[ -v seen[${key}] ]]; then
            echo "Invalid eGPU hardware profile ${EGPU_CONFIG_FILE}:${line_number}: duplicate ${key}" >&2
            return 1
        fi
        seen[${key}]=1

        if [[ ${raw} =~ ^\"([^\"\$\`\\]*)\"$ ]]; then
            value=${BASH_REMATCH[1]}
        elif [[ ${raw} =~ ^(0x[0-9a-f]+|[0-9]+)$ ]]; then
            value=${BASH_REMATCH[1]}
        elif [[ ${raw} =~ ^\$\(\(([0-9[:space:]*/+()-]+)\)\)$ ]]; then
            # Compatibility with version-1 profiles generated before values
            # were canonicalized to decimal. The strict character class keeps
            # this arithmetic-only; names, expansions and commands cannot pass.
            value=$(( BASH_REMATCH[1] ))
        else
            echo "Invalid eGPU hardware profile ${EGPU_CONFIG_FILE}:${line_number}: unsafe or unsupported value for ${key}" >&2
            return 1
        fi
        printf -v "${key}" '%s' "${value}"
    done < "${EGPU_CONFIG_FILE}"
}

load_egpu_config || return 1 2>/dev/null || exit 1

egpu_config_die() {
    echo "Invalid eGPU hardware profile ${EGPU_CONFIG_FILE}: $*" >&2
    return 1
}

validate_egpu_config() {
    local variable value function
    local -a required=(
        EGPU_CONFIG_VERSION PROFILE_NAME DESKTOP_USER DESKTOP_UID
        EGPU_DISPLAY_NAME IGPU_DISPLAY_NAME ENCLOSURE_DISPLAY_NAME
        NVIDIA_DRM_FBDEV
        EGPU_VENDOR EGPU_DEVICE EGPU_AUDIO_VENDOR EGPU_AUDIO_DEVICE
        IGPU_VENDOR IGPU_DEVICE TH5P4_VENDOR TH5P4_DEVICE
        TH5P4_TB_VENDOR TH5P4_TB_DEVICE TH5P4_TB_NAME
        TH5P4_USB_DIAG_VENDOR TH5P4_USB_DIAG_PRODUCT HP_DOCK_SUPPORT
        EXPECTED_GPU_BDF EXPECTED_AUDIO_BDF EXPECTED_GPU_BRIDGE_BDF
        EXPECTED_TH5P4_UPSTREAM_BDF EXPECTED_USB4_ROOT_PORT_BDF EXPECTED_IGPU_BDF
        ROOT_BUS_SECONDARY ROOT_BUS_SUBORDINATE UPSTREAM_BUS_SECONDARY
        GPU_BUS_SECONDARY FIRMWARE_UNUSED_FIRST_BUS
        PORT1_BUS_START PORT1_BUS_END PORT2_BUS_START PORT2_BUS_END
        PORT3_BUS_START PORT3_BUS_END ROOT_IO_START ROOT_IO_END
        ROOT_MMIO_START ROOT_MMIO_END ROOT_PREF_START ROOT_PREF_END
        GPU_IO_START GPU_IO_END GPU_MMIO_START GPU_MMIO_END
        GPU_PREF_START GPU_PREF_END MIN_BUSES_PER_PORT MIN_IO_BYTES
        MIN_MMIO_BYTES MIN_PREF_BYTES TARGET_MMIO_BYTES TARGET_PREF_BYTES
        EGPU_BAR0_SIZE EGPU_BAR1_SIZE EGPU_BAR3_SIZE EGPU_BAR5_SIZE
        EGPU_AUDIO_BAR0_SIZE
    )

    if [[ ${HP_DOCK_SUPPORT:-} == 1 ]]; then
        required+=(
            DOCK_DISPLAY_NAME HP_DOCK_TB_VENDOR HP_DOCK_TB_DEVICE HP_DOCK_TB_NAME
            HP_DOCK_BRIDGE_VENDOR HP_DOCK_BRIDGE_DEVICE HP_DOCK_NIC_VENDOR
            HP_DOCK_NIC_DEVICE HP_DOCK_NIC_SUBSYSTEM_VENDOR
            HP_DOCK_NIC_SUBSYSTEM_DEVICE HP_DOCK_DISPLAYLINK_VENDOR
            HP_DOCK_DISPLAYLINK_PRODUCT HP_DOCK_EXPECTED_DESCENDANTS
            HP_DOCK_EXPECTED_BRIDGES HP_DOCK_INTERNAL_BRIDGE_FUNCTIONS
            HP_NIC_BAR0_SIZE HP_NIC_BAR3_SIZE
        )
    fi

    [[ ${EGPU_CONFIG_VERSION:-} == 1 ]] || {
        egpu_config_die "EGPU_CONFIG_VERSION must be 1"
        return 1
    }
    for variable in "${required[@]}"; do
        [[ -v ${variable} && -n ${!variable} ]] || {
            egpu_config_die "${variable} is required"
            return 1
        }
    done
    [[ ${DESKTOP_USER} =~ ^[a-z_][a-z0-9_.-]*$ ]] || {
        egpu_config_die "DESKTOP_USER has an unsafe value"
        return 1
    }
    [[ ${DESKTOP_UID} =~ ^[0-9]+$ ]] || {
        egpu_config_die "DESKTOP_UID must be numeric"
        return 1
    }
    [[ ${HP_DOCK_SUPPORT} == 0 || ${HP_DOCK_SUPPORT} == 1 ]] || {
        egpu_config_die "HP_DOCK_SUPPORT must be 0 or 1"
        return 1
    }
    [[ ${NVIDIA_DRM_FBDEV} == 0 || ${NVIDIA_DRM_FBDEV} == 1 ]] || {
        egpu_config_die "NVIDIA_DRM_FBDEV must be 0 or 1"
        return 1
    }
    if [[ -v EGPU_ENDPOINT_WAIT_SECONDS ]]; then
        [[ ${EGPU_ENDPOINT_WAIT_SECONDS} =~ ^[1-9][0-9]*$ ]] &&
            ((EGPU_ENDPOINT_WAIT_SECONDS <= 30)) || {
                egpu_config_die "EGPU_ENDPOINT_WAIT_SECONDS must be between 1 and 30"
                return 1
            }
    fi

    for variable in EGPU_VENDOR EGPU_DEVICE EGPU_AUDIO_VENDOR EGPU_AUDIO_DEVICE \
                    IGPU_VENDOR IGPU_DEVICE TH5P4_VENDOR TH5P4_DEVICE \
                    TH5P4_TB_VENDOR TH5P4_TB_DEVICE; do
        value=${!variable}
        [[ ${value} =~ ^0x[0-9a-f]{1,4}$ ]] || {
            egpu_config_die "${variable} must look like 0x10de"
            return 1
        }
    done
    if [[ ${HP_DOCK_SUPPORT} == 1 ]]; then
        for variable in HP_DOCK_TB_VENDOR HP_DOCK_TB_DEVICE \
                        HP_DOCK_BRIDGE_VENDOR HP_DOCK_BRIDGE_DEVICE \
                        HP_DOCK_NIC_VENDOR HP_DOCK_NIC_DEVICE \
                        HP_DOCK_NIC_SUBSYSTEM_VENDOR HP_DOCK_NIC_SUBSYSTEM_DEVICE; do
            value=${!variable}
            [[ ${value} =~ ^0x[0-9a-f]{1,4}$ ]] || {
                egpu_config_die "${variable} must be a lowercase sysfs hex ID"
                return 1
            }
        done
        for variable in HP_DOCK_DISPLAYLINK_VENDOR HP_DOCK_DISPLAYLINK_PRODUCT; do
            value=${!variable}
            [[ ${value} =~ ^[0-9a-f]{4}$ ]] || {
                egpu_config_die "${variable} must be a four-digit USB ID"
                return 1
            }
        done
        for variable in HP_DOCK_EXPECTED_DESCENDANTS HP_DOCK_EXPECTED_BRIDGES; do
            value=${!variable}
            [[ ${value} =~ ^[1-9][0-9]*$ ]] || {
                egpu_config_die "${variable} must be a positive integer"
                return 1
            }
        done
        for function in ${HP_DOCK_INTERNAL_BRIDGE_FUNCTIONS}; do
            [[ ${function} =~ ^[0-9a-f]{2}\.[0-7]$ ]] || {
                egpu_config_die "invalid HP_DOCK_INTERNAL_BRIDGE_FUNCTIONS entry: ${function}"
                return 1
            }
        done
    fi
    for variable in TH5P4_USB_DIAG_VENDOR TH5P4_USB_DIAG_PRODUCT; do
        value=${!variable}
        [[ ${value} =~ ^[0-9a-f]{4}$ ]] || {
            egpu_config_die "${variable} must be a four-digit USB ID"
            return 1
        }
    done
    for variable in EXPECTED_GPU_BDF EXPECTED_AUDIO_BDF EXPECTED_GPU_BRIDGE_BDF \
                    EXPECTED_TH5P4_UPSTREAM_BDF EXPECTED_USB4_ROOT_PORT_BDF EXPECTED_IGPU_BDF; do
        value=${!variable}
        [[ ${value} =~ ^0000:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]$ ]] || {
            egpu_config_die "${variable} is not a full PCI BDF"
            return 1
        }
    done
    for variable in ROOT_BUS_SECONDARY ROOT_BUS_SUBORDINATE UPSTREAM_BUS_SECONDARY \
                    GPU_BUS_SECONDARY FIRMWARE_UNUSED_FIRST_BUS \
                    PORT1_BUS_START PORT1_BUS_END PORT2_BUS_START PORT2_BUS_END \
                    PORT3_BUS_START PORT3_BUS_END ROOT_IO_START ROOT_IO_END \
                    ROOT_MMIO_START ROOT_MMIO_END ROOT_PREF_START ROOT_PREF_END \
                    GPU_IO_START GPU_IO_END GPU_MMIO_START GPU_MMIO_END \
                    GPU_PREF_START GPU_PREF_END MIN_BUSES_PER_PORT MIN_IO_BYTES \
                    MIN_MMIO_BYTES MIN_PREF_BYTES TARGET_MMIO_BYTES TARGET_PREF_BYTES \
                    EGPU_BAR0_SIZE EGPU_BAR1_SIZE EGPU_BAR3_SIZE EGPU_BAR5_SIZE \
                    EGPU_AUDIO_BAR0_SIZE; do
        value=${!variable}
        [[ ${value} =~ ^(0x[0-9a-f]+|[0-9]+)$ ]] || {
            egpu_config_die "${variable} must be a non-negative integer or lowercase hexadecimal value"
            return 1
        }
    done
    if [[ ${HP_DOCK_SUPPORT} == 1 ]]; then
        for variable in HP_NIC_BAR0_SIZE HP_NIC_BAR3_SIZE; do
            value=${!variable}
            [[ ${value} =~ ^(0x[0-9a-f]+|[0-9]+)$ ]] || {
                egpu_config_die "${variable} must be a non-negative integer or lowercase hexadecimal value"
                return 1
            }
        done
    fi
}

validate_egpu_config || return 1 2>/dev/null || exit 1

pci_id_at() {
    local bdf=$1
    local sysfs="/sys/bus/pci/devices/${bdf}"

    [[ -r ${sysfs}/vendor && -r ${sysfs}/device ]] || return 1
    printf '%s:%s\n' "$(<"${sysfs}/vendor")" "$(<"${sysfs}/device")"
}

find_unique_pci_device() {
    local vendor=$1
    local device=$2
    local sysfs
    local -a matches=()

    for sysfs in /sys/bus/pci/devices/0000:*; do
        [[ -r ${sysfs}/vendor && -r ${sysfs}/device ]] || continue
        if [[ $(<"${sysfs}/vendor") == "${vendor}" &&
              $(<"${sysfs}/device") == "${device}" ]]; then
            matches+=("${sysfs##*/}")
        fi
    done

    if ((${#matches[@]} == 0)); then
        return 1
    fi
    if ((${#matches[@]} != 1)); then
        echo "Expected one PCI device ${vendor}:${device}, found: ${matches[*]}" >&2
        return 2
    fi

    printf '%s\n' "${matches[0]}"
}

find_hp_dock_router_dir() {
    local device_dir
    local name vendor device
    local -a matches=()

    [[ ${HP_DOCK_SUPPORT} == 1 ]] || return 1
    for device_dir in /sys/bus/thunderbolt/devices/*; do
        [[ -d ${device_dir} ]] || continue
        name="$(cat -- "${device_dir}/device_name" 2>/dev/null || true)"
        vendor="$(cat -- "${device_dir}/vendor" 2>/dev/null || true)"
        device="$(cat -- "${device_dir}/device" 2>/dev/null || true)"
        if [[ ${name} == "${HP_DOCK_TB_NAME}" &&
              ${vendor} == "${HP_DOCK_TB_VENDOR}" &&
              ${device} == "${HP_DOCK_TB_DEVICE}" ]]; then
            matches+=("${device_dir}")
        fi
    done

    ((${#matches[@]} == 1)) || return 1
    printf '%s\n' "${matches[0]}"
}

find_th5p4_router_dir() {
    local device_dir
    local name vendor device
    local -a matches=()

    for device_dir in /sys/bus/thunderbolt/devices/*; do
        [[ -d ${device_dir} ]] || continue
        name="$(cat -- "${device_dir}/device_name" 2>/dev/null || true)"
        vendor="$(cat -- "${device_dir}/vendor" 2>/dev/null || true)"
        device="$(cat -- "${device_dir}/device" 2>/dev/null || true)"
        if [[ ${name} == "${TH5P4_TB_NAME}" &&
              ${vendor} == "${TH5P4_TB_VENDOR}" &&
              ${device} == "${TH5P4_TB_DEVICE}" ]]; then
            matches+=("${device_dir}")
        fi
    done

    ((${#matches[@]} == 1)) || return 1
    printf '%s\n' "${matches[0]}"
}

th5p4_router_present() {
    find_th5p4_router_dir >/dev/null
}

th5p4_usb_diagnostic_present() {
    local device_dir
    local vendor product

    for device_dir in /sys/bus/usb/devices/*; do
        [[ -r ${device_dir}/idVendor && -r ${device_dir}/idProduct ]] || continue
        vendor="$(<"${device_dir}/idVendor")"
        product="$(<"${device_dir}/idProduct")"
        if [[ ${vendor} == "${TH5P4_USB_DIAG_VENDOR}" &&
              ${product} == "${TH5P4_USB_DIAG_PRODUCT}" ]]; then
            return 0
        fi
    done

    return 1
}

hp_dock_router_present() {
    find_hp_dock_router_dir >/dev/null
}

is_pci_descendant_of() {
    local child=$1
    local ancestor=$2
    local child_path ancestor_path

    [[ -d /sys/bus/pci/devices/${child} &&
       -d /sys/bus/pci/devices/${ancestor} ]] || return 1
    child_path="$(readlink -f "/sys/bus/pci/devices/${child}")"
    ancestor_path="$(readlink -f "/sys/bus/pci/devices/${ancestor}")"
    [[ ${child_path} == "${ancestor_path}/"* ]]
}

hp_pci_subtree_present() {
    local port1 sysfs bdf

    [[ -n ${BRIDGE:-} ]] || return 1
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

resolve_egpu_gpu() {
    GPU="$(find_unique_pci_device "${EGPU_VENDOR}" "${EGPU_DEVICE}")" || return
}

resolve_egpu_topology() {
    local gpu_path
    local bridge_path
    local upstream_path
    local expected_audio
    local actual

    resolve_egpu_gpu || {
        echo "${EGPU_DISPLAY_NAME} ${EGPU_VENDOR}:${EGPU_DEVICE} is absent." >&2
        return 1
    }

    expected_audio="${GPU%.*}.1"
    actual="$(pci_id_at "${expected_audio}" 2>/dev/null || true)"
    if [[ ${actual} != "${EGPU_AUDIO_VENDOR}:${EGPU_AUDIO_DEVICE}" ]]; then
        echo "Expected ${EGPU_DISPLAY_NAME} audio ${EGPU_AUDIO_VENDOR}:${EGPU_AUDIO_DEVICE} at ${expected_audio}, found ${actual:-nothing}." >&2
        return 1
    fi
    AUDIO="${expected_audio}"

    gpu_path="$(readlink -f "/sys/bus/pci/devices/${GPU}")"
    bridge_path="$(dirname -- "${gpu_path}")"
    upstream_path="$(dirname -- "${bridge_path}")"
    BRIDGE="${bridge_path##*/}"
    UPSTREAM="${upstream_path##*/}"
    ROOT_PORT="$(basename -- "$(dirname -- "${upstream_path}")")"

    for bdf in "${BRIDGE}" "${UPSTREAM}"; do
        actual="$(pci_id_at "${bdf}" 2>/dev/null || true)"
        if [[ ${actual} != "${TH5P4_VENDOR}:${TH5P4_DEVICE}" ]]; then
            echo "Expected ${ENCLOSURE_DISPLAY_NAME} bridge ${TH5P4_VENDOR}:${TH5P4_DEVICE} at ${bdf}, found ${actual:-nothing}." >&2
            return 1
        fi
    done

    if [[ ! -d /sys/bus/pci/devices/${ROOT_PORT} ]]; then
        echo "Could not resolve the PCIe root port above ${UPSTREAM}." >&2
        return 1
    fi

    IGPU="$(find_unique_pci_device "${IGPU_VENDOR}" "${IGPU_DEVICE}")" || {
        echo "${IGPU_DISPLAY_NAME} ${IGPU_VENDOR}:${IGPU_DEVICE} is absent or ambiguous." >&2
        return 1
    }
}

validate_expected_topology() {
    [[ ${GPU} == "${EXPECTED_GPU_BDF}" &&
       ${AUDIO} == "${EXPECTED_AUDIO_BDF}" &&
       ${BRIDGE} == "${EXPECTED_GPU_BRIDGE_BDF}" &&
       ${UPSTREAM} == "${EXPECTED_TH5P4_UPSTREAM_BDF}" &&
       ${ROOT_PORT} == "${EXPECTED_USB4_ROOT_PORT_BDF}" &&
       ${IGPU} == "${EXPECTED_IGPU_BDF}" ]] || {
        echo "Resolved topology does not match profile ${PROFILE_NAME}:" >&2
        printf '  resolved: root=%s upstream=%s bridge=%s gpu=%s audio=%s igpu=%s\n' \
            "${ROOT_PORT}" "${UPSTREAM}" "${BRIDGE}" "${GPU}" "${AUDIO}" "${IGPU}" >&2
        printf '  expected: root=%s upstream=%s bridge=%s gpu=%s audio=%s igpu=%s\n' \
            "${EXPECTED_USB4_ROOT_PORT_BDF}" "${EXPECTED_TH5P4_UPSTREAM_BDF}" \
            "${EXPECTED_GPU_BRIDGE_BDF}" "${EXPECTED_GPU_BDF}" \
            "${EXPECTED_AUDIO_BDF}" "${EXPECTED_IGPU_BDF}" >&2
        return 1
    }
}
