#!/usr/bin/env python3
"""Validate shipped locale contracts and the exact offline third-party assets."""
import hashlib
import json
from pathlib import Path
import re

root=Path(__file__).resolve().parents[1]
web=root/'src/loopcast/web'
registry=json.loads((web/'locales/registry.json').read_text())
assert len(registry)==20 and len({r['code'] for r in registry})==20
base=json.loads((web/'locales/en.json').read_text())
plural=re.compile(r'_(zero|one|two|few|many|other)$')
slots=lambda value:set(re.findall(r'{{\s*([\w]+)(?:\s*,[^}]+)?\s*}}',value))
for language in registry:
    data=json.loads((web/'locales'/f"{language['code']}.json").read_text())
    assert set(data)==set(base),language
    for namespace in base:
        keys=lambda values:{plural.sub('',key) for key in values}
        assert keys(data[namespace])==keys(base[namespace]),(language,namespace)
        for key,value in data[namespace].items():
            reference=base[namespace].get(key,base[namespace].get(plural.sub('',key)+'_other'))
            assert isinstance(value,str) and value.strip(),(language,key)
            assert slots(value)==slots(reference),(language,key,'interpolation')
        if language['code']!='en':
            equal=sum(value==base[namespace].get(key) for key,value in data[namespace].items())
            assert equal < len(data[namespace])*.8,(language,namespace,'untranslated bundle')
# UI resource IDs are stable; user content and raw evidence never go through t().
for source in [web/'app.js',web/'index.html']:
    text=source.read_text()
    literals=re.findall(r"\bt\('([\w]+)'[),]",text)+re.findall(r'data-i18n="([\w]+)"',text)
    for key in literals:assert key in base['ui'] or key+'_other' in base['ui'],(source.name,key)
for package in json.loads((root/'web-vendor.lock.json').read_text())['packages']:
    name='i18next' if package['name']=='i18next' else 'language-detector'
    assert hashlib.sha256((web/(name+'.js')).read_bytes()).hexdigest()==package['sha256']
    assert hashlib.sha256((web/(name+'.LICENSE.txt')).read_bytes()).hexdigest()==package['license_sha256']
print(f"UI assets passed: {len(registry)} independent language bundles, namespaces, stable keys, interpolation, vendor hashes/licenses")
