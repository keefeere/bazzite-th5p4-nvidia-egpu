"""Synthetic topology and fake D-Bus tests. No host mode/policy changes."""
import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location(
    'profiles', Path(__file__).resolve().parents[1] / 'egpu-desktop-profile-plan.py')
profiles = importlib.util.module_from_spec(spec)
spec.loader.exec_module(profiles)


class ProfilePlans(unittest.TestCase):
    def setUp(self):
        self.config = {'DESKTOP_USER': 'tester', 'DESKTOP_UID': '1000',
                       'EGPU_VENDOR': '0x10de', 'EGPU_DEVICE': '0x2c05',
                       'IGPU_VENDOR': '0x1002', 'IGPU_DEVICE': '0x150e'}
        self.hardware = {'errors': [], 'gpus': {
            'nvidia': {'pci': '0000:09:00.0', 'card': 'card7', 'driver': 'nvidia'},
            'igpu': {'pci': '0000:61:00.0', 'card': 'card4', 'driver': 'amdgpu'}},
            'connectors': [{'name': 'card7-DP-23', 'gpu': 'nvidia', 'status': 'connected', 'enabled': 'enabled'},
                           {'name': 'card4-eDP-1', 'gpu': 'igpu', 'status': 'connected', 'enabled': 'enabled'}]}
        self.cardwire = {'errors': [], 'available_modes': [0, 1, 3], 'app_policies': [{}],
                         'gpus': {
                             '0': {'id': 0, 'pci': '0000:61:00.0', 'default': True, 'discrete': False},
                             '1': {'id': 1, 'pci': '0000:09:00.0', 'default': False, 'discrete': True}}}
        self.probe = {'status': 'passed'}

    def plan(self, name='work-nvidia', **kwargs):
        return profiles.make_plan(name, self.config, self.hardware, self.cardwire,
                                  kwargs.get('units', ['llama.service']), kwargs.get('probe', self.probe),
                                  kwargs.get('scopes'))

    def test_dynamic_gpu_nodes_not_historical_numbers(self):
        plan = self.plan('gaming-nvidia')
        self.assertEqual(plan['blockers'], [])
        self.assertEqual(plan['session_environment_candidate']['KWIN_DRM_DEVICES'], '/dev/dri/card7:/dev/dri/card4')
        self.assertNotIn('DRI_PRIME', plan['session_environment_candidate'])

    def test_work_nvidia_preserves_scanout_but_routes_apps_amd(self):
        plan = self.plan()
        self.assertEqual(plan['blockers'], [])
        self.assertEqual(plan['cardwire_candidate']['mode'], 'smart')
        self.assertEqual(plan['session_environment_candidate']['DRI_PRIME'], 'pci-0000_61_00_0')
        self.assertEqual(plan['session_environment_candidate']['KWIN_DRM_DEVICES'], '/dev/dri/card4:/dev/dri/card7')
        self.assertEqual(plan['primary_renderer_candidate'], 'amd')
        self.assertEqual(plan['compositor_environment_candidate'], {'CARDWIRE_ALLOW': '0'})

    def test_never_grant_entire_session(self):
        self.assertNotIn('CARDWIRE_ALLOW', self.plan()['session_environment_candidate'])
        self.assertEqual(self.plan()['compute_environment_candidate'], {'CARDWIRE_ALLOW': '1'})
        self.assertIn('VK_LOADER_DRIVERS_SELECT', self.plan()['compute_remove_inherited_render_overrides'])

    def test_work_igpu_rejects_connected_nvidia(self):
        self.assertTrue(any('Move/disconnect' in s for s in self.plan('work-igpu')['blockers']))

    def test_dpms_off_is_not_unplug(self):
        self.hardware['connectors'][0]['enabled'] = 'disabled'
        self.assertTrue(any('DPMS-off' in s for s in self.plan('work-igpu')['blockers']))

    def test_work_igpu_only_amd_in_compositor(self):
        self.hardware['connectors'][0]['status'] = 'disconnected'
        plan = self.plan('work-igpu')
        self.assertEqual(plan['blockers'], [])
        self.assertEqual(plan['session_environment_candidate']['KWIN_DRM_DEVICES'], '/dev/dri/card4')
        self.assertEqual(plan['compositor_environment_candidate'], {'CARDWIRE_ALLOW': '0'})
        self.assertFalse(any('plasma-kwin_wayland.service' in e.get('units', []) for e in plan['exceptions']))

    def test_displaylink_not_assumed_to_be_igpu(self):
        self.hardware['connectors'] = [{'name': 'card9-DVI-I-1', 'gpu': 'other', 'status': 'connected'}]
        blockers = self.plan('work-igpu')['blockers']
        self.assertTrue(any('No connected AMD' in s for s in blockers))
        self.assertTrue(any('unclassified' in s for s in blockers))

    def test_absent_egpu_does_not_stage_driver(self):
        self.hardware['gpus']['nvidia'] = None
        plan = self.plan()
        self.assertTrue(any('preserve existing AMD fallback' in s for s in plan['blockers']))
        self.assertNotIn('KWIN_DRM_DEVICES', plan['session_environment_candidate'])

    def test_unbound_gpu_is_not_ready(self):
        self.hardware['gpus']['nvidia']['driver'] = None
        self.assertTrue(self.plan()['blockers'])

    def test_smart_requires_expected_ids_and_default(self):
        for key, value in [('default', True), ('discrete', False), ('id', 3)]:
            saved = copy.deepcopy(self.cardwire)
            self.cardwire['gpus']['1'][key] = value
            self.assertTrue(any('two-GPU' in s for s in self.plan()['blockers']))
            self.cardwire = saved

    def test_reject_third_gpu(self):
        self.cardwire['gpus']['2'] = {'id': 2}
        self.assertTrue(any('two-GPU' in s for s in self.plan()['blockers']))

    def test_smart_unavailable(self):
        self.cardwire['available_modes'] = [1, 2]
        self.assertTrue(any('Smart mode' in s for s in self.plan()['blockers']))

    def test_failed_or_missing_runtime_grant_blocks_work(self):
        for probe in (None, {'status': 'failed'}, {'status': 'not-run'}):
            self.assertTrue(any('exception API' in s for s in self.plan(probe=probe)['blockers']))

    def test_unknown_app_policy_not_silently_replaced(self):
        self.cardwire['app_policies'] = [{'steam': ['Steam', [], [], 1]}]
        saved = copy.deepcopy(self.cardwire)
        self.assertTrue(any('per-app policies' in s for s in self.plan()['blockers']))
        self.assertEqual(saved, self.cardwire)

    def test_query_error_fails_closed(self):
        self.cardwire['errors'] = ['mode: access denied']
        self.assertIn('mode: access denied', self.plan()['blockers'])

    def test_report_never_claims_applied_even_with_all_prerequisites(self):
        for name in profiles.PROFILES:
            plan = self.plan(name)
            self.assertIsNone(plan['applied'])
            self.assertFalse(plan['activation_implemented'])
            self.assertFalse(plan['changed_host'])

    def test_compute_unit_scope(self):
        for name in ('*.service', 'user@1000.service', 'dbus.service', '../llama.service',
                     'plasma-kwin_wayland.service', 'llama\n.service', 'llama@model.service'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.plan(units=[name])
        self.assertEqual(profiles.compute_units(['llama.service', 'llama.service']), ['llama.service'])

    def test_no_inference_or_pci_side_effects_in_plan(self):
        plan = self.plan()
        self.assertIn('llama model/context/KV', plan['excluded_changes'])
        self.assertIn('PCI resources', plan['excluded_changes'])
        self.assertTrue(any('Do not stop inference' in s for s in plan['required_activation_gates']))

    def test_incomplete_optional_audit_blocks_work_not_gaming(self):
        scopes = {'status': 'incomplete', 'errors': ['compositor exe unknown']}
        self.assertIn('compositor exe unknown', self.plan(scopes=scopes)['blockers'])
        self.assertNotIn('compositor exe unknown', self.plan('gaming-nvidia', scopes=scopes)['blockers'])

    def test_compositor_exception_is_not_a_blanket_unit_grant(self):
        exception = self.plan()['exceptions'][-1]
        self.assertEqual(exception['executables'], ['/usr/bin/kwin_wayland', '/usr/bin/Xwayland'])
        self.assertTrue(exception['inheritance_requires_review'])
        self.assertEqual(exception['process_policy_candidate'], 'Allow_dGPU_Exact')
        self.assertFalse(exception['grant_children'])

    def test_work_display_and_driver_exceptions_cannot_use_inherited_allow(self):
        for name in ('work-nvidia', 'work-igpu'):
            plan = self.plan(name)
            self.assertEqual(plan['compositor_environment_candidate'], {'CARDWIRE_ALLOW': '0'})
            scoped = [e for e in plan['exceptions'] if 'process_policy_candidate' in e]
            self.assertTrue(scoped)
            for exception in scoped:
                self.assertEqual(exception['process_policy_candidate'], 'Allow_dGPU_Exact')
                self.assertFalse(exception['grant_children'])
            self.assertTrue(any('installed Allow_dGPU_Exact enforcement' in gate
                                for gate in plan['required_activation_gates']))
            self.assertIsNone(plan['applied'])

    def test_gaming_keeps_legacy_compositor_candidate_without_exact_requirement(self):
        plan = self.plan('gaming-nvidia')
        self.assertEqual(plan['compositor_environment_candidate'], {'CARDWIRE_ALLOW': '1'})
        self.assertTrue(all(e.get('process_policy_candidate') is None for e in plan['exceptions']))


    def test_read_hardware_config_not_shell(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / 'hardware.conf'
            config.write_text('\n'.join(f'{k}="{v}"' for k, v in self.config.items()))
            self.assertEqual(profiles.read_hardware_config(config), self.config)
            with config.open('a') as stream:
                stream.write('\nDESKTOP_USER="$(touch /tmp/not-executed)"\n')
            with self.assertRaises(ValueError):
                profiles.read_hardware_config(config)

    def test_missing_identity_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / 'hardware.conf'
            config.write_text('DESKTOP_USER="tester"\n')
            with self.assertRaisesRegex(ValueError, 'Missing'):
                profiles.read_hardware_config(config)

    def test_shell_expansion_is_rejected_without_evaluation(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / 'hardware.conf'
            for raw in ('"$(id)"', '"`id`"', '"${USER}"'):
                config.write_text('DESKTOP_USER=' + raw + '\n')
                with self.subTest(raw=raw), self.assertRaisesRegex(ValueError, 'Unsafe'):
                    profiles.read_hardware_config(config)

    def test_root_desktop_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / 'hardware.conf'
            values = dict(self.config, DESKTOP_UID='0')
            config.write_text('\n'.join(f'{k}="{v}"' for k, v in values.items()))
            with self.assertRaisesRegex(ValueError, 'non-root'):
                profiles.read_hardware_config(config)

    def test_unknown_profile_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'Unknown'):
            self.plan('work')

    def test_available_modes_is_a_method_not_property(self):
        calls = []
        def fake(args):
            calls.append(args)
            if args[:2] == ['cardwire', '--version']:
                return 'cardwire-cli 0.12.3'
            if args[:2] == ['cardwire', 'list']:
                return json.dumps(self.cardwire['gpus'])
            data = [[0, 1, 3]] if args[-1] == 'AvailableModes' else [{}] if args[-1] == 'GetAppPolicies' else False
            return json.dumps({'data': data})
        result = profiles.inspect_cardwire(fake)
        self.assertEqual(result['available_modes'], [0, 1, 3])
        self.assertEqual(result['errors'], [])
        self.assertTrue(any('call' in c and c[-1] == 'AvailableModes' for c in calls))
        self.assertFalse(any('set-property' in c for c in calls))

    def test_dbus_failure_preserves_error(self):
        def fail(args):
            raise RuntimeError('missing service')
        result = profiles.inspect_cardwire(fail)
        self.assertEqual(len(result['errors']), 8)


class SyntheticSysfs(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.pci = self.root / 'bus/pci/devices'
        self.drm = self.root / 'class/drm'
        self.pci.mkdir(parents=True)
        self.drm.mkdir(parents=True)
        self.config = {'EGPU_VENDOR': '0x10de', 'EGPU_DEVICE': '0x2c05',
                       'IGPU_VENDOR': '0x1002', 'IGPU_DEVICE': '0x150e'}
        self.device('0000:09:00.0', 'nvidia', 'card7', '0x10de', '0x2c05')
        self.device('0000:61:00.0', 'amdgpu', 'card4', '0x1002', '0x150e')

    def device(self, bdf, driver, card, vendor, device):
        path = self.pci / bdf
        (path / 'drm' / card).mkdir(parents=True)
        (path / 'vendor').write_text(vendor)
        (path / 'device').write_text(device)
        (path / 'driver').symlink_to(self.root / 'drivers' / driver)
        connector = self.drm / (card + '-DP-99')
        connector.mkdir(exist_ok=True)
        (connector / 'status').write_text('connected')
        (connector / 'enabled').write_text('disabled')
        (connector / 'edid').write_bytes(b'test-edid')

    def test_renumbered_cards_and_dp_nodes_are_resolved(self):
        found = profiles.inspect_hardware(self.config, self.root)
        self.assertEqual(found['gpus']['nvidia']['card'], 'card7')
        self.assertEqual(found['gpus']['igpu']['driver'], 'amdgpu')
        self.assertEqual(found['errors'], [])
        self.assertEqual(len(found['connectors']), 2)
        self.assertEqual(len(found['connectors'][0]['edid_sha256']), 64)

    def test_duplicate_nvidia_not_guessed(self):
        self.device('0000:0a:00.0', 'nvidia', 'card8', '0x10de', '0x2c05')
        found = profiles.inspect_hardware(self.config, self.root)
        self.assertIsNone(found['gpus']['nvidia'])
        self.assertTrue(any('Ambiguous' in e for e in found['errors']))

    def test_ambiguous_drm_node_not_guessed(self):
        (self.pci / '0000:09:00.0/drm/card8').mkdir()
        found = profiles.inspect_hardware(self.config, self.root)
        self.assertIsNone(found['gpus']['nvidia']['card'])
        self.assertTrue(found['errors'])


class ServiceScopeAudit(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.proc, self.cgroups = root / 'proc', root / 'cgroup'
        self.proc.mkdir()
        self.cgroups.mkdir()
        self.config = {'DESKTOP_USER': 'tester', 'DESKTOP_UID': '1000'}
        self.states, self.calls = {}, []
        for n, unit in enumerate(('cardwired.service', 'systemd-logind.service', 'nvidia-persistenced.service')):
            group = '/system.slice/' + unit
            self.add_unit(unit, group, 100 + n)
            self.add_process(100 + n, 1, 0, '/usr/bin/' + unit[:-8], group)
        self.kwin = '/user.slice/user-1000.slice/user@1000.service/session.slice/plasma-kwin_wayland.service'
        self.llama = '/user.slice/user-1000.slice/user@1000.service/app.slice/llama.service'
        self.add_unit('plasma-kwin_wayland.service', self.kwin, 200)
        self.add_process(200, 50, 1000, '/usr/bin/kwin_wayland_wrapper', self.kwin)
        self.add_process(201, 200, 1000, '/usr/bin/kwin_wayland', self.kwin)
        self.add_process(202, 201, 1000, '/usr/bin/Xwayland', self.kwin)
        self.add_process(203, 201, 1000, '/usr/bin/plasma-keyboard-custom', self.kwin)
        self.add_unit('llama.service', self.llama, 300)
        self.add_process(300, 50, 1000, '/home/tester/.local/bin/llama', self.llama)
        self.add_process(301, 300, 1000, '/home/tester/.local/bin/llama', self.llama + '/workers')

    def add_unit(self, name, group, pid):
        self.states[name] = {'Id': name, 'LoadState': 'loaded', 'ActiveState': 'active',
                             'MainPID': str(pid), 'ControlGroup': group, 'InvocationID': 'same'}

    def add_process(self, pid, ppid, uid, exe, group, comm=None):
        path = self.proc / str(pid)
        path.mkdir()
        comm = comm or Path(exe).name
        (path / 'stat').write_text(f'{pid} ({comm}) S {ppid} ' + '0 ' * 17 + '12345 0\n')
        (path / 'status').write_text(f'Uid:\t{uid}\t{uid}\t{uid}\t{uid}\n')
        (path / 'cgroup').write_text(f'0::{group}\n')
        (path / 'exe').symlink_to(exe)
        directory = self.cgroups / group.lstrip('/')
        directory.mkdir(parents=True, exist_ok=True)
        with (directory / 'cgroup.procs').open('a') as stream:
            stream.write(f'{pid}\n')

    def runner(self, args):
        self.calls.append(args)
        unit = args[args.index('show') + 1]
        return '\n'.join(f'{key}={value}' for key, value in self.states[unit].items())

    def audit(self, **kwargs):
        return profiles.inspect_service_scopes(self.config, ['llama.service'], kwargs.get('runner', self.runner),
                                                self.proc, self.cgroups, kwargs.get('euid', 1000))

    def test_exact_members_not_just_main_pid(self):
        report = self.audit()
        self.assertEqual(report['errors'], [])
        kwin = report['units'][3]['processes']
        self.assertEqual(kwin[0]['role_hint'], 'launcher-not-a-compositor-grant')
        self.assertEqual(kwin[1]['role_hint'], 'compositor')
        self.assertEqual(kwin[1]['observed_direct_children_in_unit'], [202, 203])
        self.assertEqual(kwin[3]['role_hint'], 'unknown-display-member-needs-review')
        self.assertIn('outside the unit', ' '.join(report['limits']))
        self.assertEqual([p['pid'] for p in report['units'][4]['processes']], [300, 301])

    def test_no_commands_change_service_or_policy(self):
        report = self.audit()
        self.assertFalse(report['changed_host'])
        self.assertTrue(all(call[0] == 'systemctl' and 'show' in call for call in self.calls))
        self.assertEqual(len(self.calls), 10)
        self.assertTrue(all('--user' in c for c in self.calls[6:]))

    def test_unknown_executable_not_inferred_from_comm(self):
        original = profiles.os.readlink
        def denied(path):
            if path == self.proc / '201/exe':
                raise PermissionError(13, 'Permission denied')
            return original(path)
        with patch.object(profiles.os, 'readlink', side_effect=denied):
            report = self.audit()
        item = report['units'][3]['processes'][1]
        self.assertEqual(item['comm'], 'kwin_wayland')
        self.assertIsNone(item['executable'])
        self.assertEqual(item['role_hint'], 'unknown-display-member-needs-review')
        self.assertEqual(report['status'], 'incomplete')

    def test_parent_override_limitation_is_explicit(self):
        self.assertIn("A child's Force_GPU=0 does not override an allowed parent in Smart", self.audit()['limits'])

    def test_process_stat_with_spaces_and_parentheses(self):
        self.add_process(400, 200, 1000, '/tmp/fake', self.kwin, 'name ) with ( spaces')
        item = profiles.process_identity(400, self.proc)
        self.assertEqual((item['ppid'], item['start_ticks'], item['comm']), (200, 12345, 'name ) with ( spaces'))

    def test_pid_reuse_is_reported(self):
        original = Path.read_text
        reads = 0
        def changed(path, *args, **kwargs):
            nonlocal reads
            result = original(path, *args, **kwargs)
            if path == self.proc / '201/stat':
                reads += 1
                if reads == 2:
                    return result.replace('12345', '99999')
            return result
        with patch.object(Path, 'read_text', changed):
            report = self.audit()
        self.assertTrue(any('identity changed' in e for e in report['errors']))

    def test_unit_restart_is_reported(self):
        def changed(args):
            result = self.runner(args)
            if len(self.calls) == 2:
                return result.replace('InvocationID=same', 'InvocationID=new')
            return result
        self.assertTrue(any('Service state changed' in e for e in self.audit(runner=changed)['errors']))

    def test_exec_with_unchanged_pid_start_is_reported(self):
        original = profiles.os.readlink
        reads = 0
        def changed(path):
            nonlocal reads
            result = original(path)
            if path == self.proc / '201/exe':
                reads += 1
                if reads == 2:
                    return '/usr/bin/unreviewed-helper'
            return result
        with patch.object(profiles.os, 'readlink', side_effect=changed):
            self.assertTrue(any('executable changed' in e for e in self.audit()['errors']))

    def test_uid_change_with_unchanged_pid_start_is_reported(self):
        original = Path.read_text
        reads = 0
        def changed(path, *args, **kwargs):
            nonlocal reads
            result = original(path, *args, **kwargs)
            if path == self.proc / '201/status':
                reads += 1
                if reads == 2:
                    return 'Uid:\t0\t0\t0\t0\n'
            return result
        with patch.object(Path, 'read_text', changed):
            self.assertTrue(any('credentials or cgroup changed' in e for e in self.audit()['errors']))

    def test_cgroup_move_with_unchanged_pid_start_is_reported(self):
        original = Path.read_text
        reads = 0
        def changed(path, *args, **kwargs):
            nonlocal reads
            result = original(path, *args, **kwargs)
            if path == self.proc / '201/cgroup':
                reads += 1
                if reads == 2:
                    return '0::/user.slice/other.service\n'
            return result
        with patch.object(Path, 'read_text', changed):
            self.assertTrue(any('credentials or cgroup changed' in e for e in self.audit()['errors']))

    def test_executable_disappearing_during_inspection_is_not_accepted(self):
        original = profiles.os.readlink
        reads = 0
        def changed(path):
            nonlocal reads
            if path == self.proc / '201/exe':
                reads += 1
                if reads == 2:
                    raise FileNotFoundError(2, 'No such file or directory')
            return original(path)
        with patch.object(profiles.os, 'readlink', side_effect=changed):
            report = self.audit()
        self.assertEqual(report['status'], 'incomplete')
        self.assertTrue(any('PID 201' in e for e in report['errors']))

    def test_cgroup_escape_or_broad_user_manager_rejected(self):
        for group in ('/', '/system.slice', '/system.slice/../cardwired.service', '/system.slice//cardwired.service'):
            self.states['cardwired.service']['ControlGroup'] = group
            self.assertTrue(any('over-broad' in e for e in self.audit()['errors']))
        self.states['plasma-kwin_wayland.service']['ControlGroup'] = '/user.slice/user-1000.slice/user@1000.service'
        self.assertTrue(any('user:plasma-' in e and 'over-broad' in e for e in self.audit()['errors']))

    def test_process_that_left_scope_is_reported(self):
        (self.proc / '201/cgroup').write_text('0::/user.slice/elsewhere\n')
        self.assertTrue(any('left the unit' in e for e in self.audit()['errors']))

    def test_mismatched_uid_is_reported(self):
        (self.proc / '201/status').write_text('Uid:\t0\t0\t0\t0\n')
        self.assertTrue(any('effective UID' in e for e in self.audit()['errors']))

    def test_main_pid_not_in_cgroup_is_reported(self):
        self.states['llama.service']['MainPID'] = '900'
        self.assertTrue(any('MainPID is absent' in e for e in self.audit()['errors']))

    def test_exited_process_is_not_silently_dropped(self):
        (self.proc / '201/stat').unlink()
        self.assertTrue(any('PID 201' in e for e in self.audit()['errors']))

    def test_inactive_unit_does_not_appear_verified(self):
        self.states['llama.service']['ActiveState'] = 'inactive'
        self.assertTrue(any('not active' in e for e in self.audit()['errors']))

    def test_root_queries_named_users_bus_without_restarts(self):
        report = self.audit(euid=0)
        self.assertEqual(report['errors'], [])
        call = self.calls[6]
        self.assertEqual(call[:4], ['runuser', '-u', 'tester', '--'])
        self.assertIn('DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus', call)
        self.assertIn('show', call)

    def test_other_user_manager_is_not_used(self):
        report = self.audit(euid=2000)
        self.assertEqual(report['status'], 'incomplete')
        self.assertEqual(len(self.calls), 6)
        self.assertTrue(any('configured desktop user' in e for e in report['errors']))


class ProcessProbe(unittest.TestCase):
    def test_failed_grant_reaps_only_own_child(self):
        with patch.object(profiles.subprocess, 'Popen') as popen:
            child = popen.return_value.__enter__.return_value
            child.pid = 12345
            child.poll.return_value = None
            calls = []
            def fail(args):
                calls.append(args)
                raise RuntimeError('bpf_map_delete_elem failed')
            result = profiles.probe_process_access(fail, euid=0)
            self.assertEqual(result['status'], 'failed')
            self.assertEqual(calls[0][-3:], ['12345', 'Allow_dGPU', '1'])
            child.terminate.assert_called_once()
            child.wait.assert_called_once()

    def test_success_requires_readback(self):
        with patch.object(profiles.subprocess, 'Popen') as popen:
            child = popen.return_value.__enter__.return_value
            child.pid = 12345
            child.poll.return_value = None
            def fake(args):
                return json.dumps({'data': ['', []]}) if 'GetProcessStatus' in args else ''
            self.assertEqual(profiles.probe_process_access(fake, euid=0)['status'], 'failed')
            child.terminate.assert_called_once()

    def test_success_is_scoped_not_a_hardware_rendering_claim(self):
        with patch.object(profiles.subprocess, 'Popen') as popen:
            child = popen.return_value.__enter__.return_value
            child.pid = 12345
            child.poll.return_value = None
            def fake(args):
                return json.dumps({'data': ['Allowed', [0]]}) if 'GetProcessStatus' in args else ''
            result = profiles.probe_process_access(fake, euid=0)
            self.assertEqual(result['status'], 'passed')
            self.assertIn('no routing test', result['scope'])
            child.terminate.assert_called_once()

    def test_unprivileged_probe_never_spawns_or_attempts_a_grant(self):
        with patch.object(profiles.os, 'geteuid', return_value=1000), \
                patch.object(profiles.subprocess, 'Popen') as popen, \
                patch.object(profiles, 'command') as command:
            result = profiles.probe_process_access(command)
        self.assertEqual(result['status'], 'requires-root')
        self.assertFalse(result['changed_host'])
        command.assert_not_called()
        popen.assert_not_called()


if __name__ == '__main__':
    unittest.main()
