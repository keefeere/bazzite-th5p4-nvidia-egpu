"""Read-only guard tests using fake sysfs; never request system sleep."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]


class SleepGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.sysfs = Path(self.tmp.name)
        self.procfs = self.sysfs / 'proc'
        self.runtime = self.sysfs / 'run'
        self.pm_test = self.sysfs / 'pm_test'
        for bus in ('pci', 'thunderbolt', 'usb'):
            (self.sysfs / 'bus' / bus / 'devices').mkdir(parents=True)
        (self.sysfs / 'module' / 'thunderbolt' / 'parameters').mkdir(parents=True)
        (self.sysfs / 'module' / 'thunderbolt' / 'parameters' / 'host_reset').write_text('N\n')
        (self.procfs / 'sys' / 'kernel' / 'random').mkdir(parents=True)
        (self.procfs / 'sys' / 'kernel' / 'random' / 'boot_id').write_text('test-boot\n')
        (self.procfs / 'cmdline').write_text(
            'quiet thunderbolt.host_reset=0 egpu.host_reset_test=1 egpu.host_reset_nodock=1\n')
        self.runtime.mkdir()
        self.pm_test.write_text('none core processors [platform] devices freezer\n')

    def device(self, bus, name, **attributes):
        path = self.sysfs / 'bus' / bus / 'devices' / name
        path.mkdir()
        for key, value in attributes.items():
            (path / key).write_text(value + '\n')

    def check_guard(self, expected, unit='systemd-suspend.service'):
        result = subprocess.run(
            ['bash', str(REPO / 'egpu-sleep-guard.sh'), *([unit] if unit else [])],
            env={**os.environ, 'EGPU_SLEEP_SYSFS': str(self.sysfs),
                 'EGPU_SLEEP_PROCFS': str(self.procfs),
                 'EGPU_SLEEP_RUNTIME': str(self.runtime),
                 'EGPU_SLEEP_PM_TEST': str(self.pm_test),
                 'EGPU_SLEEP_MARKER_OWNER': str(os.getuid()),
                 'EGPU_CONFIG_FILE': str(REPO / 'hardware.conf')},
            capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        return result

    def test_absent(self):
        self.check_guard(0)

    def test_freezer_marker_does_not_authorize_other_sleep_or_readonly_checks(self):
        self.device('pci', '0000:03:00.0', vendor='0x10de', device='0x2c05')
        marker = self.runtime / 'egpu-sleep-guard-freezer-once'
        marker.write_text('not interpreted by these operations')
        for unit in ('systemd-hibernate.service', 'systemd-hybrid-sleep.service',
                     'systemd-suspend-then-hibernate.service', None):
            with self.subTest(unit=unit):
                self.check_guard(1, unit)
                self.assertTrue(marker.exists())

    def test_freezer_marker_has_no_fake_sysfs_success_path(self):
        marker = self.runtime / 'egpu-sleep-guard-freezer-once'
        marker.write_text('any content')
        self.check_guard(2)

    def test_hp_dock_and_igpu_only(self):
        self.device('pci', '0000:64:00.0', vendor='0x1002', device='0x1586')
        self.device('pci', '0000:22:00.0', vendor='0x8086', device='0x0b26')
        self.device('thunderbolt', '0-2', vendor='0xf0', device='0x488')
        self.check_guard(0)

    def test_gpu_without_driver(self):
        self.device('pci', '0000:03:00.0', vendor='0x10de', device='0x2c05')
        self.check_guard(1)

    def test_bridge_without_gpu(self):
        self.device('pci', '0000:01:00.0', vendor='0x8086', device='0x5786')
        self.check_guard(1)

    def test_router_after_logical_detach(self):
        self.device('thunderbolt', '0-2', vendor='0x8087', device='0x1234')
        self.check_guard(1)

    def test_diagnostic_only(self):
        self.device('usb', '1-3', idVendor='8086', idProduct='1234')
        self.check_guard(1)

    def test_missing_sysfs_fails_closed(self):
        (self.sysfs / 'bus' / 'pci' / 'devices').rmdir()
        self.check_guard(2)

    def test_exact_platform_marker_allows_only_one_invocation(self):
        self.device('pci', '0000:03:00.0', vendor='0x10de', device='0x2c05')
        marker = self.runtime / 'egpu-sleep-guard-platform-once'
        marker.write_text('test-boot\nplatform\n')
        marker.chmod(0o600)
        result = self.check_guard(0)
        self.assertIn('consumed', result.stdout)
        self.assertFalse(marker.exists())
        self.check_guard(1)

    def test_platform_marker_fails_closed_on_wrong_kernel_argument(self):
        self.device('pci', '0000:03:00.0', vendor='0x10de', device='0x2c05')
        marker = self.runtime / 'egpu-sleep-guard-platform-once'
        marker.write_text('test-boot\nplatform\n')
        marker.chmod(0o600)
        (self.procfs / 'cmdline').write_text(
            'quiet thunderbolt.host_reset=1 egpu.host_reset_test=1 egpu.host_reset_nodock=1\n')
        self.check_guard(2)
        self.assertFalse(marker.exists())

    def test_platform_marker_fails_closed_outside_platform_stage(self):
        marker = self.runtime / 'egpu-sleep-guard-platform-once'
        marker.write_text('test-boot\nplatform\n')
        marker.chmod(0o600)
        self.pm_test.write_text('none core processors platform devices [freezer]\n')
        self.check_guard(2)
        self.assertFalse(marker.exists())

    def test_marker_never_allows_other_sleep_operations_or_readonly_check(self):
        self.device('pci', '0000:03:00.0', vendor='0x10de', device='0x2c05')
        marker = self.runtime / 'egpu-sleep-guard-platform-once'
        marker.write_text('test-boot\nplatform\n')
        marker.chmod(0o600)
        for unit in ('systemd-hibernate.service', 'systemd-hybrid-sleep.service',
                     'systemd-suspend-then-hibernate.service', None):
            with self.subTest(unit=unit):
                self.check_guard(1, unit)
                self.assertTrue(marker.exists())

    def test_stale_marker_and_duplicate_arguments_fail_closed(self):
        marker = self.runtime / 'egpu-sleep-guard-platform-once'
        for payload, extra in (('other-boot\nplatform\n', ''),
                               ('test-boot\nplatform\n', ' thunderbolt.host_reset=0')):
            with self.subTest(payload=payload, extra=extra):
                marker.write_text(payload)
                marker.chmod(0o600)
                (self.procfs / 'cmdline').write_text(
                    'thunderbolt.host_reset=0 egpu.host_reset_test=1 egpu.host_reset_nodock=1' + extra + '\n')
                self.check_guard(2)
                self.assertFalse(marker.exists())

    def cardwire_experiment(self):
        self.device('pci', '0000:03:00.0', vendor='0x10de', device='0x2c05')
        (self.procfs / 'cmdline').write_text('quiet\n')
        (self.sysfs / 'module/thunderbolt/parameters/host_reset').write_text('Y\n')
        (self.runtime / 'systemd/system').mkdir(parents=True)
        (self.runtime / 'systemd/system/cardwired.service').symlink_to('/dev/null')
        (self.runtime / 'cardwire-active-state').write_text('inactive\n')
        marker = self.runtime / 'egpu-sleep-guard-platform-once'
        marker.write_text('test-boot\nplatform\ncardwire-off\n')
        marker.chmod(0o600)
        return marker

    def test_cardwire_off_allows_exactly_one_platform_invocation(self):
        marker = self.cardwire_experiment()
        self.assertIn('cardwire-off', self.check_guard(0).stdout)
        self.assertFalse(marker.exists())
        self.check_guard(1)

    def test_cardwire_marker_cannot_allow_hibernate_or_status_check(self):
        marker = self.cardwire_experiment()
        for unit in ('systemd-hibernate.service', 'systemd-hybrid-sleep.service',
                     'systemd-suspend-then-hibernate.service', None):
            with self.subTest(unit=unit):
                self.check_guard(1, unit)
                self.assertTrue(marker.exists())

    def test_cardwire_off_rejects_missing_runtime_mask(self):
        marker = self.cardwire_experiment()
        (self.runtime / 'systemd/system/cardwired.service').unlink()
        self.check_guard(2)
        self.assertFalse(marker.exists())

    def test_cardwire_off_rejects_active_service(self):
        marker = self.cardwire_experiment()
        (self.runtime / 'cardwire-active-state').write_text('active\n')
        self.check_guard(2)
        self.assertFalse(marker.exists())

    def test_cardwire_off_rejects_unmanaged_daemon(self):
        marker = self.cardwire_experiment()
        (self.procfs / '123').mkdir()
        (self.procfs / '123/comm').write_text('cardwired\n')
        self.check_guard(2)
        self.assertFalse(marker.exists())

    def test_cardwire_off_rejects_wrong_host_reset(self):
        self.cardwire_experiment()
        (self.sysfs / 'module/thunderbolt/parameters/host_reset').write_text('N\n')
        self.check_guard(2)

    def test_cardwire_off_rejects_experiment_arguments(self):
        marker = self.cardwire_experiment()
        for token in ('thunderbolt.host_reset=1', 'egpu.host_reset_test=1', 'egpu.host_reset_nodock=1'):
            with self.subTest(token=token):
                marker.write_text('test-boot\nplatform\ncardwire-off\n')
                marker.chmod(0o600)
                (self.procfs / 'cmdline').write_text('quiet ' + token + '\n')
                self.check_guard(2)
                self.assertFalse(marker.exists())

    def test_cardwire_off_rejects_stale_or_wrong_stage_markers(self):
        marker = self.cardwire_experiment()
        for payload in ('old-boot\nplatform\ncardwire-off\n', 'test-boot\nnone\ncardwire-off\n',
                        'test-boot\nplatform\nunknown\n', 'test-boot\nplatform\ncardwire-off\nextra\n'):
            with self.subTest(payload=payload):
                marker.write_text(payload)
                marker.chmod(0o600)
                self.check_guard(2)
                self.assertFalse(marker.exists())

    def test_cardwire_off_never_allows_real_sleep(self):
        self.cardwire_experiment()
        self.pm_test.write_text('[none] core processors platform devices freezer\n')
        self.check_guard(2)

    def test_cardwire_off_requires_private_marker(self):
        marker = self.cardwire_experiment()
        marker.chmod(0o644)
        self.check_guard(2)

    def no_graphics_experiment(self):
        marker = self.cardwire_experiment()
        marker.write_text('test-boot\nplatform\nno-graphics\n')
        (self.runtime / 'no-graphics-check').write_text('passed\n')
        (self.runtime / 'displaylink-hook-isolated').write_text('yes\n')
        return marker

    def test_no_graphics_exact_marker_allows_one_invocation(self):
        marker = self.no_graphics_experiment()
        self.assertIn('no-graphics', self.check_guard(0).stdout)
        self.assertFalse(marker.exists())
        self.check_guard(1)

    def test_no_graphics_requires_both_quiescence_and_hook_isolation(self):
        marker = self.no_graphics_experiment()
        for filename in ('no-graphics-check', 'displaylink-hook-isolated'):
            with self.subTest(filename=filename):
                saved = (self.runtime / filename).read_text()
                (self.runtime / filename).write_text('failed\n')
                marker.write_text('test-boot\nplatform\nno-graphics\n')
                self.check_guard(2)
                self.assertFalse(marker.exists())
                (self.runtime / filename).write_text(saved)

    def test_no_graphics_cannot_bypass_other_sleep_operations(self):
        marker = self.no_graphics_experiment()
        for unit in ('systemd-hibernate.service', 'systemd-hybrid-sleep.service',
                     'systemd-suspend-then-hibernate.service', None):
            self.check_guard(1, unit)
            self.assertTrue(marker.exists())

    def test_no_graphics_cannot_allow_full_sleep(self):
        self.no_graphics_experiment()
        self.pm_test.write_text('[none] core processors platform devices freezer\n')
        self.check_guard(2)


if __name__ == '__main__':
    unittest.main()
