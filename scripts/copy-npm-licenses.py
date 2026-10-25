#!/usr/bin/env python3
"""Copy installed npm license notices into the standalone deployment assets."""
import json
from pathlib import Path
import re
import shutil

root = Path(__file__).resolve().parents[1]
frontend = root / "frontend"
modules = frontend / "node_modules"
public = frontend / "public" / "licenses"
package_lock = json.loads((frontend / "package-lock.json").read_text())
entries = package_lock["packages"]
if not modules.is_dir():
    raise SystemExit("Run npm ci before collecting frontend dependency notices")

if (public / 'npm').exists():
    shutil.rmtree(public / 'npm')
(public / "npm").mkdir(parents=True)
manifest = []
for lock_path, lock_entry in sorted(entries.items()):
    if not lock_path.startswith("node_modules/"):
        continue
    package_dir = (frontend / lock_path).resolve()
    if not package_dir.is_relative_to(modules.resolve()):
        raise SystemExit(f"Locked npm package is missing or escaped node_modules: {lock_path}")
    if not package_dir.is_dir() and lock_entry.get("optional"):
        continue
    if not package_dir.is_dir():
        raise SystemExit(f"Locked npm package is missing: {lock_path}")
    metadata_path = package_dir / "package.json"
    metadata = json.loads(metadata_path.read_text())
    name, version = metadata.get("name"), metadata.get("version")
    if name != lock_entry.get("name", name) or version != lock_entry.get("version"):
        raise SystemExit(f"Installed npm package differs from lock: {lock_path}")
    license_value = metadata.get("license") or lock_entry.get("license")
    licenses = metadata.get("licenses")
    if not license_value and licenses:
        license_value = " AND ".join(item.get("type", "UNKNOWN") for item in licenses)
    if not isinstance(license_value, (str, dict)):
        raise SystemExit(f"npm package does not declare its license: {name}@{version}")
    license_text = license_value if isinstance(license_value, str) else license_value.get("type", "UNKNOWN")
    package_key = re.sub(r"[^A-Za-z0-9._@+-]+", "_", f"{name}@{version}")
    copied = []
    for notice in sorted(package_dir.iterdir()):
        if notice.is_file() and re.match(r"^(license|licence|copying|notice)(\.|$)", notice.name, re.IGNORECASE):
            target = public / "npm" / package_key / notice.name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(notice, target)
            copied.append(target.relative_to(frontend / "public").as_posix())
    manifest.append({"name": name, "version": version, "license": license_text, "notices": copied})

(public / "npm-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
print(f"Copied license notices and manifest for {len(manifest)} locked npm packages")
