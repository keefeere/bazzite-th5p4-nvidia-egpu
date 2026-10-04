#!/usr/bin/python3
"""One NO-SLEEP read recording/export probe of the owned reserved instance.

check is read-only. run archives the verified old marker generation, replaces
ONLY that instance's records, captures harmless /dev/zero reads and consumes its
raw CPU buffers into private files. No PM, drivers, services or global trace
controls are changed. Offline decoding is a separate unprivileged action.
"""
import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import sys
import tempfile
import time
import uuid

import egpu_persistent_trace as metadata
import egpu_trace_retention as retention


FUNCTION = 'vfs_read'
EVENT = 'events/syscalls/sys_enter_read'
PAGE_SIZE = 4096
MAX_RAW = 20 * 1024 * 1024
CALLS = 8
CONTROL_FILES = ('set_ftrace_filter', 'set_ftrace_notrace', 'set_ftrace_pid',
                 'set_ftrace_notrace_pid', 'set_event_pid', 'set_event_notrace_pid')
GLOBAL_FILES = ('tracing_on', 'current_tracer', 'events/enable', 'trace_clock')


def text(path):
    return metadata.read(path).decode().strip()


def json_bytes(value):
    return (json.dumps(value, indent=2) + '\n').encode()


def inert_filter(value):
    return all(not line.strip() or line.strip() == 'no pid'
               or line.lstrip().startswith('#') for line in value.splitlines())


def global_state():
    return {name: text(retention.TRACEFS / name) for name in GLOBAL_FILES}


def cpu_stats(instance):
    result = {}
    for path in sorted((instance / 'per_cpu').glob('cpu*/stats')):
        cpu = int(path.parent.name[3:])
        data = metadata.read(path).decode()
        fields = {}
        for name in ('entries', 'overrun', 'commit overrun', 'dropped events', 'read events'):
            values = re.findall(r'^' + re.escape(name) + r':\s*(\d+)\s*$', data, re.M)
            retention.require(len(values) == 1, f'Missing/ambiguous CPU {cpu} statistic: {name}')
            fields[name] = int(values[0])
        result[cpu] = fields
    # trace-cmd restore interprets input position as CPU ID. Never collapse gaps.
    retention.require(result and sorted(result) == list(range(len(result))) and len(result) <= 256,
                      'CPU set is empty, non-contiguous or too large; review export mapping.')
    return result


def no_loss(stats):
    retention.require(all(not values[key] for values in stats.values()
                          for key in ('overrun', 'commit overrun', 'dropped events')),
                      'Trace loss detected; this is not a successful capture.')


