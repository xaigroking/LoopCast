#!/usr/bin/env bash
set -euo pipefail
root_dir=$(cd "$(dirname "$0")/.." && pwd)
cd "$root_dir"
git diff --quiet && git diff --cached --quiet || { echo "Build release from a clean committed source tree" >&2; exit 1; }

python scripts/check-ui-assets.py
python scripts/check-architecture.py
python -m build --no-isolation
python scripts/check-licenses.py
python -m pip install --require-hashes -r requirements-runtime.lock
python -m pip install --no-deps --force-reinstall dist/*.whl
python -m pip check
python -m loopcast.openapi > dist/openapi.json

node_version=$(node --version)
npm_version=$(npm --version)
[[ "$node_version" == "v24.22.1" ]] || { echo "Expected Node v24.22.1, found $node_version" >&2; exit 1; }
[[ "$npm_version" == "11.19.1" ]] || { echo "Expected npm 11.19.1, found $npm_version" >&2; exit 1; }
cd "$root_dir/frontend"
npm ci --no-audit --no-fund
npm run types:api
git -C "$root_dir" diff --exit-code -- frontend/src/lib/api-schema.ts
npm run typecheck
npm run check:architecture
npm run test:network
python "$root_dir/scripts/copy-npm-licenses.py"
LOOPCAST_BUILD_ID="$(git -C "$root_dir" rev-parse HEAD)" NEXT_TELEMETRY_DISABLED=1 npm run build

mkdir -p .next/standalone/public .next/standalone/.next/static
cp -a public/. .next/standalone/public/
cp -a .next/static/. .next/standalone/.next/static/
cd "$root_dir"

revision=$(git rev-parse HEAD)
wheel=$(find dist -maxdepth 1 -name 'loopcast-*.whl' -print -quit)
web_archive=dist/loopcast-web-standalone.tar.gz
tar -czf "$web_archive" -C frontend/.next/standalone .
python scripts/check-licenses.py --frontend-archive
python - "$revision" "$wheel" "$web_archive" <<'PY'
import hashlib, json, pathlib, sys
revision, wheel, web = sys.argv[1:]
def digest(path):
    value = hashlib.sha256()
    with open(path, "rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()
manifest = {
    "source_revision": revision,
    "backend_wheel": {"file": pathlib.Path(wheel).name, "sha256": digest(wheel)},
    "frontend_standalone": {"file": pathlib.Path(web).name, "sha256": digest(web)},
    "runtime": {"python_lock": "requirements-runtime.lock", "node": "24.22.1", "npm": "11.19.1"},
}
pathlib.Path("dist/release-manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
print(json.dumps(manifest, indent=2, sort_keys=True))
PY
