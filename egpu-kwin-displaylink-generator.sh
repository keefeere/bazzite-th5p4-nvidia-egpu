#!/usr/bin/env bash
# systemd user-environment generator (runs at every login; prints VAR=value lines).
# Keeps whatever GPU order earlier generators/environment.d chose for KWIN_DRM_DEVICES
# (the eGPU/profile order) and APPENDS the DisplayLink EVDI card, so KWin may drive a
# DisplayLink adapter (e.g. StarTech 17e9:4301) without disturbing the primary GPU.
# Prints nothing when KWIN_DRM_DEVICES is not set. The by-path directory is overridable
# only for the offline test.
set -eu
[[ -n ${KWIN_DRM_DEVICES:-} ]] || exit 0
cards=$KWIN_DRM_DEVICES
shopt -s nullglob
paths=("${EGPU_DRI_BY_PATH:-/dev/dri/by-path}"/platform-evdi.*-card)
# Keep the first EVDI device eligible even if the USB adapter is connected after login.
((${#paths[@]})) || paths=("${EGPU_DRI_BY_PATH:-/dev/dri/by-path}/platform-evdi.0-card")
for path in "${paths[@]}"; do
    resolved=$(readlink -f "$path" 2>/dev/null || true)
    case ":$cards:" in
        *":$path:"*) continue ;;
    esac
    if [[ -n $resolved ]]; then
        case ":$cards:" in *":$resolved:"*) continue ;; esac
    fi
    cards+=":$path"
done
printf 'KWIN_DRM_DEVICES=%s\n' "$cards"
