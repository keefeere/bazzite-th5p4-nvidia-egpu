"""One explicitly requested logout/test/recovery; imported by the root worker.

Does not terminate the whole user manager, touch PCI, unload drivers or reset a
GPU. Unreleased clients abort the test. A hung/uncertain PM cycle has no timer
that restarts the display stack blindly.
"""
import json
import os
from pathlib import Path
import pwd
import re
import stat
import subprocess

UNITS = ('cardwired.service', 'displaylink.service', 'sddm.service')
RUNTIME = Path('/run/systemd/system')
USER_RUNTIME = Path('/run/user')
DESKTOP_UNITS = ('graphical-session.target', 'plasma-workspace.target',
                 'plasma-kwin_wayland.service', 'plasma-plasmashell.service')
STOP_BUDGET = 75  # Shared polling deadline; diagnostic commands have their own bounds.
FATAL_GPU = re.compile(r'NVRM:.*Xid|NV_ERR_|GPU has fallen off|Kernel panic|BUG:', re.I)
DISPLAY_ERROR = re.compile(
    r'Flip event timeout|__nv_drm_handle_flip_event|'
    r'(?:nvidia|amdgpu|\[drm).*?(?:\*ERROR\*|\bERROR\b|timed? ?out|GPU reset|GPU fault)|'
    r'WARNING:.*(?:nvidia|amdgpu|drm)|AER:.*(?:Uncorrected|Fatal)', re.I)


def durable_json(path, value):
    with path.open('w') as stream:
        json.dump(value, stream, indent=2)
        stream.flush()
        os.fsync(stream.fileno())


def kernel_records(output):
    records = [json.loads(line) for line in output.splitlines() if line.strip()]
    if any(not isinstance(item, dict) or not isinstance(item.get('MESSAGE'), str)
           or not isinstance(item.get('__CURSOR'), str) or not item['__CURSOR']
           or not isinstance(item.get('_BOOT_ID'), str) or not item['_BOOT_ID'] for item in records):
        raise RuntimeError('Cannot validate kernel journal records; refusing to proceed.')
    return records


def begin_kernel_window(lab, folder):
    records = kernel_records(lab.require_command(
        ['journalctl', '-k', '-b', '-n', '1', '--no-pager', '-o', 'json']))
    if len(records) != 1:
        raise RuntimeError('Cannot establish the pre-logout kernel journal cursor.')
    durable_json(folder / 'graphics-kernel-baseline.json', records[0])


def capture_kernel(lab, folder, phase):
    baseline = json.loads((folder / 'graphics-kernel-baseline.json').read_text())
    # Include and validate the cursor itself: --after-cursor alone could silently
    # skip evidence if journald vacuumed the original record during this run.
    records = kernel_records(lab.require_command([
        'journalctl', '-k', '-b', '--cursor=' + baseline['__CURSOR'],
        '--no-pager', '-o', 'json']))
    if (not records or records[0]['__CURSOR'] != baseline['__CURSOR']
            or any(item.get('_BOOT_ID') != baseline.get('_BOOT_ID') for item in records)):
        raise RuntimeError('Kernel journal baseline disappeared or changed boots; inspection required.')
    records = records[1:]  # Old errors, including the baseline itself, are not new errors.
    durable_json(folder / ('graphics-kernel-' + phase + '.json'), records)
    findings = [{'cursor': item['__CURSOR'], 'message': item['MESSAGE'],
                 'fatal': bool(FATAL_GPU.search(item['MESSAGE']))}
                for item in records if FATAL_GPU.search(item['MESSAGE'])
                or DISPLAY_ERROR.search(item['MESSAGE'])]
    durable_json(folder / ('graphics-errors-' + phase + '.json'), findings)
    return findings


def check_kernel(lab, folder, phase):
    findings = capture_kernel(lab, folder, phase)
    if findings:
        raise RuntimeError('New GPU/DRM errors during the graphical transition; not a clean result. '
                           + findings[0]['message'])


def preflight_graphics(lab):
    # Reuse only the read-only normal-boot/driver/recovery checks. No bypass is
    # armed, and the graphics-only worker never calls lab.experiment().
    info = lab.preflight('platform', 'no-graphics', preparing=True)
    info.update(requested_stage=None, experiment='graphics-only', sleep_guard_bypass='disabled',
                warning='One logout and graphics restart only. Save all work. No sleep, PM settings, '
                        'GPU reset, module unload or PCI changes. Display recovery is not guaranteed.')
    info.pop('nvidia_depth_for_test', None)
    return info


