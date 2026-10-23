"""Stage/retry drafts and publish only after GitHub confirms the exact asset hashes."""
import argparse
import os
from pathlib import Path
import re
import sys
from urllib.parse import quote

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.build_windows import parse_version
from scripts.release_artifacts import verify_release


class ReleaseClient:
    def __init__(self, repository, token, session=None):
        if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository) or not token:
            raise ValueError('GitHub repository and token are required')
        self.base = 'https://api.github.com/repos/' + repository
        self.session = session or requests.Session()
        self.session.headers.update({'Authorization': 'Bearer ' + token,
                                     'Accept': 'application/vnd.github+json',
                                     'X-GitHub-Api-Version': '2026-03-10'})

    def call(self, method, path, missing_ok=False, **kwargs):
        result = self.session.request(method, self.base + path, timeout=30, **kwargs)
        if missing_ok and result.status_code == 404:
            return None
        if not 200 <= result.status_code < 300:
            raise RuntimeError(f'GitHub release API rejected {method} (HTTP {result.status_code})')
        return result.json() if result.content else None

    def assets(self, release_id):
        rows = []
        page = 1
        while True:
            batch = self.call('GET', f'/releases/{release_id}/assets?per_page=100&page={page}')
            rows.extend(batch)
            if len(batch) < 100:
                return rows
            page += 1


def prepare_draft(client, tag):
    parse_version(tag)
    release = client.call('GET', '/releases/tags/' + quote(tag, safe=''), missing_ok=True)
    if release is None:
        return
    if not release.get('draft') or release.get('immutable'):
        raise RuntimeError('This tag already has a published/immutable release. Use a new version tag; published assets will not be replaced.')
    # Clear only a retryable draft, so omitted optional executables cannot survive.
    for asset in client.assets(release['id']):
        client.call('DELETE', f"/releases/assets/{asset['id']}")


def publish_verified(client, root, tag, release_id):
    expected = {row['name']: row for row in verify_release(root, tag)}
    release = client.call('GET', f'/releases/{release_id}')
    if release.get('tag_name') != tag or not release.get('draft') or release.get('immutable'):
        raise RuntimeError('Only the expected mutable draft can be published')
    assets = client.assets(release_id)
    if len(assets) != len(expected) or {row['name'] for row in assets} != set(expected):
        raise RuntimeError('GitHub draft asset set differs from the verified local release')
    for asset in assets:
        row = expected[asset['name']]
        if (asset.get('state') != 'uploaded' or asset.get('size') != row['size']
                or asset.get('digest') != 'sha256:' + row['sha256']):
            raise RuntimeError('GitHub asset hash/size is unverified: ' + row['name'])
    result = client.call('PATCH', f'/releases/{release_id}',
                         json={'draft': False, 'prerelease': False, 'make_latest': 'true'})
    if result.get('draft') is not False:
        raise RuntimeError('GitHub did not confirm publication')
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('prepare', 'publish'))
    parser.add_argument('--tag', required=True)
    parser.add_argument('--release-id', type=int)
    args = parser.parse_args()
    client = ReleaseClient(os.environ.get('GITHUB_REPOSITORY', ''), os.environ.get('GH_TOKEN', ''))
    if args.action == 'prepare':
        prepare_draft(client, args.tag)
    else:
        if not args.release_id:
            parser.error('--release-id is required for publish')
        publish_verified(client, Path(__file__).resolve().parent.parent, args.tag, args.release_id)
        print('Published verified release: ' + args.tag)
