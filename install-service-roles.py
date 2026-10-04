#!/usr/bin/env python3
"""Persistent install of the service-roles Cardwire (reversible). Requires root.

--install     pinned daemon + guard object under /var/opt, drop-in 96 (rebinds /usr/bin/cardwired),
              /etc/cardwire/service-roles.toml (Gaming profile active from the first moment) and
              a reconcile service that keeps the llama (compute) and KWin (display) roles bound
              to their CURRENT cgroup and admits their running processes.
--uninstall   exact reverse; restores the secure 6213983 daemon and removes guard pins/FD store.
--status      what is installed and live.
Never switches GPU mode and never restarts KWin or llama. Any failed post-check uninstalls.
Role processes are enrolled AFTER they start (reconciler), so the default profile must stay
gaming-nvidia; Work profiles are applied explicitly (egpu-service-roles-ctl.py apply ...).
"""
import hashlib
import importlib.util
import json
import os
import pwd
from pathlib import Path
import shutil
import subprocess
import sys
import time

HERE = Path(__file__).resolve().parent
DIST = Path('/var/home/keefeere/_repos/_home/cardwire-stable-process-access/dist')
DAEMON_SHA = '9cc15a1d06a9db1e29fd6898e1e81821122853573d29da2b60c74021df0478b2'
OBJECT_SHA = 'e6b22a22cf515f510b830ce1aecab368dd71f23ece3d168ff0a0b996347f84d0'
CANDIDATE = DIST / f'local-service-roles-{DAEMON_SHA[:16]}'
RELEASE = Path('/var/opt/cardwire-fork/releases') / f'service-roles-{DAEMON_SHA[:16]}'
SHARE = Path('/var/opt/egpu-nvidia-service-roles')
DROPIN = Path('/etc/systemd/system/cardwired.service.d/96-service-roles.conf')
CONFIG = Path('/etc/cardwire/service-roles.toml')
UNIT_FILE = Path('/etc/systemd/system/egpu-service-roles-reconcile.service')
RECONCILE = 'egpu-service-roles-reconcile.service'
PROFILE_UNIT_SRC = HERE / 'egpu-service-roles-profile@.service'
PROFILE_UNIT = Path('/etc/systemd/system/egpu-service-roles-profile@.service')
POLKIT_SRC = HERE / '49-egpu-service-roles.rules.in'
POLKIT = Path('/etc/polkit-1/rules.d/49-egpu-service-roles.rules')
PLASMOID_SRC = HERE / 'plasmoid/com.keefeere.egpu'
PLASMOID_BACKUP_SUFFIX = '.pre-service-roles'
GENERATOR_SRC = HERE / 'egpu-kwin-order-generator.sh'
GENERATOR = Path('/etc/systemd/user-environment-generators/90-egpu-kwin-order')
PINS = Path('/sys/fs/bpf/cardwire-service-roles')
PIN_NAMES = ('exec_link', 'open_link', 'CW_ACTIVE', 'CW_DEVICES_MAP', 'CW_ROLE_TASKS')
HARDWARE = Path('/etc/egpu-nvidia/hardware.conf')
LLAMA_EXE = '/var/home/keefeere/.local/bin/llama'
KWIN_EXE = '/usr/bin/kwin_wayland'
USER_UID = 1000
BUS = 'org.opengamingcollective.cardwire'
OBJECT = '/org/opengamingcollective/cardwire'
OLD_SHA = '799918680da1f5e9c4458357e83d9b76f7d49f722eee52bd615d8f8e62b7bf4a'
OLD_BINARY = Path('/var/opt/cardwire-fork/releases/62139830260839c037c97e6a8dc607cc47559b90/bin/cardwired')


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run(args, timeout=60, check=True):
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if check and result.returncode:
        raise RuntimeError(f'{args[0]} ({result.returncode}): {result.stderr.strip()}')
    return result.stdout.strip()


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def daemon_digest():
    pid = int(run(['systemctl', 'show', 'cardwired.service', '-p', 'MainPID', '--value']))
    if pid <= 1:
        raise RuntimeError('Cardwire is not running')
    return digest(f'/proc/{pid}/exe')


def mode():
    return run(['busctl', '--system', 'get-property', BUS, OBJECT, BUS + '.Mode', 'Mode'])


