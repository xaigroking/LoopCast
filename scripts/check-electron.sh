#!/usr/bin/env bash
set -euo pipefail
root_dir=$(cd "$(dirname "$0")/.." && pwd)
work_dir=$(mktemp -d)
wm_pid=
cleanup() {
  if [[ -n "$wm_pid" ]]; then kill "$wm_pid" 2>/dev/null || true; wait "$wm_pid" 2>/dev/null || true; fi
  rm -rf "$work_dir"
}
trap cleanup EXIT
python -m venv --without-pip "$work_dir/installed"
python -m pip --python "$work_dir/installed/bin/python" install --require-hashes -r "$root_dir/requirements-runtime.lock"
python -m pip --python "$work_dir/installed/bin/python" install --no-deps "$root_dir"/dist/*.whl
export LOOPCAST_DESKTOP_PYTHON="$work_dir/installed/bin/python"
export LOOPCAST_DESKTOP_NEXT="$root_dir/frontend/.next/standalone"
export LOOPCAST_DESKTOP_REVISION
LOOPCAST_DESKTOP_REVISION=$(git -C "$root_dir" rev-parse HEAD)
openbox --sm-disable >"$work_dir/display.log" 2>&1 &
wm_pid=$!
node --test "$root_dir/desktop/tests/electron.integration.mjs"
