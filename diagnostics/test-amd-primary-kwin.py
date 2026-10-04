#!/usr/bin/env python3
"""Reversible AMD-render/NVIDIA-scanout session trial. Default: read-only check.

--start --restart-session ENDS THE DESKTOP SESSION. Inference is not stopped.
Confirm visible output within twenty-five minutes (configurable at start), or the
watchdog attempts to restore the old KWin configuration and graphical session.
A started login manager is NOT proof of visible output. --confirm-restored
requires user confirmation as well as the baseline renderer and scanout checks.
No Smart mode, driver, PCI, monitor mode or persistent file is changed.
"""
import argparse
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = Path('/run/egpu-amd-primary-kwin-test')
TIMER = 'egpu-amd-primary-kwin-rollback'
UNIT = 'plasma-kwin_wayland.service'
DROPIN_NAME = '90-egpu-amd-primary-test.conf'
SESSION_UNITS = ('graphical-session.target', 'plasma-workspace.target',
                 'plasma-workspace-wayland.target', UNIT)
TRIAL_SECONDS = 1500
SHUTDOWN_SECONDS = 30
RECOVERY_SECONDS = 120


class ShutdownTimeout(RuntimeError):
    """The bounded observation window expired, not a tiny subprocess timeout."""


class Deadline:
    def __init__(self, seconds, description):
        self.end = time.monotonic() + seconds
        self.description = description

    def remaining(self):
        left = self.end - time.monotonic()
        # Never launch a new systemctl/busctl with a near-zero timeout. An
        # exhausted budget is a controller result, not a failed 10ms query.
        if left < 1:
            raise ShutdownTimeout(self.description)
        return min(5, left)


def trial_seconds(value):
    value = int(value)
    if not 300 <= value <= 3600:
        raise ValueError('Trial timeout must be between 300 and 3600 seconds')
    return value


def run(args, timeout=25, **kwargs):
    result = subprocess.run(args, text=True, capture_output=True, timeout=timeout, check=False, **kwargs)
    if result.returncode:
        raise RuntimeError(f'{args[0]} failed ({result.returncode}): {result.stderr.strip()}')
    return result.stdout.strip()


def show(unit, user=None, timeout=25):
    args = ['systemctl'] + (['--user'] if user else [])
    args += ['show', unit, '-p', 'Id,LoadState,ActiveState,SubState,MainPID,ControlPID,PartOf,ControlGroup,InvocationID,Job,RequisiteOf']
    data = run(user_args(user) + args if user else args, timeout=timeout)
    return dict(line.split('=', 1) for line in data.splitlines() if '=' in line)


def user_args(config):
    uid = int(config['DESKTOP_UID'])
    return ['runuser', '-u', config['DESKTOP_USER'], '--', 'env', f'XDG_RUNTIME_DIR=/run/user/{uid}',
            f'DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/{uid}/bus']


