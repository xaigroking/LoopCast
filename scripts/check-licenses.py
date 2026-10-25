#!/usr/bin/env python3
"""Check original-code notices and their delivered copies without relicensing dependencies."""
import hashlib
import json
from pathlib import Path
import zipfile

root = Path(__file__).resolve().parents[1]
license_text = (root / 'LICENSE').read_bytes()
assert b'Apache License' in license_text and b'Version 2.0, January 2004' in license_text
assert json.loads((root / 'frontend/package.json').read_text())['license'] == 'Apache-2.0'
for name in ['LICENSE', 'NOTICE', 'THIRD_PARTY.md']:
    assert (root / name).read_bytes() == (root / 'frontend/public/licenses/app' / name).read_bytes()
for path in (root / 'third_party').iterdir():
    assert path.read_bytes() == (root / 'frontend/public/licenses/app' / path.name).read_bytes()
wheel = next((root / 'dist').glob('loopcast-*.whl'), None)
if wheel:
    with zipfile.ZipFile(wheel) as archive:
        prefix = next(name for name in archive.namelist() if name.endswith('.dist-info/licenses/LICENSE'))
        assert archive.read(prefix) == license_text
        assert any(name.endswith('.dist-info/licenses/NOTICE') for name in archive.namelist())
        assert all(any(name.endswith('/third_party/' + path.name) for name in archive.namelist()) for path in (root / 'third_party').iterdir())
print('Original-code and third-party license delivery passed; Apache text SHA256', hashlib.sha256(license_text).hexdigest())
