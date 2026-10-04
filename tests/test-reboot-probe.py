"""Read-only reboot gate tests; all root/trace interfaces are fixtures."""
import contextlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1] / 'diagnostics'
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location('egpu_reboot_probe_test', ROOT / 'egpu_reboot_probe.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class RebootGateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.log = self.root / 'log'
        self.log.mkdir(mode=0o700)
        self.folder = self.log / 'persistent-probe-example'
        (self.folder / 'capture').mkdir(parents=True, mode=0o700)
        self.instance = self.root / 'instance'
        self.instance.mkdir()
        self.boot = self.root / 'boot-id'
        self.boot.write_text('boot-1\n')
        self.state = {'phase': 'probe-exported', 'kernel': {'release': 'test'},
                      'mapping': {'address': '0x1000', 'size': 16777216},
                      'probe': {'boot': 'boot-1', 'folder': str(self.folder),
                                'token': 'EGPU_CAPTURE_' + 'a' * 32, 'pid': 77,
                                'result': {'raw_export_complete': True, 'error': None,
                                           'cleanup_errors': [], 'sleep_requested': False}}}
        (self.folder / 'result.json').write_text(json.dumps(self.state['probe']['result']))
        self.manifest = {'kind': 'read-raw-probe', 'boot': 'boot-1', 'raw_complete': True,
                         'token': self.state['probe']['token'], 'pid': 77}
        (self.folder / 'capture/manifest.json').write_text(json.dumps(self.manifest))
        (self.instance / 'trace').write_text('# tracer: nop\n# no records\n')
        for name in (*m.probe.CONTROL_FILES, m.probe.EVENT + '/filter',
                     m.probe.EVENT + '/enable'):
            path = self.instance / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('none\n' if name.endswith('/filter') else '0\n'
                            if name.endswith('/enable') else '\n')
        self.patches = [
            patch.object(m.metadata, 'LOG_ROOT', self.log),
            patch.object(m.retention, 'BOOT_ID', self.boot),
            patch.object(m.retention, 'locked', contextlib.nullcontext),
            patch.object(m.retention, 'load_state', return_value=self.state),
            patch.object(m.retention, 'secure'),
            patch.object(m.retention, 'validate_live'),
            patch.object(m.retention, 'instance_state', return_value=(self.instance, self.state['mapping'])),
            patch.object(m.probe, 'cpu_stats', return_value={0: {'entries': 0}}),
            patch.object(m.probe, 'no_loss'),
            patch.object(m.probe, 'global_state', return_value={'current_tracer': 'nop',
                                                                'events/enable': '0'}),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def test_clean_export_and_empty_reserved_instance(self):
        info = m.check()
        self.assertEqual(info['cpu_buffers'], 1)
        self.assertEqual(info['boot'], 'boot-1')

    def test_unfinished_previous_probe_refused(self):
        self.state['phase'] = 'probe-failed'
        with self.assertRaisesRegex(RuntimeError, 'not completed'):
            m.check()

    def test_changed_capture_manifest_refused(self):
        self.manifest['pid'] = 78
        (self.folder / 'capture/manifest.json').write_text(json.dumps(self.manifest))
        with self.assertRaisesRegex(RuntimeError, 'not the exported generation'):
            m.check()

    def test_nonempty_trace_refused(self):
        (self.instance / 'trace').write_text('process-77 [000] ...1. 1.0: print: still-here\n')
        with self.assertRaisesRegex(RuntimeError, 'still contains records'):
            m.check()

    def test_existing_filter_refused(self):
        (self.instance / 'set_ftrace_filter').write_text('vfs_read\n')
        with self.assertRaisesRegex(RuntimeError, 'Existing set_ftrace_filter'):
            m.check()

    def test_arm_archives_before_one_record_and_leaves_tracing_off(self):
        folder = self.root / 'persistent-reboot-probe-next'
        folder.mkdir(mode=0o700)
        info = {'boot': 'boot-1', 'kernel': self.state['kernel'],
                'mapping': self.state['mapping']}
        recorded = []

        def record(_instance, pid, token):
            self.assertEqual(self.state['phase'], 'reboot-probe-preparing')
            self.assertEqual(recorded, ['before'])
            self.assertEqual(pid, os.getpid())
            self.assertTrue(token.startswith('EGPU_CAPTURE_'))
            recorded.append('record')

        def save_tree(path, _info, _files):
            recorded.append(path.name)

        with patch.object(m, '_check_locked', return_value=info), \
                patch.object(m, '_fresh_folder', return_value=folder), \
                patch.object(m, 'baseline_files', return_value={
                    'trace-before.txt': (self.instance / 'trace').read_bytes()}), \
                patch.object(m.retention, 'save_state'), \
                patch.object(m.probe, 'save_tree', side_effect=save_tree), \
                patch.object(m.probe, 'record', side_effect=record), \
                patch.object(m.probe, 'cpu_stats', return_value={0: {'entries': 18, 'read events': 0}}), \
                patch.object(m.probe, 'capture_files', return_value={'trace.txt': b'fake trace'}), \
                patch.object(m.retention, 'marker_record', return_value={
                    'pid': 77, 'cpu': 0, 'timestamp': '1.0'}):
            m.arm()
        self.assertEqual(recorded, ['before', 'record', 'armed'])
        self.assertEqual(self.state['phase'], 'reboot-probe-armed')
        self.assertEqual((self.instance / 'tracing_on').read_text(), '0\n')
        self.assertEqual((self.instance / m.probe.EVENT / 'enable').read_text(), '0\n')

    def test_failed_arm_cannot_silently_retry(self):
        folder = self.root / 'persistent-reboot-probe-next'
        folder.mkdir(mode=0o700)
        info = {'boot': 'boot-1', 'kernel': self.state['kernel'],
                'mapping': self.state['mapping']}
        with patch.object(m, '_check_locked', return_value=info), \
                patch.object(m, '_fresh_folder', return_value=folder), \
                patch.object(m, 'baseline_files', return_value={
                    'trace-before.txt': (self.instance / 'trace').read_bytes()}), \
                patch.object(m.retention, 'save_state'), \
                patch.object(m.probe, 'save_tree'), \
                patch.object(m.probe, 'record', side_effect=RuntimeError('injected failure')), \
                patch.object(m.probe, 'cpu_stats', return_value={0: {'entries': 0}}):
            with self.assertRaisesRegex(RuntimeError, 'Arm incomplete'):
                m.arm()
        self.assertEqual(self.state['phase'], 'reboot-probe-failed')
        self.assertEqual(self.state['reboot_probe']['error'], 'injected failure')

    def recovered_fixture(self):
        folder = self.log / 'persistent-reboot-probe-next'
        folder.mkdir(mode=0o700)
        token = 'EGPU_CAPTURE_' + 'b' * 32
        marker = {'pid': 77, 'cpu': 0, 'timestamp': '1.000000'}
        self.state.update(phase='reboot-probe-armed', reboot_probe={
            'boot': 'boot-1', 'folder': str(folder), 'pid': 77, 'token': token,
            'mapping': self.state['mapping'],
            'markers': {'_BEGIN': marker, '_END': marker}})
        self.boot.write_text('boot-2\n')
        (self.instance / 'trace').write_text(
            f'python3-77 [000] ...1. 1.000000: wrong_symbol: {token}_BEGIN\n'
            f'python3-77 [000] ...1. 1.000000: wrong_symbol: {token}_END\n')
        return folder, {'schema': 1, 'boot': 'boot-1', 'pid': 77,
                        'token': token, 'marker_retained': True}

    def test_recovered_markers_required_before_raw_consumption(self):
        folder, manifest = self.recovered_fixture()
        (self.instance / 'trace').write_text('# marker absent\n')
        with patch.object(m, '_load_armed', return_value=(manifest, {})), \
                patch.object(m.retention, 'save_state'), \
                patch.object(m.probe, 'export_raw') as export:
            with self.assertRaisesRegex(RuntimeError, 'did not survive'):
                m.verify()
            export.assert_not_called()
        self.assertEqual(self.state['phase'], 'reboot-probe-not-retained')
        self.assertFalse(self.state['reboot_probe']['verification']['raw_consumed'])
        self.assertFalse((folder / 'capture').exists())
        self.assertEqual((folder / 'recovered/trace.txt').read_text(), '# marker absent\n')

    def test_recovery_snapshot_is_idempotent_but_cannot_be_overwritten(self):
        folder, _ = self.recovered_fixture()
        text = (self.instance / 'trace').read_bytes()
        m.archive_recovered_text(folder, 'boot-2', text)
        m.archive_recovered_text(folder, 'boot-2', text)
        with self.assertRaisesRegex(RuntimeError, 'snapshot differs'):
            m.archive_recovered_text(folder, 'boot-3', text)
        self.assertEqual((folder / 'recovered/boot-id.txt').read_text(), 'boot-2\n')

    def test_verified_generation_exports_then_becomes_single_use(self):
        folder, manifest = self.recovered_fixture()
        files = {'events/header_page': b'fixture header',
                 'saved_cmdlines.txt': b'77 probe-read\n'}

        def export(_instance, path, cpus):
            self.assertEqual(self.state['phase'], 'reboot-probe-verifying')
            self.assertEqual(cpus, [0])
            path.mkdir(mode=0o700)
            (path / 'cpu0').write_bytes(b'R' * 4096)

        with patch.object(m, '_load_armed', return_value=(manifest, files)), \
                patch.object(m.retention, 'save_state'), \
                patch.object(m.probe, 'cpu_stats', side_effect=[
                    {0: {'entries': 18, 'read events': 0}},
                    {0: {'entries': 0, 'read events': 18}}]), \
                patch.object(m.probe, 'export_raw', side_effect=export):
            m.verify()
        self.assertEqual(self.state['phase'], 'reboot-probe-exported')
        self.assertTrue((folder / 'capture/raw-manifest.json').is_file())
        self.assertEqual(json.loads((folder / 'capture/manifest.json').read_text())['raw_complete'], True)


if __name__ == '__main__':
    unittest.main()
