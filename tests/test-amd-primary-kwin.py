"""Synthetic tests only; no unit writes or live restarts."""
import importlib.util
import json
from contextlib import contextmanager
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('trial', Path(__file__).resolve().parents[1] / 'diagnostics/test-amd-primary-kwin.py')
trial = importlib.util.module_from_spec(spec)
spec.loader.exec_module(trial)


class KwinTrial(unittest.TestCase):
    config = {'DESKTOP_USER': 'tester', 'DESKTOP_UID': '1001'}

    def unit(self, **values):
        return {'LoadState': 'loaded', 'ActiveState': 'inactive', 'SubState': 'dead',
                'Job': '', 'MainPID': '0', 'ControlPID': '0', 'InvocationID': 'old',
                'ControlGroup': '/user.slice/test-kwin.service', **values}

    def snapshot(self, **values):
        states = {u: self.unit() for u in trial.SESSION_UNITS}
        for unit, fields in values.items():
            states[unit].update(fields)
        return states

    @contextmanager
    def shutdown_host(self, snapshots, *, submit_error=False, populated=False, jobs=False):
        """Only fake responses; no commands, sleeps, cgroups or user bus access."""
        cursor = [0]
        clock = [0.0]
        def show(unit, user=None, **kwargs):
            if unit == 'sddm.service':
                return self.unit()
            current = snapshots(clock[0]) if callable(snapshots) else snapshots[min(cursor[0], len(snapshots) - 1)]
            result = current[unit]
            if unit == trial.UNIT:
                cursor[0] += 1
            return result
        def tick(seconds):
            clock[0] += seconds
        with patch.object(trial, 'run', side_effect=RuntimeError('Job canceled') if submit_error else None) as run, \
                patch.object(trial, 'show', side_effect=show), \
                patch.object(trial, 'cgroup_empty', return_value=not populated), \
                patch.object(trial, 'user_jobs_pending', return_value=jobs), \
                patch.object(trial.time, 'monotonic', side_effect=lambda: clock[0]), \
                patch.object(trial.time, 'sleep', side_effect=tick), patch('builtins.print') as log:
            yield run, log

    def hardware(self):
        return {'errors': [], 'gpus': {
            'igpu': {'card': 'card5', 'driver': 'amdgpu'},
            'nvidia': {'card': 'card8', 'driver': 'nvidia'}},
            'connectors': [{'gpu': 'nvidia', 'status': 'connected', 'enabled': 'enabled'}]}

    def test_both_gpus_remain_and_amd_is_first(self):
        text = trial.override_text(self.hardware())
        self.assertIn('KWIN_DRM_DEVICES=/dev/dri/card5:/dev/dri/card8', text)
        self.assertNotIn('ExecStart', text)
        self.assertNotIn('CARDWIRE_', text)

    def test_absent_or_unbound_nvidia_is_not_initialized(self):
        for value in (None, {'card': 'card8', 'driver': None}):
            hw = self.hardware()
            hw['gpus']['nvidia'] = value
            with self.assertRaises(RuntimeError):
                trial.override_text(hw)

    def test_no_unit_file_injection_from_card_name(self):
        hw = self.hardware()
        hw['gpus']['igpu']['card'] = 'card5\nExecStart=bad'
        with self.assertRaises(RuntimeError):
            trial.override_text(hw)

    def test_user_bus_is_explicit(self):
        args = trial.user_args({'DESKTOP_USER': 'tester', 'DESKTOP_UID': '1001'})
        self.assertEqual(args[:4], ['runuser', '-u', 'tester', '--'])
        self.assertIn('DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1001/bus', args)

    def test_graphical_restart_never_terminates_user_or_inference(self):
        with self.shutdown_host([self.snapshot()]) as (run, log):
            trial.restart_graphics(self.config)
        commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual(commands[0], ['systemctl', '--no-block', 'stop', 'sddm.service'])
        self.assertEqual(commands[1][-5:], ['--user', '--no-block', 'stop', 'graphical-session.target', 'plasma-workspace.target'])
        self.assertEqual(commands[2], ['systemctl', 'start', 'sddm.service'])
        self.assertNotIn('restart', str(commands))
        self.assertNotIn('llama.service', str(commands))
        self.assertNotIn('terminate-user', str(commands))
        self.assertNotIn('user@', str(commands))

    def test_canceled_stop_is_accepted_only_after_targets_and_kwin_exit(self):
        on = self.snapshot(**{trial.UNIT: {'ActiveState': 'deactivating', 'MainPID': '123'}})
        with self.shutdown_host([on, self.snapshot()], submit_error=True) as (run, log):
            trial.stop_graphical_session(self.config, self.unit())
        self.assertEqual(run.call_count, 1)
        self.assertIn('Job canceled', str(log.call_args_list))

    def test_failed_shutdown_with_live_compositor_does_not_restart_sddm(self):
        on = self.snapshot(**{trial.UNIT: {'ActiveState': 'active', 'MainPID': '123'}})
        with self.shutdown_host([on], submit_error=True) as (run, log):
            with self.assertRaisesRegex(RuntimeError, 'did not converge'):
                trial.stop_graphical_session(self.config, self.unit())
        self.assertEqual(run.call_count, 1)
        self.assertIn('Job canceled', str(log.call_args_list))

    def test_successful_stop_return_does_not_override_live_state(self):
        on = self.snapshot(**{trial.UNIT: {'MainPID': '123'}})
        with self.shutdown_host([on]) as (run, log):
            with self.assertRaisesRegex(RuntimeError, 'did not converge'):
                trial.stop_graphical_session(self.config, self.unit())
        self.assertEqual(run.call_count, 1)

    def test_unknown_shutdown_state_is_not_treated_as_success(self):
        with patch.object(trial, 'run', return_value='') as run, \
                patch.object(trial, 'show', side_effect=RuntimeError('User bus unavailable')):
            with self.assertRaisesRegex(RuntimeError, 'User bus unavailable'):
                trial.stop_graphical_session(self.config, self.unit())
        self.assertEqual(run.call_count, 1)

    def test_observed_residual_target_gets_exactly_one_reconciliation(self):
        residual = self.snapshot(**{'graphical-session.target': {'ActiveState': 'active'}})
        on = self.snapshot(**{'graphical-session.target': {'ActiveState': 'active'},
                             trial.UNIT: {'ActiveState': 'deactivating', 'MainPID': '123'}})
        with self.shutdown_host([on, residual, self.snapshot()]) as (run, log):
            trial.stop_graphical_session(self.config, self.unit())
        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args_list[1].args[0][-4:],
                         ['--user', '--no-block', 'stop', 'graphical-session.target'])

    def test_observed_sequence_opens_greeter_once_only_after_reconciliation(self):
        residual = self.snapshot(**{'graphical-session.target': {'ActiveState': 'active'}})
        on = self.snapshot(**{trial.UNIT: {'ActiveState': 'active', 'MainPID': '123'}})
        # First snapshot supplies the pre-SDDM-stop compositor identity.
        with self.shutdown_host([on, on, residual, self.snapshot()]) as (run, log):
            trial.restart_graphics(self.config)
        commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual(len(commands), 4)
        self.assertEqual(commands[-2][-2:], ['stop', 'graphical-session.target'])
        self.assertEqual(commands[-1], ['systemctl', 'start', 'sddm.service'])
        self.assertEqual(sum('sddm.service' in c and 'start' in c for c in commands), 1)

    def test_reconciliation_cannot_loop_forever(self):
        residual = self.snapshot(**{'graphical-session.target': {'ActiveState': 'active'}})
        for error in (False, True):
            with self.subTest(error=error), self.shutdown_host([residual], submit_error=error) as (run, log):
                with self.assertRaisesRegex(RuntimeError, 'did not converge'):
                    trial.stop_graphical_session(self.config, self.unit())
                self.assertEqual(run.call_count, 2)

    def test_pending_target_job_is_observed_not_replaced(self):
        pending = self.snapshot(**{'graphical-session.target': {'ActiveState': 'active', 'Job': '37'}})
        with self.shutdown_host([pending, self.snapshot()]) as (run, log):
            trial.stop_graphical_session(self.config, self.unit())
        self.assertEqual(run.call_count, 1)

    def test_workspace_must_be_gone_before_reconciliation(self):
        for target in trial.SESSION_UNITS[1:-1]:
            residual = self.snapshot(**{'graphical-session.target': {'ActiveState': 'active'},
                                        target: {'ActiveState': 'active'}})
            with self.subTest(target=target), self.shutdown_host([residual]) as (run, log):
                with self.assertRaisesRegex(RuntimeError, 'did not converge'):
                    trial.stop_graphical_session(self.config, self.unit())
                self.assertEqual(run.call_count, 1)

    def test_zero_wrapper_pid_with_remaining_cgroup_cannot_pass(self):
        with self.shutdown_host([self.snapshot()], populated=True) as (run, log):
            with self.assertRaisesRegex(RuntimeError, 'did not converge'):
                trial.stop_graphical_session(self.config, self.unit())
        self.assertEqual(run.call_count, 1)

    def test_inactive_with_job_control_pid_or_unknown_property_cannot_pass(self):
        for field, value in [('Job', '55'), ('ControlPID', '456'), ('ControlPID', None),
                             ('Job', None), ('LoadState', None), ('ActiveState', None)]:
            states = self.snapshot(**{trial.UNIT: {field: value}})
            with self.subTest(field=field, value=value), self.shutdown_host([states]):
                with self.assertRaisesRegex(RuntimeError, 'did not converge'):
                    trial.stop_graphical_session(self.config, self.unit())

    def test_different_compositor_invocation_prevents_retry(self):
        states = self.snapshot(**{trial.UNIT: {'InvocationID': 'new'}})
        with self.shutdown_host([states]) as (run, log):
            with self.assertRaisesRegex(RuntimeError, 'invocation changed'):
                trial.stop_graphical_session(self.config, self.unit())
        self.assertEqual(run.call_count, 1)

    def test_sddm_reactivated_during_shutdown_prevents_retry(self):
        with self.shutdown_host([self.snapshot()]) as (run, log), \
                patch.object(trial, 'require_login_gated', side_effect=RuntimeError('SDDM is not stopped')):
            with self.assertRaisesRegex(RuntimeError, 'SDDM is not stopped'):
                trial.stop_graphical_session(self.config, self.unit())
        self.assertEqual(run.call_count, 1)

    def test_queued_propagated_jobs_prevent_login(self):
        with self.shutdown_host([self.snapshot()], jobs=True):
            with self.assertRaisesRegex(RuntimeError, 'did not converge'):
                trial.stop_graphical_session(self.config, self.unit())

    def test_already_stopped_compositor_without_cgroup_is_valid_for_rollback(self):
        stopped = self.unit(ControlGroup='', InvocationID='')
        with self.shutdown_host([self.snapshot(**{trial.UNIT: stopped})]):
            trial.stop_graphical_session(self.config, stopped)

    def test_cgroup_population_is_verified_recursively_or_gone(self):
        group = '/user.slice/test-kwin.service'
        for text, expected in [('populated 0\nfrozen 0\n', True), ('populated 1\n', False)]:
            with patch.object(Path, 'read_text', return_value=text):
                self.assertEqual(trial.cgroup_empty(group), expected)
        with patch.object(Path, 'read_text', side_effect=FileNotFoundError):
            self.assertTrue(trial.cgroup_empty(group))
        for failure in (PermissionError, ValueError):
            with patch.object(Path, 'read_text', side_effect=failure):
                with self.assertRaisesRegex(RuntimeError, 'Cannot verify'):
                    trial.cgroup_empty(group)
        with patch.object(Path, 'read_text', return_value='frozen 0\n'):
            with self.assertRaisesRegex(RuntimeError, 'Unknown'):
                trial.cgroup_empty(group)
        for invalid in ('', '/', 'relative', '/user/../etc'):
            with self.subTest(group=invalid), self.assertRaisesRegex(RuntimeError, 'Unknown'):
                trial.cgroup_empty(invalid)

    def test_job_gate_requires_authoritative_empty_response_and_never_cancels(self):
        with patch.object(trial, 'run', return_value='{"type":"a(usssoo)","data":[[]]}') as run:
            trial.require_no_user_jobs(self.config)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.args[0][-1], 'ListJobs')
        for response in ('{}', '{"type":"a(usssoo)","data":[[[42,"portal","stop"]]]}'):
            with patch.object(trial, 'run', return_value=response), self.assertRaises(RuntimeError):
                trial.require_no_user_jobs(self.config)

    def test_no_session_teardown_while_sddm_can_accept_a_login(self):
        with patch.object(trial, 'run') as run, \
                patch.object(trial, 'show', return_value={'ActiveState': 'active', 'MainPID': '123'}), \
                patch.object(trial, 'stop_graphical_session') as stop:
            with self.assertRaisesRegex(RuntimeError, 'SDDM is not stopped'):
                trial.restart_graphics({'DESKTOP_USER': 'tester', 'DESKTOP_UID': '1001'})
        run.assert_called_once_with(['systemctl', '--no-block', 'stop', 'sddm.service'], timeout=5)
        stop.assert_not_called()

    def test_trial_failure_keeps_login_gated_until_outer_rollback(self):
        with patch.object(trial, 'run') as run, \
                patch.object(trial, 'show', return_value=self.unit()), \
                patch.object(trial, 'stop_graphical_session', side_effect=RuntimeError('Teardown failed')):
            with self.assertRaisesRegex(RuntimeError, 'Teardown failed'):
                trial.restart_graphics({'DESKTOP_USER': 'tester', 'DESKTOP_UID': '1001'})
        run.assert_called_once_with(['systemctl', '--no-block', 'stop', 'sddm.service'], timeout=5)

    def test_rollback_removes_override_but_keeps_login_gated_on_teardown_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'started').touch()
            saved = {'config': {'DESKTOP_USER': 'tester', 'DESKTOP_UID': '1001'}, 'override': 'owned'}
            actions = []
            def fail_restart(config, *, recovery):
                self.assertEqual(actions[0], ('remove', 'owned'))
                self.assertTrue(recovery)
                raise RuntimeError('Teardown failed')
            with patch.object(trial, 'ROOT', root), patch.object(trial, 'state', return_value=saved), \
                    patch.object(trial, 'user_file', side_effect=lambda config, action, text: actions.append((action, text))), \
                    patch.object(trial, 'run', side_effect=lambda args: actions.append(args)), \
                    patch.object(trial, 'require_no_user_jobs', side_effect=lambda config: actions.append('jobs-clear')), \
                    patch.object(trial, 'restart_graphics', side_effect=fail_restart):
                with self.assertRaisesRegex(RuntimeError, 'Teardown failed'):
                    trial.restore(restart=True)
            self.assertFalse(any(isinstance(action, list) and 'sddm.service' in action for action in actions))
            self.assertFalse((root / 'restored').exists())
            failure = json.loads((root / 'recovery-required.json').read_text())
            self.assertTrue(failure['configuration_restored'])
            self.assertFalse(failure['visible_output_verified'])
            self.assertEqual(failure['error'], 'Teardown failed')

    def test_successful_rollback_does_not_recheck_jobs_after_opening_login(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'started').touch()
            saved = {'config': self.config, 'override': 'owned'}
            with patch.object(trial, 'ROOT', root), patch.object(trial, 'state', return_value=saved), \
                    patch.object(trial, 'user_file') as user_file, patch.object(trial, 'run') as run, \
                    patch.object(trial, 'restart_graphics', return_value=self.unit()) as restart, \
                    patch.object(trial, 'verify_inference'), \
                    patch.object(trial, 'require_no_user_jobs', side_effect=RuntimeError('new-login jobs')) as gate:
                trial.restore(restart=True)
            self.assertTrue((root / 'restored').exists())
            user_file.assert_called_once_with(self.config, 'remove', 'owned')
            restart.assert_called_once_with(self.config, recovery=True)
            gate.assert_not_called()
            self.assertFalse(any('sddm.service' in call.args[0] for call in run.call_args_list))
            result = json.loads((root / 'restore-result.json').read_text())
            self.assertTrue(result['login_manager_start_returned'])
            self.assertFalse(result['visible_output_verified'])
            self.assertTrue(result['needs_visual_confirmation'])
            self.assertFalse((root / 'restore-confirmed').exists())

    def test_rollback_does_not_open_greeter_over_queued_stop_jobs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'started').touch()
            saved = {'config': self.config, 'override': 'owned'}
            with patch.object(trial, 'ROOT', root), patch.object(trial, 'state', return_value=saved), \
                    patch.object(trial, 'user_file'), patch.object(trial, 'run') as run, \
                    patch.object(trial, 'restart_graphics', side_effect=RuntimeError('pending jobs')), \
                    patch.object(trial, 'require_no_user_jobs', side_effect=RuntimeError('pending jobs')):
                with self.assertRaisesRegex(RuntimeError, 'pending jobs'):
                    trial.restore(restart=True)
            self.assertFalse(any('sddm.service' in call.args[0] for call in run.call_args_list))
            self.assertFalse((root / 'restored').exists())

    def test_inference_identity_must_survive(self):
        base = {'MainPID': '100', 'InvocationID': 'one', 'ActiveState': 'active'}
        saved = {'config': {}, 'llama': base}
        with patch.object(trial, 'show', return_value=base):
            trial.verify_inference(saved)
        for key, value in (('MainPID', '101'), ('InvocationID', 'two'), ('ActiveState', 'inactive')):
            with patch.object(trial, 'show', return_value={**base, key: value}), self.assertRaises(RuntimeError):
                trial.verify_inference(saved)

    def test_config_order_does_not_prove_hardware_rendering(self):
        for text in ('KWIN_DRM_DEVICES=card5:card8', 'OpenGL renderer string: NVIDIA RTX',
                     'OpenGL renderer string: llvmpipe (AMD emulated)', ''):
            with self.assertRaises(RuntimeError):
                trial.require_amd_renderer(text)
        self.assertEqual(trial.require_amd_renderer('OpenGL renderer string: AMD Radeon 890M'), 'AMD Radeon 890M')

    def test_confirmation_requires_enabled_nv_scanout(self):
        hw = self.hardware()
        hw['connectors'][0]['enabled'] = 'disabled'
        saved = {'config': {'DESKTOP_USER': 'tester', 'DESKTOP_UID': '1001'}, 'gpus': hw['gpus']}
        with patch.object(trial, 'run', return_value=json.dumps({'data': ['OpenGL renderer string: AMD Radeon']})), \
                patch.object(trial, 'planner') as planner:
            planner.return_value.inspect_hardware.return_value = hw
            with self.assertRaisesRegex(RuntimeError, 'enabled NVIDIA'):
                trial.verify_renderer(saved)

    def test_no_restart_without_explicit_flag(self):
        with patch.object(trial.sys, 'argv', ['trial', '--start']), patch.object(trial, 'start') as start:
            with self.assertRaises(SystemExit) as error:
                trial.main()
            self.assertEqual(error.exception.code, 2)
            start.assert_not_called()

    def test_user_file_helper_compiles_and_never_reverts_other_overrides(self):
        compile(trial.USER_FILE, '<user-file>', 'exec')
        self.assertIn("path.open('x')", trial.USER_FILE)
        self.assertIn('path.read_text() == content', trial.USER_FILE)
        self.assertNotIn('revert', trial.USER_FILE)
        self.assertNotIn('rmtree', trial.USER_FILE)

    def test_effective_unit_environment_is_verified_before_logout(self):
        saved = {'config': {'DESKTOP_USER': 'tester', 'DESKTOP_UID': '1001'}, 'gpus': self.hardware()['gpus']}
        with patch.object(trial, 'run', side_effect=['KWIN_DRM_DEVICES=/dev/dri/card5:/dev/dri/card8', '']):
            trial.verify_effective_override(saved)
        with patch.object(trial, 'run', return_value='KWIN_DRM_DEVICES=/dev/dri/card8:/dev/dri/card5'):
            with self.assertRaisesRegex(RuntimeError, 'masks'):
                trial.verify_effective_override(saved)
        with patch.object(trial, 'run', side_effect=['KWIN_DRM_DEVICES=/dev/dri/card5:/dev/dri/card8', 'KWIN_DRM_DEVICES']):
            with self.assertRaisesRegex(RuntimeError, 'cancels'):
                trial.verify_effective_override(saved)

    def test_internal_execute_cannot_bypass_start_acknowledgement_from_source(self):
        with patch.object(trial.sys, 'argv', ['trial', '--execute']), patch.object(trial, 'execute') as execute:
            with self.assertRaises(SystemExit) as error:
                trial.main()
            self.assertEqual(error.exception.code, 2)
            execute.assert_not_called()

    def test_archive_requires_authoritatively_inactive_units(self):
        with patch.object(trial, 'run', side_effect=[
                'LoadState=loaded\nActiveState=failed\nMainPID=0\nJob=',
                'LoadState=not-found\nActiveState=inactive\nMainPID=0\nJob=',
                'LoadState=not-found\nActiveState=inactive\nJob=']):
            self.assertEqual(trial.inactive_trial_units(), ['egpu-amd-primary-kwin-trial.service'])
        for bad in ('LoadState=loaded\nActiveState=active\nMainPID=123\nJob=',
                    'LoadState=loaded\nActiveState=inactive\nMainPID=0\nJob=45',
                    'LoadState=loaded\nActiveState=failed\nMainPID=123\nJob=',
                    'LoadState=error\nActiveState=inactive\nMainPID=0\nJob=',
                    'LoadState=loaded\nActiveState=inactive\nMainPID=0', ''):
            with self.subTest(bad=bad), patch.object(trial, 'run', return_value=bad):
                with self.assertRaisesRegex(RuntimeError, 'refusing to archive'):
                    trial.inactive_trial_units()

    def test_archive_preserves_artifacts_without_restarting_anything(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'test-trial'
            root.mkdir()
            (root / 'restored').touch()
            (root / 'result.json').write_text('{"historical": true}')
            saved = {'config': {}, 'override': 'owned'}
            with patch.object(trial, 'ROOT', root), patch.object(trial, 'state', return_value=saved), \
                    patch.object(trial, 'inactive_trial_units', return_value=['egpu-amd-primary-kwin-trial.service']), \
                    patch.object(trial, 'user_file') as user_file, \
                    patch.object(trial, 'verify_inference') as inference, patch.object(trial, 'run') as run:
                archive = trial.archive_restored()
            self.assertFalse(root.exists())
            self.assertEqual((archive / 'result.json').read_text(), '{"historical": true}')
            user_file.assert_called_once_with(saved['config'], 'check', 'owned')
            inference.assert_called_once_with(saved)
            run.assert_called_once_with(['systemctl', 'reset-failed', 'egpu-amd-primary-kwin-trial.service'])

    def test_archive_refuses_unrestored_attempt(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(trial, 'ROOT', Path(directory)), \
                patch.object(trial, 'state', return_value={}), patch.object(trial, 'inactive_trial_units') as units:
            with self.assertRaisesRegex(RuntimeError, 'Only a restored trial'):
                trial.archive_restored()
            units.assert_not_called()

    def test_archive_leaves_artifacts_when_override_or_inference_checks_fail(self):
        for failed_check in ('user_file', 'verify_inference'):
            with self.subTest(failed_check=failed_check), tempfile.TemporaryDirectory() as directory:
                root = Path(directory) / 'trial'
                root.mkdir()
                (root / 'restored').touch()
                with patch.object(trial, 'ROOT', root), \
                        patch.object(trial, 'state', return_value={'config': {}, 'override': 'owned'}), \
                        patch.object(trial, 'inactive_trial_units', return_value=[]), \
                        patch.object(trial, 'user_file') as user_file, \
                        patch.object(trial, 'verify_inference') as inference, patch.object(trial, 'run') as run:
                    {'user_file': user_file, 'verify_inference': inference}[failed_check].side_effect = RuntimeError('Changed')
                    with self.assertRaisesRegex(RuntimeError, 'Changed'):
                        trial.archive_restored()
                self.assertTrue((root / 'restored').exists())
                self.assertEqual(list(Path(directory).iterdir()), [root])
                run.assert_not_called()

    def test_exhausted_budget_never_starts_a_tiny_query(self):
        for elapsed in (29.5, 30.01, 60):
            clock = [0.0]
            def slow_sddm_read(*args, **kwargs):
                clock[0] = elapsed
                return self.unit()
            with self.subTest(elapsed=elapsed), patch.object(trial, 'run') as run, \
                    patch.object(trial, 'show', side_effect=slow_sddm_read) as show, \
                    patch.object(trial.time, 'monotonic', side_effect=lambda: clock[0]):
                with self.assertRaisesRegex(trial.ShutdownTimeout, 'did not converge'):
                    trial.stop_graphical_session(self.config, self.unit())
            show.assert_called_once_with('sddm.service', timeout=5)
            self.assertTrue(all(call.kwargs['timeout'] >= 1 for call in run.call_args_list))

    def test_query_timeout_at_deadline_becomes_explicit_shutdown_failure(self):
        clock = [0.0]
        def slow_query(*args, **kwargs):
            clock[0] = 31
            raise trial.subprocess.TimeoutExpired('systemctl', kwargs['timeout'])
        with patch.object(trial, 'run') as run, patch.object(trial, 'show', side_effect=slow_query) as show, \
                patch.object(trial.time, 'monotonic', side_effect=lambda: clock[0]), patch('builtins.print'):
            with self.assertRaisesRegex(trial.ShutdownTimeout, 'login remains gated'):
                trial.stop_graphical_session(self.config, self.unit())
        self.assertEqual(show.call_count, 1)
        self.assertEqual(run.call_count, 1)

    def test_one_slow_read_is_retried_without_a_second_stop(self):
        with self.shutdown_host([self.snapshot()]) as (run, log):
            original = trial.show.side_effect
            count = [0]
            def delayed_read(*args, **kwargs):
                count[0] += 1
                if count[0] == 1:
                    raise trial.subprocess.TimeoutExpired('systemctl', kwargs['timeout'])
                return original(*args, **kwargs)
            trial.show.side_effect = delayed_read
            trial.stop_graphical_session(self.config, self.unit())
        self.assertEqual(run.call_count, 1)

    def test_slow_teardown_fails_forward_but_converges_in_recovery_window(self):
        pending = self.snapshot(**{trial.UNIT: {'ActiveState': 'deactivating', 'MainPID': '123', 'Job': '17'}})
        sequence = lambda seconds: pending if seconds < 45 else self.snapshot()
        with self.shutdown_host(sequence) as (run, log):
            with self.assertRaises(trial.ShutdownTimeout):
                trial.stop_graphical_session(self.config, self.unit())
            self.assertEqual(run.call_count, 1)
        with self.shutdown_host(sequence) as (run, log):
            trial.restart_graphics(self.config, recovery=True)
            commands = [call.args[0] for call in run.call_args_list]
            self.assertEqual(commands[-1], ['systemctl', 'start', 'sddm.service'])
            self.assertEqual(len(commands), 3)
            self.assertGreaterEqual(trial.time.monotonic(), 45)

    def test_recovery_timeout_still_never_opens_login_over_live_kwin(self):
        pending = self.snapshot(**{trial.UNIT: {'ActiveState': 'deactivating', 'MainPID': '123'}})
        with self.shutdown_host([pending]) as (run, log):
            with self.assertRaises(trial.ShutdownTimeout):
                trial.restart_graphics(self.config, recovery=True)
            commands = [call.args[0] for call in run.call_args_list]
        self.assertFalse(any('start' in command for command in commands))
        self.assertFalse(any('kill' in command or 'cancel' in command for command in commands))

    def test_sddm_stop_has_its_own_bounded_recovery_wait(self):
        clock = [0.0]
        def tick(seconds):
            clock[0] += seconds
        def sddm(unit, **kwargs):
            self.assertEqual(unit, 'sddm.service')
            return self.unit(ActiveState='deactivating', MainPID='55', Job='12') if clock[0] < 45 else self.unit()
        with patch.object(trial, 'run') as run, patch.object(trial, 'show', side_effect=sddm), \
                patch.object(trial.time, 'monotonic', side_effect=lambda: clock[0]), \
                patch.object(trial.time, 'sleep', side_effect=tick):
            trial.stop_login_manager(trial.RECOVERY_SECONDS)
        run.assert_called_once_with(['systemctl', '--no-block', 'stop', 'sddm.service'], timeout=5)
        self.assertGreaterEqual(clock[0], 45)

    def test_failed_recovery_is_not_automatically_repeated_by_watchdog(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'recovery-required.json').write_text('{"error":"stuck teardown"}')
            with patch.object(trial, 'ROOT', root), patch.object(trial, 'state', return_value={}), \
                    patch.object(trial, 'user_file') as user_file, \
                    patch.object(trial, 'restart_graphics') as restart:
                trial.restore(restart=True, watchdog=True)
            user_file.assert_not_called()
            restart.assert_not_called()

    def test_late_diagnostic_failure_cannot_trigger_another_logout(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'started').touch()
            saved = {'config': self.config, 'override': 'owned'}
            with patch.object(trial, 'ROOT', root), patch.object(trial, 'state', return_value=saved), \
                    patch.object(trial, 'user_file'), patch.object(trial, 'run'), \
                    patch.object(trial, 'restart_graphics', return_value=self.unit()) as restart, \
                    patch.object(trial, 'verify_inference', side_effect=RuntimeError('llama changed')):
                with self.assertRaisesRegex(RuntimeError, 'llama changed'):
                    trial.restore(restart=True)
                self.assertTrue((root / 'restored').exists())
                trial.restore(restart=True, watchdog=True)
                trial.restore(restart=True)
                self.assertEqual(restart.call_count, 1)
            self.assertTrue((root / 'recovery-required.json').exists())
            self.assertFalse((root / 'restore-confirmed').exists())

    def test_configurable_trial_timeout_has_a_safe_default_and_bounds(self):
        self.assertEqual(trial.TRIAL_SECONDS, 1500)
        for value in (300, 900, 1500, '1800', 3600):
            self.assertEqual(trial.trial_seconds(value), int(value))
        for value in (0, 299, 3601, 'not-a-number'):
            with self.assertRaises(ValueError):
                trial.trial_seconds(value)

    def test_controller_uses_saved_timeout_not_hardcoded_five_minutes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            hardware = self.hardware()
            saved = {'config': self.config, 'gpus': hardware['gpus'], 'override': 'owned', 'timeout_seconds': 1800}
            with patch.object(trial, 'ROOT', root), patch.object(trial, 'state', return_value=saved), \
                    patch.object(trial, 'run') as run, patch.object(trial, 'planner') as planner, \
                    patch.object(trial, 'user_file'), patch.object(trial, 'verify_inference'), \
                    patch.object(trial, 'verify_effective_override'), patch.object(trial, 'restart_graphics'):
                planner.return_value.inspect_hardware.return_value = hardware
                trial.execute()
            command = run.call_args_list[0].args[0]
            self.assertIn('--on-active=1800s', command)
            self.assertNotIn('--on-active=300s', command)

    def test_failed_forward_transition_enters_controlled_restore(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            hardware = self.hardware()
            saved = {'config': self.config, 'gpus': hardware['gpus'], 'override': 'owned', 'timeout_seconds': 900}
            with patch.object(trial, 'ROOT', root), patch.object(trial, 'state', return_value=saved), \
                    patch.object(trial, 'run'), patch.object(trial, 'planner') as planner, \
                    patch.object(trial, 'user_file'), patch.object(trial, 'verify_inference'), \
                    patch.object(trial, 'verify_effective_override'), \
                    patch.object(trial, 'restart_graphics', side_effect=trial.ShutdownTimeout('slow old KWin')), \
                    patch.object(trial, 'restore') as restore:
                planner.return_value.inspect_hardware.return_value = hardware
                with self.assertRaises(trial.ShutdownTimeout):
                    trial.execute()
            restore.assert_called_once_with(restart=True)

    def test_slow_exit_end_to_end_restores_baseline_and_opens_login_only_once(self):
        # Exercise the real execute -> restart -> deadline -> restore ->
        # recovery path against fake units/clock, not a mocked restore().
        pending = self.snapshot(**{trial.UNIT: {'ActiveState': 'deactivating', 'MainPID': '123', 'Job': '17'}})
        sequence = lambda seconds: pending if seconds < 45 else self.snapshot()
        hardware = self.hardware()
        saved = {'config': self.config, 'gpus': hardware['gpus'], 'override': 'owned', 'timeout_seconds': 900}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.shutdown_host(sequence) as (run, log), patch.object(trial, 'ROOT', root), \
                    patch.object(trial, 'state', return_value=saved), patch.object(trial, 'planner') as planner, \
                    patch.object(trial, 'user_file') as file, patch.object(trial, 'verify_inference'), \
                    patch.object(trial, 'verify_effective_override'):
                planner.return_value.inspect_hardware.return_value = hardware
                with self.assertRaises(trial.ShutdownTimeout):
                    trial.execute()
                commands = [call.args[0] for call in run.call_args_list]
                self.assertEqual(sum(command == ['systemctl', 'start', 'sddm.service'] for command in commands), 1)
                self.assertGreaterEqual(trial.time.monotonic(), 45)
                self.assertEqual([call.args[1] for call in file.call_args_list], ['write', 'remove'])
            self.assertTrue((root / 'restored').exists())
            self.assertFalse((root / 'recovery-required.json').exists())
            self.assertFalse(json.loads((root / 'restore-result.json').read_text())['visible_output_verified'])

    def test_sddm_permanently_stuck_is_bounded_and_never_forced(self):
        clock = [0.0]
        def tick(seconds):
            clock[0] += seconds
        with patch.object(trial, 'run') as run, \
                patch.object(trial, 'show', return_value=self.unit(ActiveState='deactivating', MainPID='55', Job='12')), \
                patch.object(trial.time, 'monotonic', side_effect=lambda: clock[0]), \
                patch.object(trial.time, 'sleep', side_effect=tick):
            with self.assertRaisesRegex(trial.ShutdownTimeout, 'SDDM shutdown did not converge'):
                trial.stop_login_manager(trial.RECOVERY_SECONDS)
        self.assertEqual(run.call_count, 1)
        self.assertLessEqual(clock[0], trial.RECOVERY_SECONDS)

    def test_confirmations_require_explicit_visual_acknowledgement(self):
        for action in ('--confirm', '--confirm-restored'):
            with self.subTest(action=action), patch.object(trial.sys, 'argv', ['trial', action]), \
                    patch.object(trial, 'confirm') as confirm, patch.object(trial, 'confirm_restored') as baseline:
                with self.assertRaises(SystemExit) as error:
                    trial.main()
                self.assertEqual(error.exception.code, 2)
                confirm.assert_not_called()
                baseline.assert_not_called()

    def test_timeout_flag_does_not_pretend_to_extend_a_running_trial(self):
        with patch.object(trial.sys, 'argv', ['trial', '--check', '--timeout-seconds', '1800']):
            with self.assertRaises(SystemExit) as error:
                trial.main()
            self.assertEqual(error.exception.code, 2)

    def test_restored_baseline_requires_actual_nvidia_renderer_and_scanout(self):
        hardware = self.hardware()
        saved = {'config': self.config, 'gpus': hardware['gpus'], 'baseline_renderer': 'NVIDIA RTX'}
        for renderer in ('AMD Radeon', 'llvmpipe NVIDIA', 'NVIDIA different GPU', 'NVIDIA RTX'):
            with self.subTest(renderer=renderer), patch.object(trial, 'renderer_information',
                    return_value='OpenGL renderer string: ' + renderer), patch.object(trial, 'planner') as planner:
                planner.return_value.inspect_hardware.return_value = hardware
                if renderer == 'NVIDIA RTX':
                    self.assertEqual(trial.verify_renderer(saved, vendor='NVIDIA'), renderer)
                    hardware['connectors'][0]['enabled'] = 'disabled'
                    with self.assertRaisesRegex(RuntimeError, 'enabled NVIDIA'):
                        trial.verify_renderer(saved, vendor='NVIDIA')
                else:
                    with self.assertRaises(RuntimeError):
                        trial.verify_renderer(saved, vendor='NVIDIA')

    def test_confirmation_never_restarts_anything_and_only_then_marks_visual_success(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'restored').touch()
            (root / 'restore-result.json').write_text(json.dumps({'previous_kwin_invocation': 'old',
                                                                'visible_output_verified': False}))
            saved = {'config': self.config, 'override': 'owned'}
            with patch.object(trial, 'ROOT', root), patch.object(trial, 'state', return_value=saved), \
                    patch.object(trial, 'user_file') as file, patch.object(trial, 'run') as run, \
                    patch.object(trial, 'show', return_value=self.unit(ActiveState='active', MainPID='456', InvocationID='new')), \
                    patch.object(trial, 'verify_inference'), \
                    patch.object(trial, 'verify_renderer', return_value='NVIDIA RTX') as renderer:
                trial.confirm_restored()
            run.assert_not_called()
            file.assert_called_once_with(self.config, 'check', 'owned')
            renderer.assert_called_once_with(saved, vendor='NVIDIA')
            self.assertTrue(json.loads((root / 'restore-result.json').read_text())['visible_output_verified'])
            self.assertTrue((root / 'restore-confirmed').exists())

    def test_confirmation_refuses_same_dead_or_queued_compositor(self):
        for change in ({'InvocationID': 'old'}, {'MainPID': '0'}, {'ActiveState': 'failed'}, {'Job': '42'}):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / 'restored').touch()
                (root / 'restore-result.json').write_text('{"previous_kwin_invocation":"old"}')
                unit = self.unit(ActiveState='active', MainPID='456', InvocationID='new')
                unit.update(change)
                with patch.object(trial, 'ROOT', root), patch.object(trial, 'state', return_value={'config': self.config, 'override': 'owned'}), \
                        patch.object(trial, 'user_file'), patch.object(trial, 'show', return_value=unit), \
                        patch.object(trial, 'verify_inference'), patch.object(trial, 'verify_renderer') as renderer:
                    with self.assertRaisesRegex(RuntimeError, 'new active baseline'):
                        trial.confirm_restored()
                    renderer.assert_not_called()
                self.assertFalse((root / 'restore-confirmed').exists())

    def test_archive_of_started_trial_requires_visual_baseline_confirmation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'restored').touch()
            (root / 'started').touch()
            with patch.object(trial, 'ROOT', root), patch.object(trial, 'state', return_value={}), \
                    patch.object(trial, 'inactive_trial_units') as units:
                with self.assertRaisesRegex(RuntimeError, 'Visible baseline'):
                    trial.archive_restored()
                units.assert_not_called()


if __name__ == '__main__':
    unittest.main()
