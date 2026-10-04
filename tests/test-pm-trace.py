"""Fake tracefs only. Never activates host tracing, services, or sleep."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location('pmtrace', Path(__file__).resolve().parents[1]
                                            / 'diagnostics/egpu_pm_trace.py')
trace = importlib.util.module_from_spec(spec)
spec.loader.exec_module(trace)


class TraceTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.fs = self.root / 'tracefs'
        self.fs.mkdir()
        self.logs = self.root / 'logs'
        self.logs.mkdir()
        (self.fs / 'instances').mkdir()
        (self.fs / 'events').mkdir()
        for name, value in {
            'available_events': '\n'.join(trace.EVENTS),
            'available_filter_functions': '\n'.join(trace.FUNCTIONS),
            'available_tracers': 'function nop', 'trace_clock': '[local] mono',
            'current_tracer': 'function_graph', 'tracing_on': '1', 'events/enable': 'X',
        }.items():
            (self.fs / name).write_text(value)
        self.baseline = {str(p): p.read_bytes() for p in self.fs.rglob('*') if p.is_file()}
        p = patch.object(trace, 'TRACEFS', self.fs)
        p.start()
        self.addCleanup(p.stop)

    def fake_instance(self, capture):
        """Populate virtual entries when the collector creates its instance."""
        mkdir = Path.mkdir
        def create(path, *args, **kwargs):
            result = mkdir(path, *args, **kwargs)
            if path == capture.instance and not kwargs.get('exist_ok', False):
                for name in ('tracing_on', 'current_tracer', 'buffer_size_kb',
                             'trace_clock', 'set_ftrace_filter', 'trace_pipe',
                             'trace_marker', 'events/enable', 'per_cpu/cpu0/stats'):
                    item = path / name
                    item.parent.mkdir(parents=True, exist_ok=True)
                    item.write_text('0\n')
                for event in trace.EVENTS:
                    item = path / 'events' / event.replace(':', '/') / 'enable'
                    item.parent.mkdir(parents=True, exist_ok=True)
                    item.write_text('0\n')
            return result
        return patch.object(Path, 'mkdir', create)

    def assert_global_unchanged(self):
        for path, value in self.baseline.items():
            self.assertEqual(Path(path).read_bytes(), value)

    def test_check_is_read_only_even_with_existing_global_events(self):
        self.assertEqual(trace.check()['global_events_enabled'], 'X')
        self.assertEqual(list((self.fs / 'instances').iterdir()), [])
        self.assert_global_unchanged()

    def test_missing_target_fails_before_instance_creation(self):
        (self.fs / 'available_events').write_text('power:suspend_resume')
        with self.assertRaisesRegex(RuntimeError, 'Missing trace targets'):
            trace.Capture(self.logs).start()
        self.assertEqual(list((self.fs / 'instances').iterdir()), [])

    def test_missing_clock_tracer_or_instances_is_rejected(self):
        for field, value in (('trace_clock', '[local]'), ('available_tracers', 'nop')):
            with self.subTest(field=field):
                original = (self.fs / field).read_text()
                (self.fs / field).write_text(value)
                with self.assertRaises(RuntimeError):
                    trace.check()
                (self.fs / field).write_text(original)
        (self.fs / 'instances').rmdir()
        with self.assertRaisesRegex(RuntimeError, 'instances'):
            trace.check()

    def test_existing_instance_is_not_adopted_or_removed(self):
        capture = trace.Capture(self.logs)
        capture.instance.mkdir()
        evidence = capture.instance / 'sentinel'
        evidence.write_text('foreign')
        with self.assertRaises(FileExistsError):
            capture.start()
        capture.stop()
        self.assertEqual(evidence.read_text(), 'foreign')

    def test_isolated_capture_persists_marker_and_disables_before_removal(self):
        capture = trace.Capture(self.logs)
        chunks = [capture.marker + b'\n']
        def read(fd, size):
            if chunks:
                return chunks.pop(0)
            raise BlockingIOError()
        def remove(path):
            self.assertEqual(path, capture.instance)
            self.assertEqual((path / 'tracing_on').read_text().strip(), '0')
            self.assertEqual((path / 'current_tracer').read_text().strip(), 'nop')
        with self.fake_instance(capture), patch.object(trace.os, 'read', side_effect=read), \
             patch.object(trace.select, 'select', side_effect=lambda *args: time.sleep(.001)), \
             patch.object(Path, 'rmdir', side_effect=remove, autospec=True) as removal:
            capture.start()
            capture.ensure_running()
            self.assertIn(capture.marker, (self.logs / 'kernel-trace.txt').read_bytes())
            capture.stop()
            removal.assert_called_once()
        self.assert_global_unchanged()
        status = json.loads((self.logs / 'kernel-trace-status.json').read_text())
        self.assertIsNone(status['capture_error'])
        self.assertEqual(status['cleanup_errors'], [])
        self.assertFalse(status['instance_retained'])

    def test_wrong_filter_never_enables_function_tracer(self):
        capture = trace.Capture(self.logs)
        read_text = Path.read_text
        def read(path, *args, **kwargs):
            if path == capture.instance / 'set_ftrace_filter':
                return '#### all functions enabled ####\n'
            return read_text(path, *args, **kwargs)
        with self.fake_instance(capture), patch.object(Path, 'read_text', read), \
             patch.object(Path, 'rmdir'):
            with self.assertRaisesRegex(RuntimeError, 'Exact function filter'):
                capture.start()
        self.assertEqual((capture.instance / 'current_tracer').read_text().strip(), 'nop')
        self.assert_global_unchanged()

    def test_byte_limit_and_partial_writes(self):
        capture = trace.Capture(self.logs)
        capture.output = Mock()
        capture.output.write.side_effect = [2, 3]
        with patch.object(trace.os, 'fsync') as sync:
            capture.save(b'12345')
            sync.assert_called_once()
        self.assertEqual(capture.byte_count, 5)
        with patch.object(trace, 'MAX_BYTES', 6):
            with self.assertRaisesRegex(RuntimeError, 'limit'):
                capture.save(b'78')
        self.assertEqual(capture.byte_count, 5)

    def test_zero_write_is_an_error(self):
        capture = trace.Capture(self.logs)
        capture.output = Mock()
        capture.output.write.return_value = 0
        with self.assertRaises(OSError):
            capture.save(b'x')

    def test_failed_sync_never_acknowledges_persisted_bytes(self):
        capture = trace.Capture(self.logs)
        capture.output = Mock()
        capture.output.write.return_value = 1
        with patch.object(trace.os, 'fsync', side_effect=OSError('disk error')):
            with self.assertRaises(OSError):
                capture.save(b'x')
        self.assertEqual(capture.byte_count, 0)

    def test_missing_marker_cleans_up_and_refuses_start(self):
        capture = trace.Capture(self.logs)
        thread = Mock()
        thread.is_alive.return_value = False
        with self.fake_instance(capture), patch.object(trace.threading, 'Thread', return_value=thread), \
             patch.object(capture.ready, 'wait', return_value=False), patch.object(Path, 'rmdir'):
            with self.assertRaisesRegex(RuntimeError, 'marker was not persisted'):
                capture.start()
        self.assertIsNone(capture.identity)
        self.assert_global_unchanged()

    def test_stalled_reader_is_not_killed_or_deleted(self):
        capture = trace.Capture(self.logs)
        with self.fake_instance(capture):
            capture.instance.mkdir()
        state = capture.instance.stat()
        capture.identity = (state.st_dev, state.st_ino)
        capture.thread = Mock()
        capture.thread.is_alive.return_value = True
        with patch.object(Path, 'rmdir') as remove:
            capture.stop()
        remove.assert_not_called()
        capture.thread.join.assert_called_once_with(timeout=3)
        self.assertEqual((capture.instance / 'tracing_on').read_text().strip(), '0')
        status = json.loads((self.logs / 'kernel-trace-status.json').read_text())
        self.assertTrue(status['instance_retained'])
        self.assertIn('reader still alive', status['cleanup_errors'][0])

    def test_trace_pipe_must_close_before_tracer_switch(self):
        capture = trace.Capture(self.logs)
        with self.fake_instance(capture):
            capture.instance.mkdir()
        state = capture.instance.stat()
        capture.identity = (state.st_dev, state.st_ino)
        (capture.instance / 'current_tracer').write_text('function\n')
        capture.fd = os.open(capture.instance / 'trace_pipe', os.O_RDONLY)
        reader_fd = capture.fd
        original_write = capture.write
        def kernel_write(name, value):
            if name == 'current_tracer':
                if capture.fd is not None:
                    raise OSError(16, 'Device or resource busy: trace_pipe is still open')
                with self.assertRaises(OSError):
                    os.fstat(reader_fd)
                self.assertEqual((capture.instance / 'tracing_on').read_text().strip(), '0')
            original_write(name, value)
        with patch.object(capture, 'write', side_effect=kernel_write), patch.object(Path, 'rmdir') as remove:
            capture.stop()
        status = json.loads((self.logs / 'kernel-trace-status.json').read_text())
        self.assertEqual(status['cleanup_errors'], [])
        self.assertFalse(status['instance_retained'])
        remove.assert_called_once_with()

    def test_deadline_disables_only_private_capture(self):
        capture = trace.Capture(self.logs)
        with patch.object(trace.time, 'monotonic', side_effect=[0, trace.MAX_SECONDS + 1]), \
             patch.object(capture, 'write') as write:
            capture.read()
        self.assertIn('deadline', capture.error)
        write.assert_called_once_with('tracing_on', '0')
        self.assert_global_unchanged()

    def test_changed_identity_refuses_cleanup_writes_and_removal(self):
        capture = trace.Capture(self.logs)
        with self.fake_instance(capture):
            capture.instance.mkdir()
        state = capture.instance.stat()
        capture.identity = (state.st_dev, state.st_ino + 1)
        with patch.object(Path, 'rmdir') as remove:
            capture.stop()
        remove.assert_not_called()
        self.assertEqual((capture.instance / 'current_tracer').read_text(), '0\n')
        status = json.loads((self.logs / 'kernel-trace-status.json').read_text())
        self.assertEqual(len(status['cleanup_errors']), 3)
        self.assertTrue(status['instance_retained'])


if __name__ == '__main__':
    unittest.main()
