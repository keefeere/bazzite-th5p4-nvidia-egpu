"""Hardware-free tests: no sleep, privileged commands or real sysfs writes."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location("lab", Path(__file__).resolve().parents[1] / "diagnostics" / "egpu-sleep-lab.py")
lab = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lab)

GOOD_LOG = """PM: suspend entry (s2idle)
Freezing user space processes completed (0.001 seconds)
suspend debug: Waiting for 5 second(s).
PM: suspend exit
"""
DEVICES_LOG = GOOD_LOG.replace("suspend debug: Waiting for", "PM: suspend devices took 0.100 seconds\nsuspend debug: Waiting for").replace(
    "PM: suspend exit", "PM: resume devices took 0.200 seconds\nPM: suspend exit")
PLATFORM_LOG = GOOD_LOG.replace(
    "suspend debug: Waiting for",
    "PM: suspend devices took 0.100 seconds\n"
    "PM: late suspend of devices complete after 1.000 msecs\n"
    "PM: noirq suspend of devices complete after 2.000 msecs\n"
    "suspend debug: Waiting for").replace(
    "PM: suspend exit",
    "PM: noirq resume of devices complete after 2.000 msecs\n"
    "PM: early resume of devices complete after 1.000 msecs\n"
    "PM: resume devices took 0.200 seconds\nPM: suspend exit")


def state(stamp="100", active="inactive", result="success"):
    return {"ExecMainStartTimestampMonotonic": stamp, "ActiveState": active, "Result": result}


class PMControl:
    def __init__(self):
        self.value = "none"
        self.writes = []

    def read_text(self):
        return " ".join(f"[{word}]" if word == self.value else word
                        for word in ("none", "core", "processors", "platform", "devices", "freezer"))

    def write_text(self, text):
        self.value = text.strip()
        self.writes.append(self.value)


class ParsingTests(unittest.TestCase):
    def test_compute_clients_exclude_only_kwin(self):
        output = ("37737, /usr/bin/kwin_wayland\n"
                  "90311, /opt/Steam/fossilize_replay\n"
                  "90416, /usr/bin/python3\n")
        self.assertEqual(lab.parse_compute_clients(output),
                         [("90311", "/opt/Steam/fossilize_replay"),
                          ("90416", "/usr/bin/python3")])
        self.assertEqual(lab.parse_compute_clients("No running processes found\n"), [])
        with self.assertRaises(RuntimeError):
            lab.parse_compute_clients("not csv")

    def test_platform_requires_deep_callbacks_but_not_real_s2idle_loop(self):
        self.assertTrue(lab.stage_confirmed("platform", state(), PLATFORM_LOG))
        self.assertFalse(lab.stage_confirmed("platform", state(), DEVICES_LOG))
        for marker in ("late suspend of devices", "noirq suspend of devices",
                       "noirq resume of devices", "early resume of devices"):
            with self.subTest(marker=marker):
                self.assertFalse(lab.stage_confirmed("platform", state(), PLATFORM_LOG.replace(marker, "missing")))
        self.assertFalse(lab.stage_confirmed("platform", state(), PLATFORM_LOG.replace(
            "PM: noirq resume of devices complete after 2.000 msecs\n", "")
            + "PM: noirq resume of devices complete after 2.000 msecs\n"))
        self.assertFalse(lab.stage_confirmed("platform", state(), PLATFORM_LOG + "PM: suspend-to-idle\n"))

    def test_devices_requires_both_device_phases_in_order(self):
        self.assertTrue(lab.stage_confirmed("devices", state(), DEVICES_LOG))
        self.assertFalse(lab.stage_confirmed("devices", state(), GOOD_LOG))
        self.assertFalse(lab.stage_confirmed("devices", state(), DEVICES_LOG.replace("PM: resume devices took", "missing")))
        self.assertFalse(lab.stage_confirmed("devices", state(), DEVICES_LOG + "PM: noirq suspend of devices complete"))
        self.assertFalse(lab.stage_confirmed("devices", state(), DEVICES_LOG * 2))
        self.assertFalse(lab.stage_confirmed("devices", state(result="exit-code"), DEVICES_LOG))

    def test_unsupported_stage_never_reaches_preflight(self):
        with patch.object(lab, "preflight") as preflight:
            for stage in ("none", "core", "processors"):
                with self.assertRaises(RuntimeError):
                    lab.experiment(Path("/not-used"), stage)
            preflight.assert_not_called()

    def test_cli_stage_defaults_to_freezer(self):
        with patch.object(lab.sys, "argv", ["lab", "run"]), \
             patch.object(lab, "launch", return_value=0) as launch:
            self.assertEqual(lab.main(), 0)
            launch.assert_called_once_with("freezer", False)

    def test_cli_devices_is_explicit(self):
        with patch.object(lab.sys, "argv", ["lab", "run", "--stage", "devices"]), \
             patch.object(lab, "launch", return_value=0) as launch:
            self.assertEqual(lab.main(), 0)
            launch.assert_called_once_with("devices", False)

    def test_cli_platform_is_explicit(self):
        with patch.object(lab.sys, "argv", ["lab", "run", "--stage", "platform"]), \
             patch.object(lab, "launch", return_value=0) as launch:
            self.assertEqual(lab.main(), 0)
            launch.assert_called_once_with("platform", False)

    def test_cli_guard_bypass_is_explicit_platform_only(self):
        with patch.object(lab.sys, "argv", ["lab", "run", "--stage", "platform",
                                             "--host-reset-guard-bypass"]), \
             patch.object(lab, "launch", return_value=0) as launch:
            self.assertEqual(lab.main(), 0)
            launch.assert_called_once_with("platform", True)
        with patch.object(lab.sys, "argv", ["lab", "run", "--stage", "devices",
                                             "--host-reset-guard-bypass"]), \
             self.assertRaises(SystemExit):
            lab.main()

    def test_cli_devices_check_does_not_launch(self):
        with patch.object(lab.sys, "argv", ["lab", "check", "--stage", "devices"]), \
             patch.object(lab, "preflight", return_value={}) as preflight, \
             patch.object(lab, "launch") as launch, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(lab.main(), 0)
            preflight.assert_called_once_with("devices", False)
            launch.assert_not_called()

    def test_runtime_labels_use_existing_policy_nonrecursively(self):
        paths = [Path("/run/systemd/sleep.conf.d/test.conf"),
                 Path("/run/systemd/system/systemd-suspend.service.d/test.conf")]
        with patch.object(Path, "exists", return_value=True), \
             patch.object(lab, "require_command") as command:
            lab.label_overrides(paths)
            targets = [str(p) for path in paths for p in (path.parent, path)]
            self.assertEqual(command.call_args_list[0].args[0], ["restorecon", "-F", *targets])
            self.assertEqual(command.call_args_list[1].args[0], ["matchpathcon", "-V", *targets])

    def test_labels_noop_without_selinux(self):
        with patch.object(Path, "exists", return_value=False), \
             patch.object(lab, "require_command") as command:
            lab.label_overrides([Path("/test.conf")])
            command.assert_not_called()

    def test_request_keeps_inhibitors_and_avoids_client_session_check(self):
        self.assertEqual(lab.SUSPEND_REQUEST[-3:], ["SuspendWithFlags", "t", "1"])
        self.assertNotIn("systemctl", lab.SUSPEND_REQUEST)
        self.assertIn("--allow-interactive-authorization=no", lab.SUSPEND_REQUEST)

    def test_only_definitive_refusals_allow_cleanup(self):
        self.assertTrue(lab.definitive_rejection("Operation denied due to active block inhibitor"))
        self.assertTrue(lab.definitive_rejection("Please retry operation after closing inhibitors and logging out other users."))
        self.assertFalse(lab.definitive_rejection("Connection timed out"))
        self.assertFalse(lab.definitive_rejection("Action suspend already in progress"))

    def test_idle_check_rejects_pending_logind_action(self):
        with patch.object(lab, "require_command", return_value="b true"):
            with self.assertRaises(RuntimeError):
                lab.assert_sleep_idle()

    def test_idle_check_rejects_queued_or_changed_invocation(self):
        with patch.object(lab, "unit_state", return_value=state()), \
             patch.object(lab, "require_command", side_effect=["b false", "42 suspend.target start waiting"]):
            with self.assertRaises(RuntimeError):
                lab.assert_sleep_idle()
        with patch.object(lab, "unit_state", return_value=state("101")), \
             patch.object(lab, "require_command", return_value="b false"):
            with self.assertRaises(RuntimeError):
                lab.assert_sleep_idle("100")

    def test_setting_selection(self):
        self.assertEqual(lab.selected("[none] core freezer"), "none")
        with self.assertRaises(RuntimeError):
            lab.selected("default modeset uvm")

    def test_single_state_requires_reset(self):
        config = "[Sleep]\nSuspendState=mem standby freeze\n[Sleep]\nSuspendState=mem\n"
        self.assertNotEqual(lab.effective_suspend_states(config), ["mem"])
        config += "[Sleep]\nSuspendState=\nSuspendState=mem\n"
        self.assertEqual(lab.effective_suspend_states(config), ["mem"])

    def test_config_default_and_sections(self):
        self.assertEqual(lab.effective_suspend_states("#SuspendState=mem\n"), ["mem", "standby", "freeze"])
        self.assertEqual(lab.effective_suspend_states("[Other]\nSuspendState=mem\n"), ["mem", "standby", "freeze"])

    def test_freezer_confirmation(self):
        self.assertTrue(lab.freezer_confirmed(state(), GOOD_LOG))

    def test_no_false_success(self):
        for log in ("", GOOD_LOG * 2,
                    GOOD_LOG.replace("suspend debug: Waiting for", "missing stage"),
                    GOOD_LOG + "PM: suspend devices took 0.25 seconds",
                    GOOD_LOG + "Suspending console(s)",
                    GOOD_LOG.replace("completed", "aborted after")):
            with self.subTest(log=log):
                self.assertFalse(lab.freezer_confirmed(state(), log))
        self.assertFalse(lab.freezer_confirmed(state(result="exit-code"), GOOD_LOG))

    def test_enqueue_is_not_completion(self):
        states = [state(), state("101", "activating"), state("101", "deactivating"), state("101")]
        with patch.object(lab, "unit_state", side_effect=states) as read, patch.object(lab.time, "sleep"):
            self.assertEqual(lab.wait_for_cycle("100"), states[-1])
            self.assertEqual(read.call_count, 4)

    def test_failed_new_cycle_is_finished(self):
        failed = state("102", "failed", "exit-code")
        with patch.object(lab, "unit_state", return_value=failed):
            self.assertEqual(lab.wait_for_cycle("100"), failed)

    def test_collected_unit_retains_recorded_result(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "ended.json"
            marker.write_text(json.dumps({"service_result": "exit-code"}))
            with patch.object(lab, "unit_state", return_value=state("0")):
                result = lab.wait_for_cycle("100", timeout=0, completion=marker)
                self.assertEqual(result["Result"], "exit-code")

    def test_completion_marker_does_not_override_busy_unit(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "ended.json"
            marker.write_text(json.dumps({"service_result": "success"}))
            with patch.object(lab, "unit_state", return_value=state("101", "deactivating")):
                with self.assertRaises(RuntimeError):
                    lab.wait_for_cycle("100", timeout=0, completion=marker)

    def test_missing_or_old_timestamp_does_not_complete(self):
        for value in (state(), {"ActiveState": "inactive"}, state("0")):
            with self.subTest(value=value), patch.object(lab, "unit_state", return_value=value):
                with self.assertRaises(RuntimeError):
                    lab.wait_for_cycle("100", timeout=0)

    def test_check_does_not_launch(self):
        with patch.object(lab.sys, "argv", ["lab", "check"]), \
             patch.object(lab, "preflight", return_value={}), \
             patch.object(lab, "launch") as launch, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(lab.main(), 0)
            launch.assert_not_called()


class GuardPreflightTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        for constant, name, content in (
                ("CMDLINE", "cmdline", "thunderbolt.host_reset=0 egpu.host_reset_test=1 egpu.host_reset_nodock=1"),
                ("HOST_RESET", "reset", "N"), ("SLEEP_GUARD", "guard", "installed")):
            path = self.root / name
            path.write_text(content)
            self.stack.enter_context(patch.object(lab, constant, path))
        self.stack.enter_context(patch.object(lab, "SLEEP_GUARD_ONCE", self.root / "marker"))
        self.exec_pre = f"/usr/bin/bash {lab.SLEEP_GUARD} systemd-suspend.service"
        self.status = {"transaction": None, "deployments": [{"booted": False, "staged": False}]}
        self.next_args = "quiet ostree=/example"
        self.stack.enter_context(patch.object(lab, "require_command", side_effect=self.command))

    def command(self, argv):
        if argv[0] == "systemctl":
            return self.exec_pre
        if argv[1] == "status":
            return json.dumps(self.status)
        if argv[1] == "kargs":
            return self.next_args
        raise AssertionError(argv)

    def test_exact_experiment_with_finalized_recovery_passes_readonly(self):
        lab.validate_guard_bypass("platform")
        self.assertFalse(lab.SLEEP_GUARD_ONCE.exists())

    def test_staging_is_not_enough_for_forced_reset_recovery(self):
        self.status["deployments"][0]["staged"] = True
        with self.assertRaisesRegex(RuntimeError, "not finalized"):
            lab.validate_guard_bypass("platform")

    def test_retained_experiment_on_next_boot_is_rejected(self):
        self.next_args += " egpu.host_reset_test=1"
        with self.assertRaisesRegex(RuntimeError, "Next-boot"):
            lab.validate_guard_bypass("platform")

    def test_old_guard_without_service_scope_is_rejected(self):
        self.exec_pre = f"/usr/bin/bash {lab.SLEEP_GUARD}"
        with self.assertRaisesRegex(RuntimeError, "unit-scoped"):
            lab.validate_guard_bypass("platform")

    def test_normal_boot_and_wrong_stage_are_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "only.*platform"):
            lab.validate_guard_bypass("freezer")
        lab.HOST_RESET.write_text("Y")
        with self.assertRaisesRegex(RuntimeError, "not N"):
            lab.validate_guard_bypass("platform")


class ExperimentTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.folder = self.root / "logs"
        self.folder.mkdir()
        self.pm = PMControl()
        self.depth = self.root / "depth"
        self.depth.write_text("default modeset uvm")
        self.flags = (self.root / "debug", self.root / "times")
        self.flags[0].write_text("0")
        self.flags[1].write_text("1")
        self.conf = self.root / "sleep.conf"
        self.unitconf = self.root / "unit.conf"
        self.guard_once = self.root / "guard-once"
        self.stack.enter_context(patch.object(lab, "SLEEP_GUARD_ONCE", self.guard_once))
        self.request_rc = 0
        self.request_output = "test request"
        self.stage = "freezer"
        self.log = GOOD_LOG
        self.config = "[Sleep]\nSuspendState=\nSuspendState=mem\n"
        self.stack.enter_context(patch.object(lab, "PM_TEST", self.pm))
        self.stack.enter_context(patch.object(lab, "DEPTH", self.depth))
        self.stack.enter_context(patch.object(lab, "FLAGS", self.flags))
        self.stack.enter_context(patch.object(lab, "SLEEP_CONF", self.conf))
        self.stack.enter_context(patch.object(lab, "UNIT_CONF", self.unitconf))
        self.stack.enter_context(patch.object(lab, "preflight", return_value={"kernel": "test"}))
        self.stack.enter_context(patch.object(lab, "snapshot"))
        self.stack.enter_context(patch.object(lab, "observer"))
        self.stack.enter_context(patch.object(lab, "save_command"))
        self.label = self.stack.enter_context(patch.object(lab, "label_overrides"))
        self.stack.enter_context(patch.object(lab.os, "sync"))
        self.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        self.stack.enter_context(patch.object(lab, "unit_state", return_value=state()))
        self.calls = self.stack.enter_context(patch.object(lab, "command", side_effect=self.fake_command))
        self.stack.enter_context(patch.object(lab, "require_command", side_effect=self.fake_required))
        self.wait = self.stack.enter_context(patch.object(lab, "wait_for_cycle", side_effect=self.finish))

    def fake_command(self, argv, timeout=10):
        if argv == lab.SUSPEND_REQUEST:
            self.assertEqual(self.pm.value, self.stage)
            return subprocess.CompletedProcess(argv, self.request_rc, self.request_output)
        return subprocess.CompletedProcess(argv, 0, "")

    def fake_required(self, argv):
        if argv[0] == "systemd-analyze":
            return self.config
        if "Environment" in argv:
            return "Environment=SYSTEMD_LOG_LEVEL=debug"
        if "-k" in argv:
            return self.log
        return ""

    def finish(self, baseline, **kwargs):
        # No reset to normal sleep is permitted while the cycle is in flight.
        self.assertEqual(self.pm.value, self.stage)
        self.assertTrue(self.conf.exists())
        self.assertTrue(self.unitconf.exists())
        self.assertEqual(baseline, "100")
        return state("101")

    def assert_restored(self):
        self.assertEqual(self.pm.value, "none")
        self.assertEqual([p.read_text().strip() for p in self.flags], ["0", "1"])
        self.assertEqual(self.depth.read_text().strip(), "default")
        self.assertFalse(self.conf.exists())
        self.assertFalse(self.unitconf.exists())
        self.assertTrue((self.folder / "restored.txt").exists())

    def assert_retained(self):
        self.assertEqual(self.pm.value, self.stage)
        self.assertTrue(self.conf.exists())
        self.assertTrue(self.unitconf.exists())
        self.assertFalse((self.folder / "restored.txt").exists())
        self.assertTrue((self.folder / "reboot-clears-test.txt").exists())
        self.assertEqual(sum(c.args[0] == lab.SUSPEND_REQUEST for c in self.calls.call_args_list), 1)

    def test_success_restores_after_cycle(self):
        self.assertEqual(lab.experiment(self.folder), 0)
        self.assert_restored()
        self.label.assert_called_once()

    def test_devices_restores_without_full_sleep_or_retry(self):
        self.stage = "devices"
        self.log = DEVICES_LOG
        self.assertEqual(lab.experiment(self.folder, self.stage), 0)
        self.assert_restored()
        self.assertEqual(self.pm.writes, ["devices", "none"])
        self.assertEqual(sum(c.args[0] == lab.SUSPEND_REQUEST for c in self.calls.call_args_list), 1)
        self.assertIn("DEVICES STAGE RETURNED", (self.folder / "result.txt").read_text())

    def test_devices_timeout_preserves_selected_guard(self):
        self.stage = "devices"
        self.wait.side_effect = RuntimeError("timeout")
        with self.assertRaises(RuntimeError):
            lab.experiment(self.folder, self.stage)
        self.assert_retained()
        self.assertIn("pm_test=devices", (self.folder / "reboot-clears-test.txt").read_text())

    def test_platform_restores_without_entering_real_sleep(self):
        self.stage = "platform"
        self.log = PLATFORM_LOG
        self.assertEqual(lab.experiment(self.folder, self.stage), 0)
        self.assert_restored()
        self.assertEqual(self.pm.writes, ["platform", "none"])
        self.assertEqual(sum(c.args[0] == lab.SUSPEND_REQUEST for c in self.calls.call_args_list), 1)
        self.assertIn("PLATFORM STAGE RETURNED", (self.folder / "result.txt").read_text())

    def test_unconsumed_exception_is_removed_even_on_request_error(self):
        self.stage = "platform"
        self.request_rc = 1
        def arm(_):
            self.assertEqual(self.pm.value, "platform")
            self.guard_once.write_text("test-boot\nplatform\n")
        with patch.object(lab, "arm_guard_platform_once", side_effect=arm) as arm_call:
            with self.assertRaises(RuntimeError):
                lab.experiment(self.folder, "platform", True)
            arm_call.assert_called_once()
        self.assertFalse(self.guard_once.exists())
        self.assert_retained()

    def test_label_failure_aborts_before_sleep_and_restores(self):
        self.label.side_effect = RuntimeError("label mismatch")
        with self.assertRaisesRegex(RuntimeError, "label mismatch"):
            lab.experiment(self.folder)
        self.assert_restored()
        self.assertFalse(any(c.args[0] == lab.SUSPEND_REQUEST for c in self.calls.call_args_list))

    def test_failed_finished_cycle_also_restores(self):
        self.wait.side_effect = lambda _, **kwargs: state("101", "failed", "exit-code")
        self.assertEqual(lab.experiment(self.folder), 1)
        self.assert_restored()

    def test_failed_setup_restores_without_sleep(self):
        self.config = "[Sleep]\nSuspendState=mem freeze\n"
        with self.assertRaises(RuntimeError):
            lab.experiment(self.folder)
        self.assert_restored()
        self.assertFalse(any(c.args[0] == lab.SUSPEND_REQUEST for c in self.calls.call_args_list))

    def test_timeout_never_restores_to_real_sleep_early(self):
        self.wait.side_effect = RuntimeError("timeout")
        with self.assertRaises(RuntimeError):
            lab.experiment(self.folder)
        self.assert_retained()

    def test_request_error_never_retries(self):
        self.request_rc = 1
        with self.assertRaises(RuntimeError):
            lab.experiment(self.folder)
        self.assert_retained()
        self.wait.assert_not_called()

    def test_confirmed_refusal_restores_without_reboot(self):
        self.request_rc = 1
        self.request_output = "Call failed: Operation denied due to active block inhibitor"
        with patch.object(lab, "assert_sleep_idle") as idle:
            with self.assertRaisesRegex(RuntimeError, "refused before the test"):
                lab.experiment(self.folder)
            idle.assert_called_once_with("100")
        self.assert_restored()
        self.wait.assert_not_called()

    def test_refusal_with_concurrent_sleep_still_retains_guard(self):
        self.request_rc = 1
        self.request_output = "Call failed: Operation denied due to active block inhibitor"
        with patch.object(lab, "assert_sleep_idle", side_effect=RuntimeError("pending")):
            with self.assertRaises(RuntimeError):
                lab.experiment(self.folder)
        self.assert_retained()

    def prepare_cleanup(self):
        self.pm.value = "freezer"
        saved = {"boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
                 "pm_test": "none", "flags": {str(self.flags[0]): "0", str(self.flags[1]): "1"}}
        (self.folder / "before.json").write_text(json.dumps(saved))
        (self.folder / "request.txt").write_text(
            "Please retry operation after closing inhibitors and logging out other users.\nexit=1\n")
        self.conf.write_text("[Sleep]\nSuspendState=\nSuspendState=mem\n")
        self.unitconf.write_text(lab.unit_override(self.folder))
        self.stack.enter_context(patch.object(lab, "LOG_ROOT", self.root))
        self.stack.enter_context(patch.object(lab, "LOCK", self.root / "lock"))
        self.stack.enter_context(patch.object(lab.os, "geteuid", return_value=0))
        original_stat = Path.stat
        self.stack.enter_context(patch.object(Path, "stat", lambda p, **kw:
            SimpleNamespace(st_uid=0) if p == self.folder else original_stat(p, **kw)))
        return self.stack.enter_context(patch.object(lab, "assert_sleep_idle"))

    def test_explicit_cleanup_after_legacy_session_refusal(self):
        idle = self.prepare_cleanup()
        lab.cleanup_rejected(self.folder)
        idle.assert_called_once()
        self.assert_restored()

    def test_cleanup_refuses_foreign_override(self):
        self.prepare_cleanup()
        self.unitconf.write_text("belongs to another run")
        with self.assertRaisesRegex(RuntimeError, "does not belong"):
            lab.cleanup_rejected(self.folder)
        self.assertEqual(self.pm.value, "freezer")
        self.assertTrue(self.conf.exists())

    def test_cleanup_refuses_unknown_request_error(self):
        self.prepare_cleanup()
        (self.folder / "request.txt").write_text("Timed out\nexit=1\n")
        with self.assertRaisesRegex(RuntimeError, "not definitively rejected"):
            lab.cleanup_rejected(self.folder)
        self.assertEqual(self.pm.value, "freezer")

    def test_interrupt_after_enqueue_retains_guard(self):
        self.wait.side_effect = InterruptedError("stopped")
        with self.assertRaises(InterruptedError):
            lab.experiment(self.folder)
        self.assert_retained()

    def test_foreign_override_is_not_deleted(self):
        self.conf.write_text("someone else's diagnostic")
        with self.assertRaises(FileExistsError):
            lab.experiment(self.folder)
        self.assertEqual(self.conf.read_text(), "someone else's diagnostic")
        self.assertFalse(any(c.args[0] == lab.SUSPEND_REQUEST for c in self.calls.call_args_list))


if __name__ == "__main__":
    unittest.main()
