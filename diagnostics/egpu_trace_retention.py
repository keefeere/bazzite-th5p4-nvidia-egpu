#!/usr/bin/python3
"""Opt-in, NO-SLEEP marker-retention experiment for the inspected Atomic host.

Never reboots, requests PM, installs services, resets GPUs or clears trace data.
Only stage/cancel edit next-boot arguments; only mark writes its boot instance.
"""
import argparse
from collections import Counter
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shlex
import stat
import subprocess
import tempfile
import uuid


TESTED_KERNEL = '7.2.4-ogc3.1.fc44.x86_64'
NAME = 'egpu_pm_retention'
LABEL = 'egpu_pmtrace'
SIZE = 16 * 1024 * 1024
ALIGN = 32 * 1024 * 1024
ARGS = (f'reserve_mem=16M:0x2000000:{LABEL}',
        f'trace_instance={NAME}^traceoff@{LABEL}')
ROOT = Path('/var/lib/egpu-trace-retention')
TRACEFS = Path('/sys/kernel/tracing')
CMDLINE = Path('/proc/cmdline')
BOOT_ID = Path('/proc/sys/kernel/random/boot_id')
PM_TEST = Path('/sys/power/pm_test')
RUNTIME = Path('/run')
GUARD = Path('/etc/systemd/system/systemd-suspend.service.d/90-egpu-sleep-guard.conf')
MAX_TRACE_TEXT = 4 * 1024 * 1024  # Marker-only, not a general trace export.
SLEEP_UNITS = ('systemd-suspend.service', 'systemd-hibernate.service',
               'systemd-hybrid-sleep.service', 'systemd-suspend-then-hibernate.service',
               'sleep.target', 'egpu-sleep-lab-*')


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def command(argv):
    result = subprocess.run(argv, text=True, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, timeout=300, check=False)
    require(result.returncode == 0,
            f'{argv[0]} failed: {result.stderr.strip() or result.stdout.strip()}')
    return result.stdout


def tokens(text):
    return shlex.split(text)


def comparable(args):
    # OSTree updates this boot path when it creates a deployment. BOOT_IMAGE
    # and initrd are bootloader-added metadata, not modified by this script.
    return Counter(a for a in args if not a.startswith(('ostree=', 'BOOT_IMAGE=', 'initrd=')))


def without_owned(args):
    return [a for a in args if a not in ARGS]


def reject_conflicts(args, owned=False):
    for arg in args:
        if owned and arg in ARGS:
            continue
        key = arg.split('=', 1)[0]
        require(not (key in ('reserve_mem', 'memmap', 'nokaslr', 'bootconfig', 'thunderbolt.host_reset')
                     or key.startswith(('ftrace', 'trace', 'tp_printk', 'egpu.'))),
                f'Existing tracing/memory/diagnostic argument: {arg}; not replacing it.')
    if owned:
        require(all(args.count(a) == 1 for a in ARGS),
                'Both exact owned arguments must occur once; refusing partial/changed setup.')


def deployment_state():
    state = json.loads(command(['rpm-ostree', 'status', '--json']))
    require(state.get('transaction') is None, 'An rpm-ostree transaction is active.')
    deployments = state.get('deployments', [])
    require(deployments and sum(d.get('booted') is True for d in deployments) == 1,
            'Cannot resolve the booted OSTree deployment.')
    return deployments


def quiet_deployment():
    deployments = deployment_state()
    require(deployments[0].get('booted') is True
            and not any(d.get('staged') for d in deployments),
            'Pending/default-unbooted deployment exists; resolve it before this experiment.')
    return deployments[0]


def kernel_identity():
    release = platform.release()
    require(release == TESTED_KERNEL,
            f'Only {TESTED_KERNEL} was inspected; no change is allowed on {release}.')
    image = Path('/usr/lib/modules') / release / 'vmlinuz'
    with image.open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    return {'release': release, 'image_sha256': digest}


