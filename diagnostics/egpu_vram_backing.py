#!/usr/bin/python3
"""Bounded VRAM backing-store diagnostic support. `probe` NEVER requests sleep."""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
import time
import uuid

GIB = 1024 ** 3
CAPACITY = 6 * GIB
MAX_USED_MIB = 4096
MIN_AVAILABLE = 10 * GIB
TMPFS = '/var/tmp:rw,size=6G,mode=1777,nosuid,nodev,huge=never,noswap'
# Retain the original filename for cleanup compatibility. The payload's exact
# stage, never the filename, determines what this one-shot authorizes.
MARKER = Path('/run/egpu-sleep-guard-freezer-once')
PLATFORM_MARKER = Path('/run/egpu-sleep-guard-platform-once')
BOOT_ID = Path('/proc/sys/kernel/random/boot_id')
PM_TEST = Path('/sys/power/pm_test')
PARAMS = Path('/proc/driver/nvidia/params')
PROCFS_PM_ACTIVE = Path('/run/egpu-nvidia-procfs-pm-boot')
PROCFS_PM_HOOK = Path('/etc/egpu-nvidia/egpu-nvidia-procfs-pm.py')
SLEEP_GUARD = Path('/etc/egpu-nvidia/egpu-sleep-guard.sh')
PROCFS_PM_MARKER_OWNER = 0
MEMINFO = Path('/proc/meminfo')
SERIAL_MODE = 'vram-tmpfs-serial'
RTC_MODE = 'vram-tmpfs-serial-rtc'
SERIAL_MODES = (SERIAL_MODE, RTC_MODE)
MODES = ('vram-current', 'vram-tmpfs', *SERIAL_MODES)
STAGES = ('freezer', 'devices', 'platform')
PM_ASYNC = Path('/sys/power/pm_async')
PM_TRACE = Path('/sys/power/pm_trace')


def check_device_policy(mode, *, preparing=False):
    if mode not in SERIAL_MODES:
        return {}
    expected = '1' if preparing else '0'
    expected_trace = '0' if preparing or mode == SERIAL_MODE else '1'
    if PM_ASYNC.read_text().strip() != expected or PM_TRACE.read_text().strip() != expected_trace:
        raise RuntimeError(f'Serial device-PM test requires pm_async={expected} '
                           f'and pm_trace={expected_trace} here.')
    return {'pm_async': expected, 'pm_trace': expected_trace}


def command(argv, timeout=10):
    result = subprocess.run(argv, text=True, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, timeout=timeout, check=False)
    if result.returncode:
        raise RuntimeError(f'{argv[0]} failed: {result.stdout.strip()}')
    return result.stdout.strip()


def check_budget(gpu_csv, meminfo):
    rows = [row.split(',') for row in gpu_csv.splitlines() if row.strip()]
    if len(rows) != 1 or len(rows[0]) != 3:
        raise RuntimeError('This diagnostic requires exactly one NVIDIA GPU.')
    version, used, total = (x.strip() for x in rows[0])
    if version != '615.71.09' or not used.isdigit() or not total.isdigit():
        raise RuntimeError('Only the inspected NVIDIA 615.71.09 driver is supported by this diagnostic.')
    used, total = int(used), int(total)
    if not 0 <= used <= MAX_USED_MIB or not max(used, 1) <= total <= 16384:
        raise RuntimeError('VRAM must be at most 4096 MiB used, on the tested <=16 GiB GPU.')
    match = re.search(r'^MemAvailable:\s+(\d+) kB$', meminfo, re.M)
    available = int(match[1]) * 1024 if match else 0
    if available < MIN_AVAILABLE:
        raise RuntimeError('At least 10 GiB MemAvailable is required; swap does not count.')
    return {'driver': version, 'vram_used_mib': used, 'vram_total_mib': total,
            'mem_available_bytes': available, 'tmpfs_limit_bytes': CAPACITY}


