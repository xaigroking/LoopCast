#!/usr/bin/env bash
set -euo pipefail
root_dir=$(cd "$(dirname "$0")/.." && pwd)
python "$root_dir/scripts/check-ui-assets.py"
work_dir=$(mktemp -d)
backend_pid=
frontend_pid=
proxy_pid=
stop_services() {
  for pid in "$proxy_pid" "$frontend_pid" "$backend_pid"; do
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then kill "$pid"; fi
  done
  for pid in "$proxy_pid" "$frontend_pid" "$backend_pid"; do
    if [[ -n "$pid" ]]; then wait "$pid" 2>/dev/null || true; fi
  done
  rm -rf "$work_dir"
}
trap stop_services EXIT

python -m venv --without-pip "$work_dir/backend"
python -m pip --python "$work_dir/backend/bin/python" install --require-hashes -r "$root_dir/requirements-runtime.lock"
python -m pip --python "$work_dir/backend/bin/python" install --no-deps "$root_dir"/dist/*.whl
python -m pip --python "$work_dir/backend/bin/python" check
tar -xzf "$root_dir/dist/loopcast-web-standalone.tar.gz" -C "$work_dir"
generator_dir="$work_dir/generator"
mkdir -p "$generator_dir"
PYTHONPATH="$root_dir/src:$root_dir/tests/integration" python - "$generator_dir" <<'PY'
import json, pathlib, sys
from studio_support import configurations
directory = pathlib.Path(sys.argv[1])
configurations(directory)
PY

public_port=18889
data_dir="$work_dir/data"
mkdir -p "$data_dir"
"$work_dir/backend/bin/python" -I -m loopcast serve --host 127.0.0.1 --port 18888 --data-dir "$data_dir" --generator-config "$generator_dir/generator.json" --media-config "$generator_dir/media.json" --social-config "$generator_dir/social.json" --public-origin "http://127.0.0.1:$public_port" --trusted-proxy 127.0.0.1/32 >"$work_dir/backend.log" 2>&1 &
backend_pid=$!
(cd "$work_dir" && HOSTNAME=127.0.0.1 PORT=18887 NODE_ENV=production node server.js) >"$work_dir/frontend.log" 2>&1 &
frontend_pid=$!
if [[ -n "${LOOPCAST_CADDY:-}" ]]; then
  LOOPCAST_PUBLIC_ORIGIN="http://127.0.0.1:$public_port" LOOPCAST_API_UPSTREAM=127.0.0.1:18888 LOOPCAST_WEB_UPSTREAM=127.0.0.1:18887 \
    "$LOOPCAST_CADDY" run --adapter caddyfile --config "$root_dir/deploy/Caddyfile.example" >"$work_dir/proxy.log" 2>&1 &
  proxy_pid=$!
else
  python "$root_dir/tests/browser/reverse_proxy.py" 127.0.0.1 "$public_port" 127.0.0.1 18888 127.0.0.1 18887 >"$work_dir/proxy.log" 2>&1 &
  proxy_pid=$!
fi

for _ in $(seq 1 100); do
  if curl -fsS "http://127.0.0.1:$public_port/healthz" >/dev/null 2>&1 && curl -fsS "http://127.0.0.1:$public_port/" >/dev/null 2>&1; then break; fi
  sleep .1
done
curl -fsS "http://127.0.0.1:$public_port/healthz" | "$work_dir/backend/bin/python" -c 'import json,sys; assert json.load(sys.stdin)["ready"] is True'
browser_python=${LOOPCAST_BROWSER_PYTHON:-$(command -v python)}
LOOPCAST_BASE_URL="http://127.0.0.1:$public_port" "$browser_python" -I "$root_dir/tests/browser/check_console.py"

LOOPCAST_BASE_URL="http://127.0.0.1:$public_port" LOOPCAST_TEST_MEDIA="$generator_dir/sample.mp4" "$browser_python" -I "$root_dir/tests/browser/check_studio.py"

installed_bridge=$("$work_dir/backend/bin/python" -I -c 'import loopcast.social_bridge; print(loopcast.social_bridge.__file__)')
LOOPCAST_INSTALLED_BRIDGE="$installed_bridge" "$browser_python" -I "$root_dir/tests/browser/check_authorization.py"
LOOPCAST_BASE_URL="http://127.0.0.1:$public_port" "$browser_python" -I "$root_dir/tests/browser/check_feedback.py"
LOOPCAST_BASE_URL="http://127.0.0.1:$public_port" "$browser_python" -I "$root_dir/tests/browser/check_operations.py"

# Reuse the owner with an explicit isolated local-scheduling policy.
kill "$backend_pid"
wait "$backend_pid" 2>/dev/null || true
backend_pid=
python - "$generator_dir" <<'SCHEDULING'
import json,pathlib,sys
root=pathlib.Path(sys.argv[1]);path=root/'social.json';social=json.loads(path.read_text())
social['contract_profiles']['douyin'].extend([{'stable_subject':p,'label':p+' contract account'} for p in ('plan-douyin','plan-second','plan-third')])
path.write_text(json.dumps(social))
(root/'scheduling.json').write_text(json.dumps({'format':'loopcast.scheduling.v1','enabled':True,'allow_live':False}))
SCHEDULING
"$work_dir/backend/bin/python" -I -m loopcast serve --host 127.0.0.1 --port 18888 --data-dir "$data_dir" --generator-config "$generator_dir/generator.json" --media-config "$generator_dir/media.json" --social-config "$generator_dir/social.json" --schedule-config "$generator_dir/scheduling.json" --public-origin "http://127.0.0.1:$public_port" --trusted-proxy 127.0.0.1/32 >"$work_dir/scheduling-backend.log" 2>&1 &
backend_pid=$!
for _ in $(seq 1 100); do
  if curl -fsS "http://127.0.0.1:$public_port/healthz" >/dev/null 2>&1; then break; fi
  sleep .1
done
LOOPCAST_BASE_URL="http://127.0.0.1:$public_port" "$browser_python" -I "$root_dir/tests/browser/check_scheduling.py"

# Exercise the same installed distributions after an actual stopped-owner restore.
kill "$backend_pid"
wait "$backend_pid" 2>/dev/null || true
backend_pid=
backup_digest=$("$work_dir/backend/bin/python" -I -m loopcast backup create --data-dir "$data_dir" --output "$work_dir/private-backup" | "$work_dir/backend/bin/python" -c 'import json,sys; print(json.load(sys.stdin)["manifest_sha256"])')
"$work_dir/backend/bin/python" -I -m loopcast restore --backup "$work_dir/private-backup" --expected-manifest-sha256 "$backup_digest" --data-dir "$work_dir/restored" >"$work_dir/restore-report.json"
"$work_dir/backend/bin/python" -I -m loopcast serve --host 127.0.0.1 --port 18888 --data-dir "$work_dir/restored" --schedule-config "$generator_dir/scheduling.json" --generator-config "$generator_dir/generator.json" --media-config "$generator_dir/media.json" --social-config "$generator_dir/social.json" --public-origin "http://127.0.0.1:$public_port" --trusted-proxy 127.0.0.1/32 >"$work_dir/restored-backend.log" 2>&1 &
backend_pid=$!
for _ in $(seq 1 100); do
  if curl -fsS "http://127.0.0.1:$public_port/healthz" >/dev/null 2>&1; then break; fi
  sleep .1
done
curl -fsS "http://127.0.0.1:$public_port/healthz" | "$work_dir/backend/bin/python" -c 'import json,sys; assert json.load(sys.stdin)["ready"] is True'
LOOPCAST_BASE_URL="http://127.0.0.1:$public_port" LOOPCAST_EXPECT_RESTORE_GUARD=1 "$browser_python" -I "$root_dir/tests/browser/check_operations.py"

LOOPCAST_BASE_URL="http://127.0.0.1:$public_port" LOOPCAST_EXPECT_RESTORE_GUARD=1 "$browser_python" -I "$root_dir/tests/browser/check_scheduling.py"