def idle():
    require('[none]' in PM_TEST.read_text().split(), 'pm_test is not none; stop and inspect.')
    require(GUARD.is_file() and 'ExecStartPre=/usr/bin/bash /etc/egpu-nvidia/egpu-sleep-guard.sh %n'
            in GUARD.read_text(), 'The installed sleep guard is missing or differs.')
    effective_guard = command(['systemctl', 'show', 'systemd-suspend.service',
                               '-p', 'ExecStartPre', '--value'])
    require('/etc/egpu-nvidia/egpu-sleep-guard.sh' in effective_guard
            and 'ignore_errors=no' in effective_guard,
            'Sleep guard is not present in the effective suspend service.')
    for name in ('egpu-sleep-guard-platform-once', 'egpu-sleep-guard-freezer-once'):
        path = RUNTIME / name
        require(not path.exists() and not path.is_symlink(), 'A PM bypass is armed; stop and inspect.')
    for name in ('systemd/sleep.conf.d/zzzz-egpu-sleep-lab.conf',
                 'systemd/system/systemd-suspend.service.d/zzzz-egpu-sleep-lab.conf'):
        path = RUNTIME / name
        require(not path.exists() and not path.is_symlink(), 'Temporary sleep-lab configuration remains.')
    active = command(['systemctl', 'list-units', '--plain', '--no-legend', '--no-pager',
                      '--state=active,activating,deactivating,reloading', *SLEEP_UNITS])
    require(not active.strip(), 'A sleep/lab unit is active; no retention action allowed.')
    jobs = command(['systemctl', 'list-jobs', '--no-legend', '--no-pager'])
    require(not re.search(r'\b(?:systemd-(?:suspend|hibernate|hybrid-sleep)|sleep\.target|egpu-sleep-lab-)', jobs),
            'A sleep/lab job is queued; no retention action allowed.')


def fresh_plan():
    require(not ROOT.exists() and not ROOT.is_symlink(),
            f'{ROOT} already exists; use status/cancel, never overwrite experiment history.')
    idle()
    deployment = quiet_deployment()
    args = tokens(command(['rpm-ostree', 'kargs']))
    reject_conflicts(args)
    live_args = tokens(CMDLINE.read_text())
    reject_conflicts(live_args)
    require(comparable(live_args) == comparable(args),
            'Live and next-boot arguments differ; resolve the existing change first.')
    return {'schema': 1, 'phase': 'staging', 'owned_args': list(ARGS),
            'kernel': kernel_identity(), 'stage_boot': BOOT_ID.read_text().strip(),
            'base_args': args, 'baseline_deployment': deployment['checksum'],
            'baseline_base_checksum': deployment.get('base-checksum', deployment['checksum'])}


def secure(path, directory=False):
    st = path.lstat()
    require(not stat.S_ISLNK(st.st_mode) and st.st_uid == os.geteuid()
            and not st.st_mode & 0o077
            and (stat.S_ISDIR(st.st_mode) if directory else stat.S_ISREG(st.st_mode)),
            f'Unsafe diagnostic state path: {path}')


