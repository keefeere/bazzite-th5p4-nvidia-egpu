"""Hardware-free metadata collection tests. No live tracing, subprocesses or PM."""
import contextlib
import copy
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


DIAGNOSTICS = Path(__file__).resolve().parents[1] / 'diagnostics'


def load(name):
    spec = importlib.util.spec_from_file_location(name, DIAGNOSTICS / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


r = load('egpu_trace_retention')
with patch.dict(sys.modules, {'egpu_trace_retention': r}):
    m = load('egpu_persistent_trace')

BOOT = '43a59cbc-dc88-49c1-8bae-9c2c709d08a3'
MARKER = 'EGPU_RETENTION_test_previousboot'
ORIGINAL = f'python3-24306 [013] ...1. 647.057509: tracing_mark_write: {MARKER}\n'
RECOVERED = f'<...>-24306 [013] ...1. 647.057509: kallsyms_offsets: {MARKER}\n'


class MetadataTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        for name in ('ROOT', 'TRACEFS', 'CMDLINE', 'BOOT_ID', 'PM_TEST', 'RUNTIME', 'GUARD'):
            self.mock(r, name, self.tmp / name.lower())
        self.mock(m, 'LOG_ROOT', self.tmp / 'logs')
        self.mock(m, 'PROC_ROOT', self.tmp / 'proc')
        self.mock(m.shutil, 'which', lambda _: None)
        self.mock(r.subprocess, 'run', side_effect=AssertionError('No real subprocess permitted'))
        self.mock(r, 'command', self.command)
        self.kernel = {'release': r.TESTED_KERNEL, 'image_sha256': 'test-hash'}
        self.mock(r, 'kernel_identity', lambda: self.kernel)
        self.args = ['quiet', *r.ARGS]
        self.pending = False
        self.active = ''
        self.jobs = ''
        self.effective_guard = 'argv[]=/usr/bin/bash /etc/egpu-nvidia/egpu-sleep-guard.sh; ignore_errors=no'
        self.mapping = {'address': '0x644000000', 'size': r.SIZE}
        self.instance = r.TRACEFS / 'instances' / r.NAME
        self.state = {'schema': 1, 'phase': 'verified', 'owned_args': list(r.ARGS),
                      'base_args': ['quiet'], 'staged_checksum': 'test-deployment',
                      'kernel': self.kernel, 'marker': MARKER, 'mapping': self.mapping,
                      'verification': {'retained': True, 'same_record': True, 'boot': BOOT}}
        self.put(r.ROOT / 'state.json', json.dumps(self.state))
        self.put(r.ROOT / 'written-marker-trace.txt', ORIGINAL)
        self.put(r.BOOT_ID, BOOT)
        self.put(r.CMDLINE, ' '.join(self.args))
        self.put(r.PM_TEST, '[none] freezer devices platform')
        self.put(r.GUARD, 'ExecStartPre=/usr/bin/bash /etc/egpu-nvidia/egpu-sleep-guard.sh %n')
        for name, value in {
            'tracing_on': '0', 'current_tracer': 'nop', 'events/enable': '0',
            'options/markers': '1', 'options/trace_printk_dest': '0',
            'events/power/suspend_resume/trigger': '# no triggers\n',
            'trace': RECOVERED, 'last_boot_info': 'previous boot metadata',
            'trace_clock': 'local [global]', 'per_cpu/cpu0/stats': 'entries: 0\n',
            'per_cpu/cpu0/buffer_meta': b'metadata\x00\x80',
            'per_cpu/cpu13/stats': 'entries: 1\n',
            'per_cpu/cpu13/buffer_meta': b'metadata\x00\xff',
        }.items():
            self.put(self.instance / name, value)
        for event_id, event in enumerate(m.EVENTS, 1):
            group, name = event.split(':')
            fields = ''
            if name == 'suspend_resume':
                fields += '\tfield:const char * action;\toffset:8;\tsize:8;\tsigned:0;\n'
            if name.startswith('device_pm_callback_'):
                fields += '\tfield:__data_loc char[] device;\toffset:8;\tsize:4;\tsigned:0;\n'
                fields += '\tfield:__data_loc char[] driver;\toffset:12;\tsize:4;\tsigned:0;\n'
            self.put(r.TRACEFS / 'events' / group / name / 'format',
                     f'name: {name}\nID: {event_id}\nformat:\n'
                     '\tfield:unsigned short common_type;\toffset:0;\tsize:2;\tsigned:0;\n' + fields)
        for name in ('header_page', 'header_event'):
            self.put(r.TRACEFS / 'events' / name, 'binary format description\n')
        self.put(r.TRACEFS / 'printk_formats', 'kernel string table\n')
        self.put(r.TRACEFS / 'available_filter_functions', '\n' + '\n'.join(m.FUNCTIONS)
                 + '\n\nnv_set_system_power_state.part.0 [nvidia]\nunrelated_function\n')
        self.put(m.PROC_ROOT / 'kallsyms', '\n'.join(
            f'ffffffff8100{i:04x} T {name}'
            for i, name in enumerate(('_text', 'tracing_mark_write', *m.FUNCTIONS), 1)))
        self.put(m.PROC_ROOT / 'modules', 'nvidia 12345 9 nvidia_modeset, Live 0xffffffffa1000000 (OE)\n')
        self.output = io.StringIO()
        redirect = contextlib.redirect_stdout(self.output)
        redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)

    def mock(self, obj, name, value=None, **kwargs):
        manager = patch.object(obj, name, **kwargs) if kwargs else patch.object(obj, name, value)
        result = manager.start()
        self.addCleanup(manager.stop)
        return result

    def put(self, path, data):
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.write_bytes(data.encode() if isinstance(data, str) else data)
        path.chmod(0o600)

    def command(self, argv):
        if argv == ['rpm-ostree', 'status', '--json']:
            return json.dumps({'transaction': None, 'deployments': [
                {'booted': True, 'staged': self.pending, 'checksum': 'test-deployment'}]})
        if argv == ['rpm-ostree', 'kargs']:
            return ' '.join(self.args)
        if argv == ['stat', '-f', '-c', '%T', str(r.TRACEFS)]:
            return 'tracefs'
        if argv == ['journalctl', '-k', '-b', '--no-pager', '-o', 'cat']:
            return (f'Tracing: mapped boot instance {r.NAME} at physical memory '
                    f'{self.mapping["address"]} of size 0x1000000\n')
        if argv[:2] == ['systemctl', 'show']:
            return self.effective_guard
        if argv[:2] == ['systemctl', 'list-units']:
            return self.active
        if argv[:2] == ['systemctl', 'list-jobs']:
            return self.jobs
        raise AssertionError('Unexpected external command: ' + repr(argv))

    def snapshot(self):
        return {str(p.relative_to(self.tmp)): p.read_bytes()
                for p in self.tmp.rglob('*') if p.is_file()}

    def change_state(self, **updates):
        self.state.update(updates)
        self.put(r.ROOT / 'state.json', json.dumps(self.state))

    def test_read_only_collection_with_changed_symbol_and_blank_function_lines(self):
        before = self.snapshot()
        info, files = m.collect()
        self.assertEqual(self.snapshot(), before)
        self.assertTrue(info['marker_retained'])
        self.assertFalse(info['trace_cmd_available'])
        self.assertEqual(files['marker-recovered.txt'].decode(), RECOVERED)
        self.assertIn(b'nv_set_system_power_state.part.0', files['available-function-targets.txt'])
        self.assertNotIn(b'unrelated_function', files['available-function-targets.txt'])
        self.assertEqual(len(info['event_ids']), 6)
        self.assertNotIn('ffffffff8100', json.dumps(info))

    def test_verify_must_have_passed_in_current_boot(self):
        baseline = copy.deepcopy(self.state)
        for change in ({'phase': 'not-retained'}, {'verification': {}},
                       {'verification': {**baseline['verification'], 'boot': 'another-boot'}},
                       {'verification': {**baseline['verification'], 'same_record': False}},
                       {'verification': {**baseline['verification'], 'retained': False}}):
            with self.subTest(change=change):
                self.state = copy.deepcopy(baseline)
                self.change_state(**change)
                with self.assertRaisesRegex(RuntimeError, 'corrected retention verify'):
                    m.collect()

    def test_requires_unchanged_live_marker_not_just_state(self):
        for content in ('# empty trace', RECOVERED.replace('647.057509', '647.057510'),
                        RECOVERED + RECOVERED):
            with self.subTest(content=content):
                self.put(self.instance / 'trace', content)
                with self.assertRaisesRegex(RuntimeError, 'marker is no longer intact'):
                    m.collect()

    def test_mapping_mismatch_refused(self):
        self.mapping = {**self.mapping, 'address': '0x646000000'}
        with self.assertRaisesRegex(RuntimeError, 'mapping changed'):
            m.collect()

    def test_active_trace_controls_refused_without_resetting(self):
        for name, value in (('tracing_on', '1'), ('current_tracer', 'function'),
                            ('events/enable', '1'), ('options/trace_printk_dest', '1'),
                            ('events/power/suspend_resume/trigger', 'traceon')):
            path = self.instance / name
            original = path.read_bytes()
            self.put(path, value)
            with self.subTest(name=name), self.assertRaises(RuntimeError):
                m.collect()
            self.assertEqual(path.read_text(), value)
            self.put(path, original)

    def test_missing_guard_and_active_lab_refused(self):
        self.effective_guard = ''
        with self.assertRaisesRegex(RuntimeError, 'guard'):
            m.collect()
        self.effective_guard = 'egpu-sleep-guard.sh ignore_errors=no /etc/egpu-nvidia/egpu-sleep-guard.sh'
        self.active = 'egpu-sleep-lab-example.service active running'
        with self.assertRaisesRegex(RuntimeError, 'unit is active'):
            m.collect()

    def test_pending_deployment_refused(self):
        self.pending = True
        with self.assertRaisesRegex(RuntimeError, 'Pending'):
            m.collect()

    def test_hidden_or_duplicate_symbols_refused(self):
        path = m.PROC_ROOT / 'kallsyms'
        original = path.read_text()
        for content in (original.replace('ffffffff81000001', '0000000000000000'),
                        original + '\nffffffff81000001 T _text\n'):
            self.put(path, content)
            with self.assertRaisesRegex(RuntimeError, 'Symbol address'):
                m.collect()

    def test_duplicate_event_ids_refused(self):
        path = r.TRACEFS / 'events/ftrace/function/format'
        self.put(path, path.read_text().replace('ID: 6', 'ID: 1'))
        with self.assertRaisesRegex(RuntimeError, 'IDs are not unique'):
            m.collect()

    def test_unreviewed_pointer_string_abi_refused(self):
        path = r.TRACEFS / 'events/power/suspend_resume/format'
        self.put(path, path.read_text().replace('const char * action', '__data_loc char[] action'))
        with self.assertRaisesRegex(RuntimeError, 'action format differs'):
            m.collect()

    def test_unreviewed_device_string_abi_refused(self):
        path = r.TRACEFS / 'events/power/device_pm_callback_end/format'
        self.put(path, path.read_text().replace('__data_loc char[] device', 'const char * device'))
        with self.assertRaisesRegex(RuntimeError, 'not an inline string'):
            m.collect()

    def test_missing_last_boot_metadata_refused(self):
        self.put(self.instance / 'last_boot_info', '')
        with self.assertRaisesRegex(RuntimeError, 'last_boot_info is empty'):
            m.collect()

    def test_zero_previous_kernel_base_is_not_decode_confirmation(self):
        info = m.last_boot_summary(b'0\t[kernel]\n')
        self.assertFalse(info['previous_kernel_base_available'])
        self.assertEqual(info['previous_module_count'], 0)
        self.assertIn('cannot correct', info['state'])

    def test_previous_base_and_modules_inventory_does_not_publish_addresses(self):
        info = m.last_boot_summary(b'ffffffff81000000 [kernel]\nffffffffc0000000 nvidia\n')
        self.assertTrue(info['previous_kernel_base_available'])
        self.assertEqual(info['previous_module_count'], 1)
        self.assertNotIn('ffffffff', json.dumps(info))

    def test_current_unknown_and_duplicate_boot_map_are_not_previous_base(self):
        for data in (b'# Current\n', b'unrecognized\n',
                     b'ffffffff81000000 [kernel]\nffffffff82000000 [kernel]\n'):
            self.assertFalse(m.last_boot_summary(data)['previous_kernel_base_available'])

    def test_missing_stats_refused(self):
        for path in self.instance.glob('per_cpu/cpu*/stats'):
            path.unlink()
        with self.assertRaisesRegex(RuntimeError, 'No per-CPU'):
            m.collect()

    def test_required_function_missing_refused(self):
        self.put(r.TRACEFS / 'available_filter_functions', 'unrelated_function\n')
        with self.assertRaisesRegex(RuntimeError, 'trace targets'):
            m.collect()

    def test_read_and_bundle_bounds(self):
        with self.assertRaisesRegex(RuntimeError, 'exceeds bound'):
            m.read(self.instance / 'trace', 1)
        with patch.object(m, 'MAX_BUNDLE', 1), self.assertRaisesRegex(RuntimeError, 'size bound'):
            m.collect()

    def test_module_reference_changes_are_not_relocations(self):
        first = b'nvidia 100 1 - Live 0xffffffffa0000000\n'
        second = b'nvidia 100 9 nvidia_drm, Live 0xffffffffa0000000\n'
        self.assertEqual(m.module_layout(first), m.module_layout(second))
        with self.assertRaisesRegex(RuntimeError, 'modules record'):
            m.module_layout(b'incomplete\n')

    def test_module_relocation_during_collection_refused(self):
        original_read = m.read
        def changing(path, *args):
            result = original_read(path, *args)
            if path == m.PROC_ROOT / 'modules':
                self.put(path, result.replace(b'0xffffffffa1000000', b'0xffffffffa2000000'))
            return result
        with patch.object(m, 'read', changing), self.assertRaisesRegex(RuntimeError, 'Module layout changed'):
            m.collect()

    def test_changing_event_format_refused(self):
        original_read = m.read
        def changing(path, *args):
            result = original_read(path, *args)
            if path == r.TRACEFS / 'events/ftrace/print/format':
                self.put(path, result + b'\n')
            return result
        with patch.object(m, 'read', changing), self.assertRaisesRegex(RuntimeError, 'formats changed'):
            m.collect()

    def test_changed_trace_refused(self):
        original_read = m.read
        def changing(path, *args):
            result = original_read(path, *args)
            if path == self.instance / 'trace':
                self.put(path, result + b'\n')
            return result
        with patch.object(m, 'read', changing), self.assertRaisesRegex(RuntimeError, 'Trace changed'):
            m.collect()

    def test_changed_ownership_refused(self):
        original_read = m.read
        def changing(path, *args):
            result = original_read(path, *args)
            if path == self.instance / 'last_boot_info':
                self.change_state(phase='cancelled')
            return result
        with patch.object(m, 'read', changing), self.assertRaisesRegex(RuntimeError, 'state changed'):
            m.collect()

    def test_bundle_permissions_hashes_no_trace_or_state_mutations(self):
        before = self.snapshot()
        folder = m.bundle()
        manifest = json.loads((folder / 'manifest.json').read_text())
        for name, desc in manifest['files'].items():
            contents = (folder / name).read_bytes()
            self.assertEqual(len(contents), desc['bytes'])
            self.assertEqual(hashlib.sha256(contents).hexdigest(), desc['sha256'])
        for path in [folder, *folder.rglob('*')]:
            self.assertEqual(path.stat().st_mode & 0o777, 0o700 if path.is_dir() else 0o600)
        for name, contents in before.items():
            self.assertEqual((self.tmp / name).read_bytes(), contents)
        self.assertEqual(json.loads((r.ROOT / 'state.json').read_text()), self.state)
        self.assertIn('NO SLEEP', self.output.getvalue())
        other = m.bundle()
        self.assertNotEqual(folder, other)
        self.assertEqual(json.loads((folder / 'manifest.json').read_text()), manifest)

    def test_bundle_refuses_public_or_symlink_log_root(self):
        m.LOG_ROOT.mkdir(mode=0o755)
        with self.assertRaisesRegex(RuntimeError, 'Unsafe diagnostic state path'):
            m.bundle()
        m.LOG_ROOT.rmdir()
        m.LOG_ROOT.symlink_to(r.ROOT, target_is_directory=True)
        with self.assertRaisesRegex(RuntimeError, 'Unsafe diagnostic state path'):
            m.bundle()

    def test_partial_bundle_retained_without_success_manifest(self):
        original = m.write_private
        def failing(path, data):
            if path.name == 'manifest.json':
                raise OSError('disk full')
            original(path, data)
        with patch.object(m, 'write_private', failing), self.assertRaisesRegex(OSError, 'disk full'):
            m.bundle()
        self.assertIn('INCOMPLETE', self.output.getvalue())
        folder, = m.LOG_ROOT.glob('persistent-meta-*')
        self.assertTrue((folder / 'marker-recovered.txt').is_file())
        self.assertFalse((folder / 'manifest.json').exists())

    def test_private_writer_never_overwrites_or_follows_symlink(self):
        target = self.tmp / 'private-output'
        m.write_private(target, b'first')
        with self.assertRaises(FileExistsError):
            m.write_private(target, b'second')
        link = self.tmp / 'link'
        link.symlink_to(target)
        with self.assertRaises(OSError):
            m.write_private(link, b'second')
        self.assertEqual(target.read_bytes(), b'first')

    def test_cli_check_is_read_only(self):
        before = self.snapshot()
        # Only this CLI-root simulation bypasses secure(); other tests use actual
        # temp-file ownership/modes with the real (unprivileged) effective UID.
        with patch.object(sys, 'argv', ['tool', 'check']), patch.object(m.os, 'geteuid', return_value=0), \
                patch.object(r, 'secure'), patch.object(m, 'collect', wraps=m.collect):
            m.main()
        self.assertEqual(before, self.snapshot())
        self.assertIn('READ-ONLY METADATA CHECK PASSED', self.output.getvalue())

    def test_cli_refuses_unprivileged_call(self):
        with patch.object(sys, 'argv', ['tool', 'bundle']), patch.object(m.os, 'geteuid', return_value=1000), \
                self.assertRaisesRegex(RuntimeError, 'sudo'):
            m.main()
        self.assertFalse(m.LOG_ROOT.exists())

    def test_cli_exposes_no_run_or_sleep(self):
        for action in ('run', 'sleep', 'mark', 'stage', 'record'):
            with self.subTest(action=action), patch.object(sys, 'argv', ['tool', action]), \
                    contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                m.main()
            self.assertEqual(error.exception.code, 2)


if __name__ == '__main__':
    unittest.main()
