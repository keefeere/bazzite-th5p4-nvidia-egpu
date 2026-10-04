"""NO hardware access: all host paths/subprocesses/calls replaced with fixtures."""
import contextlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch


HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('metadata_tests', HERE / 'test-persistent-trace.py')
base = importlib.util.module_from_spec(spec)
spec.loader.exec_module(base)
r, metadata = base.r, base.m
with patch.dict(sys.modules, {'egpu_trace_retention': r, 'egpu_persistent_trace': metadata}):
    probe = base.load('egpu_persistent_probe')


def stats(entries=0, reads=0, loss=0):
    return (f'entries: {entries}\noverrun: {loss}\ncommit overrun: 0\n'
            f'dropped events: 0\nread events: {reads}\n')


class ProbeTests(unittest.TestCase):
    mock = base.MetadataTests.mock
    put = base.MetadataTests.put
    command = base.MetadataTests.command
    snapshot = base.MetadataTests.snapshot

    def setUp(self):
        base.MetadataTests.setUp(self)
        metadata.LOG_ROOT.mkdir(mode=0o700)
        for name in probe.CONTROL_FILES:
            self.put(self.instance / name, '')
        self.put(self.instance / 'available_tracers', 'nop function')
        self.put(self.instance / 'tracing_cpumask', '3fff')
        self.put(self.instance / probe.EVENT / 'filter', 'none')
        self.put(self.instance / probe.EVENT / 'enable', '0')
        self.put(self.instance / 'options/function-fork', '0')
        path = r.TRACEFS / 'available_filter_functions'
        self.put(path, path.read_text() + '\n' + probe.FUNCTION + '\n')
        path = metadata.PROC_ROOT / 'kallsyms'
        self.put(path, path.read_text() + '\nffffffff82001000 T ' + probe.FUNCTION + '\n')
        self.put(r.TRACEFS / 'events/header_page',
                 'field: u64 timestamp; offset:0; size:8;\n'
                 'field: local_t commit; offset:8; size:8;\n'
                 'field: char data; offset:16; size:4080;\n')
        self.put(r.TRACEFS / probe.EVENT / 'format',
                 'name: sys_enter_read\nID: 100\nformat:\n'
                 'field: unsigned short common_type; offset:0; size:2; signed:0;\n')
        for cpu in range(14):
            self.put(self.instance / f'per_cpu/cpu{cpu}/buffer_meta', 'subbuf_size:   4096\n')
            self.put(self.instance / f'per_cpu/cpu{cpu}/stats', stats(entries=int(cpu == 13)))
            self.put(self.instance / f'per_cpu/cpu{cpu}/trace_pipe_raw', b'')
        for name, value in {'current_tracer': 'nop', 'events/enable': '0',
                            'tracing_on': '1', 'trace_clock': '[local] mono'}.items():
            self.put(r.TRACEFS / name, value)
        self.put(metadata.PROC_ROOT / 'sys/kernel/ftrace_enabled', '1')
        self.controls = []
        self.trace_count = 0
        self.mock(probe, 'control', self.control)
        self.mock(probe, 'generate_calls', self.calls)
        self.mock(probe, 'deadline', contextlib.nullcontext)
        self.real_export = probe.export_raw
        self.mock(probe, 'export_raw', self.export)

    def append(self, record):
        path = self.instance / 'trace'
        self.put(path, path.read_text() + record + '\n')
        self.trace_count += 1
        self.put(self.instance / 'per_cpu/cpu0/stats', stats(entries=self.trace_count))
        self.put(self.instance / 'per_cpu/cpu0/trace_pipe_raw', b'R' * probe.PAGE_SIZE)

    def calls(self, _fd):
        pid = int((self.instance / 'set_ftrace_pid').read_text().strip())
        for _ in range(probe.CALLS):
            self.append(f'python3-{pid} [000] ...1. 99.000001: vfs_read <-ksys_read')
            self.append(f'python3-{pid} [000] ...1. 99.000001: sys_read(fd: 7, buf: 0, count: 32)')

    def control(self, instance, name, value):
        self.assertEqual(instance, self.instance)
        # Old evidence and intent must already be durable before any write.
        folder = Path(r.load_state()['probe']['folder'])
        self.assertTrue((folder / 'before/manifest.json').is_file())
        self.assertEqual((folder / 'before/marker-recovered.txt').read_text(), base.RECOVERED)
        self.controls.append((name, value))
        self.put(instance / name, value + '\n')
        if name == probe.EVENT + '/enable':
            self.put(instance / 'events/enable', value)
        if name == 'current_tracer' or name == 'trace':
            self.put(instance / 'trace', '')
            self.put(instance / 'last_boot_info', '# Current\n')
            self.trace_count = 0
            for cpu in range(14):
                self.put(instance / f'per_cpu/cpu{cpu}/stats', stats())
        if name == 'trace_marker':
            pid = (instance / 'set_ftrace_pid').read_text().strip()
            self.append(f'python3-{pid} [000] ...1. 99.000000: tracing_mark_write: {value}')

    def export(self, instance, folder, cpus):
        self.assertEqual(probe.text(instance / 'tracing_on'), '0')
        self.assertTrue((folder.parent / 'kallsyms.txt').is_file())
        self.assertTrue((folder.parent / 'events/syscalls/sys_enter_read/format').is_file())
        result = self.real_export(instance, folder, cpus)
        for cpu, values in probe.cpu_stats(instance).items():
            self.put(instance / f'per_cpu/cpu{cpu}/stats', stats(reads=values['entries']))
        return result

    def test_preflight_is_read_only(self):
        before = self.snapshot()
        info, files, instance, controls = probe.preflight()
        self.assertEqual(before, self.snapshot())
        self.assertEqual(instance, self.instance)
        self.assertEqual(info['boot'], base.BOOT)
        self.assertIn(probe.EVENT + '/format', files)
        self.assertEqual(controls['tracing_on'], '1')
        self.assertEqual(self.controls, [])

    def test_prior_record_loss_and_reads_refused(self):
        path = self.instance / 'per_cpu/cpu13/stats'
        for value in (stats(2), stats(1, reads=1), stats(1, loss=1)):
            with self.subTest(value=value):
                self.put(path, value)
                with self.assertRaises(RuntimeError):
                    probe.preflight()
        self.assertFalse(self.controls)

    def test_missing_syscall_function_refused(self):
        path = r.TRACEFS / 'available_filter_functions'
        self.put(path, path.read_text().replace(probe.FUNCTION, 'different'))
        with self.assertRaisesRegex(RuntimeError, 'not traceable'):
            probe.preflight()

    def test_custom_filters_masks_options_and_global_recorders_refused(self):
        cases = [(self.instance / name, '123') for name in probe.CONTROL_FILES]
        cases += [(self.instance / probe.EVENT / 'filter', 'common_pid == 1'),
                  (self.instance / 'tracing_cpumask', '1'),
                  (self.instance / 'options/function-fork', '1'),
                  (r.TRACEFS / 'current_tracer', 'function'),
                  (r.TRACEFS / 'events/enable', 'X'),
                  (metadata.PROC_ROOT / 'sys/kernel/ftrace_enabled', '0')]
        for path, value in cases:
            with self.subTest(path=path):
                old = path.read_bytes()
                self.put(path, value)
                with self.assertRaises(RuntimeError):
                    probe.preflight()
                self.put(path, old)
        self.assertFalse(self.controls)

    def test_raw_layout_and_cpu_gaps_refused(self):
        path = self.instance / 'per_cpu/cpu0/buffer_meta'
        self.put(path, 'subbuf_size: 8192')
        with self.assertRaisesRegex(RuntimeError, 'subbuffers'):
            probe.preflight()
        self.put(path, 'subbuf_size: 4096')
        (self.instance / 'per_cpu/cpu1/stats').unlink()  # Temporary fixture only.
        with self.assertRaisesRegex(RuntimeError, 'CPU set'):
            probe.preflight()

    def test_preflight_rejects_low_disk_space(self):
        with patch.object(probe.shutil, 'disk_usage', return_value=type('Disk', (), {'free': 1})()):
            with self.assertRaisesRegex(RuntimeError, '128 MiB'):
                probe.preflight()

    def test_full_mock_run_archives_exports_stops_and_preserves_ownership(self):
        globals_before = probe.global_state()
        probe.run()
        state = r.load_state()
        self.assertEqual(state['phase'], 'probe-exported')
        self.assertEqual(state['verification'], self.state['verification'])
        self.assertEqual(state['owned_args'], list(r.ARGS))
        self.assertEqual(probe.global_state(), globals_before)
        self.assertEqual(probe.text(self.instance / 'tracing_on'), '0')
        self.assertEqual(probe.text(self.instance / 'current_tracer'), 'nop')
        folder = Path(state['probe']['folder'])
        capture = json.loads((folder / 'capture/manifest.json').read_text())
        self.assertTrue(capture['raw_complete'])
        self.assertEqual(capture['cpus'], list(range(14)))
        self.assertTrue((folder / 'capture/raw/cpu13').exists())  # Empty CPUs not dropped.
        self.assertEqual((folder / 'capture/raw/cpu0').stat().st_size, 4096)
        self.assertFalse(state['probe']['result']['offline_decode_validated'])
        self.assertTrue((folder / 'trace.txt').read_text().count('sys_read(') == 8)
        self.assertLess(self.controls.index(('set_ftrace_filter', probe.FUNCTION)),
                        self.controls.index(('current_tracer', 'function')))
        for path in folder.rglob('*'):
            self.assertFalse(path.stat().st_mode & 0o077)
        before_retry = list(self.controls)
        with self.assertRaisesRegex(RuntimeError, 'corrected retention verify'):
            probe.run()
        self.assertEqual(self.controls, before_retry)

    def test_archive_failure_performs_no_trace_write(self):
        with patch.object(probe, 'save_tree', side_effect=OSError('disk full')):
            with self.assertRaisesRegex(OSError, 'disk full'):
                probe.run()
        self.assertFalse(self.controls)
        self.assertEqual(r.load_state()['phase'], 'verified')

    def test_failed_export_turns_off_but_never_sets_nop_or_clears_new_evidence(self):
        with patch.object(probe, 'export_raw', side_effect=OSError('busy')):
            with self.assertRaisesRegex(RuntimeError, 'Probe incomplete'):
                probe.run()
        self.assertEqual(r.load_state()['phase'], 'probe-failed')
        self.assertEqual(probe.text(self.instance / 'tracing_on'), '0')
        self.assertEqual(probe.text(self.instance / 'current_tracer'), 'function')
        self.assertNotIn(('current_tracer', 'nop'), self.controls)
        self.assertIn('EGPU_CAPTURE_', (self.instance / 'trace').read_text())

    def test_failed_call_stops_recording_without_retry(self):
        with patch.object(probe, 'generate_calls', side_effect=RuntimeError('interrupted')) as generate:
            with self.assertRaisesRegex(RuntimeError, 'Probe incomplete'):
                probe.run()
        generate.assert_called_once()
        self.assertEqual(probe.text(self.instance / 'tracing_on'), '0')
        self.assertEqual(r.load_state()['phase'], 'probe-failed')

    def test_short_page_is_preserved_not_padded(self):
        path = self.instance / 'per_cpu/cpu0/trace_pipe_raw'
        self.put(path, b'partial')
        output = self.tmp / 'raw'
        with self.assertRaisesRegex(RuntimeError, 'Short raw page'):
            self.real_export(self.instance, output, [0])
        self.assertEqual((output / 'cpu0').read_bytes(), b'partial')

    def test_active_recorder_refused_before_consuming(self):
        self.put(self.instance / 'tracing_on', '1')
        with self.assertRaisesRegex(RuntimeError, 'running recorder'):
            self.real_export(self.instance, self.tmp / 'raw', [0])
        self.assertFalse((self.tmp / 'raw').exists())

    def test_raw_reader_nonblocking_eagain(self):
        with patch.object(probe.os, 'read', side_effect=BlockingIOError):
            with self.assertRaisesRegex(RuntimeError, 'No raw pages'):
                self.real_export(self.instance, self.tmp / 'raw', [0])

    def test_bad_pid_and_duplicate_markers_refused(self):
        token = 'EGPU_CAPTURE_' + 'a' * 32
        trace = (f'python3-7 [001] ...1. 1.0: tracing_mark_write: {token}_BEGIN\n'
                 f'python3-7 [001] ...1. 2.0: vfs_read <-ksys_read\n'
                 f'python3-7 [001] ...1. 3.0: sys_enter_read: fd=7\n'
                 f'python3-7 [001] ...1. 4.0: tracing_mark_write: {token}_END\n')
        probe.validate_text(trace, 7, token)
        for value, pid in ((trace, 8), (trace + trace, 7), (trace.replace('sys_enter_read:', 'other:'), 7)):
            with self.assertRaises(RuntimeError):
                probe.validate_text(value, pid, token)

    def test_byte_limit_keeps_partial_evidence(self):
        self.put(self.instance / 'per_cpu/cpu0/trace_pipe_raw', b'R' * 8192)
        with patch.object(probe, 'MAX_RAW', 4096), self.assertRaisesRegex(RuntimeError, 'byte bound'):
            self.real_export(self.instance, self.tmp / 'raw', [0])
        self.assertEqual((self.tmp / 'raw/cpu0').stat().st_size, 8192)

    def test_export_empty_and_expired_refused(self):
        with self.assertRaisesRegex(RuntimeError, 'No raw pages'):
            self.real_export(self.instance, self.tmp / 'empty-raw', [0])
        with patch.object(probe.time, 'monotonic', side_effect=[0, 11]):
            with self.assertRaisesRegex(RuntimeError, 'time bound'):
                self.real_export(self.instance, self.tmp / 'late-raw', [0])

    def test_cleanup_failure_not_reported_as_success(self):
        def fail_nop(instance, name, value):
            if name == 'current_tracer' and value == 'nop':
                raise OSError('cleanup busy')
            return self.control(instance, name, value)
        with patch.object(probe, 'control', fail_nop):
            with self.assertRaisesRegex(RuntimeError, 'Probe incomplete'):
                probe.run()
        state = r.load_state()
        self.assertEqual(state['phase'], 'probe-failed')
        self.assertTrue(state['probe']['result']['raw_export_complete'])
        self.assertIn('cleanup busy', state['probe']['result']['cleanup_errors'])

    def test_deadline_resets_timer_and_handlers(self):
        # Run the real context manager with fake signal APIs, never arm a timer.
        with patch.object(probe.signal, 'getitimer', return_value=(0.0, 0.0)), \
                patch.object(probe.signal, 'getsignal', return_value=123), \
                patch.object(probe.signal, 'signal') as handler, \
                patch.object(probe.signal, 'setitimer') as timer:
            # setUp replaced the function; get its original via a fresh import.
            with patch.dict(sys.modules, {'egpu_trace_retention': r, 'egpu_persistent_trace': metadata}):
                real = base.load('egpu_persistent_probe')
            with self.assertRaisesRegex(RuntimeError, 'interrupt'):
                with real.deadline():
                    raise RuntimeError('interrupt')
            self.assertEqual(timer.call_args_list[0].args, (probe.signal.ITIMER_REAL, 10))
            self.assertEqual(timer.call_args_list[-1].args, (probe.signal.ITIMER_REAL, 0))
            self.assertEqual(handler.call_count, 6)


if __name__ == '__main__':
    unittest.main()
