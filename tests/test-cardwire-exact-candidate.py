"""Offline candidate deployment guards. No host services or GPU access."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('candidate', Path(__file__).resolve().parents[1] /
                                             'diagnostics/test-cardwire-exact-candidate.py')
candidate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(candidate)


class CandidateGuards(unittest.TestCase):
    def test_unknown_daemon_refused_before_write(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'trial'
            with patch.object(candidate, 'ROOT', root), patch.object(candidate, 'PRIVATE', root / 'private'), \
                    patch.object(candidate, 'PRIVATE_DROPIN', root / 'private.conf'), \
                    patch.object(candidate, 'DROPIN', root / 'binary.conf'), \
                    patch.object(candidate, 'daemon_digest', return_value='unknown'), \
                    patch.object(candidate, 'run') as run:
                with self.assertRaisesRegex(RuntimeError, 'baseline'):
                    candidate.start()
                run.assert_not_called()
                self.assertFalse(root.exists())

    def test_existing_state_never_overwritten(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(candidate, 'ROOT', Path(directory)), \
                patch.object(candidate, 'daemon_digest') as daemon:
            with self.assertRaisesRegex(RuntimeError, 'Existing state'):
                candidate.start()
            daemon.assert_not_called()

    def test_changed_or_symlink_override_not_removed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'override'
            path.write_text('foreign')
            with self.assertRaisesRegex(RuntimeError, 'changed'):
                candidate.checked_remove(path, b'ours')
            self.assertEqual(path.read_text(), 'foreign')
            link = Path(directory) / 'link'
            link.symlink_to(path)
            with self.assertRaisesRegex(RuntimeError, 'symlink'):
                candidate.checked_remove(link, b'foreign')
            self.assertTrue(link.is_symlink())

    def test_only_owned_override_removed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'override'
            path.write_bytes(b'ours')
            candidate.checked_remove(path, b'ours')
            self.assertFalse(path.exists())
            candidate.checked_remove(path, b'ours')

    def test_restart_or_file_change_detected(self):
        before = {'policy': {'mode': 1}, 'files': {'/expected': 'original'}, 'clients': {'KWin': 100}}
        with patch.object(candidate, 'daemon_digest', return_value=candidate.OLD_SHA), \
                patch.object(candidate, 'policy', return_value=before['policy']), \
                patch.object(candidate, 'digest', return_value='original'), \
                patch.object(candidate, 'clients', return_value={'KWin': 200}):
            with self.assertRaisesRegex(RuntimeError, 'identity'):
                candidate.baseline_matches(before)
        with patch.object(candidate, 'daemon_digest', return_value=candidate.OLD_SHA), \
                patch.object(candidate, 'policy', return_value=before['policy']), \
                patch.object(candidate, 'digest', return_value='changed'):
            with self.assertRaisesRegex(RuntimeError, 'file changed'):
                candidate.baseline_matches(before)

    def test_candidate_cannot_count_as_restored(self):
        with patch.object(candidate, 'daemon_digest', return_value=candidate.NEW_SHA):
            with self.assertRaisesRegex(RuntimeError, 'not restored'):
                candidate.baseline_matches({'policy': {}})

    def test_repository_copy_cannot_run_internal_actions(self):
        with self.assertRaisesRegex(RuntimeError, 'staged controller'):
            candidate.require_runtime()

    def test_binary_mount_reset_precedes_private_mounts(self):
        text = candidate.override_text()
        self.assertIn('BindReadOnlyPaths=\nBindReadOnlyPaths=', text)
        self.assertNotIn('BindPaths=', text)
        self.assertIn('/usr/bin/cardwired', text)
        self.assertLess(candidate.PERMANENT.name, candidate.DROPIN.name)
        self.assertLess(candidate.DROPIN.name, candidate.PRIVATE_DROPIN.name)

    def test_bind_merge_regression_old_order_erases_private_state(self):
        # systemd.exec: assigning empty to either resets BOTH bind lists.
        def merge(dropins):
            bindings = []
            for _, text in sorted(dropins):
                for line in text.splitlines():
                    key, sep, value = line.partition('=')
                    if sep and key in ('BindPaths', 'BindReadOnlyPaths'):
                        if not value:
                            bindings.clear()
                        else:
                            bindings.extend(value.split())
            return bindings
        private = '[Service]\nBindPaths=/trial/config:/etc/cardwire /trial/state:/var/lib/cardwire\n'
        permanent = '[Service]\nBindReadOnlyPaths=/old/cardwired:/usr/bin/cardwired\n'
        binary = (candidate.DROPIN.name, candidate.override_text())
        old = merge([('91-egpu-smart-test.conf', private), (candidate.PERMANENT.name, permanent), binary])
        fixed = merge([(candidate.PRIVATE_DROPIN.name, private), (candidate.PERMANENT.name, permanent), binary])
        self.assertEqual(old, [str(candidate.ROOT) + '/cardwired:/usr/bin/cardwired'])
        self.assertEqual(fixed, old + ['/trial/config:/etc/cardwire', '/trial/state:/var/lib/cardwire'])

    def test_archive_requires_quiescent_worker_and_control_process(self):
        good = 'LoadState=loaded\nActiveState=failed\nSubState=failed\nMainPID=0\nControlPID=0\nJob='
        for text in (good.replace('MainPID=0', 'MainPID=12'), good.replace('ControlPID=0', 'ControlPID=13'),
                     good.replace('Job=', 'Job=27'), good.replace('ActiveState=failed', 'ActiveState=active'), ''):
            with self.subTest(state=text), patch.object(candidate, 'run', return_value=text):
                with self.assertRaisesRegex(RuntimeError, 'not quiescent'):
                    candidate.quiescent_unit('test.service')
        with patch.object(candidate, 'run', return_value=good):
            candidate.quiescent_unit('test.service')

    def test_collected_timer_has_no_pid_properties(self):
        # Exact output observed on the host after a completed watchdog.
        for load_state in ('not-found', 'loaded'):
            text = f'LoadState={load_state}\nActiveState=inactive\nSubState=dead\nJob='
            with patch.object(candidate, 'run', return_value=text) as run:
                candidate.quiescent_unit('test.timer')
                self.assertEqual(run.call_args.args[0][-1], 'LoadState,ActiveState,SubState,Job')

    def test_active_or_queued_timer_is_not_archivable(self):
        for text in ('LoadState=loaded\nActiveState=active\nSubState=waiting\nJob=',
                     'LoadState=loaded\nActiveState=inactive\nSubState=dead\nJob=123',
                     'LoadState=masked\nActiveState=inactive\nSubState=dead\nJob='):
            with self.subTest(state=text), patch.object(candidate, 'run', return_value=text):
                with self.assertRaisesRegex(RuntimeError, 'not quiescent'):
                    candidate.quiescent_unit('test.timer')

    def test_service_pids_remain_required_even_when_unit_is_collected(self):
        text = 'LoadState=not-found\nActiveState=inactive\nSubState=dead\nJob='
        with patch.object(candidate, 'run', return_value=text):
            with self.assertRaisesRegex(RuntimeError, 'not quiescent'):
                candidate.quiescent_unit('test.service')
        with patch.object(candidate, 'run', return_value=text + '\nMainPID=0\nControlPID=0'):
            candidate.quiescent_unit('test.service')

    def test_other_unit_types_are_not_guessed(self):
        with patch.object(candidate, 'run') as run:
            with self.assertRaisesRegex(RuntimeError, 'Unsupported'):
                candidate.quiescent_unit('test.socket')
            run.assert_not_called()

    def test_archive_rejects_unrestored_or_foreign_private_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'outer'
            root.mkdir()
            with patch.object(candidate, 'ROOT', root), patch.object(candidate, 'owned_trial_directory'), \
                    patch('builtins.open'), patch.object(candidate.fcntl, 'flock'), \
                    patch.object(candidate, 'run') as run:
                with self.assertRaisesRegex(RuntimeError, 'not recorded'):
                    candidate.archive_restored()
                run.assert_not_called()

    def test_archive_preserves_both_reports_without_service_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root, private = base / 'outer', base / 'inner'
            root.mkdir(mode=0o700)
            private.mkdir(mode=0o700)
            marker = root / 'restored'
            marker.touch()
            policy = {'mode': 1, 'config': {k: False for k in candidate.CONFIG_KEYS}}
            before = {'policy': policy, 'files': {str(p): 'digest' for p in candidate.FILES}, 'clients': {}}
            inner = {'daemon_sha256': candidate.NEW_SHA, **policy,
                     'files': {str(p): 'digest' for p in candidate.FILES[2:]}}
            (root / 'before.json').write_text(json.dumps(before))
            (private / 'before.json').write_text(json.dumps(inner))
            (private / 'result.json').write_text('original failure evidence')
            real_stat = Path.stat
            def stat(path, *args, **kwargs):
                result = real_stat(path, *args, **kwargs)
                if path == marker:
                    fields = list(result)
                    fields[4] = 0
                    return os.stat_result(fields)
                return result
            with patch.object(candidate, 'ROOT', root), patch.object(candidate, 'PRIVATE', private), \
                    patch.object(candidate, 'DROPIN', base / '98.conf'), \
                    patch.object(candidate, 'PRIVATE_DROPIN', base / '99.conf'), \
                    patch.object(candidate, 'LEGACY_PRIVATE_DROPIN', base / '91.conf'), \
                    patch.object(candidate, 'owned_trial_directory'), patch.object(Path, 'stat', stat), \
                    patch.object(candidate, 'quiescent_unit') as quiet, \
                    patch.object(candidate, 'baseline_matches') as baseline, \
                    patch.object(candidate, 'run') as run, patch('builtins.open'), \
                    patch.object(candidate.fcntl, 'flock'), patch('builtins.print'):
                candidate.archive_restored()
                baseline.assert_called_once_with(before)
                self.assertEqual(quiet.call_count, 3)
                run.assert_called_once_with(['systemctl', 'reset-failed', candidate.UNIT + '.service'])
            self.assertFalse(root.exists())
            self.assertFalse(private.exists())
            saved = list(base.glob('outer-archive-*/trial/private-policy-trial/result.json'))
            self.assertEqual(len(saved), 1)
            self.assertEqual(saved[0].read_text(), 'original failure evidence')


if __name__ == '__main__':
    unittest.main()
