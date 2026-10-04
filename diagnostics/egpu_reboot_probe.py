#!/usr/bin/python3
"""Single no-sleep cross-reboot trace-generation test of the owned buffer.

check is read-only. arm writes harmless read records and leaves tracing OFF;
verify archives recovered records, then exports raw pages. Neither action
requests a reboot, PM transition, logout or sleep-guard bypass. No retry.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import uuid

import egpu_persistent_probe as probe
import egpu_persistent_trace as metadata
import egpu_trace_retention as retention


def _check_locked():
        state = retention.load_state()
        retention.require(state.get('phase') == 'probe-exported',
                          'The one-shot no-sleep export has not completed; preserve existing evidence.')
        previous = state.get('probe', {})
        result = previous.get('result', {})
        retention.require(result.get('raw_export_complete') is True
                          and result.get('error') is None
                          and result.get('cleanup_errors') == []
                          and result.get('sleep_requested') is False,
                          'The previous probe is incomplete or unsafe to build upon.')
        boot = retention.BOOT_ID.read_text().strip()
        retention.require(previous.get('boot') == boot,
                          'The previous probe was not exported in this boot; inspect state first.')
        folder = Path(previous.get('folder', ''))
        retention.require(folder.parent == metadata.LOG_ROOT
                          and folder.name.startswith('persistent-probe-'),
                          'Unexpected previous probe archive path.')
        retention.secure(metadata.LOG_ROOT, directory=True)
        retention.secure(folder, directory=True)
        retention.secure(folder / 'result.json')
        retention.require(json.loads((folder / 'result.json').read_text()) == result,
                          'The archived result does not match the ownership state.')
        retention.secure(folder / 'capture', directory=True)
        retention.secure(folder / 'capture/manifest.json')
        manifest = json.loads((folder / 'capture/manifest.json').read_text())
        retention.require(manifest.get('kind') == 'read-raw-probe'
                          and manifest.get('boot') == boot
                          and manifest.get('raw_complete') is True
                          and manifest.get('token') == previous.get('token')
                          and manifest.get('pid') == previous.get('pid'),
                          'The previous capture manifest is not the exported generation.')
        retention.validate_live(state)
        instance, mapping = retention.instance_state()
        retention.require(mapping == state.get('mapping'),
                          'Reserved buffer mapping changed; no cross-reboot plan is valid.')
        stats = probe.cpu_stats(instance)
        probe.no_loss(stats)
        retention.require(all(values['entries'] == 0 for values in stats.values()),
                          'The instance still contains records; never overwrite them.')
        trace = metadata.read(instance / 'trace', retention.MAX_TRACE_TEXT).decode()
        retention.require(not any(line.strip() and not line.lstrip().startswith('#')
                                  for line in trace.splitlines()),
                          'Readable trace still contains records; preserve them.')
        for name in probe.CONTROL_FILES:
            retention.require(probe.inert_filter(probe.text(instance / name)),
                              f'Existing {name} filter; not adopting it.')
        retention.require(probe.text(instance / probe.EVENT / 'filter') in ('none', '0')
                          and probe.text(instance / probe.EVENT / 'enable') == '0',
                          'Read event/filter is not idle.')
        globals_now = probe.global_state()
        retention.require(globals_now['current_tracer'] == 'nop'
                          and globals_now['events/enable'] == '0',
                          'Global tracing is active; do not mix experiments.')
        retention.require(shutil.disk_usage(metadata.LOG_ROOT).free >= 128 * 1024 * 1024,
                          'Need at least 128 MiB free before planning another archive.')
        return {'boot': boot, 'kernel': state['kernel'], 'mapping': mapping,
                'cpu_buffers': len(stats), 'previous_export': str(folder),
                'next_gate': 'explicitly arm one no-sleep generation, then ordinary reboot and verify',
                'warning': 'This check does not arm recording or request reboot/PM.'}


def check():
    with retention.locked():
        return _check_locked()


def baseline_files(instance):
    root = retention.TRACEFS
    names = ('events/header_page', 'events/header_event',
             *(f'events/{event.replace(":", "/")}/format' for event in metadata.EVENTS),
             f'{probe.EVENT}/format')
    files = {name: metadata.read(root / name) for name in names}
    files['modules.txt'] = metadata.read(metadata.PROC_ROOT / 'modules')
    files['trace-before.txt'] = metadata.read(instance / 'trace', retention.MAX_TRACE_TEXT)
    retention.require(not any(line.strip() and not line.lstrip().startswith('#')
                              for line in files['trace-before.txt'].decode().splitlines()),
                      'Trace changed before archival; refusing to overwrite it.')
    return files


def _fresh_folder():
    retention.secure(metadata.LOG_ROOT, directory=True)
    return Path(tempfile.mkdtemp(prefix='persistent-reboot-probe-', dir=metadata.LOG_ROOT))


def arm():
    with retention.locked():
        info = _check_locked()
        state = retention.load_state()
        instance, mapping = retention.instance_state()
        retention.require(mapping == info['mapping'], 'Mapping changed after preflight.')
        files = baseline_files(instance)
        folder = _fresh_folder()
        before = folder / 'before'
        before.mkdir(mode=0o700)
        probe.save_tree(before, {'schema': 1, **info}, files)
        before_stats = probe.cpu_stats(instance)
        retention.require(retention.load_state() == state
                          and probe.cpu_stats(instance) == before_stats
                          and metadata.read(instance / 'trace', retention.MAX_TRACE_TEXT)
                          == files['trace-before.txt'],
                          'State or trace changed while archiving.')
        retention.validate_live(state)
        checked_instance, checked_mapping = retention.instance_state()
        retention.require(checked_instance == instance and checked_mapping == mapping,
                          'Reserved instance changed while archiving.')
        pid = os.getpid()
        token = 'EGPU_CAPTURE_' + uuid.uuid4().hex
        state.update(phase='reboot-probe-preparing', reboot_probe={
            'boot': info['boot'], 'folder': str(folder), 'pid': pid,
            'token': token, 'mapping': mapping})
        retention.save_state(state)  # Intent is durable BEFORE trace controls change.
        try:
            probe.record(instance, pid, token)
            stats = probe.cpu_stats(instance)
            probe.no_loss(stats)
            retention.require(sum(v['entries'] for v in stats.values()) >= 18
                              and all(v['read events'] == 0 for v in stats.values()),
                              'Incomplete or consumed no-sleep generation.')
            captured = probe.capture_files(instance, files, pid, token)
            trace = captured['trace.txt'].decode()
            markers = {suffix: retention.marker_record(trace, token + suffix)
                       for suffix in ('_BEGIN', '_END')}
            retention.require(all(markers.values()), 'New markers not unique in live trace.')
            armed = folder / 'armed'
            armed.mkdir(mode=0o700)
            probe.save_tree(armed, {'schema': 1, **info, 'pid': pid,
                            'token': token, 'markers': markers}, captured)
            retention.require(retention.BOOT_ID.read_text().strip() == info['boot']
                              and retention.load_state() == state
                              and probe.global_state()['current_tracer'] == 'nop',
                              'Boot, state or global tracing changed during arm.')
            state['reboot_probe']['markers'] = markers
            state['phase'] = 'reboot-probe-armed'
        except BaseException as exc:
            state['reboot_probe']['error'] = str(exc) or type(exc).__name__
            state['phase'] = 'reboot-probe-failed'
        finally:
            errors = []
            for name, value in (('tracing_on', '0'), (probe.EVENT + '/enable', '0')):
                try:
                    probe.control(instance, name, value)
                except BaseException as exc:
                    errors.append(f'{name}: {exc}')
            if errors:
                state['reboot_probe']['cleanup_errors'] = errors
                state['phase'] = 'reboot-probe-failed'
            retention.save_state(state)
        retention.require(state['phase'] == 'reboot-probe-armed',
                          'Arm incomplete; preserve evidence and inspect: ' + str(folder))
    print(f'NO-SLEEP generation armed, tracing OFF. Evidence: {folder}')
    print('No reboot requested. When ready, ordinary reboot into the SAME deployment, then verify.')
    print('Do not run PM, cancel boot arguments or update the OS before verification.')


def _load_armed(folder):
    armed = folder / 'armed'
    retention.secure(armed, directory=True)
    retention.secure(armed / 'manifest.json')
    manifest = json.loads((armed / 'manifest.json').read_text())
    result, total = {}, 0
    for name, item in manifest['files'].items():
        relative = Path(name)
        retention.require(not relative.is_absolute() and '..' not in relative.parts,
                          'Unsafe archived metadata path.')
        path = armed / relative
        retention.secure(path)
        data = metadata.read(path, metadata.MAX_METADATA)
        retention.require(len(data) == item['bytes']
                          and hashlib.sha256(data).hexdigest() == item['sha256'],
                          'Archived metadata changed: ' + name)
        total += len(data)
        result[name] = data
    retention.require(total == manifest['metadata_bytes'] and total <= metadata.MAX_BUNDLE,
                      'Archived metadata size changed.')
    return manifest, result


def archive_recovered_text(folder, boot, trace):
    """Preserve even a failed marker check before any consuming raw read."""
    recovered_dir = folder / 'recovered'
    if not recovered_dir.exists():
        recovered_dir.mkdir(mode=0o700)
        probe.save_files(recovered_dir, {'trace.txt': trace,
                                         'boot-id.txt': (boot + '\n').encode()})
    else:
        retention.secure(recovered_dir, directory=True)
        for name, expected in (('trace.txt', trace), ('boot-id.txt', (boot + '\n').encode())):
            path = recovered_dir / name
            retention.secure(path)
            retention.require(metadata.read(path, retention.MAX_TRACE_TEXT) == expected,
                              'An earlier recovery snapshot differs; preserve it and inspect.')


def verify():
    with retention.locked():
        state = retention.load_state()
        retention.require(state.get('phase') == 'reboot-probe-armed',
                          'No armed generation to verify, or it has already been attempted.')
        owned = state['reboot_probe']
        folder = Path(owned['folder'])
        retention.require(folder.parent == metadata.LOG_ROOT
                          and folder.name.startswith('persistent-reboot-probe-'),
                          'Unexpected reboot-probe folder.')
        retention.secure(folder, directory=True)
        boot = retention.BOOT_ID.read_text().strip()
        retention.require(boot != owned['boot'], 'Same boot is not a retention test.')
        retention.validate_live(state)
        instance, mapping = retention.instance_state()
        retention.require(mapping == owned['mapping'], 'Reserved mapping changed.')
        manifest, files = _load_armed(folder)
        retention.require(manifest['boot'] == owned['boot']
                          and manifest['pid'] == owned['pid']
                          and manifest['token'] == owned['token'],
                          'Armed metadata identity changed.')
        trace = metadata.read(instance / 'trace', retention.MAX_TRACE_TEXT)
        archive_recovered_text(folder, boot, trace)
        recovered = {suffix: retention.marker_record(trace.decode(), owned['token'] + suffix)
                     for suffix in ('_BEGIN', '_END')}
        same_record = all(recovered[suffix] is not None
                          and all(recovered[suffix][key] == owned['markers'][suffix][key]
                                  for key in ('pid', 'cpu', 'timestamp'))
                          for suffix in recovered)
        if not same_record:
            state['phase'] = 'reboot-probe-not-retained'
            state['reboot_probe']['verification'] = {
                'boot': boot, 'same_mapping': True,
                'markers_present': {suffix: recovered[suffix] is not None for suffix in recovered},
                'same_record': False, 'raw_consumed': False,
                'recovered_trace': str(folder / 'recovered/trace.txt')}
            retention.save_state(state)
            raise RuntimeError('The newly recorded markers did not survive intact; '
                               'recovery snapshot saved, raw pages untouched.')
        stats_before = probe.cpu_stats(instance)
        probe.no_loss(stats_before)
        retention.require(sum(v['entries'] for v in stats_before.values()) >= 18
                          and all(v['read events'] == 0 for v in stats_before.values()),
                          'Recovered generation is incomplete or was already read.')
        state['phase'] = 'reboot-probe-verifying'
        retention.save_state(state)  # No automatic retry after raw consumption begins.
        capture = folder / 'capture'
        capture.mkdir(mode=0o700)
        probe.save_files(capture, files)
        probe.export_raw(instance, capture / 'raw', sorted(stats_before))
        stats_after = probe.cpu_stats(instance)
        probe.no_loss(stats_after)
        retention.require(all(v['entries'] == 0 for v in stats_after.values())
                          and all(stats_after[cpu]['read events'] == values['entries']
                                  for cpu, values in stats_before.items()),
                          'Recovered raw export accounting failed; preserve partial files.')
        stats_files = {f'stats/cpu{cpu}.json': probe.json_bytes({
            'before': stats_before[cpu], 'after': stats_after[cpu]}) for cpu in sorted(stats_before)}
        probe.save_files(capture, stats_files)
        files.update(stats_files)
        probe.seal_tree(capture, {'schema': 1, **manifest, 'kind': 'read-raw-probe',
                        'marker_retained': True, 'cpus': sorted(stats_before),
                        'raw_complete': True, 'verified_after_boot': boot}, files)
        raw_hashes = {f'raw/cpu{cpu}': {'bytes': (capture / f'raw/cpu{cpu}').stat().st_size,
                      'sha256': hashlib.sha256(metadata.read(capture / f'raw/cpu{cpu}',
                                                       probe.MAX_RAW)).hexdigest()}
                      for cpu in sorted(stats_before)}
        metadata.write_private(capture / 'raw-manifest.json', probe.json_bytes(raw_hashes))
        probe.sync_directory(capture)
        retention.require(retention.BOOT_ID.read_text().strip() == boot
                          and probe.global_state()['current_tracer'] == 'nop',
                          'Boot/global tracing changed during recovered export.')
        state['phase'] = 'reboot-probe-exported'
        state['reboot_probe']['verified_boot'] = boot
        retention.save_state(state)
    print(f'NEW GENERATION RETAINED and raw-exported. Evidence: {folder}')
    print('Next gate: unprivileged offline decode of capture, NOT sleep.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('check', 'arm', 'verify'))
    args = parser.parse_args()
    retention.require(os.geteuid() == 0, 'Run with sudo; no action requests reboot or sleep.')
    if args.action == 'check':
        print(json.dumps(check(), indent=2))
        print('READ-ONLY REBOOT-PROBE GATE PASSED. No trace, PM or boot state changed.')
    else:
        globals()[args.action]()


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, OSError, ValueError, KeyError) as exc:
        print(f'ERROR: {exc}\nNo reboot or PM requested. arm/verify may have changed '
              'the owned trace; preserve evidence and inspect before any retry.', file=sys.stderr)
        raise SystemExit(1)
