#!/usr/bin/env python3
"""Short Smart isolation test with private Cardwire config/state and rollback.

Existing application GPU clients are temporarily grandfathered to avoid
migrating/killing the desktop or inference. Session/system launchers are never
granted, even when they hold stored device FDs. This is NOT Work activation.
Only explicit --apply changes state; --restore is independently watchdog-safe.
"""
import argparse
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile

ROOT = Path('/run/egpu-cardwire-smart-test')
RENDER_ROOT = Path('/run/egpu-cardwire-smart-render-test')
AMD_RENDER_ROOT = Path('/run/egpu-cardwire-smart-amd-render-test')
EXACT_RENDER_ROOT = Path('/run/egpu-cardwire-smart-exact-render-test')
if Path(__file__).resolve().parent in (ROOT, RENDER_ROOT, AMD_RENDER_ROOT, EXACT_RENDER_ROOT):
    ROOT = Path(__file__).resolve().parent
LEGACY_DROPIN = Path('/run/systemd/system/cardwired.service.d/91-egpu-smart-test.conf')
# Candidate binary override 98 resets BOTH writable and read-only bind lists.
# Exact trial's private configuration must be appended after that reset.
EXACT_DROPIN = Path('/run/systemd/system/cardwired.service.d/99-egpu-smart-test.conf')
DROPIN = EXACT_DROPIN if ROOT == EXACT_RENDER_ROOT else LEGACY_DROPIN
TIMER = ROOT.name + '-rollback'
SERVICE = 'org.opengamingcollective.cardwire'
OBJECT = '/org/opengamingcollective/cardwire'
# Stable fork 0.12.3, commit 6213983: root-authenticated, single-map PID API.
# Earlier runtime-only prototypes are preserved in their archived artifacts;
# new isolation trials must not silently run against those older binaries.
EXPECTED_SHA = '799918680da1f5e9c4458357e83d9b76f7d49f722eee52bd615d8f8e62b7bf4a'
# Local source snapshot 2e63169e3ff064cb, native Rust 1.95/nightly-2026-08-12.
# This exact daemon passed the full two-GPU VM suite; source also passed the
# Nix 2/3/15 GPU matrix. Not installed automatically by this diagnostic.
EXACT_SHA = '57b495249ae2959f15c79b41183533ee8299b88eda43f68a2ef952f2772039bd'
PROPERTIES = ('ExperimentalNvidiaBlock', 'ExternalDisplayAutoSwitch', 'BatteryAutoSwitch')
RENDER_COMMANDS = (
    ('glxinfo', '-B'),
    ('eglinfo', '-B', '-p', 'wayland'),
    ('vkcube', '--wsi', 'wayland', '--c', '120', '--width', '320', '--height', '240', '--suppress_popups'),
)


def run(args, **kwargs):
    result = subprocess.run(args, text=True, capture_output=True, timeout=20, check=False, **kwargs)
    if result.returncode:
        raise RuntimeError(f'{args[0]} failed ({result.returncode}): {result.stderr.strip()}')
    return result.stdout.strip()


def get(interface, key, owner=SERVICE):
    return json.loads(run(['busctl', '--json=short', 'get-property', owner, OBJECT,
                           SERVICE + '.' + interface, key]))['data']


def set_property(owner, interface, key, signature, value):
    run(['busctl', 'set-property', owner, OBJECT, SERVICE + '.' + interface,
         key, signature, str(value).lower()])


def private_daemon_owner():
    owner = json.loads(run(['busctl', '--json=short', 'call', 'org.freedesktop.DBus',
                           '/org/freedesktop/DBus', 'org.freedesktop.DBus', 'GetNameOwner', 's', SERVICE]))['data'][0]
    pid = json.loads(run(['busctl', '--json=short', 'call', 'org.freedesktop.DBus',
                         '/org/freedesktop/DBus', 'org.freedesktop.DBus', 'GetConnectionUnixProcessID', 's', owner]))['data'][0]
    for local, remote in (('config/cardwire.toml', '/etc/cardwire/cardwire.toml'), ('state/mode.json', '/var/lib/cardwire/mode.json')):
        if not os.path.samefile(ROOT / local, Path(f'/proc/{pid}/root{remote}')):
            raise RuntimeError('Cardwire is not using private test config/state')
    verified_daemon_sha(pid)
    # All writes target this UNIQUE bus name. If the watchdog restarts Cardwire,
    # a stale controller cannot accidentally write Smart to the restored daemon.
    return owner


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verified_daemon_sha(pid):
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 1:
        raise RuntimeError('Invalid Cardwire daemon PID')
    digest = sha(Path(f'/proc/{pid}/exe'))
    expected = EXACT_SHA if ROOT == EXACT_RENDER_ROOT else EXPECTED_SHA
    if digest != expected:
        raise RuntimeError('The audited secure Cardwire build for this specific trial is required')
    return digest


