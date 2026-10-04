#!/usr/bin/env python3
import importlib.util
from pathlib import Path
import os
import tempfile
import unittest

spec = importlib.util.spec_from_file_location(
    "gen", Path(__file__).resolve().parents[1] / "egpu-service-roles-config.py")
gen = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gen)

NODES = [("/dev/dri/card1", {"display"}), ("/dev/dri/renderD129", {"render"}),
         ("/dev/nvidiactl", {"compute", "display"}), ("/dev/nvidia-uvm", {"compute"})]
ROLES = [("compute", "/usr/bin/x", "/sys/fs/cgroup/a", 1000),
         ("display", "/usr/bin/y", "/sys/fs/cgroup/b", 0)]


class ServiceRolesConfig(unittest.TestCase):
    def test_masks_per_profile(self):
        self.assertEqual(gen.masks("gaming-nvidia", ROLES, NODES), [15, 15])
        self.assertEqual(gen.masks("work-nvidia", ROLES, NODES), [12, 7])
        self.assertEqual(gen.masks("work-igpu", ROLES, NODES), [12, 0])

    def test_default_mask_only_open_for_gaming(self):
        self.assertEqual(gen.default_mask("gaming-nvidia", NODES), 15)
        self.assertEqual(gen.default_mask("work-nvidia", NODES), 0)
        self.assertEqual(gen.default_mask("work-igpu", NODES), 0)
        text = gen.render(NODES, ROLES, "/o.bpf.o")
        self.assertIn("permissions = [15, 15]\ndefault_mask = 15", text)
        self.assertIn("permissions = [12, 0]\ndefault_mask = 0", text)

    def test_initial_profile_is_top_level_and_validated(self):
        text = gen.render(NODES, ROLES, "/o.bpf.o", initial_profile="gaming-nvidia")
        self.assertLess(text.index("initial_profile"), text.index("[[role]]"))
        with self.assertRaises(ValueError):
            gen.render(NODES, ROLES, "/o.bpf.o", initial_profile="nope")

    def test_masks_fit_device_count_and_toml_shape(self):
        text = gen.render(NODES, ROLES, "/o.bpf.o")
        self.assertEqual(text.count("[[role]]"), 2)
        self.assertEqual(text.count("[[profile]]"), 3)
        self.assertIn('name = "work-igpu"\npermissions = [12, 0]', text)
        for profile in gen.PROFILES:
            for mask in gen.masks(profile, ROLES, NODES):
                self.assertLess(mask, 1 << len(NODES))

    def test_rejects_unsafe_and_bad_input(self):
        with self.assertRaises(ValueError):
            gen.render(NODES, [("compute", '/bad"\n', "/sys/fs/cgroup/a", 0)], "/o")
        with self.assertRaises(ValueError):
            gen.render(NODES, [], "/o")
        for bad in ("x", "gpu:/bin/sh:/sys/fs/cgroup:1", "compute:/bin/sh:/nope:1",
                    "compute:/nonexistent-exe:/sys/fs/cgroup:1", "compute:/bin/sh:/sys/fs/cgroup:-1"):
            with self.assertRaises(ValueError):
                gen.parse_role(bad)
        self.assertEqual(gen.parse_role("display:/bin/sh:/sys/fs/cgroup:0")[0], "display")

    def test_inventory_uses_pci_identity_not_card_numbers(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pci = root / "sys/bus/pci/devices"
            for name, vendor, dev, cards in (("0000:03:00.0", "0x10de", "0x2684", ("card7", "renderD140")),
                                             ("0000:c1:00.0", "0x1002", "0x150e", ("card0", "renderD128"))):
                d = pci / name
                (d / "drm").mkdir(parents=True)
                (d / "vendor").write_text(vendor + "\n")
                (d / "device").write_text(dev + "\n")
                for c in cards:
                    (d / "drm" / c).mkdir()
            (root / "dev/dri").mkdir(parents=True)
            # Regular files are not char devices: inventory must refuse them.
            (root / "dev/dri/card7").write_text("")
            with self.assertRaises(ValueError):
                gen.inventory("0x10de", "0x2684", root / "sys", root / "dev")
            (root / "dev/dri/card7").unlink()
            os.symlink("/dev/null", root / "dev/dri/card7")  # symlink is rejected too
            with self.assertRaises(ValueError):
                gen.inventory("0x10de", "0x2684", root / "sys", root / "dev")


if __name__ == "__main__":
    unittest.main()
