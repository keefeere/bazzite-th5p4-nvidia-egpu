"""No hardware, privileged state, external decoder or real subprocesses."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location('offline', Path(__file__).resolve().parents[1]
                                            / 'diagnostics/egpu-trace-offline-check.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def formats():
    layouts = {
        'ftrace/print': [('unsigned long ip', 8, 8), ('char buf[]', 16, 0)],
        'ftrace/function': [('unsigned long ip', 8, 8), ('unsigned long parent_ip', 16, 8)],
        'power/suspend_resume': [('const char * action', 8, 8), ('int val', 16, 4), ('bool start', 20, 1)],
        'power/device_pm_callback_start': [('__data_loc char[] device', 8, 4),
            ('__data_loc char[] driver', 12, 4), ('__data_loc char[] parent', 16, 4),
            ('__data_loc char[] pm_ops', 20, 4), ('int event', 24, 4)],
        'power/device_pm_callback_end': [('__data_loc char[] device', 8, 4),
            ('__data_loc char[] driver', 12, 4), ('int error', 16, 4)],
    }
    result = {}
    for number, (key, fields) in enumerate(layouts.items(), 1):
        fields = [('unsigned short common_type', 0, 2), ('unsigned char common_flags', 2, 1),
                  ('unsigned char common_preempt_count', 3, 1), ('int common_pid', 4, 4), *fields]
        result[f'events/{key}/format'] = (f'name: {key.split("/")[-1]}\nID: {number}\nformat:\n' + ''.join(
            f'field:{field}; offset:{offset}; size:{size}; signed:0;\n'
            for field, offset, size in fields)).encode()
    result['events/header_page'] = (b'field:u64 timestamp; offset:0; size:8;\n'
                                   b'field:local_t commit; offset:8; size:8;\n'
                                   b'field:char data; offset:16; size:4080;\n')
    result['kallsyms.txt'] = (b'1000 T tracing_mark_write\n2000 T nv_pm_notifier [nvidia]\n'
                            b'3000 T pm_notifier_call_chain_robust\n')
    result['printk_formats.txt'] = b'0x4000 : "dpm_suspend_noirq"\n'
    return result


class OfflineTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.bundle = self.root / 'bundle'
        self.bundle.mkdir(mode=0o700)
        self.files = formats()
        self.manifest = {'schema': 1, 'marker_retained': True, 'boot': 'fixture-boot',
                         'metadata_bytes': sum(map(len, self.files.values())), 'files': {}}
        for name, data in self.files.items():
            path = self.bundle / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            self.manifest['files'][name] = {'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
        self.save_manifest()
        blocker = patch.object(m.subprocess, 'run', side_effect=AssertionError('No real subprocess'))
        blocker.start()
        self.addCleanup(blocker.stop)

    def save_manifest(self):
        (self.bundle / 'manifest.json').write_text(json.dumps(self.manifest))

    def test_load_valid_hashes_without_changing_inputs(self):
        before = {p: p.read_bytes() for p in self.bundle.rglob('*') if p.is_file()}
        info, files = m.load_bundle(self.bundle)
        self.assertEqual(files, self.files)
        self.assertEqual(info['boot'], 'fixture-boot')
        self.assertEqual(before, {p: p.read_bytes() for p in self.bundle.rglob('*') if p.is_file()})

    def test_modified_contents_rejected(self):
        path = self.bundle / 'kallsyms.txt'
        path.write_bytes(path.read_bytes().replace(b'1000', b'9999'))
        with self.assertRaisesRegex(RuntimeError, 'Hash mismatch'):
            m.load_bundle(self.bundle)

    def test_manifest_traversal_rejected(self):
        self.manifest['files']['../outside'] = {'bytes': 0, 'sha256': hashlib.sha256(b'').hexdigest()}
        self.save_manifest()
        with self.assertRaisesRegex(RuntimeError, 'Unsafe manifest path'):
            m.load_bundle(self.bundle)

    def test_symlink_input_rejected(self):
        path = self.bundle / 'kallsyms.txt'
        path.unlink()
        path.symlink_to('/proc/kallsyms')
        with self.assertRaisesRegex(RuntimeError, 'Symlink'):
            m.load_bundle(self.bundle)

    def test_public_directory_rejected(self):
        self.bundle.chmod(0o755)
        with self.assertRaisesRegex(RuntimeError, 'private'):
            m.load_bundle(self.bundle)

    def test_metadata_bounds_and_total(self):
        self.manifest['metadata_bytes'] += 1
        self.save_manifest()
        with self.assertRaisesRegex(RuntimeError, 'Bundle total'):
            m.load_bundle(self.bundle)
        self.manifest['files']['kallsyms.txt']['bytes'] = 33 * 1024 * 1024
        self.save_manifest()
        with self.assertRaisesRegex(RuntimeError, 'Oversize metadata file'):
            m.load_bundle(self.bundle)

    def test_synthetic_page_contains_all_five_record_types(self):
        raw = m.fixtures(self.files)
        self.assertEqual(len(raw), 4096)
        timestamp, committed = struct.unpack_from('<QQ', raw)
        self.assertEqual(timestamp, 1_000_000_000)
        position = 16
        event_types = []
        while position < committed + 16:
            header, = struct.unpack_from('<I', raw, position)
            self.assertEqual(header >> 5, 1_000_000)
            payload_size = (header & 31) * 4
            event_type, = struct.unpack_from('<H', raw, position + 4)
            event_types.append(event_type)
            position += 4 + payload_size
        self.assertEqual(position, 16 + committed)
        self.assertEqual(event_types, [1, 2, 3, 4, 5])
        self.assertEqual(raw[position:], bytes(4096 - position))
        self.assertIn(b'EGPU_OFFLINE_SYNTHETIC_ONLY', raw)

    def test_unknown_record_or_page_layout_refused(self):
        for name, old, new in (
            ('events/header_page', b'size:4080', b'size:8160'),
            ('events/ftrace/print/format', b'offset:8', b'offset:12')):
            files = dict(self.files)
            files[name] = files[name].replace(old, new)
            with self.subTest(name=name), self.assertRaisesRegex(RuntimeError, 'layout'):
                m.fixtures(files)

    def test_unknown_pm_phase_pointer_refused(self):
        self.files['printk_formats.txt'] = b'0x4000 : "another-phase"\n'
        with self.assertRaisesRegex(RuntimeError, 'saved pointer'):
            m.fixtures(self.files)

    def test_missing_or_ambiguous_function_refused(self):
        for contents in (b'', b'0 T nv_pm_notifier\n', b'1000 T nv_pm_notifier\n2000 T nv_pm_notifier\n'):
            with self.assertRaisesRegex(RuntimeError, 'Unavailable symbol'):
                m.unique_symbol(contents, 'nv_pm_notifier')

    def test_oversize_ring_record_refused(self):
        with self.assertRaisesRegex(RuntimeError, 'large'):
            m.page([bytes(116)])

    def test_check_never_runs_as_root(self):
        with patch.object(m.os, 'geteuid', return_value=0), self.assertRaisesRegex(RuntimeError, 'WITHOUT sudo'):
            m.check(self.bundle, Path('/not/a/tool'))

    def test_check_uses_only_offline_restore_and_report(self):
        binary = self.root / 'fake-trace-cmd'
        binary.write_text('not executable')
        output = self.root / 'output'
        output.mkdir(mode=0o700)
        report = ('tracing_mark_write: EGPU_OFFLINE_SYNTHETIC_ONLY\n'
                  'nv_pm_notifier <-- pm_notifier_call_chain_robust\n'
                  'dpm_suspend_noirq[0] begin\n'
                  'nvidia 0000:03:00.0, parent: 0000:02:00.0, fixture_noirq[suspend]\n'
                  'nvidia 0000:03:00.0, err=-16\n')
        commands = []
        def decoder(tool, args, folder):
            self.assertEqual(tool, binary)
            self.assertEqual(folder, output)
            commands.append(args)
            if args[0] == 'report':
                if args[-1].name == 'control.dat':
                    return report.replace('nv_pm_notifier', 'FIXTURE_saved_nv_notifier').replace(
                        'dpm_suspend_noirq', 'FIXTURE_saved_phase')
                return report
            self.assertEqual(args[0], 'restore')
            return ''
        # Skip only ownership validation during fake-root testing in CI; load_bundle
        # security is exercised with actual temp paths by the preceding tests.
        with patch.object(m.os, 'geteuid', return_value=1000), \
                patch.object(m, 'load_bundle', return_value=(self.manifest, self.files)), \
                patch.object(m.tempfile, 'mkdtemp', return_value=str(output)), \
                patch.object(m, 'run_tool', decoder):
            result = m.check(self.bundle, binary)
        self.assertTrue(result['synthetic_decode_passed'])
        self.assertTrue(result['saved_metadata_controls_decoding'])
        self.assertEqual([args[0] for args in commands], ['restore', 'restore', 'report'] * 2)
        self.assertEqual((self.bundle / 'kallsyms.txt').read_bytes(), self.files['kallsyms.txt'])
        self.assertIn(b'FIXTURE_saved_nv_notifier', (output / 'control-metadata/kallsyms').read_bytes())


if __name__ == '__main__':
    unittest.main()
