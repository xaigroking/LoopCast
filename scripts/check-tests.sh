#!/usr/bin/env bash
set -euo pipefail
root_dir=$(cd "$(dirname "$0")/.." && pwd)
work_dir=$(mktemp -d)
trap 'rm -rf "$work_dir"' EXIT
python -m venv --without-pip "$work_dir/installed"
python -m pip --python "$work_dir/installed/bin/python" install --require-hashes -r "$root_dir/requirements-runtime.lock"
python -m pip --python "$work_dir/installed/bin/python" install --no-deps "$root_dir"/dist/*.whl
python -m pip --python "$work_dir/installed/bin/python" check
cd "$work_dir"
"$work_dir/installed/bin/python" -I -m unittest discover -s "$root_dir/tests/unit" -p 'test_*.py'
"$work_dir/installed/bin/python" -I -m unittest discover -s "$root_dir/tests/integration" -p 'test_*.py'