def check_pm_transport(params):
    notifier = re.findall(r'^UseKernelSuspendNotifiers:\s*([01])\s*$', params, re.M)
    if len(notifier) != 1:
        raise RuntimeError('NVIDIA notifier mode is missing or ambiguous.')
    active = PROCFS_PM_ACTIVE.exists() or PROCFS_PM_ACTIVE.is_symlink()
    if notifier[0] == '1':
        if active:
            raise RuntimeError('Procfs PM A/B marker exists with kernel notifiers enabled.')
        return 'kernel-notifier'
    if not active or PROCFS_PM_ACTIVE.is_symlink() or not PROCFS_PM_ACTIVE.is_file():
        raise RuntimeError('NVIDIA notifier disabled without a paired procfs PM boot marker.')
    status = PROCFS_PM_ACTIVE.stat()
    if (status.st_uid != PROCFS_PM_MARKER_OWNER or status.st_mode & 0o777 != 0o600
            or PROCFS_PM_ACTIVE.read_text().strip() != BOOT_ID.read_text().strip()):
        raise RuntimeError('NVIDIA procfs PM A/B boot marker is stale or unsafe.')
    if not PROCFS_PM_HOOK.is_file():
        raise RuntimeError('NVIDIA procfs PM hook is absent.')
    pre = command(['systemctl', 'show', 'systemd-suspend.service', '-p', 'ExecStartPre', '--value'])
    post = command(['systemctl', 'show', 'systemd-suspend.service', '-p', 'ExecStopPost', '--value'])
    guard_entry = f'argv[]=/usr/bin/bash {SLEEP_GUARD} systemd-suspend.service ; ignore_errors=no'
    pre_entry = f'argv[]=/usr/bin/python3 {PROCFS_PM_HOOK} pre ; ignore_errors=no'
    post_entry = f'argv[]=/usr/bin/python3 {PROCFS_PM_HOOK} post ; ignore_errors=no'
    guard_at = pre.find(guard_entry)
    hook_at = pre.find(pre_entry)
    if guard_at < 0 or hook_at <= guard_at or post_entry not in post:
        raise RuntimeError('NVIDIA procfs PM hook is absent, ignored or misordered.')
    return 'paired-procfs'


def check_host():
    params = PARAMS.read_text()
    pm_transport = check_pm_transport(params)
    for name, value in (('PreserveVideoMemoryAllocations', '1'),
                        ('TemporaryFilePath', '"/var/tmp"')):
        if not re.search(rf'^{name}:\s+{re.escape(value)}$', params, re.M):
            raise RuntimeError(f'Required live NVIDIA parameter: {name}={value}')
    if Path('/sys/module/thunderbolt/parameters/host_reset').read_text().strip() != 'Y':
        raise RuntimeError('Requires the normal host_reset=Y boot.')
    if any(token.split('=')[0] in ('thunderbolt.host_reset', 'egpu.host_reset_test',
                                   'egpu.host_reset_nodock')
           for token in Path('/proc/cmdline').read_text().split()):
        raise RuntimeError('Do not combine the backing-store test with a host-reset experiment.')
    gpu = command(['nvidia-smi', '--query-gpu=driver_version,memory.used,memory.total',
                   '--format=csv,noheader,nounits'], timeout=8)
    return {**check_budget(gpu, MEMINFO.read_text()), 'nvidia_pm_transport': pm_transport}


def mount_record(text):
    matches = []
    for line in text.splitlines():
        left, separator, right = line.partition(' - ')
        fields, trailing = left.split(), right.split()
        if separator and len(fields) >= 6 and len(trailing) >= 3 and fields[4] == '/var/tmp':
            matches.append({'fstype': trailing[0],
                            'options': set(fields[5].split(',')) | set(trailing[2].split(','))})
    if len(matches) != 1:
        raise RuntimeError('Expected exactly one private /var/tmp mount.')
    return matches[0]


