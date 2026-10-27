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
python - "$work_dir" <<'PY'
import hashlib,json,os,pathlib,subprocess,sys
root=pathlib.Path(sys.argv[1]);tools=pathlib.Path(os.environ['LOOPCAST_MEDIA_TOOLS'])
settings={key:str(tools/key) for key in ['ffmpeg','ffprobe']}
settings.update({key+'_sha256':hashlib.sha256((tools/key).read_bytes()).hexdigest() for key in ['ffmpeg','ffprobe']})
(root/'media.json').write_text(json.dumps(settings))
subprocess.run([str(tools/'ffmpeg'),'-v','error','-f','lavfi','-i','color=c=blue:s=64x64:d=0.2','-an','-c:v','libvpx-vp9',str(root/'sample.mp4')],check=True)
PY
export LOOPCAST_DESKTOP_MEDIA_CONFIG="$work_dir/media.json"
export LOOPCAST_DESKTOP_SAMPLE="$work_dir/sample.mp4"
openbox --sm-disable >"$work_dir/display.log" 2>&1 &
wm_pid=$!
node --test "$root_dir/desktop/tests/electron.integration.mjs"
