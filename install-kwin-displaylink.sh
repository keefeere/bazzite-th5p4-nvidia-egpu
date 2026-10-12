#!/usr/bin/env bash
# Install (or --remove) the KWin DisplayLink generator. Needs root; changes nothing else.
set -euo pipefail
(( EUID == 0 )) || { echo 'Run with sudo.' >&2; exit 1; }
here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
source_file=$here/egpu-kwin-displaylink-generator.sh
target=/etc/systemd/user-environment-generators/99-kwin-displaylink
case ${1:-install} in
    install)
        if [[ -e $target || -L $target ]]; then
            cmp -s "$source_file" "$target" && { echo "Already installed: $target"; exit 0; }
            # The first (hand-installed) version had this exact header; replacing it is safe.
            grep -qxF '# Preserve the GPU order chosen by earlier generators; append DisplayLink.' "$target" \
                || { echo "Different file already exists: $target; not overwriting." >&2; exit 1; }
        fi
        install -D -m 0755 "$source_file" "$target"
        restorecon "$target" 2>/dev/null || true
        echo "Installed: $target"
        echo 'Now: systemctl --user daemon-reload (as your user), then log out of Plasma and in again.'
        ;;
    --remove)
        [[ -e $target ]] || { echo 'Not installed.'; exit 0; }
        cmp -s "$source_file" "$target" \
            || grep -qxF '# Preserve the GPU order chosen by earlier generators; append DisplayLink.' "$target" \
            || { echo "File was changed; not removing: $target" >&2; exit 1; }
        rm -- "$target"
        echo "Removed: $target"
        ;;
    *) echo "Usage: $0 [--remove]" >&2; exit 2 ;;
esac
