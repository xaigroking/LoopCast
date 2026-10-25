"""An independently installed, deliberately small public CLI contract double."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import zipfile
from loopcast.contracts import CAPABILITIES, MPT_REVISION, MPT_SOURCE

LAUNCHER = '''import argparse,json,os,sys
from pathlib import Path
import shutil

def main():
 p=argparse.ArgumentParser();p.add_argument('cli');p.add_argument('--video-subject');p.add_argument('--task-id');a=p.parse_args()
 work=Path(os.environ['MPT_HOME'])
 assert not any('CANARY' in k for k in os.environ)
 config=(work/'config.toml').read_text();assert 'upload_post_enabled = false' in config and 'upload_post_auto_upload = false' in config
 if a.video_subject == 'bad-json': print('invalid');return 0
 if a.video_subject == 'overflow': print('x'*2000000);return 0
 if a.video_subject == 'exit-failure': print(config,file=sys.stderr);return 2
 path=work/'storage'/'tasks'/a.task_id/'video.mp4';path.parent.mkdir(parents=True)
 shutil.copyfile(Path(__file__).parent/'sample.mp4',path)
 if a.video_subject == 'broken-media': path.write_bytes(b'broken')
 print(json.dumps({'task_id':a.task_id,'result':{'videos':[str(path)]}}))
 return 0
'''


def settings(directory):
    root = Path(directory)
    tools = Path(os.environ['LOOPCAST_MEDIA_TOOLS'])
    sample = root / 'sample.mp4'
    subprocess.run([str(tools/'ffmpeg'),'-v','error','-f','lavfi','-i','color=c=orange:s=64x64:d=0.2','-an','-c:v','libvpx-vp9',str(sample)],check=True)
    wheel = root / 'mpt-0.0.0-py3-none-any.whl'
    files = {'app/__init__.py':b'', 'app/launcher.py':LAUNCHER.encode(),'app/sample.mp4':sample.read_bytes(),
             'mpt-0.0.0.dist-info/METADATA':b'Metadata-Version: 2.1\nName: mpt\nVersion: 0.0.0\n',
             'mpt-0.0.0.dist-info/RECORD':b'', 'mpt-0.0.0.dist-info/WHEEL':b'Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n'}
    with zipfile.ZipFile(wheel,'w') as archive:
        for name,data in files.items():archive.writestr(name,data)
    venv = root/'component'
    subprocess.run([sys.executable,'-m','venv','--without-pip',str(venv)],check=True)
    library=venv/'lib'/f'python{sys.version_info.major}.{sys.version_info.minor}'/'site-packages'
    with zipfile.ZipFile(wheel) as archive:archive.extractall(library)
    probe=tools/'ffprobe'
    return {'manifest':{'source':MPT_SOURCE,'revision':MPT_REVISION,'artifact_sha256':hashlib.sha256(wheel.read_bytes()).hexdigest(),
                        'contract_major':1,'capabilities':{k:k=='generate' for k in CAPABILITIES},'verification':'contract_tested'},
            'python':str(venv/'bin/python'),'wheel':str(wheel),'ffprobe':str(probe),
            'ffprobe_sha256':hashlib.sha256(probe.read_bytes()).hexdigest(),'timeout':10}
