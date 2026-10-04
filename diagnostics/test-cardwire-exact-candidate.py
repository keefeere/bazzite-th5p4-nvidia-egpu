#!/usr/bin/env python3
"""One bounded, temporary native-candidate Smart test; never install a release.

The transient systemd service owns the trial. ExecStopPost restores the original
binary even if its worker times out or the initiating terminal disappears.
Existing Smart-test watchdog remains an additional, earlier policy rollback.
"""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path('/run/egpu-cardwire-exact-candidate-test')
UNIT = ROOT.name
SCRIPT = ROOT / 'controller.py'
PRIVATE = Path('/run/egpu-cardwire-smart-exact-render-test')
LEGACY_PRIVATE_DROPIN = Path('/run/systemd/system/cardwired.service.d/91-egpu-smart-test.conf')
PRIVATE_DROPIN = Path('/run/systemd/system/cardwired.service.d/99-egpu-smart-test.conf')
DROPIN = Path('/run/systemd/system/cardwired.service.d/98-exact-candidate-test.conf')
PERMANENT = Path('/etc/systemd/system/cardwired.service.d/95-local-secure-policy.conf')
OLD_BINARY = Path('/var/opt/cardwire-fork/releases/62139830260839c037c97e6a8dc607cc47559b90/bin/cardwired')
CANDIDATE = Path('/var/home/keefeere/_repos/_home/cardwire-stable-process-access/dist/local-exact-btf-2e63169e3ff064cb/bin/cardwired')
OLD_SHA = '799918680da1f5e9c4458357e83d9b76f7d49f722eee52bd615d8f8e62b7bf4a'
NEW_SHA = '57b495249ae2959f15c79b41183533ee8299b88eda43f68a2ef952f2772039bd'
FILES = (PERMANENT, Path('/usr/bin/cardwired'), Path('/etc/cardwire/cardwire.toml'),
         Path('/var/lib/cardwire/mode.json'))
BUS = 'org.opengamingcollective.cardwire'
OBJECT = '/org/opengamingcollective/cardwire'
CONFIG_KEYS = ('ExperimentalNvidiaBlock', 'ExternalDisplayAutoSwitch', 'BatteryAutoSwitch')


def run(args, timeout=30):
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f'{args[0]} ({result.returncode}): {result.stderr.strip()}')
    return result.stdout.strip()


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def daemon_digest():
    pid = int(run(['systemctl', 'show', 'cardwired.service', '-p', 'MainPID', '--value']))
    if pid <= 1:
        raise RuntimeError('Cardwire is not running')
    return digest(Path(f'/proc/{pid}/exe'))


def property_value(interface, key):
    return json.loads(run(['busctl', '--json=short', 'get-property', BUS, OBJECT,
                          BUS + '.' + interface, key]))['data']


def policy():
    return {'mode': property_value('Mode', 'Mode'),
            'config': {key: property_value('Config', key) for key in CONFIG_KEYS}}


def clients():
    result = {}
    for unit in ('plasma-kwin_wayland.service', 'llama.service'):
        text = run(['runuser', '-u', 'keefeere', '--', 'env', 'XDG_RUNTIME_DIR=/run/user/1000',
                    'DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus',
                    'systemctl', '--user', 'show', unit,
                    '-p', 'MainPID,InvocationID,ActiveState'])
        state = dict(line.split('=', 1) for line in text.splitlines())
        if state['ActiveState'] != 'active' or int(state['MainPID']) <= 1:
            raise RuntimeError(f'{unit} must already be running')
        result[unit] = state
    return result


def override_text():
    # Empty BindReadOnlyPaths resets BOTH bind lists. The private Smart-test
    # mount override is deliberately ordered AFTER this file (99 after 98).
    return f'[Service]\nBindReadOnlyPaths=\nBindReadOnlyPaths={ROOT}/cardwired:/usr/bin/cardwired\n'


def checked_remove(path, expected):
    if path.is_symlink():
        raise RuntimeError(f'Refusing symlink override: {path}')
    if path.exists():
        if path.read_bytes() != expected:
            raise RuntimeError(f'Refusing changed override: {path}')
        path.unlink()


def require_runtime():
    if (Path(__file__).resolve() != SCRIPT or ROOT.is_symlink()
            or ROOT.stat().st_uid != 0 or ROOT.stat().st_mode & 0o077):
        raise RuntimeError('Internal actions require the private root-owned staged controller')


def baseline_matches(before, include_clients=True):
    if daemon_digest() != OLD_SHA or policy() != before['policy']:
        raise RuntimeError('Original daemon/policy not restored')
    for name, value in before['files'].items():
        if digest(Path(name)) != value:
            raise RuntimeError(f'Baseline file changed: {name}')
    if include_clients and clients() != before['clients']:
        raise RuntimeError('KWin or llama identity changed during test')


