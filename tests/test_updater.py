import hashlib
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
import updater


def release(data=b'MZwindows executable', tag='v1.2.0'):
    return {'tag_name': tag, 'assets': [{'name': 'SIPANTAU.exe', 'size': len(data),
            'digest': 'sha256:' + hashlib.sha256(data).hexdigest(),
            'browser_download_url': f'https://github.com/{updater.REPOSITORY}/releases/download/{tag}/SIPANTAU.exe'}]}


class UpdaterTests(unittest.TestCase):
    def test_stable_newer_release_requires_trusted_url_digest_and_size(self):
        self.assertIsNone(updater.select_asset(release(tag='v1.0.0'), '1.0.0'))
        self.assertIsNone(updater.select_asset({**release(), 'prerelease': True}, '1.0.0'))
        self.assertIsNone(updater.select_asset(release(tag='v0.9.0'), '1.0.0'))
        self.assertEqual(updater.select_asset(release(), '1.0.0')[0], 'v1.2.0')
        for key, value in [('digest', ''), ('browser_download_url', 'http://evil/agent.exe'),
                           ('size', updater.MAX_DOWNLOAD+1), ('size', True)]:
            bad = release(); bad['assets'][0][key] = value
            with self.assertRaises(ValueError): updater.select_asset(bad, '1.0.0')
        with self.assertRaises(ValueError): updater.version_tuple('v1.0.1-preview')

    def test_corrupt_truncated_or_non_executable_download_never_replaces_staged_file(self):
        session = mock.MagicMock()
        response = session.get.return_value.__enter__.return_value
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)/'update.exe'
            target.write_bytes(b'previous download')
            for data in [b'corrupt', b'MZwindows executable extra', b'']:
                response.iter_content.return_value = [data]
                with self.assertRaises(ValueError): updater.download_asset(session, release()['assets'][0], target)
                self.assertEqual(target.read_bytes(), b'previous download')
                self.assertFalse(target.with_suffix('.part').exists())
            data = b'not an executable'
            response.iter_content.return_value = [data]
            with self.assertRaises(ValueError): updater.download_asset(session, release(data)['assets'][0], target)
            response.iter_content.return_value = [b'MZwindows executable']
            updater.download_asset(session, release()['assets'][0], target)
            self.assertEqual(target.read_bytes(), b'MZwindows executable')
            self.assertTrue(session.get.call_args.kwargs['verify'])

    def test_off_by_default_and_never_checks_while_session_active(self):
        install = mock.Mock()
        update = updater.ReleaseUpdater({}, '.', '1.0.0', mock.Mock(), lambda: True, install)
        self.assertFalse(update.enabled)
        with mock.patch.object(updater.requests, 'Session') as network:
            update.check_once(); network.assert_not_called()
            update.enabled = True
            update.can_install = lambda: False
            update.check_once(); network.assert_not_called()
        install.assert_not_called()

    def test_rechecks_lock_state_after_download_before_install(self):
        with tempfile.TemporaryDirectory() as directory:
            callbacks = []
            locked = [True]
            install = mock.Mock()
            update = updater.ReleaseUpdater({}, directory, '1.0.0', callbacks.append, lambda: locked[0], install)
            update.enabled = True
            with mock.patch.object(updater.requests, 'Session') as session, mock.patch.object(updater, 'download_asset'):
                session.return_value.__enter__.return_value.get.return_value.json.return_value = release()
                update.check_once()
            self.assertEqual(len(callbacks), 1)
            locked[0] = False
            callbacks.pop()()
            install.assert_not_called()
            self.assertFalse(update.in_progress)

    def test_readiness_marker_only_written_in_config_directory_when_healthy(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory)/('update-health-'+'a'*32+'.ready')
            with mock.patch.object(sys, 'argv', ['agent.exe', '--update-health', str(marker)]):
                updater.signal_ready(directory, True, False)
                self.assertFalse(marker.exists())
                updater.signal_ready(directory, True, True)
                self.assertEqual(marker.read_text(), 'ready')
            with mock.patch.object(sys, 'argv', ['agent.exe', '--update-health', str(Path(directory)/'config.json')]):
                updater.signal_ready(directory, True, True)
                self.assertFalse((Path(directory)/'config.json').exists())

    def test_installer_escapes_paths_and_restores_previous_executable_after_failed_readiness(self):
        script = updater.installer_script("C:/Staff's PC/SIPANTAU.exe", 'staged.exe', 'marker', 123, 'a'*64, 'v1.2.0')
        self.assertIn("Staff''s PC", script)
        self.assertNotIn('(Get-FileHash ', script)
        self.assertIn('[System.Security.Cryptography.SHA256]::Create()', script)
        self.assertIn('GetProcessById(123)', script)
        self.assertIn('WaitForExit(60000)', script)
        self.assertIn("Save-Outcome 'rolled-back'", script)
        self.assertIn('[System.IO.File]::Replace($staged, $target, $backup)', script)
        self.assertIn('[System.IO.File]::Copy($backup, $target, $true)', script)
        self.assertIn('taskkill.exe', script)
        self.assertIn('phase = $phase; error = $detail', script)

    def test_manual_check_uses_verified_public_metadata_without_download_or_install(self):
        with mock.patch.object(updater.requests, 'Session') as session, mock.patch.object(updater, 'download_asset') as download:
            client = session.return_value.__enter__.return_value
            client.get.return_value.json.return_value = release()
            selected = updater.check_for_update('1.0.0')
            self.assertEqual(selected[0], 'v1.2.0')
            client.get.assert_called_once_with(
                f'https://api.github.com/repos/{updater.REPOSITORY}/releases/latest', timeout=(5, 15), verify=True)
            client.headers.update.assert_called_once_with(
                {'Accept': 'application/vnd.github+json', 'User-Agent': 'SIPANTAU-Updater'})
            download.assert_not_called()
            client.get.return_value.json.return_value = release(tag='v1.0.0')
            self.assertIsNone(updater.check_for_update('1.0.0'))

    def test_manual_check_rejects_untrusted_assets_and_non_stable_metadata(self):
        bad_asset = release()
        bad_asset['assets'][0]['browser_download_url'] = 'https://other.example/SIPANTAU.exe'
        for data in [bad_asset, [], {**release(), 'prerelease': True}, {**release(), 'draft': True}]:
            with self.subTest(kind=type(data).__name__), mock.patch.object(updater.requests, 'Session') as session:
                session.return_value.__enter__.return_value.get.return_value.json.return_value = data
                with self.assertRaises(ValueError):
                    updater.check_for_update('1.0.0')

    def test_manual_check_propagates_offline_and_http_errors(self):
        for error in [updater.requests.ConnectionError('offline'), updater.requests.HTTPError('not found')]:
            with self.subTest(error=type(error).__name__), mock.patch.object(updater.requests, 'Session') as session:
                session.return_value.__enter__.return_value.get.side_effect = error
                with self.assertRaises(type(error)):
                    updater.check_for_update('1.0.0')

    def test_folder_build_cannot_enable_executable_only_updates(self):
        from types import SimpleNamespace
        with mock.patch.object(updater, 'os', SimpleNamespace(name='nt')), mock.patch.object(updater.sys, 'frozen', True, create=True):
            with mock.patch.object(updater, 'BUILD_FORMAT', 'onedir'):
                update = updater.ReleaseUpdater({'auto_update': True}, '.', '1.0.0', mock.Mock(), lambda: True, mock.Mock())
                self.assertFalse(update.supported)
                with mock.patch.object(updater.requests, 'Session') as network:
                    update.check_once()
                    network.assert_not_called()
            with mock.patch.object(updater, 'BUILD_FORMAT', 'onefile'):
                update = updater.ReleaseUpdater({'auto_update': True}, '.', '1.0.0', mock.Mock(), lambda: True, mock.Mock())
                self.assertTrue(update.enabled)

    def test_folder_only_releases_are_visible_manually_and_skipped_by_executable_updater(self):
        data = release()
        asset = data['assets'][0]
        asset['name'] = 'SIPANTAU-portable.zip'
        asset['browser_download_url'] = asset['browser_download_url'].replace('SIPANTAU.exe', asset['name'])
        for package_format in ('onedir', 'onefile'):
            with self.subTest(package_format=package_format), mock.patch.object(updater.requests, 'Session') as session, mock.patch.object(updater, 'BUILD_FORMAT', package_format):
                client = session.return_value.__enter__.return_value
                client.get.return_value.json.return_value = data
                self.assertEqual(updater.check_for_update('1.0.0')[1]['name'], 'SIPANTAU-portable.zip')
                self.assertIsNone(updater.fetch_update(client, '1.0.0'))
                asset['digest'] = ''
                with self.assertRaises(ValueError):
                    updater.check_for_update('1.0.0')
                asset['digest'] = 'sha256:' + 'a'*64