def preflight():
    info, files = metadata.collect()  # Requires current-boot verified old marker.
    instance = retention.TRACEFS / 'instances' / retention.NAME
    retention.require(sys.byteorder == 'little' and os.sysconf('SC_PAGESIZE') == PAGE_SIZE,
                      'Only the inspected little-endian 4 KiB page layout is supported.')
    header = files['events/header_page'].decode()
    retention.require(re.search(r'field:\s*u64 timestamp;\s*offset:0;\s*size:8;', header)
                      and re.search(r'field:\s*local_t commit;\s*offset:8;\s*size:8;', header)
                      and re.search(r'field:\s*char data;\s*offset:16;\s*size:4080;', header),
                      'Unreviewed raw ring-page header.')
    available = metadata.read(retention.TRACEFS / 'available_filter_functions', metadata.MAX_METADATA)
    retention.require(FUNCTION in {line.split()[0] for line in available.decode().splitlines() if line.strip()},
                      f'{FUNCTION} is not traceable; do not substitute a PM function.')
    symbols = [line.split()[0] for line in files['kallsyms.txt'].decode().splitlines()
               if len(line.split()) >= 3 and line.split()[2] == FUNCTION]
    retention.require(len(symbols) == 1 and int(symbols[0], 16) != 0, 'No unique visible read symbol.')
    files['probe-function-target.txt'] = (FUNCTION + '\n').encode()
    retention.require('function' in text(instance / 'available_tracers').split(), 'Function tracer unavailable.')
    for name in CONTROL_FILES:
        retention.require(inert_filter(text(instance / name)), f'Existing {name}; not overwriting filters.')
    retention.require(text(instance / EVENT / 'filter') in ('none', '0'), 'Existing syscall event filter.')
    for name in ('function-fork', 'event-fork', 'func_stack_trace', 'stacktrace'):
        path = instance / 'options' / name
        if path.exists():
            retention.require(text(path) == '0', f'Unexpected tracing option: {name}')
    stats = cpu_stats(instance)
    no_loss(stats)
    retention.require(sum(v['entries'] for v in stats.values()) == 1
                      and all(v['read events'] == 0 for v in stats.values()),
                      'Expected only the original unread marker; preserve other records.')
    mask = int(text(instance / 'tracing_cpumask').replace(',', ''), 16)
    retention.require(mask == (1 << len(stats)) - 1, 'Non-default CPU trace mask; refusing to change it.')
    for cpu in stats:
        description = files[f'per_cpu/cpu{cpu}/buffer_meta'].decode()
        retention.require(re.findall(r'^subbuf_size:\s*(\d+)\s*$', description, re.M) == ['4096'],
                          'Only 4096-byte raw subbuffers were reviewed; not resizing.')
        raw = instance / f'per_cpu/cpu{cpu}/trace_pipe_raw'
        retention.require(raw.exists() and not raw.is_symlink(), 'Raw CPU interface unavailable.')
    description = metadata.read(retention.TRACEFS / EVENT / 'format')
    event_id = metadata.event_descriptor(description, 'sys_enter_read')
    retention.require(event_id not in info['event_ids'].values(), 'Duplicate syscall event ID.')
    info.update(probe_event_id=event_id, probe_function=FUNCTION)
    files[EVENT + '/format'] = description
    globals_before = global_state()
    retention.require(globals_before['current_tracer'] == 'nop'
                      and globals_before['events/enable'] == '0',
                      'Another global recorder is active; do not mix experiments.')
    retention.require(text(metadata.PROC_ROOT / 'sys/kernel/ftrace_enabled') == '1',
                      'Global ftrace is disabled; this probe will not enable it.')
    retention.secure(metadata.LOG_ROOT, directory=True)
    retention.require(shutil.disk_usage(metadata.LOG_ROOT).free >= 128 * 1024 * 1024,
                      'Need at least 128 MiB free for private archives and raw export.')
    return info, files, instance, globals_before


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def save_files(folder, files):
    """Caller owns a fresh private directory; all evidence is exclusive/fsynced."""
    for name, data in files.items():
        path = folder / name
        parent = folder
        for part in Path(name).parts[:-1]:
            parent /= part
            parent.mkdir(mode=0o700, exist_ok=True)
        metadata.write_private(path, data)
    for path in [p for p in folder.rglob('*') if p.is_dir()] + [folder, folder.parent]:
        sync_directory(path)


def seal_tree(folder, info, files):
    manifest = {**info, 'metadata_bytes': sum(map(len, files.values())), 'files': {
        name: {'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
        for name, data in files.items()}}
    metadata.write_private(folder / 'manifest.json', json_bytes(manifest))
    for path in [p for p in folder.rglob('*') if p.is_dir()] + [folder, folder.parent]:
        sync_directory(path)


def save_tree(folder, info, files):
    save_files(folder, files)
    seal_tree(folder, info, files)


def control(instance, name, value):
    # All names supplied by this module; no user-selected instance or control.
    (instance / name).write_text(value + '\n')


@contextlib.contextmanager
def deadline():
    """Bound normal recording and catch terminal interrupts; SIGKILL is not recoverable."""
    def interrupted(signum, _frame):
        raise RuntimeError(f'Probe interrupted by signal {signum}; no automatic retry.')
    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGALRM, signal.SIGTERM, signal.SIGINT)}
    retention.require(signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0), 'Existing process timer.')
    try:
        for sig in previous:
            signal.signal(sig, interrupted)
        signal.setitimer(signal.ITIMER_REAL, 10)
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def generate_calls(fd):
    # /dev/zero read passes through the VFS and an ordinary read syscall.
    for _ in range(CALLS):
        retention.require(os.read(fd, 32) == bytes(32), 'Unexpected /dev/zero read.')


