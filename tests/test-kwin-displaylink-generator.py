"""Offline test of the DisplayLink KWin generator with a fake by-path directory."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'egpu-kwin-displaylink-generator.sh'


def run(by_path, devices, evdi=('platform-evdi.0-card',)):
    for name in evdi:
        (by_path / name).symlink_to('../card0')
    env = {k: v for k, v in os.environ.items() if k != 'KWIN_DRM_DEVICES'}
    env['EGPU_DRI_BY_PATH'] = str(by_path)
    if devices is not None:
        env['KWIN_DRM_DEVICES'] = devices
    return subprocess.run(['bash', str(SCRIPT)], env=env, capture_output=True, text=True, check=True).stdout


class Generator(unittest.TestCase):
    def test_appends_evdi_after_the_existing_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = run(Path(tmp), '/dev/dri/card1:/dev/dri/card2')
            self.assertEqual(out, f'KWIN_DRM_DEVICES=/dev/dri/card1:/dev/dri/card2:{tmp}/platform-evdi.0-card\n')

    def test_nothing_without_a_base_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(run(Path(tmp), None), '')

    def test_no_duplicate_when_already_listed(self):
        with tempfile.TemporaryDirectory() as tmp:
            by_path = Path(tmp)
            listed = f'/dev/dri/card1:{by_path}/platform-evdi.0-card'
            self.assertEqual(run(by_path, listed), f'KWIN_DRM_DEVICES={listed}\n')

    def test_first_evdi_is_listed_even_before_the_usb_adapter_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = run(Path(tmp), '/dev/dri/card1', evdi=())
            self.assertEqual(out, f'KWIN_DRM_DEVICES=/dev/dri/card1:{tmp}/platform-evdi.0-card\n')

    def test_primary_gpu_stays_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = run(Path(tmp), '/dev/dri/card2:/dev/dri/card1')
            self.assertTrue(out.startswith('KWIN_DRM_DEVICES=/dev/dri/card2:/dev/dri/card1:'))


if __name__ == '__main__':
    unittest.main()
