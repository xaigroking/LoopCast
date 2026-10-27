"""Test-only fixed runtime provisioning; never imported by the application."""
import hashlib
import json
import os
from pathlib import Path
import sys
import urllib.request
import zipfile

lock = json.loads((Path(__file__).resolve().parents[1]/'electron-test-runtime.lock.json').read_text())
root = Path(sys.argv[1]).resolve(); root.mkdir(parents=True, exist_ok=True)
archive = root/'runtime.zip'
if not archive.exists() or hashlib.sha256(archive.read_bytes()).hexdigest() != lock['sha256']:
    with urllib.request.urlopen(lock['url'], timeout=60) as source, archive.open('wb') as target:
        while block := source.read(1048576): target.write(block)
if hashlib.sha256(archive.read_bytes()).hexdigest() != lock['sha256']: raise SystemExit('Test runtime digest mismatch')
with zipfile.ZipFile(archive) as bundle:
    for entry in bundle.infolist():
        target = root/entry.filename
        if not target.resolve().is_relative_to(root): raise SystemExit('Invalid archive entry')
        bundle.extract(entry, root)
        if entry.external_attr >> 16: target.chmod((entry.external_attr >> 16) & 0o777)
print('Verified test Electron', lock['version'], lock['platform'], lock['sha256'])