def dropin_text():
    # The empty assignment resets 95-local-secure-policy.conf's bind list; ours follows.
    return ('[Service]\nBindReadOnlyPaths=\n'
            f'BindReadOnlyPaths={RELEASE}/bin/cardwired:/usr/bin/cardwired\n'
            'NotifyAccess=main\nFileDescriptorStoreMax=64\nFileDescriptorStorePreserve=yes\n'
            'ReadWritePaths=/sys/fs/bpf\n')


def role_args():
    return [f'0:user:llama.service:{LLAMA_EXE}:{USER_UID}',
            f'1:user:plasma-kwin_wayland.service:{KWIN_EXE}:{USER_UID}']


def unit_text(dependency='Wants'):
    # Wants, not Requires: Requires stops this unit whenever cardwired is stopped (seen on
    # the host: the egpu flow stops/starts cardwired) and nothing starts it again. The loop
    # already tolerates a missing or restarting daemon.
    roles = ' '.join(f'--role-unit {r}' for r in role_args())
    return ('[Unit]\nDescription=Keep Cardwire service roles bound to their current services\n'
            f'After=cardwired.service\n{dependency}=cardwired.service\n\n'
            '[Service]\nType=simple\n'
            f'ExecStart=/usr/bin/python3 {SHARE}/egpu-service-roles-ctl.py reconcile --loop '
            f'--interval 5 {roles}\nRestart=always\nRestartSec=3\n\n'
            '[Install]\nWantedBy=multi-user.target\n')


def config_text():
    generator = load(HERE / 'egpu-service-roles-config.py', 'generator')
    planner = load(HERE / 'egpu-desktop-profile-plan.py', 'planner')
    identity = planner.read_hardware_config(HARDWARE)
    nodes = generator.inventory(identity['EGPU_VENDOR'], identity['EGPU_DEVICE'])
    # Placeholder cgroups only; the reconciler binds the real service cgroups after start.
    # They must be distinct: shared references cannot be re-enrolled later.
    roles = [generator.parse_role(f'compute:{LLAMA_EXE}:/sys/fs/cgroup/system.slice:{USER_UID}'),
             generator.parse_role(f'display:{KWIN_EXE}:/sys/fs/cgroup/user.slice:{USER_UID}')]
    return generator.render(nodes, roles, str(RELEASE / 'service_guard.bpf.o'),
                            initial_profile='gaming-nvidia')


def desktop_user():
    planner = load(HERE / 'egpu-desktop-profile-plan.py', 'planner')
    return planner.read_hardware_config(HARDWARE)['DESKTOP_USER']


def polkit_text(user):
    return POLKIT_SRC.read_text().replace('@DESKTOP_USER@', user)


def plasmoid_dir(user):
    return Path(pwd.getpwnam(user).pw_dir) / '.local/share/plasma/plasmoids/com.keefeere.egpu'


OUR_MARKERS = ('egpu-service-roles-profile@', '"Version": "1.')


def is_ours(path):
    """A widget file written by an earlier run of this installer (any version of it)."""
    try:
        text = path.read_text()
    except OSError:
        return False
    return OUR_MARKERS[0] in text or (path.name == 'metadata.json' and 'com.keefeere.egpu' in text
                                      and '"Version": "1.' in text and path.with_name(path.name + PLASMOID_BACKUP_SUFFIX).exists())


def install_generator():
    """KWin GPU order follows the remembered profile at every login (see the script header)."""
    write_new(GENERATOR, GENERATOR_SRC.read_text(), 0o755)


