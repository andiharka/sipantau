"""Verify the exact approved publication set and generate outcome-specific notes."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app_identity import APP_NAME, EXECUTABLE_STEM, EXECUTABLE_NAME, PORTABLE_NAME
from scripts.build_windows import DEFAULT_CONFIG, parse_version, write_manifest

REPORT_NAMES = {'portable-scan.json', 'portable-archive-scan.json', 'legacy-scan.json',
                'portable-smoke.json', 'legacy-smoke.json'}
LEGACY_STATES = ('included', 'build-failed', 'smoke-failed', 'scan-failed')


def digest(stream):
    result = hashlib.sha256()
    for chunk in iter(lambda: stream.read(128 * 1024), b''):
        result.update(chunk)
    return result.hexdigest()


def file_record(file):
    if file.is_symlink() or not file.is_file():
        raise ValueError('Missing or invalid release file: ' + file.name)
    with file.open('rb') as stream:
        return {'name': file.name, 'size': file.stat().st_size, 'sha256': digest(stream)}


def read_scan(release, name):
    data = json.loads((release / name).read_text(encoding='utf-8-sig'))
    if data.get('outcome') != 'passed' or data.get('scan_exit_code') != 0 or not data.get('files'):
        raise ValueError('Required Defender scan did not pass: ' + name)
    return data['files']


def matches_scan(file, row):
    record = file_record(file)
    if record['size'] != row['size'] or record['sha256'] != row['sha256']:
        raise ValueError('Release bytes differ from scanned bytes: ' + file.name)


def validate_payload(root, tag, legacy):
    parse_version(tag)
    if legacy not in LEGACY_STATES:
        raise ValueError('Unknown legacy package outcome')
    release = Path(root) / 'release'
    required = {PORTABLE_NAME, 'SIPANTAU-portable.zip', 'config.json',
                'portable-scan.json', 'portable-archive-scan.json', 'portable-smoke.json'}
    if legacy == 'included':
        required |= {EXECUTABLE_NAME, 'SIPANTAU.exe', 'legacy-scan.json', 'legacy-smoke.json'}
    allowed = required | REPORT_NAMES | {'build-manifest.json', 'SHA256SUMS.txt'}
    actual = {path.name for path in release.iterdir()}
    if not required <= actual or not actual <= allowed:
        raise ValueError(f'Release file set mismatch: missing={sorted(required - actual)}, unexpected={sorted(actual - allowed)}')
    for name in actual:
        file_record(release / name)
    if json.loads((release / 'config.json').read_text(encoding='utf-8-sig')) != DEFAULT_CONFIG:
        raise ValueError('Release config is not the public template')
    archive_rows = read_scan(release, 'portable-archive-scan.json')
    if len(archive_rows) != 1 or archive_rows[0]['path'] != PORTABLE_NAME:
        raise ValueError('Portable archive scan does not identify the required ZIP')
    matches_scan(release / PORTABLE_NAME, archive_rows[0])
    matches_scan(release / 'SIPANTAU-portable.zip', archive_rows[0])
    folder_rows = read_scan(release, 'portable-scan.json')
    expected = {EXECUTABLE_STEM + '/' + row['path']: row for row in folder_rows}
    if len(expected) != len(folder_rows):
        raise ValueError('Duplicate paths in folder scan report')
    if (EXECUTABLE_STEM + '/' + EXECUTABLE_NAME not in expected or
            not any(name.startswith(EXECUTABLE_STEM + '/_internal/') for name in expected)):
        raise ValueError('Folder scan does not contain a complete executable and runtime')
    with zipfile.ZipFile(release / PORTABLE_NAME) as archive:
        entries = [item for item in archive.infolist() if not item.is_dir()]
        if len(entries) != len(expected) or {entry.filename for entry in entries} != set(expected):
            raise ValueError('Portable ZIP contents differ from the scanned folder')
        for entry in entries:
            row = expected[entry.filename]
            with archive.open(entry) as stream:
                if entry.file_size != row['size'] or digest(stream) != row['sha256']:
                    raise ValueError('Portable ZIP contains changed bytes: ' + entry.filename)
    formats = [('portable-smoke.json', 'onedir')]
    if legacy == 'included':
        rows = read_scan(release, 'legacy-scan.json')
        if len(rows) != 1 or rows[0]['path'] != EXECUTABLE_NAME:
            raise ValueError('Single-file scan does not identify the expected executable')
        matches_scan(release / EXECUTABLE_NAME, rows[0])
        matches_scan(release / 'SIPANTAU.exe', rows[0])
        formats.append(('legacy-smoke.json', 'onefile'))
    for name, package_format in formats:
        data = json.loads((release / name).read_text(encoding='utf-8'))
        if (data.get('ok') is not True or data.get('frozen') is not True
                or data.get('version') != tag[1:] or data.get('build_format') != package_format):
            raise ValueError('Required packaged smoke check did not pass: ' + name)
    return sorted(actual - {'build-manifest.json', 'SHA256SUMS.txt'})


def verify_release(root, tag):
    release = Path(root) / 'release'
    manifest = json.loads((release / 'build-manifest.json').read_text(encoding='utf-8'))
    if manifest['version'] != tag:
        raise ValueError('Manifest version differs from the release tag')
    if os.environ.get('GITHUB_SHA') and manifest['commit'] != os.environ['GITHUB_SHA']:
        raise ValueError('Manifest commit differs from the current workflow')
    names = validate_payload(root, tag, manifest['packages']['legacy'])
    records = [file_record(release / name) for name in names]
    if manifest['files'] != records:
        raise ValueError('Manifest file set or hashes differ from current release files')
    manifest_record = file_record(release / 'build-manifest.json')
    checksums = ''.join(f"{row['sha256']}  {row['name']}\n" for row in [*records, manifest_record])
    if (release / 'SHA256SUMS.txt').read_text(encoding='ascii') != checksums:
        raise ValueError('SHA256SUMS.txt differs from the verified publication set')
    return [*records, manifest_record, file_record(release / 'SHA256SUMS.txt')]


def release_notes(tag, legacy):
    optional = (f'Single-file package: {EXECUTABLE_NAME} is included. SIPANTAU.exe is its identical compatibility alias.'
                if legacy == 'included' else
                f'Single-file package OMITTED ({legacy}). No standalone executable is published; executable-only automatic updaters will skip this release. Install the complete folder package manually.')
    return f'''{APP_NAME} — {tag} (SIPANTAU project)

Recommended download: {PORTABLE_NAME}. Extract the entire folder and keep _internal beside {EXECUTABLE_NAME}. SIPANTAU-portable.zip is an identical compatibility alias of this ZIP.

{optional}

Configure server_url, api_key and a private master_password before first use. The separate config.json is a public template, not saved workstation settings. When upgrading, retain your existing config.json and state.json. Folder updates require replacing the complete package.

Automatic startup: set autorun=true, then launch once as Administrator while signed in to the same administrator account that will use the PC. Confirm “Pendaftaran saat masuk: Terdaftar” in settings. Startup occurs after that account signs in following a restart. Standard-user sessions require a separate privileged-service design.

Every included package passed its required Defender scan and packaged smoke check. Smoke checks load bundled dependencies, the icon and a hidden Tk window; they do not verify real kiosk controls or reboot/sign-in behavior. Windows workstation acceptance is still required.

SHA256SUMS.txt, build-manifest.json and reports describe this exact asset set. No Authenticode signing is configured. A clean scan is a point-in-time result, not a guarantee against future or other-engine detections.
'''


def prepare_release(root, tag, legacy):
    root = Path(root)
    validate_payload(root, tag, legacy)
    write_manifest(root, tag, packages={'portable': 'included', 'legacy': legacy})
    records = verify_release(root, tag)
    notes = release_notes(tag, legacy)
    (root / 'build-reports' / 'release-notes.md').write_text(notes, encoding='utf-8')
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a', encoding='utf-8') as stream:
            stream.write(notes + '\n')
    if os.environ.get('GITHUB_OUTPUT'):
        with open(os.environ['GITHUB_OUTPUT'], 'a', encoding='utf-8') as stream:
            stream.write('files<<VERIFIED_RELEASE_FILES\n')
            stream.writelines('release/' + row['name'] + '\n' for row in records)
            stream.write('VERIFIED_RELEASE_FILES\n')
    return records


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('prepare', 'verify'))
    parser.add_argument('--tag', required=True)
    parser.add_argument('--legacy', choices=LEGACY_STATES)
    args = parser.parse_args()
    root = Path(__file__).resolve().parent.parent
    if args.action == 'prepare':
        if not args.legacy:
            parser.error('--legacy is required for prepare')
        prepare_release(root, args.tag, args.legacy)
    else:
        verify_release(root, args.tag)