def identity(lab):
    # Reuse the installed strict profile parser, not shell-evaluated user data.
    value = lab.require_command(['/usr/bin/bash', '-c',
        'source /etc/egpu-nvidia/egpu-pci-lib.sh && printf "%s\\n%s\\n" "$DESKTOP_UID" "$DESKTOP_USER"'])
    uid, name = value.strip().splitlines()
    if not uid.isdigit() or int(uid) < 1000 or pwd.getpwuid(int(uid)).pw_name != name:
        raise RuntimeError('Invalid configured desktop identity; refusing session termination.')
    return uid, name


def user_command(saved, *args):
    """Direct user bus, not a transient bridge living in the stopping user manager."""
    account = pwd.getpwuid(int(saved['uid']))
    if account.pw_name != saved['name']:
        raise RuntimeError('Configured desktop identity changed.')
    runtime = USER_RUNTIME / saved['uid']
    if runtime.is_symlink() or runtime.stat().st_uid != account.pw_uid:
        raise RuntimeError('Untrusted desktop runtime directory.')
    bus = runtime / 'bus'
    info = bus.lstat()
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != account.pw_uid:
        raise RuntimeError('Desktop user bus is not an owned socket.')
    return ['/usr/bin/setpriv', '--reuid=' + saved['uid'], '--regid=' + str(account.pw_gid),
            '--init-groups', '--inh-caps=-all', '--ambient-caps=-all',
            '/usr/bin/env', '-i', 'XDG_RUNTIME_DIR=' + str(runtime),
            'DBUS_SESSION_BUS_ADDRESS=unix:path=' + str(bus), 'LC_ALL=C.UTF-8',
            '/usr/bin/systemctl', '--user', *args]


def desktop_state(lab, saved):
    output = lab.require_command(user_command(saved, 'show', *DESKTOP_UNITS,
        '-p', 'Id', '-p', 'LoadState', '-p', 'ActiveState', '-p', 'SubState',
        '-p', 'MainPID', '-p', 'Job', '-p', 'Result'))
    states = {}
    for block in output.strip().split('\n\n'):
        info = dict(line.split('=', 1) for line in block.splitlines() if '=' in line)
        if info.get('Id') in states:
            raise RuntimeError('Ambiguous desktop unit state.')
        states[info.get('Id')] = info
    if set(states) != set(DESKTOP_UNITS) or any(
            info.get('LoadState') != 'loaded' or not info.get('ActiveState') for info in states.values()):
        raise RuntimeError('Cannot resolve the exact Plasma desktop units; refusing logout.')
    return states


def desktop_settled(states):
    return all(info['ActiveState'] in ('inactive', 'failed')
               and info.get('MainPID', '0') == '0'
               and info.get('Job', '') in ('', '0') for info in states.values())


def capture_desktop_shutdown(lab, folder, saved):
    # The user bus can vanish before its final Result properties can be read.
    # Retain systemd's recorded timeout/abort instead of calling that a clean stop.
    baseline = json.loads((folder / 'graphics-kernel-baseline.json').read_text())
    output = lab.require_command(['journalctl', '-b', '_SYSTEMD_UNIT=user@' + saved['uid'] + '.service',
        '_COMM=systemd', '--after-cursor=' + baseline['__CURSOR'], '--no-pager', '-o', 'json'])
    records = kernel_records(output)
    failures = [item for item in records if item.get('USER_UNIT') in DESKTOP_UNITS
                and re.search(r'timed out|Failed with result|signal SIG(?:ABRT|KILL)|status=.*ABRT', item['MESSAGE'])]
    durable_json(folder / 'desktop-shutdown-errors.json', failures)
    for item in failures:
        saved.setdefault('desktop_stop_errors', {})[item['USER_UNIT']] = item['MESSAGE']
    save(folder, saved)


def wait_quiescent(lab, folder, saved):
    """One shared deadline for stop and recovery; never start atop old GPU clients."""
    deadline = saved['stop_deadline_monotonic']
    while True:
        detail = {'remaining_seconds': max(0, deadline - lab.time.monotonic())}
        manager = lab.unit_state('user@' + saved['uid'] + '.service')
        # A disappearing user bus is expected only after its manager has ended.
        if manager.get('ActiveState') in ('inactive', 'failed') and manager.get('MainPID', '0') == '0':
            detail['desktop'] = 'user manager stopped naturally'
            settled = True
        else:
            try:
                states = desktop_state(lab, saved)
                detail['desktop'] = states
                settled = manager.get('ActiveState') == 'active' and desktop_settled(states)
                for unit, info in states.items():
                    if info.get('Result', 'success') != 'success':
                        saved.setdefault('desktop_stop_errors', {})[unit] = info['Result']
            except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
                detail['desktop_error'] = str(exc)
                settled = False
        check = lab.command(['/usr/bin/python3', str(lab.NO_GRAPHICS_CHECK)], timeout=10)
        detail['gpu_check'] = {'exit': check.returncode, 'output': check.stdout}
        save(folder, saved)
        durable_json(folder / 'graphics-stop-progress.json', detail)
        if settled and check.returncode == 0:
            capture_desktop_shutdown(lab, folder, saved)
            (folder / 'graphics-quiescent.json').write_text(check.stdout)
            return
        if lab.time.monotonic() >= deadline:
            raise RuntimeError('Desktop shutdown is incomplete; no sleep or new display stack will be started. '
                               'See graphics-stop-progress.json. No forced kill or automatic retry.')
        lab.time.sleep(0.5)


