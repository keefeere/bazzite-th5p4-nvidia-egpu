"""Hardware-free tests: no sleep, privileged commands or real sysfs writes."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch


spec = importlib.util.spec_from_file_location("lab", Path(__file__).resolve().parents[1] / "diagnostics" / "egpu-sleep-lab.py")
lab = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = lab
spec.loader.exec_module(lab)

GOOD_LOG = """PM: suspend entry (s2idle)
Freezing user space processes completed (0.001 seconds)
suspend debug: Waiting for 5 second(s).
PM: suspend exit
"""
DEVICES_LOG = GOOD_LOG.replace("suspend debug: Waiting for", "PM: suspend devices took 0.100 seconds\nsuspend debug: Waiting for").replace(
    "PM: suspend exit", "PM: resume devices took 0.200 seconds\nPM: suspend exit")
PLATFORM_LOG = GOOD_LOG.replace(
    "suspend debug: Waiting for",
    "PM: suspend devices took 0.100 seconds\n"
    "PM: late suspend of devices complete after 1.000 msecs\n"
    "PM: noirq suspend of devices complete after 2.000 msecs\n"
    "suspend debug: Waiting for").replace(
    "PM: suspend exit",
    "PM: noirq resume of devices complete after 2.000 msecs\n"
    "PM: early resume of devices complete after 1.000 msecs\n"
    "PM: resume devices took 0.200 seconds\nPM: suspend exit")


def state(stamp="100", active="inactive", result="success"):
    return {"ExecMainStartTimestampMonotonic": stamp, "ActiveState": active, "Result": result}


class StackCaptureTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.proc = self.root / 'proc'
        self.proc.mkdir()
        self.logs = self.root / 'logs'
        self.logs.mkdir()
        self.patch = patch.object(lab, 'PROC_ROOT', self.proc)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def task(self, pid, name, status='S', stack='sample_stack\n'):
        proc = self.proc / str(pid)
        task = proc / 'task' / str(pid)
        task.mkdir(parents=True)
        (proc / 'comm').write_text(name + '\n')
        (proc / 'status').write_text(f'State:\t{status} (test)\n')
        (task / 'status').write_text(f'Name:\t{name}\nState:\t{status} (test)\n'
                                   f'Pid:\t{pid}\nPPid:\t1\nvoluntary_ctxt_switches:\t3\n')
        (task / 'wchan').write_text('do_wait\n')
        (task / 'schedstat').write_text('1000000 2000000 3\n')
        (task / 'stack').write_text(stack)
        return proc

    def test_empty_stack_keeps_independent_scheduler_evidence(self):
        task = self.task(30, 'systemd-sleep', status='R', stack='') / 'task' / '30'
        (task / 'wchan').write_text('0\n')
        lab.capture_stacks(self.logs, 'empty-running', focused=True)
        text = (self.logs / 'stacks-empty-running.txt').read_text()
        self.assertIn('State:\tR (test)', text)
        self.assertIn('1000000 2000000 3', text)
        self.assertIn('voluntary_ctxt_switches:\t3', text)
        self.assertIn('wchan: sample_monotonic=', text)
        self.assertIn('stack_sample_monotonic=', text)
        self.assertIn('non-atomic', text)
        self.assertIn('not a completion signal', text)

    def test_context_is_synced_before_attempting_kernel_stack(self):
        task = self.task(30, 'systemd-sleep') / 'task' / '30'
        original_read = Path.read_text
        capture = self.logs / 'stacks-context-first.txt'
        with patch.object(lab.os, 'fsync') as sync:
            def read(path, *args, **kwargs):
                if path == task / 'stack':
                    saved = original_read(capture)
                    self.assertIn('State:\tS (test)', saved)
                    self.assertIn('1000000 2000000 3', saved)
                    self.assertGreaterEqual(sync.call_count, 7)
                    raise PermissionError('test stack unavailable')
                return original_read(path, *args, **kwargs)
            with patch.object(Path, 'read_text', read):
                lab.capture_stacks(self.logs, 'context-first', focused=True)
        self.assertIn('stack unavailable', capture.read_text())

    def test_missing_context_does_not_skip_remaining_fields_or_stack(self):
        task = self.task(30, 'systemd-sleep') / 'task' / '30'
        (task / 'status').unlink()
        (task / 'wchan').unlink()
        lab.capture_stacks(self.logs, 'missing-context', focused=True)
        text = (self.logs / 'stacks-missing-context.txt').read_text()
        self.assertIn('status unavailable', text)
        self.assertIn('wchan unavailable', text)
        self.assertIn('1000000 2000000 3', text)
        self.assertIn('sample_stack', text)

    def test_status_keeps_only_selected_scheduler_fields(self):
        task = self.task(30, 'systemd-sleep') / 'task' / '30'
        with (task / 'status').open('a') as stream:
            stream.write('VmRSS:\t1234 kB\nUnrelated:\tfixture-secret\n')
        (task / 'schedstat').write_text('0 0 0\n')
        lab.capture_stacks(self.logs, 'selected-context', focused=True)
        text = (self.logs / 'stacks-selected-context.txt').read_text()
        self.assertIn('State:', text)
        self.assertIn('0 0 0', text)
        self.assertNotIn('VmRSS:', text)
        self.assertNotIn('fixture-secret', text)

    def test_focus_only_reads_sleep_caller_not_other_threads(self):
        self.task(10, 'nvidia')
        self.task(20, 'blocked', 'D')
        sleeper = self.task(30, 'systemd-sleep')
        self.assertEqual(lab.stack_candidates(focused=True), [(sleeper, 'systemd-sleep')])
        with patch.object(lab, 'snapshot') as snapshot, patch.object(lab, 'command') as command, \
             patch.object(lab.os, 'sync') as sync:
            lab.dump_stacks(self.logs, 'early-1', focused=True)
        text = (self.logs / 'stacks-early-1.txt').read_text()
        self.assertIn('PID 30 systemd-sleep', text)
        self.assertNotIn('PID 10', text)
        self.assertNotIn('PID 20', text)
        snapshot.assert_not_called()
        command.assert_not_called()
        sync.assert_not_called()

    def test_full_capture_prioritizes_sleep_and_keeps_blocked_tasks(self):
        self.task(3, 'ordinary')
        self.task(2, 'blocked', 'D')
        self.task(1, 'nvidia')
        self.task(40, 'systemd-sleep')
        self.assertEqual([p.name for p, _ in lab.stack_candidates()], ['40', '1', '2'])

    def test_pm_stack_is_flushed_and_synced_before_later_task_read(self):
        later = self.task(1, 'nvidia') / 'task' / '1' / 'stack'
        self.task(50, 'systemd-sleep', stack='nv_pm_notifier\n')
        original_read = Path.read_text
        path = self.logs / 'stacks-1.txt'
        with patch.object(lab.os, 'fsync') as sync:
            def read(p, *args, **kwargs):
                if p == later:
                    self.assertIn('nv_pm_notifier', original_read(path))
                    self.assertGreaterEqual(sync.call_count, 4)
                    raise PermissionError('test stack access denied')
                return original_read(p, *args, **kwargs)

            with patch.object(Path, 'read_text', read):
                lab.capture_stacks(self.logs, 1)
        self.assertIn('stack unavailable', path.read_text())
        self.assertIn('nv_pm_notifier', path.read_text())

    def test_absence_and_empty_stack_are_not_success_signals(self):
        lab.capture_stacks(self.logs, 'absent', focused=True)
        self.assertIn('PM stage cannot be inferred', (self.logs / 'stacks-absent.txt').read_text())
        self.task(2, 'systemd-sleep', stack='')
        lab.capture_stacks(self.logs, 'empty', focused=True)
        self.assertIn('not a completion signal', (self.logs / 'stacks-empty.txt').read_text())

    def test_existing_capture_is_never_overwritten(self):
        path = self.logs / 'stacks-1.txt'
        path.write_text('previous evidence')
        with self.assertRaises(FileExistsError):
            lab.capture_stacks(self.logs, 1)
        self.assertEqual(path.read_text(), 'previous evidence')

    def test_observer_has_early_probes_and_stops_on_completion(self):
        from unittest.mock import Mock, call
        done = Mock()
        done.wait.side_effect = [False, False, True]
        with patch.object(lab.time, 'monotonic', side_effect=[100, 100, 103, 107]), \
             patch.object(lab, 'dump_stacks') as dump, contextlib.redirect_stdout(io.StringIO()):
            lab.observer(self.logs, done)
        self.assertEqual(done.wait.call_args_list, [call(2), call(3), call(8)])
        self.assertEqual(dump.call_args_list, [call(self.logs, 'early-1', focused=True),
                                               call(self.logs, 'early-2', focused=True)])

    def test_failed_capture_does_not_prevent_next_probe(self):
        from unittest.mock import Mock
        done = Mock()
        done.wait.side_effect = [False, False, True]
        with patch.object(lab.time, 'monotonic', return_value=100), \
             patch.object(lab, 'dump_stacks', side_effect=[OSError('disk full'), None]) as dump, \
             contextlib.redirect_stdout(io.StringIO()) as output:
            lab.observer(self.logs, done)
        self.assertEqual(dump.call_count, 2)
        self.assertIn('disk full', output.getvalue())


class TraceCliTests(unittest.TestCase):
    def test_rtc_opt_in_is_routed_only_with_serial_platform_mode(self):
        args = ['lab', 'run', '--stage', 'platform', '--vram-backing', 'private-tmpfs',
                '--serial-device-pm', '--rtc-pm-trace']
        with patch.object(sys, 'argv', args), patch.object(lab, 'launch', return_value=0) as launch:
            self.assertEqual(lab.main(), 0)
        launch.assert_called_once_with('platform', lab.RTC_MODE)

    def test_rtc_opt_in_rejects_missing_serial_or_combined_kernel_trace(self):
        for suffix in (['--rtc-pm-trace'],
                       ['--serial-device-pm', '--rtc-pm-trace', '--kernel-trace']):
            args = ['lab', 'run', '--stage', 'platform', '--vram-backing', 'private-tmpfs', *suffix]
            with self.subTest(suffix=suffix), patch.object(sys, 'argv', args), \
                 patch.object(lab, 'launch') as launch, contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    lab.main()
                launch.assert_not_called()

    def test_trace_check_only_inspects_and_never_starts_capture(self):
        helper = Mock()
        helper.check.return_value = {'events': ['test']}
        with patch.object(sys, 'argv', ['lab', 'check', '--kernel-trace']), \
             patch.object(lab, 'preflight', return_value={}), \
             patch.object(lab, 'trace_module', return_value=helper), \
             patch.object(lab, 'launch') as launch, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(lab.main(), 0)
        helper.check.assert_called_once()
        helper.Capture.assert_not_called()
        launch.assert_not_called()

    def test_trace_run_forwards_explicit_opt_in(self):
        with patch.object(sys, 'argv', ['lab', 'run', '--kernel-trace']), \
             patch.object(lab, 'launch', return_value=0) as launch:
            self.assertEqual(lab.main(), 0)
        launch.assert_called_once_with('freezer', False, kernel_trace=True)

    def test_trace_rejects_graphics_only_and_cleanup(self):
        for args in (['check', '--graphics-only'], ['run', '--no-graphics', '--allow-logout'],
                     ['cleanup', '/not-used']):
            with self.subTest(args=args), patch.object(sys, 'argv', ['lab', *args, '--kernel-trace']), \
                 patch.object(lab, 'launch') as launch, contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    lab.main()
                launch.assert_not_called()


class GraphicsRoutingTests(unittest.TestCase):
    def test_trace_launcher_archives_helper_and_passes_flag(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original_stat = Path.stat
            def root_stat(path, *args, **kwargs):
                if path == root:
                    return SimpleNamespace(st_uid=0, st_mode=0o40700)
                return original_stat(path, *args, **kwargs)
            helper = Mock()
            helper.check.return_value = {'tracing': 'read-only check'}
            with patch.object(lab, 'LOG_ROOT', root), patch.object(Path, 'stat', root_stat), \
                 patch.object(lab, 'preflight', return_value={}), \
                 patch.object(lab, 'trace_module', return_value=helper), \
                 patch.object(lab, 'command', return_value=subprocess.CompletedProcess([], 0, '')) as command, \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(lab.launch('devices', 'vram-tmpfs', kernel_trace=True), 0)
            self.assertIn('--kernel-trace', command.call_args.args[0])
            archived = next(root.glob('*/egpu_pm_trace.py'))
            self.assertEqual(archived.read_bytes(), (Path(lab.__file__).parent / archived.name).read_bytes())
            self.assertEqual(archived.stat().st_mode & 0o777, 0o600)
            helper.check.assert_called_once()

    def test_serial_launcher_preserves_mode_in_archived_worker_command(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original_stat = Path.stat
            def root_stat(path, *args, **kwargs):
                if path == root:
                    return SimpleNamespace(st_uid=0, st_mode=0o40700)
                return original_stat(path, *args, **kwargs)
            with patch.object(lab, 'LOG_ROOT', root), patch.object(Path, 'stat', root_stat), \
                 patch.object(lab, 'preflight', return_value={'experiment': lab.SERIAL_MODE}), \
                 patch.object(lab, 'command', return_value=subprocess.CompletedProcess([], 0, '')) as command, \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(lab.launch('platform', lab.SERIAL_MODE), 0)
            argv = command.call_args.args[0]
            self.assertEqual(argv[0], 'systemd-run')
            self.assertEqual(argv[-5:], ['--stage', 'platform', '--vram-backing',
                                        'private-tmpfs', '--serial-device-pm'])
            self.assertIn('--property=Restart=no', argv)
            self.assertNotIn('--allow-logout', argv)
            folder = next(root.iterdir())
            self.assertEqual((folder / 'egpu_vram_backing.py').read_bytes(),
                             (Path(lab.__file__).parent / 'egpu_vram_backing.py').read_bytes())
            self.assertFalse((folder / 'sleep-guard-bypass.txt').exists())

    def test_rtc_launcher_passes_explicit_flag_without_kernel_trace(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            real_stat = Path.stat
            def root_stat(path, *args, **kwargs):
                return (SimpleNamespace(st_uid=0, st_mode=0o40700) if path == root
                        else real_stat(path, *args, **kwargs))
            with patch.object(lab, 'LOG_ROOT', root), patch.object(Path, 'stat', root_stat), \
                 patch.object(lab, 'preflight', return_value={'experiment': lab.RTC_MODE}), \
                 patch.object(lab, 'command', return_value=subprocess.CompletedProcess([], 0, '')) as command, \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(lab.launch('platform', lab.RTC_MODE), 0)
            argv = command.call_args.args[0]
            self.assertEqual(argv[-6:], ['--stage', 'platform', '--vram-backing',
                                        'private-tmpfs', '--serial-device-pm', '--rtc-pm-trace'])
            self.assertNotIn('--kernel-trace', argv)

    def test_graphics_launcher_holds_sleep_inhibitor_without_pm_stage(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            real_stat = Path.stat

            def log_root_stat(path, *args, **kwargs):
                if path == root:
                    return SimpleNamespace(st_uid=0, st_mode=0o40700)
                return real_stat(path, *args, **kwargs)

            helper = SimpleNamespace(preflight_graphics=lambda _: {'experiment': 'graphics-only'})
            with patch.object(lab, 'LOG_ROOT', root), patch.object(Path, 'stat', log_root_stat), \
                 patch.object(lab, 'no_graphics_module', return_value=helper), \
                 patch.object(lab, 'command', return_value=subprocess.CompletedProcess([], 0, '')) as command, \
                 contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(lab.launch(guard_bypass='graphics-only'), 0)
            argv = command.call_args.args[0]
            self.assertEqual(argv[0], 'systemd-run')
            self.assertIn('systemd-inhibit', argv)
            self.assertIn('--mode=block', argv)
            self.assertIn('--graphics-only', argv)
            self.assertIn('--allow-logout', argv)
            self.assertNotIn('--stage', argv)
            self.assertNotIn('--host-reset-guard-bypass', argv)
            self.assertIn('NO SLEEP', output.getvalue())
            folder = next(root.iterdir())
            self.assertTrue((folder / 'egpu_no_graphics.py').exists())
            self.assertFalse((folder / 'sleep-guard-bypass.txt').exists())

    def test_worker_routes_graphics_only_without_entering_sleep_code(self):
        from unittest.mock import Mock
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            helper = SimpleNamespace(run=Mock(return_value=0))
            with patch.object(lab, 'LOCK', root / 'lab-lock'), \
                 patch.object(lab, 'TRANSITION_LOCK', root / 'transition-lock'), \
                 patch.object(lab, 'no_graphics_module', return_value=helper), \
                 patch.object(lab, 'experiment') as experiment:
                self.assertEqual(lab.worker(root, guard_bypass='graphics-only'), 0)
                helper.run.assert_called_once_with(lab, root, graphics_only=True)
                experiment.assert_not_called()


class PMControl:
    def __init__(self):
        self.value = "none"
        self.writes = []

    def read_text(self):
        return " ".join(f"[{word}]" if word == self.value else word
                        for word in ("none", "core", "processors", "platform", "devices", "freezer"))

    def write_text(self, text):
        self.value = text.strip()
        self.writes.append(self.value)


class ParsingTests(unittest.TestCase):
    def test_serial_cli_requires_exact_platform_private_mode(self):
        rejected = [
            ['run', '--serial-device-pm'],
            ['run', '--vram-backing', 'private-tmpfs', '--serial-device-pm'],
            ['run', '--stage', 'devices', '--vram-backing', 'private-tmpfs', '--serial-device-pm'],
            ['run', '--stage', 'platform', '--vram-backing', 'current', '--serial-device-pm'],
            ['run', '--stage', 'platform', '--serial-device-pm'],
            ['run', '--stage', 'platform', '--vram-backing', 'private-tmpfs', '--serial-device-pm', '--allow-logout'],
            ['cleanup', '--serial-device-pm'],
        ]
        with patch.object(lab, 'launch') as launch, patch.object(lab, 'preflight') as preflight:
            for argv in rejected:
                with self.subTest(argv=argv), patch.object(lab.sys, 'argv', ['lab', *argv]), \
                     contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                    lab.main()
            launch.assert_not_called()
            preflight.assert_not_called()

    def test_serial_cli_run_routes_distinct_mode(self):
        with patch.object(lab.sys, 'argv', ['lab', 'run', '--stage', 'platform',
                '--vram-backing', 'private-tmpfs', '--serial-device-pm']), \
             patch.object(lab, 'launch', return_value=0) as launch:
            self.assertEqual(lab.main(), 0)
            launch.assert_called_once_with('platform', lab.SERIAL_MODE)

    def test_serial_cli_check_never_launches(self):
        with patch.object(lab.sys, 'argv', ['lab', 'check', '--stage', 'platform',
                '--vram-backing', 'private-tmpfs', '--serial-device-pm']), \
             patch.object(lab, 'preflight', return_value={}) as preflight, \
             patch.object(lab, 'launch') as launch, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(lab.main(), 0)
            preflight.assert_called_once_with('platform', lab.SERIAL_MODE)
            launch.assert_not_called()

    def test_vram_mode_rejects_deeper_stages_and_is_exclusive(self):
        rejected = [
            ['run', '--vram-backing', 'private-tmpfs', '--stage', 'processors'],
            ['run', '--vram-backing', 'private-tmpfs', '--stage', 'core'],
            ['run', '--vram-backing', 'current', '--stage', 'none'],
            ['run', '--vram-backing', 'private-tmpfs', '--no-graphics'],
            ['run', '--vram-backing', 'current', '--allow-logout'],
            ['cleanup', '--vram-backing', 'current'],
        ]
        with patch.object(lab, 'launch') as launch:
            for argv in rejected:
                with self.subTest(argv=argv), patch.object(lab.sys, 'argv', ['lab', *argv]), \
                     contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                    lab.main()
        launch.assert_not_called()

    def test_vram_cli_routes_only_named_modes(self):
        for option, mode in (('current', 'vram-current'), ('private-tmpfs', 'vram-tmpfs')):
            with self.subTest(option=option), patch.object(lab.sys, 'argv', ['lab', 'run', '--vram-backing', option]), \
                 patch.object(lab, 'launch', return_value=0) as launch:
                self.assertEqual(lab.main(), 0)
                launch.assert_called_once_with('freezer', mode)

    def test_vram_cli_devices_requires_explicit_stage(self):
        for option, mode in (('current', 'vram-current'), ('private-tmpfs', 'vram-tmpfs')):
            with self.subTest(option=option), patch.object(lab.sys, 'argv',
                    ['lab', 'run', '--stage', 'devices', '--vram-backing', option]), \
                 patch.object(lab, 'launch', return_value=0) as launch:
                self.assertEqual(lab.main(), 0)
                launch.assert_called_once_with('devices', mode)

    def test_vram_cli_platform_requires_explicit_stage(self):
        for option, mode in (('current', 'vram-current'), ('private-tmpfs', 'vram-tmpfs')):
            with self.subTest(option=option), patch.object(lab.sys, 'argv',
                    ['lab', 'run', '--stage', 'platform', '--vram-backing', option]), \
                 patch.object(lab, 'launch', return_value=0) as launch:
                self.assertEqual(lab.main(), 0)
                launch.assert_called_once_with('platform', mode)

    def test_vram_platform_check_never_launches_or_arms(self):
        with patch.object(lab.sys, 'argv',
                ['lab', 'check', '--stage', 'platform', '--vram-backing', 'private-tmpfs']), \
             patch.object(lab, 'preflight', return_value={}) as preflight, \
             patch.object(lab, 'launch') as launch, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(lab.main(), 0)
        preflight.assert_called_once_with('platform', 'vram-tmpfs')
        launch.assert_not_called()

    def test_vram_override_is_private_and_does_not_replace_exec_or_freeze_policy(self):
        private = lab.unit_override(Path('/run/test'), 'vram-tmpfs')
        current = lab.unit_override(Path('/run/test'), 'vram-current')
        self.assertEqual(lab.unit_override(Path('/run/test'), lab.SERIAL_MODE), private)
        self.assertIn('TemporaryFileSystem=/var/tmp:rw,size=6G', private)
        self.assertNotIn('TemporaryFileSystem', current)
        for output in (private, current):
            self.assertNotIn('ExecStart=', output)
            self.assertNotIn('ExecStartPre=', output)
            self.assertNotIn('SYSTEMD_SLEEP_FREEZE_USER_SESSIONS', output)
            self.assertNotIn('BindPaths', output)

    def test_graphics_only_requires_explicit_logout_and_no_pm_flags(self):
        rejected = [
            ['run', '--graphics-only'], ['_worker', '--graphics-only'],
            ['check', '--graphics-only', '--allow-logout'],
            ['check', '--graphics-only', '--stage', 'platform'],
            ['run', '--graphics-only', '--allow-logout', '--stage', 'freezer'],
            ['check', '--graphics-only', '--no-graphics'],
            ['check', '--graphics-only', '--host-reset-guard-bypass'],
            ['check', '--graphics-only', '--cardwire-off-guard-bypass'],
            ['cleanup', '--graphics-only'],
        ]
        with patch.object(lab, 'launch') as launch, patch.object(lab, 'preflight') as preflight:
            for args in rejected:
                with self.subTest(args=args), patch.object(lab.sys, 'argv', ['lab', *args]), \
                     contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                    lab.main()
            launch.assert_not_called()
            preflight.assert_not_called()

    def test_graphics_only_check_is_readonly(self):
        helper = SimpleNamespace(preflight_graphics=lambda _: {'experiment': 'graphics-only'})
        with patch.object(lab.sys, 'argv', ['lab', 'check', '--graphics-only']), \
             patch.object(lab, 'no_graphics_module', return_value=helper), \
             patch.object(lab, 'launch') as launch, patch.object(lab, 'experiment') as experiment, \
             contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(lab.main(), 0)
            self.assertIn('READ-ONLY PREFLIGHT PASSED', output.getvalue())
            launch.assert_not_called()
            experiment.assert_not_called()

    def test_graphics_only_run_routes_to_distinct_mode(self):
        with patch.object(lab.sys, 'argv', ['lab', 'run', '--graphics-only', '--allow-logout']), \
             patch.object(lab, 'launch', return_value=0) as launch:
            self.assertEqual(lab.main(), 0)
            launch.assert_called_once_with('freezer', 'graphics-only')

    def test_compute_clients_exclude_only_kwin(self):
        output = ("37737, /usr/bin/kwin_wayland\n"
                  "90311, /opt/Steam/fossilize_replay\n"
                  "90416, /usr/bin/python3\n")
        self.assertEqual(lab.parse_compute_clients(output),
                         [("90311", "/opt/Steam/fossilize_replay"),
                          ("90416", "/usr/bin/python3")])
        self.assertEqual(lab.parse_compute_clients("No running processes found\n"), [])
        with self.assertRaises(RuntimeError):
            lab.parse_compute_clients("not csv")

    def test_compute_summary_omits_arguments_and_keeps_executable(self):
        clients = [('16286', 'gjs -m /app/bin/re.sonny.Junction'),
                   ('107981', '/usr/lib/opt/google/chrome/chrome --type=gpu-process '
                    '--render-node-override=/dev/dri/renderD129 --token=private')]
        self.assertEqual(lab.compute_client_summary(clients), '16286:gjs, 107981:chrome')
        # Formatting must not reclassify these as harmless or whitelist them.
        self.assertEqual(len(lab.parse_compute_clients('\n'.join(f'{p}, {n}' for p, n in clients))), 2)

    def test_compute_summary_is_bounded_and_handles_missing_names(self):
        self.assertEqual(lab.compute_client_summary([('1', '')]), '1:unknown')
        self.assertEqual(lab.compute_client_summary([('1', 'x' * 100)]), '1:' + 'x' * 64)
        result = lab.compute_client_summary([(str(i), 'app') for i in range(10)])
        self.assertTrue(result.endswith(', +2 more'))
        self.assertNotIn('8:app', result)

    def test_platform_requires_deep_callbacks_but_not_real_s2idle_loop(self):
        self.assertTrue(lab.stage_confirmed("platform", state(), PLATFORM_LOG))
        self.assertFalse(lab.stage_confirmed("platform", state(), DEVICES_LOG))
        for marker in ("late suspend of devices", "noirq suspend of devices",
                       "noirq resume of devices", "early resume of devices"):
            with self.subTest(marker=marker):
                self.assertFalse(lab.stage_confirmed("platform", state(), PLATFORM_LOG.replace(marker, "missing")))
        self.assertFalse(lab.stage_confirmed("platform", state(), PLATFORM_LOG.replace(
            "PM: noirq resume of devices complete after 2.000 msecs\n", "")
            + "PM: noirq resume of devices complete after 2.000 msecs\n"))
        self.assertFalse(lab.stage_confirmed("platform", state(), PLATFORM_LOG + "PM: suspend-to-idle\n"))

    def test_devices_requires_both_device_phases_in_order(self):
        self.assertTrue(lab.stage_confirmed("devices", state(), DEVICES_LOG))
        self.assertFalse(lab.stage_confirmed("devices", state(), GOOD_LOG))
        self.assertFalse(lab.stage_confirmed("devices", state(), DEVICES_LOG.replace("PM: resume devices took", "missing")))
        self.assertFalse(lab.stage_confirmed("devices", state(), DEVICES_LOG + "PM: noirq suspend of devices complete"))
        self.assertFalse(lab.stage_confirmed("devices", state(), DEVICES_LOG * 2))
        self.assertFalse(lab.stage_confirmed("devices", state(result="exit-code"), DEVICES_LOG))

    def test_unsupported_stage_never_reaches_preflight(self):
        with patch.object(lab, "preflight") as preflight:
            for stage in ("none", "core", "processors"):
                with self.assertRaises(RuntimeError):
                    lab.experiment(Path("/not-used"), stage)
            preflight.assert_not_called()

    def test_cli_stage_defaults_to_freezer(self):
        with patch.object(lab.sys, "argv", ["lab", "run"]), \
             patch.object(lab, "launch", return_value=0) as launch:
            self.assertEqual(lab.main(), 0)
            launch.assert_called_once_with("freezer", False)

    def test_cli_devices_is_explicit(self):
        with patch.object(lab.sys, "argv", ["lab", "run", "--stage", "devices"]), \
             patch.object(lab, "launch", return_value=0) as launch:
            self.assertEqual(lab.main(), 0)
            launch.assert_called_once_with("devices", False)

    def test_cli_platform_is_explicit(self):
        with patch.object(lab.sys, "argv", ["lab", "run", "--stage", "platform"]), \
             patch.object(lab, "launch", return_value=0) as launch:
            self.assertEqual(lab.main(), 0)
            launch.assert_called_once_with("platform", False)

    def test_cli_guard_bypass_is_explicit_platform_only(self):
        with patch.object(lab.sys, "argv", ["lab", "run", "--stage", "platform",
                                             "--host-reset-guard-bypass"]), \
             patch.object(lab, "launch", return_value=0) as launch:
            self.assertEqual(lab.main(), 0)
            launch.assert_called_once_with("platform", True)
        with patch.object(lab.sys, "argv", ["lab", "run", "--stage", "devices",
                                             "--host-reset-guard-bypass"]), \
             self.assertRaises(SystemExit):
            lab.main()

    def test_cli_devices_check_does_not_launch(self):
        with patch.object(lab.sys, "argv", ["lab", "check", "--stage", "devices"]), \
             patch.object(lab, "preflight", return_value={}) as preflight, \
             patch.object(lab, "launch") as launch, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(lab.main(), 0)
            preflight.assert_called_once_with("devices", False)
            launch.assert_not_called()

    def test_cli_cardwire_off_is_explicit_platform_only(self):
        with patch.object(lab.sys, "argv", ["lab", "run", "--stage", "platform",
                                             "--cardwire-off-guard-bypass"]), \
             patch.object(lab, "launch", return_value=0) as launch:
            self.assertEqual(lab.main(), 0)
            launch.assert_called_once_with("platform", "cardwire-off")
        for extra in ([], ["--stage", "freezer"], ["--stage", "platform", "--host-reset-guard-bypass"]):
            with self.subTest(extra=extra), patch.object(lab.sys, "argv", [
                    "lab", "run", "--cardwire-off-guard-bypass", *extra]), \
                 patch.object(lab, "launch") as launch, self.assertRaises(SystemExit):
                lab.main()
            launch.assert_not_called()

    def test_cli_cardwire_check_is_readonly(self):
        with patch.object(lab.sys, "argv", ["lab", "check", "--stage", "platform",
                                             "--cardwire-off-guard-bypass"]), \
             patch.object(lab, "preflight", return_value={}) as preflight, \
             patch.object(lab, "launch") as launch, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(lab.main(), 0)
            preflight.assert_called_once_with("platform", "cardwire-off")
            launch.assert_not_called()

    def test_no_graphics_requires_explicit_logout_consent(self):
        argv = ['lab', 'run', '--stage', 'platform', '--no-graphics']
        with patch.object(lab.sys, 'argv', argv), patch.object(lab, 'launch') as launch, self.assertRaises(SystemExit):
            lab.main()
        launch.assert_not_called()
        with patch.object(lab.sys, 'argv', argv + ['--allow-logout']), patch.object(lab, 'launch', return_value=0) as launch:
            self.assertEqual(lab.main(), 0)
            launch.assert_called_once_with('platform', 'no-graphics')

    def test_no_graphics_check_does_not_log_out(self):
        with patch.object(lab.sys, 'argv', ['lab', 'check', '--stage', 'platform', '--no-graphics']), \
             patch.object(lab, 'preflight', return_value={}) as preflight, \
             patch.object(lab, 'launch') as launch, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(lab.main(), 0)
            preflight.assert_called_once_with('platform', 'no-graphics', preparing=True)
            launch.assert_not_called()

    def test_displaylink_hook_isolation_is_only_for_no_graphics(self):
        folder = Path('/test')
        self.assertNotIn('BindReadOnlyPaths', lab.unit_override(folder))
        self.assertNotIn('BindReadOnlyPaths', lab.unit_override(folder, 'cardwire-off'))
        self.assertIn('BindReadOnlyPaths=/usr/bin/true:/usr/lib/systemd/system-sleep/displaylink',
                      lab.unit_override(folder, 'no-graphics'))

    def test_runtime_labels_use_existing_policy_nonrecursively(self):
        paths = [Path("/run/systemd/sleep.conf.d/test.conf"),
                 Path("/run/systemd/system/systemd-suspend.service.d/test.conf")]
        with patch.object(Path, "exists", return_value=True), \
             patch.object(lab, "require_command") as command:
            lab.label_overrides(paths)
            targets = [str(p) for path in paths for p in (path.parent, path)]
            self.assertEqual(command.call_args_list[0].args[0], ["restorecon", "-F", *targets])
            self.assertEqual(command.call_args_list[1].args[0], ["matchpathcon", "-V", *targets])

    def test_labels_noop_without_selinux(self):
        with patch.object(Path, "exists", return_value=False), \
             patch.object(lab, "require_command") as command:
            lab.label_overrides([Path("/test.conf")])
            command.assert_not_called()

    def test_request_keeps_inhibitors_and_avoids_client_session_check(self):
        self.assertEqual(lab.SUSPEND_REQUEST[-3:], ["SuspendWithFlags", "t", "1"])
        self.assertNotIn("systemctl", lab.SUSPEND_REQUEST)
        self.assertIn("--allow-interactive-authorization=no", lab.SUSPEND_REQUEST)

    def test_only_definitive_refusals_allow_cleanup(self):
        self.assertTrue(lab.definitive_rejection("Operation denied due to active block inhibitor"))
        self.assertTrue(lab.definitive_rejection("Please retry operation after closing inhibitors and logging out other users."))
        self.assertFalse(lab.definitive_rejection("Connection timed out"))
        self.assertFalse(lab.definitive_rejection("Action suspend already in progress"))

    def test_idle_check_rejects_pending_logind_action(self):
        with patch.object(lab, "require_command", return_value="b true"):
            with self.assertRaises(RuntimeError):
                lab.assert_sleep_idle()

    def test_idle_check_rejects_queued_or_changed_invocation(self):
        with patch.object(lab, "unit_state", return_value=state()), \
             patch.object(lab, "require_command", side_effect=["b false", "42 suspend.target start waiting"]):
            with self.assertRaises(RuntimeError):
                lab.assert_sleep_idle()
        with patch.object(lab, "unit_state", return_value=state("101")), \
             patch.object(lab, "require_command", return_value="b false"):
            with self.assertRaises(RuntimeError):
                lab.assert_sleep_idle("100")

    def test_setting_selection(self):
        self.assertEqual(lab.selected("[none] core freezer"), "none")
        with self.assertRaises(RuntimeError):
            lab.selected("default modeset uvm")

    def test_single_state_requires_reset(self):
        config = "[Sleep]\nSuspendState=mem standby freeze\n[Sleep]\nSuspendState=mem\n"
        self.assertNotEqual(lab.effective_suspend_states(config), ["mem"])
        config += "[Sleep]\nSuspendState=\nSuspendState=mem\n"
        self.assertEqual(lab.effective_suspend_states(config), ["mem"])

    def test_config_default_and_sections(self):
        self.assertEqual(lab.effective_suspend_states("#SuspendState=mem\n"), ["mem", "standby", "freeze"])
        self.assertEqual(lab.effective_suspend_states("[Other]\nSuspendState=mem\n"), ["mem", "standby", "freeze"])

    def test_freezer_confirmation(self):
        self.assertTrue(lab.freezer_confirmed(state(), GOOD_LOG))

    def test_no_false_success(self):
        for log in ("", GOOD_LOG * 2,
                    GOOD_LOG.replace("suspend debug: Waiting for", "missing stage"),
                    GOOD_LOG + "PM: suspend devices took 0.25 seconds",
                    GOOD_LOG + "Suspending console(s)",
                    GOOD_LOG.replace("completed", "aborted after")):
            with self.subTest(log=log):
                self.assertFalse(lab.freezer_confirmed(state(), log))
        self.assertFalse(lab.freezer_confirmed(state(result="exit-code"), GOOD_LOG))

    def test_enqueue_is_not_completion(self):
        states = [state(), state("101", "activating"), state("101", "deactivating"), state("101")]
        with patch.object(lab, "unit_state", side_effect=states) as read, patch.object(lab.time, "sleep"):
            self.assertEqual(lab.wait_for_cycle("100"), states[-1])
            self.assertEqual(read.call_count, 4)

    def test_failed_new_cycle_is_finished(self):
        failed = state("102", "failed", "exit-code")
        with patch.object(lab, "unit_state", return_value=failed):
            self.assertEqual(lab.wait_for_cycle("100"), failed)

    def test_collected_unit_retains_recorded_result(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "ended.json"
            marker.write_text(json.dumps({"service_result": "exit-code"}))
            with patch.object(lab, "unit_state", return_value=state("0")):
                result = lab.wait_for_cycle("100", timeout=0, completion=marker)
                self.assertEqual(result["Result"], "exit-code")

    def test_completion_marker_does_not_override_busy_unit(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "ended.json"
            marker.write_text(json.dumps({"service_result": "success"}))
            with patch.object(lab, "unit_state", return_value=state("101", "deactivating")):
                with self.assertRaises(RuntimeError):
                    lab.wait_for_cycle("100", timeout=0, completion=marker)

    def test_missing_or_old_timestamp_does_not_complete(self):
        for value in (state(), {"ActiveState": "inactive"}, state("0")):
            with self.subTest(value=value), patch.object(lab, "unit_state", return_value=value):
                with self.assertRaises(RuntimeError):
                    lab.wait_for_cycle("100", timeout=0)

    def test_check_does_not_launch(self):
        with patch.object(lab.sys, "argv", ["lab", "check"]), \
             patch.object(lab, "preflight", return_value={}), \
             patch.object(lab, "launch") as launch, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(lab.main(), 0)
            launch.assert_not_called()


class GuardPreflightTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        for constant, name, content in (
                ("CMDLINE", "cmdline", "thunderbolt.host_reset=0 egpu.host_reset_test=1 egpu.host_reset_nodock=1"),
                ("HOST_RESET", "reset", "N"), ("SLEEP_GUARD", "guard", "installed")):
            path = self.root / name
            path.write_text(content)
            self.stack.enter_context(patch.object(lab, constant, path))
        self.stack.enter_context(patch.object(lab, "SLEEP_GUARD_ONCE", self.root / "marker"))
        self.exec_pre = f"/usr/bin/bash {lab.SLEEP_GUARD} systemd-suspend.service"
        self.status = {"transaction": None, "deployments": [{"booted": False, "staged": False}]}
        self.next_args = "quiet ostree=/example"
        self.stack.enter_context(patch.object(lab, "require_command", side_effect=self.command))

    def command(self, argv):
        if argv[0] == "systemctl":
            return self.exec_pre
        if argv[1] == "status":
            return json.dumps(self.status)
        if argv[1] == "kargs":
            return self.next_args
        raise AssertionError(argv)

    def test_exact_experiment_with_finalized_recovery_passes_readonly(self):
        lab.validate_guard_bypass("platform")
        self.assertFalse(lab.SLEEP_GUARD_ONCE.exists())

    def test_staging_is_not_enough_for_forced_reset_recovery(self):
        self.status["deployments"][0]["staged"] = True
        with self.assertRaisesRegex(RuntimeError, "not finalized"):
            lab.validate_guard_bypass("platform")

    def test_retained_experiment_on_next_boot_is_rejected(self):
        self.next_args += " egpu.host_reset_test=1"
        with self.assertRaisesRegex(RuntimeError, "Next-boot"):
            lab.validate_guard_bypass("platform")

    def test_old_guard_without_service_scope_is_rejected(self):
        self.exec_pre = f"/usr/bin/bash {lab.SLEEP_GUARD}"
        with self.assertRaisesRegex(RuntimeError, "unit-scoped"):
            lab.validate_guard_bypass("platform")

    def test_normal_boot_and_wrong_stage_are_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "only.*platform"):
            lab.validate_guard_bypass("freezer")
        lab.HOST_RESET.write_text("Y")
        with self.assertRaisesRegex(RuntimeError, "not N"):
            lab.validate_guard_bypass("platform")

    def backing_experiment(self):
        lab.CMDLINE.write_text('quiet')
        lab.HOST_RESET.write_text('Y')
        lab.SLEEP_GUARD.write_text('# EGPU_SLEEP_GUARD_VRAM_STAGES_V4\n')
        source = self.root / 'helper-source.py'
        installed = self.root / 'helper-installed.py'
        source.write_text('same helper')
        installed.write_text('same helper')
        installed.chmod(0o755)
        helper = SimpleNamespace(__file__=str(source),
                                 STAGES=('freezer', 'devices', 'platform'),
                                 MARKER=self.root / 'backing-once',
                                 check_host=Mock(), check_service_policy=Mock(),
                                 check_current_mount=Mock(), check_device_policy=Mock())
        self.stack.enter_context(patch.object(lab, 'VRAM_CHECK', installed))
        self.stack.enter_context(patch.object(lab, 'vram_module', return_value=helper))
        original = Path.stat
        def stat(path, *args, **kwargs):
            value = original(path, *args, **kwargs)
            return SimpleNamespace(st_uid=0, st_mode=value.st_mode) if path == installed else value
        self.stack.enter_context(patch.object(Path, 'stat', stat))
        return helper

    def test_backing_platform_preflight_is_readonly(self):
        helper = self.backing_experiment()
        lab.validate_guard_bypass('platform', 'vram-tmpfs')
        helper.check_host.assert_called_once()
        helper.check_service_policy.assert_called_once_with(lab)
        helper.check_current_mount.assert_called_once()
        self.assertFalse(helper.MARKER.exists())
        self.assertFalse(lab.SLEEP_GUARD_ONCE.exists())

    def test_serial_preflight_checks_unchanged_baseline_without_arming(self):
        helper = self.backing_experiment()
        lab.validate_guard_bypass('platform', lab.SERIAL_MODE)
        helper.check_device_policy.assert_called_once_with(lab.SERIAL_MODE, preparing=True)
        self.assertFalse(helper.MARKER.exists())
        self.assertFalse(lab.SLEEP_GUARD_ONCE.exists())

    def test_rtc_preflight_requires_matching_helper_and_readonly_baseline(self):
        helper = self.backing_experiment()
        lab.validate_guard_bypass('platform', lab.RTC_MODE)
        helper.check_device_policy.assert_called_once_with(lab.RTC_MODE, preparing=True)
        self.assertFalse(helper.MARKER.exists())
        self.assertFalse(lab.SLEEP_GUARD_ONCE.exists())
        lab.VRAM_CHECK.write_text('old helper')
        with self.assertRaisesRegex(RuntimeError, 'differs'):
            lab.validate_guard_bypass('platform', lab.RTC_MODE)
        helper.check_device_policy.reset_mock()
        with self.assertRaisesRegex(RuntimeError, 'explicit platform'):
            lab.validate_guard_bypass('devices', lab.RTC_MODE)
        helper.check_device_policy.assert_not_called()

    def test_serial_preflight_refuses_wrong_stage_or_device_policy(self):
        helper = self.backing_experiment()
        with self.assertRaisesRegex(RuntimeError, 'explicit platform'):
            lab.validate_guard_bypass('devices', lab.SERIAL_MODE)
        helper.check_device_policy.assert_not_called()
        helper.check_device_policy.side_effect = RuntimeError('pm_async/trace mismatch')
        with self.assertRaisesRegex(RuntimeError, 'mismatch'):
            lab.validate_guard_bypass('platform', lab.SERIAL_MODE)
        helper.check_host.assert_not_called()
        self.assertFalse(helper.MARKER.exists())

    def test_backing_preflight_rejects_old_guard_and_mismatched_helper(self):
        self.backing_experiment()
        lab.SLEEP_GUARD.write_text('# EGPU_SLEEP_GUARD_VRAM_STAGES_V3\n')
        with self.assertRaisesRegex(RuntimeError, 'backing-store-aware'):
            lab.validate_guard_bypass('platform', 'vram-tmpfs')
        lab.SLEEP_GUARD.write_text('# EGPU_SLEEP_GUARD_VRAM_STAGES_V4\n')
        lab.VRAM_CHECK.write_text('different archived helper')
        with self.assertRaisesRegex(RuntimeError, 'differs'):
            lab.validate_guard_bypass('platform', 'vram-tmpfs')

    def test_backing_preflight_rejects_normal_sleep_and_stale_marker(self):
        helper = self.backing_experiment()
        with self.assertRaisesRegex(RuntimeError, 'no full sleep'):
            lab.validate_guard_bypass('none', 'vram-tmpfs')
        helper.check_host.assert_not_called()
        helper.MARKER.write_text('pending permission')
        with self.assertRaisesRegex(RuntimeError, 'stale'):
            lab.validate_guard_bypass('platform', 'vram-tmpfs')
        self.assertEqual(helper.MARKER.read_text(), 'pending permission')

    def cardwire_experiment(self):
        lab.CMDLINE.write_text('quiet')
        lab.HOST_RESET.write_text('Y')
        lab.SLEEP_GUARD.write_text('# EGPU_SLEEP_GUARD_CARDWIRE_OFF_V1\n')
        mask = self.root / 'cardwired.service'
        mask.symlink_to('/dev/null')
        self.stack.enter_context(patch.object(lab, 'CARDWIRE_MASK', mask))
        self.proc = self.root / 'proc'
        self.proc.mkdir()
        self.stack.enter_context(patch.object(lab, 'PROC_ROOT', self.proc))
        self.service = self.stack.enter_context(patch.object(lab, 'unit_state', return_value=state()))

    def test_cardwire_preflight_passes_without_creating_marker(self):
        self.cardwire_experiment()
        lab.validate_guard_bypass('platform', 'cardwire-off')
        self.assertFalse(lab.SLEEP_GUARD_ONCE.exists())

    def test_cardwire_preflight_requires_mask_and_inactive_state(self):
        self.cardwire_experiment()
        self.service.return_value = state(active='active')
        with self.assertRaisesRegex(RuntimeError, 'inactive'):
            lab.validate_guard_bypass('platform', 'cardwire-off')
        self.service.return_value = state()
        lab.CARDWIRE_MASK.unlink()
        with self.assertRaisesRegex(RuntimeError, 'runtime-masked'):
            lab.validate_guard_bypass('platform', 'cardwire-off')

    def test_cardwire_preflight_rejects_unmanaged_process(self):
        self.cardwire_experiment()
        (self.proc / '123').mkdir()
        (self.proc / '123/comm').write_text('cardwired\n')
        with self.assertRaisesRegex(RuntimeError, 'still running'):
            lab.validate_guard_bypass('platform', 'cardwire-off')

    def test_cardwire_preflight_rejects_old_guard(self):
        self.cardwire_experiment()
        lab.SLEEP_GUARD.write_text('old version')
        with self.assertRaisesRegex(RuntimeError, 'Cardwire-aware'):
            lab.validate_guard_bypass('platform', 'cardwire-off')

    def test_cardwire_preflight_rejects_host_reset_ab_and_staged_recovery(self):
        self.cardwire_experiment()
        lab.CMDLINE.write_text('quiet egpu.host_reset_test=1')
        with self.assertRaisesRegex(RuntimeError, 'normal host_reset'):
            lab.validate_guard_bypass('platform', 'cardwire-off')
        lab.CMDLINE.write_text('quiet')
        lab.HOST_RESET.write_text('N')
        with self.assertRaisesRegex(RuntimeError, 'normal host_reset'):
            lab.validate_guard_bypass('platform', 'cardwire-off')
        lab.HOST_RESET.write_text('Y')
        self.status['deployments'][0]['staged'] = True
        with self.assertRaisesRegex(RuntimeError, 'not finalized'):
            lab.validate_guard_bypass('platform', 'cardwire-off')


class ExperimentTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.folder = self.root / "logs"
        self.folder.mkdir()
        self.pm = PMControl()
        self.pm_async = self.root / 'async'
        self.pm_async.write_text('1')
        self.stack.enter_context(patch.object(lab, 'PM_ASYNC', self.pm_async))
        self.pm_trace = self.root / 'trace'
        self.pm_trace.write_text('0')
        self.stack.enter_context(patch.object(lab, 'PM_TRACE', self.pm_trace))
        self.depth = self.root / "depth"
        self.depth.write_text("default modeset uvm")
        self.flags = (self.root / "debug", self.root / "times")
        self.flags[0].write_text("0")
        self.flags[1].write_text("1")
        self.conf = self.root / "sleep.conf"
        self.unitconf = self.root / "unit.conf"
        self.guard_once = self.root / "guard-once"
        self.stack.enter_context(patch.object(lab, "SLEEP_GUARD_ONCE", self.guard_once))
        self.request_rc = 0
        self.request_output = "test request"
        self.stage = "freezer"
        self.log = GOOD_LOG
        self.config = "[Sleep]\nSuspendState=\nSuspendState=mem\n"
        self.stack.enter_context(patch.object(lab, "PM_TEST", self.pm))
        self.stack.enter_context(patch.object(lab, "DEPTH", self.depth))
        self.stack.enter_context(patch.object(lab, "FLAGS", self.flags))
        self.stack.enter_context(patch.object(lab, "SLEEP_CONF", self.conf))
        self.stack.enter_context(patch.object(lab, "UNIT_CONF", self.unitconf))
        self.stack.enter_context(patch.object(lab, "preflight", return_value={"kernel": "test"}))
        self.stack.enter_context(patch.object(lab, "snapshot"))
        self.stack.enter_context(patch.object(lab, "observer"))
        self.stack.enter_context(patch.object(lab, "save_command"))
        self.label = self.stack.enter_context(patch.object(lab, "label_overrides"))
        self.stack.enter_context(patch.object(lab.os, "sync"))
        self.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        self.stack.enter_context(patch.object(lab, "unit_state", return_value=state()))
        self.calls = self.stack.enter_context(patch.object(lab, "command", side_effect=self.fake_command))
        self.stack.enter_context(patch.object(lab, "require_command", side_effect=self.fake_required))
        self.wait = self.stack.enter_context(patch.object(lab, "wait_for_cycle", side_effect=self.finish))

    def fake_command(self, argv, timeout=10):
        if argv == lab.SUSPEND_REQUEST:
            self.assertEqual(self.pm.value, self.stage)
            return subprocess.CompletedProcess(argv, self.request_rc, self.request_output)
        return subprocess.CompletedProcess(argv, 0, "")

    def fake_required(self, argv):
        if argv[0] == "systemd-analyze":
            return self.config
        if "Environment" in argv:
            return "Environment=SYSTEMD_LOG_LEVEL=debug"
        if "-k" in argv:
            return self.log
        return ""

    def finish(self, baseline, **kwargs):
        # No reset to normal sleep is permitted while the cycle is in flight.
        self.assertEqual(self.pm.value, self.stage)
        self.assertTrue(self.conf.exists())
        self.assertTrue(self.unitconf.exists())
        self.assertEqual(baseline, "100")
        return state("101")

    def assert_restored(self):
        self.assertEqual(self.pm.value, "none")
        self.assertEqual([p.read_text().strip() for p in self.flags], ["0", "1"])
        self.assertEqual(self.depth.read_text().strip(), "default")
        self.assertFalse(self.conf.exists())
        self.assertFalse(self.unitconf.exists())
        self.assertTrue((self.folder / "restored.txt").exists())

    def assert_retained(self):
        self.assertEqual(self.pm.value, self.stage)
        self.assertTrue(self.conf.exists())
        self.assertTrue(self.unitconf.exists())
        self.assertFalse((self.folder / "restored.txt").exists())
        self.assertTrue((self.folder / "reboot-clears-test.txt").exists())
        self.assertEqual(sum(c.args[0] == lab.SUSPEND_REQUEST for c in self.calls.call_args_list), 1)

    def test_success_restores_after_cycle(self):
        self.assertEqual(lab.experiment(self.folder), 0)
        self.assert_restored()
        self.label.assert_called_once()

    def trace_helper(self):
        helper = Mock()
        capture = helper.Capture.return_value
        def start():
            self.assertEqual(self.pm.value, 'none')
            self.assertFalse(self.conf.exists())
        capture.start.side_effect = start
        self.stack.enter_context(patch.object(lab, 'trace_module', return_value=helper))
        return capture

    def test_optional_trace_preserves_cycle_and_cleanup(self):
        capture = self.trace_helper()
        self.assertEqual(lab.experiment(self.folder, kernel_trace=True), 0)
        capture.start.assert_called_once()
        capture.ensure_running.assert_called_once()
        capture.stop.assert_called_once()
        self.assert_restored()

    def test_trace_start_failure_never_requests_sleep(self):
        capture = self.trace_helper()
        capture.start.side_effect = RuntimeError('trace preflight failed')
        with self.assertRaisesRegex(RuntimeError, 'trace preflight'):
            lab.experiment(self.folder, kernel_trace=True)
        self.assertFalse(any(c.args[0] == lab.SUSPEND_REQUEST for c in self.calls.call_args_list))
        capture.stop.assert_called_once()
        self.assert_restored()

    def test_trace_dying_before_request_restores_without_sleep(self):
        capture = self.trace_helper()
        capture.ensure_running.side_effect = RuntimeError('capture stopped')
        with self.assertRaisesRegex(RuntimeError, 'capture stopped'):
            lab.experiment(self.folder, kernel_trace=True)
        self.assertFalse(any(c.args[0] == lab.SUSPEND_REQUEST for c in self.calls.call_args_list))
        self.assert_restored()

    def test_trace_cleanup_error_cannot_skip_pm_cleanup(self):
        capture = self.trace_helper()
        capture.stop.side_effect = OSError('trace instance busy')
        self.assertEqual(lab.experiment(self.folder, kernel_trace=True), 0)
        self.assert_restored()

    def test_uncertain_cycle_stops_trace_but_retains_pm_test(self):
        capture = self.trace_helper()
        self.wait.side_effect = RuntimeError('uncertain cycle')
        with self.assertRaisesRegex(RuntimeError, 'uncertain cycle'):
            lab.experiment(self.folder, kernel_trace=True)
        capture.stop.assert_called_once()
        self.assert_retained()

    def vram_helper(self):
        from unittest.mock import Mock
        marker = self.root / 'freezer-marker'
        helper = SimpleNamespace(MARKER=marker, TMPFS='/var/tmp:rw,size=6G')
        def arm(folder, mode, stage='freezer'):
            self.assertEqual(self.pm.value, stage)
            if mode in lab.SERIAL_MODES:
                self.assertEqual(self.pm_async.read_text().strip(), '0')
            self.assertEqual(self.pm_trace.read_text().strip(),
                             '1' if mode == lab.RTC_MODE else '0')
            marker.write_text('one-shot')
        helper.arm = Mock(side_effect=arm)
        self.stack.enter_context(patch.object(lab, 'vram_module', return_value=helper))
        return helper

    def test_serial_platform_restores_async_after_one_complete_cycle(self):
        self.stage, self.log = 'platform', PLATFORM_LOG
        helper = self.vram_helper()
        def serial_finish(*args, **kwargs):
            self.assertEqual(self.pm_async.read_text().strip(), '0')
            self.assertIn('TemporaryFileSystem=', self.unitconf.read_text())
            return self.finish(*args, **kwargs)
        self.wait.side_effect = serial_finish
        with patch.object(lab, 'arm_guard_platform_once') as old_arm:
            self.assertEqual(lab.experiment(self.folder, 'platform', lab.SERIAL_MODE), 0)
            old_arm.assert_not_called()
        helper.arm.assert_called_once_with(self.folder, lab.SERIAL_MODE, stage='platform')
        self.assertEqual(json.loads((self.folder / 'before.json').read_text())['pm_async'], '1')
        self.assertEqual(self.pm_async.read_text().strip(), '1')
        self.assertFalse(helper.MARKER.exists())
        self.assert_restored()
        self.assertEqual(sum(c.args[0] == lab.SUSPEND_REQUEST for c in self.calls.call_args_list), 1)

    def test_rtc_platform_restores_trace_and_async_after_complete_cycle(self):
        self.stage, self.log = 'platform', PLATFORM_LOG
        helper = self.vram_helper()
        self.assertEqual(lab.experiment(self.folder, 'platform', lab.RTC_MODE), 0)
        helper.arm.assert_called_once_with(self.folder, lab.RTC_MODE, stage='platform')
        self.assertEqual(self.pm_trace.read_text().strip(), '0')
        self.assertEqual(self.pm_async.read_text().strip(), '1')
        self.assertIn('NTP resync', (self.folder / 'restored.txt').read_text())
        self.assertFalse(helper.MARKER.exists())
        self.assert_restored()

    def test_rtc_uncertain_cycle_retains_trace_and_revokes_permission(self):
        self.stage = 'platform'
        helper = self.vram_helper()
        self.wait.side_effect = RuntimeError('uncertain')
        with self.assertRaisesRegex(RuntimeError, 'uncertain'):
            lab.experiment(self.folder, 'platform', lab.RTC_MODE)
        self.assertEqual(self.pm_trace.read_text().strip(), '1')
        self.assertEqual(self.pm_async.read_text().strip(), '0')
        self.assertFalse(helper.MARKER.exists())
        self.assertIn('RTC clock may be wrong', (self.folder / 'reboot-clears-test.txt').read_text())
        self.assert_retained()

    def test_rtc_arm_failure_restores_trace_without_sleep(self):
        self.stage = 'platform'
        helper = self.vram_helper()
        helper.arm.side_effect = RuntimeError('arm refused')
        with self.assertRaisesRegex(RuntimeError, 'arm refused'):
            lab.experiment(self.folder, 'platform', lab.RTC_MODE)
        self.assertEqual(self.pm_trace.read_text().strip(), '0')
        self.assertEqual(self.pm_async.read_text().strip(), '1')
        self.assertFalse(any(c.args[0] == lab.SUSPEND_REQUEST for c in self.calls.call_args_list))

    def test_serial_uncertain_cycle_retains_async_and_stage_but_revokes_permission(self):
        self.stage = 'platform'
        helper = self.vram_helper()
        self.wait.side_effect = RuntimeError('uncertain')
        with self.assertRaisesRegex(RuntimeError, 'uncertain'):
            lab.experiment(self.folder, 'platform', lab.SERIAL_MODE)
        self.assertEqual(self.pm_async.read_text().strip(), '0')
        self.assertFalse(helper.MARKER.exists())
        self.assert_retained()
        self.assertNotIn('none', self.pm.writes)

    def test_serial_arm_failure_restores_async_without_sleep(self):
        self.stage = 'platform'
        helper = self.vram_helper()
        helper.arm.side_effect = RuntimeError('arm refused')
        with self.assertRaisesRegex(RuntimeError, 'arm refused'):
            lab.experiment(self.folder, 'platform', lab.SERIAL_MODE)
        self.assertEqual(self.pm_async.read_text().strip(), '1')
        self.assert_restored()
        self.assertFalse(any(c.args[0] == lab.SUSPEND_REQUEST for c in self.calls.call_args_list))

    def test_serial_incomplete_stage_is_not_success_but_finished_cycle_restores(self):
        self.stage, self.log = 'platform', DEVICES_LOG
        helper = self.vram_helper()
        self.assertEqual(lab.experiment(self.folder, 'platform', lab.SERIAL_MODE), 1)
        self.assertEqual(self.pm_async.read_text().strip(), '1')
        self.assertFalse(helper.MARKER.exists())
        self.assert_restored()

    def test_invalid_saved_async_value_refuses_restoration_before_any_write(self):
        self.conf.write_text('owned diagnostic')
        saved = {'pm_test': 'none', 'flags': {}, 'pm_async': 'bad'}
        with self.assertRaisesRegex(RuntimeError, 'saved pm_async'):
            lab.finish_settings(saved, [self.conf])
        self.assertEqual(self.conf.read_text(), 'owned diagnostic')
        self.assertEqual(self.depth.read_text(), 'default modeset uvm')
        self.assertEqual(self.pm.writes, [])
        self.calls.assert_not_called()

    def test_serial_definitive_refusal_restores_async_without_retry(self):
        self.stage = 'platform'
        helper = self.vram_helper()
        self.request_rc = 1
        self.request_output = 'Call failed: Operation denied due to active block inhibitor'
        with patch.object(lab, 'assert_sleep_idle') as idle:
            with self.assertRaisesRegex(RuntimeError, 'refused before the test'):
                lab.experiment(self.folder, 'platform', lab.SERIAL_MODE)
            idle.assert_called_once_with('100')
        self.assertEqual(self.pm_async.read_text().strip(), '1')
        self.assertFalse(helper.MARKER.exists())
        self.assert_restored()
        self.wait.assert_not_called()
        self.assertEqual(sum(c.args[0] == lab.SUSPEND_REQUEST for c in self.calls.call_args_list), 1)

    def test_serial_changed_baseline_refuses_before_any_mutation(self):
        helper = self.vram_helper()
        for value in ('0', 'bad'):
            with self.subTest(value=value):
                self.pm_async.write_text(value)
                with self.assertRaisesRegex(RuntimeError, 'unchanged pm_async=1'):
                    lab.experiment(self.folder, 'platform', lab.SERIAL_MODE)
                self.assertEqual(self.pm_async.read_text(), value)
                self.assertFalse(self.conf.exists())
                self.assertFalse(self.unitconf.exists())
                self.assertEqual(self.pm.writes, [])
        helper.arm.assert_not_called()
        self.calls.assert_not_called()

    def test_ordinary_experiment_does_not_touch_async_policy(self):
        self.pm_async.write_text('unrelated sentinel')
        self.assertEqual(lab.experiment(self.folder), 0)
        self.assertEqual(self.pm_async.read_text(), 'unrelated sentinel')
        self.assertNotIn('pm_async', json.loads((self.folder / 'before.json').read_text()))

    def test_vram_success_removes_override_and_unused_marker(self):
        helper = self.vram_helper()
        self.assertEqual(lab.experiment(self.folder, 'freezer', 'vram-tmpfs'), 0)
        helper.arm.assert_called_once_with(self.folder, 'vram-tmpfs', stage='freezer')
        self.assertFalse(helper.MARKER.exists())
        self.assert_restored()

    def test_vram_uncertain_request_keeps_pm_test_but_revokes_exception(self):
        helper = self.vram_helper()
        self.wait.side_effect = RuntimeError('uncertain')
        with self.assertRaises(RuntimeError):
            lab.experiment(self.folder, 'freezer', 'vram-tmpfs')
        self.assertFalse(helper.MARKER.exists())
        self.assert_retained()
        self.assertIn('TemporaryFileSystem=', self.unitconf.read_text())

    def test_vram_devices_roundtrip_arms_only_devices_and_restores(self):
        self.stage, self.log = 'devices', DEVICES_LOG
        helper = self.vram_helper()
        self.assertEqual(lab.experiment(self.folder, 'devices', 'vram-tmpfs'), 0)
        helper.arm.assert_called_once_with(self.folder, 'vram-tmpfs', stage='devices')
        self.assertFalse(helper.MARKER.exists())
        self.assert_restored()
        self.assertEqual(sum(c.args[0] == lab.SUSPEND_REQUEST for c in self.calls.call_args_list), 1)

    def test_vram_devices_uncertain_request_has_no_retry_or_real_sleep(self):
        self.stage = 'devices'
        helper = self.vram_helper()
        self.wait.side_effect = RuntimeError('uncertain')
        with self.assertRaises(RuntimeError):
            lab.experiment(self.folder, 'devices', 'vram-tmpfs')
        self.assertFalse(helper.MARKER.exists())
        self.assert_retained()
        self.assertNotIn('none', self.pm.writes)

    def test_vram_platform_roundtrip_arms_only_platform_and_restores(self):
        self.stage, self.log = 'platform', PLATFORM_LOG
        helper = self.vram_helper()
        with patch.object(lab, 'arm_guard_platform_once') as old_arm:
            self.assertEqual(lab.experiment(self.folder, 'platform', 'vram-tmpfs'), 0)
            old_arm.assert_not_called()
        helper.arm.assert_called_once_with(self.folder, 'vram-tmpfs', stage='platform')
        self.assertFalse(helper.MARKER.exists())
        self.assert_restored()
        self.assertEqual(self.pm.writes, ['platform', 'none'])
        self.assertEqual(sum(c.args[0] == lab.SUSPEND_REQUEST for c in self.calls.call_args_list), 1)

    def test_vram_platform_uncertain_request_keeps_stage_and_revokes_permission(self):
        self.stage = 'platform'
        helper = self.vram_helper()
        self.wait.side_effect = RuntimeError('uncertain')
        with self.assertRaises(RuntimeError):
            lab.experiment(self.folder, 'platform', 'vram-tmpfs')
        self.assertFalse(helper.MARKER.exists())
        self.assert_retained()
        self.assertNotIn('none', self.pm.writes)
        self.assertIn('TemporaryFileSystem=', self.unitconf.read_text())

    def test_vram_platform_partial_device_return_is_not_platform_success(self):
        self.stage, self.log = 'platform', DEVICES_LOG
        helper = self.vram_helper()
        self.assertEqual(lab.experiment(self.folder, 'platform', 'vram-tmpfs'), 1)
        self.assertFalse(helper.MARKER.exists())
        self.assert_restored()
        self.assertIn('PLATFORM STAGE NOT CONFIRMED', (self.folder / 'result.txt').read_text())

    def test_devices_restores_without_full_sleep_or_retry(self):
        self.stage = "devices"
        self.log = DEVICES_LOG
        self.assertEqual(lab.experiment(self.folder, self.stage), 0)
        self.assert_restored()
        self.assertEqual(self.pm.writes, ["devices", "none"])
        self.assertEqual(sum(c.args[0] == lab.SUSPEND_REQUEST for c in self.calls.call_args_list), 1)
        self.assertIn("DEVICES STAGE RETURNED", (self.folder / "result.txt").read_text())

    def test_devices_timeout_preserves_selected_guard(self):
        self.stage = "devices"
        self.wait.side_effect = RuntimeError("timeout")
        with self.assertRaises(RuntimeError):
            lab.experiment(self.folder, self.stage)
        self.assert_retained()
        self.assertIn("pm_test=devices", (self.folder / "reboot-clears-test.txt").read_text())

    def test_platform_restores_without_entering_real_sleep(self):
        self.stage = "platform"
        self.log = PLATFORM_LOG
        self.assertEqual(lab.experiment(self.folder, self.stage), 0)
        self.assert_restored()
        self.assertEqual(self.pm.writes, ["platform", "none"])
        self.assertEqual(sum(c.args[0] == lab.SUSPEND_REQUEST for c in self.calls.call_args_list), 1)
        self.assertIn("PLATFORM STAGE RETURNED", (self.folder / "result.txt").read_text())

    def test_unconsumed_exception_is_removed_even_on_request_error(self):
        self.stage = "platform"
        self.request_rc = 1
        def arm(_):
            self.assertEqual(self.pm.value, "platform")
            self.guard_once.write_text("test-boot\nplatform\n")
        with patch.object(lab, "arm_guard_platform_once", side_effect=arm) as arm_call:
            with self.assertRaises(RuntimeError):
                lab.experiment(self.folder, "platform", True)
            arm_call.assert_called_once()
        self.assertFalse(self.guard_once.exists())
        self.assert_retained()

    def test_cardwire_exception_failure_is_cleaned_without_retry(self):
        self.stage = 'platform'
        self.request_rc = 1
        def arm(folder, experiment):
            self.assertEqual(experiment, 'cardwire-off')
            self.assertEqual(self.pm.value, 'platform')
            self.guard_once.write_text('test-boot\nplatform\ncardwire-off\n')
        with patch.object(lab, 'arm_guard_platform_once', side_effect=arm) as arm_call:
            with self.assertRaises(RuntimeError):
                lab.experiment(self.folder, 'platform', 'cardwire-off')
            arm_call.assert_called_once_with(self.folder, 'cardwire-off')
        self.assertFalse(self.guard_once.exists())
        self.assert_retained()

    def test_label_failure_aborts_before_sleep_and_restores(self):
        self.label.side_effect = RuntimeError("label mismatch")
        with self.assertRaisesRegex(RuntimeError, "label mismatch"):
            lab.experiment(self.folder)
        self.assert_restored()
        self.assertFalse(any(c.args[0] == lab.SUSPEND_REQUEST for c in self.calls.call_args_list))

    def test_failed_finished_cycle_also_restores(self):
        self.wait.side_effect = lambda _, **kwargs: state("101", "failed", "exit-code")
        self.assertEqual(lab.experiment(self.folder), 1)
        self.assert_restored()

    def test_failed_setup_restores_without_sleep(self):
        self.config = "[Sleep]\nSuspendState=mem freeze\n"
        with self.assertRaises(RuntimeError):
            lab.experiment(self.folder)
        self.assert_restored()
        self.assertFalse(any(c.args[0] == lab.SUSPEND_REQUEST for c in self.calls.call_args_list))

    def test_timeout_never_restores_to_real_sleep_early(self):
        self.wait.side_effect = RuntimeError("timeout")
        with self.assertRaises(RuntimeError):
            lab.experiment(self.folder)
        self.assert_retained()

    def test_request_error_never_retries(self):
        self.request_rc = 1
        with self.assertRaises(RuntimeError):
            lab.experiment(self.folder)
        self.assert_retained()
        self.wait.assert_not_called()

    def test_confirmed_refusal_restores_without_reboot(self):
        self.request_rc = 1
        self.request_output = "Call failed: Operation denied due to active block inhibitor"
        with patch.object(lab, "assert_sleep_idle") as idle:
            with self.assertRaisesRegex(RuntimeError, "refused before the test"):
                lab.experiment(self.folder)
            idle.assert_called_once_with("100")
        self.assert_restored()
        self.wait.assert_not_called()

    def test_refusal_with_concurrent_sleep_still_retains_guard(self):
        self.request_rc = 1
        self.request_output = "Call failed: Operation denied due to active block inhibitor"
        with patch.object(lab, "assert_sleep_idle", side_effect=RuntimeError("pending")):
            with self.assertRaises(RuntimeError):
                lab.experiment(self.folder)
        self.assert_retained()

    def prepare_cleanup(self):
        self.pm.value = "freezer"
        saved = {"boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
                 "pm_test": "none", "flags": {str(self.flags[0]): "0", str(self.flags[1]): "1"}}
        (self.folder / "before.json").write_text(json.dumps(saved))
        (self.folder / "request.txt").write_text(
            "Please retry operation after closing inhibitors and logging out other users.\nexit=1\n")
        self.conf.write_text("[Sleep]\nSuspendState=\nSuspendState=mem\n")
        self.unitconf.write_text(lab.unit_override(self.folder))
        self.stack.enter_context(patch.object(lab, "LOG_ROOT", self.root))
        self.stack.enter_context(patch.object(lab, "LOCK", self.root / "lock"))
        self.stack.enter_context(patch.object(lab.os, "geteuid", return_value=0))
        original_stat = Path.stat
        self.stack.enter_context(patch.object(Path, "stat", lambda p, **kw:
            SimpleNamespace(st_uid=0) if p == self.folder else original_stat(p, **kw)))
        return self.stack.enter_context(patch.object(lab, "assert_sleep_idle"))

    def test_explicit_cleanup_after_legacy_session_refusal(self):
        idle = self.prepare_cleanup()
        lab.cleanup_rejected(self.folder)
        idle.assert_called_once()
        self.assert_restored()

    def test_cleanup_refuses_foreign_override(self):
        self.prepare_cleanup()
        self.unitconf.write_text("belongs to another run")
        with self.assertRaisesRegex(RuntimeError, "does not belong"):
            lab.cleanup_rejected(self.folder)
        self.assertEqual(self.pm.value, "freezer")
        self.assertTrue(self.conf.exists())

    def test_cleanup_refuses_unknown_request_error(self):
        self.prepare_cleanup()
        (self.folder / "request.txt").write_text("Timed out\nexit=1\n")
        with self.assertRaisesRegex(RuntimeError, "not definitively rejected"):
            lab.cleanup_rejected(self.folder)
        self.assertEqual(self.pm.value, "freezer")

    def test_interrupt_after_enqueue_retains_guard(self):
        self.wait.side_effect = InterruptedError("stopped")
        with self.assertRaises(InterruptedError):
            lab.experiment(self.folder)
        self.assert_retained()

    def test_foreign_override_is_not_deleted(self):
        self.conf.write_text("someone else's diagnostic")
        with self.assertRaises(FileExistsError):
            lab.experiment(self.folder)
        self.assertEqual(self.conf.read_text(), "someone else's diagnostic")
        self.assertFalse(any(c.args[0] == lab.SUSPEND_REQUEST for c in self.calls.call_args_list))


class NvidiaPmTransportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.active = root / 'active-boot'
        self.hook = root / 'hook.py'
        self.hook.write_text('paired hook')
        self.active.write_text('test-boot\n')
        self.active.chmod(0o600)
        self.owner = patch.object(lab, 'PROCFS_PM_MARKER_OWNER', self.active.stat().st_uid)
        self.owner.start()
        self.addCleanup(self.owner.stop)
        self.paths = [patch.object(lab, 'PROCFS_PM_ACTIVE', self.active),
                      patch.object(lab, 'PROCFS_PM_HOOK', self.hook)]
        for item in self.paths:
            item.start()
            self.addCleanup(item.stop)

    def systemctl_output(self, argv):
        if 'ExecStartPre' in argv:
            return (f'argv[]=/usr/bin/bash {lab.SLEEP_GUARD} systemd-suspend.service ; ignore_errors=no ; '
                    f'argv[]=/usr/bin/python3 {self.hook} pre ; ignore_errors=no')
        return f'argv[]=/usr/bin/python3 {self.hook} post ; ignore_errors=no'

    def test_normal_notifier_mode_requires_no_ab_marker(self):
        self.active.unlink()
        self.assertEqual(lab.nvidia_pm_mode('UseKernelSuspendNotifiers: 1\n', 'test-boot'),
                         'kernel-notifier')
        self.active.write_text('test-boot\n')
        with self.assertRaisesRegex(RuntimeError, 'marker exists'):
            lab.nvidia_pm_mode('UseKernelSuspendNotifiers: 1\n', 'test-boot')

    def test_paired_procfs_mode_is_accepted_only_with_ordered_hooks(self):
        with patch.object(lab, 'require_command', side_effect=self.systemctl_output):
            self.assertEqual(lab.nvidia_pm_mode('UseKernelSuspendNotifiers: 0\n', 'test-boot'),
                             'paired-procfs')

    def test_disabled_notifier_without_marker_or_hook_is_rejected(self):
        self.active.unlink()
        with self.assertRaisesRegex(RuntimeError, 'valid procfs PM boot marker'):
            lab.nvidia_pm_mode('UseKernelSuspendNotifiers: 0\n', 'test-boot')
        self.active.write_text('test-boot\n')
        self.active.chmod(0o600)
        self.hook.unlink()
        with self.assertRaisesRegex(RuntimeError, 'not installed'):
            lab.nvidia_pm_mode('UseKernelSuspendNotifiers: 0\n', 'test-boot')

    def test_stale_marker_and_missing_resume_are_rejected(self):
        with self.assertRaisesRegex(RuntimeError, 'stale or unsafe'):
            lab.nvidia_pm_mode('UseKernelSuspendNotifiers: 0\n', 'other-boot')
        with patch.object(lab, 'require_command', return_value='no paired hook'):
            with self.assertRaisesRegex(RuntimeError, 'missing, ignored or ordered'):
                lab.nvidia_pm_mode('UseKernelSuspendNotifiers: 0\n', 'test-boot')

    def test_duplicate_or_unknown_notifier_value_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, 'missing or ambiguous'):
            lab.nvidia_pm_mode('UseKernelSuspendNotifiers: 0\nUseKernelSuspendNotifiers: 1\n',
                               'test-boot')
        with self.assertRaisesRegex(RuntimeError, 'missing or ambiguous'):
            lab.nvidia_pm_mode('UseKernelSuspendNotifiers: 2\n', 'test-boot')


if __name__ == "__main__":
    unittest.main()
