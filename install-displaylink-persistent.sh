#!/usr/bin/env bash
# Keep the DisplayLink 6.3.0 driver across Bazzite updates (an image update dropped it).
# Copies DisplayLinkManager (+ firmware .spkg files) from an older deployment that still has it into
# /var/opt/displaylink (survives updates), installs a systemd unit and udev rule under /etc, and
# loads evdi. Nothing proprietary is stored in this repository. Needs root. --remove undoes it.
set -euo pipefail
(( EUID == 0 )) || { echo 'Run with sudo.' >&2; exit 1; }
DEST=/var/opt/displaylink
UNIT=/etc/systemd/system/displaylink.service
RULE=/etc/udev/rules.d/99-displaylink.rules
MANAGER_SHA=9c6a88b61f1fd6a2c7f00c2a8a6ba096a82b5df7392ccaeb5e454a1f506a21b4

unit_text() {
    cat <<UNITEOF
[Unit]
Description=DisplayLink Manager Service (persistent copy)
After=display-manager.service
Conflicts=getty@tty7.service

[Service]
ExecStartPre=/sbin/modprobe evdi
ExecStart=$DEST/DisplayLinkManager
Restart=always
WorkingDirectory=$DEST
RestartSec=5

[Install]
WantedBy=multi-user.target
UNITEOF
}
rule_text() {
    cat <<'RULEEOF'
ACTION=="add", SUBSYSTEM=="usb", ENV{ID_VENDOR}=="DisplayLink", MODE="0660", TAG+="systemd", ENV{SYSTEMD_WANTS}+="displaylink.service"
ACTION=="remove", SUBSYSTEM=="usb", ENV{ID_VENDOR}=="DisplayLink", RUN+="/bin/systemctl stop displaylink.service"
RULEEOF
}

case ${1:-install} in
    install)
        for existing in "$UNIT" "$RULE"; do
            [[ ! -e $existing ]] || { echo "Already exists: $existing; inspect it first." >&2; exit 1; }
        done
        [[ ! -e $DEST ]] || { echo "Already exists: $DEST; inspect it first." >&2; exit 1; }
        if [[ -x /usr/libexec/displaylink/DisplayLinkManager ]]; then
            source_dir=/usr/libexec/displaylink
        else
            source_dir=
            for deployment in /ostree/deploy/*/deploy/*/; do
                candidate=${deployment}usr/libexec/displaylink
                if [[ -x $candidate/DisplayLinkManager ]] \
                    && [[ $(sha256sum "$candidate/DisplayLinkManager" | cut -d' ' -f1) == "$MANAGER_SHA" ]]; then
                    source_dir=$candidate
                    break
                fi
            done
        fi
        [[ -n $source_dir ]] || { echo 'No deployment with the expected DisplayLinkManager found (an update may have removed the old one).' >&2; exit 1; }
        [[ $(sha256sum "$source_dir/DisplayLinkManager" | cut -d' ' -f1) == "$MANAGER_SHA" ]] \
            || { echo 'DisplayLinkManager differs from the tested 6.3.0 build; refusing.' >&2; exit 1; }
        install -d -m 0755 "$DEST"
        install -m 0755 "$source_dir/DisplayLinkManager" "$DEST/DisplayLinkManager"
        for firmware in "$source_dir"/*.spkg; do
            install -m 0644 "$firmware" "$DEST/"
        done
        chcon --reference=/usr/bin/true "$DEST/DisplayLinkManager" 2>/dev/null || true  # bin_t
        restorecon -R "$DEST" 2>/dev/null || true
        unit_text > "$UNIT"; chmod 0644 "$UNIT"
        rule_text > "$RULE"; chmod 0644 "$RULE"
        systemctl daemon-reload
        udevadm control --reload
        modprobe evdi
        systemctl enable --now displaylink.service
        udevadm trigger --subsystem-match=usb --action=add --attr-match=idVendor=17e9
        echo "Installed: $DEST, $UNIT, $RULE"
        systemctl --no-pager --lines=3 status displaylink.service || true
        ;;
    --remove)
        systemctl disable --now displaylink.service 2>/dev/null || true
        [[ ! -e $UNIT ]] || { [[ $(cat "$UNIT") == "$(unit_text)" ]] || { echo "Unit was changed; not removing: $UNIT" >&2; exit 1; }; rm -- "$UNIT"; }
        [[ ! -e $RULE ]] || { [[ $(cat "$RULE") == "$(rule_text)" ]] || { echo "Rule was changed; not removing: $RULE" >&2; exit 1; }; rm -- "$RULE"; }
        rm -rf -- "$DEST"
        systemctl daemon-reload
        udevadm control --reload
        echo 'Removed.'
        ;;
    *) echo "Usage: $0 [--remove]" >&2; exit 2 ;;
esac
