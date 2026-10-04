#!/usr/bin/python3
"""Decode synthetic PM or --recorded NO-SLEEP read data with saved metadata.

Run unprivileged with a private COPY of an egpu_persistent_trace.py bundle and a
separately built trace-cmd. Never accesses live tracing/PM. Default output uses
synthetic fixtures; --recorded decodes a private copy of an actual probe export.
Neither mode validates suspend or reset retention.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
import tempfile


def require(value, message):
    if not value:
        raise RuntimeError(message)


def load_bundle(folder, kind=None):
    require(not folder.is_symlink(), 'Use a private directory, not a symlink.')
    require(folder.stat().st_uid == os.geteuid() and not folder.stat().st_mode & 0o077,
            'Bundle copy must be owned by you and private (0700).')
    manifest_path = folder / 'manifest.json'
    require(not manifest_path.is_symlink() and manifest_path.stat().st_size <= 1024 * 1024,
            'Unexpected manifest.')
    manifest = json.loads(manifest_path.read_text())
    require(manifest.get('schema') == 1 and manifest.get('marker_retained') is True,
            'Not a verified marker metadata bundle.')
    require(manifest.get('kind') == kind, 'Wrong bundle kind for this decoder mode.')
    files = {}
    total = 0
    for name, description in manifest['files'].items():
        relative = Path(name)
        require(not relative.is_absolute() and '..' not in relative.parts and relative.parts,
                'Unsafe manifest path.')
        path = folder
        for part in relative.parts:
            path /= part
            require(not path.is_symlink(), 'Symlink inside bundle.')
        expected = description['bytes']
        require(isinstance(expected, int) and 0 <= expected <= 32 * 1024 * 1024,
                'Oversize metadata file.')
        total += expected
        require(total <= 64 * 1024 * 1024, 'Oversize metadata bundle.')
        require(path.is_file() and path.stat().st_size == expected, f'Size mismatch: {name}')
        with path.open('rb') as stream:
            data = stream.read(expected + 1)
        require(len(data) == expected and hashlib.sha256(data).hexdigest() == description['sha256'],
                f'Hash mismatch: {name}')
        files[name] = data
    require(manifest.get('metadata_bytes') == total, 'Bundle total does not match manifest.')
    return manifest, files


def unique_symbol(data, name):
    addresses = [int(parts[0], 16) for line in data.decode().splitlines()
                 if len(parts := line.split()) >= 3 and parts[2] == name]
    require(len(addresses) == 1 and addresses[0] != 0, f'Unavailable symbol: {name}')
    return addresses[0]


def event(files, group, name, expected_fields):
    text = files[f'events/{group}/{name}/format'].decode()
    for field, offset, size in (
        ('unsigned short common_type', 0, 2), ('unsigned char common_flags', 2, 1),
        ('unsigned char common_preempt_count', 3, 1), ('int common_pid', 4, 4),
        *expected_fields):
        pattern = (r'field:\s*' + re.escape(field) + r';\s*offset:' + str(offset)
                   + r';\s*size:' + str(size) + ';')
        require(re.search(pattern, text) is not None, f'Unreviewed field layout: {name}/{field}')
    ids = re.findall(r'^ID:\s*(\d+)\s*$', text, re.M)
    require(len(ids) == 1 and 0 < int(ids[0]) <= 65535, f'Invalid event ID: {name}')
    return struct.pack('<HBBi', int(ids[0]), 0, 0, 4242)


def page(records):
    body = bytearray()
    for payload in records:
        payload += b'\0' * (-len(payload) % 4)
        words = len(payload) // 4
        require(0 < words <= 28, 'Fixture record unexpectedly large.')
        body += struct.pack('<I', (1_000_000 << 5) | words) + payload
    require(len(body) <= 4080, 'Fixture page overflow.')
    return struct.pack('<QQ', 1_000_000_000, len(body)) + body + bytes(4080 - len(body))


def fixtures(files):
    header = files['events/header_page'].decode()
    require(re.search(r'field:\s*u64 timestamp;\s*offset:0;\s*size:8;', header)
            and re.search(r'field:\s*local_t commit;\s*offset:8;\s*size:8;', header)
            and re.search(r'field:\s*char data;\s*offset:16;\s*size:4080;', header),
            'Only the inspected x86_64 4 KiB ring-page layout is supported.')
    syms = files['kallsyms.txt']
    mark_ip = unique_symbol(syms, 'tracing_mark_write')
    nv_ip = unique_symbol(syms, 'nv_pm_notifier')
    parent = unique_symbol(syms, 'pm_notifier_call_chain_robust')
    marker = event(files, 'ftrace', 'print', [('unsigned long ip', 8, 8), ('char buf[]', 16, 0)])
    marker += struct.pack('<Q', mark_ip) + b'EGPU_OFFLINE_SYNTHETIC_ONLY\n\0'
    function = event(files, 'ftrace', 'function', [('unsigned long ip', 8, 8), ('unsigned long parent_ip', 16, 8)])
    function += struct.pack('<QQ', nv_ip, parent)
    phase = event(files, 'power', 'suspend_resume',
                  [('const char * action', 8, 8), ('int val', 16, 4), ('bool start', 20, 1)])
    phases = set(re.findall(rb'^(0x[0-9a-fA-F]+)\s*:\s*"dpm_suspend_noirq"\s*$',
                           files['printk_formats.txt'], re.M))
    require(len(phases) == 1, 'No unique saved pointer for the fixture PM phase.')
    phase += struct.pack('<QiB', int(phases.pop(), 16), 0, 1)
    start = bytearray(event(files, 'power', 'device_pm_callback_start',
        [('__data_loc char[] device', 8, 4), ('__data_loc char[] driver', 12, 4),
         ('__data_loc char[] parent', 16, 4), ('__data_loc char[] pm_ops', 20, 4), ('int event', 24, 4)]))
    start += bytes(16) + struct.pack('<i', 2)
    for offset, value in ((8, b'0000:03:00.0\0'), (12, b'nvidia\0'),
                          (16, b'0000:02:00.0\0'), (20, b'fixture_noirq\0')):
        struct.pack_into('<I', start, offset, (len(value) << 16) | len(start))
        start += value
    end = bytearray(event(files, 'power', 'device_pm_callback_end',
        [('__data_loc char[] device', 8, 4), ('__data_loc char[] driver', 12, 4), ('int error', 16, 4)]))
    end += bytes(8) + struct.pack('<i', -16)
    for offset, value in ((8, b'0000:03:00.0\0'), (12, b'nvidia\0')):
        struct.pack_into('<I', end, offset, (len(value) << 16) | len(end))
        end += value
    return page([marker, function, phase, bytes(start), bytes(end)])


def run_tool(binary, args, folder):
    result = subprocess.run([str(binary), *map(str, args)], cwd=folder,
                            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            timeout=60, check=False)
    require(result.returncode == 0, f'Offline trace-cmd failed: {result.stderr or result.stdout}')
    return result.stdout


def check(folder, binary):
    require(os.geteuid() != 0, 'Run this offline checker WITHOUT sudo, on a private metadata copy.')
    require(sys.byteorder == 'little' and struct.calcsize('P') == 8, 'x86_64 layout required.')
    require(binary.is_absolute() and binary.is_file(), 'Specify the absolute path to trace-cmd.')
    manifest, files = load_bundle(folder)
    raw_fixture = fixtures(files)  # Validates all used layouts before creating output.
    output = Path(tempfile.mkdtemp(prefix='egpu-trace-offline-', dir='/var/tmp'))
    # All writes below are in this newly created private directory. No tracefs,
    # /proc, logind, systemd, PCI or module interfaces are opened by this helper.
    for name, data in files.items():
        if not name.startswith('events/'):
            continue
        path = output / 'metadata' / name
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.write_bytes(data)
    (output / 'metadata/printk_formats').write_bytes(files['printk_formats.txt'])
    (output / 'metadata/kallsyms').write_bytes(files['kallsyms.txt'])
    (output / 'metadata/saved_cmdlines').write_text('4242 offline-fixture\n')
    (output / 'synthetic.cpu0').write_bytes(raw_fixture)
    trace_dir = output / 'metadata'
    run_tool(binary, ['restore', '-c', '-t', trace_dir, '-k', trace_dir / 'kallsyms',
                     '-o', output / 'partial.dat'], output)
    run_tool(binary, ['restore', '-i', output / 'partial.dat', '-o', output / 'synthetic.dat',
                     output / 'synthetic.cpu0'], output)
    report = run_tool(binary, ['report', '-N', '-i', output / 'synthetic.dat'], output)
    (output / 'synthetic-report.txt').write_text(report)
    for expected in ('tracing_mark_write: EGPU_OFFLINE_SYNTHETIC_ONLY',
                     'nv_pm_notifier', 'pm_notifier_call_chain_robust',
                     'dpm_suspend_noirq[0] begin',
                     'nvidia 0000:03:00.0, parent: 0000:02:00.0, fixture_noirq[suspend]',
                     'nvidia 0000:03:00.0, err=-16'):
        require(expected in report, f'Synthetic report failed to decode: {expected}; inspect {output}')
    # A negative control proves the reader uses saved metadata, not this host's
    # current /proc symbols or strings. Only derivative fixture files are edited.
    control = output / 'control-metadata'
    shutil.copytree(trace_dir, control)
    control_symbols, renamed = re.subn(rb'(?m)(\s)nv_pm_notifier(\s)',
                                       rb'\1FIXTURE_saved_nv_notifier\2', files['kallsyms.txt'])
    require(renamed == 1, 'Could not construct a unique saved-symbol control.')
    (control / 'kallsyms').write_bytes(control_symbols)
    (control / 'printk_formats').write_bytes(files['printk_formats.txt'].replace(
        b'"dpm_suspend_noirq"', b'"FIXTURE_saved_phase"'))
    run_tool(binary, ['restore', '-c', '-t', control, '-k', control / 'kallsyms',
                     '-o', output / 'control-partial.dat'], output)
    run_tool(binary, ['restore', '-i', output / 'control-partial.dat', '-o', output / 'control.dat',
                     output / 'synthetic.cpu0'], output)
    control_report = run_tool(binary, ['report', '-N', '-i', output / 'control.dat'], output)
    (output / 'control-report.txt').write_text(control_report)
    require('FIXTURE_saved_nv_notifier' in control_report
            and 'FIXTURE_saved_phase[0] begin' in control_report
            and 'dpm_suspend_noirq[0] begin' not in control_report,
            f'Decoder did not honor the saved-metadata control; inspect {output}')
    summary = {'bundle_boot': manifest['boot'], 'verified_files': len(files),
               'fixture_records': 5, 'synthetic_decode_passed': True,
               'saved_metadata_controls_decoding': True,
               'output': str(output),
               'warning': 'Synthetic records only. No live capture, suspend, or reboot was tested.'}
    (output / 'result.json').write_text(json.dumps(summary, indent=2) + '\n')
    return summary


def recorded_raw(folder, manifest):
    """Validate the separate, exclusive raw manifest without touching tracefs."""
    cpus = manifest.get('cpus')
    require(isinstance(cpus, list) and 0 < len(cpus) <= 256
            and all(type(cpu) is int for cpu in cpus)
            and cpus == list(range(len(cpus))) and manifest.get('raw_complete') is True,
            'Raw CPU numbering/completion is not confirmed.')
    path = folder / 'raw-manifest.json'
    require(path.is_file() and not path.is_symlink() and path.stat().st_size <= 65536,
            'Invalid raw manifest.')
    descriptions = json.loads(path.read_text())
    require(set(descriptions) == {f'raw/cpu{cpu}' for cpu in cpus}, 'Raw file set differs from CPU list.')
    require((folder / 'raw').is_dir() and not (folder / 'raw').is_symlink(), 'Invalid raw directory.')
    output, total = [], 0
    for cpu in cpus:
        name = f'raw/cpu{cpu}'
        path = folder / name
        size = descriptions[name]['bytes']
        require(type(size) is int and 0 <= size <= 20 * 1024 * 1024 and size % 4096 == 0,
                'Raw file has invalid size or partial pages.')
        total += size
        require(total <= 20 * 1024 * 1024, 'Raw bundle is too large.')
        require(not path.is_symlink() and path.is_file() and path.stat().st_size == size,
                'Raw file missing, unsafe or changed.')
        with path.open('rb') as stream:
            data = stream.read(size + 1)
        require(len(data) == size and hashlib.sha256(data).hexdigest() == descriptions[name]['sha256'],
                f'Raw checksum mismatch: {name}')
        output.append(data)
    require(total > 0, 'Raw bundle is empty.')
    return output


def validate_recorded_report(report, pid, token):
    require(type(pid) is int and pid > 0 and re.fullmatch(r'EGPU_CAPTURE_[a-f0-9]{32}', token),
            'Invalid probe identity.')
    lines = [line for line in report.splitlines() if re.search(r'-' + str(pid) + r'\s+\[', line)]
    marker_positions = {}
    for suffix in ('_BEGIN', '_END'):
        positions = [index for index, line in enumerate(lines)
                     if line.rstrip().endswith(': ' + token + suffix)]
        require(len(positions) == 1,
                'Offline marker missing/duplicated or PID differs.')
        marker_positions[suffix] = positions[0]
    require(marker_positions['_BEGIN'] < marker_positions['_END'],
            'Offline probe markers are reversed.')
    between = lines[marker_positions['_BEGIN'] + 1:marker_positions['_END']]
    functions = [line for line in between if re.search(r'function:\s+vfs_read\s+<--', line)]
    syscalls = [line for line in between if 'sys_enter_read:' in line]
    require(len(functions) == 8 and len(syscalls) == 8,
            'Expected all eight function/syscall pairs between the probe markers.')
    require(not re.search(r'LOST.*EVENT', report, re.I), 'Decoder reports lost events.')


def decode_recorded(folder, binary):
    require(os.geteuid() != 0, 'Run this offline checker WITHOUT sudo, on a private capture copy.')
    require(sys.byteorder == 'little' and struct.calcsize('P') == 8, 'x86_64 layout required.')
    require(binary.is_absolute() and binary.is_file(), 'Specify the absolute path to trace-cmd.')
    manifest, files = load_bundle(folder, kind='read-raw-probe')
    raw = recorded_raw(folder, manifest)
    output = Path(tempfile.mkdtemp(prefix='egpu-trace-decoded-', dir='/var/tmp'))
    for name, data in files.items():
        if name.startswith('events/'):
            path = output / 'metadata' / name
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            path.write_bytes(data)
    trace_dir = output / 'metadata'
    for target, source in (('printk_formats', 'printk_formats.txt'), ('kallsyms', 'kallsyms.txt'),
                           ('saved_cmdlines', 'saved_cmdlines.txt')):
        (trace_dir / target).write_bytes(files[source])
    # Pass EVERY CPU file in numeric order, including empty ones.
    for cpu, data in enumerate(raw):
        (output / f'cpu{cpu}').write_bytes(data)
    run_tool(binary, ['restore', '-c', '-t', trace_dir, '-k', trace_dir / 'kallsyms',
                     '-o', output / 'partial.dat'], output)
    run_tool(binary, ['restore', '-i', output / 'partial.dat', '-o', output / 'capture.dat',
                     *(output / f'cpu{cpu}' for cpu in range(len(raw)))], output)
    report = run_tool(binary, ['report', '-N', '-i', output / 'capture.dat'], output)
    (output / 'report.txt').write_text(report)
    validate_recorded_report(report, manifest['pid'], manifest['token'])
    # A second decode of the same raw pages with a renamed SAVED symbol proves
    # this path does not silently resolve function IPs from the running boot.
    control = output / 'control-metadata'
    shutil.copytree(trace_dir, control)
    symbols, renamed = re.subn(rb'(?m)^([0-9a-fA-F]+\s+[tT]\s+)vfs_read(\s|$)',
                               rb'\1EGPU_saved_vfs_read\2', files['kallsyms.txt'])
    require(renamed == 1, 'Could not construct a unique saved VFS symbol control.')
    (control / 'kallsyms').write_bytes(symbols)
    run_tool(binary, ['restore', '-c', '-t', control, '-k', control / 'kallsyms',
                     '-o', output / 'control-partial.dat'], output)
    run_tool(binary, ['restore', '-i', output / 'control-partial.dat',
                     '-o', output / 'control.dat',
                     *(output / f'cpu{cpu}' for cpu in range(len(raw)))], output)
    control_report = run_tool(binary, ['report', '-N', '-i', output / 'control.dat'], output)
    (output / 'control-report.txt').write_text(control_report)
    require(re.search(r'function:\s+EGPU_saved_vfs_read\s+<--', control_report)
            and not re.search(r'function:\s+vfs_read\s+<--', control_report),
            'Real-record decoder did not honor the saved-symbol control.')
    summary = {'bundle_boot': manifest['boot'], 'real_capture_decoded': True,
               'saved_metadata_controls_decoding': True,
               'cpus': len(raw), 'output': str(output),
               'warning': 'No-sleep read capture only; not PM, reboot retention or working suspend.'}
    (output / 'result.json').write_text(json.dumps(summary, indent=2) + '\n')
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('bundle', type=Path)
    parser.add_argument('--trace-cmd', type=Path, required=True)
    parser.add_argument('--recorded', action='store_true',
                        help='Decode a real NO-SLEEP probe capture copy, not synthetic fixtures.')
    args = parser.parse_args()
    action = decode_recorded if args.recorded else check
    print(json.dumps(action(args.bundle, args.trace_cmd), indent=2))


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, OSError, ValueError, KeyError, subprocess.TimeoutExpired) as exc:
        print(f'ERROR: {exc}\nNo live tracing or PM action requested.', file=sys.stderr)
        raise SystemExit(1)
