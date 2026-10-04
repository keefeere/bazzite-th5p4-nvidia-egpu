#!/usr/bin/python3
"""Read-only tracefs inspection / private metadata bundle for persistent PM tracing.

No recorder or PM invocation is exposed. Capture trace decoding metadata before
deciding how to instrument a boot-created buffer whose symbol decoding changed.
Only bundle creates files, under a new private diagnostic directory.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

import egpu_trace_retention as retention


LOG_ROOT = Path('/var/log/egpu-sleep-lab')
PROC_ROOT = Path('/proc')
EVENTS = ('power:suspend_resume', 'power:device_pm_callback_start',
          'power:device_pm_callback_end', 'notifier:notifier_run',
          'ftrace:print', 'ftrace:function')
FUNCTIONS = ('pm_prepare_console', 'pm_notifier_call_chain_robust',
             'nv_pm_notifier', 'nv_set_system_power_state')
MAX_METADATA = 32 * 1024 * 1024
MAX_SMALL = 1024 * 1024
MAX_BUNDLE = 64 * 1024 * 1024


def read(path, limit=MAX_SMALL):
    """Bounded read only, with no consuming trace interface."""
    with path.open('rb') as stream:
        data = stream.read(limit + 1)
    retention.require(len(data) <= limit, f'Metadata file exceeds bound: {path}')
    return data


def event_descriptor(data, expected_name):
    text = data.decode('utf-8', errors='strict')
    name = re.findall(r'^name:\s*(\S+)\s*$', text, re.M)
    ids = re.findall(r'^ID:\s*([0-9]+)\s*$', text, re.M)
    retention.require(name == [expected_name] and len(ids) == 1 and int(ids[0]) > 0,
                      f'Missing/ambiguous event descriptor: {expected_name}')
    retention.require(re.search(r'field:\s*unsigned short common_type;', text) is not None,
                      f'Unsupported common event header: {expected_name}')
    return int(ids[0])


def symbol_summary(data):
    addresses = {}
    for line in data.decode('utf-8', errors='strict').splitlines():
        fields = line.split()
        if len(fields) >= 3 and fields[2] in ('_text', 'tracing_mark_write', *FUNCTIONS):
            retention.require(re.fullmatch(r'[0-9a-fA-F]+', fields[0]) is not None,
                              'Invalid kallsyms address.')
            addresses.setdefault(fields[2], []).append(int(fields[0], 16))
    for name in ('_text', 'tracing_mark_write', *FUNCTIONS):
        values = addresses.get(name, [])
        retention.require(len(values) == 1 and values[0] != 0,
                          f'Symbol address hidden/missing/ambiguous: {name}; no decoding baseline.')
    # Only names and visibility reach stdout. Full addresses stay in private files.
    return {'visible_required_symbols': sorted(addresses),
            'warning': 'Do not use current-boot symbols to decode earlier raw addresses.'}


def module_layout(data):
    layout = []
    for line in data.decode('utf-8', errors='strict').splitlines():
        fields = line.split()
        retention.require(len(fields) >= 6 and fields[1].isdigit()
                          and re.fullmatch(r'0x[0-9a-fA-F]+', fields[5]) is not None,
                          'Unsupported /proc/modules record.')
        # Reference counts/users may change without modules moving. Names, size,
        # lifecycle state and load address must remain stable while collecting.
        layout.append((fields[0], fields[1], fields[4], fields[5]))
    return sorted(layout)


def last_boot_summary(data):
    """Metadata availability is independent of text-marker retention."""
    text = data.decode('utf-8', errors='strict').strip()
    lines = text.splitlines()
    rows = [re.fullmatch(r'([0-9a-fA-F]+)\s+(\S+)', line) for line in lines]
    if text.startswith('# Current'):
        return {'previous_kernel_base_available': False, 'previous_module_count': 0,
                'state': 'current-boot metadata, not a prior-boot relocation map'}
    if not rows or any(row is None for row in rows):
        return {'previous_kernel_base_available': False, 'previous_module_count': 0,
                'state': 'unrecognized; preserve verbatim and inspect'}
    kernel = [int(row[1], 16) for row in rows if row[2] == '[kernel]']
    usable = len(kernel) == 1 and kernel[0] != 0
    return {'previous_kernel_base_available': usable,
            'previous_module_count': sum(row[2] != '[kernel]' and int(row[1], 16) != 0 for row in rows),
            'state': ('prior-boot addresses supplied; decoding still needs validation' if usable else
                      'missing/zero prior kernel base; current symbols cannot correct old addresses')}


def collect():
    """No writes: require the live, verified marker and unchanged metadata."""
    state = retention.load_state()
    boot = retention.BOOT_ID.read_text().strip()
    verified = state.get('verification', {})
    retention.require(state.get('phase') == 'verified'
                      and verified.get('retained') is True
                      and verified.get('same_record') is True
                      and verified.get('boot') == boot,
                      'Run the corrected retention verify in this boot first.')
    retention.validate_live(state)
    instance, mapping = retention.instance_state()
    retention.require(mapping == state['mapping'], 'Reserved mapping changed; no metadata handoff.')
    trace = read(instance / 'trace', retention.MAX_TRACE_TEXT)
    original_path = retention.ROOT / 'written-marker-trace.txt'
    retention.secure(original_path)
    original = read(original_path, retention.MAX_TRACE_TEXT)
    marker = state['marker']
    saved_record = retention.marker_record(original.decode('utf-8'), marker)
    live_record = retention.marker_record(trace.decode('utf-8'), marker)
    retention.require(saved_record is not None and live_record is not None
                      and all(saved_record[k] == live_record[k] for k in ('pid', 'cpu', 'timestamp')),
                      'The verified marker is no longer intact; preserve the buffer and inspect.')
    root = retention.TRACEFS
    files = {
        'retention-state.json': (json.dumps(state, indent=2) + '\n').encode(),
        'marker-original.txt': original,
        'marker-recovered.txt': trace,
        'last_boot_info.txt': read(instance / 'last_boot_info'),
        'trace_clock.txt': read(instance / 'trace_clock'),
        'kallsyms.txt': read(PROC_ROOT / 'kallsyms', MAX_METADATA),
        'modules.txt': read(PROC_ROOT / 'modules'),
        'printk_formats.txt': read(root / 'printk_formats', MAX_METADATA),
        'events/header_page': read(root / 'events/header_page'),
        'events/header_event': read(root / 'events/header_event'),
    }
    summary = symbol_summary(files['kallsyms.txt'])
    retention.require(files['last_boot_info.txt'].strip(), 'last_boot_info is empty; inspect before recording.')
    formats = {}
    sources = {}
    for event in EVENTS:
        group, name = event.split(':')
        target = f'events/{group}/{name}/format'
        sources[target] = root / target
        files[target] = read(sources[target])
        formats[event] = event_descriptor(files[target], name)
    retention.require(len(set(formats.values())) == len(formats), 'Event IDs are not unique.')
    # Record this actual kernel's pointer-vs-inline string ABI, not upstream assumptions.
    suspend_format = files['events/power/suspend_resume/format'].decode()
    retention.require(re.search(r'field:\s*const char\s*\*\s*action;', suspend_format) is not None,
                      'suspend_resume action format differs; review the decoder plan.')
    for event in ('device_pm_callback_start', 'device_pm_callback_end'):
        description = files[f'events/power/{event}/format'].decode()
        for field in ('device', 'driver'):
            retention.require(re.search(r'field:\s*__data_loc char\[\]\s+' + field + ';', description) is not None,
                              f'{event}/{field} is not an inline string; review before recording.')
    available = read(root / 'available_filter_functions', MAX_METADATA).decode()
    function_lines = [line for line in available.splitlines()
                      if line.strip()
                      if any(line.split()[0] == f or line.split()[0].startswith(f + '.')
                             for f in FUNCTIONS)]
    # Exact functions and compiler clones are inventoried, never enabled here.
    retention.require(set(FUNCTIONS) <= {line.split()[0] for line in function_lines},
                      'Required function trace targets changed or are absent.')
    files['available-function-targets.txt'] = ('\n'.join(function_lines) + '\n').encode()
    for path in sorted((instance / 'per_cpu').glob('cpu*/stats')):
        retention.require(re.fullmatch(r'cpu[0-9]+', path.parent.name) is not None, 'Unexpected CPU path.')
        files[f'per_cpu/{path.parent.name}/stats'] = read(path)
        files[f'per_cpu/{path.parent.name}/buffer_meta'] = read(path.parent / 'buffer_meta')
    retention.require(any(name.endswith('/stats') for name in files), 'No per-CPU buffer statistics found.')
    retention.require(sum(map(len, files.values())) <= MAX_BUNDLE, 'Metadata bundle exceeds size bound.')
    retention.require(module_layout(files['modules.txt']) == module_layout(read(PROC_ROOT / 'modules')),
                      'Module layout changed during capture; no consistent decoding baseline.')
    for name, source in sources.items():
        retention.require(files[name] == read(source), 'Event formats changed during capture.')
    retention.require(retention.BOOT_ID.read_text().strip() == boot, 'Boot changed while collecting.')
    retention.require(read(instance / 'trace', retention.MAX_TRACE_TEXT) == trace,
                      'Trace changed during metadata collection; no consistent snapshot.')
    # Recheck boot/deployment/guard, OFF/nop/no events/triggers, and ownership.
    retention.require(retention.load_state() == state, 'Retention state changed during capture.')
    retention.validate_live(state)
    _, after_mapping = retention.instance_state()
    retention.require(after_mapping == mapping, 'Reserved mapping changed during capture.')
    info = {'schema': 1, 'boot': boot, 'kernel': state['kernel'], 'mapping': mapping,
            'marker_retained': True, 'symbols': summary, 'event_ids': formats,
            'last_boot_metadata': last_boot_summary(files['last_boot_info.txt']),
            'trace_cmd_available': shutil.which('trace-cmd') is not None,
            'metadata_bytes': sum(map(len, files.values())),
            'warning': 'Metadata only: no PM recorder is armed, no sleep permission, no forced-reset proof.'}
    return info, files


def write_private(path, data):
    with open(path, 'xb', opener=lambda p, flags: os.open(p, flags | os.O_NOFOLLOW, 0o600)) as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def bundle():
    with retention.locked():
        info, files = collect()
        if not LOG_ROOT.exists():
            LOG_ROOT.mkdir(mode=0o700)
        retention.secure(LOG_ROOT, directory=True)
        folder = Path(tempfile.mkdtemp(prefix='persistent-meta-', dir=LOG_ROOT))
        try:
            for name, data in files.items():
                path = folder / name
                parent = folder
                for part in Path(name).parts[:-1]:
                    parent /= part
                    parent.mkdir(mode=0o700, exist_ok=True)
                write_private(path, data)
            manifest = {**info, 'files': {
                name: {'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
                for name, data in files.items()}}
            write_private(folder / 'manifest.json', (json.dumps(manifest, indent=2) + '\n').encode())
            # Persist directory entries, including nested event/CPU directories.
            for path in [p for p in folder.rglob('*') if p.is_dir()] + [folder, LOG_ROOT]:
                fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
        except BaseException:
            print(f'INCOMPLETE metadata bundle retained for inspection: {folder}', flush=True)
            raise
    print(json.dumps(info, indent=2))
    print(f'METADATA BUNDLE SAVED: {folder}')
    print('Trace contents/settings and retention state unchanged. NO SLEEP or reboot requested.')
    return folder


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('check', 'bundle'))
    args = parser.parse_args()
    retention.require(os.geteuid() == 0, 'Run with sudo on the host; neither action records events or requests PM.')
    if args.action == 'check':
        info, _ = collect()
        print(json.dumps(info, indent=2))
        print('READ-ONLY METADATA CHECK PASSED. No files or trace/PM settings changed.')
    else:
        bundle()


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
        print(f'ERROR: {exc}\nNo PM recorder armed and no sleep requested.')
        raise SystemExit(1)
