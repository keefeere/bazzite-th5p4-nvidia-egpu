#!/usr/bin/env bash
# systemd user-environment generator (runs at every login; prints VAR=value lines).
# While a WORK profile is remembered AND both an AMD and an NVIDIA GPU are present, KWin must
# render on the AMD GPU: its first KWIN_DRM_DEVICES entry is the primary one. The result overrides
# /etc/environment.d/10kwin-egpu.conf (NVIDIA first), which the eGPU boot service keeps managing.
# Any doubt (no remembered work profile, a GPU missing, odd sysfs) prints nothing, so KWin starts
# exactly as before. Paths are overridable only for the offline test.
set -u
DESIRED=${EGPU_DESIRED_PROFILE_FILE:-/var/lib/egpu-nvidia-service-roles/desired-profile}
DRM=${EGPU_SYSFS_DRM:-/sys/class/drm}

{ IFS= read -r profile < "$DESIRED"; } 2>/dev/null || exit 0
case $profile in
    work-nvidia|work-igpu) ;;
    *) exit 0 ;;
esac

amd=''
nvidia=''
for path in "$DRM"/card*; do
    name=${path##*/}
    [[ $name =~ ^card[0-9]+$ ]] || continue
    { IFS= read -r vendor < "$path/device/vendor"; } 2>/dev/null || continue
    case $vendor in
        0x1002) [[ -n $amd ]] || amd=$name ;;
        0x10de) [[ -n $nvidia ]] || nvidia=$name ;;
    esac
done

[[ -n $amd && -n $nvidia ]] || exit 0
printf 'KWIN_DRM_DEVICES=/dev/dri/%s:/dev/dri/%s\n' "$amd" "$nvidia"