def check_private_mount():
    if os.readlink('/proc/self/ns/mnt') == os.readlink('/proc/1/ns/mnt'):
        raise RuntimeError('Refusing to use the host mount namespace.')
    if os.stat('/var/tmp').st_dev == os.stat('/proc/1/root/var/tmp').st_dev:
        raise RuntimeError('The backing store is not isolated from the host /var/tmp.')
    record = mount_record(Path('/proc/self/mountinfo').read_text())
    # Linux omits huge=never from show_options because it is the tmpfs default.
    huge = {option for option in record['options'] if option.startswith('huge=')}
    if (record['fstype'] != 'tmpfs' or not {'rw', 'nosuid', 'nodev', 'noswap'} <= record['options']
            or huge - {'huge=never'}):
        raise RuntimeError('Private tmpfs options differ: ' + ','.join(sorted(record['options'])))
    usage = os.statvfs('/var/tmp')
    if usage.f_blocks * usage.f_frsize != CAPACITY or usage.f_bavail * usage.f_frsize < CAPACITY - GIB:
        raise RuntimeError('Private tmpfs capacity/free space differs from the experiment.')
    # A global force override can defeat huge=never; never change it here.
    shmem = Path('/sys/kernel/mm/transparent_hugepage/shmem_enabled').read_text()
    if '[force]' in shmem:
        raise RuntimeError('Global shmem huge pages are forced; refusing this experiment.')
    return {'filesystem': record['fstype'], 'options': sorted(record['options']),
            'capacity_bytes': CAPACITY, 'private_mount': True}


def check_current_mount():
    if os.stat('/var/tmp').st_dev != os.stat('/proc/1/root/var/tmp').st_dev:
        raise RuntimeError('Baseline must use the unchanged host /var/tmp filesystem.')
    fs = command(['findmnt', '-T', '/var/tmp', '-n', '-o', 'FSTYPE'])
    if fs != 'btrfs':
        raise RuntimeError('The baseline backing store is no longer the inspected Btrfs filesystem.')
    usage = os.statvfs('/var/tmp')
    if usage.f_bavail * usage.f_frsize < 18 * GIB:
        raise RuntimeError('Baseline /var/tmp has less than 18 GiB free.')
    return {'filesystem': fs, 'private_mount': False}


def check_service_policy(lab):
    # Do not override another tool's isolation policy.  NVIDIA's Fedora
    # packaging used to force SYSTEMD_SLEEP_FREEZE_USER_SESSIONS=false, but
    # 615.71.09-3 removed that legacy Xorg-oriented override for the open
    # kernel-module notifier path.  Accept either inspected packaging state;
    # reject explicit/duplicate values other than the old exact override.
    for prop in ('TemporaryFileSystem', 'BindPaths', 'BindReadOnlyPaths', 'RootDirectory', 'RootImage'):
        if lab.require_command(['systemctl', 'show', 'systemd-suspend.service', '-p', prop, '--value']).strip():
            raise RuntimeError(f'Existing sleep-service {prop} conflicts with this diagnostic.')
    private = lab.require_command(['systemctl', 'show', 'systemd-suspend.service', '-p', 'PrivateTmp', '--value']).strip()
    environment = lab.require_command(['systemctl', 'show', 'systemd-suspend.service', '-p', 'Environment', '--value'])
    freezer = [item for item in environment.split()
               if item.startswith('SYSTEMD_SLEEP_FREEZE_USER_SESSIONS=')]
    if private != 'no' or freezer not in ([], ['SYSTEMD_SLEEP_FREEZE_USER_SESSIONS=false']):
        raise RuntimeError('Unexpected sleep-service PrivateTmp or user-session freezer policy.')
    return ('systemd-default-freeze' if not freezer else 'legacy-explicit-no-freeze')


