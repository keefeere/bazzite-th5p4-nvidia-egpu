#!/usr/bin/env bash

# Cardwire discovers GPUs from a root daemon and may keep NVIDIA device nodes
# open even after the graphical session has ended. Blocking a GPU in Cardwire
# prevents new opens through its eBPF policy; it is not a driver detach and
# does not close the daemon's own descriptors. Pause only the known systemd
# unit around a controlled transition and restore its prior state afterwards.

CARDWIRE_SERVICE="cardwired.service"
cardwire_was_active=0

egpu_pause_cardwire() {
    local main_pid executable

    cardwire_was_active=0
    systemctl is-active --quiet "${CARDWIRE_SERVICE}" || return 0

    main_pid="$(systemctl show "${CARDWIRE_SERVICE}" -p MainPID --value 2>/dev/null || true)"
    if [[ ! ${main_pid} =~ ^[1-9][0-9]*$ || ! -r /proc/${main_pid}/comm ]]; then
        echo "${CARDWIRE_SERVICE} is active but its main process could not be validated; refusing the GPU transition." >&2
        return 1
    fi
    if [[ $(< "/proc/${main_pid}/comm") != cardwired ]]; then
        echo "${CARDWIRE_SERVICE} has unexpected main process PID ${main_pid}; refusing the GPU transition." >&2
        return 1
    fi
    executable="$(readlink -f "/proc/${main_pid}/exe" 2>/dev/null || true)"
    if [[ ${executable} != /usr/bin/cardwired ]]; then
        echo "${CARDWIRE_SERVICE} has unexpected executable ${executable:-unknown}; refusing the GPU transition." >&2
        return 1
    fi

    echo "Pausing Cardwire so its root daemon releases the NVIDIA device nodes..."
    cardwire_was_active=1
    systemctl stop "${CARDWIRE_SERVICE}"
    if systemctl is-active --quiet "${CARDWIRE_SERVICE}"; then
        echo "${CARDWIRE_SERVICE} did not stop; refusing the GPU transition." >&2
        return 1
    fi
}

egpu_resume_cardwire() {
    (( cardwire_was_active )) || return 0

    echo "Restoring Cardwire after the controlled eGPU transition..."
    if systemctl start "${CARDWIRE_SERVICE}"; then
        cardwire_was_active=0
        return 0
    fi

    echo "Warning: ${CARDWIRE_SERVICE} did not restart; the eGPU transition itself is unchanged." >&2
    return 1
}
