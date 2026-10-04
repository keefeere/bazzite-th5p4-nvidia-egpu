#!/usr/bin/env python3
"""One bounded, temporary service-roles Cardwire trial; installs nothing.

Same ownership model as test-cardwire-exact-candidate.py: a transient systemd
service owns the trial and its ExecStopPost restores the secure baseline even if
the worker times out. The trial swaps in the VM-tested `service-roles` daemon,
creates the pinned guard with the Gaming profile (every process keeps ordinary
NVIDIA access) and verifies the session/CUDA path. Work profiles are NOT applied
unless --with-deny-probe is given: that flag briefly applies work-nvidia and checks
that a NEW non-role process is refused (existing FDs, KWin and llama are unaffected).

Never run by an agent without the user's explicit go. Requires root.
"""
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time

HERE = Path(__file__).resolve().parent
ROOT = Path('/run/egpu-cardwire-service-roles-test')
UNIT = ROOT.name
SCRIPT = ROOT / 'controller.py'
DROPIN = Path('/run/systemd/system/cardwired.service.d/98-service-roles-test.conf')
CONFIG = Path('/etc/cardwire/service-roles.toml')
PINS = Path('/sys/fs/bpf/cardwire-service-roles')
PIN_NAMES = ('exec_link', 'open_link', 'CW_ACTIVE', 'CW_DEVICES_MAP', 'CW_ROLE_TASKS')
HARDWARE = Path('/etc/egpu-nvidia/hardware.conf')
CANDIDATE_DIR = Path('/var/home/keefeere/_repos/_home/cardwire-stable-process-access/'
                     'dist/local-service-roles-72f973bd8b06de59')
DAEMON_SHA = '72f973bd8b06de5934e49ce6583efa44f589af200bc2e264ea7200bd7d23b408'
OBJECT_SHA = 'e6b22a22cf515f510b830ce1aecab368dd71f23ece3d168ff0a0b996347f84d0'
BUS = 'org.opengamingcollective.cardwire'
OBJECT = '/org/opengamingcollective/cardwire'
IFACE = BUS + '.ServiceRoles'


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def locate(name):
    """Repo layout (diagnostics/ + parent) or the staged ROOT/helpers copy."""
    for candidate in (HERE / 'helpers' / name, HERE / name, HERE.parent / name):
        if candidate.is_file():
            return candidate
    raise RuntimeError(f'missing helper {name}')


base = load(locate('test-cardwire-exact-candidate.py'), 'base')
generator = load(locate('egpu-service-roles-config.py'), 'generator')


def override_text():
    return ('[Service]\nBindReadOnlyPaths=\n'
            f'BindReadOnlyPaths={ROOT}/cardwired:/usr/bin/cardwired\n'
            'NotifyAccess=main\nFileDescriptorStoreMax=64\n'
            'FileDescriptorStorePreserve=yes\nReadWritePaths=/sys/fs/bpf\n')


def config_text(inventory_nodes):
    roles = [generator.parse_role('compute:/usr/bin/true:/sys/fs/cgroup/system.slice:0')]
    return generator.render(inventory_nodes, roles, str(ROOT / 'service_guard.bpf.o'),
                            initial_profile='gaming-nvidia')


def busctl(*args):
    return base.run(['busctl', '--system', *args])


def apply_profile(name):
    busctl('call', BUS, OBJECT, IFACE, 'ApplyProfile', 's', name)
    current = busctl('get-property', BUS, OBJECT, IFACE, 'CurrentProfile')
    if current != f's "{name}"':
        raise RuntimeError(f'profile readback {current!r}, wanted {name}')


def user_can_open(path):
    code = f'import os,sys\ntry:\n os.close(os.open({str(path)!r}, os.O_RDWR))\nexcept OSError as e:\n sys.exit(e.errno)'
    return subprocess.run(['runuser', '-u', 'keefeere', '--', 'python3', '-c', code]).returncode


