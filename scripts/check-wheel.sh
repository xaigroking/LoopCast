#!/usr/bin/env bash
set -euo pipefail

python scripts/check-ui-assets.py
python scripts/check-architecture.py
python -m build --no-isolation
python scripts/check-licenses.py
work_dir=$(mktemp -d)
trap 'rm -rf "$work_dir"' EXIT
python -m venv --without-pip "$work_dir/installed"
python -m pip --python "$work_dir/installed/bin/python" install --require-hashes -r requirements-runtime.lock
python -m pip --python "$work_dir/installed/bin/python" install --no-deps dist/*.whl
python -m pip --python "$work_dir/installed/bin/python" check
sha256sum dist/*.whl
cd "$work_dir"
"$work_dir/installed/bin/python" -I -m loopcast --help
"$work_dir/installed/bin/python" -I -m loopcast --version
"$work_dir/installed/bin/python" -I - <<'PY'
import subprocess
import sys
from importlib.metadata import version
from pathlib import Path
import loopcast

if not Path(loopcast.__file__).is_relative_to(sys.prefix):
    raise SystemExit("Package was imported from outside the installation environment")
result = subprocess.run([sys.executable, "-I", "-m", "loopcast", "--version"], capture_output=True, text=True, check=True)
if result.stdout.strip() != f"loopcast {version('loopcast')}":
    raise SystemExit("CLI version differs from installed package metadata")
invalid = subprocess.run([sys.executable, "-I", "-m", "loopcast", "--unknown-option"], capture_output=True, text=True)
if invalid.returncode != 2 or "unrecognized arguments" not in invalid.stderr:
    raise SystemExit("CLI did not reject an unsupported option")
print("Installed package location, version and argument validation passed")
import json
import selectors
import signal
import tempfile
import urllib.request
import urllib.error

with tempfile.TemporaryDirectory() as directory:
    process = subprocess.Popen([sys.executable, '-I', '-m', 'loopcast', 'serve', '--port', '0', '--data-dir', directory], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            if not selector.select(10):
                raise SystemExit('No service readiness signal')
        ready = process.stdout.readline().strip()
        if not ready.startswith('READY '):
            raise SystemExit('Invalid service readiness signal')
        base = ready.split(' ', 1)[1]
        for path in ['/healthz', '/v1/capabilities', '/openapi.json']:
            with urllib.request.urlopen(base + path, timeout=5) as response:
                if response.status != 200 or not response.read():
                    raise SystemExit('Installed service resource is unavailable')
        for path in ['/v1', '/v1/unknown']:
            try:
                urllib.request.urlopen(base + path, timeout=5)
            except urllib.error.HTTPError as error:
                payload=json.load(error)
                if error.code != 404 or payload.get('error',{}).get('code') != 'NOT_FOUND':
                    raise SystemExit('Unknown API path did not return a structured 404')
            else:
                raise SystemExit('Unknown API path unexpectedly succeeded')
        print('Installed ASGI readiness, OpenAPI, and structured API 404 checks passed')
    finally:
        process.terminate()
        process.wait(timeout=12)
        process.stdout.close()
        process.stderr.close()
        if process.returncode not in (0, -signal.SIGTERM):
            raise SystemExit('Service did not stop cleanly')
        if process.returncode == -signal.SIGTERM:
            print('Uvicorn completed lifespan shutdown before restoring its SIGTERM exit status')

PY
