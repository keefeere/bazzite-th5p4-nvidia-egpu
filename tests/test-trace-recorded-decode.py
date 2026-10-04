"""Offline real-capture path tests with fake pages/reports, never live tracing."""
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location('offline_tests', Path(__file__).with_name('test-trace-offline-check.py'))
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)
m = fixture.m


class RecordedTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.bundle = self.root / 'bundle'
        self.bundle.mkdir(mode=0o700)
        self.files = fixture.formats()
        self.files['kallsyms.txt'] += b'4000 T vfs_read\n'
        self.files['saved_cmdlines.txt'] = b'77 probe-read\n'
        self.manifest = {'schema': 1, 'kind': 'read-raw-probe', 'marker_retained': True,
                         'boot': 'test-boot', 'pid': 77, 'token': 'EGPU_CAPTURE_' + 'a' * 32,
                         'cpus': list(range(12)), 'raw_complete': True,
                         'metadata_bytes': sum(map(len, self.files.values())),
                         'files': {name: {'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
                                   for name, data in self.files.items()}}
        for name, data in self.files.items():
            path = self.bundle / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        (self.bundle / 'manifest.json').write_text(json.dumps(self.manifest))
        (self.bundle / 'raw').mkdir()
        self.raw = {}
        for cpu in range(12):
            data = b'R' * 4096 if cpu == 10 else b''
            (self.bundle / f'raw/cpu{cpu}').write_bytes(data)
            self.raw[f'raw/cpu{cpu}'] = {'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
        self.raw_manifest = self.bundle / 'raw-manifest.json'
        self.raw_manifest.write_text(json.dumps(self.raw))
        token = self.manifest['token']
        self.report = (f'probe-read-77 [010] ...1. 1.0: tracing_mark_write: {token}_BEGIN\n'
                       + ('probe-read-77 [010] ...1. 1.1: function: vfs_read <-- ksys_read\n'
                          'probe-read-77 [010] ...1. 1.1: sys_enter_read: fd=7\n') * 8
                       + f'probe-read-77 [010] ...1. 1.2: tracing_mark_write: {token}_END\n')

    def test_kind_is_explicit_not_confused_with_synthetic_bundle(self):
        m.load_bundle(self.bundle, kind='read-raw-probe')
        with self.assertRaisesRegex(RuntimeError, 'Wrong bundle kind'):
            m.load_bundle(self.bundle)

    def test_all_cpus_in_numeric_order_and_empty_files_preserved(self):
        raw = m.recorded_raw(self.bundle, self.manifest)
        self.assertEqual(len(raw), 12)
        self.assertEqual(raw[10], b'R' * 4096)
        self.assertEqual(raw[1], b'')

    def test_cpu_gap_duplicates_and_no_completion_refused(self):
        for value in ({**self.manifest, 'cpus': [0, 2]}, {**self.manifest, 'cpus': [0, 0]},
                      {**self.manifest, 'raw_complete': False}, {**self.manifest, 'cpus': []}):
            with self.assertRaises(RuntimeError):
                m.recorded_raw(self.bundle, value)

    def test_tampered_or_short_raw_page_refused(self):
        path = self.bundle / 'raw/cpu10'
        for data in (b'S' * 4096, b'partial'):
            path.write_bytes(data)
            with self.assertRaises(RuntimeError):
                m.recorded_raw(self.bundle, self.manifest)
        self.raw['raw/cpu10']['bytes'] = 7
        self.raw_manifest.write_text(json.dumps(self.raw))
        with self.assertRaisesRegex(RuntimeError, 'partial pages'):
            m.recorded_raw(self.bundle, self.manifest)

    def test_unsafe_extra_raw_path_refused(self):
        self.raw['../unrelated'] = self.raw['raw/cpu10']
        self.raw_manifest.write_text(json.dumps(self.raw))
        with self.assertRaisesRegex(RuntimeError, 'file set'):
            m.recorded_raw(self.bundle, self.manifest)

    def test_symlink_refused(self):
        path = self.bundle / 'raw/cpu1'
        path.unlink()
        path.symlink_to('cpu0')
        with self.assertRaisesRegex(RuntimeError, 'unsafe'):
            m.recorded_raw(self.bundle, self.manifest)

    def test_report_requires_both_unique_markers_same_pid_function_and_event(self):
        m.validate_recorded_report(self.report, 77, self.manifest['token'])
        for report in (self.report.replace('-77 ', '-78 '), self.report + self.report,
                       self.report.replace('sys_enter_read:', 'other:', 1),
                       self.report.replace('vfs_read', 'unknown', 1),
                       self.report + 'LOST 5 EVENTS\n'):
            with self.assertRaises(RuntimeError):
                m.validate_recorded_report(report, 77, self.manifest['token'])

    def test_root_decode_refused(self):
        with patch.object(m.os, 'geteuid', return_value=0), self.assertRaisesRegex(RuntimeError, 'WITHOUT sudo'):
            m.decode_recorded(self.bundle, Path('/no/tool'))

    def test_only_saved_header_restore_report_commands(self):
        output = self.root / 'output'
        output.mkdir(mode=0o700)
        binary = self.root / 'fake-tool'
        binary.write_text('not executable')
        commands = []
        def command(tool, args, folder):
            self.assertEqual(tool, binary)
            self.assertEqual(folder, output)
            self.assertIn(args[0], ('restore', 'report'))
            commands.append(args)
            if args[0] == 'report':
                return (self.report.replace('vfs_read', 'EGPU_saved_vfs_read')
                        if len([item for item in commands if item[0] == 'report']) == 2
                        else self.report)
            return ''
        with patch.object(m.os, 'geteuid', return_value=1000), \
                patch.object(m, 'load_bundle', return_value=(self.manifest, self.files)), \
                patch.object(m.tempfile, 'mkdtemp', return_value=str(output)), \
                patch.object(m, 'run_tool', command):
            result = m.decode_recorded(self.bundle, binary)
        self.assertTrue(result['real_capture_decoded'])
        self.assertTrue(result['saved_metadata_controls_decoding'])
        self.assertEqual(commands[1][5:], [output / f'cpu{cpu}' for cpu in range(12)])
        self.assertEqual((output / 'metadata/kallsyms').read_bytes(), self.files['kallsyms.txt'])
        self.assertIn(b'EGPU_saved_vfs_read', (output / 'control-metadata/kallsyms').read_bytes())

    def test_control_must_change_real_function_resolution(self):
        output = self.root / 'output'
        output.mkdir(mode=0o700)
        binary = self.root / 'fake-tool'
        binary.write_text('not executable')
        with patch.object(m.os, 'geteuid', return_value=1000), \
                patch.object(m, 'load_bundle', return_value=(self.manifest, self.files)), \
                patch.object(m.tempfile, 'mkdtemp', return_value=str(output)), \
                patch.object(m, 'run_tool', side_effect=lambda _tool, args, _folder:
                             self.report if args[0] == 'report' else ''):
            with self.assertRaisesRegex(RuntimeError, 'saved-symbol control'):
                m.decode_recorded(self.bundle, binary)


if __name__ == '__main__':
    unittest.main()