LLAMA_TRANSIENT = 'egpu-llama-role-test'
LLAMA_USER = 'keefeere'
LLAMA_PORT = 9931
ENV_KEEP = ('HOME', 'USER', 'LOGNAME', 'PATH', 'LANG', 'LD_LIBRARY_PATH', 'HF_HOME', 'XDG_CACHE_HOME')
ENV_PREFIXES = ('CUDA', 'GGML_', 'LLAMA_', 'NVIDIA_')


def user_systemctl(*args, check=True):
    command = ['runuser', '-u', LLAMA_USER, '--', 'env', 'XDG_RUNTIME_DIR=/run/user/1000',
               'DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus',
               'systemctl', '--user', *args]
    result = subprocess.run(command, capture_output=True, text=True, timeout=120)
    if check and result.returncode:
        raise RuntimeError(f'systemctl --user {args}: {result.stderr.strip()}')
    return result.stdout.strip()


def llama_snapshot(proc=Path('/proc'), run_user=user_systemctl):
    """Exact argv/cwd/whitelisted env of the running user llama service."""
    pid = run_user('show', 'llama.service', '-p', 'MainPID', '--value')
    if not pid.isdigit() or int(pid) <= 1:
        raise RuntimeError('llama.service must be running')
    base_dir = proc / pid
    argv = [a.decode() for a in (base_dir / 'cmdline').read_bytes().split(b'\0')[:-1]]
    env = {}
    for item in (base_dir / 'environ').read_bytes().split(b'\0'):
        key, _, value = item.decode(errors='replace').partition('=')
        if key in ENV_KEEP or key.startswith(ENV_PREFIXES):
            env[key] = value
    group = run_user('show', 'llama.service', '-p', 'ControlGroup', '--value')
    if not group.startswith('/') or '..' in group.split('/'):
        raise RuntimeError('unexpected llama control group')
    return {'exe': os.path.realpath(os.readlink(base_dir / 'exe')), 'argv': argv,
            'cwd': os.readlink(base_dir / 'cwd'), 'env': env,
            'cgroup': '/sys/fs/cgroup' + group}


def llama_run_command(snapshot, ctl):
    """systemd-run line: a SYSTEM unit as the user, enrolled by a root ExecStartPre BEFORE exec."""
    enroll = (f'+/usr/bin/python3 {ctl} enroll 0 {LLAMA_TRANSIENT}.service --exe {snapshot["exe"]}')
    # init_t may not execute a binary labelled gconf_home_t (seen on the host: 203/EXEC,
    # AVC execute denied), so a system unit execs /usr/bin/setpriv, which drops to the
    # user and then execs llama. The exec hook still sees the user's uid and llama's inode.
    command = ['systemd-run', '--quiet', '--unit=' + LLAMA_TRANSIENT,
               '--property=Type=exec', '--property=Restart=no',
               '--property=WorkingDirectory=' + snapshot['cwd'],
               '--property=ExecStartPre=' + enroll]
    command += [f'--setenv={key}={value}' for key, value in sorted(snapshot['env'].items())]
    return command + ['--', '/usr/bin/setpriv', f'--reuid={LLAMA_USER}', f'--regid={LLAMA_USER}',
                      '--init-groups', '--', snapshot['exe'], *snapshot['argv'][1:]]


def nvidia_fds(unit, root=Path('/sys/fs/cgroup/system.slice')):
    """NVIDIA device nodes held open by any process of the unit's cgroup."""
    found = set()
    for pid in (root / (unit + '.service') / 'cgroup.procs').read_text().split():
        try:
            for fd in (Path('/proc') / pid / 'fd').iterdir():
                target = os.readlink(fd)
                if target.startswith('/dev/nvidia'):
                    found.add(target)
        except OSError:
            continue
    return found


