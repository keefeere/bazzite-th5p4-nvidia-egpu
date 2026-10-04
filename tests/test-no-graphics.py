"""Hardware-free no-graphics checks. Never stops a real service or sleeps."""
import importlib.util
import json
import os
import pwd
from pathlib import Path
import subprocess
import tempfile
import socket
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

REPO = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gate = load('gate', REPO / 'egpu-no-graphics-check.py')
graphics = load('graphics', REPO / 'diagnostics/egpu_no_graphics.py')


class GateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.proc = self.root / 'proc'
        self.proc.mkdir()
        self.runtime = self.root / 'runtime'
        self.runtime.mkdir()
        self.sys = self.root / 'sys'
        for name in ('nvidia', 'nvidia_uvm', 'nvidia_modeset', 'nvidia_drm'):
            (self.sys / 'module' / name).mkdir(parents=True)
        for unit in gate.UNITS:
            (self.runtime / unit).symlink_to('/dev/null')
        self.states = {unit: {'ActiveState': 'inactive', 'MainPID': '0'} for unit in
                       (*gate.UNITS, 'display-manager.service', 'nvidia-persistenced.service')}
        self.patches = [patch.object(gate, 'PROC', self.proc), patch.object(gate, 'SYS', self.sys),
                        patch.object(gate, 'RUNTIME', self.runtime),
                        patch.object(gate, 'service', side_effect=lambda u: self.states[u]),
                        patch.object(gate, 'gpu_devices', return_value={os.stat('/dev/null').st_rdev})]
        self.link = os.readlink
        self.patches.append(patch.object(gate.os, 'readlink', side_effect=self.readlink))
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def readlink(self, path):
        return '/dev/nvidia0' if Path(path).parent.name == 'fd' else self.link(path)

    def process(self, pid, executable='/usr/bin/kwin_wayland'):
        process = self.proc / str(pid)
        (process / 'fd').mkdir(parents=True)
        (process / 'comm').write_text(Path(executable).name + '\n')
        (process / 'exe').symlink_to(executable)
        (process / 'fd/1').symlink_to('/dev/null')

    def test_quiet_state_passes(self):
        self.assertEqual(gate.validate()['gpu_clients'], [])

    def test_only_exact_persistence_service_pid_is_allowed(self):
        self.process(41, '/usr/bin/nvidia-persistenced')
        self.states['nvidia-persistenced.service'] = {'ActiveState': 'active', 'MainPID': '41'}
        self.assertTrue(gate.validate()['gpu_clients'][0]['allowed_persistence'])
        self.states['nvidia-persistenced.service']['MainPID'] = '42'
        with self.assertRaisesRegex(RuntimeError, 'GPU clients remain'):
            gate.validate()

    def test_matching_persistence_pid_with_wrong_executable_is_rejected(self):
        self.process(41)
        self.states['nvidia-persistenced.service'] = {'ActiveState': 'active', 'MainPID': '41'}
        with self.assertRaisesRegex(RuntimeError, 'GPU clients remain'):
            gate.validate()

    def test_remaining_graphical_client_is_rejected(self):
        self.process(52)
        with self.assertRaisesRegex(RuntimeError, 'kwin_wayland'):
            gate.validate()

    def test_compositor_is_rejected_even_after_closing_device_descriptors(self):
        self.process(52)
        (self.proc / '52/fd/1').unlink()
        with self.assertRaisesRegex(RuntimeError, 'kwin_wayland'):
            gate.validate()

    def test_daemon_access_failure_is_not_silently_ignored(self):
        self.process(52)
        with patch.object(gate.os, 'readlink', side_effect=PermissionError('denied')):
            with self.assertRaises(PermissionError):
                gate.clients()

    def test_missing_mask_is_rejected(self):
        (self.runtime / 'displaylink.service').unlink()
        with self.assertRaisesRegex(RuntimeError, 'runtime-masked'):
            gate.validate()

    def test_live_alias_is_rejected(self):
        self.states['display-manager.service']['ActiveState'] = 'active'
        with self.assertRaisesRegex(RuntimeError, 'alias'):
            gate.validate()

    def test_loaded_driver_is_required(self):
        (self.sys / 'module/nvidia_uvm').rmdir()
        with self.assertRaisesRegex(RuntimeError, 'nvidia_uvm'):
            gate.validate()


