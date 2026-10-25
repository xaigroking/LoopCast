#!/usr/bin/env bash
set -euo pipefail
root_dir=$(cd "$(dirname "$0")/.." && pwd)
work_dir=$(mktemp -d)
trap 'rm -rf "$work_dir"' EXIT
python -m venv --without-pip "$work_dir/installed"
python -m pip --python "$work_dir/installed/bin/python" install --require-hashes -r "$root_dir/requirements-web.lock"
python -m pip --python "$work_dir/installed/bin/python" install --no-deps "$root_dir"/dist/*.whl
python -m pip --python "$work_dir/installed/bin/python" check
cd "$work_dir"
"$work_dir/installed/bin/python" -I "$root_dir/tests/browser/check_console.py"

"$work_dir/installed/bin/python" -I "$root_dir/tests/browser/check_generation.py"

for suite in check_insecure.py check_preferences.py check_themes.py check_brand.py check_write_identity.py; do
  "$work_dir/installed/bin/python" -I "$root_dir/tests/browser/$suite"
done