def install_gui():
    """Profile switch in the Plasma widget: exact polkit-whitelisted units + widget update."""
    user = desktop_user()
    target = plasmoid_dir(user)
    qml, meta = target / 'contents/ui/main.qml', target / 'metadata.json'
    if not qml.is_file() or not meta.is_file():
        raise RuntimeError(f'eGPU widget is not installed at {target}')
    for path in (PROFILE_UNIT, POLKIT):
        if path.exists() or path.is_symlink():
            raise RuntimeError(f'GUI state already present: {path}')
    for name in (qml, meta):
        backup = Path(str(name) + PLASMOID_BACKUP_SUFFIX)
        if backup.is_symlink() or (backup.exists() and not is_ours(name)):
            raise RuntimeError(f'GUI state already present: {backup}')
    write_new(PROFILE_UNIT, PROFILE_UNIT_SRC.read_text(), 0o644)
    write_new(POLKIT, polkit_text(user), 0o644)
    info = pwd.getpwnam(user)
    for name, source in ((qml, PLASMOID_SRC / 'contents/ui/main.qml'), (meta, PLASMOID_SRC / 'metadata.json')):
        backup = Path(str(name) + PLASMOID_BACKUP_SUFFIX)
        if not backup.exists():  # first install: keep the user's original; later ones keep it
            shutil.copy2(name, backup)
        shutil.copy2(source, name)
        os.chown(name, info.pw_uid, info.pw_gid)
        os.chown(backup, info.pw_uid, info.pw_gid)
    run(['systemctl', 'daemon-reload'])


def remove_generator():
    checked_remove(GENERATOR, GENERATOR_SRC.read_text())


def remove_gui():
    try:
        user = desktop_user()
    except Exception:  # hardware.conf gone: leave the user's widget untouched
        user = None
    checked_remove(PROFILE_UNIT, PROFILE_UNIT_SRC.read_text())
    if user:
        checked_remove(POLKIT, polkit_text(user))
        target = plasmoid_dir(user)
        for name, source in ((target / 'contents/ui/main.qml', PLASMOID_SRC / 'contents/ui/main.qml'),
                             (target / 'metadata.json', PLASMOID_SRC / 'metadata.json')):
            backup = Path(str(name) + PLASMOID_BACKUP_SUFFIX)
            if backup.exists():
                if is_ours(name):
                    os.replace(backup, name)
                else:
                    print(f'Widget file changed since install; kept as is: {name} (backup: {backup})', file=sys.stderr)


def write_new(path, text, mode_bits):
    if path.exists() or path.is_symlink():
        raise RuntimeError(f'Existing state must be inspected, not overwritten: {path}')
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.new')
    temporary.write_text(text)
    os.chmod(temporary, mode_bits)
    os.replace(temporary, path)


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


def checked_remove(path, expected_text):
    if path.is_symlink():
        raise RuntimeError(f'Refusing symlink: {path}')
    if path.exists():
        accepted = expected_text if isinstance(expected_text, tuple) else (expected_text,)
        if path.read_text() not in accepted:
            raise RuntimeError(f'Refusing changed file: {path}')
        path.unlink()


def uninstall(expected_config=None):
    run(['systemctl', 'disable', '--now', RECONCILE], check=False)
    remove_gui()
    checked_remove(UNIT_FILE, (unit_text(), unit_text('Requires')))  # Requires: first release
    run(['systemctl', 'stop', 'cardwired.service'], timeout=90)
    run(['systemctl', 'clean', '--what=fdstore', 'cardwired.service'], check=False)
    remove_pins()
    checked_remove(DROPIN, dropin_text())
    if CONFIG.exists() and expected_config is not None:
        checked_remove(CONFIG, expected_config)
    elif CONFIG.exists():
        if 'initial_profile = "gaming-nvidia"' not in CONFIG.read_text():
            raise RuntimeError('Refusing to remove an unknown service-roles.toml')
        CONFIG.unlink()
    shutil.rmtree(SHARE, ignore_errors=True)
    run(['systemctl', 'daemon-reload'])
    run(['systemctl', 'start', 'cardwired.service'], timeout=90)
    if daemon_digest() != OLD_SHA or mode() != 'u 1':
        raise RuntimeError('Baseline not restored after uninstall')
    print('UNINSTALLED: secure 6213983 + Hybrid restored; guard pins and FD store removed.', flush=True)