def install_private_override(content):
    # The permanent binary override is in /etc now. After reboot there need
    # not be a runtime cardwired drop-in directory from an earlier API trial.
    DROPIN.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    with DROPIN.open('x') as stream:
        stream.write(content)


def load_planner():
    source = Path(__file__).resolve().parent.parent / 'egpu-desktop-profile-plan.py'
    if Path(__file__).resolve().parent == ROOT:
        source = ROOT / 'egpu-desktop-profile-plan.py'
    spec = importlib.util.spec_from_file_location('profile_plan', source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def device_targets(gpus):
    nv = gpus['1']
    amd = gpus['0']
    if (nv.get('vendor') != 'Nvidia' or amd.get('vendor') != 'AMD'
            or nv.get('nvidia_minor') != '0' or len(gpus) != 2):
        raise RuntimeError('Expected exactly the audited AMD + NVIDIA minor 0 mapping')
    return {'nvidia': [f"/dev/dri/card{nv['card']}", f"/dev/dri/renderD{nv['render']}",
                       '/dev/nvidia0', '/dev/nvidiactl'],
            'amd': [f"/dev/dri/renderD{amd['render']}"]}


def gpu_client_pids(targets):
    """Read only relevant character-device descriptors, never arbitrary FD stat."""
    nodes = {Path(path).stat().st_rdev for path in targets['nvidia']}
    found = set()
    for process in Path('/proc').glob('[0-9]*'):
        try:
            for fd in (process / 'fd').iterdir():
                try:
                    if not os.readlink(fd).startswith(('/dev/nvidia', '/dev/dri/')):
                        continue
                    info = fd.stat()
                    if stat.S_ISCHR(info.st_mode) and info.st_rdev in nodes:
                        found.add(int(process.name))
                        break
                except FileNotFoundError:
                    continue
        except FileNotFoundError:
            continue
    return found


def validate_grant(identity, controller):
    if identity['pid'] in (0, controller) or identity['executable'] is None:
        raise RuntimeError(f"Unsafe/unknown grandfathered PID {identity['pid']}, executable {identity['executable']!r}")
    return identity['pid'] != 1 and Path(identity['executable']).name not in (
        'systemd', 'sddm', 'dbus-broker', 'dbus-daemon')


def restore():
    if ROOT.is_symlink() or not ROOT.is_dir() or ROOT.stat().st_uid != 0:
        raise RuntimeError('Unknown runtime directory')
    with (ROOT / 'restore.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if (ROOT / 'restored').exists():
            return
        if DROPIN.is_symlink():
            raise RuntimeError('Refusing unknown symlink override')
        if DROPIN.exists():
            if DROPIN.read_bytes() != (ROOT / 'override.conf').read_bytes():
                raise RuntimeError('Override changed; refusing to remove foreign content')
            DROPIN.unlink()
        if (ROOT / 'activated').exists():
            run(['systemctl', 'daemon-reload'])
            run(['systemctl', 'restart', 'cardwired.service'])
            before = json.loads((ROOT / 'before.json').read_text())
            if 'daemon_sha256' in before:
                pid = int(run(['systemctl', 'show', 'cardwired.service', '-p', 'MainPID', '--value']))
                if verified_daemon_sha(pid) != before['daemon_sha256']:
                    raise RuntimeError('Cardwire daemon changed during policy rollback')
            assert get('Mode', 'Mode') == before['mode'], 'Mode rollback mismatch'
            for key, value in before['config'].items():
                assert get('Config', key) == value, f'{key} rollback mismatch'
            for name, digest in before['files'].items():
                assert sha(Path(name)) == digest, f'Original file changed: {name}'
        (ROOT / 'restored').touch()
        print('RESTORED: original Cardwire policy, patched binary remains; no session/inference restart.', flush=True)


def archive_restored():
    """Preserve a finished trial only after checking live rollback state again."""
    if ROOT.is_symlink() or not ROOT.is_dir() or ROOT.stat().st_uid != 0:
        raise RuntimeError('Unknown runtime directory')
    marker = ROOT / 'restored'
    if marker.is_symlink() or not marker.is_file() or marker.stat().st_uid != 0:
        raise RuntimeError('No root-owned restored marker; inspect the earlier trial')
    if DROPIN.exists() or DROPIN.is_symlink():
        raise RuntimeError('Private policy override still exists; cannot archive')
    for suffix in ('.timer', '.service'):
        name = TIMER + suffix
        state = dict(line.split('=', 1) for line in run([
            'systemctl', 'show', name, '-p', 'Id,ActiveState,SubState,Job,MainPID']).splitlines() if '=' in line)
        if (state.get('Id') != name or state.get('ActiveState') != 'inactive'
                or state.get('Job') != '' or state.get('MainPID', '0') != '0'):
            raise RuntimeError('Earlier rollback unit is not quiescent; cannot archive')
    before = json.loads((ROOT / 'before.json').read_text())
    files = ('/etc/cardwire/cardwire.toml', '/var/lib/cardwire/mode.json')
    if (before.get('mode') != 1 or set(before.get('config', {})) != set(PROPERTIES)
            or set(before.get('files', {})) != set(files)):
        raise RuntimeError('Unknown saved baseline')
    if get('Mode', 'Mode') != 1:
        raise RuntimeError('Hybrid is not restored; cannot archive')
    for key, value in before['config'].items():
        if get('Config', key) != value:
            raise RuntimeError(f'Policy changed since rollback: {key}')
    for path in files:
        if sha(Path(path)) != before['files'][path]:
            raise RuntimeError(f'Baseline file changed: {path}')
    state = dict(line.split('=', 1) for line in run([
        'systemctl', 'show', 'cardwired.service', '-p', 'ActiveState,SubState,MainPID,Job']).splitlines() if '=' in line)
    if state.get('ActiveState') != 'active' or state.get('SubState') != 'running' or state.get('Job') != '':
        raise RuntimeError('Cardwire is not running without a pending job')
    if verified_daemon_sha(int(state['MainPID'])) != before.get('daemon_sha256'):
        raise RuntimeError('Cardwire daemon changed since rollback')
    destination = Path(tempfile.mkdtemp(prefix=ROOT.name + '-archive-', dir=ROOT.parent)) / 'trial'
    ROOT.rename(destination)
    print(f'Archived restored trial at {destination}; no policy/service/session changes.', flush=True)
    return destination


# Sent over stdin to a fresh Python child. It cannot inherit a grant from the
# ungranted controller/runuser. A small delay lets Cardwire's exec analyzer run.
CHILD = r'''
import ctypes as C, json, os, sys, time
targets = json.loads(sys.argv[1]); allow = sys.argv[2] == '1'
time.sleep(0.3)
out = {'uid': os.geteuid(), 'allowed': allow, 'opens': {}}
for role, paths in targets.items():
    for path in paths:
        try:
            fd = os.open(path, os.O_RDWR | os.O_CLOEXEC | os.O_NONBLOCK)
            os.close(fd); out['opens'][path] = True
        except OSError as e:
            out['opens'][path] = False
            out.setdefault('errno', {})[path] = e.errno
        assert out['opens'][path] == (allow or role == 'amd'), (path, out)
if allow:
    cuda = C.CDLL('libcuda.so.1')
    cuda.cuInit.argtypes = [C.c_uint]; cuda.cuInit.restype = C.c_int
    cuda.cuDeviceGetCount.argtypes = [C.POINTER(C.c_int)]; cuda.cuDeviceGetCount.restype = C.c_int
    count = C.c_int()
    assert cuda.cuInit(0) == 0, 'cuInit failed'
    assert cuda.cuDeviceGetCount(C.byref(count)) == 0 and count.value == 1
    out['cuda_device_count'] = count.value
print(json.dumps(out))
'''


def probe(config, targets, allow, as_root=False):
    env = {k: v for k, v in os.environ.items() if not k.startswith('CARDWIRE_')}
    env['CARDWIRE_ALLOW'] = '1' if allow else '0'
    args = ['/usr/bin/python3', '-', json.dumps(targets), env['CARDWIRE_ALLOW']]
    if not as_root:
        args = ['runuser', '-u', config['DESKTOP_USER'], '--', *args]
    return json.loads(run(args, input=CHILD, env=env))


def display_environment(scopes, uid):
    kwin = [p for unit in scopes['units'] for p in unit['processes'] if p.get('role_hint') == 'compositor']
    if len(kwin) != 1:
        raise RuntimeError('Expected one validated KWin compositor')
    # Read only this process and export only display connection keys, never its
    # complete environment (which could contain private application credentials).
    data = Path(f"/proc/{kwin[0]['pid']}/environ").read_bytes().split(b'\0')
    selected = {}
    for item in data:
        key, sep, value = item.partition(b'=')
        if sep and key in (b'DISPLAY', b'WAYLAND_DISPLAY', b'XAUTHORITY'):
            selected[key.decode()] = value.decode()
    # KWin receives these via command-line FDs, so query the user's manager for
    # the actual exported names, without printing any unrelated environment.
    user = run(['getent', 'passwd', str(uid)]).split(':')[0]
    lines = run(['runuser', '-u', user, '--', 'env', f'XDG_RUNTIME_DIR=/run/user/{uid}',
                 f'DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/{uid}/bus',
                 'systemctl', '--user', 'show-environment']).splitlines()
    for line in lines:
        key, sep, value = line.partition('=')
        if sep and key in ('DISPLAY', 'WAYLAND_DISPLAY', 'XAUTHORITY'):
            if any(c in value for c in ('"', "'", '\n', '\\')):
                raise RuntimeError('Unexpected quoting in display environment; inspect first')
            selected[key] = value
    selected['XDG_RUNTIME_DIR'] = f'/run/user/{uid}'
    if 'DISPLAY' not in selected or 'WAYLAND_DISPLAY' not in selected:
        raise RuntimeError('Wayland/Xwayland display names unavailable')
    return selected


def renderer_probes(config, display, hardware, checkpoint=None):
    env = {k: v for k, v in os.environ.items() if not k.startswith(('CARDWIRE_', '__NV_', '__VK_'))}
    env.update(display)
    env.update({'CARDWIRE_ALLOW': '0', '__GLX_VENDOR_LIBRARY_NAME': 'mesa',
                'DRI_PRIME': 'pci-' + hardware['gpus']['igpu']['pci'].replace(':', '_').replace('.', '_'),
                '__NV_PRIME_RENDER_OFFLOAD': '0', 'VK_LOADER_DRIVERS_SELECT': '*radeon*'})
    results = []
    for arguments in RENDER_COMMANDS:
        command = list(arguments)
        args = ['runuser', '-u', config['DESKTOP_USER'], '--',
                'timeout', '--kill-after=2s', '12s', *command]
        try:
            process = subprocess.run(args, text=True, capture_output=True, timeout=20, env=env, check=False)
            results.append({'command': command, 'returncode': process.returncode,
                            'stdout': process.stdout.strip(), 'stderr': process.stderr.strip()})
        except subprocess.TimeoutExpired:
            results.append({'command': command, 'error': 'timed out after 20s'})
        if checkpoint is not None:
            checkpoint(results)
    return results


def renderer_assessment(results):
    """Require positive hardware evidence for ALL three explicit probes.

    Command success does not prove visible presentation. Even a fully
    evidenced report remains pending the user's visual confirmation.
    """
    patterns = {
        'glxinfo': r'^OpenGL renderer string:\s*(.+)$',
        'eglinfo': r'^OpenGL (?:core|compatibility|ES) profile renderer:\s*(.+)$',
        'vkcube': r'^Selected GPU \d+:\s*(.+?),\s*type:\s*.+$',
    }
    bad = ('llvmpipe', 'softpipe', 'swrast', 'failed', 'could not create',
           'vk_error_', 'timed out')
    errors, evidence = [], {}
    expected = {args[0]: list(args) for args in RENDER_COMMANDS}
    if not isinstance(results, list):
        return {'hardware_commands_verified': False, 'errors': ['Probe results are missing'],
                'renderers': {}, 'visible_output_verified': False}
    for name, command in expected.items():
        matches = [r for r in results if isinstance(r, dict) and r.get('command') == command]
        if len(matches) != 1:
            errors.append(f'{name}: expected exactly one result for the audited command')
            continue
        result = matches[0]
        stdout, stderr = result.get('stdout', ''), result.get('stderr', '')
        if not isinstance(stdout, str) or not isinstance(stderr, str):
            errors.append(f'{name}: unknown output format')
            continue
        text = stdout + '\n' + stderr
        if (type(result.get('returncode')) is not int or result['returncode'] != 0
                or result.get('error') or any(word in text.lower() for word in bad)):
            errors.append(f'{name}: nonzero/unknown result, timeout, software renderer or rendering error')
        renderers = re.findall(patterns[name], text, flags=re.MULTILINE)
        evidence[name] = renderers
        if not renderers or any('AMD' not in renderer or 'NVIDIA' in renderer for renderer in renderers):
            errors.append(f'{name}: actual AMD hardware renderer was not established')
    if len(results) != len(expected):
        errors.append('Incomplete or unexpected probe set')
    return {'hardware_commands_verified': not errors, 'errors': errors,
            'renderers': evidence, 'visible_output_verified': False}


def renderer_failure(results):
    return not renderer_assessment(results)['hardware_commands_verified']


def nvidia_outputs(hardware):
    """Map unambiguous connector names using an unrestricted Hybrid snapshot."""
    if hardware.get('errors'):
        raise RuntimeError('Incomplete baseline DRM topology')
    connectors = hardware['connectors']
    names = {}
    for connector in connectors:
        match = re.fullmatch(r'card[0-9]+-(.+)', connector['name'])
        if not match:
            raise RuntimeError('Unknown DRM connector name')
        names.setdefault(match[1], []).append(connector)
    selected = {}
    for name, matches in names.items():
        if any(c['gpu'] == 'nvidia' and c['status'] == 'connected' and c['enabled'] == 'enabled'
               for c in matches):
            if len(matches) != 1:
                raise RuntimeError('Ambiguous cross-GPU connector name; cannot attribute KWin output')
            selected[name] = matches[0]
    if not selected:
        raise RuntimeError('The AMD-primary presentation trial requires an enabled NVIDIA monitor')
    return selected


def kwin_outputs(report):
    """Fail closed on a changed supportInformation format or duplicate names."""
    section = re.search(r'^Screens\n=+\n(.*?)^Compositing\n=+\n', report, re.M | re.S)
    if not section:
        raise RuntimeError('KWin live output report is unavailable')
    text = section[1]
    counts = re.findall(r'^Number of Screens: ([0-9]+)$', text, re.M)
    blocks = re.split(r'^Screen ([0-9]+):\n-+\n', text, flags=re.M)
    if len(counts) != 1 or int(counts[0]) != (len(blocks) - 1) // 2:
        raise RuntimeError('Incomplete KWin live output report')
    outputs, indexes = {}, set()
    for index, block in zip(blocks[1::2], blocks[2::2]):
        names = re.findall(r'^Name: (.+)$', block, re.M)
        enabled = re.findall(r'^Enabled: ([01])$', block, re.M)
        if (index in indexes or len(names) != 1 or len(enabled) != 1 or names[0] in outputs):
            raise RuntimeError('Ambiguous KWin live output report')
        indexes.add(index)
        outputs[names[0]] = enabled[0] == '1'
    return outputs


def amd_compositor(config, hardware, evidence=None):
    """Read live KWin state; Smart intentionally hides NVIDIA sysfs from us.

    hardware is the preflight Hybrid snapshot, used ONLY to attribute output
    names to GPUs. Enabled state and the renderer come from live KWin, not
    that snapshot. A post-rollback topology check must confirm the mapping.
    Neither the observer nor its fresh children receive a GPU exception.
    """
    expected = nvidia_outputs(hardware)
    uid = config['DESKTOP_UID']
    result = json.loads(run([
        'runuser', '-u', config['DESKTOP_USER'], '--', 'env',
        f'XDG_RUNTIME_DIR=/run/user/{uid}', f'DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/{uid}/bus',
        'busctl', '--user', '--json=short', 'call', 'org.kde.KWin', '/KWin',
        'org.kde.KWin', 'supportInformation']))['data'][0]
    renderers = [line.partition(':')[2].strip() for line in result.splitlines()
                 if line.startswith('OpenGL renderer string:')]
    if (len(renderers) != 1 or 'AMD' not in renderers[0]
            or any(word in renderers[0].lower() for word in ('llvmpipe', 'softpipe', 'swrast'))):
        raise RuntimeError('The AMD-primary presentation trial requires a verified AMD hardware compositor')
    outputs = kwin_outputs(result)
    if any(outputs.get(name) is not True for name in expected):
        raise RuntimeError('KWin no longer reports every baseline NVIDIA output enabled')
    if evidence is not None:
        evidence.update({'renderer': renderers[0], 'enabled_nvidia_outputs': sorted(expected),
                         'source': 'live KWin supportInformation; GPU attribution from Hybrid baseline',
                         'visible_output_verified': False})
    return renderers[0]


def verify_presentation_restored(planner, config, baseline, compositor, identity):
    """Check attribution again only after Hybrid is restored, without a grant."""
    hardware = planner.inspect_hardware(config)
    if hardware['gpus'] != baseline['gpus'] or nvidia_outputs(hardware) != nvidia_outputs(baseline):
        raise RuntimeError('NVIDIA output topology changed during the trial')
    if planner.process_identity(identity['pid']) != identity:
        raise RuntimeError('Compositor process changed during the trial')
    if amd_compositor(config, hardware) != compositor:
        raise RuntimeError('Compositor renderer changed after policy rollback')
    return {'topology_verified': True, 'compositor_identity_verified': True,
            'visible_output_verified': False}


def save_result(result):
    # Private directory is created exclusively by this run; the watchdog never
    # writes this report. Atomic replace keeps earlier checkpoints readable.
    temporary = ROOT / 'result.json.tmp'
    temporary.write_text(json.dumps(result, indent=2))
    temporary.replace(ROOT / 'result.json')


def apply(render=False, require_amd=False):
    exact_policy = ROOT == EXACT_RENDER_ROOT
    if exact_policy and not (render and require_amd):
        raise RuntimeError('Exact trial requires AMD-primary rendering and presentation checks')
    if ROOT.exists() or ROOT.is_symlink() or DROPIN.exists() or DROPIN.is_symlink():
        raise RuntimeError('Previous test state exists; inspect instead of overwriting')
    planner = load_planner()
    config = planner.read_hardware_config(Path('/etc/egpu-nvidia/hardware.conf'))
    cardwire = planner.inspect_cardwire()
    hardware = planner.inspect_hardware(config)
    if cardwire['errors'] or cardwire['mode'] != 1:
        raise RuntimeError('A working Hybrid baseline is required')
    pid = int(run(['systemctl', 'show', 'cardwired.service', '-p', 'MainPID', '--value']))
    daemon_sha = verified_daemon_sha(pid)
    plan = planner.make_plan('work-nvidia', config, hardware, cardwire, ['llama.service'],
                             planner.probe_process_access())
    if plan['blockers']:
        raise RuntimeError(str(plan['blockers']))
    compositor = amd_compositor(config, hardware) if require_amd else None
    targets = device_targets(cardwire['gpus'])
    scopes = planner.inspect_service_scopes(config, ['llama.service'])
    if scopes['errors']:
        raise RuntimeError(str(scopes['errors']))
    display = display_environment(scopes, int(config['DESKTOP_UID'])) if render else None
    compositor_identity = None
    if require_amd:
        compositors = [p for unit in scopes['units'] for p in unit['processes']
                       if p.get('role_hint') == 'compositor']
        if len(compositors) != 1:
            raise RuntimeError('Expected one validated KWin compositor')
        compositor_identity = planner.process_identity(compositors[0]['pid'])
    members = {p['pid'] for unit in scopes['units'] for p in unit['processes']
               if unit['unit'] in ('llama.service', 'systemd-logind.service', 'nvidia-persistenced.service')}
    members |= gpu_client_pids(targets)
    members.discard(pid)  # daemon itself already has an explicit built-in exemption
    identities = [planner.process_identity(p) for p in sorted(members)]
    excluded = [identity for identity in identities if not validate_grant(identity, os.getpid())]
    identities = [identity for identity in identities if identity not in excluded]
    ROOT.mkdir(mode=0o700)
    before = {'mode': 1, 'daemon_sha256': daemon_sha,
              'config': {key: get('Config', key) for key in PROPERTIES},
              'files': {path: sha(Path(path)) for path in ('/etc/cardwire/cardwire.toml', '/var/lib/cardwire/mode.json')}}
    (ROOT / 'before.json').write_text(json.dumps(before))
    shutil.copy2(__file__, ROOT / Path(__file__).name)
    (ROOT / 'clients.json').write_text(json.dumps(identities, indent=2))
    (ROOT / 'descriptor-holders-not-granted.json').write_text(json.dumps(excluded, indent=2))
    override = f'[Service]\nBindPaths={ROOT}/config:/etc/cardwire {ROOT}/state:/var/lib/cardwire\n'
    (ROOT / 'override.conf').write_text(override)
    result = {'checks': [], 'status': 'in-progress', 'stage': 'prepared',
              'scope': 'New process device access + CUDA discovery only; existing application clients grandfathered',
              'grant_policy': 'Allow_dGPU_Exact' if exact_policy else 'Allow_dGPU',
              'persistent_profile_verified': False,
              'daemon_sha256': daemon_sha, 'grandfathered_count': len(identities),
              'descriptor_holders_not_granted': [p['pid'] for p in excluded],
              'applied_profile': None, 'visible_output_verified': False,
              'rollback': 'not-started'}
    if require_amd:
        result['baseline_nvidia_outputs'] = nvidia_outputs(hardware)
        result['baseline_compositor_identity'] = compositor_identity
    save_result(result)
    run(['systemd-run', '--quiet', '--unit=' + TIMER, '--on-active=120s', '--timer-property=AccuracySec=1s',
         '/usr/bin/python3', str(ROOT / Path(__file__).name), '--restore'])
    try:
        result['stage'] = 'activate-private-policy'
        (ROOT / 'activated').touch()  # watcher can restart original policy even during preparation
        run(['systemctl', 'stop', 'cardwired.service'])
        shutil.copytree('/etc/cardwire', ROOT / 'config', copy_function=shutil.copy2)
        shutil.copytree('/var/lib/cardwire', ROOT / 'state', copy_function=shutil.copy2)
        install_private_override(override)
        run(['systemctl', 'daemon-reload'])
        run(['systemctl', 'start', 'cardwired.service'])
        owner = private_daemon_owner()
        for identity in identities:
            now = planner.process_identity(identity['pid'])
            if any(now[key] != identity[key] for key in ('start_ticks', 'executable', 'euid', 'cgroup')):
                raise RuntimeError('Grandfathered process changed; retry only after inspection')
            run(['busctl', 'call', owner, OBJECT, SERVICE + '.SmartPolicy', 'RequestProcessAccess',
                 'usu', str(identity['pid']), result['grant_policy'], '1'])
            if exact_policy:
                actual = json.loads(run(['busctl', '--json=short', 'call', owner, OBJECT,
                    SERVICE + '.SmartPolicy', 'GetProcessStatus', 'u', str(identity['pid'])]))['data']
                if actual != ['AllowedExact', [0]]:
                    raise RuntimeError('Exact grant readback failed; no inheritable fallback is allowed')
        set_property(owner, 'Config', 'ExternalDisplayAutoSwitch', 'b', False)
        set_property(owner, 'Config', 'BatteryAutoSwitch', 'b', False)
        set_property(owner, 'Config', 'ExperimentalNvidiaBlock', 'b', True)
        set_property(owner, 'Mode', 'Mode', 'u', 3)
        if get('Mode', 'Mode', owner) != 3:
            raise RuntimeError('Private daemon did not enter Smart')
        for label, allow, as_root in (('user-denied', False, False), ('user-cuda-allowed', True, False),
                                      ('root-denied', False, True)):
            result['stage'] = label
            save_result(result)
            result['checks'].append(probe(config, targets, allow, as_root))
            save_result(result)
        if render:
            result['stage'] = 'renderer-probes'
            def checkpoint(probes):
                result['renderer_probes'] = probes
                save_result(result)
            result['renderer_probes'] = renderer_probes(config, display, hardware, checkpoint)
            result['renderer_assessment'] = renderer_assessment(result['renderer_probes'])
            result['scope'] += '; renderer command results are separate and need visual confirmation'
            save_result(result)
            if not result['renderer_assessment']['hardware_commands_verified']:
                raise RuntimeError('Hardware renderer probes failed; see saved per-command evidence')
        result['stage'] = 'live-presentation'
        # The unique owner must still be in Smart: a completed watchdog rollback
        # cannot turn a Hybrid observation into a successful isolation test.
        if get('Mode', 'Mode', owner) != 3:
            raise RuntimeError('Smart policy changed before final observation')
        if require_amd:
            if planner.process_identity(compositor_identity['pid']) != compositor_identity:
                raise RuntimeError('Compositor process changed during the presentation trial')
            presentation = {}
            if amd_compositor(config, hardware, evidence=presentation) != compositor:
                raise RuntimeError('Compositor renderer changed during the presentation trial')
            result['compositor_renderer'] = compositor
            result['live_presentation'] = presentation
        if get('Mode', 'Mode', owner) != 3:
            raise RuntimeError('Smart policy changed during final observation')
        result['status'] = 'checks-complete-awaiting-rollback'
    except BaseException as error:
        result['status'] = 'failed'
        result['failure'] = {'stage': result['stage'], 'type': type(error).__name__, 'message': str(error)}
        raise
    finally:
        try:
            restore()
            result['rollback'] = 'restored'
            run(['systemctl', 'stop', TIMER + '.timer'])
        except BaseException as error:
            result['status'] = 'failed'
            result['rollback_error'] = {'type': type(error).__name__, 'message': str(error)}
            # If restore failed, the watchdog is deliberately NOT stopped.
            if result['rollback'] != 'restored':
                result['rollback'] = 'failed'
            raise
        finally:
            save_result(result)
    try:
        result['stage'] = 'post-rollback-presentation'
        if require_amd:
            result['post_rollback'] = verify_presentation_restored(
                planner, config, hardware, compositor, compositor_identity)
        result['stage'] = 'complete'
        result['status'] = ('device-access-passed-rendering-needs-visual-confirmation' if render else 'passed')
    except BaseException as error:
        result['status'] = 'failed'
        result['failure'] = {'stage': result['stage'], 'type': type(error).__name__, 'message': str(error)}
        raise
    finally:
        save_result(result)
        print(json.dumps(result, indent=2), flush=True)


def main():
    global ROOT, TIMER, DROPIN
    parser = argparse.ArgumentParser(description=__doc__)
    actions = ('--apply', '--apply-render', '--apply-amd-render', '--apply-exact-amd-render',
               '--restore', '--archive-amd-render', '--archive-exact-amd-render')
    parser.add_argument('action', choices=actions)
    # Explicit switches only; no default live action.
    if len(sys.argv) != 2 or sys.argv[1] not in actions:
        parser.print_help()
        return 2
    if os.geteuid() != 0:
        raise RuntimeError('Requires root')
    if sys.argv[1] == '--restore':
        restore()
    else:
        if sys.argv[1] in ('--apply-exact-amd-render', '--archive-exact-amd-render'):
            ROOT = EXACT_RENDER_ROOT
            TIMER = ROOT.name + '-rollback'
        elif sys.argv[1] in ('--apply-render', '--apply-amd-render', '--archive-amd-render'):
            ROOT = RENDER_ROOT if sys.argv[1] == '--apply-render' else AMD_RENDER_ROOT
            TIMER = ROOT.name + '-rollback'
        DROPIN = EXACT_DROPIN if ROOT == EXACT_RENDER_ROOT else LEGACY_DROPIN
        with open('/run/egpu-nvidia-transition.lock', 'a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if sys.argv[1] in ('--archive-amd-render', '--archive-exact-amd-render'):
                archive_restored()
            else:
                apply(render=sys.argv[1] in ('--apply-render', '--apply-amd-render', '--apply-exact-amd-render'),
                      require_amd=sys.argv[1] in ('--apply-amd-render', '--apply-exact-amd-render'))
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as error:
        print(f'SMART TEST FAILED: {error}', file=sys.stderr)
        sys.exit(1)
