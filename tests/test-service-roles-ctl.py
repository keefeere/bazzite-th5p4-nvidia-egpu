#!/usr/bin/env python3
import importlib.util
import os
from unittest.mock import patch
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
                         "Profiles": f'as {len(self.profiles)} ' + " ".join(f'"{p}"' for p in self.profiles)}[name])
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
        self.root = root / "cg"
        self.cg = self.root / "user.slice/user-1000.slice/user@1000.service/session.slice/k.service"
        self.cg.mkdir(parents=True)
        self.proc = root / "proc"
        for pid, exe in (("10", "/bin/sh"), ("11", "/bin/true")):
            (self.proc / pid).mkdir(parents=True)
            os.symlink(exe, self.proc / pid / "exe")
            (self.proc / pid / "stat").write_text(f"{pid} (x) S " + " ".join(["0"] * 19) + f" {pid}000 0\n")
        (self.cg / "cgroup.procs").write_text("10\n11\n")
        self.spec = ctl.parse_role_unit("0:user:k.service:/bin/sh:1000")
        self.calls = []

    def tearDown(self):
        self.tmp.cleanup()

    def runner(self, cgroup_id):
        def run(args):
            self.calls.append(args)
            if args[2] == "get-property":
                return done(f"a(tttu) 1 5 {cgroup_id} 77 1000")
            return done("t 9" if "ReEnrollRole" in args else "")
        return run

    def go(self, admitted, cgroup_id, spec=None):
        return ctl.reconcile([spec or self.spec], admitted, self.runner(cgroup_id),
                             cgroup_root=self.root, proc=self.proc)

    def test_parse(self):
        self.assertEqual(ctl.parse_roles("a(tttu) 2 1 2 3 4 5 6 7 8"), [(1, 2, 3, 4), (5, 6, 7, 8)])
        for bad in ("a(tttu) 2 1 2 3 4", "au 1 2", "a(tttu)", "a(ttuu) 1 1 2 3 4"):
            with self.assertRaises(ValueError):
                ctl.parse_roles(bad)
        for bad in ("x", "0:user:a.service:rel:1", "0:user:a.service:/a/../b:1", "99:user:a.service:/a:1", "0:other:a.service:/a:1"):
            with self.assertRaises(ValueError):
                ctl.parse_role_unit(bad)

    def test_unit_cgroup_lookup(self):
        self.assertEqual(ctl.unit_cgroup_dir(self.spec, self.root), self.cg)
        other = self.root / "user.slice/user-1000.slice/user@1000.service/app.slice/k.service"
        other.mkdir(parents=True)
        with self.assertRaises(RuntimeError):
            ctl.unit_cgroup_dir(self.spec, self.root)
        system = ctl.parse_role_unit("0:system:z.service:/bin/sh:0")
        self.assertIsNone(ctl.unit_cgroup_dir(system, self.root))
        (self.root / "system.slice/z.service").mkdir(parents=True)
        self.assertEqual(ctl.unit_cgroup_dir(system, self.root), self.root / "system.slice/z.service")

    def test_new_cgroup_enrolls_then_admits_only_matching_exe(self):
        admitted = set()
        actions = self.go(admitted, cgroup_id=1)
        self.assertTrue(any("enrolled role 0" in a for a in actions))
        self.assertEqual([a for a in actions if "admitted" in a], ["k.service: admitted pid 10"])
        self.assertEqual(len(admitted), 1)

    def test_in_sync_role_does_nothing_and_admission_is_not_repeated(self):
        ino = self.cg.stat().st_ino
        admitted = set()
        first = self.go(admitted, ino)
        self.assertFalse(any("enrolled" in a for a in first))
        self.assertEqual(self.go(admitted, ino), [])

    def test_stopped_unit_and_bad_role_index(self):
        stopped = ctl.parse_role_unit("0:user:absent.service:/bin/sh:1000")
        self.assertEqual(self.go(set(), 1, stopped), ["absent.service: not running"])
        bad = ctl.parse_role_unit("3:user:k.service:/bin/sh:1000")
        with self.assertRaises(RuntimeError):
            self.go(set(), 1, bad)