def install():
    if os.geteuid() != 0:
        raise RuntimeError('Requires root')
    for path in (DROPIN, CONFIG, UNIT_FILE, PINS, SHARE):
        if path.exists() or path.is_symlink():
            raise RuntimeError(f'Already installed or foreign state: {path}')
    if daemon_digest() != OLD_SHA or digest(OLD_BINARY) != OLD_SHA or mode() != 'u 1':
        raise RuntimeError('Expected the running secure 6213983 baseline in Hybrid')
    if digest(CANDIDATE / 'bin/cardwired') != DAEMON_SHA or digest(CANDIDATE / 'service_guard.bpf.o') != OBJECT_SHA:
        raise RuntimeError('Candidate differs from the tested build')
    for exe in (LLAMA_EXE, KWIN_EXE):
        if not os.access(exe, os.X_OK):
            raise RuntimeError(f'Role executable missing: {exe}')
    config = config_text()
    run(['nvidia-smi', '-L'], timeout=20)
    # Stage everything that cannot affect the running daemon first.
    (RELEASE / 'bin').mkdir(parents=True, exist_ok=True)
    shutil.copy2(CANDIDATE / 'bin/cardwired', RELEASE / 'bin/cardwired')
    shutil.copy2(CANDIDATE / 'service_guard.bpf.o', RELEASE / 'service_guard.bpf.o')
    os.chmod(RELEASE / 'bin/cardwired', 0o755)
    run(['chcon', '--reference=/usr/bin/cardwired', str(RELEASE / 'bin/cardwired')], check=False)
    if digest(RELEASE / 'bin/cardwired') != DAEMON_SHA or digest(RELEASE / 'service_guard.bpf.o') != OBJECT_SHA:
        raise RuntimeError('Staged release mismatch')
    SHARE.mkdir(mode=0o755)
    shutil.copy2(HERE / 'egpu-service-roles-ctl.py', SHARE / 'egpu-service-roles-ctl.py')
    try:
        write_new(CONFIG, config, 0o600)
        write_new(DROPIN, dropin_text(), 0o644)
        write_new(UNIT_FILE, unit_text(), 0o644)
        run(['systemctl', 'daemon-reload'])
        run(['systemctl', 'restart', 'cardwired.service'], timeout=90)
        if daemon_digest() != DAEMON_SHA or mode() != 'u 1':
            raise RuntimeError('New daemon did not preserve Hybrid')
        profile = run(['busctl', '--system', 'get-property', BUS, OBJECT, BUS + '.ServiceRoles', 'CurrentProfile'])
        if profile != 's "gaming-nvidia"':
            raise RuntimeError(f'Guard not active with the Gaming profile: {profile!r}')
        run(['systemctl', 'enable', '--now', RECONCILE])
        deadline = time.monotonic() + 40
        while True:
            log = run(['journalctl', '-u', RECONCILE, '--no-pager', '-n', '20'], check=False)
            if 'admitted pid' in log and 'plasma-kwin_wayland' in log:
                break
            if time.monotonic() > deadline:
                raise RuntimeError(f'Reconciler did not enroll/admit KWin in time: {log[-400:]}')
            time.sleep(2)
        install_generator()
        try:
            install_gui()
            gui = 'Plasma widget profile switch installed (re-add/refresh the widget or log in again).'
        except Exception as error:
            remove_gui()
            gui = f'GUI NOT installed ({error}); core service-roles is active.'
        run(['nvidia-smi', '-L'], timeout=20)
        print(gui)
        print('INSTALLED: service-roles Cardwire active (Gaming profile), reconciler running.\n' + log[-600:], flush=True)
    except Exception:
        print('INSTALL FAILED: rolling back', file=sys.stderr, flush=True)
        try:
            uninstall(config)
        except Exception as error:  # surfaced, never hidden
            print(f'ROLLBACK ALSO FAILED: {error}', file=sys.stderr, flush=True)
        raise


def status():
    info = {name: path.exists() for name, path in (
        ('dropin', DROPIN), ('config', CONFIG), ('unit', UNIT_FILE), ('release', RELEASE), ('pins', PINS))}
    info['daemon_sha256'] = daemon_digest()
    info['reconcile_active'] = run(['systemctl', 'is-active', RECONCILE], check=False)
    print(json.dumps(info, indent=2))


ACTIONS = {'--install': install, '--uninstall': lambda: uninstall(), '--status': status}


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in ACTIONS:
        raise RuntimeError('Explicit --install, --uninstall or --status required')
    ACTIONS[sys.argv[1]]()


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(f'SERVICE-ROLES INSTALL TOOL FAILED: {error}', file=sys.stderr, flush=True)
        sys.exit(1)