def sessions(lab, uid):
    graphical = []
    for line in lab.require_command(['loginctl', 'list-sessions', '--no-legend', '--no-pager']).splitlines():
        session_id = line.split()[0]
        if not re.fullmatch(r'[a-zA-Z0-9]+', session_id):
            raise RuntimeError('Unexpected logind session ID.')
        output = lab.require_command(['loginctl', 'show-session', session_id,
            '-p', 'User', '-p', 'Name', '-p', 'Type', '-p', 'Class', '-p', 'Remote'])
        info = dict(item.split('=', 1) for item in output.splitlines() if '=' in item)
        if info.get('Type') not in ('wayland', 'x11') or info.get('Class') == 'greeter':
            continue
        if info.get('User') != uid or info.get('Class') != 'user' or info.get('Remote') != 'no':
            raise RuntimeError('Another or remote graphical session exists; refusing to stop SDDM.')
        graphical.append(session_id)
    return graphical


def plan(lab):
    uid, name = identity(lab)
    saved = {'uid': uid, 'name': name, 'sessions': sessions(lab, uid), 'units': {}, 'owned_masks': []}
    for unit in UNITS:
        mask = RUNTIME / unit
        masked = mask.is_symlink() and os.readlink(mask) == '/dev/null'
        if (mask.exists() or mask.is_symlink()) and not masked:
            raise RuntimeError(f'Foreign runtime unit at {mask}; refusing replacement.')
        info = lab.unit_state(unit)
        fragment = lab.require_command(['systemctl', 'show', unit, '-p', 'FragmentPath', '--value']).strip()
        if fragment != ('/dev/null' if masked else '/usr/lib/systemd/system/' + unit):
            raise RuntimeError(f'Unsupported or overridden service: {unit}: {fragment}')
        if info.get('ActiveState') not in ('active', 'inactive') or (masked and info['ActiveState'] != 'inactive'):
            raise RuntimeError(f'{unit} is not in a stable, supported state.')
        saved['units'][unit] = {'active': info['ActiveState'] == 'active', 'masked': masked}
    alias = lab.require_command(['systemctl', 'show', 'display-manager.service', '-p', 'Id', '--value']).strip()
    if alias != 'sddm.service':
        raise RuntimeError('Only the configured SDDM display manager is supported.')
    saved['desktop_before'] = desktop_state(lab, saved)
    if any(info.get('Job', '') not in ('', '0') or info['ActiveState'] in ('activating', 'deactivating')
           or info.get('Result', 'success') != 'success' for info in saved['desktop_before'].values()):
        raise RuntimeError('The desktop is already transitioning or has failed units; refusing another transition.')
    return saved


def save(folder, saved):
    with (folder / 'graphics-before.json').open('w') as stream:
        json.dump(saved, stream, indent=2)
        stream.flush()
        os.fsync(stream.fileno())


def stop_graphics(lab, folder, saved):
    save(folder, saved)
    begin_kernel_window(lab, folder)
    lab.snapshot(folder, 'before-logout')
    for unit in UNITS:
        if not saved['units'][unit]['masked']:
            saved['owned_masks'].append(unit)
            save(folder, saved)  # Record ownership even if the next command times out.
            lab.require_command(['systemctl', 'mask', '--runtime', unit])
    for unit in UNITS[:2]:
        lab.require_command(['systemctl', 'stop', unit])
    # Validate the direct bus again before requesting a destructive user job.
    argv = user_command(saved, 'stop', '--no-block', 'graphical-session.target', 'plasma-workspace.target')
    saved['desktop_stop_requested'] = True  # A timeout can still have queued the job.
    saved['stop_deadline_monotonic'] = lab.time.monotonic() + STOP_BUDGET
    save(folder, saved)
    try:
        request = lab.command(argv)
        (folder / 'stop-desktop-targets.txt').write_text(request.stdout + f'\n[exit={request.returncode}]\n')
    except Exception as exc:
        (folder / 'stop-desktop-targets.txt').write_text(str(exc) + '\n[unconfirmed request]\n')
        raise
    if request.returncode:
        raise RuntimeError('Desktop stop request failed; refusing to continue: ' + request.stdout.strip())
    lab.require_command(['systemctl', 'stop', 'sddm.service'])
    for session_id in sessions(lab, saved['uid']):
        lab.require_command(['loginctl', 'terminate-session', session_id])
    wait_quiescent(lab, folder, saved)
    if saved.get('desktop_stop_errors'):
        raise RuntimeError('Desktop units failed during shutdown; no PM test allowed: '
                           + json.dumps(saved['desktop_stop_errors']))


