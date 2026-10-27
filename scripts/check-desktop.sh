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
node --test "$root_dir/desktop/tests/production.integration.mjs"
export LOOPCAST_DESKTOP_NODE
LOOPCAST_DESKTOP_NODE=$(command -v node)
"${LOOPCAST_BROWSER_PYTHON:-python}" -I "$root_dir/tests/browser/check_desktop_restart.py"
