import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import zipfile

from app_identity import EXECUTABLE_STEM, EXECUTABLE_NAME, PORTABLE_NAME
from scripts.build_windows import DEFAULT_CONFIG, archive_folder
from scripts import release_artifacts as artifacts
from scripts import github_release as github

TAG = 'v1.7.0'


def fixture(root, legacy='included'):
    folder = root / 'dist' / 'onedir' / EXECUTABLE_STEM
    (folder / '_internal').mkdir(parents=True)
    (folder / EXECUTABLE_NAME).write_bytes(b'MZ harmless test program')
    (folder / '_internal' / 'runtime.dll').write_bytes(b'test runtime')
    (folder / 'config.json').write_text(json.dumps(DEFAULT_CONFIG))
    archive_folder(root)
    (root / 'build-reports').mkdir()
    release = root / 'release'
    (release / 'SIPANTAU-portable.zip').write_bytes((release / PORTABLE_NAME).read_bytes())

    def scan(name, files):
        rows = []
        for path, relative in files:
            row = artifacts.file_record(path)
            rows.append({'path': relative, 'size': row['size'], 'sha256': row['sha256']})
        (release / name).write_text(json.dumps({'outcome': 'passed', 'scan_exit_code': 0, 'files': rows}))

    def smoke(name, package_format):
        (release / name).write_text(json.dumps({'ok': True, 'frozen': True, 'version': '1.7.0', 'build_format': package_format}))

    scan('portable-scan.json', [(path, path.relative_to(folder).as_posix()) for path in folder.rglob('*') if path.is_file()])
    scan('portable-archive-scan.json', [(release / PORTABLE_NAME, PORTABLE_NAME)])
    smoke('portable-smoke.json', 'onedir')
    if legacy == 'included':
        for name in (EXECUTABLE_NAME, 'SIPANTAU.exe'):
            (release / name).write_bytes(b'MZ harmless one-file program')
        scan('legacy-scan.json', [(release / EXECUTABLE_NAME, EXECUTABLE_NAME)])
        smoke('legacy-smoke.json', 'onefile')
    return release


