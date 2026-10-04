"""Hardware-free only: fake OSTree, fake boot IDs and fake tracefs.

Unexpected subprocesses fail the test. No real kargs, tracing, reboot or PM.
"""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('retention', Path(__file__).resolve().parents[1]
                                            / 'diagnostics/egpu_trace_retention.py')
r = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r)
BOOT_A = '11111111-1111-4111-8111-111111111111'
BOOT_B = '22222222-2222-4222-8222-222222222222'
BOOT_C = '33333333-3333-4333-8333-333333333333'


class RetentionTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        for name, rel in {'ROOT': 'state', 'TRACEFS': 'tracefs', 'CMDLINE': 'cmdline',
                          'BOOT_ID': 'boot_id', 'PM_TEST': 'pm_test', 'RUNTIME': 'run',
                          'GUARD': 'guard.conf'}.items():
            self.patcher(name, self.tmp / rel)
        r.RUNTIME.mkdir()
        r.TRACEFS.mkdir()
        self.patcher_subprocess = patch.object(r.subprocess, 'run',
            side_effect=AssertionError('No real subprocess allowed in retention tests'))
        self.patcher_subprocess.start()
        self.addCleanup(self.patcher_subprocess.stop)
        self.baseline = ['ostree=/baseline', 'quiet', 'pci=hpmmiosize=32M,hpmmioprefsize=32M']
        self.kargs = list(self.baseline)
        self.deployments = [{'booted': True, 'staged': False,
                             'checksum': 'baseline', 'base-checksum': 'os-base'}]
        self.transaction = None
        self.active = ''
        self.jobs = ''
        self.guard = '{ path=/usr/bin/bash ; argv[]=/usr/bin/bash /etc/egpu-nvidia/egpu-sleep-guard.sh systemd-suspend.service ; ignore_errors=no ; }'
        self.log = f'Tracing: mapped boot instance {r.NAME} at physical memory 0x20000000 of size 0x1000000\n'
        self.fstype = 'tracefs'
        self.calls = []
        self.kernel = {'release': r.TESTED_KERNEL, 'image_sha256': 'fake-hash'}
        self.original_kernel_identity = r.kernel_identity
        self.patcher('kernel_identity', lambda: dict(self.kernel))
        self.patcher('command', self.command)
        r.CMDLINE.write_text(' '.join(self.baseline))
        r.BOOT_ID.write_text(BOOT_A)
        r.PM_TEST.write_text('[none] freezer devices platform')
        r.GUARD.write_text('[Service]\nExecStartPre=/usr/bin/bash /etc/egpu-nvidia/egpu-sleep-guard.sh %n\n')
        # Foreign/global controls must survive every action unchanged.
        for name, value in {'tracing_on': '1', 'current_tracer': 'function_graph',
                            'sentinel': 'foreign evidence'}.items():
            (r.TRACEFS / name).write_text(value)
        self.global_before = {p: p.read_bytes() for p in r.TRACEFS.iterdir()}
        self.addCleanup(self.assert_globals_unchanged)
        self.output = io.StringIO()
        redirect = contextlib.redirect_stdout(self.output)
        redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)

    def patcher(self, name, value):
        patcher = patch.object(r, name, value)
        patcher.start()
        self.addCleanup(patcher.stop)

    def assert_globals_unchanged(self):
        for path, content in self.global_before.items():
            self.assertEqual(path.read_bytes(), content)

    def command(self, argv):
        self.calls.append(argv)
        if argv == ['rpm-ostree', 'status', '--json']:
            return json.dumps({'transaction': self.transaction, 'deployments': self.deployments})
        if argv == ['rpm-ostree', 'kargs']:
            return ' '.join(self.kargs)
        if argv[:2] == ['rpm-ostree', 'kargs']:
            # A mutation must already have a durable ownership record.
            self.assertEqual(r.load_state()['owned_args'], list(r.ARGS))
            for option in argv[2:]:
                if option.startswith('--append-if-missing='):
                    token = option.removeprefix('--append-if-missing=')
                    self.assertIn(token, r.ARGS)
                    if token not in self.kargs:
                        self.kargs.append(token)
                elif option.startswith('--delete-if-present='):
                    token = option.removeprefix('--delete-if-present=')
                    self.assertIn(token, r.ARGS)
                    self.kargs = [a for a in self.kargs if a != token]
                else:
                    self.fail('Unexpected kargs mutation: ' + repr(argv))
            booted = next(d for d in self.deployments if d.get('booted'))
            self.deployments = [dict(booted=False, staged=True, checksum='staged',
                                     **{'base-checksum': 'os-base'}), booted]
            return ''
        if argv[:2] == ['systemctl', 'show']:
            return self.guard
        if argv[:2] == ['systemctl', 'list-units']:
            return self.active
        if argv[:2] == ['systemctl', 'list-jobs']:
            return self.jobs
        if argv == ['stat', '-f', '-c', '%T', str(r.TRACEFS)]:
            return self.fstype
        if argv == ['journalctl', '-k', '-b', '--no-pager', '-o', 'cat']:
            return self.log
        self.fail('Unexpected external action: ' + repr(argv))

    def mutations(self):
        return [c for c in self.calls if c[:2] == ['rpm-ostree', 'kargs'] and len(c) > 2]

    def boot_staged(self):
        r.stage()
        self.deployments = [dict(self.deployments[0], booted=True, staged=False)]
        r.CMDLINE.write_text(' '.join(self.kargs))
        r.BOOT_ID.write_text(BOOT_B)
        path = r.TRACEFS / 'instances' / r.NAME
        for name, value in {'tracing_on': '0', 'current_tracer': 'nop', 'events/enable': '0',
                            'options/markers': '1', 'options/trace_printk_dest': '0',
                            'per_cpu/cpu0/buffer_meta': 'metadata',
                            'events/power/suspend_resume/trigger': '# available triggers\n',
                            'trace': '# empty trace\n', 'trace_marker': ''}.items():
            item = path / name
            item.parent.mkdir(parents=True, exist_ok=True)
            item.write_text(value)
        self.instance = path
        self.calls.clear()
        return path

    def write_marker(self, *, failure=False):
        self.writes = []
        original = Path.write_text
        def fake_write(path, data, *args, **kwargs):
            self.writes.append((path.relative_to(self.instance).as_posix(), data))
            if path == self.instance / 'trace_marker':
                if failure:
                    raise OSError('marker write failed')
                self.assertEqual((self.instance / 'tracing_on').read_text(), '1\n')
                original(self.instance / 'trace', 'python-123 [001] ...1. 123.4: tracing_mark_write: ' + data)
            return original(path, data, *args, **kwargs)
        with patch.object(Path, 'write_text', fake_write):
            r.mark()

    def test_check_changes_nothing(self):
        before = {p: p.read_bytes() for p in self.tmp.rglob('*') if p.is_file()}
        plan = r.fresh_plan()
        self.assertEqual(plan['owned_args'], list(r.ARGS))
        self.assertFalse(r.ROOT.exists())
        self.assertEqual(before, {p: p.read_bytes() for p in self.tmp.rglob('*') if p.is_file()})
        self.assertEqual(self.mutations(), [])

    def test_pending_deployment_refused(self):
        self.deployments.insert(0, dict(booted=False, staged=True, checksum='foreign'))
        with self.assertRaisesRegex(RuntimeError, 'Pending'):
            r.stage()
        self.assertFalse(r.ROOT.exists())
        self.assertEqual(self.mutations(), [])

    def test_transaction_refused(self):
        self.transaction = ['Upgrade', '/id', ':1.5']
        with self.assertRaisesRegex(RuntimeError, 'transaction'):
            r.fresh_plan()

    def test_booted_deployment_must_be_unique(self):
        self.deployments.append(dict(self.deployments[0]))
        with self.assertRaisesRegex(RuntimeError, 'booted OSTree'):
            r.fresh_plan()

    def test_live_next_args_mismatch_refused(self):
        self.kargs.append('unrelated=1')
        with self.assertRaisesRegex(RuntimeError, 'Live and next-boot'):
            r.fresh_plan()

    def test_conflicting_arguments_refused(self):
        for arg in ('reserve_mem=4M:4096:other', 'memmap=12M$0x1000', 'trace_instance=other',
                    'traceoff_on_warning', 'ftrace=function', 'trace_event=power', 'nokaslr',
                    'bootconfig', 'egpu.host_reset_test=1', 'thunderbolt.host_reset=N', 'tp_printk'):
            with self.subTest(arg=arg), self.assertRaises(RuntimeError):
                r.reject_conflicts(self.baseline + [arg])
        self.assertEqual(self.mutations(), [])

    def test_owned_arguments_exact_pair_no_duplicates(self):
        r.reject_conflicts(self.baseline + list(r.ARGS), owned=True)
        for args in (self.baseline + [r.ARGS[0]], self.baseline + list(r.ARGS) * 2,
                     self.baseline + list(r.ARGS) + ['trace_instance=foreign']):
            with self.assertRaises(RuntimeError):
                r.reject_conflicts(args, owned=True)

    def test_metadata_only_is_ignored_in_comparison(self):
        self.assertEqual(r.comparable(self.baseline), r.comparable([
            'ostree=/new', 'BOOT_IMAGE=/vmlinuz', 'initrd=/initramfs', *self.baseline[1:]]))
        self.assertNotEqual(r.comparable(self.baseline), r.comparable(self.baseline + ['quiet']))

    def test_stale_pm_state_refused(self):
        r.PM_TEST.write_text('none [platform]')
        with self.assertRaisesRegex(RuntimeError, 'pm_test'):
            r.fresh_plan()

    def test_effective_guard_required(self):
        for guard in ('', '/etc/egpu-nvidia/egpu-sleep-guard.sh ignore_errors=yes'):
            self.guard = guard
            with self.assertRaisesRegex(RuntimeError, 'effective suspend'):
                r.fresh_plan()

    def test_installed_guard_required(self):
        r.GUARD.write_text('[Service]\n')
        with self.assertRaisesRegex(RuntimeError, 'installed sleep guard'):
            r.fresh_plan()

    def test_bypass_symlink_refused(self):
        (r.RUNTIME / 'egpu-sleep-guard-platform-once').symlink_to(self.tmp / 'missing')
        with self.assertRaisesRegex(RuntimeError, 'bypass'):
            r.fresh_plan()

    def test_stale_lab_override_refused(self):
        path = r.RUNTIME / 'systemd/sleep.conf.d/zzzz-egpu-sleep-lab.conf'
        path.parent.mkdir(parents=True)
        path.write_text('temporary')
        with self.assertRaisesRegex(RuntimeError, 'Temporary sleep-lab'):
            r.fresh_plan()

    def test_active_unit_or_job_refused(self):
        self.active = 'systemd-suspend.service loaded activating start\n'
        with self.assertRaisesRegex(RuntimeError, 'active'):
            r.fresh_plan()
        self.active = ''
        self.jobs = '12 egpu-sleep-lab-example.service start waiting\n'
        with self.assertRaisesRegex(RuntimeError, 'queued'):
            r.fresh_plan()

    def test_old_kernel_refused_before_image_open(self):
        with patch.object(r.platform, 'release', return_value='6.17.0-old'), \
             patch.object(Path, 'open', side_effect=AssertionError('Must not open image')):
            with self.assertRaisesRegex(RuntimeError, 'Only'):
                self.original_kernel_identity()

    def test_stage_adds_only_two_exact_arguments(self):
        r.stage()
        state = r.load_state()
        self.assertEqual(state['phase'], 'staged')
        self.assertEqual(state['staged_checksum'], 'staged')
        self.assertEqual(self.kargs, self.baseline + list(r.ARGS))
        self.assertEqual(self.mutations(), [['rpm-ostree', 'kargs',
                          *('--append-if-missing=' + a for a in r.ARGS)]])
        self.assertEqual((r.ROOT / 'state.json').stat().st_mode & 0o777, 0o600)
        self.assertEqual(r.ROOT.stat().st_mode & 0o777, 0o700)

    def test_restage_cannot_overwrite_history(self):
        r.stage()
        before = (r.ROOT / 'state.json').read_bytes()
        with self.assertRaisesRegex(RuntimeError, 'already exists'):
            r.stage()
        self.assertEqual((r.ROOT / 'state.json').read_bytes(), before)
        self.assertEqual(len(self.mutations()), 1)

    def test_deployment_race_stops_before_mutation(self):
        saved = r.save_state
        def race(state):
            saved(state)
            self.kargs.append('foreign=1')
        with patch.object(r, 'save_state', side_effect=race):
            with self.assertRaisesRegex(RuntimeError, 'changed during preparation'):
                r.stage()
        self.assertEqual(self.mutations(), [])

    def test_interrupted_staging_preserves_ownership_but_wont_adopt_unknown_pending(self):
        real_command = self.command
        def fail_after_staging(argv):
            output = real_command(argv)
            if len(argv) > 2 and argv[:2] == ['rpm-ostree', 'kargs']:
                raise subprocess.TimeoutExpired(argv, 300)
            return output
        with patch.object(r, 'command', side_effect=fail_after_staging):
            with self.assertRaises(subprocess.TimeoutExpired):
                r.stage()
        self.assertEqual(r.load_state()['phase'], 'staging')
        with self.assertRaisesRegex(RuntimeError, 'possibly interrupted staging'):
            r.cancel()
        self.assertEqual(len(self.mutations()), 1)

    def test_mapping_must_be_unique_aligned_and_exact_size(self):
        self.assertEqual(r.mapping_from_log(self.log), {'address': '0x20000000', 'size': r.SIZE})
        for log in ('', self.log * 2, self.log.replace('0x20000000', '0x20001000'),
                    self.log.replace('0x1000000', '0x2000000'), self.log.replace('0x20000000', '0x0')):
            with self.subTest(log=log), self.assertRaises(RuntimeError):
                r.mapping_from_log(log)

    def test_nonmapped_instance_refused(self):
        path = self.boot_staged()
        (path / 'per_cpu/cpu0/buffer_meta').unlink()
        with self.assertRaisesRegex(RuntimeError, 'metadata'):
            r.mark()
        self.assertEqual((path / 'trace_marker').read_text(), '')

    def test_changed_controls_refused_without_reset(self):
        path = self.boot_staged()
        for name, value in (('tracing_on', '1'), ('current_tracer', 'function'),
                            ('events/enable', 'X'), ('options/markers', '0'),
                            ('options/trace_printk_dest', '1')):
            original = (path / name).read_text()
            (path / name).write_text(value)
            with self.subTest(name=name), self.assertRaisesRegex(RuntimeError, 'refusing to reset'):
                r.instance_state()
            self.assertEqual((path / name).read_text(), value)
            (path / name).write_text(original)

    def test_event_trigger_refused_without_reset(self):
        path = self.boot_staged()
        trigger = path / 'events/power/suspend_resume/trigger'
        trigger.write_text('traceon:unlimited\n')
        with self.assertRaisesRegex(RuntimeError, 'trigger'):
            r.mark()
        self.assertEqual(trigger.read_text(), 'traceon:unlimited\n')

    def test_tracefs_type_required(self):
        self.boot_staged()
        self.fstype = 'tmpfs'
        with self.assertRaisesRegex(RuntimeError, 'tracefs mount'):
            r.mark()

    def test_no_marker_before_staging_reboot(self):
        self.boot_staged()
        r.BOOT_ID.write_text(BOOT_A)
        with self.assertRaisesRegex(RuntimeError, 'Reboot into'):
            r.mark()

    def test_existing_trace_is_archived_not_cleared(self):
        path = self.boot_staged()
        (path / 'trace').write_text('foreign trace\n')
        with self.assertRaisesRegex(RuntimeError, 'already has records'):
            r.mark()
        self.assertEqual((path / 'trace').read_text(), 'foreign trace\n')
        self.assertEqual((r.ROOT / 'before-marker-trace.txt').read_text(), 'foreign trace\n')

    def test_mark_touches_only_marker_and_on_off(self):
        path = self.boot_staged()
        self.write_marker()
        state = r.load_state()
        self.assertEqual(state['phase'], 'marked')
        self.assertEqual(self.writes, [('tracing_on', '1\n'),
                         ('trace_marker', state['marker'] + '\n'), ('tracing_on', '0\n')])
        self.assertEqual(self.mutations(), [])
        self.assertEqual((path / 'tracing_on').read_text(), '0\n')
        with self.assertRaisesRegex(RuntimeError, 'already attempted'):
            r.mark()

    def test_marker_write_error_leaves_recording_off_and_history(self):
        path = self.boot_staged()
        with self.assertRaisesRegex(OSError, 'marker write failed'):
            self.write_marker(failure=True)
        self.assertEqual((path / 'tracing_on').read_text(), '0\n')
        self.assertEqual(r.load_state()['phase'], 'marking')
        with self.assertRaisesRegex(RuntimeError, 'already attempted'):
            r.mark()

    def test_missing_marker_not_confirmed(self):
        self.boot_staged()
        with self.assertRaisesRegex(RuntimeError, 'Marker was not confirmed'):
            r.mark()  # Regular fake file does not inject a kernel trace record.
        self.assertEqual(r.load_state()['phase'], 'marking')

    def test_same_boot_not_a_retention_success(self):
        self.boot_staged()
        self.write_marker()
        with self.assertRaisesRegex(RuntimeError, 'Same boot'):
            r.verify()
        self.assertEqual(list(r.ROOT.glob('recovered-*')), [])

    def test_cross_boot_verification_nonconsuming_no_trace_writes(self):
        path = self.boot_staged()
        self.write_marker()
        before = {p: p.read_bytes() for p in path.rglob('*') if p.is_file()}
        r.BOOT_ID.write_text(BOOT_C)
        with patch.object(Path, 'write_text', side_effect=AssertionError('No tracefs writes')):
            self.assertEqual(r.verify(), 0)
            self.assertEqual(r.verify(), 0)
        self.assertTrue(r.load_state()['verification']['retained'])
        self.assertEqual(before, {p: p.read_bytes() for p in path.rglob('*') if p.is_file()})
        self.assertEqual((r.ROOT / f'recovered-{BOOT_C}.txt').read_text(), (path / 'trace').read_text())
        self.assertEqual(self.mutations(), [])

    def test_changed_mapping_cannot_pass_even_with_marker(self):
        self.boot_staged()
        self.write_marker()
        r.BOOT_ID.write_text(BOOT_C)
        self.log = self.log.replace('0x20000000', '0x40000000')
        self.assertEqual(r.verify(), 1)
        result = r.load_state()['verification']
        self.assertTrue(result['marker_present'])
        self.assertFalse(result['same_mapping'])

    def test_missing_recovered_marker_cannot_pass(self):
        path = self.boot_staged()
        self.write_marker()
        r.BOOT_ID.write_text(BOOT_C)
        (path / 'trace').write_text('# initialized anew\n')
        self.assertEqual(r.verify(), 1)
        self.assertEqual(r.load_state()['phase'], 'not-retained')

    def test_kernel_image_change_refused(self):
        self.boot_staged()
        self.kernel['image_sha256'] = 'new-image'
        with self.assertRaisesRegex(RuntimeError, 'Kernel image changed'):
            r.mark()

    def test_other_deployment_refused(self):
        self.boot_staged()
        self.deployments[0]['checksum'] = 'other'
        with self.assertRaisesRegex(RuntimeError, 'exact staged deployment'):
            r.mark()

    def test_modified_live_args_refused(self):
        self.boot_staged()
        r.CMDLINE.write_text(' '.join(self.kargs + ['foreign=1']))
        with self.assertRaisesRegex(RuntimeError, 'Other boot arguments changed'):
            r.mark()

    def test_marker_matching_exact_not_substring(self):
        record = 'python-123 [001] ...1. 123.4: tracing_mark_write: TOKEN\n'
        self.assertTrue(r.has_marker(record, 'TOKEN'))
        for text in (record.replace('TOKEN', 'TOKEN_EXTRA'), 'TOKEN',
                     record.replace('TOKEN', 'PREFIX_TOKEN'),
                     record.replace('TOKEN', 'TOKEN trailing text'),
                     '# ' + record, 'x: function: TOKEN\n', record + record,
                     'TOKEN\n' + record.replace('TOKEN', 'TOKEN_EXTRA')):
            with self.subTest(text=text):
                self.assertFalse(r.has_marker(text, 'TOKEN'))

    def test_reported_kallsyms_offsets_record_survived(self):
        marker = 'EGPU_RETENTION_2642e4eeac844ee0aaaffb8237c2d202_17c99098-a789-46ee-94fc-a045a6fa5b11'
        written = '     python3-24306   [013] ...1.   647.057509: tracing_mark_write: ' + marker
        recovered = '       <...>-24306   [013] ...1.   647.057509: kallsyms_offsets: ' + marker
        before = r.marker_record(written, marker)
        after = r.marker_record(recovered, marker)
        self.assertTrue(r.has_marker(recovered, marker))
        self.assertEqual(before, dict(pid=24306, cpu=13, timestamp='647.057509', symbol='tracing_mark_write'))
        self.assertEqual(after, dict(before, symbol='kallsyms_offsets'))

    def test_symbol_name_is_not_marker_identity(self):
        for symbol in ('kallsyms_offsets', 'other_symbol+0x12/0x34', '0xffffffff81234567',
                       'function_in_module [some_module]'):
            with self.subTest(symbol=symbol):
                record = f'<...>-123 [001] ...1. 123.4: {symbol}: TOKEN\n'
                self.assertTrue(r.has_marker(record, 'TOKEN'))

    def test_changed_symbol_verify_passes_and_preserves_old_false_negative(self):
        path = self.boot_staged()
        self.write_marker()
        r.BOOT_ID.write_text(BOOT_C)
        recovered = (path / 'trace').read_text().replace('python-', '<...>-').replace(
            'tracing_mark_write', 'kallsyms_offsets')
        (path / 'trace').write_text(recovered)
        r.archive(f'recovered-{BOOT_C}.txt', recovered)
        old_result = dict(boot=BOOT_C, mapping=r.load_state()['mapping'],
                          same_mapping=True, marker_present=False, retained=False)
        state = r.load_state()
        state.update(phase='not-retained', verification=old_result)
        r.save_state(state)
        with patch.object(Path, 'write_text', side_effect=AssertionError('No trace writes')):
            self.assertEqual(r.verify(), 0)
            self.assertEqual(r.verify(), 0)
        state = r.load_state()
        self.assertTrue(state['verification']['same_record'])
        self.assertTrue(state['verification']['symbol_changed'])
        self.assertEqual(state['verification_history'], [old_result])
        self.assertEqual((r.ROOT / f'recovered-{BOOT_C}.txt').read_text(), recovered)
        self.assertEqual(self.mutations(), [])

    def test_same_token_with_changed_pid_cpu_or_timestamp_cannot_pass(self):
        path = self.boot_staged()
        self.write_marker()
        r.BOOT_ID.write_text(BOOT_C)
        original = (path / 'trace').read_text()
        for old, new in (('-123', '-124'), ('[001]', '[002]'), ('123.4:', '123.5:')):
            with self.subTest(field=old):
                (path / 'trace').write_text(original.replace(old, new))
                self.assertEqual(r.verify(), 1)
                self.assertTrue(r.load_state()['verification']['marker_present'])
                self.assertFalse(r.load_state()['verification']['same_record'])

    def test_saved_archive_is_not_substituted_for_missing_live_marker(self):
        path = self.boot_staged()
        self.write_marker()
        r.BOOT_ID.write_text(BOOT_C)
        r.archive(f'recovered-{BOOT_C}.txt', (path / 'trace').read_text())
        (path / 'trace').write_text('# empty live buffer\n')
        self.assertEqual(r.verify(), 1)
        self.assertFalse(r.load_state()['verification']['marker_present'])

    def test_missing_original_record_refuses_verification(self):
        self.boot_staged()
        self.write_marker()
        r.BOOT_ID.write_text(BOOT_C)
        (r.ROOT / 'written-marker-trace.txt').write_text('# empty\n')
        with self.assertRaisesRegex(RuntimeError, 'Original marker record'):
            r.verify()
        self.assertEqual(r.load_state()['phase'], 'marked')

    def test_read_trace_bounded(self):
        path = self.boot_staged()
        (path / 'trace').write_text('123456789')
        with patch.object(r, 'MAX_TRACE_TEXT', 8):
            with self.assertRaisesRegex(RuntimeError, 'Unexpectedly large'):
                r.read_trace(path)
        self.assertEqual((path / 'trace').read_text(), '123456789')

    def test_cancel_only_owned_pair_no_evidence_deletion(self):
        path = self.boot_staged()
        self.write_marker()
        trace_before = (path / 'trace').read_bytes()
        r.cancel()
        self.assertEqual(self.kargs, self.baseline)
        self.assertEqual(self.mutations(), [['rpm-ostree', 'kargs',
                          *('--delete-if-present=' + a for a in r.ARGS)]])
        self.assertEqual((path / 'trace').read_bytes(), trace_before)
        self.assertTrue((r.ROOT / 'written-marker-trace.txt').is_file())
        self.assertEqual(r.load_state()['phase'], 'cancelled')
        r.cancel()  # No second kargs write, even though first cancellation is staged.
        self.assertEqual(len(self.mutations()), 1)

    def test_cancel_before_reboot_allowed_for_known_pending(self):
        r.stage()
        r.cancel()
        self.assertEqual(self.kargs, self.baseline)
        self.assertEqual(len(self.mutations()), 2)

    def test_cancel_rejects_unrecognized_pending(self):
        r.stage()
        self.deployments[0]['checksum'] = 'foreign-pending'
        with self.assertRaisesRegex(RuntimeError, 'Unrecognized pending'):
            r.cancel()
        self.assertEqual(len(self.mutations()), 1)

    def test_cancel_rejects_unrelated_argument_change(self):
        r.stage()
        self.kargs.append('unrelated=1')
        with self.assertRaisesRegex(RuntimeError, 'Other next-boot arguments changed'):
            r.cancel()
        self.assertEqual(len(self.mutations()), 1)

    def test_cancel_without_state_never_removes_matching_args(self):
        self.kargs.extend(r.ARGS)
        with self.assertRaises(FileNotFoundError):
            r.cancel()
        self.assertEqual(self.mutations(), [])

    def test_state_permissions_and_symlinks_refused(self):
        r.stage()
        file = r.ROOT / 'state.json'
        file.chmod(0o644)
        with self.assertRaisesRegex(RuntimeError, 'Unsafe'):
            r.load_state()
        file.chmod(0o600)
        other = r.ROOT / 'owned-evidence.json'
        file.rename(other)
        file.symlink_to(other)
        with self.assertRaisesRegex(RuntimeError, 'Unsafe'):
            r.load_state()
        self.assertTrue(other.is_file())

    def test_unknown_schema_not_adopted(self):
        r.stage()
        state = r.load_state()
        state['schema'] = 999
        r.save_state(state)
        with self.assertRaisesRegex(RuntimeError, 'Unknown state'):
            r.cancel()
        self.assertEqual(len(self.mutations()), 1)

    def test_main_check_read_only(self):
        with patch.object(sys, 'argv', ['retention.py', 'check']), \
             patch.object(r.os, 'geteuid', return_value=0):
            self.assertEqual(r.main(), 0)
        self.assertIn('READ-ONLY PREFLIGHT PASSED', self.output.getvalue())
        self.assertFalse(r.ROOT.exists())

    def test_main_requires_root(self):
        with patch.object(sys, 'argv', ['retention.py', 'stage']), \
             patch.object(r.os, 'geteuid', return_value=1000):
            with self.assertRaisesRegex(RuntimeError, 'sudo'):
                r.main()
        self.assertEqual(self.calls, [])


if __name__ == '__main__':
    unittest.main()
