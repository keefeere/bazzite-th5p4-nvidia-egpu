"""Offline guard tests; never contact host Cardwire or open GPU nodes."""
import importlib.util
from contextlib import contextmanager, ExitStack
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

spec = importlib.util.spec_from_file_location('smart', Path(__file__).resolve().parents[1] / 'diagnostics/test-cardwire-smart-runtime.py')
smart = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smart)


class SmartTestGuards(unittest.TestCase):
    def hardware(self):
        return {'errors': [], 'gpus': {'nvidia': {'pci': '0000:03:00.0', 'card': 'card7', 'driver': 'nvidia'},
                                      'igpu': {'pci': '0000:64:00.0', 'card': 'card4', 'driver': 'amdgpu'}},
                'connectors': [{'name': 'card7-DP-9', 'gpu': 'nvidia', 'status': 'connected',
                                'enabled': 'enabled', 'edid_sha256': 'monitor-a'},
                               {'name': 'card4-eDP-1', 'gpu': 'igpu', 'status': 'connected',
                                'enabled': 'enabled', 'edid_sha256': 'panel'}]}

    def kwin_report(self, name='DP-9', enabled='1', renderer='AMD Radeon 890M'):
        return ('Screens\n=======\nNumber of Screens: 2\n\n'
                'Screen 0:\n---------\nName: eDP-1\nEnabled: 1\nGeometry: 0,0,800x600\n'
                f'Screen 1:\n---------\nName: {name}\nEnabled: {enabled}\nGeometry: 800,0,800x600\n\n'
                f'Compositing\n===========\nOpenGL renderer string: {renderer}\n')

    def renderer_results(self):
        outputs = [
            'OpenGL renderer string: AMD Radeon 890M (radeonsi)\n',
            'OpenGL core profile renderer: AMD Radeon 890M (radeonsi)\n'
            'OpenGL compatibility profile renderer: AMD Radeon 890M (radeonsi)\n'
            'OpenGL ES profile renderer: AMD Radeon 890M (radeonsi)\n',
            'Selected GPU 0: AMD Radeon 890M (RADV GFX1150), type: IntegratedGpu\n',
        ]
        return [{'command': list(command), 'returncode': 0, 'stdout': output, 'stderr': ''}
                for command, output in zip(smart.RENDER_COMMANDS, outputs)]

    def test_private_override_does_not_require_the_old_runtime_binary_trial(self):
        with tempfile.TemporaryDirectory() as directory:
            dropin = Path(directory) / 'systemd/system/cardwired.service.d/91-egpu-smart-test.conf'
            with patch.object(smart, 'DROPIN', dropin):
                smart.install_private_override('our private state')
            self.assertEqual(dropin.read_text(), 'our private state')

    def test_private_override_never_overwrites_an_existing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            dropin = Path(directory) / '91-egpu-smart-test.conf'
            dropin.write_text('foreign')
            with patch.object(smart, 'DROPIN', dropin):
                with self.assertRaises(FileExistsError):
                    smart.install_private_override('ours')
            self.assertEqual(dropin.read_text(), 'foreign')

    def test_verified_live_daemon_not_the_packaged_cli_version(self):
        with patch.object(smart, 'sha', return_value=smart.EXPECTED_SHA) as sha:
            self.assertEqual(smart.verified_daemon_sha(1234), smart.EXPECTED_SHA)
            sha.assert_called_once_with(Path('/proc/1234/exe'))

    def test_old_prototype_and_unknown_binaries_cannot_start_new_trials(self):
        for digest in ('07baf8f89af7575fedf630c1c741c44b114477ed4a75b9712d7e7e233356d6ff', 'unknown'):
            with self.subTest(digest=digest), patch.object(smart, 'sha', return_value=digest):
                with self.assertRaisesRegex(RuntimeError, 'secure Cardwire'):
                    smart.verified_daemon_sha(1234)

    def test_invalid_daemon_pid_cannot_hash_arbitrary_process(self):
        for pid in (0, 1, -1, True, '1234'):
            with self.subTest(pid=pid), patch.object(smart, 'sha') as sha:
                with self.assertRaisesRegex(RuntimeError, 'Invalid'):
                    smart.verified_daemon_sha(pid)
                sha.assert_not_called()

    def test_exact_trial_accepts_only_the_vm_tested_native_bytes(self):
        with patch.object(smart, 'ROOT', smart.EXACT_RENDER_ROOT):
            for digest in (smart.EXPECTED_SHA, 'unknown'):
                with patch.object(smart, 'sha', return_value=digest):
                    with self.assertRaisesRegex(RuntimeError, 'specific trial'):
                        smart.verified_daemon_sha(1234)
            with patch.object(smart, 'sha', return_value=smart.EXACT_SHA):
                self.assertEqual(smart.verified_daemon_sha(1234), smart.EXACT_SHA)

    def test_exact_staged_watchdog_uses_late_private_mount_override(self):
        source = Path(smart.__file__).read_text()
        namespace = {'__file__': str(smart.EXACT_RENDER_ROOT / 'test-cardwire-smart-runtime.py'),
                     '__name__': 'staged_watchdog_not_main'}
        exec(compile(source, smart.__file__, 'exec'), namespace)
        self.assertEqual(namespace['ROOT'], smart.EXACT_RENDER_ROOT)
        self.assertEqual(namespace['DROPIN'], smart.EXACT_DROPIN)
        self.assertGreater(namespace['DROPIN'].name, '98-exact-candidate-test.conf')

    def test_legacy_staged_watchdog_keeps_its_original_override(self):
        source = Path(smart.__file__).read_text()
        namespace = {'__file__': str(smart.AMD_RENDER_ROOT / 'test-cardwire-smart-runtime.py'),
                     '__name__': 'staged_watchdog_not_main'}
        exec(compile(source, smart.__file__, 'exec'), namespace)
        self.assertEqual(namespace['ROOT'], smart.AMD_RENDER_ROOT)
        self.assertEqual(namespace['DROPIN'], smart.LEGACY_DROPIN)

    def test_candidate_cannot_silently_replace_baseline_trial(self):
        with patch.object(smart, 'sha', return_value=smart.EXACT_SHA):
            with self.assertRaisesRegex(RuntimeError, 'specific trial'):
                smart.verified_daemon_sha(1234)

    def test_exact_requires_render_and_amd_before_any_host_access(self):
        for render, amd in ((False, False), (True, False), (False, True)):
            with patch.object(smart, 'ROOT', smart.EXACT_RENDER_ROOT), \
                    patch.object(smart, 'load_planner') as planner, patch.object(smart, 'run') as run:
                with self.assertRaisesRegex(RuntimeError, 'AMD-primary'):
                    smart.apply(render=render, require_amd=amd)
                planner.assert_not_called()
                run.assert_not_called()

    def test_unique_bus_owner_must_still_run_the_verified_build(self):
        with patch.object(smart, 'run', side_effect=[json.dumps({'data': [':1.456']}), json.dumps({'data': [1234]})]), \
                patch.object(smart.os.path, 'samefile', return_value=True), \
                patch.object(smart, 'verified_daemon_sha', side_effect=RuntimeError('wrong build')) as verify:
            with self.assertRaisesRegex(RuntimeError, 'wrong build'):
                smart.private_daemon_owner()
            verify.assert_called_once_with(1234)

    def test_dynamic_nodes(self):
        targets = smart.device_targets({'0': {'vendor': 'AMD', 'render': 137},
                                        '1': {'vendor': 'Nvidia', 'card': 7, 'render': 139, 'nvidia_minor': '0'}})
        self.assertEqual(targets['amd'], ['/dev/dri/renderD137'])
        self.assertIn('/dev/dri/card7', targets['nvidia'])

    def test_nonzero_minor_is_not_guessed(self):
        with self.assertRaises(RuntimeError):
            smart.device_targets({'0': {'vendor': 'AMD'}, '1': {'vendor': 'Nvidia', 'nvidia_minor': '2'}})

    def test_no_grant_to_controller_or_session_manager(self):
        for pid, exe in ((99, '/usr/bin/python3'), (42, None)):
            with self.subTest(pid=pid, exe=exe), self.assertRaises(RuntimeError):
                smart.validate_grant({'pid': pid, 'executable': exe}, 99)
        for pid, exe in ((1, '/usr/lib/systemd/systemd'), (42, '/usr/lib/systemd/systemd'), (42, '/usr/bin/sddm')):
            self.assertFalse(smart.validate_grant({'pid': pid, 'executable': exe}, 99))
        self.assertTrue(smart.validate_grant({'pid': 42, 'executable': '/usr/bin/kwin_wayland'}, 99))

    def test_writes_use_unique_bus_owner(self):
        with patch.object(smart, 'run') as run:
            smart.set_property(':1.456', 'Mode', 'Mode', 'u', 3)
        self.assertEqual(run.call_args.args[0][2], ':1.456')

    def test_owner_requires_both_private_mounts(self):
        with patch.object(smart, 'run', side_effect=[json.dumps({'data': [':1.456']}), json.dumps({'data': [1234]})]), \
                patch.object(smart.os.path, 'samefile', return_value=False):
            with self.assertRaisesRegex(RuntimeError, 'private test'):
                smart.private_daemon_owner()

    def test_probe_uses_fresh_user_process_not_granted_parent(self):
        with patch.object(smart, 'run', return_value='{}') as run, \
                patch.dict(smart.os.environ, {'CARDWIRE_FORCE_GPU': '1', 'CARDWIRE_ALLOW': '1'}):
            smart.probe({'DESKTOP_USER': 'tester'}, {'nvidia': [], 'amd': []}, False)
        args, kwargs = run.call_args
        self.assertEqual(args[0][:4], ['runuser', '-u', 'tester', '--'])
        self.assertEqual(kwargs['env']['CARDWIRE_ALLOW'], '0')
        self.assertNotIn('CARDWIRE_FORCE_GPU', kwargs['env'])

    def test_child_code_compiles(self):
        compile(smart.CHILD, '<child>', 'exec')

    def test_amd_renderer_and_exit_zero_do_not_hide_broken_presentation(self):
        for index in range(3):
            for changes in ({'stderr': 'CreateSwapchainKHR failed'}, {'stdout': 'llvmpipe'},
                            {'returncode': 124}, {'stderr': 'VK_ERROR_INITIALIZATION_FAILED'},
                            {'error': 'timed out after 20s'}, {'returncode': False}):
                results = self.renderer_results()
                results[index].update(changes)
                with self.subTest(index=index, changes=changes):
                    self.assertTrue(smart.renderer_failure(results))

    def test_all_three_positive_renderer_results_still_need_visual_confirmation(self):
        result = smart.renderer_assessment(self.renderer_results())
        self.assertTrue(result['hardware_commands_verified'])
        self.assertEqual(result['errors'], [])
        self.assertEqual(set(result['renderers']), {'glxinfo', 'eglinfo', 'vkcube'})
        self.assertFalse(result['visible_output_verified'])

    def test_missing_empty_duplicate_or_wrong_probe_cannot_pass(self):
        for results in (None, [], [{}], self.renderer_results()[:2],
                        [self.renderer_results()[0]] * 3):
            with self.subTest(results=results):
                self.assertTrue(smart.renderer_failure(results))
        for index in range(3):
            for changes in ({'stdout': ''}, {'command': ['wrong', '-B']}, {'stdout': None},
                            {'returncode': None}):
                results = self.renderer_results()
                results[index].update(changes)
                with self.subTest(index=index, changes=changes):
                    self.assertTrue(smart.renderer_failure(results))

    def test_vendor_or_requested_environment_is_not_renderer_evidence(self):
        for index in range(3):
            results = self.renderer_results()
            results[index]['stdout'] = 'AMD Radeon\nEGL vendor string: Mesa\nDRI_PRIME=pci-0000_64_00_0\n'
            with self.subTest(index=index):
                self.assertTrue(smart.renderer_failure(results))

    def test_nvidia_success_and_mixed_egl_contexts_do_not_pass(self):
        for index in range(3):
            results = self.renderer_results()
            results[index]['stdout'] = results[index]['stdout'].replace('AMD Radeon 890M', 'NVIDIA RTX 5070 Ti')
            with self.subTest(index=index):
                self.assertTrue(smart.renderer_failure(results))
        results = self.renderer_results()
        results[1]['stdout'] += 'OpenGL ES profile renderer: llvmpipe (LLVM)\n'
        self.assertTrue(smart.renderer_failure(results))

    def test_vulkan_selected_device_can_be_reported_on_stderr(self):
        results = self.renderer_results()
        results[2]['stderr'], results[2]['stdout'] = results[2]['stdout'], ''
        self.assertFalse(smart.renderer_failure(results))

    def test_amd_presentation_requires_actual_amd_renderer_and_nv_scanout(self):
        config = {'DESKTOP_USER': 'tester', 'DESKTOP_UID': '1001'}
        hardware = self.hardware()
        for text in ('OpenGL renderer string: NVIDIA RTX', 'OpenGL renderer string: llvmpipe (AMD)',
                     'KWIN_DRM_DEVICES=AMD:NVIDIA', ''):
            with self.subTest(text=text), patch.object(smart, 'run', return_value=json.dumps({'data': [text]})):
                with self.assertRaisesRegex(RuntimeError, 'AMD hardware compositor'):
                    smart.amd_compositor(config, hardware)
        good = json.dumps({'data': [self.kwin_report()]})
        with patch.object(smart, 'run', return_value=good) as run:
            self.assertEqual(smart.amd_compositor(config, hardware), 'AMD Radeon 890M')
            self.assertIn('DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1001/bus', run.call_args.args[0])
            hardware['connectors'][0]['enabled'] = 'disabled'
            with self.assertRaisesRegex(RuntimeError, 'enabled NVIDIA monitor'):
                smart.amd_compositor(config, hardware)

    def test_sysfs_baseline_cannot_replace_live_kwin_output_evidence(self):
        for report in (self.kwin_report(enabled='0'), self.kwin_report(name='DP-10')):
            with self.subTest(report=report), patch.object(smart, 'run', return_value=json.dumps({'data': [report]})):
                with self.assertRaisesRegex(RuntimeError, 'no longer reports'):
                    smart.amd_compositor({'DESKTOP_USER': 'tester', 'DESKTOP_UID': '1001'}, self.hardware())

    def test_kwin_parser_rejects_missing_unknown_duplicate_or_incomplete_reports(self):
        good = self.kwin_report()
        for report in ('', good.replace('Screens\n', 'Outputs\n'),
                       good.replace('Number of Screens: 2', 'Number of Screens: 3'),
                       good.replace('Screen 1:', 'Screen 0:'), good.replace('Name: DP-9', 'Name: eDP-1'),
                       good.replace('Enabled: 1', 'Enabled: true'),
                       good.replace('Name: DP-9', 'Name: DP-9\nName: DP-8')):
            with self.subTest(report=report), self.assertRaises(RuntimeError):
                smart.kwin_outputs(report)
        self.assertEqual(smart.kwin_outputs(good), {'eDP-1': True, 'DP-9': True})

    def test_connector_name_collision_cannot_misattribute_a_monitor(self):
        hardware = self.hardware()
        hardware['connectors'].append({'name': 'card4-DP-9', 'gpu': 'igpu', 'status': 'disconnected',
                                       'enabled': 'disabled', 'edid_sha256': None})
        with self.assertRaisesRegex(RuntimeError, 'Ambiguous cross-GPU'):
            smart.nvidia_outputs(hardware)

    def test_all_baseline_nvidia_outputs_must_remain_enabled(self):
        hardware = self.hardware()
        hardware['connectors'].append({'name': 'card7-HDMI-A-1', 'gpu': 'nvidia', 'status': 'connected',
                                       'enabled': 'enabled', 'edid_sha256': 'monitor-b'})
        with patch.object(smart, 'run', return_value=json.dumps({'data': [self.kwin_report()]})):
            with self.assertRaisesRegex(RuntimeError, 'no longer reports'):
                smart.amd_compositor({'DESKTOP_USER': 'tester', 'DESKTOP_UID': '1001'}, hardware)

    def test_renderer_results_are_checkpointed_after_each_command(self):
        results = self.renderer_results()
        processes = [smart.subprocess.CompletedProcess(r['command'], r['returncode'], r['stdout'], r['stderr'])
                     for r in results]
        saved = []
        with patch.object(smart.subprocess, 'run', side_effect=processes):
            report = smart.renderer_probes({'DESKTOP_USER': 'tester'}, {}, self.hardware(),
                                           lambda items: saved.append(deepcopy(items)))
        self.assertEqual([len(items) for items in saved], [1, 2, 3])
        self.assertEqual(report, [{**r, 'stdout': r['stdout'].strip()} for r in results])

    @contextmanager
    def trial(self, reports=None, restore_error=None, probe_error=None, post_hardware=None, modes=None,
              exact_status=None):
        """Full apply control flow with ALL host I/O stubbed, real private files."""
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            stack.enter_context(patch('builtins.print'))
            root = Path(tmp) / 'private-trial'
            stack.enter_context(patch.object(smart, 'ROOT', root))
            stack.enter_context(patch.object(smart, 'DROPIN', Path(tmp) / 'dropins/private.conf'))
            planner = MagicMock()
            planner.read_hardware_config.return_value = {'DESKTOP_USER': 'tester', 'DESKTOP_UID': '1001'}
            planner.inspect_cardwire.return_value = {'errors': [], 'mode': 1, 'gpus': {}}
            planner.inspect_hardware.side_effect = [self.hardware(), post_hardware or self.hardware()]
            planner.make_plan.return_value = {'blockers': []}
            planner.inspect_service_scopes.return_value = {'errors': [], 'units': [
                {'unit': 'plasma-kwin_wayland.service', 'processes': [{'pid': 4444, 'role_hint': 'compositor'}]}]}
            planner.process_identity.return_value = {'pid': 4444, 'start_ticks': 100,
                'executable': '/usr/bin/kwin_wayland', 'euid': 1001, 'cgroup': '/test/kwin'}
            stack.enter_context(patch.object(smart, 'load_planner', return_value=planner))
            stack.enter_context(patch.object(smart, 'device_targets', return_value={'amd': [], 'nvidia': []}))
            stack.enter_context(patch.object(smart, 'gpu_client_pids', return_value={4444}))
            stack.enter_context(patch.object(smart, 'display_environment', return_value={}))
            stack.enter_context(patch.object(smart, 'verified_daemon_sha', return_value=smart.EXPECTED_SHA))
            stack.enter_context(patch.object(smart, 'sha', return_value='digest'))
            stack.enter_context(patch.object(smart, 'private_daemon_owner', return_value=':1.123'))
            stack.enter_context(patch.object(smart.shutil, 'copytree'))
            stack.enter_context(patch.object(smart.shutil, 'copy2'))
            reports = iter(reports or [self.kwin_report()] * 3)
            def fake_run(args, **kwargs):
                if args[-1] == 'supportInformation':
                    return json.dumps({'data': [next(reports)]})
                if args[:3] == ['systemctl', 'show', 'cardwired.service']:
                    return '9010'
                if 'GetProcessStatus' in args:
                    return json.dumps({'data': exact_status or ['AllowedExact', [0]]})
                return ''
            run = stack.enter_context(patch.object(smart, 'run', side_effect=fake_run))
            values = iter(modes or [3, 3, 3])
            stack.enter_context(patch.object(smart, 'get', side_effect=lambda interface, key, owner=None:
                                            next(values) if interface == 'Mode' else False))
            checks = iter([{'uid': 1001, 'allowed': False}, {'uid': 1001, 'allowed': True, 'cuda_device_count': 1},
                           {'uid': 0, 'allowed': False}])
            stack.enter_context(patch.object(smart, 'probe', side_effect=probe_error or (lambda *args: next(checks))))
            def fake_render(config, display, hardware, checkpoint):
                results = self.renderer_results()
                for count in range(1, 4):
                    checkpoint(results[:count])
                return results
            stack.enter_context(patch.object(smart, 'renderer_probes', side_effect=fake_render))
            restore = stack.enter_context(patch.object(smart, 'restore', side_effect=restore_error))
            yield root, planner, run, restore

    def test_apply_uses_live_kwin_without_reenumerating_hidden_drm_under_smart(self):
        with self.trial() as (root, planner, run, restore):
            smart.apply(render=True, require_amd=True)
            result = json.loads((root / 'result.json').read_text())
            self.assertEqual(result['status'], 'device-access-passed-rendering-needs-visual-confirmation')
            self.assertEqual(result['rollback'], 'restored')
            self.assertTrue(result['post_rollback']['topology_verified'])
            self.assertFalse(result['visible_output_verified'])
            self.assertEqual(result['live_presentation']['enabled_nvidia_outputs'], ['DP-9'])
            self.assertEqual(len(result['checks']), 3)
            self.assertEqual(planner.inspect_hardware.call_count, 2)  # preflight and AFTER rollback only
            restore.assert_called_once()
            grants = [c.args[0] for c in run.call_args_list if 'RequestProcessAccess' in c.args[0]]
            self.assertEqual([args[-3:] for args in grants], [['4444', 'Allow_dGPU', '1']])

    def test_final_presentation_failure_keeps_completed_probe_evidence(self):
        with self.trial(reports=[self.kwin_report(), self.kwin_report(enabled='0')]) as (root, _, _, restore):
            with self.assertRaisesRegex(RuntimeError, 'no longer reports'):
                smart.apply(render=True, require_amd=True)
            result = json.loads((root / 'result.json').read_text())
            self.assertEqual(result['status'], 'failed')
            self.assertEqual(result['failure']['stage'], 'live-presentation')
            self.assertEqual(result['rollback'], 'restored')
            self.assertEqual(len(result['checks']), 3)
            self.assertEqual(len(result['renderer_probes']), 3)
            restore.assert_called_once()

    def test_exact_trial_never_uses_an_inheritable_grant(self):
        with self.trial() as (root, _, run, restore), patch.object(smart, 'EXACT_RENDER_ROOT', root):
            smart.apply(render=True, require_amd=True)
            result = json.loads((root / 'result.json').read_text())
            self.assertEqual(result['grant_policy'], 'Allow_dGPU_Exact')
            self.assertFalse(result['persistent_profile_verified'])
            self.assertFalse(result['visible_output_verified'])
            grants = [c.args[0] for c in run.call_args_list if 'RequestProcessAccess' in c.args[0]]
            self.assertEqual([args[-3:] for args in grants], [['4444', 'Allow_dGPU_Exact', '1']])
            reads = [c.args[0] for c in run.call_args_list if 'GetProcessStatus' in c.args[0]]
            self.assertEqual(len(reads), 1)
            self.assertEqual(reads[0][3], ':1.123')
            restore.assert_called_once()

    def test_exact_readback_mismatch_restores_without_entering_smart(self):
        with self.trial(exact_status=['Allowed', [0]]) as (root, _, run, restore), \
                patch.object(smart, 'EXACT_RENDER_ROOT', root):
            with self.assertRaisesRegex(RuntimeError, 'Exact grant readback'):
                smart.apply(render=True, require_amd=True)
            writes = [c.args[0] for c in run.call_args_list if 'set-property' in c.args[0]]
            self.assertEqual(writes, [])
            grants = [c.args[0] for c in run.call_args_list if 'RequestProcessAccess' in c.args[0]]
            self.assertEqual(len(grants), 1)  # no legacy fallback/retry
            restore.assert_called_once()
            result = json.loads((root / 'result.json').read_text())
            self.assertEqual(result['status'], 'failed')
            self.assertEqual(result['rollback'], 'restored')

    def test_probe_failure_keeps_previous_results_and_restores(self):
        with self.trial(probe_error=[{'uid': 1001, 'allowed': False}, RuntimeError('CUDA failure')]) as (root, _, _, restore):
            with self.assertRaisesRegex(RuntimeError, 'CUDA failure'):
                smart.apply(render=True, require_amd=True)
            result = json.loads((root / 'result.json').read_text())
            self.assertEqual(len(result['checks']), 1)
            self.assertEqual(result['failure']['stage'], 'user-cuda-allowed')
            self.assertEqual(result['rollback'], 'restored')
            restore.assert_called_once()

    def test_rollback_error_cannot_be_overwritten_by_pass_or_disable_watchdog(self):
        with self.trial(restore_error=RuntimeError('rollback failed')) as (root, _, run, _):
            with self.assertRaisesRegex(RuntimeError, 'rollback failed'):
                smart.apply(render=True, require_amd=True)
            result = json.loads((root / 'result.json').read_text())
            self.assertEqual(result['status'], 'failed')
            self.assertEqual(result['rollback'], 'failed')
            self.assertEqual(result['rollback_error']['message'], 'rollback failed')
            self.assertNotIn(['systemctl', 'stop', smart.TIMER + '.timer'], [c.args[0] for c in run.call_args_list])

    def test_both_test_failure_and_rollback_failure_are_retained(self):
        with self.trial(probe_error=RuntimeError('probe failed'), restore_error=RuntimeError('restore failed')) as (root, _, _, _):
            with self.assertRaisesRegex(RuntimeError, 'restore failed'):
                smart.apply(render=True, require_amd=True)
            result = json.loads((root / 'result.json').read_text())
            self.assertEqual(result['failure']['message'], 'probe failed')
            self.assertEqual(result['rollback_error']['message'], 'restore failed')

    def test_watchdog_ending_smart_before_observation_cannot_pass(self):
        with self.trial(modes=[3, 1]) as (root, _, _, restore):
            with self.assertRaisesRegex(RuntimeError, 'Smart policy changed'):
                smart.apply(render=True, require_amd=True)
            result = json.loads((root / 'result.json').read_text())
            self.assertEqual(result['status'], 'failed')
            restore.assert_called_once()

    def test_post_rollback_changed_monitor_fails_without_second_restart(self):
        hardware = self.hardware()
        hardware['connectors'][0]['edid_sha256'] = 'different-monitor'
        with self.trial(post_hardware=hardware) as (root, _, _, restore):
            with self.assertRaisesRegex(RuntimeError, 'topology changed'):
                smart.apply(render=True, require_amd=True)
            result = json.loads((root / 'result.json').read_text())
            self.assertEqual(result['failure']['stage'], 'post-rollback-presentation')
            self.assertEqual(result['rollback'], 'restored')
            restore.assert_called_once()

    def test_post_rollback_gpu_renumbering_is_not_silently_accepted(self):
        hardware = self.hardware()
        hardware['gpus']['nvidia']['card'] = 'card8'
        with self.trial(post_hardware=hardware) as (root, _, _, restore):
            with self.assertRaisesRegex(RuntimeError, 'topology changed'):
                smart.apply(render=True, require_amd=True)
            self.assertEqual(json.loads((root / 'result.json').read_text())['status'], 'failed')
            restore.assert_called_once()

    def test_renderer_failure_is_saved_and_cannot_pass(self):
        with self.trial() as (root, _, _, restore):
            failed = self.renderer_results()
            failed[2]['returncode'] = 124
            with patch.object(smart, 'renderer_probes', return_value=failed):
                with self.assertRaisesRegex(RuntimeError, 'Hardware renderer probes failed'):
                    smart.apply(render=True, require_amd=True)
            result = json.loads((root / 'result.json').read_text())
            self.assertEqual(result['failure']['stage'], 'renderer-probes')
            self.assertEqual(result['renderer_probes'][2]['returncode'], 124)
            self.assertFalse(result['renderer_assessment']['hardware_commands_verified'])
            restore.assert_called_once()

    def test_mode_observations_can_target_the_unique_daemon_owner(self):
        with patch.object(smart, 'run', return_value=json.dumps({'data': 3})) as run:
            self.assertEqual(smart.get('Mode', 'Mode', ':1.123'), 3)
            self.assertEqual(run.call_args.args[0][3], ':1.123')

    def test_changed_compositor_cannot_pass_even_if_renderer_string_is_same(self):
        for changed_call in (4, 5):  # Smart observation, or after rollback
            with self.subTest(changed_call=changed_call), self.trial() as (root, planner, _, restore):
                identity = planner.process_identity.return_value
                planner.process_identity.side_effect = [deepcopy(identity)] * (changed_call - 1) + [
                    {**identity, 'start_ticks': identity['start_ticks'] + 1}]
                with self.assertRaisesRegex(RuntimeError, 'Compositor process changed'):
                    smart.apply(render=True, require_amd=True)
                result = json.loads((root / 'result.json').read_text())
                self.assertEqual(result['status'], 'failed')
                self.assertEqual(result['rollback'], 'restored')
                restore.assert_called_once()

    def test_atomic_checkpoint_keeps_previous_report_if_replace_fails(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(smart, 'ROOT', Path(tmp)):
            smart.save_result({'checks': ['first'], 'status': 'in-progress'})
            with patch.object(Path, 'replace', side_effect=OSError('replace failed')):
                with self.assertRaises(OSError):
                    smart.save_result({'checks': ['first', 'second']})
            self.assertEqual(json.loads((Path(tmp) / 'result.json').read_text())['checks'], ['first'])

    def test_amd_render_trial_has_separate_artifacts_and_watchdog(self):
        with patch.object(smart.sys, 'argv', ['test', '--apply-amd-render']), \
                patch.object(smart.os, 'geteuid', return_value=0), patch('builtins.open'), \
                patch.object(smart.fcntl, 'flock'), patch.object(smart, 'apply') as apply, \
                patch.object(smart, 'ROOT', smart.ROOT), patch.object(smart, 'TIMER', smart.TIMER):
            smart.main()
            self.assertEqual(smart.ROOT, smart.AMD_RENDER_ROOT)
            self.assertNotEqual(smart.ROOT, smart.RENDER_ROOT)
            self.assertEqual(smart.TIMER, 'egpu-cardwire-smart-amd-render-test-rollback')
            apply.assert_called_once_with(render=True, require_amd=True)

    def test_exact_action_has_separate_artifacts_and_watchdog(self):
        with patch.object(smart.sys, 'argv', ['test', '--apply-exact-amd-render']), \
                patch.object(smart.os, 'geteuid', return_value=0), patch('builtins.open'), \
                patch.object(smart.fcntl, 'flock'), patch.object(smart, 'apply') as apply, \
                patch.object(smart, 'ROOT', smart.ROOT), patch.object(smart, 'TIMER', smart.TIMER):
            smart.main()
            self.assertEqual(smart.ROOT, smart.EXACT_RENDER_ROOT)
            self.assertNotEqual(smart.ROOT, smart.AMD_RENDER_ROOT)
            self.assertEqual(smart.TIMER, 'egpu-cardwire-smart-exact-render-test-rollback')
            apply.assert_called_once_with(render=True, require_amd=True)

    @contextmanager
    def archive_trial(self):
        """Exercise the real archive rename, with no host service/file access."""
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            root = Path(tmp) / 'trial'
            root.mkdir()
            (root / 'restored').touch()
            baseline = {'mode': 1, 'daemon_sha256': smart.EXPECTED_SHA,
                        'config': {key: False for key in smart.PROPERTIES},
                        'files': {name: 'digest' for name in ('/etc/cardwire/cardwire.toml', '/var/lib/cardwire/mode.json')}}
            (root / 'before.json').write_text(json.dumps(baseline))
            (root / 'evidence.txt').write_text('preserve this')
            dropin = Path(tmp) / 'private.conf'
            stack.enter_context(patch.object(smart, 'ROOT', root))
            stack.enter_context(patch.object(smart, 'DROPIN', dropin))
            original = Path.stat
            def stat_owned(path, *args, **kwargs):
                result = original(path, *args, **kwargs)
                if path in (root, root / 'restored'):
                    values = list(result)
                    values[4] = 0
                    return smart.os.stat_result(values)
                return result
            stack.enter_context(patch.object(Path, 'stat', stat_owned))
            def state(args):
                if args[2] == 'cardwired.service':
                    return 'ActiveState=active\nSubState=running\nMainPID=9010\nJob='
                return f'Id={args[2]}\nActiveState=inactive\nSubState=dead\nMainPID=0\nJob='
            run = stack.enter_context(patch.object(smart, 'run', side_effect=state))
            get = stack.enter_context(patch.object(smart, 'get', side_effect=lambda i, k: 1 if i == 'Mode' else False))
            digest = stack.enter_context(patch.object(smart, 'sha', return_value='digest'))
            verify = stack.enter_context(patch.object(smart, 'verified_daemon_sha', return_value=smart.EXPECTED_SHA))
            stack.enter_context(patch('builtins.print'))
            yield root, dropin, run, get, digest, verify

    def test_archive_preserves_evidence_without_service_or_policy_writes(self):
        with self.archive_trial() as (root, _, run, _, _, verify):
            destination = smart.archive_restored()
            self.assertFalse(root.exists())
            self.assertEqual((destination / 'evidence.txt').read_text(), 'preserve this')
            self.assertEqual(destination.parent.stat().st_mode & 0o777, 0o700)
            self.assertTrue(all(c.args[0][:2] == ['systemctl', 'show'] for c in run.call_args_list))
            verify.assert_called_once_with(9010)

    def test_archive_rejects_missing_marker_or_any_private_override(self):
        for change in ('no-marker', 'marker-link', 'override', 'broken-override-link'):
            with self.subTest(change=change), self.archive_trial() as (root, dropin, run, _, _, _):
                if change in ('no-marker', 'marker-link'):
                    (root / 'restored').unlink()
                    if change == 'marker-link':
                        (root / 'restored').symlink_to(root / 'evidence.txt')
                elif change == 'override':
                    dropin.write_text('private mount')
                else:
                    dropin.symlink_to(root / 'nonexistent')
                with self.assertRaises(RuntimeError):
                    smart.archive_restored()
                run.assert_not_called()
                self.assertTrue(root.exists())

    def test_archive_refuses_active_pending_failed_or_unknown_watchdog(self):
        for changed in ('ActiveState=active', 'ActiveState=failed', 'Job=123', 'MainPID=42', 'Id=wrong'):
            with self.subTest(changed=changed), self.archive_trial() as (root, _, run, _, _, _):
                original = run.side_effect
                def state(args):
                    key = changed.partition('=')[0]
                    return '\n'.join(changed if line.startswith(key + '=') else line
                                     for line in original(args).splitlines())
                run.side_effect = state
                with self.assertRaisesRegex(RuntimeError, 'quiescent'):
                    smart.archive_restored()
                self.assertTrue(root.exists())

    def test_archive_refuses_changed_mode_policy_files_or_binary(self):
        for change in ('mode', 'policy', 'file', 'binary', 'service'):
            with self.subTest(change=change), self.archive_trial() as (root, _, run, get, digest, verify):
                if change == 'mode':
                    get.side_effect = lambda i, k: 3 if i == 'Mode' else False
                elif change == 'policy':
                    get.side_effect = lambda i, k: 1 if i == 'Mode' else True
                elif change == 'file':
                    digest.return_value = 'changed'
                elif change == 'binary':
                    verify.return_value = 'changed'
                else:
                    original = run.side_effect
                    run.side_effect = lambda args: original(args).replace('SubState=running', 'SubState=stop')
                with self.assertRaises(RuntimeError):
                    smart.archive_restored()
                self.assertTrue(root.exists())

    def test_archive_action_never_calls_apply_or_restore(self):
        with patch.object(smart.sys, 'argv', ['test', '--archive-amd-render']), \
                patch.object(smart.os, 'geteuid', return_value=0), patch('builtins.open'), \
                patch.object(smart.fcntl, 'flock'), patch.object(smart, 'apply') as apply, \
                patch.object(smart, 'restore') as restore, patch.object(smart, 'archive_restored') as archive, \
                patch.object(smart, 'ROOT', smart.ROOT), patch.object(smart, 'TIMER', smart.TIMER):
            self.assertEqual(smart.main(), 0)
            self.assertEqual(smart.ROOT, smart.AMD_RENDER_ROOT)
            archive.assert_called_once_with()
            apply.assert_not_called()
            restore.assert_not_called()

    def test_restore_refuses_foreign_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dropin = root / 'override-live.conf'
            dropin.write_text('foreign')
            (root / 'override.conf').write_text('ours')
            # Fake ownership while preserving is_dir()/is_symlink() modes.
            original = Path.stat
            def stat_owned(path, *args, **kwargs):
                result = original(path, *args, **kwargs)
                if path == root:
                    values = list(result)
                    values[4] = 0
                    return smart.os.stat_result(values)
                return result
            with patch.object(smart, 'ROOT', root), patch.object(smart, 'DROPIN', dropin), \
                    patch.object(Path, 'stat', stat_owned), patch.object(smart, 'run') as run:
                with self.assertRaisesRegex(RuntimeError, 'foreign'):
                    smart.restore()
                run.assert_not_called()
            self.assertEqual(dropin.read_text(), 'foreign')


if __name__ == '__main__':
    unittest.main()