@contextlib.contextmanager
def locked():
    secure(ROOT, directory=True)
    fd = os.open(ROOT / 'lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        secure(ROOT / 'lock')
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def save_state(state):
    secure(ROOT, directory=True)
    fd, name = tempfile.mkstemp(prefix='.state-', dir=ROOT)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(state, stream, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, ROOT / 'state.json')
        directory = os.open(ROOT, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)  # Only our exact mkstemp output, never history/trace.


def load_state():
    secure(ROOT, directory=True)
    secure(ROOT / 'state.json')
    state = json.loads((ROOT / 'state.json').read_text())
    require(state.get('schema') == 1 and state.get('owned_args') == list(ARGS),
            'Unknown state schema/argument ownership; refusing mutation.')
    return state


def archive(name, data):
    # Callers supply fixed filenames, never user input. Preserve existing files.
    with open(ROOT / name, 'x', opener=lambda p, flags: os.open(p, flags | os.O_NOFOLLOW, 0o600)) as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def stage():
    state = fresh_plan()
    ROOT.mkdir(mode=0o700)  # Exclusive: a second actor cannot adopt this run.
    with locked():
        save_state(state)  # Ownership must survive an interrupted kargs command.
        require(quiet_deployment()['checksum'] == state['baseline_deployment']
                and tokens(command(['rpm-ostree', 'kargs'])) == state['base_args'],
                'Deployment/arguments changed during preparation; nothing will be staged.')
        print('Staging the two retention arguments; no sleep/reboot will be requested...', flush=True)
        command(['rpm-ostree', 'kargs', *('--append-if-missing=' + a for a in ARGS)])
        after = tokens(command(['rpm-ostree', 'kargs']))
        reject_conflicts(after, owned=True)
        require(comparable(without_owned(after)) == comparable(state['base_args']),
                'Unexpected unrelated argument change; inspect before reboot.')
        deployments = deployment_state()
        target = deployments[0]
        require(target.get('base-checksum', target['checksum']) == state['baseline_base_checksum'],
                'OS base changed while staging; inspect before reboot.')
        state.update(phase='staged', staged_checksum=target['checksum'])
        save_state(state)
    print('Only the two retention arguments are staged. NO SLEEP or reboot requested.')
    print('This is NOT one-shot. Keep the same kernel and perform an ordinary reboot when ready.')
    print('After that boot run mark. If boot fails, choose the previous deployment; use cancel.')


def validate_live(state):
    idle()
    require(kernel_identity() == state['kernel'], 'Kernel image changed; retention comparison is invalid.')
    deployment = quiet_deployment()
    require(deployment['checksum'] == state.get('staged_checksum'),
            'Not booted into the exact staged deployment; inspect instead of writing a marker.')
    for args in (tokens(CMDLINE.read_text()), tokens(command(['rpm-ostree', 'kargs']))):
        reject_conflicts(args, owned=True)
        require(comparable(without_owned(args)) == comparable(state['base_args']),
                'Other boot arguments changed; not an isolated retention experiment.')


def mapping_from_log(log):
    pattern = (r'Tracing: mapped boot instance ' + re.escape(NAME)
               + r' at physical memory (0x[0-9a-fA-F]+) of size (0x[0-9a-fA-F]+)')
    matches = re.findall(pattern, log)
    require(len(matches) == 1, 'No unique kernel-confirmed reserved mapping; no trace writes allowed.')
    address, size = (int(part, 16) for part in matches[0])
    require(address > 0 and address % ALIGN == 0 and size == SIZE,
            'Unexpected reserved mapping address/alignment/size.')
    return {'address': hex(address), 'size': size}


def instance_state():
    require(command(['stat', '-f', '-c', '%T', str(TRACEFS)]).strip() == 'tracefs',
            'Expected tracefs mount is unavailable.')
    path = TRACEFS / 'instances' / NAME
    require(path.is_dir() and not path.is_symlink(), 'The exact boot-created instance is absent.')
    # Mapped boot buffers expose per-CPU buffer_meta; ordinary mkdir instances do not.
    metas = sorted(path.glob('per_cpu/cpu*/buffer_meta'))
    require(bool(metas), 'No mapped-buffer metadata; never substitute an ordinary trace instance.')
    expected = {'tracing_on': '0', 'current_tracer': 'nop', 'events/enable': '0',
                'options/markers': '1', 'options/trace_printk_dest': '0'}
    for name, value in expected.items():
        require((path / name).read_text().strip() == value,
                f'{NAME}/{name} is not {value}; refusing to reset or clear it.')
    for trigger in path.glob('events/*/*/trigger'):
        require(not any(line.strip() and not line.lstrip().startswith('#')
                        for line in trigger.read_text().splitlines()),
                'A trace event trigger is configured; not adopting a modified instance.')
    mapping = mapping_from_log(command(['journalctl', '-k', '-b', '--no-pager', '-o', 'cat']))
    return path, mapping


def read_trace_file(path):
    with path.open('rb') as stream:
        data = stream.read(MAX_TRACE_TEXT + 1)
    require(len(data) <= MAX_TRACE_TEXT, 'Unexpectedly large marker-only trace; preserve it and inspect.')
    return data.decode('utf-8', errors='replace')


def read_trace(path):
    # Non-consuming interface only. Never open trace_pipe or truncate trace.
    return read_trace_file(path / 'trace')


def marker_record(text, marker):
    # The symbol is a rendering of the saved callsite address, not the marker
    # payload. Across the observed reboot it became "kallsyms_offsets" while
    # the token, PID, CPU and timestamp survived unchanged. Do not whitelist
    # that incorrect label or infer that its named function actually ran.
    pattern = re.compile(
        r'[^\r\n]+?-(?P<pid>[0-9]+)[ \t]+\[(?P<cpu>[0-9]+)\]'
        r'[ \t]+\S+[ \t]+(?P<timestamp>[0-9]+\.[0-9]+):'
        r'[ \t]+(?P<symbol>[^:\r\n]+):[ \t]+(?P<payload>[^\r\n]+)')
    found = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith('#'):
            continue
        record = pattern.fullmatch(line)
        if record and marker and record['payload'].strip() == marker:
            found.append({'pid': int(record['pid']), 'cpu': int(record['cpu']),
                          'timestamp': record['timestamp'], 'symbol': record['symbol'].strip()})
    # One mark per experiment. Duplicates are ambiguous, not a retention pass.
    return found[0] if len(found) == 1 else None


def has_marker(text, marker):
    return marker_record(text, marker) is not None


def mark():
    with locked():
        state = load_state()
        require(state['phase'] == 'staged', 'Marker was already attempted or run cancelled; not overwriting.')
        validate_live(state)
        boot = BOOT_ID.read_text().strip()
        require(boot != state['stage_boot'], 'Reboot into the staged arguments before mark.')
        path, mapping = instance_state()
        before = read_trace(path)
        archive('before-marker-trace.txt', before)
        require(not any(line.strip() and not line.lstrip().startswith('#') for line in before.splitlines()),
                'Trace already has records; preserving them instead of clearing or mixing experiments.')
        marker = 'EGPU_RETENTION_' + uuid.uuid4().hex + '_' + boot
        state.update(phase='marking', marker=marker, marker_boot=boot, mapping=mapping)
        save_state(state)
        # Only these two controls of our verified boot-created instance change.
        # No events/function tracing are enabled, so this records one text marker.
        try:
            (path / 'tracing_on').write_text('1\n')
            (path / 'trace_marker').write_text(marker + '\n')
        finally:
            (path / 'tracing_on').write_text('0\n')
        after = read_trace(path)
        archive('written-marker-trace.txt', after)
        require(has_marker(after, marker), 'Marker was not confirmed; do not repeat mark or test PM.')
        state['phase'] = 'marked'
        save_state(state)
    print('Marker confirmed in RAM; recording is OFF. NO SLEEP requested.')
    print('Ordinary reboot into this same deployment, then verify. Do NOT cancel or update before verification.')


def verify():
    with locked():
        state = load_state()
        require(state['phase'] in ('marked', 'verified', 'not-retained'), 'No confirmed marker to compare.')
        boot = BOOT_ID.read_text().strip()
        require(boot != state['marker_boot'], 'Same boot: marker visibility is NOT cross-boot retention.')
        validate_live(state)
        path, mapping = instance_state()
        trace = read_trace(path)
        # Boot UUID is validated before using it as an archive filename.
        require(str(uuid.UUID(boot)) == boot, 'Unexpected boot ID format.')
        output = ROOT / ('recovered-' + boot + '.txt')
        if not output.exists():
            archive(output.name, trace)
        written_path = ROOT / 'written-marker-trace.txt'
        secure(written_path)
        original = marker_record(read_trace_file(written_path), state['marker'])
        require(original is not None, 'Original marker record is missing or ambiguous; preserve evidence.')
        recovered = marker_record(trace, state['marker'])
        same_record = recovered is not None and all(
            recovered[key] == original[key] for key in ('pid', 'cpu', 'timestamp'))
        retained = mapping == state['mapping'] and same_record
        verification = {
            'boot': boot, 'mapping': mapping, 'same_mapping': mapping == state['mapping'],
            'marker_present': recovered is not None, 'same_record': same_record,
            'original_symbol': original['symbol'],
            'recovered_symbol': recovered['symbol'] if recovered else None,
            'symbol_changed': recovered is not None and recovered['symbol'] != original['symbol'],
            'retained': retained}
        previous = state.get('verification')
        if previous is not None and previous != verification:
            state.setdefault('verification_history', []).append(previous)
        state.update(phase='verified' if retained else 'not-retained', verification=verification)
        save_state(state)
    print(json.dumps(state['verification'], indent=2))
    print('MARKER RETAINED across this reboot only.' if retained else 'RETENTION NOT CONFIRMED; do not run PM.')
    if state['verification']['symbol_changed']:
        print('Callsite symbol changed across boots; marker identity uses exact payload, PID, CPU and timestamp.')
    print('Not proof of suspend recovery or retention after a forced reset/power loss. NO SLEEP requested.')
    return 0 if retained else 1


def cancel():
    with locked():
        state = load_state()  # No ownership record => never remove even matching arguments.
        deployments = deployment_state()
        args = tokens(command(['rpm-ostree', 'kargs']))
        present = [arg for arg in ARGS if arg in args]
        if present:
            require(comparable(without_owned(args)) == comparable(state['base_args']),
                    'Other next-boot arguments changed; inspect before cancelling.')
            if not deployments[0].get('booted') or any(d.get('staged') for d in deployments):
                require(deployments[0]['checksum'] == state.get('staged_checksum'),
                        'Unrecognized pending deployment (possibly interrupted staging); '
                        'inspect status/kargs before manual recovery. No change made.')
            # Exact key=value only; never delete a whole key or someone else's experiment.
            command(['rpm-ostree', 'kargs', *('--delete-if-present=' + a for a in present)])
            after = tokens(command(['rpm-ostree', 'kargs']))
            require(not any(a in after for a in ARGS)
                    and comparable(after) == comparable(without_owned(args)),
                    'Cancellation post-check failed; inspect next-boot arguments.')
        state['phase'] = 'cancelled'
        save_state(state)
    print('Owned retention arguments are absent from the next-boot configuration.')
    print('Current RAM/trace and archived evidence are unchanged. Ordinary reboot releases the reservation.')
    print('No sleep, reboot, deployment finalization or trace deletion was requested.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('check', 'stage', 'mark', 'verify', 'cancel', 'status'))
    args = parser.parse_args()
    require(os.geteuid() == 0, 'Run with sudo; check/status are read-only. No action requests sleep.')
    if args.action == 'check':
        plan = fresh_plan()
        print(json.dumps({'kernel': plan['kernel'], 'planned_args': list(ARGS),
                          'reservation_mib': 16, 'alignment_mib': 32,
                          'warning': 'RAM survival is not guaranteed; no sleep test is authorized.'}, indent=2))
        print('READ-ONLY PREFLIGHT PASSED. No arguments, files, tracing or PM state changed.')
    elif args.action == 'status':
        print(json.dumps(load_state(), indent=2))
    else:
        return globals()[args.action]()
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (RuntimeError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
        print(f'ERROR: {exc}\nNO SLEEP or automatic reboot requested. Preserve state; inspect before retrying.')
        raise SystemExit(1)
