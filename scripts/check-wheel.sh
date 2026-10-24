#!/usr/bin/env bash
set -euo pipefail

python -m build --no-isolation
work_dir=$(mktemp -d)
trap 'rm -rf "$work_dir"' EXIT
python -m venv --without-pip "$work_dir/installed"
python -m pip --python "$work_dir/installed/bin/python" install --no-deps dist/*.whl
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
PY