def restore_graphics(lab, folder, saved):
    lab.assert_sleep_idle()
    if (lab.selected(lab.PM_TEST.read_text()) != 'none'
            or any(path.exists() for path in (lab.SLEEP_CONF, lab.UNIT_CONF, lab.SLEEP_GUARD_ONCE))):
        raise RuntimeError('PM transition/cleanup is uncertain; graphics remain stopped until reboot or inspection.')
    if saved.get('desktop_stop_requested'):
        # Also on the exception path: the old ten-second timeout used to restart
        # SDDM while KWin was still alive. Neither unmask nor start anything yet.
        wait_quiescent(lab, folder, saved)
    if (folder / 'graphics-kernel-baseline.json').exists():
        findings = capture_kernel(lab, folder, 'before-restore')
        if any(item['fatal'] for item in findings):
            raise RuntimeError('Severe GPU/kernel errors; not reopening GPU clients automatically.')
    log = folder / 'kernel-test.txt'
    if log.exists() and FATAL_GPU.search(log.read_text()):
        raise RuntimeError('NVIDIA errors after the test; not reopening GPU clients automatically.')
    errors = []
    blocked = set()
    for unit in saved['owned_masks']:
        mask = RUNTIME / unit
        if not (mask.exists() or mask.is_symlink()):
            continue
        if not mask.is_symlink() or os.readlink(mask) != '/dev/null':
            errors.append(f'Foreign replacement at {mask}; not removed')
            blocked.add(unit)
            continue
        result = lab.command(['systemctl', 'unmask', '--runtime', unit])
        if result.returncode:
            errors.append(f'Cannot unmask {unit}: {result.stdout}')
            blocked.add(unit)
    for unit in ('cardwired.service', 'sddm.service', 'displaylink.service'):
        if saved['units'][unit]['active'] and unit not in blocked:
            # A timeout is an error, not permission to retry or reset the device.
            try:
                result = lab.command(['systemctl', 'start', unit], timeout=30)
                if result.returncode:
                    errors.append(f'Cannot restore {unit}: {result.stdout}')
                elif lab.unit_state(unit).get('ActiveState') != 'active':
                    errors.append(f'{unit} did not remain active after start')
            except Exception as exc:
                errors.append(f'Cannot restore {unit}: {exc}')
    if errors:
        raise RuntimeError('; '.join(errors))
    (folder / 'graphics-restored.txt').write_text('Prior service/mask states restored. Desktop applications were not restored.\n')
    print('SDDM and auxiliary services restored to their previous states.', flush=True)


def run(lab, folder, *, graphics_only=False):
    if graphics_only:
        preflight_graphics(lab)
    else:
        lab.preflight('platform', 'no-graphics', preparing=True)
    saved = plan(lab)
    print('Ending the graphical session. NVIDIA stays loaded. ' +
          ('Graphics-only roundtrip: NO SLEEP will be requested.' if graphics_only else
           'One platform test is allowed only after a clean graphical shutdown.'), flush=True)
    outcome = {'experiment': 'graphics-only' if graphics_only else 'no-graphics',
               'status': 'preparing', 'sleep_test_called': False}
    durable_json(folder / 'graphics-outcome.json', outcome)
    try:
        stop_graphics(lab, folder, saved)
        check_kernel(lab, folder, 'after-logout')
        outcome['status'] = 'graphics-quiescent'
        if graphics_only:
            return 0
        outcome['sleep_test_called'] = True
        durable_json(folder / 'graphics-outcome.json', outcome)
        result = lab.experiment(folder, 'platform', 'no-graphics')
        outcome['pm_test_returncode'] = result
        if result:
            outcome.update(status='failed', error='PM test did not confirm its requested stage.')
        return result
    except BaseException as exc:
        outcome.update(status='failed', error=str(exc))
        raise
    finally:
        try:
            restore_graphics(lab, folder, saved)
            # Give immediate start-time DRM messages a chance to reach journald.
            # This is not a claim that autologin or the visible picture is ready.
            lab.time.sleep(2)
            lab.snapshot(folder, 'after-graphics')
            check_kernel(lab, folder, 'after-restore')
            if outcome['status'] != 'failed':
                outcome['status'] = 'services-restored; visual confirmation required'
        except Exception as exc:
            outcome.update(status='failed', recovery_error=str(exc))
            (folder / 'graphics-recovery-required.txt').write_text(str(exc) + '\n')
            print('Graphics recovery requires inspection: ' + str(exc), flush=True)
            raise
        finally:
            durable_json(folder / 'graphics-outcome.json', outcome)
