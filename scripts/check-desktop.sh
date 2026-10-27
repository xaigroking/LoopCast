#!/usr/bin/env bash
set -euo pipefail
root_dir=$(cd "$(dirname "$0")/.." && pwd)
work_dir=$(mktemp -d)
trap 'rm -rf "$work_dir"' EXIT
python -m venv --without-pip "$work_dir/installed"
python -m pip --python "$work_dir/installed/bin/python" install --require-hashes -r "$root_dir/requirements-runtime.lock"
python -m pip --python "$work_dir/installed/bin/python" install --no-deps "$root_dir"/dist/*.whl
export LOOPCAST_DESKTOP_PYTHON="$work_dir/installed/bin/python"
export LOOPCAST_DESKTOP_NEXT="$root_dir/frontend/.next/standalone"
export LOOPCAST_DESKTOP_REVISION
LOOPCAST_DESKTOP_REVISION=$(git -C "$root_dir" rev-parse HEAD)
node --test "$root_dir/desktop/tests/production.integration.mjs"
export LOOPCAST_DESKTOP_NODE
LOOPCAST_DESKTOP_NODE=$(command -v node)
"${LOOPCAST_BROWSER_PYTHON:-python}" -I "$root_dir/tests/browser/check_desktop_restart.py"