def restore():
    require_runtime()
    with (ROOT / 'restore.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if (ROOT / 'restored').exists():
            return
        before = json.loads((ROOT / 'before.json').read_text())
        # ExecStopPost runs only after the trial worker and all its children have
        # exited. Stop the independent inner watchdog before changing its binary.
        timer = PRIVATE.name + '-rollback'
        for suffix in ('.timer', '.service'):
            state = run(['systemctl', 'show', timer + suffix, '-p', 'LoadState', '--value'])
            if state != 'not-found':
                run(['systemctl', 'stop', timer + suffix], timeout=50)
        if PRIVATE_DROPIN.exists() or PRIVATE_DROPIN.is_symlink():
            expected = f'[Service]\nBindPaths={PRIVATE}/config:/etc/cardwire {PRIVATE}/state:/var/lib/cardwire\n'
            checked_remove(PRIVATE_DROPIN, expected.encode())
        checked_remove(DROPIN, override_text().encode())
        if (ROOT / 'activation-started').exists():
            # Do not use a changed recovery binary or original config blindly.
            for path in FILES:
                if digest(path) != before['files'][str(path)]:
                    raise RuntimeError(f'Original configuration changed: {path}')
            if digest(OLD_BINARY) != OLD_SHA:
                raise RuntimeError('Recovery binary changed')
            run(['systemctl', 'daemon-reload'])
            run(['systemctl', 'restart', 'cardwired.service'], timeout=50)
        baseline_matches(before)
        (ROOT / 'restored').touch()
        print('RESTORED: secure 6213983 + original Hybrid; KWin and llama unchanged.', flush=True)


def execute():
    require_runtime()
    before = json.loads((ROOT / 'before.json').read_text())
    baseline_matches(before)
    if digest(ROOT / 'cardwired') != NEW_SHA:
        raise RuntimeError('Staged candidate changed')
    if PRIVATE.exists() or PRIVATE_DROPIN.exists() or LEGACY_PRIVATE_DROPIN.exists():
        raise RuntimeError('An existing private trial must be inspected first')
    with open('/run/egpu-nvidia-transition.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        (ROOT / 'activation-started').touch()
        DROPIN.parent.mkdir(parents=True, exist_ok=True)
        with DROPIN.open('x') as stream:
            stream.write(override_text())
        run(['systemctl', 'daemon-reload'])
        run(['systemctl', 'restart', 'cardwired.service'], timeout=50)
        if daemon_digest() != NEW_SHA or policy() != before['policy']:
            raise RuntimeError('Temporary candidate activation did not preserve Hybrid')
    print('CANDIDATE ACTIVE: starting bounded Exact Smart → Hybrid trial.', flush=True)
    # The inner controller owns the eGPU transition lock and independent 120s
    # policy watchdog. The outer 180s lifetime also covers candidate activation.
    subprocess.run(['/usr/bin/python3', str(ROOT / 'diagnostics/test-cardwire-smart-runtime.py'),
                    '--apply-exact-amd-render'], check=True, timeout=145)


def start():
    for path in (ROOT, PRIVATE, PRIVATE_DROPIN, LEGACY_PRIVATE_DROPIN, DROPIN):
        if path.exists() or path.is_symlink():
            raise RuntimeError(f'Existing state must be inspected, not overwritten: {path}')
    if daemon_digest() != OLD_SHA or digest(OLD_BINARY) != OLD_SHA:
        raise RuntimeError('Expected running secure 6213983 baseline')
    if digest(CANDIDATE) != NEW_SHA:
        raise RuntimeError('Candidate differs from VM-tested binary')
    bindings = run(['systemctl', 'show', 'cardwired.service', '-p', 'BindReadOnlyPaths', '--value'])
    if bindings != str(OLD_BINARY) + ':/usr/bin/cardwired:rbind':
        raise RuntimeError('Unexpected read-only mounts')
    if run(['systemctl', 'show', 'cardwired.service', '-p', 'BindPaths', '--value']):
        raise RuntimeError('Unexpected private policy mounts')
    before = {'policy': policy(), 'files': {str(p): digest(p) for p in FILES}, 'clients': clients()}
    if before['policy'] != {'mode': 1, 'config': {key: False for key in CONFIG_KEYS}}:
        raise RuntimeError('Expected ordinary Hybrid baseline with automatic switching off')
    run(['nvidia-smi', '-L'], timeout=15)
    ROOT.mkdir(mode=0o700)
    (ROOT / 'before.json').write_text(json.dumps(before, indent=2))
    shutil.copy2(__file__, SCRIPT)
    shutil.copy2(CANDIDATE, ROOT / 'cardwired')
    os.chmod(ROOT / 'cardwired', 0o755)
    run(['chcon', '--reference=/usr/bin/cardwired', str(ROOT / 'cardwired')])
    if digest(ROOT / 'cardwired') != NEW_SHA:
        raise RuntimeError('Copied candidate mismatch')
    if 'not found' in run(['ldd', str(ROOT / 'cardwired')]):
        raise RuntimeError('Missing native dependencies')
    source = Path(__file__).resolve().parent.parent
    (ROOT / 'diagnostics').mkdir()
    for relative in ('egpu-desktop-profile-plan.py', 'diagnostics/test-cardwire-smart-runtime.py'):
        shutil.copy2(source / relative, ROOT / relative)
    run(['systemd-run', '--quiet', '--unit=' + UNIT, '--property=Type=exec',
         '--property=RuntimeMaxSec=180s', '--property=TimeoutStopSec=90s',
         '--property=KillMode=control-group',
         '--property=ExecStopPost=/usr/bin/python3 ' + str(SCRIPT) + ' --restore',
         '/usr/bin/python3', str(SCRIPT), '--execute'])
    print('STARTED: bounded candidate test; systemd will restore the old binary on exit.', flush=True)


def quiescent_unit(unit):
    # Timers have no MainPID/ControlPID properties. Request only properties
    # belonging to the unit type; omitted service PIDs still fail closed.
    fields = ['LoadState', 'ActiveState', 'SubState', 'Job']
    if unit.endswith('.service'):
        fields += ['MainPID', 'ControlPID']
    elif not unit.endswith('.timer'):
        raise RuntimeError(f'Unsupported trial unit type: {unit}')
    state = dict(line.split('=', 1) for line in run([
        'systemctl', 'show', unit, '-p', ','.join(fields)]).splitlines())
    if (set(state) != set(fields) or state.get('LoadState') not in ('loaded', 'not-found')
            or (state.get('ActiveState'), state.get('SubState')) not in
            (('inactive', 'dead'), ('failed', 'failed')) or state.get('Job')
            or state.get('MainPID', '0') != '0' or state.get('ControlPID', '0') != '0'):
        raise RuntimeError(f'Trial unit is not quiescent: {unit}')


def owned_trial_directory(path):
    if path.is_symlink() or not path.is_dir() or path.stat().st_uid != 0 or path.stat().st_mode & 0o077:
        raise RuntimeError(f'Unknown private trial directory: {path}')


def archive_restored():
    """Preserve a terminal trial, including the old 91/98 ordering failure.

    This action never starts/restarts Cardwire or enters Smart. The old staged
    controller is not rewritten. Its original report and rollback evidence stay
    together in the archive, while the installed secure baseline is rechecked.
    """
    with open('/run/egpu-nvidia-transition.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        owned_trial_directory(ROOT)
        marker = ROOT / 'restored'
        if marker.is_symlink() or not marker.is_file() or marker.stat().st_uid != 0:
            raise RuntimeError('Successful outer rollback is not recorded')
        for path in (DROPIN, PRIVATE_DROPIN, LEGACY_PRIVATE_DROPIN):
            if path.exists() or path.is_symlink():
                raise RuntimeError(f'An override still exists: {path}')
        for unit in (UNIT + '.service', PRIVATE.name + '-rollback.timer', PRIVATE.name + '-rollback.service'):
            quiescent_unit(unit)
        before = json.loads((ROOT / 'before.json').read_text())
        baseline_matches(before)
        if PRIVATE.exists() or PRIVATE.is_symlink():
            owned_trial_directory(PRIVATE)
            inner = json.loads((PRIVATE / 'before.json').read_text())
            if (inner.get('daemon_sha256') != NEW_SHA or inner.get('mode') != before['policy']['mode']
                    or inner.get('config') != before['policy']['config']
                    or inner.get('files') != {str(p): before['files'][str(p)] for p in FILES[2:]}):
                raise RuntimeError('Private trial does not belong to this candidate/baseline')
            # Move inside the still root-owned outer directory first; even an
            # interruption between renames cannot expose confidential snapshots.
            PRIVATE.rename(ROOT / 'private-policy-trial')
        destination = Path(tempfile.mkdtemp(prefix=ROOT.name + '-archive-', dir=ROOT.parent)) / 'trial'
        ROOT.rename(destination)
        # Allows the completed failed transient unit to be collected; no start.
        run(['systemctl', 'reset-failed', UNIT + '.service'])
        print(f'ARCHIVED: {destination}; no GPU policy or running service changed.', flush=True)


def main():
    if os.geteuid() != 0:
        raise RuntimeError('Requires administrator authentication')
    if len(sys.argv) != 2 or sys.argv[1] not in ('--start', '--execute', '--restore', '--archive-restored'):
        raise RuntimeError('Explicit --start required; internal actions use the staged copy only')
    {'--start': start, '--execute': execute, '--restore': restore,
     '--archive-restored': archive_restored}[sys.argv[1]]()


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(f'CANDIDATE TEST FAILED: {error}', file=sys.stderr, flush=True)
        sys.exit(1)
