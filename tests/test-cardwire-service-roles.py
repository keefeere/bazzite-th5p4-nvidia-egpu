"""Offline guards for the bounded service-roles host trial. No host service/GPU access."""
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import tomllib
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location(
    'trial', Path(__file__).resolve().parents[1] / 'diagnostics/test-cardwire-service-roles.py')
trial = importlib.util.module_from_spec(spec)
sys.dont_write_bytecode = True
spec.loader.exec_module(trial)

NODES = [('/dev/dri/card0', {'display'}), ('/dev/dri/renderD129', {'render'}),
         ('/dev/nvidia0', {'compute', 'display'}), ('/dev/nvidiactl', {'compute', 'display'})]


class Guards(unittest.TestCase):
    def test_existing_state_never_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(trial, 'ROOT', Path(tmp)), \
                patch.object(trial.base, 'daemon_digest') as digest:
            with self.assertRaisesRegex(RuntimeError, 'Existing state'):
                trial.start()
            digest.assert_not_called()

    def test_unknown_baseline_refused_before_any_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'trial'
            with patch.object(trial, 'ROOT', root), patch.object(trial, 'DROPIN', Path(tmp) / 'd.conf'), \
                    patch.object(trial, 'CONFIG', Path(tmp) / 'c.toml'), patch.object(trial, 'PINS', Path(tmp) / 'p'), \
                    patch.object(trial.base, 'daemon_digest', return_value='unknown'), \
                    patch.object(trial.base, 'run') as run:
                with self.assertRaisesRegex(RuntimeError, 'baseline'):
                    trial.start()
                run.assert_not_called()
                self.assertFalse(root.exists())

    def test_override_resets_binds_then_binds_candidate(self):
        text = trial.override_text()
        lines = text.splitlines()
        self.assertLess(lines.index('BindReadOnlyPaths='), lines.index(
            f'BindReadOnlyPaths={trial.ROOT}/cardwired:/usr/bin/cardwired'))
        for needle in ('NotifyAccess=main', 'FileDescriptorStorePreserve=yes', 'ReadWritePaths=/sys/fs/bpf'):
            self.assertIn(needle, text)

    def test_generated_config_has_gaming_default_all_and_parses(self):
        with patch.object(trial.generator, 'parse_role',
                          return_value=('compute', '/usr/bin/true', '/sys/fs/cgroup/system.slice', 0)):
            parsed = tomllib.loads(trial.config_text(NODES))
        self.assertTrue(parsed['enabled'])
        profiles = {p['name']: p for p in parsed['profile']}
        self.assertEqual(profiles['gaming-nvidia']['default_mask'], 15)
        self.assertEqual(profiles['work-nvidia']['default_mask'], 0)
        self.assertEqual(len(parsed['devices']), 4)

    def test_pin_removal_refuses_unknown_entries_and_symlinks(self):
        with tempfile.TemporaryDirectory() as tmp:
            pins = Path(tmp) / 'pins'
            pins.mkdir()
            (pins / 'CW_ACTIVE').write_text('x')
            (pins / 'unknown').write_text('x')
            with patch.object(trial, 'PINS', pins):
                with self.assertRaisesRegex(RuntimeError, 'Unknown entries'):
                    trial.remove_pins()
            link = Path(tmp) / 'link'
            link.symlink_to(pins)
            with patch.object(trial, 'PINS', link):
                with self.assertRaisesRegex(RuntimeError, 'symlink'):
                    trial.remove_pins()

    def test_restore_without_activation_only_verifies_baseline(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'trial'
            root.mkdir(mode=0o700)
            (root / 'before.json').write_text(json.dumps({'policy': {}, 'files': {}, 'clients': {}}))
            script = root / 'controller.py'
            script.write_text('x')
            with patch.object(trial, 'ROOT', root), patch.object(trial, 'SCRIPT', script), \
                    patch.object(trial, 'DROPIN', Path(tmp) / 'd.conf'), patch.object(trial, 'CONFIG', Path(tmp) / 'c.toml'), \
                    patch.object(trial, 'PINS', Path(tmp) / 'p'), \
                    patch.object(trial, 'require_runtime'), \
                    patch.object(trial.base, 'baseline_matches') as baseline, \
                    patch.object(trial.base, 'run') as run:
                trial.restore()
                baseline.assert_called_once()
                run.assert_not_called()
                self.assertTrue((root / 'restored').exists())

    def test_only_known_actions(self):
        self.assertEqual(set(trial.ACTIONS), {'--start', '--start-with-deny-probe', '--execute',
                                              '--execute-deny-probe', '--restore'})


if __name__ == '__main__':
    unittest.main()
