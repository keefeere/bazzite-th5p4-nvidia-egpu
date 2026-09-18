"""Synthetic PCI trees only; no sysfs mutation or GPU interaction."""
import importlib.util
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('checker', Path(__file__).resolve().parents[1] / 'egpu-existing-resources.py')
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)
IO, MEM, PREF = checker.IO, checker.MEM, checker.MEM | checker.PREF


class ExistingResourcesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.top = Path(self.tmp.name)
        self.base = self.top / 'bus'
        self.base.mkdir()
        self.root, self.up, self.bridge = '0000:00:01.1', '0000:01:00.0', '0000:02:00.0'
        self.gpu, self.audio = '0000:03:00.0', '0000:03:00.1'
        self.ports = [f'0000:02:0{i}.0' for i in (1, 2, 3)]
        self.paths, self.rows = {}, {}
        self.node(self.root, None, (1, 96))
        self.node(self.up, self.root, (2, 6))
        self.node(self.bridge, self.up, (3, 3))
        self.node(self.gpu, self.bridge)
        self.node(self.audio, self.bridge)
        for i, port in enumerate(self.ports):
            self.node(port, self.up, (4 + i, 4 + i))
        for bdf in (self.root, self.up, self.bridge):
            self.rows[bdf][13] = (0xb000, 0xefff if bdf != self.bridge else 0xbfff, IO)
            self.rows[bdf][14] = (0xc4000000, {self.root: 0xdbffffff, self.up: 0xc83fffff, self.bridge: 0xc80fffff}[bdf], MEM)
            self.rows[bdf][15] = (0x6000000000, 0x7fffffffff if bdf == self.root else 0x6401ffffff, PREF)
        for i, port in enumerate(self.ports):
            self.rows[port][13] = (0xc000 + i * 0x1000, 0xcfff + i * 0x1000, IO)
            self.rows[port][14] = (0xc8100000 + i * 0x100000, 0xc81fffff + i * 0x100000, MEM)
        self.rows[self.gpu][0] = (0xc4000000, 0xc7ffffff, MEM)
        self.rows[self.gpu][1] = (0x6000000000, 0x63ffffffff, PREF)
        self.rows[self.gpu][3] = (0x6400000000, 0x6401ffffff, PREF)
        self.rows[self.gpu][5] = (0xb000, 0xb07f, IO)
        self.rows[self.gpu][6] = (0xc8080000, 0xc80fffff, MEM)
        self.rows[self.audio][0] = (0xc8000000, 0xc8003fff, MEM)
        self.specs = [(self.gpu, i, size, kind) for i, size, kind in
                      ((0, 64 << 20, MEM), (1, 16 << 30, PREF), (3, 32 << 20, PREF), (5, 128, IO))]
        self.specs.append((self.audio, 0, 16384, MEM))

    def node(self, bdf, parent, buses=None):
        path = (self.paths[parent] if parent else self.top / 'devices') / bdf
        path.mkdir(parents=True)
        (self.base / bdf).symlink_to(path)
        self.paths[bdf] = path
        self.rows[bdf] = [(0, 0, 0)] * (16 if buses else 13)
        for name, value in {'vendor': '0x8086', 'device': '0x5786', 'class': '0x060400' if buses else '0x030000'}.items():
            (path / name).write_text(value)
        if buses:
            for name, value in zip(('secondary_bus_number', 'subordinate_bus_number'), buses):
                (path / name).write_text(str(value))

    def run_check(self):
        for bdf, rows in self.rows.items():
            (self.paths[bdf] / 'resource').write_text('\n'.join(' '.join(hex(x) for x in r) for r in rows))
        return checker.validate(self.base, self.root, self.up, self.bridge, self.gpu,
                                self.audio, '0x8086:0x5786', self.specs)

    def test_compact_firmware_bus_map_is_valid(self):
        self.assertIn(f'{self.up}: buses 02-06', self.run_check())

    def test_large_reserved_bus_map_is_also_valid(self):
        (self.paths[self.up] / 'subordinate_bus_number').write_text('96')
        for port, (sec, sub) in zip(self.ports, ((4, 34), (35, 65), (66, 96))):
            (self.paths[port] / 'secondary_bus_number').write_text(str(sec))
            (self.paths[port] / 'subordinate_bus_number').write_text(str(sub))
        self.run_check()

    def test_small_bar1_rejected_without_resize(self):
        self.rows[self.gpu][1] = (0x6000000000, 0x600fffffff, PREF)
        with self.assertRaisesRegex(ValueError, 'BAR1'):
            self.run_check()

    def test_bar_outside_parent(self):
        self.rows[self.bridge][15] = (0x6000000000, 0x62ffffffff, PREF)
        with self.assertRaisesRegex(ValueError, 'outside parent'):
            self.run_check()

    def test_hp_or_any_extra_endpoint_rejected(self):
        self.node('0000:04:00.0', self.ports[0])
        with self.assertRaisesRegex(ValueError, 'exact empty enclosure'):
            self.run_check()

    def test_sibling_window_overlap(self):
        self.rows[self.ports[1]][14] = self.rows[self.ports[0]][14]
        with self.assertRaisesRegex(ValueError, 'sibling resources overlap'):
            self.run_check()

    def test_sibling_bus_overlap(self):
        (self.paths[self.ports[0]] / 'subordinate_bus_number').write_text('5')
        with self.assertRaisesRegex(ValueError, 'bus ranges overlap'):
            self.run_check()

    def test_unassigned_resource(self):
        start, end, flags = self.rows[self.gpu][1]
        self.rows[self.gpu][1] = (start, end, flags | 0x20000000)
        with self.assertRaisesRegex(ValueError, 'unassigned'):
            self.run_check()

    def test_wrong_bridge_identity(self):
        (self.paths[self.ports[0]] / 'vendor').write_text('0x1002')
        with self.assertRaisesRegex(ValueError, 'identity'):
            self.run_check()

    def test_nonprefetch_bar_cannot_use_prefetch_window(self):
        self.rows[self.audio][0] = (0x6000000000, 0x6000003fff, MEM)
        with self.assertRaisesRegex(ValueError, 'outside parent'):
            self.run_check()

    def test_absent_function(self):
        (self.base / self.audio).unlink()
        with self.assertRaises(FileNotFoundError):
            self.run_check()

    def test_bridge_bus_outside_parent(self):
        (self.paths[self.up] / 'subordinate_bus_number').write_text('5')
        with self.assertRaisesRegex(ValueError, 'escapes parent'):
            self.run_check()


if __name__ == '__main__':
    unittest.main()
