"""Operator-invoked installation into a fresh private directory; never runs generation."""
import json
import os
from pathlib import Path
import platform
import shutil
import sys
import tarfile
import zipfile
from .contracts import MPT_REVISION, MPT_SOURCE, CAPABILITIES
from .media import file_digest
from .network import proxy_environment
from .runner import run, RunnerError

RESOURCES = Path(__file__).with_name('resources')


def checked_source(source):
    source = Path(source).resolve(strict=True)
    git = shutil.which('git')
    if not git: raise ValueError('GIT_REQUIRED')
    env = {'PATH':'/usr/bin:/bin','LANG':'C.UTF-8','GIT_TERMINAL_PROMPT':'0','GIT_OPTIONAL_LOCKS':'0'}
    revision = run([git,'rev-parse','HEAD'],source,env).decode().strip()
    if revision != MPT_REVISION: raise ValueError('SOURCE_REVISION_MISMATCH')
    if run([git,'status','--porcelain','--untracked-files=all'],source,env).strip(): raise ValueError('SOURCE_NOT_CLEAN')
    pins = json.loads((RESOURCES/'mpt-source.json').read_text())
    for name, checksum in pins['files'].items():
        if (source/name).is_symlink() or file_digest(source/name) != checksum: raise ValueError('SOURCE_LOCK_MISMATCH')
    return source, git


def plan(source, destination, python, uv, uv_sha256, ffmpeg, ffprobe):
    if platform.system() != 'Linux' or platform.machine() not in {'x86_64','aarch64'}: raise ValueError('PLATFORM_UNSUPPORTED')
    source, git = checked_source(source)
    destination = Path(destination).absolute()
    if destination.exists() or destination.is_relative_to(source) or source.is_relative_to(destination): raise ValueError('FRESH_DESTINATION_REQUIRED')
    python, uv = Path(python).absolute(), Path(uv).absolute()
    if not python.is_file() or not os.access(python,os.X_OK): raise ValueError('PYTHON_REQUIRED')
    if file_digest(uv) != uv_sha256 or not os.access(uv,os.X_OK): raise ValueError('UV_DIGEST_MISMATCH')
    media = {name:str(Path(value).resolve(strict=True)) for name,value in [('ffmpeg',ffmpeg),('ffprobe',ffprobe)]}
    if any(not os.access(path,os.X_OK) for path in media.values()): raise ValueError('MEDIA_TOOL_REQUIRED')
    media.update({name+'_sha256':file_digest(value) for name,value in list(media.items())})
    return {'source':str(source),'revision':MPT_REVISION,'destination':str(destination),'python':str(python),'uv':str(uv),
            'uv_sha256':uv_sha256,'git':git,'media':media,'steps':['archive_source','build_environment','locked_builder','build_wheel','frozen_runtime','install_wheel','verify_dependencies','verify_cli','write_configuration']}


