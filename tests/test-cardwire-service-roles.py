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

    def test_archive_requires_recorded_rollback(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'trial'
            root.mkdir(mode=0o700)
            with patch.object(trial, 'ROOT', root), patch.object(trial.base, 'owned_trial_directory'), \
                    patch('builtins.open', create=True), patch.object(trial.fcntl, 'flock'), \
                    patch.object(trial.base, 'run') as run:
                with self.assertRaisesRegex(RuntimeError, 'rollback is not recorded'):
                    trial.archive_restored()
                run.assert_not_called()
                self.assertTrue(root.exists())

    def test_llama_command_enrolls_as_root_before_exec(self):
        snap = {'exe': '/home/u/.local/bin/llama', 'argv': ['llama', 'serve', '--port', '9931'],
                'cwd': '/home/u', 'env': {'HOME': '/home/u', 'CUDA_VISIBLE_DEVICES': '0'}}
        command = trial.llama_run_command(snap, Path('/run/t/helpers/ctl.py'))
        pre = [a for a in command if a.startswith('--property=ExecStartPre=')][0]
        self.assertTrue(pre.startswith('--property=ExecStartPre=+/usr/bin/python3 /run/t/helpers/ctl.py enroll 0 '))
        self.assertIn('--exe /home/u/.local/bin/llama', pre)
        self.assertFalse(any(a.startswith('--uid=') for a in command))  # init_t cannot exec home files
        self.assertIn('--reuid=keefeere', command)
        self.assertIn('--setenv=CUDA_VISIBLE_DEVICES=0', command)
        self.assertEqual(command[command.index('--') + 1:],
                         ['/usr/bin/setpriv', '--reuid=keefeere', '--regid=keefeere', '--init-groups', '--',
                          '/home/u/.local/bin/llama', 'serve', '--port', '9931'])

    def test_snapshot_whitelists_environment_and_exact_argv(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = Path(tmp)
            (proc / '42').mkdir()
            (proc / '42/cmdline').write_bytes(b'/x/llama\0serve\0--port\09931\0')
            (proc / '42/environ').write_bytes(b'HOME=/h\0SECRET_TOKEN=abc\0CUDA_X=1\0PATH=/bin\0')
            os.symlink('/bin/sh', proc / '42/exe')
            os.symlink('/home', proc / '42/cwd')
            answers = {'MainPID': '42', 'ControlGroup': '/user.slice/llama.service'}
            snap = trial.llama_snapshot(proc, lambda *a, **k: answers[a[3]])
        self.assertEqual(snap['argv'], ['/x/llama', 'serve', '--port', '9931'])
        self.assertEqual(snap['env'], {'HOME': '/h', 'CUDA_X': '1', 'PATH': '/bin'})
        self.assertNotIn('SECRET_TOKEN', snap['env'])
        self.assertEqual(snap['cgroup'], '/sys/fs/cgroup/user.slice/llama.service')
        with self.assertRaises(RuntimeError):
            trial.llama_snapshot(Path('/nonexistent'), lambda *a, **k: '0')

    def test_nvidia_fd_detection(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'u.service').mkdir()
            (root / 'u.service/cgroup.procs').write_text('1\n')
            self.assertEqual(trial.nvidia_fds('u', root), set())  # pid 1 fds unreadable or non-NVIDIA

    def test_archive_after_llama_trial_ignores_llama_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'trial'
            root.mkdir(mode=0o700)
            (root / 'restored').write_text('')
            (root / 'llama-stopped').write_text('')
            (root / 'before.json').write_text(json.dumps(
                {'policy': {}, 'files': {}, 'clients': {'plasma-kwin_wayland.service': {'MainPID': '1'}}}))
            seen = {}
            def baseline(before, include_clients=True):
                seen['include_clients'] = include_clients
            with patch.object(trial, 'ROOT', root), patch.object(trial, 'DROPIN', Path(tmp) / 'd'), \
                    patch.object(trial, 'CONFIG', Path(tmp) / 'c'), patch.object(trial, 'PINS', Path(tmp) / 'p'), \
                    patch.object(trial.base, 'owned_trial_directory'), patch.object(trial.base, 'quiescent_unit'), \
                    patch.object(trial.base, 'baseline_matches', side_effect=baseline), \
                    patch.object(trial.base, 'clients', return_value={'plasma-kwin_wayland.service': {'MainPID': '1'}}), \
                    patch.object(trial.base, 'run', return_value='not-found'), \
                    patch.object(trial.fcntl, 'flock'), patch('builtins.open', create=True):
                trial.archive_restored()
            self.assertFalse(seen['include_clients'])

    def test_only_known_actions(self):
        self.assertEqual(set(trial.ACTIONS), {'--start', '--start-with-deny-probe', '--execute',
                                              '--execute-deny-probe', '--restore', '--archive-restored',
                                              '--start-llama-role', '--execute-llama-role',
                                              '--start-kwin-role', '--execute-kwin-role'})


if __name__ == '__main__':
    unittest.main()
