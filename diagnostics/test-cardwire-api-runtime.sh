#!/usr/bin/env bash
# Temporary A/B test; an explicit keep option lasts only until reboot.
# Never installs/replaces the RPM. Check/apply retain their original semantics.
set -euo pipefail

EXPECTED_SHA=07baf8f89af7575fedf630c1c741c44b114477ed4a75b9712d7e7e233356d6ff
ROOT=/run/egpu-cardwire-api-test
DROPIN=/run/systemd/system/cardwired.service.d/90-egpu-process-access-test.conf
TIMER=egpu-cardwire-api-test-rollback
SERVICE=org.opengamingcollective.cardwire
OBJECT=/org/opengamingcollective/cardwire
HERE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
BINARY=${1:?Usage: test-cardwire-api-runtime.sh /absolute/path/cardwired [--check|--apply|--keep-until-reboot]}
MODE=${2:---check}
[[ $# -le 2 && ( $MODE == --check || $MODE == --apply || $MODE == --keep-until-reboot ) ]] || exit 2
if [[ $MODE == --keep-until-reboot ]]; then
    ROOT=/run/egpu-cardwire-profile-test
    DROPIN=/run/systemd/system/cardwired.service.d/90-egpu-profile-api-test.conf
    TIMER=egpu-cardwire-profile-test-rollback
fi
keep_success=0
[[ $BINARY == /* && -f $BINARY && ! -L $BINARY ]] || { echo 'Expected an absolute regular binary path.' >&2; exit 1; }
[[ $(sha256sum -- "$BINARY" | cut -d ' ' -f1) == "$EXPECTED_SHA" ]] || { echo 'Candidate checksum mismatch.' >&2; exit 1; }
[[ $(rpm -q cardwire) == cardwire-0.12.3-2.fc44.x86_64 ]] || { echo 'Installed Cardwire package changed; re-audit first.' >&2; exit 1; }
[[ ! -e $ROOT && ! -L $ROOT && ! -e $DROPIN && ! -L $DROPIN ]] || { echo 'Previous test state/override exists; inspect it instead of overwriting.' >&2; exit 1; }
systemctl is-active --quiet cardwired.service
[[ $(busctl get-property "$SERVICE" "$OBJECT" "$SERVICE.Mode" Mode) == 'u 1' ]] || { echo 'Hybrid mode required; refusing to switch it.' >&2; exit 1; }
[[ -z $(systemctl show cardwired.service -p BindReadOnlyPaths --value) ]] || { echo 'Existing bind overrides need review.' >&2; exit 1; }
[[ -f $HERE/egpu-cardwire-api-probe.py ]] || exit 1

echo 'PRECHECK PASSED: stable 0.12.3 candidate, unchanged Hybrid mode, no existing test override.'
if [[ $MODE == --check ]]; then
    echo 'Read-only check complete. --apply requires root and restarts only cardwired, then restores it.'
    exit 0
fi
[[ $EUID == 0 ]] || { echo 'Activation requires sudo/pkexec.' >&2; exit 1; }

# Serialize with boot/attach/detach. This test never performs any GPU transition.
exec 9>/run/egpu-nvidia-transition.lock
flock -n -x 9 || { echo 'An eGPU transition is in progress; aborting.' >&2; exit 1; }
pid=$(systemctl show cardwired.service -p MainPID --value)
[[ $pid =~ ^[1-9][0-9]*$ && $(readlink -f "/proc/$pid/exe") == /usr/bin/cardwired ]] || exit 1
original_sha=$(sha256sum /usr/bin/cardwired | cut -d ' ' -f1)
[[ $(sha256sum "/proc/$pid/exe" | cut -d ' ' -f1) == "$original_sha" ]] || { echo 'Running daemon is not the packaged binary.' >&2; exit 1; }

mkdir -m 0700 -- "$ROOT"
install -m 0755 -- "$BINARY" "$ROOT/cardwired"
[[ $(sha256sum "$ROOT/cardwired" | cut -d ' ' -f1) == "$EXPECTED_SHA" ]] || exit 1
# Keep the SELinux executable label; do not weaken SELinux or unit hardening.
chcon --reference=/usr/bin/cardwired "$ROOT/cardwired"
install -m 0644 -- "$HERE/egpu-cardwire-api-probe.py" "$ROOT/probe.py"
printf '%s\n' "$original_sha" > "$ROOT/original.sha256"
printf '[Service]\nBindReadOnlyPaths=%s/cardwired:/usr/bin/cardwired\n' "$ROOT" > "$ROOT/override.conf"

cat > "$ROOT/rollback.sh" <<'ROLLBACK'
#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
case "$ROOT" in
    /run/egpu-cardwire-api-test)
        DROPIN=/run/systemd/system/cardwired.service.d/90-egpu-process-access-test.conf ;;
    /run/egpu-cardwire-profile-test)
        DROPIN=/run/systemd/system/cardwired.service.d/90-egpu-profile-api-test.conf ;;
    *) exit 1 ;;
esac
[[ $# == 0 || ( $# == 1 && $1 == --force ) ]] || exit 2
[[ $EUID == 0 && -d $ROOT && ! -L $ROOT && $(stat -c %u "$ROOT") == 0 ]] || exit 1
exec 8>"$ROOT/rollback.lock"
flock -x 8
[[ ! -e $ROOT/restored ]] || exit 0
[[ ! -e $ROOT/kept || ${1:-} == --force ]] || exit 0
# Work profiles may depend on the patched API. Restore the GPU policy before
# explicitly returning to the packaged daemon; do not silently strand Work.
if [[ -e $ROOT/kept ]]; then
    [[ $(busctl get-property org.opengamingcollective.cardwire /org/opengamingcollective/cardwire org.opengamingcollective.cardwire.Mode Mode) == 'u 1' ]] || { echo 'Restore Hybrid/profile policy before removing the patched daemon.' >&2; exit 1; }
fi
if [[ -e $DROPIN || -L $DROPIN ]]; then
    [[ -f $DROPIN && ! -L $DROPIN ]] && cmp -s "$ROOT/override.conf" "$DROPIN" || { echo 'Override changed; refusing to remove an unknown file.' >&2; exit 1; }
    rm -- "$DROPIN"
fi
if [[ -e $ROOT/activated ]]; then
    systemctl daemon-reload
    timeout 40 systemctl restart cardwired.service
    pid=$(systemctl show cardwired.service -p MainPID --value)
    [[ $pid =~ ^[1-9][0-9]*$ ]] || exit 1
    expected=$(< "$ROOT/original.sha256")
    [[ $(sha256sum "/proc/$pid/exe" | cut -d ' ' -f1) == "$expected" ]] || { echo 'Packaged daemon readback mismatch!' >&2; exit 1; }
    [[ $(busctl get-property org.opengamingcollective.cardwire /org/opengamingcollective/cardwire org.opengamingcollective.cardwire.Mode Mode) == 'u 1' ]] || { echo 'Hybrid mode readback mismatch!' >&2; exit 1; }
fi
touch "$ROOT/restored"
echo 'RESTORED: original packaged Cardwire, Hybrid mode. No RPM or session changes.'
ROLLBACK
chmod 0700 "$ROOT/rollback.sh"

cleanup() {
    local rc=$?
    trap - EXIT INT TERM
    exec 8>&-
    if (( keep_success && rc == 0 )); then
        echo "KEPT UNTIL REBOOT: patched Cardwire, unchanged Hybrid; original RPM untouched."
        echo "Rollback (in Hybrid): sudo bash $ROOT/rollback.sh --force"
        exit 0
    fi
    if /usr/bin/bash "$ROOT/rollback.sh"; then
        systemctl stop --no-block "$TIMER.timer" || true
    else
        echo "ROLLBACK FAILED. Run: sudo bash $ROOT/rollback.sh" >&2
        rc=1
    fi
    exit "$rc"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
# The independent timer survives a killed terminal/controller. Keep logs and
# candidate under ROOT for inspection; reboot removes all runtime test files.
systemd-run --quiet --unit="$TIMER" --on-active=120s --timer-property=AccuracySec=1s \
    --property=Type=oneshot /usr/bin/bash "$ROOT/rollback.sh"
touch "$ROOT/activated"
mkdir -p -- "$(dirname -- "$DROPIN")"
install -m 0644 -- "$ROOT/override.conf" "$DROPIN"
systemctl daemon-reload
timeout 40 systemctl restart cardwired.service
pid=$(systemctl show cardwired.service -p MainPID --value)
[[ $pid =~ ^[1-9][0-9]*$ ]] || exit 1
[[ $(sha256sum "/proc/$pid/exe" | cut -d ' ' -f1) == "$EXPECTED_SHA" ]] || { echo 'Candidate is not the running daemon; aborting test.' >&2; exit 1; }
echo 'CANDIDATE VERIFIED: probing only temporary child PIDs; Hybrid mode stays unchanged.'
timeout 40 /usr/bin/python3 "$ROOT/probe.py" | tee "$ROOT/result.json"
[[ $(sha256sum /usr/bin/cardwired | cut -d ' ' -f1) == "$original_sha" ]] || { echo 'Packaged binary unexpectedly changed!' >&2; exit 1; }
if [[ $MODE == --keep-until-reboot ]]; then
    systemctl stop "$TIMER.timer"
    exec 8>"$ROOT/rollback.lock"
    flock -x 8
    [[ ! -e $ROOT/restored ]] || { echo 'Watchdog already restored the packaged daemon.' >&2; exit 1; }
    pid=$(systemctl show cardwired.service -p MainPID --value)
    [[ $pid =~ ^[1-9][0-9]*$ && $(sha256sum "/proc/$pid/exe" | cut -d ' ' -f1) == "$EXPECTED_SHA" ]] || exit 1
    [[ $(busctl get-property "$SERVICE" "$OBJECT" "$SERVICE.Mode" Mode) == 'u 1' ]] || exit 1
    touch "$ROOT/kept"
    keep_success=1
    flock -u 8
fi