def install(value, network=None):
    destination = Path(value['destination'])
    destination.mkdir(mode=0o700,parents=True,exist_ok=False)
    (destination/'tmp').mkdir(mode=0o700)
    env = {'PATH':'/usr/bin:/bin','LANG':'C.UTF-8','HOME':str(destination),'TMPDIR':str(destination/'tmp'),
           'UV_CACHE_DIR':str(destination/'cache'),'UV_PROJECT_ENVIRONMENT':str(destination/'runtime'),
           'UV_PYTHON_DOWNLOADS':'never','UV_NO_PROGRESS':'1',**proxy_environment(network)}
    uv, python, source = value['uv'], value['python'], Path(value['source'])
    def command(args, cwd=destination):
        return run(args,cwd,env,timeout=1800,limit=2097152)
    record = {'source':MPT_SOURCE,'revision':MPT_REVISION,'uv_sha256':value['uv_sha256'],
              'upstream_lock_sha256':file_digest(source/'uv.lock'),'builder_lock_sha256':file_digest(RESOURCES/'mpt-build.lock'),
              'steps':[],'evidence':'installation_only_live_generation_unverified'}
    try:
        version=command([python,'-I','-c','import sys;assert (3,11)<=sys.version_info[:2]<(3,15);print(sys.version)']).decode().strip()
        record['python_version']=version
        record['uv_version']=command([uv,'--version']).decode().strip()
        for name in ('ffmpeg','ffprobe'):
            command([value['media'][name],'-version'])
        archive=destination/'source.tar'
        command([value['git'],'archive','--format=tar','--output',str(archive),'HEAD'],source)
        staged=destination/'source';staged.mkdir(mode=0o700)
        with tarfile.open(archive) as bundle: bundle.extractall(staged,filter='data')
        archive.unlink();record['steps'].append('archive_source')
        build=destination/'build'
        command([python,'-I','-m','venv','--without-pip',str(build)]);record['steps'].append('build_environment')
        command([uv,'--no-config','pip','install','--python',str(build/'bin/python'),'--require-hashes','-r',str(RESOURCES/'mpt-build.lock')]);record['steps'].append('locked_builder')
        wheels=destination/'wheels';wheels.mkdir(mode=0o700)
        command([str(build/'bin/python'),'-I','-c','from hatchling.build import build_wheel; import sys; build_wheel(sys.argv[1])',str(wheels)],staged);record['steps'].append('build_wheel')
        files=list(wheels.glob('mpt-*.whl'))
        if len(files)!=1:raise ValueError('WHEEL_MISSING')
        wheel=files[0]
        with zipfile.ZipFile(wheel) as bundle:
            if not {'app/cli.py','app/launcher.py','app/_assets/config.example.toml'} <= set(bundle.namelist()): raise ValueError('WHEEL_LAYOUT_INVALID')
            fonts=[n for n in bundle.namelist() if n.startswith('app/_assets/resource/fonts/')]
            if not fonts: raise ValueError('FONT_RESOURCES_MISSING')
            record['font_resources']=fonts
        command([uv,'--no-config','sync','--frozen','--no-dev','--no-install-project','--project',str(staged),'--python',python]);record['steps'].append('frozen_runtime')
        runtime=destination/'runtime/bin/python'
        command([uv,'--no-config','pip','install','--python',str(runtime),'--no-deps',str(wheel)]);record['steps'].append('install_wheel')
        command([uv,'--no-config','pip','check','--python',str(runtime)]);record['steps'].append('verify_dependencies')
        inventory=json.loads(command([uv,'--no-config','pip','list','--format','json','--python',str(runtime)]))
        record['installed_packages']=inventory
        command([str(runtime),'-I','-c','from app.launcher import main; raise SystemExit(main())','cli','--help']);record['steps'].append('verify_cli')
        generator={'manifest':{'source':MPT_SOURCE,'revision':MPT_REVISION,'artifact_sha256':file_digest(wheel),'contract_major':1,
                              'capabilities':{k:k=='generate' for k in CAPABILITIES},'verification':'unverified'},
                   'python':str(runtime),'wheel':str(wheel),'ffprobe':value['media']['ffprobe'],'ffprobe_sha256':value['media']['ffprobe_sha256'],
                   'ffmpeg':value['media']['ffmpeg'],'ffmpeg_sha256':value['media']['ffmpeg_sha256'],'timeout':600}
        record['wheel']={'name':wheel.name,'sha256':file_digest(wheel)}
        for name,data in [('generator.json',generator),('media.json',value['media'])]:
            path=destination/name;path.write_text(json.dumps(data,indent=2)+'\n');path.chmod(0o600)
        record['steps'].append('write_configuration');record['state']='INSTALLED'
    except (OSError,ValueError,RunnerError):
        record['state']='FAILED'
        raise
    finally:
        path=destination/'installation.json';path.write_text(json.dumps(record,indent=2)+'\n');path.chmod(0o600)
    return record