def planner():
    path = ROOT / 'egpu-desktop-profile-plan.py' if Path(__file__).resolve().parent == ROOT else Path(__file__).resolve().parents[1] / 'egpu-desktop-profile-plan.py'
    spec = importlib.util.spec_from_file_location('plan', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def override_text(hardware):
    amd, nv = hardware['gpus']['igpu'], hardware['gpus']['nvidia']
    if hardware['errors'] or not amd or not nv or amd['driver'] != 'amdgpu' or nv['driver'] != 'nvidia':
        raise RuntimeError('Both audited, bound DRM GPUs are required')
    for gpu in (amd, nv):
        if not gpu['card'] or not gpu['card'].startswith('card') or not gpu['card'][4:].isdigit():
            raise RuntimeError('Invalid DRM card name')
    return ('# Temporary AMD compositor trial; NVIDIA scanout remains available.\n'
            '[Service]\n'
            f'Environment="KWIN_DRM_DEVICES=/dev/dri/{amd["card"]}:/dev/dri/{nv["card"]}"\n')


# Run as the desktop user: root never follows user-controlled symlinks while
# writing/unlinking user configuration. Only this exact file is owned by us.
USER_FILE = r'''
import os, pathlib, sys
mode, uid, content = sys.argv[1:]
assert os.geteuid() == int(uid) and int(uid) > 0
path = pathlib.Path('/run/user') / uid / 'systemd/user/plasma-kwin_wayland.service.d/90-egpu-amd-primary-test.conf'
if mode == 'check':
    assert not path.exists() and not path.is_symlink(), 'Test override already exists'
elif mode == 'write':
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x') as stream: stream.write(content)
elif mode == 'remove':
    assert not path.is_symlink(), 'Unknown override symlink'
    if path.exists():
        assert path.read_text() == content, 'Override edited; refusing to remove it'
        path.unlink()
elif mode == 'verify':
    assert not path.is_symlink() and path.read_text() == content
else: raise RuntimeError('Unknown file action')
'''


def user_file(config, action, content):
    run(user_args(config) + ['/usr/bin/python3', '-c', USER_FILE, action, config['DESKTOP_UID'], content])


def check():
    lib = planner()
    config = lib.read_hardware_config(Path('/etc/egpu-nvidia/hardware.conf'))
    hw = lib.inspect_hardware(config)
    content = override_text(hw)
    mode = run(['busctl', 'get-property', 'org.opengamingcollective.cardwire', '/org/opengamingcollective/cardwire',
                'org.opengamingcollective.cardwire.Mode', 'Mode'])
    if mode != 'u 1':
        raise RuntimeError('Hybrid baseline required; this trial does not change Cardwire policy')
    if run(['loginctl', 'show-user', config['DESKTOP_UID'], '-p', 'Linger', '--value']) != 'yes':
        raise RuntimeError('User manager must survive logout; refusing to risk inference')
    llama = show('llama.service', config)
    if llama['ActiveState'] != 'active' or llama['PartOf']:
        raise RuntimeError('Expected independent, active llama.service')
    sddm = show('display-manager.service')
    if sddm['Id'] != 'sddm.service' or sddm['ActiveState'] != 'active':
        raise RuntimeError('Expected active SDDM')
    kwin = show(UNIT, config)
    if kwin['ActiveState'] != 'active':
        raise RuntimeError('Expected active desktop KWin')
    user_file(config, 'check', content)
    baseline_renderer = require_hardware_renderer(renderer_information(config), 'NVIDIA')
    return {'config': config, 'gpus': hw['gpus'], 'override': content,
            'llama': llama, 'kwin_before': kwin, 'baseline_renderer': baseline_renderer,
            'timeout_seconds': TRIAL_SECONDS}


def state():
    if not ROOT.is_dir() or ROOT.is_symlink() or ROOT.stat().st_uid != 0:
        raise RuntimeError('Unknown trial directory')
    return json.loads((ROOT / 'state.json').read_text())


def verify_inference(saved):
    current = show('llama.service', saved['config'])
    if any(current[key] != saved['llama'][key] for key in ('MainPID', 'InvocationID', 'ActiveState')):
        raise RuntimeError('llama.service changed during the trial; inspect before continuing')


def require_hardware_renderer(information, vendor):
    renderers = [line.partition(':')[2].strip() for line in information.splitlines()
                 if line.startswith('OpenGL renderer string:')]
    if (len(renderers) != 1 or vendor not in renderers[0]
            or any(word in renderers[0].lower() for word in ('llvmpipe', 'softpipe', 'swrast'))):
        raise RuntimeError(f'KWin has not confirmed a {vendor} hardware renderer')
    return renderers[0]


def require_amd_renderer(information):
    return require_hardware_renderer(information, 'AMD')


def renderer_information(config):
    return json.loads(run(user_args(config) + [
        'busctl', '--user', '--json=short', 'call', 'org.kde.KWin', '/KWin',
        'org.kde.KWin', 'supportInformation']))['data'][0]


def verify_renderer(saved, vendor='AMD'):
    renderer = require_hardware_renderer(renderer_information(saved['config']), vendor)
    if vendor == 'NVIDIA' and renderer != saved.get('baseline_renderer'):
        raise RuntimeError('KWin renderer does not match the recorded NVIDIA baseline')
    hw = planner().inspect_hardware(saved['config'])
    if hw['errors'] or hw['gpus'] != saved['gpus']:
        raise RuntimeError('GPU topology changed; trial cannot be confirmed')
    if not any(c['gpu'] == 'nvidia' and c['status'] == 'connected' and c['enabled'] == 'enabled'
               for c in hw['connectors']):
        raise RuntimeError('No enabled NVIDIA monitor; this is not the intended scanout test')
    return renderer


def terminal(current):
    return (current.get('LoadState') == 'loaded'
            and current.get('ActiveState') in ('inactive', 'failed')
            and current.get('Job') == '')


def require_login_gated(timeout=25):
    current = show('sddm.service', timeout=timeout)
    if not terminal(current) or current.get('MainPID') != '0' or current.get('ControlPID') != '0':
        raise RuntimeError('SDDM is not stopped; refusing to race a new login')


def stop_login_manager(timeout_seconds):
    budget = Deadline(timeout_seconds, 'SDDM shutdown did not converge; inspect before recovery')
    try:
        run(['systemctl', '--no-block', 'stop', 'sddm.service'], timeout=budget.remaining())
    except (RuntimeError, subprocess.TimeoutExpired) as error:
        print(f'SDDM stop request reported: {error}', flush=True)
    while True:
        try:
            current = show('sddm.service', timeout=budget.remaining())
        except subprocess.TimeoutExpired as error:
            print(f'Waiting for SDDM shutdown state: {error}', flush=True)
            budget.remaining()
            time.sleep(0.2)
            continue
        if terminal(current) and current.get('MainPID') == '0' and current.get('ControlPID') == '0':
            return
        if (current.get('LoadState') != 'loaded' or 'Job' not in current
                or not current.get('MainPID', '').isdigit() or not current.get('ControlPID', '').isdigit()
                or current.get('ActiveState') not in ('active', 'activating', 'deactivating', 'inactive', 'failed')):
            raise RuntimeError('SDDM is not stopped and its state is unknown; refusing session teardown')
        budget.remaining()
        time.sleep(0.2)


def cgroup_empty(group):
    # MainPID is a wrapper: its exit alone does not prove KWin has exited.
    path = Path(group)
    if not group or not path.is_absolute() or '..' in path.parts or path == Path('/'):
        raise RuntimeError('Unknown old KWin cgroup')
    events = Path('/sys/fs/cgroup') / group.lstrip('/') / 'cgroup.events'
    try:
        values = dict(line.split() for line in events.read_text().splitlines())
    except FileNotFoundError:
        return True  # Kernel has removed the old service cgroup.
    except (OSError, ValueError) as error:
        raise RuntimeError('Cannot verify old KWin cgroup') from error
    if values.get('populated') not in ('0', '1'):
        raise RuntimeError('Unknown old KWin cgroup population')
    return values['populated'] == '0'


def user_jobs_pending(config, timeout=25):
    # Read-only conservative gate, including propagated portal stop jobs.
    # Do not cancel jobs we cannot prove we own, nor stop the user manager.
    reply = json.loads(run(user_args(config) + [
        'busctl', '--user', '--json=short', 'call', 'org.freedesktop.systemd1',
        '/org/freedesktop/systemd1', 'org.freedesktop.systemd1.Manager', 'ListJobs'], timeout=timeout))
    if (reply.get('type') != 'a(usssoo)' or not isinstance(reply.get('data'), list)
            or len(reply['data']) != 1 or not isinstance(reply['data'][0], list)):
        raise RuntimeError('Unknown user job state; login remains gated')
    return bool(reply['data'][0])


def require_no_user_jobs(config):
    if user_jobs_pending(config):
        raise RuntimeError('User manager still has pending jobs; login remains gated')


def stop_graphical_session(config, old_kwin, timeout_seconds=SHUTDOWN_SECONDS):
    # Unlike PCI attach, do NOT terminate-user, stop user@UID or reset its env.
    # The per-unit override wins over stale manager KWIN_DRM_DEVICES values.
    budget = Deadline(timeout_seconds, 'Graphical shutdown did not converge; login remains gated')
    retried = False
    previous = None

    def request_stop(*units):
        timeout = budget.remaining()
        try:
            # Don't spend the observation budget waiting inside systemctl.
            run(user_args(config) + ['systemctl', '--user', '--no-block', 'stop', *units],
                timeout=timeout)
        except (RuntimeError, subprocess.TimeoutExpired) as error:
            print(f'Graphical stop request reported: {error}', flush=True)
            # Submission/return code is not proof of the resulting unit state.

    request_stop('graphical-session.target', 'plasma-workspace.target')
    while True:
        try:
            require_login_gated(timeout=budget.remaining())
            states = {unit: show(unit, config, timeout=budget.remaining()) for unit in SESSION_UNITS}
        except subprocess.TimeoutExpired as error:
            # A slow read is not proof of teardown completion. Continue only
            # within this budget; do not issue another stop or open SDDM.
            print(f'Waiting for shutdown state: {error}', flush=True)
            budget.remaining()
            time.sleep(0.2)
            continue
        if states != previous:
            print('Graphical shutdown state: ' + json.dumps(states, sort_keys=True), flush=True)
            previous = states
        kwin = states[UNIT]
        if ('InvocationID' not in old_kwin
                or kwin.get('InvocationID') not in ('', old_kwin['InvocationID'])):
            raise RuntimeError('KWin invocation changed during shutdown; refusing another stop')
        # Rollback may begin after KWin is already fully stopped and its
        # cgroup removed. An active snapshot must have a nonempty cgroup.
        old_already_gone = (terminal(old_kwin) and old_kwin.get('MainPID') == '0'
                            and old_kwin.get('ControlPID') == '0' and old_kwin.get('ControlGroup') == '')
        exited = (terminal(kwin) and kwin.get('MainPID') == '0' and kwin.get('ControlPID') == '0'
                  and (old_already_gone or cgroup_empty(old_kwin.get('ControlGroup', ''))))
        if all(terminal(s) for s in states.values()) and exited:
            try:
                if not user_jobs_pending(config, timeout=budget.remaining()):
                    return
            except subprocess.TimeoutExpired as error:
                print(f'Waiting for the user job queue: {error}', flush=True)
        budget.remaining()
        graph = states['graphical-session.target']
        if (not retried and exited
                and all(terminal(states[u]) for u in SESSION_UNITS[1:-1])
                and graph.get('LoadState') == 'loaded'
                and graph.get('ActiveState') == 'active' and graph.get('Job') == ''):
            # A reactivated Requisite client (e.g. desktop portal) can replace
            # a target stop with verify-active, then pin the target. Reconcile
            # exactly once, only after the old workspace/compositor are gone.
            retried = True
            print('Old desktop exited but graphical target remains active without a job; reconciling once.', flush=True)
            request_stop('graphical-session.target')
        time.sleep(0.2)


def restart_graphics(config, *, recovery=False):
    # Gate new logins before ending the old desktop. Leaving SDDM running
    # exposes a greeter while Plasma teardown/rollback is still in progress.
    old_kwin = show(UNIT, config)
    print('Stopping SDDM before ending the old desktop; login is gated during the transition.', flush=True)
    timeout_seconds = RECOVERY_SECONDS if recovery else SHUTDOWN_SECONDS
    stop_login_manager(timeout_seconds)
    require_login_gated()
    stop_graphical_session(config, old_kwin, timeout_seconds=timeout_seconds)
    require_login_gated()
    require_no_user_jobs(config)
    print('Old desktop exited; starting the login screen once with the prepared KWin order.', flush=True)
    run(['systemctl', 'start', 'sddm.service'])
    return old_kwin


def verify_effective_override(saved):
    prefix = user_args(saved['config']) + ['systemctl', '--user', 'show', UNIT, '--value', '-p']
    entries = shlex.split(run(prefix + ['Environment']))
    kwin_values = [entry.partition('=')[2] for entry in entries if entry.startswith('KWIN_DRM_DEVICES=')]
    amd, nv = saved['gpus']['igpu']['card'], saved['gpus']['nvidia']['card']
    if kwin_values != [f'/dev/dri/{amd}:/dev/dri/{nv}']:
        raise RuntimeError('Another override masks the trial KWin device order')
    unset = shlex.split(run(prefix + ['UnsetEnvironment']))
    if any(entry.partition('=')[0] == 'KWIN_DRM_DEVICES' for entry in unset):
        raise RuntimeError('Existing UnsetEnvironment cancels the trial KWin device order')


def restore(restart=False, watchdog=False):
    saved = state()
    with (ROOT / 'state.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if (ROOT / 'restored').exists() or (watchdog and (ROOT / 'confirmed').exists()):
            return
        if watchdog and (ROOT / 'recovery-required.json').exists():
            print('Recovery previously failed; no automatic second logout. Inspect before manual recovery.', flush=True)
            return
        result = {'configuration_restored': False, 'login_manager_start_returned': False,
                  'visible_output_verified': False, 'needs_visual_confirmation': True}
        try:
            user_file(saved['config'], 'remove', saved['override'])
            run(user_args(saved['config']) + ['systemctl', '--user', 'daemon-reload'])
            result['configuration_restored'] = True
            if restart and (ROOT / 'started').exists():
                # Recovery has its own longer window for systemd's existing
                # stop job to finish. Never force-kill, cancel foreign jobs,
                # or open SDDM over a live/unknown compositor or queued stop.
                old_kwin = restart_graphics(saved['config'], recovery=True)
                result['previous_kwin_invocation'] = old_kwin.get('InvocationID')
                result['login_manager_start_returned'] = True
            # Persist the mechanical result before any later diagnostic can
            # fail: retries must not log out a newly opened session again.
            (ROOT / 'restore-result.json').write_text(json.dumps(result, indent=2))
            (ROOT / 'restored').touch()
            verify_inference(saved)
        except Exception as error:
            result['error'] = str(error)
            (ROOT / 'recovery-required.json').write_text(json.dumps(result, indent=2))
            print('RECOVERY NEEDS ATTENTION: no forced GPU reset or further automatic logout. '
                  'Inspect the saved recovery-required.json and service journal.', flush=True)
            raise
        (ROOT / 'recovery-required.json').unlink(missing_ok=True)
        print('CONFIGURATION RESTORED: trial override removed; inference identity unchanged. '
              'Visible GUI is NOT confirmed. After seeing the desktop, use '
              '--confirm-restored --visible-output-ok.', flush=True)
        if not restart and (ROOT / 'started').exists():
            print('The running compositor is unchanged until the next login.')


def start(timeout_seconds=TRIAL_SECONDS):
    if ROOT.exists() or ROOT.is_symlink():
        raise RuntimeError('Previous trial state exists; inspect instead of overwriting')
    saved = check()
    saved['timeout_seconds'] = trial_seconds(timeout_seconds)
    ROOT.mkdir(mode=0o700)
    (ROOT / 'state.json').write_text(json.dumps(saved, indent=2))
    shutil.copy2(__file__, ROOT / Path(__file__).name)
    shutil.copy2(planner().__file__, ROOT / 'egpu-desktop-profile-plan.py')
    # Independent execution survives the desktop/app going away at logout.
    run(['systemd-run', '--quiet', '--unit=egpu-amd-primary-kwin-trial',
         '/usr/bin/python3', str(ROOT / Path(__file__).name), '--execute'])
    print(f'Trial queued. Save work BEFORE --start: graphical logout follows. '
          f'Confirm visible output within {saved["timeout_seconds"]} seconds of controller start.')


def execute():
    saved = state()
    seconds = trial_seconds(saved['timeout_seconds'])
    run(['systemd-run', '--quiet', '--unit=' + TIMER, f'--on-active={seconds}s', '--timer-property=AccuracySec=1s',
         '/usr/bin/python3', str(ROOT / Path(__file__).name), '--watchdog'])
    try:
        with (ROOT / 'state.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if (ROOT / 'restored').exists():
                raise RuntimeError('Trial already rolled back')
            if planner().inspect_hardware(saved['config'])['gpus'] != saved['gpus']:
                raise RuntimeError('DRM topology changed since preflight')
            verify_inference(saved)
            user_file(saved['config'], 'write', saved['override'])
            run(user_args(saved['config']) + ['systemctl', '--user', 'daemon-reload'])
            verify_effective_override(saved)
            (ROOT / 'started').touch()
            restart_graphics(saved['config'])
            verify_inference(saved)
    except BaseException:
        restore(restart=True)
        raise


def confirm():
    saved = state()
    with (ROOT / 'state.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if ((ROOT / 'restored').exists() or (ROOT / 'recovery-required.json').exists()
                or not (ROOT / 'started').exists()):
            raise RuntimeError('No running trial to confirm')
        user_file(saved['config'], 'verify', saved['override'])
        kwin = show(UNIT, saved['config'])
        if kwin['ActiveState'] != 'active' or kwin['InvocationID'] == saved['kwin_before']['InvocationID']:
            raise RuntimeError('A new KWin session must be active before confirmation')
        verify_inference(saved)
        renderer = verify_renderer(saved)
        (ROOT / 'renderer.txt').write_text(renderer + '\n')
        (ROOT / 'confirmed').touch()
    run(['systemctl', 'stop', TIMER + '.timer'])
    print('Visual trial confirmed by user; AMD-first override stays until reboot/restore. Work isolation not activated.')


def confirm_restored():
    """Only after a human sees usable output; never inferred from service state."""
    saved = state()
    with (ROOT / 'state.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not (ROOT / 'restored').exists():
            raise RuntimeError('Configuration rollback has not completed')
        result = json.loads((ROOT / 'restore-result.json').read_text())
        user_file(saved['config'], 'check', saved['override'])
        kwin = show(UNIT, saved['config'])
        if (kwin.get('ActiveState') != 'active' or kwin.get('Job') != ''
                or not kwin.get('InvocationID') or kwin.get('MainPID') in (None, '', '0')
                or (result.get('previous_kwin_invocation')
                    and kwin['InvocationID'] == result['previous_kwin_invocation'])):
            raise RuntimeError('A new active baseline KWin session is required')
        verify_inference(saved)
        result['renderer'] = verify_renderer(saved, vendor='NVIDIA')
        result['visible_output_verified'] = True
        result['needs_visual_confirmation'] = False
        (ROOT / 'restore-result.json').write_text(json.dumps(result, indent=2))
        (ROOT / 'restore-confirmed').touch()
        (ROOT / 'recovery-required.json').unlink(missing_ok=True)
    print('Baseline renderer/scanout checked; usable desktop visually confirmed by user. No restart performed.')


def inactive_trial_units():
    """A marker alone cannot prove the controller or its watchdog has stopped."""
    units = ('egpu-amd-primary-kwin-trial.service', TIMER + '.service', TIMER + '.timer')
    failed = []
    for unit in units:
        properties = run(['systemctl', 'show', unit, '-p', 'LoadState,ActiveState,MainPID,Job'])
        current = dict(line.split('=', 1) for line in properties.splitlines() if '=' in line)
        if (current.get('LoadState') not in ('loaded', 'not-found')
                or current.get('ActiveState') not in ('inactive', 'failed')
                or current.get('Job') != ''
                or (unit.endswith('.service') and current.get('MainPID') != '0')):
            raise RuntimeError(f'{unit} is still running/queued or its state is unknown; refusing to archive')
        if current['ActiveState'] == 'failed':
            failed.append(unit)
    return failed


def archive_restored():
    """Preserve a rolled-back attempt so a new explicitly authorized trial can run."""
    saved = state()
    with (ROOT / 'state.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        marker = ROOT / 'restored'
        if marker.is_symlink() or not marker.is_file():
            raise RuntimeError('Only a restored trial can be archived; restore/inspect the current trial first')
        if (ROOT / 'started').exists() and not (ROOT / 'restore-confirmed').is_file():
            raise RuntimeError('Visible baseline must be confirmed before archiving a started trial')
        failed = inactive_trial_units()
        user_file(saved['config'], 'check', saved['override'])
        verify_inference(saved)
        if failed:
            # Release completed transient units; never stop an active controller.
            run(['systemctl', 'reset-failed', *failed])
        # The new private directory prevents overwriting any previous archive.
        archive = Path(tempfile.mkdtemp(prefix=ROOT.name + '-archive-', dir=ROOT.parent)) / 'trial'
        ROOT.rename(archive)
        print(f'Previous restored trial preserved at {archive}. No session or inference restart performed.')
        return archive


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_mutually_exclusive_group()
    for action in ('check', 'start', 'execute', 'confirm', 'confirm-restored', 'restore', 'watchdog', 'archive-restored'):
        actions.add_argument('--' + action, action='store_true')
    parser.add_argument('--restart-session', action='store_true')
    parser.add_argument('--timeout-seconds', type=trial_seconds, default=None,
                        help=f'Trial watchdog: 300–3600 seconds; default {TRIAL_SECONDS}; only with --start')
    parser.add_argument('--visible-output-ok', action='store_true',
                        help='User has explicitly confirmed usable visible output (not just active services)')
    args = parser.parse_args()
    if args.start and not args.restart_session:
        parser.error('--start requires --restart-session: this closes desktop applications')
    if args.timeout_seconds is not None and not args.start:
        parser.error('--timeout-seconds is only valid with --start; it does not extend a running timer')
    if (args.confirm or args.confirm_restored) and not args.visible_output_ok:
        parser.error('Confirmation requires --visible-output-ok after actual user observation')
    if (args.execute or args.watchdog) and Path(__file__).resolve().parent != ROOT:
        parser.error('Internal actions must use the staged root-owned runtime script')
    if os.geteuid() != 0:
        raise RuntimeError('Use sudo/pkexec; default --check is read-only')
    if args.watchdog:
        restore(restart=True, watchdog=True)
    elif args.restore:
        restore(restart=args.restart_session)
    elif args.confirm:
        confirm()
    elif args.confirm_restored:
        confirm_restored()
    else:
        with open('/run/egpu-nvidia-transition.lock', 'a') as lock:
            # The queued execute service may start before the submitting
            # --start process has returned and released its preflight lock.
            deadline = time.monotonic() + (10 if args.execute else 0)
            while True:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise RuntimeError('Another eGPU transition is active')
                    time.sleep(0.1)
            if args.execute:
                execute()
            elif args.archive_restored:
                archive_restored()
            elif args.start:
                start(timeout_seconds=args.timeout_seconds or TRIAL_SECONDS)
            else:
                print(json.dumps(check(), indent=2))
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as error:
        print(f'KWIN TRIAL FAILED: {error}', file=sys.stderr)
        sys.exit(1)
