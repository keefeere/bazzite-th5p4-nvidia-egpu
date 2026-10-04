"""Offline test of the KWin GPU-order generator with a fake sysfs. No host access."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'egpu-kwin-order-generator.sh'


def run(root, profile, cards):
    drm = root / 'drm'
    drm.mkdir(exist_ok=True)
    for name, vendor in cards.items():
        (drm / name / 'device').mkdir(parents=True, exist_ok=True)
        (drm / name / 'device/vendor').write_text(vendor + '\n')
    desired = root / 'desired'
    if profile is not None:
        desired.write_text(profile + '\n')
    env = {**os.environ, 'EGPU_DESIRED_PROFILE_FILE': str(desired), 'EGPU_SYSFS_DRM': str(drm)}
    return subprocess.run(['bash', str(SCRIPT)], env=env, capture_output=True, text=True, check=True).stdout


class Generator(unittest.TestCase):
    def test_work_profiles_put_amd_first_by_vendor_not_by_number(self):
        for profile in ('work-nvidia', 'work-igpu'):
            with tempfile.TemporaryDirectory() as tmp:
                out = run(Path(tmp), profile, {'card0': '0x10de', 'card1': '0x1002'})
                self.assertEqual(out, 'KWIN_DRM_DEVICES=/dev/dri/card1:/dev/dri/card0\n')
        with tempfile.TemporaryDirectory() as tmp:  # numbers swapped: still AMD first
            out = run(Path(tmp), 'work-igpu', {'card0': '0x1002', 'card1': '0x10de'})
            self.assertEqual(out, 'KWIN_DRM_DEVICES=/dev/dri/card0:/dev/dri/card1\n')

    def test_gaming_unknown_or_missing_profile_prints_nothing(self):
        for profile in ('gaming-nvidia', 'garbage', '', None):
            with tempfile.TemporaryDirectory() as tmp:
                self.assertEqual(run(Path(tmp), profile, {'card0': '0x10de', 'card1': '0x1002'}), '')

    def test_missing_gpu_or_connector_dirs_never_break_login(self):
        with tempfile.TemporaryDirectory() as tmp:  # eGPU absent: KWin must start unchanged
            self.assertEqual(run(Path(tmp), 'work-igpu', {'card0': '0x1002'}), '')
        with tempfile.TemporaryDirectory() as tmp:  # connectors like card0-DP-1 are ignored
            out = run(Path(tmp), 'work-nvidia', {'card0': '0x10de', 'card0-DP-1': '0x1002', 'card1': '0x1002'})
            self.assertEqual(out, 'KWIN_DRM_DEVICES=/dev/dri/card1:/dev/dri/card0\n')

    def test_output_is_only_a_single_assignment(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = run(Path(tmp), 'work-nvidia', {'card0': '0x10de', 'card1': '0x1002'})
            self.assertEqual(len(out.splitlines()), 1)
            self.assertRegex(out, r'^KWIN_DRM_DEVICES=/dev/dri/card\d+:/dev/dri/card\d+\n$')


if __name__ == '__main__':
    unittest.main()