class PublicationTests(unittest.TestCase):
    def test_exact_assets_and_notes_for_complete_and_folder_only_releases(self):
        for legacy in artifacts.LEGACY_STATES:
            with self.subTest(legacy=legacy), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                release = fixture(root, legacy)
                records = artifacts.prepare_release(root, TAG, legacy)
                self.assertEqual(records, artifacts.verify_release(root, TAG))
                self.assertEqual({row['name'] for row in records}, {path.name for path in release.iterdir()})
                notes = (root / 'build-reports' / 'release-notes.md').read_text()
                self.assertIn('autorun=true', notes)
                self.assertIn('do not verify', notes)
                if legacy == 'included':
                    self.assertIn(EXECUTABLE_NAME + ' is included', notes)
                else:
                    self.assertIn('OMITTED (' + legacy + ')', notes)
                    self.assertNotIn(EXECUTABLE_NAME + ' is included', notes)
                    self.assertNotIn('SIPANTAU.exe', {row['name'] for row in records})

    def test_reports_alone_cannot_be_published(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'release').mkdir()
            (root / 'release' / 'portable-scan.json').write_text('{"outcome":"passed"}')
            with self.assertRaisesRegex(ValueError, 'missing='):
                artifacts.prepare_release(root, TAG, 'scan-failed')

    def test_missing_modified_unexpected_and_unapproved_files_block(self):
        changes = ('missing-zip', 'missing-alias', 'changed-zip', 'changed-alias',
                   'private-config', 'extra-file', 'omitted-executable', 'missing-executable')
        for change in changes:
            with self.subTest(change=change), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                release = fixture(root)
                artifacts.prepare_release(root, TAG, 'included')
                if change == 'missing-zip': (release / PORTABLE_NAME).unlink()
                elif change == 'missing-alias': (release / 'SIPANTAU-portable.zip').unlink()
                elif change == 'changed-zip': (release / PORTABLE_NAME).write_bytes(b'changed')
                elif change == 'changed-alias': (release / 'SIPANTAU.exe').write_bytes(b'changed')
                elif change == 'private-config': (release / 'config.json').write_text('{"master_password":"private"}')
                elif change == 'extra-file': (release / 'unscanned.exe').write_bytes(b'MZ')
                elif change == 'missing-executable': (release / EXECUTABLE_NAME).unlink()
                else:
                    with self.assertRaises(ValueError):
                        artifacts.prepare_release(root, TAG, 'scan-failed')
                    continue
                with self.assertRaises(ValueError):
                    artifacts.verify_release(root, TAG)

    def test_zip_contents_must_match_the_scanned_folder_even_if_zip_hash_matches(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            release = fixture(root)
            with zipfile.ZipFile(release / PORTABLE_NAME, 'a') as archive:
                archive.writestr(EXECUTABLE_STEM + '/unscanned.dll', b'unscanned')
            (release / 'SIPANTAU-portable.zip').write_bytes((release / PORTABLE_NAME).read_bytes())
            report = json.loads((release / 'portable-archive-scan.json').read_text())
            row = artifacts.file_record(release / PORTABLE_NAME)
            report['files'] = [{'path': PORTABLE_NAME, 'size': row['size'], 'sha256': row['sha256']}]
            (release / 'portable-archive-scan.json').write_text(json.dumps(report))
            with self.assertRaisesRegex(ValueError, 'ZIP contents'):
                artifacts.prepare_release(root, TAG, 'included')

    def test_failed_scans_and_wrong_smoke_reports_block(self):
        for name, field, value in [('portable-scan.json', 'outcome', 'blocked'),
                                   ('legacy-scan.json', 'scan_exit_code', 2),
                                   ('portable-smoke.json', 'ok', False),
                                   ('legacy-smoke.json', 'version', '1.6.2'),
                                   ('portable-smoke.json', 'frozen', False)]:
            with self.subTest(name=name, field=field), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                release = fixture(root)
                data = json.loads((release / name).read_text())
                data[field] = value
                (release / name).write_text(json.dumps(data))
                with self.assertRaises(ValueError):
                    artifacts.prepare_release(root, TAG, 'included')

    def test_manifest_checksums_and_version_are_rechecked(self):
        for change in ('manifest', 'checksums', 'tag'):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                release = fixture(root)
                artifacts.prepare_release(root, TAG, 'included')
                if change == 'manifest':
                    data = json.loads((release / 'build-manifest.json').read_text())
                    data['files'].pop()
                    (release / 'build-manifest.json').write_text(json.dumps(data))
                elif change == 'checksums':
                    (release / 'SHA256SUMS.txt').write_text('wrong')
                with self.assertRaises(ValueError):
                    artifacts.verify_release(root, 'v9.9.9' if change == 'tag' else TAG)

    def test_workflow_outputs_use_explicit_paths_and_describe_omissions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture(root, 'scan-failed')
            output, summary = root / 'output', root / 'summary'
            with mock.patch.dict('os.environ', GITHUB_OUTPUT=str(output), GITHUB_STEP_SUMMARY=str(summary)):
                records = artifacts.prepare_release(root, TAG, 'scan-failed')
            lines = output.read_text().splitlines()
            self.assertEqual(lines[1:-1], ['release/' + row['name'] for row in records])
            self.assertNotIn('*', output.read_text())
            self.assertIn('OMITTED (scan-failed)', summary.read_text())


class GitHubPublicationTests(unittest.TestCase):
    def test_published_and_immutable_tags_are_never_modified(self):
        for release in ({'id': 1, 'draft': False}, {'id': 1, 'draft': True, 'immutable': True}):
            client = mock.Mock()
            client.call.return_value = release
            with self.assertRaisesRegex(RuntimeError, 'new version tag'):
                github.prepare_draft(client, TAG)
            self.assertEqual(client.call.call_count, 1)
            client.assets.assert_not_called()

    def test_new_tag_does_not_mutate_github_and_draft_retry_clears_old_assets(self):
        client = mock.Mock()
        client.call.return_value = None
        github.prepare_draft(client, TAG)
        self.assertEqual(client.call.call_count, 1)
        client.reset_mock()
        client.call.return_value = {'id': 7, 'draft': True}
        client.assets.return_value = [{'id': 11, 'name': 'SIPANTAU.exe'}, {'id': 12, 'name': 'old-report.json'}]
        github.prepare_draft(client, TAG)
        self.assertEqual(client.call.call_args_list[1:], [mock.call('DELETE', '/releases/assets/11'), mock.call('DELETE', '/releases/assets/12')])

    def test_publication_requires_exact_uploaded_asset_digests(self):
        records = [{'name': PORTABLE_NAME, 'size': 100, 'sha256': 'a' * 64}]
        good = {'name': PORTABLE_NAME, 'size': 100, 'digest': 'sha256:' + 'a' * 64, 'state': 'uploaded'}
        cases = ('valid', 'extra', 'missing', 'hash', 'size', 'unverified', 'uploading', 'wrong-tag', 'already-public')
        for case in cases:
            with self.subTest(case=case), mock.patch.object(github, 'verify_release', return_value=records):
                client = mock.Mock()
                assets = [copy.deepcopy(good)]
                release = {'tag_name': TAG, 'draft': True}
                if case == 'extra': assets.append({**good, 'name': 'stale.exe'})
                elif case == 'missing': assets.clear()
                elif case == 'hash': assets[0]['digest'] = 'sha256:' + 'b' * 64
                elif case == 'size': assets[0]['size'] = 101
                elif case == 'unverified': assets[0]['digest'] = None
                elif case == 'uploading': assets[0]['state'] = 'new'
                elif case == 'wrong-tag': release['tag_name'] = 'v1.6.2'
                elif case == 'already-public': release['draft'] = False
                client.assets.return_value = assets
                client.call.side_effect = [release, {'draft': False}]
                if case == 'valid':
                    github.publish_verified(client, Path('/unused'), TAG, 7)
                    self.assertEqual(client.call.call_args.args, ('PATCH', '/releases/7'))
                else:
                    with self.assertRaises(RuntimeError):
                        github.publish_verified(client, Path('/unused'), TAG, 7)
                    self.assertFalse(any(call.args[0] == 'PATCH' for call in client.call.call_args_list))

    def test_api_errors_are_not_treated_as_a_missing_release(self):
        session = mock.Mock()
        session.headers = {}
        session.request.return_value = mock.Mock(status_code=403)
        client = github.ReleaseClient('andiharka/sipantau', 'private-token', session)
        with self.assertRaisesRegex(RuntimeError, 'HTTP 403') as caught:
            client.call('GET', '/releases/tags/' + TAG, missing_ok=True)
        self.assertNotIn('private-token', str(caught.exception))
        self.assertEqual(session.request.call_args.kwargs['timeout'], 30)
