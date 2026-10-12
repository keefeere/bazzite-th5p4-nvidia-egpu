"""Offline guards for install-displaylink-persistent.sh. Never runs the install as root."""
from pathlib import Path
import subprocess
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'install-displaylink-persistent.sh'


class Persistent(unittest.TestCase):
    def test_refuses_without_root_and_changes_nothing(self):
        result = subprocess.run(['bash', str(SCRIPT)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn('sudo', result.stderr)

    def test_unknown_argument_rejected_only_after_the_root_check(self):
        self.assertEqual(subprocess.run(['bash', str(SCRIPT), '--bogus'], capture_output=True).returncode, 1)

    def test_pins_the_tested_binary_and_stores_nothing_proprietary_in_the_repo(self):
        text = SCRIPT.read_text()
        self.assertIn('MANAGER_SHA=9c6a88b61f1fd6a2c7f00c2a8a6ba096a82b5df7392ccaeb5e454a1f506a21b4', text)
        self.assertIn('refusing', text)
        self.assertFalse(list(SCRIPT.parent.glob('**/DisplayLinkManager')))
        self.assertFalse(list(SCRIPT.parent.glob('*.spkg')))

    def test_unit_and_rule_match_the_original_package(self):
        text = SCRIPT.read_text()
        self.assertIn('ExecStartPre=/sbin/modprobe evdi', text)
        self.assertIn('ENV{SYSTEMD_WANTS}+="displaylink.service"', text)
        self.assertIn('WorkingDirectory=$DEST', text)


if __name__ == '__main__':
    unittest.main()
