#!/usr/bin/env python3
import importlib.util
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


if __name__ == "__main__":
    unittest.main()
