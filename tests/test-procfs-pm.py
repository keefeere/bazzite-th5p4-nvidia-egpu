#!/usr/bin/env python3
"""Hardware-free tests for the uninstalled NVIDIA procfs PM hook."""

import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('egpu_nvidia_procfs_pm', REPO / 'egpu-nvidia-procfs-pm.py')
pm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pm)


class ProcfsPmTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.params = root / 'params'
        self.control = root / 'suspend'
        self.boot = root / 'boot_id'
        self.marker = root / 'marker'
        self.active = root / 'active-boot'
        self.params.write_text('UseKernelSuspendNotifiers: 0\n')
        self.control.write_text('')
        self.boot.write_text('test-boot-id\n')
        patches = [patch.object(pm, name, path) for name, path in (
            ('PARAMS', self.params), ('SUSPEND', self.control),
            ('BOOT_ID', self.boot), ('MARKER', self.marker),
            ('ACTIVE_BOOT', self.active))]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        owner = patch.object(pm, 'MARKER_OWNER', os.getuid())
        owner.start()
        self.addCleanup(owner.stop)
        self.uid = patch.object(pm.os, 'geteuid', return_value=0)
        self.uid.start()
        self.addCleanup(self.uid.stop)
        self.active.write_text('test-boot-id\n')
        self.active.chmod(0o600)

    def test_inactive_mode_does_not_touch_gpu(self):
        self.active.unlink()
        pm.pre()
        pm.post()
        self.assertFalse(self.marker.exists())
        self.assertEqual(self.control.read_text(), '')

    def test_active_marker_from_other_boot_is_rejected(self):
        self.active.write_text('other-boot\n')
        with self.assertRaisesRegex(RuntimeError, 'does not match'):
            pm.pre()
        self.assertFalse(self.marker.exists())

    def test_pre_post_roundtrip(self):
        pm.pre()
        self.assertEqual(self.control.read_text(), 'suspend')
        self.assertEqual(self.marker.read_text(), 'test-boot-id\n')
        pm.post()
        self.assertEqual(self.control.read_text(), 'resume')
        self.assertFalse(self.marker.exists())

    def test_notifier_mode_must_be_disabled(self):
        self.params.write_text('UseKernelSuspendNotifiers: 1\n')
        with self.assertRaisesRegex(RuntimeError, 'still enabled'):
            pm.pre()
        self.assertFalse(self.marker.exists())
        pm.post()
        self.assertEqual(self.control.read_text(), '')

    def test_ambiguous_notifier_mode_is_rejected(self):
        self.params.write_text('UseKernelSuspendNotifiers: 0\nUseKernelSuspendNotifiers: 1\n')
        with self.assertRaisesRegex(RuntimeError, 'still enabled'):
            pm.pre()
        self.assertFalse(self.marker.exists())

    def test_duplicate_pre_refused(self):
        pm.pre()
        with self.assertRaisesRegex(RuntimeError, 'unresolved'):
            pm.pre()
        self.assertEqual(self.control.read_text(), 'suspend')
        pm.post()

    def test_failed_suspend_write_preserves_recovery_marker(self):
        with patch.object(pm, 'write_procfs', side_effect=OSError('simulated failure')):
            with self.assertRaises(OSError):
                pm.pre()
        self.assertTrue(self.marker.exists())
        pm.post()
        self.assertFalse(self.marker.exists())

    def test_failed_resume_preserves_recovery_marker(self):
        pm.pre()
        with patch.object(pm, 'write_procfs', side_effect=OSError('simulated failure')):
            with self.assertRaises(OSError):
                pm.post()
        self.assertTrue(self.marker.exists())

    def test_other_boot_is_not_resumed(self):
        pm.pre()
        self.boot.write_text('other-boot\n')
        with self.assertRaisesRegex(RuntimeError, 'another boot'):
            pm.post()
        self.assertEqual(self.control.read_text(), 'suspend')

    def test_symlink_marker_is_rejected(self):
        self.marker.symlink_to(self.boot)
        with self.assertRaisesRegex(RuntimeError, 'symlink'):
            pm.pre()

    def test_guard_precedes_procfs_pre_in_uninstalled_dropins(self):
        guard = (REPO / 'egpu-sleep-guard.conf').read_text()
        hook = (REPO / '95-egpu-nvidia-procfs-pm.conf').read_text()
        self.assertIn('ExecStartPre=', guard)
        self.assertIn('ExecStartPre=', hook)
        self.assertIn('ExecStopPost=', hook)
        self.assertLess('90-egpu-sleep-guard.conf', '95-egpu-nvidia-procfs-pm.conf')


if __name__ == '__main__':
    unittest.main()