class SafetyAndPersistence(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def kwin_tree(self, devices, vendor):
        cg = self.root / "cg/user.slice/user-1000.slice/user@1000.service/session.slice/plasma-kwin_wayland.service"
        cg.mkdir(parents=True)
        (cg / "cgroup.procs").write_text("5\n7\n")
        proc = self.root / "proc"
        for pid, env in (("5", b"HOME=/h\0"), ("7", b"A=1\0KWIN_DRM_DEVICES=" + devices + b"\0")):
            (proc / pid).mkdir(parents=True)
            (proc / pid / "environ").write_bytes(env)
        card = self.root / "sys/class/drm/card0/device"
        card.mkdir(parents=True)
        (card / "vendor").write_text(vendor + "\n")
        return self.root / "cg", proc, self.root / "sys"

    def test_kwin_primary_vendor_is_read_from_the_first_device(self):
        cg, proc, sysfs = self.kwin_tree(b"/dev/dri/card0:/dev/dri/card1", "0x10de")
        self.assertEqual(ctl.kwin_primary_vendor(cg, proc, sysfs), "0x10de")
        self.assertIsNone(ctl.kwin_primary_vendor(self.root / "empty", proc, sysfs))

    def test_malicious_or_odd_device_values_are_not_trusted(self):
        cg, proc, sysfs = self.kwin_tree(b"../../etc/passwd:/dev/dri/card1", "0x1002")
        self.assertIsNone(ctl.kwin_primary_vendor(cg, proc, sysfs))

    def test_work_profiles_refused_while_kwin_renders_on_nvidia(self):
        with self.assertRaisesRegex(RuntimeError, "AMD"):
            ctl.preflight("work-nvidia", lambda: "0x10de")
        with self.assertRaisesRegex(RuntimeError, "AMD"):
            ctl.preflight("work-igpu", lambda: None)
        ctl.preflight("work-nvidia", lambda: "0x1002")      # AMD primary: allowed
        ctl.preflight("gaming-nvidia", lambda: "0x10de")    # gaming is always safe

    def test_apply_refuses_before_touching_the_daemon(self):
        bus = FakeBus()
        with patch.object(ctl, "kwin_primary_vendor", return_value="0x10de"):
            with self.assertRaisesRegex(RuntimeError, "AMD"):
                ctl.apply_profile("work-nvidia", bus)
        self.assertEqual(bus.calls, [])

    def test_note_and_remember_are_best_effort(self):
        with patch.object(ctl, "LAST_ERROR", self.root / "err"), patch.object(ctl, "DESIRED", self.root / "d/desired"):
            ctl.note_error("boom")
            self.assertEqual((self.root / "err").read_text(), "boom\n")
            ctl.note_error(None)
            self.assertFalse((self.root / "err").exists())
            ctl.remember_profile("work-nvidia")
            self.assertEqual((self.root / "d/desired").read_text(), "work-nvidia\n")
        with patch.object(ctl, "LAST_ERROR", Path("/proc/nope/err")):
            ctl.note_error("x")  # must not raise

    def test_remembered_profile_restored_only_when_bound_and_safe(self):
        desired = self.root / "desired"
        desired.write_text("work-nvidia\n")
        cg = self.root / "cg/user.slice/user-1000.slice/user@1000.service/app.slice/llama.service"
        cg.mkdir(parents=True)
        spec = ctl.parse_role_unit("0:user:llama.service:/bin/sh:1000")
        ino = cg.stat().st_ino
        state = {}
        applied = []

        class Bus(FakeBus):
            def __init__(self, bound):
                super().__init__(current="gaming")
                self.bound = bound
            def __call__(self, args):
                if args[2] == "get-property" and args[-1] == "Roles":
                    return done(f"a(tttu) 1 1 {ino if self.bound else 5} 9 1000")
                if args[2] == "call" and args[-3] == "ApplyProfile":
                    applied.append(args[-1])
                    self.current = args[-1]
                    return done("t 5")
                return super().__call__(args)

        def go(bus, vendor):
            with patch.object(ctl, "kwin_primary_vendor", return_value=vendor):
                return ctl.restore_desired([spec], state, bus, desired, self.root / "cg", vendor_reader=lambda: vendor)

        bus = Bus(bound=False)
        bus.profiles = ["gaming", "work-nvidia"]
        self.assertIsNone(go(bus, "0x1002"))            # roles not bound yet: wait
        self.assertEqual(applied, [])
        bus.bound = True
        self.assertIn("not restored", go(bus, "0x10de"))  # KWin on NVIDIA: refuse, say so
        self.assertIsNone(go(bus, "0x10de"))              # ...only once
        self.assertEqual(applied, [])
        self.assertIn("restored remembered profile", go(bus, "0x1002"))
        self.assertEqual(applied, ["work-nvidia"])
        self.assertIsNone(go(bus, "0x1002"))              # already current


if __name__ == "__main__":
    unittest.main()