def record(instance, pid, token):
    with deadline():
        fd = os.open('/dev/zero', os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        try:
            record_open_file(instance, pid, token, fd)
        finally:
            os.close(fd)


def record_open_file(instance, pid, token, fd):
    """The caller owns one open read-only character-device descriptor."""
    # Install narrow filters BEFORE enabling anything. Tracer/event setup may
    # clear the previous generation; durable archival precedes this function.
    control(instance, 'set_ftrace_filter', FUNCTION)
    control(instance, 'set_ftrace_pid', str(pid))
    control(instance, EVENT + '/filter', f'common_pid == {pid}')
    retention.require(text(instance / 'set_ftrace_filter').split() == [FUNCTION]
                      and text(instance / 'set_ftrace_pid') == str(pid)
                      and text(instance / EVENT / 'filter') == f'common_pid == {pid}',
                      'PID/function filter did not apply.')
    control(instance, 'current_tracer', 'function')
    control(instance, EVENT + '/enable', '1')
    control(instance, 'trace', '')  # Explicit, authorized replacement of this archived generation only.
    try:
        control(instance, 'tracing_on', '1')
        control(instance, 'trace_marker', token + '_BEGIN')
        generate_calls(fd)
        control(instance, 'trace_marker', token + '_END')
    finally:
        control(instance, 'tracing_on', '0')


def export_raw(instance, folder, cpus):
    """Consume only stopped owned CPU buffers. Preserve even partial output on failure."""
    retention.require(text(instance / 'tracing_on') == '0', 'Refusing to export a running recorder.')
    folder.mkdir(mode=0o700)
    total = 0
    stop_at = time.monotonic() + 10
    for cpu in cpus:
        source = instance / f'per_cpu/cpu{cpu}/trace_pipe_raw'
        # Open output first: permission/disk errors must not first consume RAM.
        with open(folder / f'cpu{cpu}', 'xb', opener=lambda p, f: os.open(p, f | os.O_NOFOLLOW, 0o600)) as out:
            fd = os.open(source, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
            try:
                while True:
                    retention.require(time.monotonic() < stop_at, 'Raw export exceeded time bound.')
                    try:
                        data = os.read(fd, PAGE_SIZE)
                    except BlockingIOError:
                        break
                    if not data:
                        break
                    out.write(data)
                    total += len(data)
                    retention.require(len(data) == PAGE_SIZE, 'Short raw page retained; no padding or decoding guess.')
                    retention.require(total <= MAX_RAW, 'Raw export exceeded byte bound; partial files retained.')
            finally:
                os.close(fd)
                out.flush()
                os.fsync(out.fileno())
    sync_directory(folder)
    retention.require(total > 0, 'No raw pages captured.')
    return total


def validate_text(trace, pid, token):
    for suffix in ('_BEGIN', '_END'):
        marker = retention.marker_record(trace, token + suffix)
        retention.require(marker is not None and marker['pid'] == pid, 'Probe marker absent/ambiguous or wrong PID.')
    lines = [line for line in trace.splitlines() if re.search(r'-' + str(pid) + r'\s+\[', line)]
    retention.require(any(FUNCTION in line for line in lines)
                      # Kernel trace_syscalls.c renders the live event as
                      # sys_read(...); trace-cmd normally labels its event name.
                      and any(re.search(r':\s*(?:sys_read\(|sys_enter_read:)', line) for line in lines),
                      'Actual syscall/function records missing; no capture success.')


def capture_files(instance, before_files, pid, token):
    # Metadata is collected before consuming raw pages and from the SAME boot.
    files = {name: data for name, data in before_files.items() if name.startswith('events/')}
    for name, path, limit in (
        ('kallsyms.txt', metadata.PROC_ROOT / 'kallsyms', metadata.MAX_METADATA),
        ('modules.txt', metadata.PROC_ROOT / 'modules', metadata.MAX_SMALL),
        ('printk_formats.txt', retention.TRACEFS / 'printk_formats', metadata.MAX_METADATA),
        ('trace.txt', instance / 'trace', retention.MAX_TRACE_TEXT),
        ('last_boot_info.txt', instance / 'last_boot_info', metadata.MAX_SMALL),
        ('trace_clock.txt', instance / 'trace_clock', metadata.MAX_SMALL),
    ):
        files[name] = metadata.read(path, limit)
    files['saved_cmdlines.txt'] = f'{pid} probe-read\n'.encode()
    retention.require(metadata.module_layout(files['modules.txt']) == metadata.module_layout(before_files['modules.txt']),
                      'Module layout changed; captured addresses cannot be paired with baseline metadata.')
    for name in files:
        if name.startswith('events/'):
            retention.require(metadata.read(retention.TRACEFS / name) == files[name], 'Trace format changed.')
    validate_text(files['trace.txt'].decode(), pid, token)
    return files


def run():
    with retention.locked():
        info, old_files, instance, globals_before = preflight()
        folder = Path(tempfile.mkdtemp(prefix='persistent-probe-', dir=metadata.LOG_ROOT))
        old = folder / 'before'
        old.mkdir(mode=0o700)
        save_tree(old, info, old_files)  # Complete old marker/metadata archive before ANY trace write.
        state = retention.load_state()
        retention.require(state == json.loads(old_files['retention-state.json']), 'State changed while archiving.')
        retention.validate_live(state)
        checked_instance, mapping = retention.instance_state()
        retention.require(checked_instance == instance and mapping == info['mapping']
                          and metadata.read(instance / 'trace', retention.MAX_TRACE_TEXT) == old_files['marker-recovered.txt']
                          and global_state() == globals_before, 'Trace changed while archiving; nothing reset.')
        pid = os.getpid()
        token = 'EGPU_CAPTURE_' + uuid.uuid4().hex
        state.update(phase='probe-preparing', probe={'boot': info['boot'], 'folder': str(folder), 'token': token, 'pid': pid})
        retention.save_state(state)  # No automatic reuse of the previous verification/marker.
        print(f'NO-SLEEP probe; previous generation archived at {old}', flush=True)
        error = None
        cleanup_errors = []
        exported = False
        try:
            record(instance, pid, token)
            stats_before = cpu_stats(instance)
            no_loss(stats_before)
            retention.require(all(v['read events'] == 0 for v in stats_before.values()), 'Another reader consumed records.')
            files = capture_files(instance, old_files, pid, token)
            capture = folder / 'capture'
            capture.mkdir(mode=0o700)
            # Save readable evidence BEFORE destructive raw reads, including when export fails.
            for name in ('trace.txt', 'last_boot_info.txt'):
                metadata.write_private(folder / name, files[name])
            save_files(capture, files)  # Fresh decoding metadata must survive even a partial raw export.
            export_raw(instance, capture / 'raw', sorted(stats_before))
            stats_after = cpu_stats(instance)
            no_loss(stats_after)
            retention.require(stats_before.keys() == stats_after.keys()
                              and all(v['entries'] == 0 for v in stats_after.values()), 'Raw export did not drain all CPUs.')
            retention.require(all(stats_after[cpu]['read events'] == values['entries']
                                  for cpu, values in stats_before.items()), 'Raw read-event accounting mismatch.')
            retention.validate_live(state)
            retention.require(retention.BOOT_ID.read_text().strip() == info['boot']
                              and global_state() == globals_before, 'Boot or global trace state changed.')
            retention.require(metadata.module_layout(metadata.read(metadata.PROC_ROOT / 'modules'))
                              == metadata.module_layout(files['modules.txt']), 'Modules moved during export.')
            stat_files = {f'stats/cpu{cpu}.json': json_bytes({'before': stats_before[cpu], 'after': stats_after[cpu]})
                          for cpu in sorted(stats_before)}
            save_files(capture, stat_files)
            files.update(stat_files)
            capture_info = {**info, 'kind': 'read-raw-probe', 'pid': pid, 'token': token,
                            'cpus': sorted(stats_before), 'raw_complete': True,
                            'warning': 'No-sleep capture only; offline decode and forced-reset retention are unproven.'}
            seal_tree(capture, capture_info, files)
            # Add raw file hashes without replacing the immutable metadata manifest.
            raw_hashes = {f'raw/cpu{cpu}': {'bytes': (capture / f'raw/cpu{cpu}').stat().st_size,
                          'sha256': hashlib.sha256(metadata.read(capture / f'raw/cpu{cpu}', MAX_RAW)).hexdigest()}
                          for cpu in sorted(stats_before)}
            metadata.write_private(capture / 'raw-manifest.json', json_bytes(raw_hashes))
            sync_directory(capture)
            exported = True
        except BaseException as exc:
            error = str(exc) or type(exc).__name__
        finally:
            # Never change current_tracer until evidence is durably exported:
            # changing it can reset records. On failure retain OFF + narrow filter.
            for name, value in (('tracing_on', '0'), (EVENT + '/enable', '0')):
                try:
                    control(instance, name, value)
                except BaseException as exc:
                    cleanup_errors.append(f'{name}: {exc}')
            if exported and not cleanup_errors:
                try:
                    control(instance, 'current_tracer', 'nop')
                    for name in ('set_ftrace_filter', 'set_ftrace_pid'):
                        control(instance, name, '')
                    control(instance, EVENT + '/filter', '0')
                    retention.instance_state()
                    retention.require(all(inert_filter(text(instance / name)) for name in CONTROL_FILES)
                                      and text(instance / EVENT / 'filter') in ('0', 'none'),
                                      'Probe filters were not cleared; inspect before other tracing.')
                    retention.require(global_state() == globals_before, 'Global tracing changed (not restored by probe).')
                except BaseException as exc:
                    cleanup_errors.append(str(exc))
        result = {'raw_export_complete': exported, 'error': error, 'cleanup_errors': cleanup_errors,
                  'offline_decode_validated': False, 'sleep_requested': False,
                  'warning': 'Inspect failures; do not repeat, reboot or request PM automatically.'}
        metadata.write_private(folder / 'result.json', json_bytes(result))
        sync_directory(folder)
        state['phase'] = 'probe-exported' if exported and error is None and not cleanup_errors else 'probe-failed'
        state['probe']['result'] = result
        retention.save_state(state)
        print(json.dumps(result, indent=2))
        print(f'Probe evidence: {folder}')
        retention.require(state['phase'] == 'probe-exported', 'Probe incomplete; preserve files and inspect. No retry.')
        print('RAW EXPORT COMPLETE, tracing OFF. Next: offline decode, NOT sleep. No reboot requested.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('check', 'run'))
    args = parser.parse_args()
    retention.require(os.geteuid() == 0, 'Run with sudo on the host; neither action requests sleep.')
    if args.action == 'check':
        info, _, _, _ = preflight()
        print(json.dumps({'boot': info['boot'], 'kernel': info['kernel'],
                          'function': FUNCTION, 'event': EVENT, 'calls': CALLS,
                          'scope': 'one owned RAM instance; archive, replace records, export; NO PM'}, indent=2))
        print('READ-ONLY PROBE PREFLIGHT PASSED. No files, trace controls or PM state changed.')
    else:
        run()


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, OSError, ValueError) as exc:
        print(f'ERROR: {exc}\nNo sleep requested; do not retry a failed run automatically.', file=sys.stderr)
        raise SystemExit(1)