def arm(folder, mode, stage='freezer'):
    if (mode not in MODES or stage not in STAGES
            or (mode in SERIAL_MODES and stage != 'platform')
            or re.findall(r'\[([^]]+)\]', PM_TEST.read_text()) != [stage]):
        raise RuntimeError('Only an exact freezer/devices/platform backing-store experiment can be armed.')
    if PLATFORM_MARKER.exists() or PLATFORM_MARKER.is_symlink():
        raise RuntimeError('A platform exception already exists; refusing mixed tests.')
    policy = check_device_policy(mode)
    info = {**check_host(), **policy}
    payload = {'boot_id': BOOT_ID.read_text().strip(), 'stage': stage, 'mode': mode,
               'expires': time.monotonic() + 90}
    fd = os.open(MARKER, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(payload, stream)
        stream.flush()
        os.fsync(stream.fileno())
    command(['restorecon', '-F', str(MARKER)])
    command(['matchpathcon', '-V', str(MARKER)])
    (folder / 'sleep-guard-bypass.txt').write_text(json.dumps({**payload, **info}, indent=2))


def validate_marker(payload, *, boot_id, pm_test, now):
    if (not isinstance(payload, dict) or set(payload) != {'boot_id', 'stage', 'mode', 'expires'}
            or payload['boot_id'] != boot_id or payload['stage'] not in STAGES
            or payload['mode'] not in MODES
            or (payload['mode'] in SERIAL_MODES and payload['stage'] != 'platform')
            or re.findall(r'\[([^]]+)\]', pm_test) != [payload['stage']]
            or type(payload['expires']) not in (int, float)
            or not 0 < payload['expires'] - now <= 90):
        raise RuntimeError('Invalid, expired or mismatched backing-store exception.')


def consume():
    # Called only by the unit-scoped shell guard, under its flock. Never by
    # ordinary checks or by hibernation. Consume before validation/PM so a
    # failure cannot accidentally authorize a second invocation.
    fd = os.open(MARKER, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as stream:
        status = os.fstat(stream.fileno())
        if not stat.S_ISREG(status.st_mode) or status.st_uid != 0 or stat.S_IMODE(status.st_mode) != 0o600:
            raise RuntimeError('Unsafe backing-store marker ownership/type/mode.')
        MARKER.unlink()
        payload = json.loads(stream.read(4096))
    validate_marker(payload, boot_id=BOOT_ID.read_text().strip(),
                    pm_test=PM_TEST.read_text(), now=time.monotonic())
    if PLATFORM_MARKER.exists() or PLATFORM_MARKER.is_symlink():
        raise RuntimeError('Mixed backing-store/platform exceptions are forbidden.')
    policy = check_device_policy(payload['mode'])
    info = {**check_host(), **policy}
    info.update(check_current_mount() if payload['mode'] == 'vram-current' else check_private_mount())
    print(f"Consumed one {payload['stage']}-only backing-store exception: " + json.dumps(info), flush=True)


def probe_private():
    info = check_private_mount()
    # Only 1 MiB is actually allocated. Closing this unnamed file deletes it.
    fd = os.open('/var/tmp', os.O_TMPFILE | os.O_RDWR, 0o600)
    try:
        data = bytes(range(256)) * 4096
        os.posix_fallocate(fd, 0, len(data))
        if os.pwrite(fd, data, 0) != len(data):
            raise RuntimeError('Short temporary-file write.')
        os.fsync(fd)
        if os.pread(fd, len(data), 0) != data:
            raise RuntimeError('Temporary-file readback differs.')
        info['unnamed_file_roundtrip_bytes'] = len(data)
    finally:
        os.close(fd)
    print(json.dumps(info, indent=2))
    print('PROBE PASSED: no NVIDIA/PM action; no sleep requested.')


def probe():
    before = os.stat('/var/tmp')
    with tempfile.TemporaryDirectory(prefix='egpu-vram-probe-', dir='/run') as directory:
        worker = Path(directory) / 'probe.py'
        shutil.copyfile(Path(__file__).resolve(), worker)
        worker.chmod(0o600)
        unit = 'egpu-vram-probe-' + uuid.uuid4().hex
        try:
            output = command(['systemd-run', '--wait', '--pipe', '--collect', '--unit=' + unit,
                              '--property=Type=exec', '--property=RuntimeMaxSec=20s',
                              '--property=TimeoutStopSec=5s', '--property=Restart=no',
                              '--property=TemporaryFileSystem=' + TMPFS,
                              '/usr/bin/python3', str(worker), '_probe'], timeout=35)
        finally:
            after = os.stat('/var/tmp')
            if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
                raise RuntimeError('Host /var/tmp mount identity changed; inspect before proceeding.')
    print(output)
    print('Host /var/tmp identity unchanged; private unit and mount have ended.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('check', 'probe', '_probe', '_consume'))
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise RuntimeError('Run on the host with sudo.')
    if args.action == 'check':
        print(json.dumps(check_host(), indent=2))
        print('READ-ONLY capacity/driver check passed; not a sleep preflight.')
    elif args.action == 'probe':
        probe()
    elif args.action == '_probe':
        probe_private()
    else:
        consume()


if __name__ == '__main__':
    try:
        main()
    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
        raise SystemExit(f'VRAM backing-store diagnostic refused: {exc}')