def wait_port(port, seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        with socket.socket() as probe:
            probe.settimeout(1)
            if probe.connect_ex(('127.0.0.1', port)) == 0:
                return
        time.sleep(1)
    raise RuntimeError(f'llama did not listen on {port}')


def llama_flow(before):
    """Real compute role: llama restarts as a system unit, enrolled before exec,
    and must still use NVIDIA under work-nvidia while a plain process is refused."""
    user_systemctl('stop', 'llama.service')
    (ROOT / 'llama-stopped').touch()
    apply_profile('work-nvidia')
    snapshot = json.loads((ROOT / 'llama.json').read_text())
    subprocess.run(['systemctl', 'reset-failed', LLAMA_TRANSIENT + '.service'], capture_output=True)
    base.run(llama_run_command(snapshot, ROOT / 'helpers/egpu-service-roles-ctl.py'))
    wait_port(LLAMA_PORT, 120)
    held = nvidia_fds(LLAMA_TRANSIENT)
    if not held:
        raise RuntimeError('llama runs but holds no NVIDIA device: role not admitted')
    denied = user_can_open('/dev/nvidia0')
    if denied == 0:
        raise RuntimeError('work-nvidia admitted a plain non-role process')
    print(f'LLAMA ROLE OK: llama holds {sorted(held)} under work-nvidia; plain process refused '
          f'(errno {denied}).', flush=True)
    apply_profile('gaming-nvidia')
    base.run(['systemctl', 'stop', LLAMA_TRANSIENT + '.service'], timeout=100)
    user_systemctl('start', 'llama.service')


def require_runtime():
    if (Path(__file__).resolve() != SCRIPT or ROOT.is_symlink()
            or ROOT.stat().st_uid != 0 or ROOT.stat().st_mode & 0o077):
        raise RuntimeError('Internal actions require the private root-owned staged controller')


def remove_pins():
    if PINS.is_symlink():
        raise RuntimeError('Refusing symlinked pin directory')
    if not PINS.exists():
        return
    for name in PIN_NAMES:
        (PINS / name).unlink(missing_ok=True)
    if any(PINS.iterdir()):
        raise RuntimeError('Unknown entries in the owner pin directory; not removing')
    PINS.rmdir()


def restore():
    require_runtime()
    with (ROOT / 'restore.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if (ROOT / 'restored').exists():
            return
        before = json.loads((ROOT / 'before.json').read_text())
        llama_mode = (ROOT / 'llama-stopped').exists()
        if llama_mode:
            subprocess.run(['systemctl', 'stop', LLAMA_TRANSIENT + '.service'], timeout=100)
            subprocess.run(['systemctl', 'reset-failed', LLAMA_TRANSIENT + '.service'], capture_output=True)
            if user_systemctl('show', 'llama.service', '-p', 'ActiveState', '--value') != 'active':
                user_systemctl('start', 'llama.service')
        if (ROOT / 'activation-started').exists():
            # Stop first: the daemon holds no references after exit, then drop the
            # preserved FD store and the pins so the hooks are released.
            base.run(['systemctl', 'stop', 'cardwired.service'], timeout=60)
            base.run(['systemctl', 'clean', '--what=fdstore', 'cardwired.service'])
            remove_pins()
        base.checked_remove(DROPIN, override_text().encode())
        if CONFIG.is_symlink():
            raise RuntimeError(f'Refusing symlink: {CONFIG}')
        if CONFIG.exists():
            if CONFIG.read_text() != (ROOT / 'service-roles.toml').read_text():
                raise RuntimeError('Refusing changed service-roles.toml')
            CONFIG.unlink()
        if (ROOT / 'activation-started').exists():
            for path in base.FILES:
                if base.digest(path) != before['files'][str(path)]:
                    raise RuntimeError(f'Original configuration changed: {path}')
            if base.digest(base.OLD_BINARY) != base.OLD_SHA:
                raise RuntimeError('Recovery binary changed')
            base.run(['systemctl', 'daemon-reload'])
            base.run(['systemctl', 'start', 'cardwired.service'], timeout=60)
        base.baseline_matches(before, include_clients=not llama_mode)
        if llama_mode:
            # llama legitimately has a new identity; KWin must be untouched.
            kwin = 'plasma-kwin_wayland.service'
            if base.clients()[kwin] != before['clients'][kwin]:
                raise RuntimeError('KWin identity changed during test')
        if PINS.exists():
            raise RuntimeError('Guard pins survived rollback')
        (ROOT / 'restored').touch()
        print('RESTORED: secure 6213983 + original Hybrid; guard pins and FD store removed; '
              'KWin and llama unchanged.', flush=True)


def execute(deny_probe, llama=False):
    require_runtime()
    before = json.loads((ROOT / 'before.json').read_text())
    base.baseline_matches(before)
    if base.digest(ROOT / 'cardwired') != DAEMON_SHA or base.digest(ROOT / 'service_guard.bpf.o') != OBJECT_SHA:
        raise RuntimeError('Staged candidate changed')
    with open('/run/egpu-nvidia-transition.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        (ROOT / 'activation-started').touch()
        CONFIG.write_text((ROOT / 'service-roles.toml').read_text())
        os.chmod(CONFIG, 0o600)
        DROPIN.parent.mkdir(parents=True, exist_ok=True)
        with DROPIN.open('x') as stream:
            stream.write(override_text())
        base.run(['systemctl', 'daemon-reload'])
        base.run(['systemctl', 'restart', 'cardwired.service'], timeout=60)
        # Guard starts deny-all; the Gaming profile restores ordinary access at once.
        apply_profile('gaming-nvidia')
        if base.daemon_digest() != DAEMON_SHA or base.policy() != before['policy']:
            raise RuntimeError('Candidate activation did not preserve Hybrid')
        base.run(['nvidia-smi', '-L'], timeout=20)
        for node in ('/dev/nvidiactl', '/dev/nvidia0'):
            if user_can_open(node):
                raise RuntimeError(f'Gaming profile: user cannot open {node}')
        if base.clients() != before['clients']:
            raise RuntimeError('KWin or llama identity changed')
        print('GAMING PROFILE OK: guard active, user opens NVIDIA nodes, session unchanged.', flush=True)
        if llama:
            llama_flow(before)
            return
        if deny_probe:
            apply_profile('work-nvidia')
            denied = user_can_open('/dev/nvidia0')
            apply_profile('gaming-nvidia')  # always revert before judging
            if denied == 0:
                raise RuntimeError('work-nvidia did not deny a new non-role process')
            print(f'WORK PROFILE DENY OK (errno {denied}); reverted to gaming.', flush=True)
        time.sleep(5)  # short soak with the guard attached


def start(deny_probe=False, llama=False):
    for path in (ROOT, DROPIN, CONFIG, PINS):
        if path.exists() or path.is_symlink():
            raise RuntimeError(f'Existing state must be inspected, not overwritten: {path}')
    if base.daemon_digest() != base.OLD_SHA or base.digest(base.OLD_BINARY) != base.OLD_SHA:
        raise RuntimeError('Expected running secure 6213983 baseline')
    if base.digest(CANDIDATE_DIR / 'bin/cardwired') != DAEMON_SHA \
            or base.digest(CANDIDATE_DIR / 'service_guard.bpf.o') != OBJECT_SHA:
        raise RuntimeError('Candidate differs from VM-tested artifacts')
    before = {'policy': base.policy(), 'files': {str(p): base.digest(p) for p in base.FILES},
              'clients': base.clients()}
    if before['policy'] != {'mode': 1, 'config': {key: False for key in base.CONFIG_KEYS}}:
        raise RuntimeError('Expected ordinary Hybrid baseline with automatic switching off')
    base.run(['nvidia-smi', '-L'], timeout=15)
    planner = load(locate('egpu-desktop-profile-plan.py'), 'planner')
    identity = planner.read_hardware_config(HARDWARE)
    nodes = generator.inventory(identity['EGPU_VENDOR'], identity['EGPU_DEVICE'])
    snapshot = llama_snapshot() if llama else None
    ROOT.mkdir(mode=0o700)
    (ROOT / 'before.json').write_text(json.dumps(before, indent=2))
    if llama:
        (ROOT / 'llama.json').write_text(json.dumps(snapshot, indent=2))
        roles = [generator.parse_role(f'compute:{snapshot["exe"]}:{snapshot["cgroup"]}:1000')]
        (ROOT / 'service-roles.toml').write_text(generator.render(
            nodes, roles, str(ROOT / 'service_guard.bpf.o'), initial_profile='gaming-nvidia'))
    else:
        (ROOT / 'service-roles.toml').write_text(config_text(nodes))
    shutil.copy2(__file__, SCRIPT)
    shutil.copy2(CANDIDATE_DIR / 'bin/cardwired', ROOT / 'cardwired')
    os.chmod(ROOT / 'cardwired', 0o755)
    base.run(['chcon', '--reference=/usr/bin/cardwired', str(ROOT / 'cardwired')])
    shutil.copy2(CANDIDATE_DIR / 'service_guard.bpf.o', ROOT / 'service_guard.bpf.o')
    if base.digest(ROOT / 'cardwired') != DAEMON_SHA or base.digest(ROOT / 'service_guard.bpf.o') != OBJECT_SHA:
        raise RuntimeError('Copied candidate mismatch')
    if 'not found' in base.run(['ldd', str(ROOT / 'cardwired')]):
        raise RuntimeError('Missing native dependencies')
    helpers = ROOT / 'helpers'
    helpers.mkdir()
    source = HERE.parent
    for relative in ('diagnostics/test-cardwire-exact-candidate.py', 'egpu-service-roles-config.py',
                     'egpu-desktop-profile-plan.py', 'egpu-service-roles-ctl.py'):
        shutil.copy2(source / relative, helpers / Path(relative).name)
    flag = (['--execute-llama-role'] if llama else
            ['--execute-deny-probe'] if deny_probe else ['--execute'])
    lifetime = '330s' if llama else '150s'
    base.run(['systemd-run', '--quiet', '--unit=' + UNIT, '--property=Type=exec',
              '--property=RuntimeMaxSec=' + lifetime, '--property=TimeoutStopSec=90s',
              '--property=KillMode=control-group',
              '--property=ExecStopPost=/usr/bin/python3 ' + str(SCRIPT) + ' --restore',
              '/usr/bin/python3', str(SCRIPT), *flag])
    print('STARTED: bounded service-roles trial; systemd restores the baseline on exit.', flush=True)


def archive_restored():
    """Move a terminal, fully restored trial aside so a new --start may run.

    Never starts/restarts Cardwire or changes GPU policy; it only verifies that
    the baseline is intact and renames the root-owned trial directory.
    """
    import tempfile
    with open('/run/egpu-nvidia-transition.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        base.owned_trial_directory(ROOT)
        marker = ROOT / 'restored'
        if marker.is_symlink() or not marker.is_file() or marker.stat().st_uid != 0:
            raise RuntimeError('Successful rollback is not recorded')
        for path in (DROPIN, CONFIG, PINS):
            if path.exists() or path.is_symlink():
                raise RuntimeError(f'State still exists: {path}')
        base.quiescent_unit(UNIT + '.service')
        base.baseline_matches(json.loads((ROOT / 'before.json').read_text()))
        destination = Path(tempfile.mkdtemp(prefix=ROOT.name + '-archive-', dir=ROOT.parent)) / 'trial'
        ROOT.rename(destination)
        # A successful trial leaves no unit to reset; only a failed one does.
        if base.run(['systemctl', 'show', UNIT + '.service', '-p', 'LoadState', '--value']) != 'not-found':
            base.run(['systemctl', 'reset-failed', UNIT + '.service'])
        print(f'ARCHIVED: {destination}; no GPU policy or running service changed.', flush=True)


ACTIONS = {
    '--archive-restored': archive_restored,
    '--start': lambda: start(False),
    '--start-with-deny-probe': lambda: start(True),
    '--start-llama-role': lambda: start(llama=True),
    '--execute-llama-role': lambda: execute(False, True),
    '--execute': lambda: execute(False),
    '--execute-deny-probe': lambda: execute(True),
    '--restore': restore,
}


def main():
    if os.geteuid() != 0:
        raise RuntimeError('Requires administrator authentication')
    if len(sys.argv) != 2 or sys.argv[1] not in ACTIONS:
        raise RuntimeError('Explicit --start, --start-with-deny-probe or --archive-restored required')
    ACTIONS[sys.argv[1]]()


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(f'SERVICE-ROLES TEST FAILED: {error}', file=sys.stderr, flush=True)
        sys.exit(1)
