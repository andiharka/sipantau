"""Build transparent Windows packages; never copy a workstation's private config."""
import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import zipfile

# Direct script execution puts scripts/, rather than the project root, on sys.path.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app_identity import APP_NAME, EXECUTABLE_STEM, EXECUTABLE_NAME, PORTABLE_NAME

DEFAULT_CONFIG = {
    'api_key': 'SET_A_GENERATED_KEY_OF_AT_LEAST_32_CHARACTERS',
    'server_url': 'http://[IP_ADDRESS]', 'master_password': '', 'verify_ssl': True,
    'screenshot_jpeg_quality': 80, 'auto_update': False, 'autorun': False,
    'auto_lock_on_run': False, 'hostname': 'PC-00',
}
INSTALL_NOTES = f'''Folder package — keep {EXECUTABLE_NAME} and _internal together.

Extract into a new folder with administrator-controlled write permissions.
For an upgrade, stop the old agent and back up its directory first. Copy your
existing config.json and state.json into this new folder; do not overwrite them
with the template. Configure server_url, api_key and a private master_password
before starting a new installation. Start {EXECUTABLE_NAME} with administrator access.

The folder package supports manual update checks. Install its updates as a complete
folder; executable-only automatic replacement is disabled for this package.
'''


def parse_version(tag):
    if not re.fullmatch(r'v\d+\.\d+\.\d+', tag):
        raise ValueError('Expected stable vMAJOR.MINOR.PATCH tag')
    numbers = tuple(int(number) for number in tag[1:].split('.'))
    if any(number > 65535 for number in numbers):
        raise ValueError('Windows version components must fit in 16 bits')
    return numbers


def version_resource(tag):
    numbers = (*parse_version(tag), 0)
    return f'''VSVersionInfo(
  ffi=FixedFileInfo(filevers={numbers!r}, prodvers={numbers!r}, mask=0x3f,
                   flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[StringFileInfo([StringTable('040904B0', [
    StringStruct('CompanyName', 'SIPANTAU project'),
    StringStruct('FileDescription', {APP_NAME!r}),
    StringStruct('FileVersion', {tag[1:]!r}),
    StringStruct('ProductVersion', {tag[1:]!r}),
    StringStruct('ProductName', {APP_NAME!r}),
    StringStruct('OriginalFilename', {EXECUTABLE_NAME!r})
  ])]), VarFileInfo([VarStruct('Translation', [1033, 1200])])]
)
'''


def build(root, tag, package_format, run=subprocess.run):
    root = Path(root).resolve()
    parse_version(tag)
    if package_format not in ('onedir', 'onefile'):
        raise ValueError('Unsupported Windows package format')
    output = root/'dist'/package_format
    work = root/'build'/package_format
    work.mkdir(parents=True, exist_ok=True)
    resource = work/'windows-version.txt'
    resource.write_text(version_resource(tag), encoding='utf-8')
    version_path = root/'version.py'
    original = version_path.read_bytes()

    def clear_version_cache():
        for cache in (root/'__pycache__').glob('version.*.pyc'):
            cache.unlink()

    try:
        version_path.write_text(f'VERSION = {tag[1:]!r}\nBUILD_FORMAT = {package_format!r}\n', encoding='utf-8')
        clear_version_cache()
        run([sys.executable, '-m', 'PyInstaller', '--' + package_format, '--noconsole', '--uac-admin',
             '--noupx', '--clean', '--noconfirm', '--name', EXECUTABLE_STEM, '--version-file', str(resource),
             '--icon', str(root/'assets'/'app.ico'), '--add-data', str(root/'assets') + os.pathsep + 'assets',
             '--distpath', str(output), '--workpath', str(work), '--specpath', str(work),
             str(root/'agent.py')], cwd=root, check=True)
    finally:
        version_path.write_bytes(original)
        clear_version_cache()
    executable = output/EXECUTABLE_STEM/EXECUTABLE_NAME if package_format == 'onedir' else output/EXECUTABLE_NAME
    if not executable.is_file():
        raise RuntimeError('Build did not leave an executable; inspect the build and antivirus logs')
    if package_format == 'onedir':
        (executable.parent/'config.json').write_text(json.dumps(DEFAULT_CONFIG, indent=2) + '\n', encoding='utf-8')
        (executable.parent/'INSTALL.txt').write_text(INSTALL_NOTES, encoding='utf-8')
    return executable


def archive_folder(root):
    root = Path(root).resolve()
    folder = root/'dist'/'onedir'/EXECUTABLE_STEM
    if not (folder/EXECUTABLE_NAME).is_file() or not (folder/'_internal').is_dir():
        raise RuntimeError('Incomplete folder package')
    destination = root/'release'/PORTABLE_NAME
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Publish a separate template for single-file installations, without private settings.
    (destination.parent/'config.json').write_text(json.dumps(DEFAULT_CONFIG, indent=2) + '\n', encoding='utf-8')
    with zipfile.ZipFile(destination, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for file in sorted(folder.rglob('*')):
            if file.is_symlink():
                raise RuntimeError('Release package must not contain symlinks')
            if file.is_file():
                archive.write(file, file.relative_to(folder.parent).as_posix())
    return destination


def write_manifest(root, tag):
    root = Path(root).resolve()
    parse_version(tag)
    release = root/'release'
    files = []
    for file in sorted(release.iterdir()):
        if file.is_file() and file.name not in ('SHA256SUMS.txt', 'build-manifest.json'):
            with file.open('rb') as stream:
                digest = hashlib.sha256()
                for chunk in iter(lambda: stream.read(128 * 1024), b''):
                    digest.update(chunk)
            files.append({'name': file.name, 'size': file.stat().st_size, 'sha256': digest.hexdigest()})
    dependencies = sorted([{'name': distribution.metadata['Name'], 'version': distribution.version}
                           for distribution in importlib.metadata.distributions()], key=lambda item: item['name'].lower())
    manifest = {'version': tag, 'commit': os.environ.get('GITHUB_SHA'),
                'run_url': ('https://github.com/' + os.environ['GITHUB_REPOSITORY'] + '/actions/runs/' + os.environ['GITHUB_RUN_ID'])
                           if os.environ.get('GITHUB_REPOSITORY') and os.environ.get('GITHUB_RUN_ID') else None,
                'python': sys.version, 'dependencies': dependencies, 'files': files,
                'signing': 'No Authenticode signing configured; checksums are not publisher signatures'}
    path = release/'build-manifest.json'
    path.write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
    manifest_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    lines = [f"{item['sha256']}  {item['name']}\n" for item in files]
    lines.append(f'{manifest_hash}  build-manifest.json\n')
    (release/'SHA256SUMS.txt').write_text(''.join(lines), encoding='ascii')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('onedir', 'onefile', 'archive', 'manifest'))
    parser.add_argument('--tag', required=True)
    arguments = parser.parse_args()
    parse_version(arguments.tag)
    root = Path(__file__).resolve().parent.parent
    if arguments.action in ('onedir', 'onefile'):
        if os.name != 'nt':
            parser.error('Windows executables must be built on Windows')
        build(root, arguments.tag, arguments.action)
    elif arguments.action == 'archive':
        archive_folder(root)
    else:
        write_manifest(root, arguments.tag)


if __name__ == '__main__':
    main()
