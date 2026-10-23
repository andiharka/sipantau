"""Launch a built agent's isolated smoke mode from an unrelated working directory."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile

import psutil


def check_package(executable, report, tag, package_format):
    executable, report = Path(executable).resolve(), Path(report).resolve()
    report.parent.mkdir(parents=True, exist_ok=True)
    report.unlink(missing_ok=True)
    with tempfile.TemporaryDirectory(prefix='agent-smoke-') as working:
        process = subprocess.Popen([str(executable), '--smoke-test', str(report)], cwd=working)
        try:
            code = process.wait(timeout=60)
        except subprocess.TimeoutExpired:
            try:
                children = psutil.Process(process.pid).children(recursive=True)
                for child in children:
                    try:
                        child.kill()
                    except psutil.NoSuchProcess:
                        pass
                process.kill()
                process.wait(timeout=10)
                psutil.wait_procs(children, timeout=10)
            except psutil.NoSuchProcess:
                pass
            raise RuntimeError('Packaged startup smoke check timed out') from None
    if code != 0:
        raise RuntimeError(f'Packaged startup smoke check exited with {code}')
    data = json.loads(report.read_text(encoding='utf-8'))
    if (data.get('ok') is not True or data.get('frozen') is not True
            or data.get('version') != tag.removeprefix('v')
            or data.get('build_format') != package_format):
        raise RuntimeError('Packaged smoke report does not match the expected build')
    return data


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--executable', required=True)
    parser.add_argument('--report', required=True)
    parser.add_argument('--tag', required=True)
    parser.add_argument('--format', choices=('onedir', 'onefile'), required=True)
    args = parser.parse_args()
    check_package(args.executable, args.report, args.tag, args.format)
