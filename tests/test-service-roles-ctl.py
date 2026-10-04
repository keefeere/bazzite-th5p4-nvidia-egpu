#!/usr/bin/env python3
import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

spec = importlib.util.spec_from_file_location(
    "ctl", Path(__file__).resolve().parents[1] / "egpu-service-roles-ctl.py")
ctl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ctl)


def done(out="", code=0, err=""):
    return subprocess.CompletedProcess([], code, out, err)


class FakeBus:
    """Models the daemon: ApplyProfile sets the profile unless `broken`."""

    def __init__(self, current="gaming", broken=None):
        self.current, self.broken, self.calls = current, broken, []
        self.profiles = ["gaming", "work"]

    def __call__(self, args):
        self.calls.append(args)
        if args[0] == "systemctl":
            return done("/system.slice/llama.service\n" if "ControlGroup" in args else "0\n")
        verb = args[2]
        if verb == "get-property":
            name = args[-1]
            return done({"Generation": "t 3", "CurrentProfile": f's "{self.current}"',
                         "Permissions": "au 2 1 3", "DefaultMask": "u 0",
                         "Profiles": 'as 2 "gaming" "work"'}[name])
        if verb == "call" and args[-3] == "ApplyProfile":
            target = args[-1]
            if self.broken == "fail" and target == "work":
                return done("", 1, "denied")
            if self.broken != "noop" or target != "work":
                self.current = target
            return done("t 4")
        if verb == "call" and args[-5] == "ReEnrollRole":
            return done("t 9")
        raise AssertionError(args)


class Ctl(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(ctl.parse_value("au 2 1 3"), [1, 3])
        self.assertEqual(ctl.parse_value('as 2 "a" "b"'), ["a", "b"])
        self.assertEqual(ctl.parse_value('s ""'), "")
        for bad in ("au 3 1", "x 1", 'as 2 "a"'):
            with self.assertRaises(ValueError):
                ctl.parse_value(bad)

    def test_status_and_apply(self):
        bus = FakeBus()
        self.assertEqual(ctl.status(bus)["permissions"], [1, 3])
        self.assertEqual(ctl.status(bus)["default_mask"], 0)
        result = ctl.apply_profile("work", bus)
        self.assertEqual((result["applied"], result["previous"]), ("work", "gaming"))

    def test_apply_rejects_unknown_and_malformed(self):
        for bad in ("nope", "Bad Name", "../x", ""):
            with self.assertRaises(ValueError):
                ctl.apply_profile(bad, FakeBus())

    def test_failed_readback_restores_previous(self):
        bus = FakeBus(broken="noop")
        with self.assertRaises(RuntimeError) as caught:
            ctl.apply_profile("work", bus)
        self.assertIn("restored: True", str(caught.exception))
        self.assertEqual(bus.current, "gaming")

    def test_daemon_error_propagates(self):
        with self.assertRaises(RuntimeError):
            ctl.apply_profile("work", FakeBus(broken="fail"))

    def test_enroll(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "system.slice/llama.service").mkdir(parents=True)
            bus = FakeBus()
            runner = lambda a: bus(a)
            out = ctl.unit_cgroup("llama.service", runner, root)
            self.assertTrue(out.endswith("system.slice/llama.service"))
            for bad in ("x", "a b.service", "../x.service"):
                with self.assertRaises(ValueError):
                    ctl.unit_cgroup(bad, runner, root)
            result = ctl.enroll(3, "llama.service", "/usr/bin/true", bus, root)
        self.assertEqual(result["generation"], 9)
        call = [c for c in bus.calls if "ReEnrollRole" in c][0]
        self.assertEqual(call[-3:], ["3", "/usr/bin/true", result["cgroup"]])

    def test_enroll_validates_inputs(self):
        bus = FakeBus()
        with self.assertRaises(ValueError):
            ctl.enroll(16, "llama.service", "/usr/bin/true", bus)
        with self.assertRaises(ValueError):
            ctl.enroll(0, "llama.service", "../bin/x", bus)


class Reconcile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.cg = root / "cg/user.slice/k.service"
        self.cg.mkdir(parents=True)
        self.proc = root / "proc"
        for pid, exe in (("10", "/bin/sh"), ("11", "/bin/true")):
            (self.proc / pid).mkdir(parents=True)
            os.symlink(exe, self.proc / pid / "exe")
            (self.proc / pid / "stat").write_text(f"{pid} (x) S " + " ".join(["0"] * 19) + f" {pid}000 0\n")
        (self.cg / "cgroup.procs").write_text("10\n11\n")
        self.calls = []
        self.spec = ctl.parse_role_unit("0:system:k.service:/bin/sh:1000")

    def tearDown(self):
        self.tmp.cleanup()

    def runner(self, cgroup_id):
        def run(args):
            self.calls.append(args)
            if args[0] == "systemctl":
                return done("/user.slice/k.service\n")
            if args[2] == "get-property":
                return done(f"a(tttu) 1 5 {cgroup_id} 77 1000")
            return done("t 9" if "ReEnrollRole" in args else "")
        return run

    def verbs(self):
        return [c[-1] if "AdmitProcess" not in c else "admit:" + c[-2] for c in self.calls if c[0] == "busctl" and c[2] == "call"]

    def test_parse(self):
        self.assertEqual(ctl.parse_roles("a(tttu) 2 1 2 3 4 5 6 7 8"), [(1, 2, 3, 4), (5, 6, 7, 8)])
        for bad in ("a(tttu) 2 1 2 3 4", "au 1 2", "a(tttu)"):
            with self.assertRaises(ValueError):
                ctl.parse_roles(bad)
        for bad in ("x", "0:user:a.service:rel:1", "0:user:a.service:/a/../b:1", "99:user:a.service:/a:1", "0:other:a.service:/a:1"):
            with self.assertRaises(ValueError):
                ctl.parse_role_unit(bad)

    def test_new_cgroup_enrolls_then_admits_only_matching_exe(self):
        admitted = set()
        actions = ctl.reconcile([self.spec], admitted, self.runner(cgroup_id=1),
                                cgroup_root=Path(self.tmp.name) / "cg", proc=self.proc)
        self.assertTrue(any("enrolled role 0" in a for a in actions))
        self.assertEqual([a for a in actions if "admitted" in a], ["k.service: admitted pid 10"])
        self.assertEqual(len(admitted), 1)

    def test_in_sync_role_does_nothing_and_admission_is_not_repeated(self):
        ino = (Path(self.tmp.name) / "cg/user.slice/k.service").stat().st_ino
        admitted = set()
        first = ctl.reconcile([self.spec], admitted, self.runner(ino), cgroup_root=Path(self.tmp.name) / "cg", proc=self.proc)
        self.assertFalse(any("enrolled" in a for a in first))
        second = ctl.reconcile([self.spec], admitted, self.runner(ino), cgroup_root=Path(self.tmp.name) / "cg", proc=self.proc)
        self.assertEqual(second, [])

    def test_stopped_unit_and_bad_role_index(self):
        def stopped(args):
            return done("\n") if args[0] == "systemctl" else done("a(tttu) 1 5 1 77 1000")
        self.assertEqual(ctl.reconcile([self.spec], set(), stopped), ["k.service: not running"])
        spec = ctl.parse_role_unit("3:system:k.service:/bin/sh:1000")
        with self.assertRaises(RuntimeError):
            ctl.reconcile([spec], set(), self.runner(1), cgroup_root=Path(self.tmp.name) / "cg", proc=self.proc)


if __name__ == "__main__":
    unittest.main()
