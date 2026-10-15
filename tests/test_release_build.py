import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock
import zipfile
from scripts.build_windows import build, archive_folder, write_manifest, parse_version
from app_identity import APP_NAME, EXECUTABLE_STEM, EXECUTABLE_NAME, PORTABLE_NAME


class ReleaseBuildTests(unittest.TestCase):
    def test_folder_build_embeds_format_and_preserves_source_and_private_config(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = b'VERSION = "dev"\nBUILD_FORMAT = "source"\n'
            (root/'version.py').write_bytes(original)
            (root/'agent.py').write_text('print("test application")\n')
            private = {'master_password': 'private-password', 'api_key': 'private-key'}
            (root/'config.json').write_text(json.dumps(private))
            captured = []

            def fake_build(command, **kwargs):
                captured.append((command, (root/'version.py').read_text()))
                folder = root/'dist'/'onedir'/EXECUTABLE_STEM
                (folder/'_internal').mkdir(parents=True)
                (folder/EXECUTABLE_NAME).write_bytes(b'MZtest executable')
                (folder/'_internal'/'python311.dll').write_bytes(b'MZtest runtime')

            executable = build(root, 'v2.3.4', 'onedir', run=fake_build)
            self.assertEqual((root/'version.py').read_bytes(), original)
            self.assertIn("BUILD_FORMAT = 'onedir'", captured[0][1])
            self.assertIn("VERSION = '2.3.4'", captured[0][1])
            self.assertIn('--noupx', captured[0][0])
            command = captured[0][0]
            self.assertEqual(command[command.index('--name') + 1], EXECUTABLE_STEM)
            self.assertEqual(command[command.index('--icon') + 1], str(root.resolve()/'assets'/'app.ico'))
            self.assertIn('--add-data', command)
            resource = (root/'build'/'onedir'/'windows-version.txt').read_text()
            self.assertIn(APP_NAME, resource)
            self.assertIn(EXECUTABLE_NAME, resource)
            self.assertIn('SIPANTAU project', resource)
            self.assertNotIn('--onefile', captured[0][0])
            self.assertEqual(json.loads((root/'config.json').read_text()), private)
            template = json.loads((executable.parent/'config.json').read_text())
            self.assertEqual(template['master_password'], '')
            self.assertFalse(template['auto_update'])
            archive = archive_folder(root)
            standalone = root/'release'/'config.json'
            self.assertEqual(json.loads(standalone.read_text()), template)
            self.assertEqual(standalone.read_bytes(), (executable.parent/'config.json').read_bytes())
            self.assertNotIn(b'private-password', standalone.read_bytes())
            self.assertNotIn(b'private-key', standalone.read_bytes())
            self.assertEqual(json.loads((root/'config.json').read_text()), private)
            write_manifest(root, 'v2.3.4')
            report = json.loads((root/'release'/'build-manifest.json').read_text())
            config_entry = next(item for item in report['files'] if item['name'] == 'config.json')
            digest = hashlib.sha256(standalone.read_bytes()).hexdigest()
            self.assertEqual(config_entry['sha256'], digest)
            self.assertIn(f'{digest}  config.json', (root/'release'/'SHA256SUMS.txt').read_text())
            with zipfile.ZipFile(archive) as bundle:
                self.assertIn(EXECUTABLE_STEM + '/_internal/python311.dll', bundle.namelist())
                self.assertIn(EXECUTABLE_STEM + '/INSTALL.txt', bundle.namelist())
                self.assertNotIn(b'private-password', bundle.read(EXECUTABLE_STEM + '/config.json'))

    def test_failed_build_restores_original_version_module(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = b'VERSION = "dev"\nBUILD_FORMAT = "source"\n'
            (root/'version.py').write_bytes(original)
            with self.assertRaises(subprocess.CalledProcessError):
                build(root, 'v2.0.0', 'onefile', run=mock.Mock(side_effect=subprocess.CalledProcessError(1, 'PyInstaller')))
            self.assertEqual((root/'version.py').read_bytes(), original)
            for tag in ('main', 'v1.2.3-alpha', 'v65536.0.0', 'v1.2.3;bad'):
                with self.assertRaises(ValueError):
                    parse_version(tag)

    def test_manifest_records_exact_artifact_hashes_and_excludes_its_previous_outputs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'release').mkdir()
            payload = b'complete portable package'
            (root/'release'/PORTABLE_NAME).write_bytes(payload)
            with mock.patch.dict('os.environ', GITHUB_SHA='a'*40):
                write_manifest(root, 'v2.0.0')
                write_manifest(root, 'v2.0.0')
            report = json.loads((root/'release'/'build-manifest.json').read_text())
            self.assertEqual(report['commit'], 'a'*40)
            self.assertEqual([item['name'] for item in report['files']], [PORTABLE_NAME])
            self.assertEqual(report['files'][0]['sha256'], hashlib.sha256(payload).hexdigest())
            self.assertIn('build-manifest.json', (root/'release'/'SHA256SUMS.txt').read_text())
