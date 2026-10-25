#!/usr/bin/env python3
"""Validate the independent frontend's offline locale and font assets."""
import hashlib
import json
from pathlib import Path
import re

root=Path(__file__).resolve().parents[1]
web=root/'frontend/public'
registry=json.loads((web/'locales/registry.json').read_text())
assert len(registry)==20 and len({r['code'] for r in registry})==20
base=json.loads((web/'locales/en.json').read_text())
plural=re.compile(r'_(zero|one|two|few|many|other)$')
slots=lambda value:set(re.findall(r'{{\s*([\w]+)(?:\s*,[^}]+)?\s*}}',value))
for language in registry:
    data=json.loads((web/'locales'/f"{language['code']}.json").read_text())
    primary=language['code'] in {'en','zh-Hans'}
    assert set(data)<=set(base),language
    if primary: assert set(data)==set(base),language
    for namespace in data:
        keys=lambda values:{plural.sub('',key) for key in values}
        assert keys(data[namespace])<=keys(base[namespace]),(language,namespace)
        if primary: assert keys(data[namespace])==keys(base[namespace]),(language,namespace)
        for key,value in data[namespace].items():
            reference=base[namespace].get(key,base[namespace].get(plural.sub('',key)+'_other'))
            assert isinstance(value,str) and value.strip(),(language,key)
            assert slots(value)==slots(reference),(language,key,'interpolation')
        if language['code']!='en':
            equal=sum(value==base[namespace].get(key) for key,value in data[namespace].items())
            assert not data[namespace] or equal < len(data[namespace])*.8,(language,namespace,'untranslated bundle')
# UI resource IDs are stable; user content and raw evidence stay out of translation calls.
sources=list((root/'frontend/src').rglob('*.tsx'))+list((root/'frontend/src').rglob('*.ts'))
for source in sources:
    text=source.read_text()
    literals=re.findall(r"\bt\('([\w]+)'[),]",text)
    for key in literals:
        if key.startswith('state:'):
            assert key.split(':',1)[1] in base['state'],(source.name,key)
        elif key.startswith('errors:'):
            assert key.split(':',1)[1] in base['errors'],(source.name,key)
        else:
            assert key in base['ui'] or key+'_other' in base['ui'],(source.name,key)
package=json.loads((root/'frontend/package.json').read_text())
lock=json.loads((root/'frontend/package-lock.json').read_text())
for name,version in package['dependencies'].items():
    assert lock['packages'][f'node_modules/{name}']['version']==version,(name,version)
print(f"UI assets passed: {len(registry)} loadable language bundles; complete English/Simplified Chinese, valid frozen translations, interpolation and npm locks")

for font in json.loads((root/'web-fonts.lock.json').read_text()):
    assert hashlib.sha256((web/'fonts'/font['file']).read_bytes()).hexdigest()==font['sha256']
    assert hashlib.sha256((web/'fonts'/(font['file']+'.LICENSE.txt')).read_bytes()).hexdigest()==font['license_sha256']
print('Offline font hashes and OFL licenses passed')

# Check actual OpenType cmap coverage for the scripts for which fonts are bundled.
import struct

def font_characters(path):
    content=path.read_bytes()
    u16=lambda offset:struct.unpack_from('>H',content,offset)[0]
    u32=lambda offset:struct.unpack_from('>I',content,offset)[0]
    tables={content[12+16*i:16+16*i].decode():u32(20+16*i) for i in range(u16(4))}
    start=tables['cmap'];subtables=[start+u32(start+8+8*i) for i in range(u16(start+2))]
    result=set()
    for table in set(subtables):
        if u16(table)==12:
            for i in range(u32(table+12)):
                first,last,glyph=struct.unpack_from('>III',content,table+16+12*i)
                result.update(range(first+(glyph==0),last+1))
        elif u16(table)==4:
            count=u16(table+6)//2
            for i in range(count):
                end=u16(table+14+2*i);first=u16(table+16+2*count+2*i)
                delta=u16(table+16+4*count+2*i);where=table+16+6*count+2*i;offset=u16(where)
                for point in range(first,end+1):
                    glyph=(point+delta)&65535 if offset==0 else u16(where+offset+2*(point-first))
                    if glyph:result.add(point)
    return result

all_text=''.join((web/'locales'/f"{r['code']}.json").read_text() for r in registry)
for name,ranges in [('NotoSans.ttf',[(0,0x24f),(0x300,0x52f),(0x1e00,0x1fff)]),('NotoCJK.otf',[(0x1100,0x11ff),(0x2e80,0x9fff),(0xac00,0xd7af),(0xf900,0xfaff),(0xff00,0xffef)]),
                    ('NotoArabic.ttf',[(0x600,0x6ff),(0x750,0x77f),(0x8a0,0x8ff)]),
                    ('NotoHebrew.ttf',[(0x590,0x5ff)]),('NotoDevanagari.ttf',[(0x900,0x97f)]),('NotoThai.ttf',[(0xe00,0xe7f)])]:
    required={ord(char) for char in all_text if not char.isspace() and any(start<=ord(char)<=end for start,end in ranges)}
    missing=required-font_characters(web/'fonts'/name)
    assert not missing,(name,sorted(missing))
print('OpenType glyph coverage passed for shipped CJK, Arabic, Hebrew, Devanagari and Thai text')
