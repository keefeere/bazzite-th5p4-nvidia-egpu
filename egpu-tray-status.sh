#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
SAFE_MARKER="/run/egpu-safe-to-unplug"
DETACH_BLOCK_MARKER="/run/egpu-nvidia-detach-block"
DETACH_SERVICE="egpu-nvidia-detach.service"
ATTACH_SERVICE="egpu-nvidia-hot-attach.service"
REBOOT_MARKER="/run/egpu-nvidia-reboot-required"
LOCAL_RESERVE_FAILURE_MARKER="/run/egpu-local-reserve-failed"

# shellcheck source=egpu-pci-lib.sh
source "${SCRIPT_DIR}/egpu-pci-lib.sh"

locale_name="${LC_ALL:-${LC_MESSAGES:-${LANG:-en}}}"
case ${1:-auto} in
    --language)
        [[ $# == 2 ]] || { echo "Usage: $0 [--language en|uk]" >&2; exit 2; }
        language=$2
        ;;
    auto) language=auto ;;
    *) echo "Usage: $0 [--language en|uk]" >&2; exit 2 ;;
esac
case ${language} in
    uk) ukrainian=1 ;;
    en) ukrainian=0 ;;
    auto) ukrainian=0; [[ ${locale_name,,} == uk* ]] && ukrainian=1 ;;
    *) echo "Unsupported language: ${language}" >&2; exit 2 ;;
esac

tr() {
    if (( ukrainian )); then
        printf '%s' "$2"
    else
        printf '%s' "$1"
    fi
}

emit() {
    printf '%s\t%s\t%s\n' "$1" "$2" "$3"
    exit 0
}

if [[ -s ${SAFE_MARKER} ]]; then
    if ! th5p4_router_present; then
        emit absent \
            "$(tr "eGPU is disconnected" "eGPU не підключена")" \
            "$(tr "The released ${ENCLOSURE_DISPLAY_NAME} is no longer connected." "Відпущений ${ENCLOSURE_DISPLAY_NAME} більше не підключений.")"
    fi
    emit safe \
        "$(tr "Safe to unplug" "Можна від’єднувати")" \
        "$(tr "The GPU is released. Unplug the cable, or click Connect eGPU while it remains attached." "GPU відпущено. Від’єднай кабель або натисни «Підключити eGPU», поки він лишається підключеним.")"
fi

if systemctl is-active --quiet "${ATTACH_SERVICE}"; then
    emit attaching \
        "$(tr "Connecting eGPU…" "Підключення eGPU…")" \
        "$(tr "Initializing NVIDIA at Gen3 and preparing a new graphical session." "Ініціалізуємо NVIDIA у Gen3 та готуємо новий графічний сеанс.")"
fi

if systemctl is-active --quiet "${DETACH_SERVICE}"; then
    emit detaching \
        "$(tr "Detaching…" "Від’єднання…")" \
        "$(tr "Ending the graphical session and releasing NVIDIA." "Завершуємо графічний сеанс і звільняємо NVIDIA.")"
fi

if systemctl is-failed --quiet "${DETACH_SERVICE}"; then
    emit error \
        "$(tr "Detach failed" "Помилка від’єднання")" \
        "$(tr "Do not unplug the cable. Check ${DETACH_SERVICE}." "Кабель не від’єднувати. Перевір ${DETACH_SERVICE}.")"
fi

if [[ -s ${LOCAL_RESERVE_FAILURE_MARKER} ]]; then
    emit error \
        "$(tr "PCI reserve failed" "Помилка резервування PCI")" \
        "$(tr "NVIDIA stayed blocked. Check egpu-nvidia-boot.service, correct the kernel compatibility state, then reboot." "NVIDIA лишилася заблокованою. Перевір egpu-nvidia-boot.service, виправ стан сумісності ядра та перезавантаж систему.")"
fi

if [[ -e ${REBOOT_MARKER} ]] ||
   { [[ -e /etc/egpu-nvidia/enable-local-reserve &&
        ! -s /run/egpu-local-reserve-applied ]] && resolve_egpu_gpu; }; then
    emit reboot \
        "$(tr "Reboot required" "Потрібне перезавантаження")" \
        "$(tr "The enclosure was physically reconnected. Reboot with it attached to restore validated PCI resources." "Корпус було фізично перепідключено. Перезавантаж систему з підключеним корпусом для відновлення перевірених PCI-ресурсів.")"
fi

if ! resolve_egpu_gpu; then
    if th5p4_usb_diagnostic_present && ! th5p4_router_present; then
        emit stuck \
            "$(tr "${ENCLOSURE_DISPLAY_NAME} is stuck" "${ENCLOSURE_DISPLAY_NAME} завис")" \
            "$(tr "USB diagnostics are present, but the USB4/PCIe router did not start. Power-cycle the enclosure/eGPU." "USB diagnostic присутній, але USB4/PCIe router не піднявся. Потрібен power-cycle корпуса/eGPU.")"
    fi
    emit absent \
        "$(tr "eGPU is disconnected" "eGPU не підключена")" \
        "$(tr "${EGPU_DISPLAY_NAME} was not found on PCIe." "${EGPU_DISPLAY_NAME} не знайдена на шині PCIe.")"
fi

if grep -q '^nvidia_drm ' /proc/modules &&
   compgen -G "/sys/bus/pci/devices/${GPU}/drm/card*" >/dev/null; then
    egpu_card="$(find "/sys/bus/pci/devices/${GPU}/drm" -mindepth 1 -maxdepth 1 -name 'card[0-9]*' -printf '/dev/dri/%f\n' | head -n 1)"
    live_kwin_order="$(systemctl --user show-environment 2>/dev/null | sed -n 's/^KWIN_DRM_DEVICES=//p')"
    if [[ ${live_kwin_order%%:*} != "${egpu_card}" ]]; then
        emit present \
            "$(tr "${EGPU_DISPLAY_NAME} is loaded" "${EGPU_DISPLAY_NAME} — завантажена")" \
            "$(tr "The current session still uses ${IGPU_DISPLAY_NAME}. Click Connect eGPU to make NVIDIA primary." "Поточний сеанс ще використовує ${IGPU_DISPLAY_NAME}. Натисни «Підключити eGPU», щоб зробити NVIDIA основною.")"
    fi
    speed="$(cat -- "/sys/bus/pci/devices/${GPU}/current_link_speed" 2>/dev/null || true)"
    width="$(cat -- "/sys/bus/pci/devices/${GPU}/current_link_width" 2>/dev/null || true)"
    case "${speed}" in
        "16.0 GT/s PCIe") link="PCIe Gen4 x${width:-?}" ;;
        "8.0 GT/s PCIe") link="PCIe Gen3 x${width:-?}" ;;
        *) link="${speed:-PCIe} x${width:-?}" ;;
    esac
    emit ready \
        "$(tr "${EGPU_DISPLAY_NAME} is active" "${EGPU_DISPLAY_NAME} — активна")" \
        "$(tr "${link}. Click to safely detach the eGPU." "${link}. Натисни для безпечного від’єднання eGPU.")"
fi

if grep -q '^nvidia ' /proc/modules; then
    emit initializing \
        "$(tr "NVIDIA is initializing" "NVIDIA ініціалізується")" \
        "$(tr "The PCIe device is present; waiting for DRM/KWin." "PCIe-пристрій присутній, очікуємо DRM/KWin.")"
fi

emit present \
    "$(tr "${EGPU_DISPLAY_NAME} detected" "${EGPU_DISPLAY_NAME} знайдена")" \
    "$(tr "The NVIDIA driver is not loaded yet." "Драйвер NVIDIA ще не завантажений.")"
