"""No sleep, mount, NVIDIA ioctl, real sysfs or privileged command in these tests."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location('vram', Path(__file__).resolve().parents[1] / 'diagnostics/egpu_vram_backing.py')
vram = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vram)


class BudgetTests(unittest.TestCase):
    def test_tested_idle_desktop(self):
        result = vram.check_budget('615.71.09, 3272, 16303', 'MemAvailable: 10485760 kB\n')
        self.assertEqual(result['tmpfs_limit_bytes'], 6 * vram.GIB)

    def test_rejects_bad_or_busy_gpu(self):
        for gpu in ('615.71.09, 4097, 16303', '615.71.10, 1000, 16303',
                    '615.71.09, N/A, 16303', '615.71.09, 1000, 32768', '',
                    '615.71.09, 2, 16303\n615.71.09, 2, 16303'):
            with self.subTest(gpu=gpu), self.assertRaises(RuntimeError):
                vram.check_budget(gpu, 'MemAvailable: 20000000 kB\n')

    def test_swap_or_free_does_not_substitute_for_available(self):
        for mem in ('MemAvailable: 10485759 kB\n', 'SwapFree: 50000000 kB\n',
                    'MemFree: 50000000 kB\n', ''):
            with self.subTest(mem=mem), self.assertRaises(RuntimeError):
                vram.check_budget('615.71.09, 1000, 16303', mem)


class PmTransportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.active = root / 'active'
        self.boot = root / 'boot'
        self.hook = root / 'hook.py'
        self.boot.write_text('test-boot\n')
        self.hook.write_text('paired hook')
        for name, path in (('PROCFS_PM_ACTIVE', self.active), ('BOOT_ID', self.boot),
                           ('PROCFS_PM_HOOK', self.hook)):
            item = patch.object(vram, name, path)
            item.start()
            self.addCleanup(item.stop)
        owner = patch.object(vram, 'PROCFS_PM_MARKER_OWNER', os.getuid())
        owner.start()
        self.addCleanup(owner.stop)

    def test_notifier_mode_and_disabled_without_marker(self):
        self.assertEqual(vram.check_pm_transport('UseKernelSuspendNotifiers: 1\n'),
                         'kernel-notifier')
        with self.assertRaisesRegex(RuntimeError, 'without a paired'):
            vram.check_pm_transport('UseKernelSuspendNotifiers: 0\n')

    def test_paired_procfs_mode_requires_hooks(self):
        self.active.write_text('test-boot\n')
        self.active.chmod(0o600)
        def show(argv, timeout=10):
            if 'ExecStartPre' in argv:
                return (f'argv[]=/usr/bin/bash {vram.SLEEP_GUARD} systemd-suspend.service ; ignore_errors=no ; '
                        f'argv[]=/usr/bin/python3 {self.hook} pre ; ignore_errors=no')
            return f'argv[]=/usr/bin/python3 {self.hook} post ; ignore_errors=no'
        with patch.object(vram, 'command', side_effect=show):
            self.assertEqual(vram.check_pm_transport('UseKernelSuspendNotifiers: 0\n'),
                             'paired-procfs')
        with patch.object(vram, 'command', return_value='no hook'):
            with self.assertRaisesRegex(RuntimeError, 'misordered'):
                vram.check_pm_transport('UseKernelSuspendNotifiers: 0\n')

    def test_stale_marker_and_duplicate_notifier_are_rejected(self):
        self.active.write_text('old-boot\n')
        self.active.chmod(0o600)
        with self.assertRaisesRegex(RuntimeError, 'stale or unsafe'):
            vram.check_pm_transport('UseKernelSuspendNotifiers: 0\n')
        with self.assertRaisesRegex(RuntimeError, 'ambiguous'):
            vram.check_pm_transport('UseKernelSuspendNotifiers: 0\nUseKernelSuspendNotifiers: 1\n')


class MountTests(unittest.TestCase):
    LINE = '100 80 0:52 / /var/tmp rw,nosuid,nodev - tmpfs tmpfs rw,size=6291456k,noswap\n'

    def check(self, mountinfo=None, host_dev=1, self_dev=2, namespace='mnt:[2]',
              capacity=6 * vram.GIB, available=6 * vram.GIB, shmem='always [never] force'):
        def read(path):
            if str(path).endswith('mountinfo'):
                return self.LINE if mountinfo is None else mountinfo
            return shmem

        with patch.object(vram.os, 'readlink', side_effect=[namespace, 'mnt:[1]']), \
             patch.object(vram.os, 'stat', side_effect=[SimpleNamespace(st_dev=self_dev), SimpleNamespace(st_dev=host_dev)]), \
             patch.object(vram.os, 'statvfs', return_value=SimpleNamespace(f_blocks=capacity // 4096,
                                      f_bavail=available // 4096, f_frsize=4096)), \
             patch.object(Path, 'read_text', read):
            return vram.check_private_mount()

    def test_exact_mount_and_implicit_huge_never(self):
        self.assertTrue(self.check()['private_mount'])
        self.assertTrue(self.check(self.LINE.replace('noswap', 'noswap,huge=never'))['private_mount'])

    def test_rejects_host_namespace_or_filesystem(self):
        for changes in ({'namespace': 'mnt:[1]'}, {'self_dev': 1},
                        {'mountinfo': self.LINE.replace('tmpfs tmpfs', 'btrfs /dev/test')}):
            with self.subTest(changes=changes), self.assertRaises(RuntimeError):
                self.check(**changes)

    def test_rejects_unsafe_options_capacity_and_force_override(self):
        for changes in ({'mountinfo': self.LINE.replace(',noswap', '')},
                        {'mountinfo': self.LINE.replace('noswap', 'noswap,huge=always')},
                        {'capacity': 7 * vram.GIB}, {'available': 4 * vram.GIB},
                        {'shmem': 'never [force]'}, {'mountinfo': self.LINE * 2}):
            with self.subTest(changes=changes), self.assertRaises(RuntimeError):
                self.check(**changes)


class MarkerTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.marker = self.root / 'once'
        self.platform = self.root / 'platform'
        self.boot = self.root / 'boot'
        self.boot.write_text('test-boot')
        self.pm = self.root / 'pm'
        self.pm.write_text('none platform [freezer]')
        self.pm_async = self.root / 'async'
        self.pm_async.write_text('1')
        self.pm_trace = self.root / 'trace'
        self.pm_trace.write_text('0')
        for name, path in (('MARKER', self.marker), ('PLATFORM_MARKER', self.platform),
                           ('BOOT_ID', self.boot), ('PM_TEST', self.pm),
                           ('PM_ASYNC', self.pm_async), ('PM_TRACE', self.pm_trace)):
            self.stack.enter_context(patch.object(vram, name, path))
        self.host = self.stack.enter_context(patch.object(vram, 'check_host', return_value={}))
        self.private = self.stack.enter_context(patch.object(vram, 'check_private_mount', return_value={}))
        self.current = self.stack.enter_context(patch.object(vram, 'check_current_mount', return_value={}))
        self.stack.enter_context(patch.object(vram, 'command', return_value=''))
        self.stack.enter_context(patch.object(vram.time, 'monotonic', return_value=100))
        self.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        original = os.fstat
        # Fake root ownership for the test's temporary marker, not real files.
        self.stack.enter_context(patch.object(vram.os, 'fstat', side_effect=lambda fd:
            SimpleNamespace(st_mode=original(fd).st_mode, st_uid=0)))

    def test_arm_and_consume_once_for_both_modes(self):
        for mode in ('vram-current', 'vram-tmpfs'):
            with self.subTest(mode=mode):
                vram.arm(self.root, mode)
                self.assertEqual(self.marker.stat().st_mode & 0o777, 0o600)
                vram.consume()
                self.assertFalse(self.marker.exists())
                with self.assertRaises(FileNotFoundError):
                    vram.consume()
        self.private.assert_called_once()
        self.current.assert_called_once()

    def test_serial_policy_checks_are_readonly_and_require_trace_off(self):
        self.assertEqual(vram.check_device_policy(vram.SERIAL_MODE, preparing=True),
                         {'pm_async': '1', 'pm_trace': '0'})
        with self.assertRaises(RuntimeError):
            vram.check_device_policy(vram.SERIAL_MODE)
        self.assertEqual(self.pm_async.read_text(), '1')
        self.pm_async.write_text('0')
        self.assertEqual(vram.check_device_policy(vram.SERIAL_MODE),
                         {'pm_async': '0', 'pm_trace': '0'})
        with self.assertRaises(RuntimeError):
            vram.check_device_policy(vram.SERIAL_MODE, preparing=True)
        self.pm_trace.write_text('1')
        with self.assertRaises(RuntimeError):
            vram.check_device_policy(vram.SERIAL_MODE)
        self.assertEqual(self.pm_trace.read_text(), '1')
        self.assertFalse(self.marker.exists())

    def test_rtc_policy_requires_exact_serial_and_trace_values(self):
        self.assertEqual(vram.check_device_policy(vram.RTC_MODE, preparing=True),
                         {'pm_async': '1', 'pm_trace': '0'})
        self.pm_async.write_text('0')
        self.pm_trace.write_text('1')
        self.assertEqual(vram.check_device_policy(vram.RTC_MODE),
                         {'pm_async': '0', 'pm_trace': '1'})
        self.pm_trace.write_text('0')
        with self.assertRaises(RuntimeError):
            vram.check_device_policy(vram.RTC_MODE)

    def test_rtc_marker_is_single_use_and_platform_only(self):
        self.pm.write_text('none freezer devices [platform]')
        self.pm_async.write_text('0')
        self.pm_trace.write_text('1')
        vram.arm(self.root, vram.RTC_MODE, stage='platform')
        self.assertEqual(json.loads(self.marker.read_text())['mode'], vram.RTC_MODE)
        vram.consume()
        self.assertFalse(self.marker.exists())
        self.private.assert_called_once()
        self.pm.write_text('none freezer [devices] platform')
        with self.assertRaises(RuntimeError):
            vram.arm(self.root, vram.RTC_MODE, stage='devices')

    def test_serial_platform_arm_consume_once_in_private_mount(self):
        self.pm.write_text('none freezer devices [platform]')
        self.pm_async.write_text('0')
        vram.arm(self.root, vram.SERIAL_MODE, stage='platform')
        self.assertEqual(json.loads(self.marker.read_text())['mode'], vram.SERIAL_MODE)
        vram.consume()
        self.assertFalse(self.marker.exists())
        self.private.assert_called_once()
        self.current.assert_not_called()
        with self.assertRaises(FileNotFoundError):
            vram.consume()

    def test_serial_rejects_wrong_stage_before_arming(self):
        self.pm_async.write_text('0')
        for stage in ('freezer', 'devices', 'none'):
            with self.subTest(stage=stage), self.assertRaises(RuntimeError):
                self.pm.write_text(f'[{stage}] platform')
                vram.arm(self.root, vram.SERIAL_MODE, stage=stage)
            self.assertFalse(self.marker.exists())
        self.host.assert_not_called()

    def test_serial_rejects_wrong_policy_before_arming(self):
        self.pm.write_text('none freezer devices [platform]')
        for async_value, trace_value in (('1', '0'), ('0', '1'), ('bad', '0')):
            with self.subTest(async_value=async_value, trace_value=trace_value):
                self.pm_async.write_text(async_value)
                self.pm_trace.write_text(trace_value)
                with self.assertRaises(RuntimeError):
                    vram.arm(self.root, vram.SERIAL_MODE, stage='platform')
                self.assertFalse(self.marker.exists())
        self.host.assert_not_called()

    def test_serial_changed_policy_consumes_permission_but_fails_closed(self):
        self.pm.write_text('none freezer devices [platform]')
        for async_value, trace_value in (('1', '0'), ('0', '1')):
            with self.subTest(async_value=async_value, trace_value=trace_value):
                self.pm_async.write_text('0')
                self.pm_trace.write_text('0')
                vram.arm(self.root, vram.SERIAL_MODE, stage='platform')
                self.host.reset_mock()
                self.pm_async.write_text(async_value)
                self.pm_trace.write_text(trace_value)
                with self.assertRaises(RuntimeError):
                    vram.consume()
                self.assertFalse(self.marker.exists())
                self.host.assert_not_called()
        self.private.assert_not_called()

    def test_serial_marker_cannot_be_retargeted_to_shallower_stage(self):
        self.pm.write_text('none freezer devices [platform]')
        self.pm_async.write_text('0')
        vram.arm(self.root, vram.SERIAL_MODE, stage='platform')
        payload = json.loads(self.marker.read_text())
        payload['stage'] = 'devices'
        self.marker.write_text(json.dumps(payload))
        self.pm.write_text('none freezer [devices] platform')
        with self.assertRaises(RuntimeError):
            vram.consume()
        self.assertFalse(self.marker.exists())
        self.private.assert_not_called()

    def test_devices_requires_an_exact_devices_marker_and_active_stage(self):
        self.pm.write_text('none freezer [devices] platform')
        vram.arm(self.root, 'vram-tmpfs', stage='devices')
        self.assertEqual(json.loads(self.marker.read_text())['stage'], 'devices')
        vram.consume()
        self.private.assert_called_once()
        self.assertFalse(self.marker.exists())

    def test_platform_requires_an_exact_platform_marker_and_active_stage(self):
        self.pm.write_text('none freezer devices [platform]')
        vram.arm(self.root, 'vram-tmpfs', stage='platform')
        self.assertEqual(json.loads(self.marker.read_text())['stage'], 'platform')
        vram.consume()
        self.private.assert_called_once()
        self.assertFalse(self.marker.exists())
        with self.assertRaises(FileNotFoundError):
            vram.consume()

    def test_stage_exception_cannot_escalate_or_change_to_another_stage(self):
        for initial, changed in ((a, b) for a in vram.STAGES
                                 for b in (*vram.STAGES, 'none') if a != b):
            with self.subTest(initial=initial, changed=changed):
                self.pm.write_text(f'none [{initial}]')
                vram.arm(self.root, 'vram-tmpfs', stage=initial)
                self.pm.write_text(f'none [{changed}]')
                with self.assertRaises(RuntimeError):
                    vram.consume()
                self.assertFalse(self.marker.exists())
        self.private.assert_not_called()

    def test_backing_mode_never_authorizes_real_sleep_or_unsupported_stages(self):
        for stage in ('none', 'processors', 'core', 'unknown'):
            with self.subTest(stage=stage):
                self.pm.write_text(f'[{stage}] freezer devices')
                with self.assertRaises(RuntimeError):
                    vram.arm(self.root, 'vram-tmpfs', stage=stage)
                self.assertFalse(self.marker.exists())

    def test_malformed_multi_selected_stage_is_rejected(self):
        self.pm.write_text('[freezer] [devices]')
        with self.assertRaises(RuntimeError):
            vram.arm(self.root, 'vram-tmpfs', stage='devices')
        self.assertFalse(self.marker.exists())

    def test_invalid_stage_never_arms(self):
        self.pm.write_text('none [platform] freezer')
        with self.assertRaises(RuntimeError):
            vram.arm(self.root, 'vram-tmpfs')
        self.assertFalse(self.marker.exists())

    def test_mixed_markers_never_arm(self):
        self.platform.write_text('old')
        with self.assertRaises(RuntimeError):
            vram.arm(self.root, 'vram-tmpfs')
        self.assertFalse(self.marker.exists())

    def test_platform_backing_marker_cannot_mix_with_old_platform_exception(self):
        self.pm.write_text('none [platform] freezer devices')
        vram.arm(self.root, 'vram-tmpfs', stage='platform')
        self.platform.write_text('another experiment')
        with self.assertRaisesRegex(RuntimeError, 'Mixed'):
            vram.consume()
        self.assertFalse(self.marker.exists())
        self.private.assert_not_called()

    def test_does_not_overwrite_existing_marker_or_follow_symlink(self):
        target = self.root / 'unrelated'
        target.write_text('keep')
        self.marker.symlink_to(target)
        with self.assertRaises(OSError):
            vram.arm(self.root, 'vram-tmpfs')
        with self.assertRaises(OSError):
            vram.consume()
        self.assertEqual(target.read_text(), 'keep')

    def test_invalid_payload_is_consumed_but_never_allows_pm(self):
        for change in ({'boot_id': 'other'}, {'stage': 'platform'}, {'mode': 'unknown'},
                       {'expires': 100}, {'expires': 191}, {'expires': float('nan')},
                       {'expires': '120'}):
            with self.subTest(change=change):
                vram.arm(self.root, 'vram-tmpfs')
                payload = json.loads(self.marker.read_text())
                payload.update(change)
                self.marker.write_text(json.dumps(payload))
                with self.assertRaises(RuntimeError):
                    vram.consume()
                self.assertFalse(self.marker.exists())
        self.private.assert_not_called()

    def test_changed_stage_or_memory_fails_closed_after_consumption(self):
        vram.arm(self.root, 'vram-tmpfs')
        self.pm.write_text('[none] freezer')
        with self.assertRaises(RuntimeError):
            vram.consume()
        self.assertFalse(self.marker.exists())
        self.pm.write_text('none [freezer]')
        vram.arm(self.root, 'vram-tmpfs')
        self.host.side_effect = RuntimeError('budget changed')
        with self.assertRaises(RuntimeError):
            vram.consume()
        self.assertFalse(self.marker.exists())

    def test_unsafe_permissions_do_not_authorize(self):
        vram.arm(self.root, 'vram-current')
        self.marker.chmod(0o644)
        with self.assertRaises(RuntimeError):
            vram.consume()
        self.current.assert_not_called()


class PolicyAndProbeTests(unittest.TestCase):
    @staticmethod
    def service_query(*, environment='', private='no', conflict=None):
        def query(argv):
            key = argv[argv.index('-p') + 1]
            if key == conflict:
                return 'conflicting'
            if key == 'PrivateTmp':
                return private
            if key == 'Environment':
                return environment
            return ''
        return query

    def test_service_policy_accepts_new_systemd_default_freezer(self):
        policy = vram.check_service_policy(SimpleNamespace(
            require_command=self.service_query()))
        self.assertEqual(policy, 'systemd-default-freeze')

    def test_service_policy_accepts_legacy_explicit_no_freeze(self):
        policy = vram.check_service_policy(SimpleNamespace(require_command=self.service_query(
            environment='SYSTEMD_SLEEP_FREEZE_USER_SESSIONS=false')))
        self.assertEqual(policy, 'legacy-explicit-no-freeze')

    def test_service_policy_rejects_other_or_duplicate_freezer_values(self):
        for environment in (
                'SYSTEMD_SLEEP_FREEZE_USER_SESSIONS=true',
                'SYSTEMD_SLEEP_FREEZE_USER_SESSIONS=false SYSTEMD_SLEEP_FREEZE_USER_SESSIONS=false',
                'SYSTEMD_SLEEP_FREEZE_USER_SESSIONS=false SYSTEMD_SLEEP_FREEZE_USER_SESSIONS=true'):
            with self.subTest(environment=environment), self.assertRaises(RuntimeError):
                vram.check_service_policy(SimpleNamespace(
                    require_command=self.service_query(environment=environment)))

    def test_probe_has_no_pm_entrypoint_and_allocates_only_one_mib(self):
        data = bytes(range(256)) * 4096
        with patch.object(vram, 'check_private_mount', return_value={}), \
             patch.object(vram.os, 'open', return_value=17) as opened, \
             patch.object(vram.os, 'posix_fallocate') as allocated, \
             patch.object(vram.os, 'pwrite', return_value=len(data)), \
             patch.object(vram.os, 'pread', return_value=data), \
             patch.object(vram.os, 'fsync'), patch.object(vram.os, 'close') as closed, \
             patch.object(vram, 'command') as command, contextlib.redirect_stdout(io.StringIO()):
            vram.probe_private()
        self.assertTrue(opened.call_args.args[1] & os.O_TMPFILE)
        allocated.assert_called_once_with(17, 0, 1024 ** 2)
        closed.assert_called_once_with(17)
        command.assert_not_called()

    def test_probe_refuses_unvalidated_mount_before_writing(self):
        with patch.object(vram, 'check_private_mount', side_effect=RuntimeError('not private')), \
             patch.object(vram.os, 'open') as opened, self.assertRaises(RuntimeError):
            vram.probe_private()
        opened.assert_not_called()

    def test_conflicting_service_policy_is_not_overwritten(self):
        for prop in ('TemporaryFileSystem', 'BindPaths', 'BindReadOnlyPaths', 'RootDirectory', 'RootImage', 'PrivateTmp'):
            with self.subTest(prop=prop), self.assertRaises(RuntimeError):
                vram.check_service_policy(SimpleNamespace(
                    require_command=self.service_query(conflict=prop)))


if __name__ == '__main__':
    unittest.main()
