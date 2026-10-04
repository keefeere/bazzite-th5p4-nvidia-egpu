"""Offline guards for the persistent installer. No host service/GPU access."""
import importlib.util
from pathlib import Path
import sys
import tempfile
import tomllib
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
spec = importlib.util.spec_from_file_location(
    'inst', Path(__file__).resolve().parents[1] / 'install-service-roles.py')
inst = importlib.util.module_from_spec(spec)
spec.loader.exec_module(inst)


class Installer(unittest.TestCase):
    def test_dropin_resets_bind_list_then_binds_new_binary(self):
        lines = inst.dropin_text().splitlines()
        self.assertLess(lines.index('BindReadOnlyPaths='),
                        lines.index(f'BindReadOnlyPaths={inst.RELEASE}/bin/cardwired:/usr/bin/cardwired'))
        for needle in ('NotifyAccess=main', 'FileDescriptorStorePreserve=yes', 'ReadWritePaths=/sys/fs/bpf'):
            self.assertIn(needle, lines)

    def test_unit_runs_reconcile_loop_for_both_roles(self):
        text = inst.unit_text()
        self.assertIn('After=cardwired.service', text)
        self.assertIn('--loop', text)
        self.assertEqual(text.count('--role-unit'), 2)
        self.assertIn('0:user:llama.service:' + inst.LLAMA_EXE, text)
        self.assertIn('1:user:plasma-kwin_wayland.service:' + inst.KWIN_EXE, text)
        self.assertIn('Restart=always', text)
        self.assertIn('Wants=cardwired.service', text)
        self.assertNotIn('Requires=', text)  # Requires would stop it with cardwired for good
        self.assertIn('Requires=cardwired.service', inst.unit_text('Requires'))

    def test_pinned_build_matches_the_wrapper(self):
        wrapper = Path(__file__).resolve().parents[1] / 'diagnostics/test-cardwire-service-roles.py'
        self.assertIn(f"DAEMON_SHA = '{inst.DAEMON_SHA}'", wrapper.read_text())
        self.assertIn(f"OBJECT_SHA = '{inst.OBJECT_SHA}'", wrapper.read_text())

    def test_existing_state_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / 'f'
            target.write_text('foreign')
            with self.assertRaisesRegex(RuntimeError, 'Existing state'):
                inst.write_new(target, 'x', 0o600)
            self.assertEqual(target.read_text(), 'foreign')
            fresh = Path(tmp) / 'sub/new'
            inst.write_new(fresh, 'ok', 0o600)
            self.assertEqual(fresh.read_text(), 'ok')
            self.assertEqual(oct(fresh.stat().st_mode & 0o777), '0o600')

    def test_refuses_changed_files_on_removal(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'f'
            path.write_text('edited')
            with self.assertRaisesRegex(RuntimeError, 'changed'):
                inst.checked_remove(path, 'original')
            self.assertTrue(path.exists())
            path.write_text('original')
            inst.checked_remove(path, 'original')
            self.assertFalse(path.exists())

    def test_uninstall_accepts_the_first_release_unit_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'u.service'
            path.write_text(inst.unit_text('Requires'))
            inst.checked_remove(path, (inst.unit_text(), inst.unit_text('Requires')))
            self.assertFalse(path.exists())
            path.write_text(inst.unit_text() + 'edited')
            with self.assertRaisesRegex(RuntimeError, 'changed'):
                inst.checked_remove(path, (inst.unit_text(), inst.unit_text('Requires')))

    def test_pin_removal_refuses_unknown_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            pins = Path(tmp) / 'pins'
            pins.mkdir()
            (pins / 'CW_ACTIVE').write_text('x')
            (pins / 'unknown').write_text('x')
            with patch.object(inst, 'PINS', pins):
                with self.assertRaisesRegex(RuntimeError, 'Unknown entries'):
                    inst.remove_pins()

    def test_install_requires_root_and_clean_state(self):
        with patch.object(inst.os, 'geteuid', return_value=1000):
            with self.assertRaisesRegex(RuntimeError, 'root'):
                inst.install()
        with tempfile.TemporaryDirectory() as tmp, patch.object(inst.os, 'geteuid', return_value=0), \
                patch.object(inst, 'CONFIG', Path(tmp)), patch.object(inst, 'daemon_digest') as digest:
            with self.assertRaisesRegex(RuntimeError, 'foreign state'):
                inst.install()
            digest.assert_not_called()

    def test_polkit_rule_is_exact_and_user_scoped(self):
        text = inst.polkit_text('keefeere')
        self.assertIn('subject.user == "keefeere"', text)
        for profile in ('gaming-nvidia', 'work-nvidia', 'work-igpu'):
            self.assertIn(f'"egpu-service-roles-profile@{profile}.service"', text)
        self.assertNotIn('*', text)            # no wildcard instance
        self.assertNotIn('@DESKTOP_USER@', text)
        self.assertIn('action.lookup("verb") == "start"', text)
        self.assertIn('subject.local && subject.active', text)

    def test_profile_unit_runs_only_the_control_tool(self):
        text = inst.PROFILE_UNIT_SRC.read_text()
        self.assertIn('Type=oneshot', text)
        self.assertEqual(text.count('ExecStart='), 1)
        self.assertIn('egpu-service-roles-ctl.py apply %i', text)

    def test_widget_lists_exactly_the_whitelisted_profiles(self):
        qml = (inst.PLASMOID_SRC / 'contents/ui/main.qml').read_text()
        for profile in ('gaming-nvidia', 'work-nvidia', 'work-igpu'):
            self.assertIn(f'id: "{profile}"', qml)
        self.assertEqual(qml.count('egpu-service-roles-profile@'), 1)  # one command template
        self.assertNotIn('pkexec', qml)
        self.assertNotIn('sudo', qml)

    def test_gui_install_refuses_existing_state_without_writing(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            qml = home / '.local/share/plasma/plasmoids/com.keefeere.egpu/contents/ui/main.qml'
            qml.parent.mkdir(parents=True)
            qml.write_text('old')
            (qml.parents[2] / 'metadata.json').write_text('{}')
            Path(str(qml) + inst.PLASMOID_BACKUP_SUFFIX).write_text('leftover')
            with patch.object(inst, 'desktop_user', return_value='u'), \
                    patch.object(inst.pwd, 'getpwnam', return_value=type('P', (), {'pw_dir': str(home), 'pw_uid': 1, 'pw_gid': 1})()), \
                    patch.object(inst, 'PROFILE_UNIT', home / 'unit'), patch.object(inst, 'POLKIT', home / 'rule'):
                with self.assertRaisesRegex(RuntimeError, 'already present'):
                    inst.install_gui()
            self.assertEqual(qml.read_text(), 'old')
            self.assertFalse((home / 'unit').exists())

    def test_only_known_actions(self):
        self.assertEqual(set(inst.ACTIONS), {'--install', '--uninstall', '--status'})

    def test_generated_config_shape(self):
        nodes = [('/dev/dri/card0', {'display'}), ('/dev/nvidia0', {'compute', 'display'})]
        gen = inst.load(inst.HERE / 'egpu-service-roles-config.py', 'g')
        roles = [('compute', '/bin/sh', '/sys/fs/cgroup/system.slice', 1000),
                 ('display', '/bin/true', '/sys/fs/cgroup/user.slice', 1000)]
        parsed = tomllib.loads(gen.render(nodes, roles, '/o', initial_profile='gaming-nvidia'))
        self.assertEqual(parsed['initial_profile'], 'gaming-nvidia')
        self.assertEqual(len(parsed['role']), 2)
        # distinct placeholder cgroups: shared references cannot be re-enrolled later
        self.assertNotEqual(parsed['role'][0]['cgroup'], parsed['role'][1]['cgroup'])


if __name__ == '__main__':
    unittest.main()
