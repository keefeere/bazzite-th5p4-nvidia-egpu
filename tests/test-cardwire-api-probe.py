"""Offline tests: no host D-Bus calls, device access, or services changed."""
import importlib.util
import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import MagicMock

spec = importlib.util.spec_from_file_location(
    'api_probe', Path(__file__).resolve().parents[1] / 'diagnostics/egpu-cardwire-api-probe.py')
api = importlib.util.module_from_spec(spec)
spec.loader.exec_module(api)


class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.children = []
        self.policies = {}
        self.calls = []
        self.mode = 1

    def spawn(self, args, env):
        self.assertEqual(args, ['/usr/bin/sleep', '60'])
        self.assertFalse(any(k.startswith('CARDWIRE_') for k in env))
        manager = MagicMock()
        child = manager.__enter__.return_value
        child.pid = 12340 + len(self.children)
        child.poll.return_value = None
        self.children.append(child)
        return manager

    def bus_call(self, args):
        self.calls.append(args)
        if 'get-property' in args:
            return json.dumps({'type': 'u', 'data': self.mode})
        if 'GetProcessStatus' in args:
            pid = int(args[-1])
            return json.dumps({'data': self.policies.get(pid, ['', []])})
        self.assertIn('RequestProcessAccess', args)
        pid, policy, value = int(args[-3]), args[-2], int(args[-1])
        self.assertIn(pid, [child.pid for child in self.children])
        if policy != 'Default':
            self.policies[pid] = (['AllowedExact', [0]] if policy == 'Allow_dGPU_Exact'
                                  else ['Allowed', [0]] if policy == 'Allow_dGPU' else ['Forced', [value]])
        return ''

    def test_full_cycle_only_own_children_and_readback(self):
        result = api.probe(self.bus_call, self.spawn)
        self.assertEqual(result['status'], 'passed')
        self.assertEqual(result['count'], 21)
        self.assertIn('no rendering or isolation claim', result['scope'])
        self.assertEqual(len(self.children), 3)
        for child in self.children:
            child.terminate.assert_called_once()
            child.wait.assert_called_once_with(timeout=3)
        self.assertFalse(any('set-property' in args for args in self.calls))
        self.assertFalse(result['exact_policy_readback_verified'])
        self.assertFalse(any('Allow_dGPU_Exact' in args for args in self.calls))

    def test_exact_opt_in_exercises_fresh_repeated_and_replaced_policy(self):
        result = api.probe(self.bus_call, self.spawn, exact_policy=True)
        self.assertEqual(result['status'], 'passed')
        self.assertEqual(result['count'], 52)
        self.assertEqual(len(self.children), 4)
        self.assertTrue(result['exact_policy_readback_verified'])
        self.assertFalse(result['inheritance_enforcement_verified'])
        self.assertEqual(self.policies, {child.pid: ['AllowedExact', [0]] for child in self.children})
        for child in self.children:
            child.terminate.assert_called_once()
            child.wait.assert_called_once_with(timeout=3)
        self.assertFalse(any('set-property' in args for args in self.calls))

    def test_old_daemon_exact_rejection_is_failure_not_legacy_fallback(self):
        def old(args):
            if 'Allow_dGPU_Exact' in args:
                raise RuntimeError('invalid arg: Allow_dGPU_Exact')
            return self.bus_call(args)
        with self.assertRaisesRegex(RuntimeError, 'invalid arg'):
            api.probe(old, self.spawn, exact_policy=True)
        self.assertEqual(len(self.children), 1)
        self.children[0].terminate.assert_called_once()
        self.children[0].wait.assert_called_once_with(timeout=3)

    def test_exact_silently_downgraded_to_inherited_allow_fails(self):
        def downgrade(args):
            result = self.bus_call(args)
            if 'Allow_dGPU_Exact' in args:
                self.policies[int(args[-3])] = ['Allowed', [0]]
            return result
        with self.assertRaisesRegex(RuntimeError, 'readback'):
            api.probe(downgrade, self.spawn, exact_policy=True)
        self.children[0].terminate.assert_called_once()

    def test_exact_mode_requires_hybrid_before_any_child_or_grant(self):
        self.mode = 3
        with self.assertRaisesRegex(RuntimeError, 'Hybrid'):
            api.probe(self.bus_call, self.spawn, exact_policy=True)
        self.assertEqual(self.children, [])

    def test_non_hybrid_refuses_without_spawning(self):
        self.mode = 3
        with self.assertRaisesRegex(RuntimeError, 'Hybrid'):
            api.probe(self.bus_call, self.spawn)
        self.assertEqual(self.children, [])

    def test_original_missing_key_bug_is_detected(self):
        def broken(args):
            if 'RequestProcessAccess' in args:
                raise RuntimeError('bpf_map_delete_elem failed')
            return self.bus_call(args)
        with self.assertRaisesRegex(RuntimeError, 'bpf_map_delete_elem'):
            api.probe(broken, self.spawn)
        self.assertEqual(len(self.children), 1)
        self.children[0].terminate.assert_called_once()
        self.children[0].wait.assert_called_once()

    def test_incorrect_value_readback_is_not_success(self):
        def wrong(args):
            result = self.bus_call(args)
            if 'GetProcessStatus' in args and self.policies:
                return json.dumps({'data': ['Allowed', [1]]})
            return result
        with self.assertRaisesRegex(RuntimeError, 'readback'):
            api.probe(wrong, self.spawn)
        self.children[0].terminate.assert_called_once()

    def test_preclassified_child_refuses_without_grant(self):
        def classified(args):
            if 'GetProcessStatus' in args:
                return json.dumps({'data': ['Allowed', [0]]})
            return self.bus_call(args)
        with self.assertRaisesRegex(RuntimeError, 'already classified'):
            api.probe(classified, self.spawn)
        self.assertFalse(any('RequestProcessAccess' in args for args in self.calls))
        self.children[0].terminate.assert_called_once()

    def test_timeout_still_reaps_own_child(self):
        def timeout(args):
            if 'GetProcessStatus' in args:
                raise subprocess.TimeoutExpired(args, 5)
            return self.bus_call(args)
        with self.assertRaises(subprocess.TimeoutExpired):
            api.probe(timeout, self.spawn)
        self.children[0].terminate.assert_called_once()
        self.children[0].wait.assert_called_once()

    def test_child_ignoring_term_is_killed_and_reaped(self):
        def stubborn(args, env):
            manager = self.spawn(args, env)
            self.children[-1].wait.side_effect = [subprocess.TimeoutExpired('sleep', 3), 0]
            return manager
        self.assertEqual(api.probe(self.bus_call, stubborn)['status'], 'passed')
        for child in self.children:
            child.kill.assert_called_once()
            self.assertEqual(child.wait.call_count, 2)


if __name__ == '__main__':
    unittest.main()