class UserBusTests(unittest.TestCase):
    def test_direct_transport_drops_identity_without_pam_or_machine_bridge(self):
        account = pwd.getpwuid(os.getuid())
        with tempfile.TemporaryDirectory() as directory, socket.socket(socket.AF_UNIX) as bus:
            runtime = Path(directory) / str(account.pw_uid)
            runtime.mkdir()
            bus.bind(str(runtime / 'bus'))
            with patch.object(graphics, 'USER_RUNTIME', Path(directory)):
                argv = graphics.user_command({'uid': str(account.pw_uid), 'name': account.pw_name},
                                             'show', 'graphical-session.target')
            self.assertEqual(argv[0], '/usr/bin/setpriv')
            self.assertIn('--reuid=' + str(account.pw_uid), argv)
            self.assertIn('--regid=' + str(account.pw_gid), argv)
            self.assertIn('DBUS_SESSION_BUS_ADDRESS=unix:path=' + str(runtime / 'bus'), argv)
            self.assertNotIn('runuser', argv)
            self.assertFalse(any('--machine' in arg for arg in argv))
            self.assertIn('-i', argv)

    def test_fake_bus_and_identity_mismatch_are_rejected(self):
        account = pwd.getpwuid(os.getuid())
        saved = {'uid': str(account.pw_uid), 'name': account.pw_name}
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory) / str(account.pw_uid)
            runtime.mkdir()
            (runtime / 'bus').symlink_to('/dev/null')
            with patch.object(graphics, 'USER_RUNTIME', Path(directory)):
                with self.assertRaisesRegex(RuntimeError, 'owned socket'):
                    graphics.user_command(saved, 'show')
                with self.assertRaisesRegex(RuntimeError, 'identity changed'):
                    graphics.user_command({**saved, 'name': 'wrong-user'}, 'show')


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        p = patch.object(graphics, 'RUNTIME', self.root / 'units')
        p.start()
        self.addCleanup(p.stop)
        graphics.RUNTIME.mkdir()
        self.folder = self.root / 'log'
        self.folder.mkdir()
        self.baseline = {'__CURSOR': 'cursor-before', '_BOOT_ID': 'boot-a', 'MESSAGE': 'baseline'}
        self.events = []
        self.clock = 0
        self.desktop = {unit: {'Id': unit, 'LoadState': 'loaded', 'ActiveState': 'inactive',
                              'SubState': 'dead', 'MainPID': '0', 'Job': '', 'Result': 'success'}
                        for unit in graphics.DESKTOP_UNITS}
        self.desktop_events = []
        p = patch.object(graphics, 'user_command', side_effect=lambda saved, *args: ['user-systemctl', *args])
        p.start()
        self.addCleanup(p.stop)
        graphics.durable_json(self.folder / 'graphics-kernel-baseline.json', self.baseline)
        self.lab = SimpleNamespace(
            assert_sleep_idle=Mock(), PM_TEST=SimpleNamespace(read_text=lambda: '[none] platform'),
            selected=lambda text: text.split('[')[1].split(']')[0],
            SLEEP_CONF=self.root / 'sleep', UNIT_CONF=self.root / 'unit', SLEEP_GUARD_ONCE=self.root / 'once',
            command=Mock(return_value=subprocess.CompletedProcess([], 0, '{}')),
            require_command=Mock(side_effect=self.required), snapshot=Mock(), save_command=Mock(),
            unit_state=Mock(return_value={'ActiveState': 'active'}),
            time=SimpleNamespace(sleep=Mock(side_effect=self.advance), monotonic=lambda: self.clock),
            NO_GRAPHICS_CHECK=Path('/not-executed/check.py'),
            preflight=Mock(return_value={'nvidia_depth_for_test': 'default'}), experiment=Mock(return_value=0))
        self.saved = {'uid': '1000', 'name': 'test-user', 'sessions': ['5'],
                      'units': {u: {'active': True, 'masked': False} for u in graphics.UNITS},
                      'owned_masks': list(graphics.UNITS)}

    def advance(self, seconds):
        self.clock += seconds

    def required(self, argv):
        if argv[0] == 'journalctl':
            if '_COMM=systemd' in argv:
                return '\n'.join(json.dumps(item) for item in self.desktop_events)
            records = [self.baseline] if '-n' in argv else [self.baseline, *self.events]
            return '\n'.join(json.dumps(item) for item in records)
        if argv[0] == 'user-systemctl':
            return '\n\n'.join('\n'.join(key + '=' + value for key, value in info.items())
                               for info in self.desktop.values())
        return ''

    def event(self, message):
        self.events.append({'__CURSOR': 'cursor-' + str(len(self.events)),
                            '_BOOT_ID': 'boot-a', 'MESSAGE': message})

    def masks(self):
        for unit in self.saved['owned_masks']:
            (graphics.RUNTIME / unit).symlink_to('/dev/null')

    def test_restore_only_owned_masks_and_originally_active_services(self):
        self.saved['units']['cardwired.service'] = {'active': False, 'masked': True}
        self.saved['owned_masks'].remove('cardwired.service')
        self.masks()
        graphics.restore_graphics(self.lab, self.folder, self.saved)
        calls = [c.args[0] for c in self.lab.command.call_args_list]
        self.assertNotIn(['systemctl', 'start', 'cardwired.service'], calls)
        self.assertNotIn(['systemctl', 'unmask', '--runtime', 'cardwired.service'], calls)
        self.assertIn(['systemctl', 'start', 'sddm.service'], calls)
        self.assertTrue((self.folder / 'graphics-restored.txt').exists())

    def test_pending_pm_state_prevents_graphics_restart(self):
        self.lab.PM_TEST.read_text = lambda: 'none [platform]'
        with self.assertRaisesRegex(RuntimeError, 'uncertain'):
            graphics.restore_graphics(self.lab, self.folder, self.saved)
        self.lab.command.assert_not_called()

    def test_pending_logind_transition_prevents_restart(self):
        self.lab.assert_sleep_idle.side_effect = RuntimeError('sleep pending')
        with self.assertRaisesRegex(RuntimeError, 'pending'):
            graphics.restore_graphics(self.lab, self.folder, self.saved)
        self.lab.command.assert_not_called()

    def test_driver_error_prevents_automatic_reopening(self):
        (self.folder / 'kernel-test.txt').write_text('NVRM: Xid 79\n')
        with self.assertRaisesRegex(RuntimeError, 'NVIDIA errors'):
            graphics.restore_graphics(self.lab, self.folder, self.saved)
        self.lab.command.assert_not_called()

    def test_new_xid_during_logout_prevents_reopening(self):
        self.event('NVRM: Xid (PCI:0000:03:00): 79, GPU has fallen off the bus')
        with self.assertRaisesRegex(RuntimeError, 'Severe GPU'):
            graphics.restore_graphics(self.lab, self.folder, self.saved)
        self.lab.command.assert_not_called()

    def test_service_start_success_but_inactive_is_not_reported_as_restored(self):
        self.lab.unit_state.return_value = {'ActiveState': 'failed'}
        with self.assertRaisesRegex(RuntimeError, 'did not remain active'):
            graphics.restore_graphics(self.lab, self.folder, self.saved)
        self.assertFalse((self.folder / 'graphics-restored.txt').exists())

    def test_new_flip_timeout_blocks_pm_but_allows_one_graphics_restore(self):
        self.event('[drm:nv_drm_atomic_commit [nvidia_drm]] *ERROR* Flip event timeout on head 0')
        with patch.object(graphics, 'plan', return_value=self.saved), patch.object(graphics, 'stop_graphics'):
            with self.assertRaisesRegex(RuntimeError, 'New GPU/DRM errors'):
                graphics.run(self.lab, self.folder)
        self.lab.experiment.assert_not_called()
        calls = [c.args[0] for c in self.lab.command.call_args_list]
        self.assertEqual(calls.count(['systemctl', 'start', 'sddm.service']), 1)
        outcome = json.loads((self.folder / 'graphics-outcome.json').read_text())
        self.assertEqual(outcome['status'], 'failed')
        self.assertFalse(outcome['sleep_test_called'])

    def test_graphics_only_never_calls_pm_experiment(self):
        with patch.object(graphics, 'plan', return_value=self.saved), \
             patch.object(graphics, 'stop_graphics'), patch.object(graphics, 'restore_graphics') as restore:
            self.assertEqual(graphics.run(self.lab, self.folder, graphics_only=True), 0)
            self.lab.experiment.assert_not_called()
            restore.assert_called_once()
        outcome = json.loads((self.folder / 'graphics-outcome.json').read_text())
        self.assertFalse(outcome['sleep_test_called'])
        self.assertEqual(outcome['experiment'], 'graphics-only')
        self.assertIn('visual confirmation required', outcome['status'])
        for path in (self.lab.PM_TEST,):
            self.assertEqual(path.read_text(), '[none] platform')
        self.assertFalse(self.lab.SLEEP_GUARD_ONCE.exists())
        self.assertFalse(self.lab.UNIT_CONF.exists())
        self.assertFalse(self.lab.SLEEP_CONF.exists())

    def test_graphics_preflight_reports_no_sleep_or_bypass(self):
        info = graphics.preflight_graphics(self.lab)
        self.assertIsNone(info['requested_stage'])
        self.assertEqual(info['sleep_guard_bypass'], 'disabled')
        self.assertNotIn('nvidia_depth_for_test', info)
        self.lab.experiment.assert_not_called()
        self.lab.command.assert_not_called()

    def test_graphics_only_detects_errors_introduced_on_restart(self):
        def restore(*_):
            self.event('WARNING: nvidia-drm/nvidia-drm-crtc.h:368 at __nv_drm_handle_flip_event')
        with patch.object(graphics, 'plan', return_value=self.saved), \
             patch.object(graphics, 'stop_graphics'), patch.object(graphics, 'restore_graphics', side_effect=restore):
            with self.assertRaisesRegex(RuntimeError, 'New GPU/DRM errors'):
                graphics.run(self.lab, self.folder, graphics_only=True)
        self.lab.experiment.assert_not_called()
        self.assertEqual(json.loads((self.folder / 'graphics-outcome.json').read_text())['status'], 'failed')

    def test_baseline_error_does_not_count_as_new_error(self):
        self.baseline['MESSAGE'] = 'NVRM: Xid 79'
        graphics.durable_json(self.folder / 'graphics-kernel-baseline.json', self.baseline)
        self.assertEqual(graphics.capture_kernel(self.lab, self.folder, 'test'), [])

    def test_cursor_gap_and_boot_change_fail_closed(self):
        for output in ('', json.dumps({**self.baseline, '__CURSOR': 'different'}),
                       '\n'.join(json.dumps(item) for item in
                                 (self.baseline, {**self.baseline, '_BOOT_ID': 'boot-b'}))):
            with patch.object(self.lab, 'require_command', return_value=output):
                with self.assertRaisesRegex(RuntimeError, 'baseline disappeared'):
                    graphics.capture_kernel(self.lab, self.folder, 'test')

    def test_journal_failure_aborts_before_sleep(self):
        self.lab.require_command.side_effect = RuntimeError('journal unavailable')
        with patch.object(graphics, 'plan', return_value=self.saved), patch.object(graphics, 'stop_graphics'):
            with self.assertRaisesRegex(RuntimeError, 'journal unavailable'):
                graphics.run(self.lab, self.folder)
        self.lab.experiment.assert_not_called()
        self.lab.command.assert_not_called()

    def test_error_classification_keeps_routine_messages(self):
        self.event('amdgpu 0000:64:00.0: [drm] DMUB HPD RX IRQ callback: link_index=4')
        self.event('NVRM: loading NVIDIA UNIX Open Kernel Module for x86_64 615.71.09')
        self.assertEqual(graphics.capture_kernel(self.lab, self.folder, 'routine'), [])
        self.event('amdgpu 0000:64:00.0: [drm] *ERROR* ring gfx timeout')
        self.event('NVRM: nvAssertFailedNoLog: Assertion failed: NV_ERR_NO_MEMORY')
        findings = graphics.capture_kernel(self.lab, self.folder, 'errors')
        self.assertEqual(len(findings), 2)
        self.assertFalse(findings[0]['fatal'])
        self.assertTrue(findings[1]['fatal'])

    def test_foreign_mask_is_not_removed(self):
        (graphics.RUNTIME / 'sddm.service').write_text('foreign unit')
        with self.assertRaisesRegex(RuntimeError, 'Foreign replacement'):
            graphics.restore_graphics(self.lab, self.folder, self.saved)
        self.assertEqual((graphics.RUNTIME / 'sddm.service').read_text(), 'foreign unit')
        self.assertNotIn(['systemctl', 'unmask', '--runtime', 'sddm.service'],
                         [c.args[0] for c in self.lab.command.call_args_list])
        self.assertNotIn(['systemctl', 'start', 'sddm.service'],
                         [c.args[0] for c in self.lab.command.call_args_list])

    def test_stop_never_terminates_entire_user_or_unloads_modules(self):
        self.saved['owned_masks'] = []
        with patch.object(graphics, 'sessions', return_value=['5']):
            graphics.stop_graphics(self.lab, self.folder, self.saved)
        calls = [c.args[0] for c in self.lab.require_command.call_args_list]
        self.assertIn(['loginctl', 'terminate-session', '5'], calls)
        self.assertFalse(any('terminate-user' in call or 'modprobe' in call or 'rmmod' in call for call in calls))
        self.assertTrue((self.folder / 'graphics-quiescent.json').exists())

    def test_leftover_clients_abort_before_experiment(self):
        self.saved['owned_masks'] = []
        self.lab.command.side_effect = lambda argv, **kw: subprocess.CompletedProcess(
            argv, 1 if str(self.lab.NO_GRAPHICS_CHECK) in argv else 0, 'clients remain')
        with patch.object(graphics, 'plan', return_value=self.saved), \
             patch.object(graphics, 'sessions', return_value=[]), \
             patch.object(graphics, 'restore_graphics') as restore:
            with self.assertRaisesRegex(RuntimeError, 'shutdown is incomplete'):
                graphics.run(self.lab, self.folder)
            self.lab.experiment.assert_not_called()
            restore.assert_called_once()

    def test_nonzero_desktop_request_is_not_ignored_or_retried(self):
        self.lab.command.return_value = subprocess.CompletedProcess([], 1, 'Connection reset by peer')
        with self.assertRaisesRegex(RuntimeError, 'Desktop stop request failed'):
            graphics.stop_graphics(self.lab, self.folder, self.saved)
        self.assertEqual(self.lab.command.call_count, 1)
        self.assertTrue(self.saved['desktop_stop_requested'])
        self.assertIn('[exit=1]', (self.folder / 'stop-desktop-targets.txt').read_text())
        self.assertNotIn(['systemctl', 'stop', 'sddm.service'],
                         [call.args[0] for call in self.lab.require_command.call_args_list])

    def test_stop_request_timeout_is_uncertain_not_retried(self):
        self.lab.command.side_effect = subprocess.TimeoutExpired('user-systemctl', 10)
        with self.assertRaises(subprocess.TimeoutExpired):
            graphics.stop_graphics(self.lab, self.folder, self.saved)
        self.assertTrue(self.saved['desktop_stop_requested'])
        self.assertEqual(self.lab.command.call_count, 1)
        self.assertIn('unconfirmed request', (self.folder / 'stop-desktop-targets.txt').read_text())

    def test_old_clients_block_every_recovery_start_and_unmask(self):
        self.saved.update(desktop_stop_requested=True, stop_deadline_monotonic=1)
        self.masks()
        self.lab.command.return_value = subprocess.CompletedProcess([], 1, 'kwin still holds GPU')
        with self.assertRaisesRegex(RuntimeError, 'shutdown is incomplete'):
            graphics.restore_graphics(self.lab, self.folder, self.saved)
        self.assertTrue(all((graphics.RUNTIME / unit).is_symlink() for unit in graphics.UNITS))
        self.assertTrue(all(str(self.lab.NO_GRAPHICS_CHECK) in call.args[0]
                            for call in self.lab.command.call_args_list))
        self.assertFalse((self.folder / 'graphics-restored.txt').exists())

    def test_queued_jobs_prevent_restart_even_when_gpu_handles_are_gone(self):
        self.saved.update(desktop_stop_requested=True, stop_deadline_monotonic=1)
        self.desktop['plasma-kwin_wayland.service']['Job'] = '123'
        with self.assertRaisesRegex(RuntimeError, 'shutdown is incomplete'):
            graphics.restore_graphics(self.lab, self.folder, self.saved)
        self.assertFalse(any(call.args[0][0] == 'systemctl' for call in self.lab.command.call_args_list))

    def test_deadline_not_renewed_by_recovery(self):
        self.saved.update(desktop_stop_requested=True, stop_deadline_monotonic=1)
        self.lab.command.return_value = subprocess.CompletedProcess([], 1, 'old KWin')
        with self.assertRaises(RuntimeError):
            graphics.wait_quiescent(self.lab, self.folder, self.saved)
        elapsed = self.clock
        with self.assertRaises(RuntimeError):
            graphics.restore_graphics(self.lab, self.folder, self.saved)
        self.assertEqual(self.clock, elapsed)

    def test_actual_completion_ends_wait_early(self):
        self.saved.update(desktop_stop_requested=True, stop_deadline_monotonic=75)
        results = [subprocess.CompletedProcess([], 1, 'old client'), subprocess.CompletedProcess([], 0, '{}')]
        self.lab.command.side_effect = results
        graphics.wait_quiescent(self.lab, self.folder, self.saved)
        self.assertEqual(self.clock, 0.5)
        self.assertTrue((self.folder / 'graphics-quiescent.json').exists())

    def test_active_user_manager_with_broken_bus_is_not_considered_stopped(self):
        self.saved.update(desktop_stop_requested=True, stop_deadline_monotonic=1)
        with patch.object(graphics, 'desktop_state', side_effect=RuntimeError('bus disconnected')):
            with self.assertRaisesRegex(RuntimeError, 'shutdown is incomplete'):
                graphics.wait_quiescent(self.lab, self.folder, self.saved)

    def test_deactivating_manager_cannot_overlap_new_display_stack(self):
        self.saved.update(desktop_stop_requested=True, stop_deadline_monotonic=1)
        self.lab.unit_state.return_value = {'ActiveState': 'deactivating', 'MainPID': '42'}
        with self.assertRaisesRegex(RuntimeError, 'shutdown is incomplete'):
            graphics.wait_quiescent(self.lab, self.folder, self.saved)

    def test_naturally_ended_user_manager_is_allowed_only_with_quiet_gpu(self):
        self.saved.update(desktop_stop_requested=True, stop_deadline_monotonic=1)
        self.lab.unit_state.return_value = {'ActiveState': 'inactive', 'MainPID': '0'}
        with patch.object(graphics, 'desktop_state', side_effect=AssertionError('no bus needed')):
            graphics.wait_quiescent(self.lab, self.folder, self.saved)
            self.lab.command.return_value = subprocess.CompletedProcess([], 1, 'surviving GPU client')
            with self.assertRaisesRegex(RuntimeError, 'shutdown is incomplete'):
                graphics.wait_quiescent(self.lab, self.folder, self.saved)

    def test_timeout_result_is_retained_and_blocks_pm_after_quiescence(self):
        self.desktop['plasma-kwin_wayland.service']['Result'] = 'timeout'
        with patch.object(graphics, 'sessions', return_value=[]):
            with self.assertRaisesRegex(RuntimeError, 'Desktop units failed'):
                graphics.stop_graphics(self.lab, self.folder, self.saved)
        self.assertEqual(self.saved['desktop_stop_errors']['plasma-kwin_wayland.service'], 'timeout')

    def test_journal_retains_abort_after_user_bus_disappears(self):
        self.saved.update(desktop_stop_requested=True, stop_deadline_monotonic=1)
        self.lab.unit_state.return_value = {'ActiveState': 'inactive', 'MainPID': '0'}
        self.desktop_events = [{**self.baseline, '__CURSOR': 'after',
            'USER_UNIT': 'plasma-plasmashell.service',
            'MESSAGE': "plasma-plasmashell.service: State 'stop-sigterm' timed out. Aborting."}]
        graphics.wait_quiescent(self.lab, self.folder, self.saved)
        self.assertIn('timed out', self.saved['desktop_stop_errors']['plasma-plasmashell.service'])

    def test_desktop_parser_rejects_missing_units(self):
        del self.desktop['plasma-kwin_wayland.service']
        with self.assertRaisesRegex(RuntimeError, 'exact Plasma'):
            graphics.desktop_state(self.lab, self.saved)

    def test_success_restores_and_requests_exactly_one_test(self):
        with patch.object(graphics, 'plan', return_value=self.saved), \
             patch.object(graphics, 'stop_graphics'), patch.object(graphics, 'restore_graphics') as restore:
            self.assertEqual(graphics.run(self.lab, self.folder), 0)
            self.lab.experiment.assert_called_once_with(self.folder, 'platform', 'no-graphics')
            restore.assert_called_once()

    def test_returned_but_unconfirmed_pm_cycle_stays_failed_after_graphics_restore(self):
        self.lab.experiment.return_value = 1
        with patch.object(graphics, 'plan', return_value=self.saved), \
             patch.object(graphics, 'stop_graphics'), patch.object(graphics, 'restore_graphics'):
            self.assertEqual(graphics.run(self.lab, self.folder), 1)
        outcome = json.loads((self.folder / 'graphics-outcome.json').read_text())
        self.assertEqual(outcome['status'], 'failed')
        self.assertEqual(outcome['pm_test_returncode'], 1)

    def test_uncertain_cycle_does_not_restore_or_repeat(self):
        self.lab.experiment.side_effect = RuntimeError('timeout')
        self.lab.PM_TEST.read_text = lambda: 'none [platform]'
        with patch.object(graphics, 'plan', return_value=self.saved), patch.object(graphics, 'stop_graphics'):
            with self.assertRaisesRegex(RuntimeError, 'uncertain'):
                graphics.run(self.lab, self.folder)
        self.lab.command.assert_not_called()
        self.assertEqual(self.lab.experiment.call_count, 1)
        self.assertTrue((self.folder / 'graphics-recovery-required.txt').exists())

    def test_sessions_reject_other_users_and_remote_gui(self):
        for detail in ('User=1001\nType=wayland\nClass=user\nRemote=no',
                       'User=1000\nType=wayland\nClass=user\nRemote=yes'):
            self.lab.require_command.side_effect = ['5 1000 user seat0', detail]
            with self.assertRaisesRegex(RuntimeError, 'Another or remote'):
                graphics.sessions(self.lab, '1000')

    def test_sessions_leave_tty_and_manager_alone(self):
        self.lab.require_command.side_effect = ['5 1000 user\n6 1000 user\n7 1000 user',
            'User=1000\nType=tty\nClass=user\nRemote=yes',
            'User=1000\nType=unspecified\nClass=manager\nRemote=no',
            'User=1000\nType=wayland\nClass=user\nRemote=no']
        self.assertEqual(graphics.sessions(self.lab, '1000'), ['7'])


if __name__ == '__main__':
    unittest.main()
