"""Explicit non-generating inspection. Never returns private values or raw tool output."""
import json
from pathlib import Path
import stat
import tempfile
import zipfile
from .contracts import manifest
from .generation import Generator, config_text
from .media import file_digest
from .runner import run

REQUIRED_FLAGS = ('--video-script','--video-materials','--video-aspect','--voice-name','--custom-audio-file','--stop-at','--bgm-file','--no-subtitle-enabled')


def inspect_generator(settings=None, media_settings=None):
    report={'state':'NOT_CONFIGURED','callable':False,'live_verification':'UNVERIFIED','checks':[],
            'paths':{'local_inputs':'UNVERIFIED','online_model':'UNVERIFIED','online_materials':'UNVERIFIED','online_tts':'UNVERIFIED'},
            'network':{'model':'ENV_PROXY','materials':'ENV_PROXY','edge_tts':'EXPLICIT_PROXY','other_tts':'UNSUPPORTED'},
            'supported_stages':['script','audio','materials','subtitle','video']}
    def check(name, action, code):
        try: action();report['checks'].append({'id':name,'state':'PASS','code':'OK'});return True
        except Exception:
            report['checks'].append({'id':name,'state':'FAIL','code':code});report['state']='INVALID';return False
    if settings is None:
        report['checks'].append({'id':'configuration','state':'FAIL','code':'GENERATOR_NOT_CONFIGURED'});return report
    if not isinstance(settings,dict): report['state']='INVALID';report['checks'].append({'id':'configuration','state':'FAIL','code':'CONFIG_INVALID'});return report
    if not check('manifest',lambda:manifest(settings['manifest']),'COMPONENT_MANIFEST_INVALID'):return report
    def wheel():
        path=Path(settings['wheel'])
        if not path.is_file() or file_digest(path)!=settings['manifest']['artifact_sha256']:raise ValueError()
        with zipfile.ZipFile(path) as archive:
            if not {'app/launcher.py'} <= set(archive.namelist()):raise ValueError()
    if not check('wheel',wheel,'COMPONENT_WHEEL_INVALID'):report['state']='NOT_INSTALLED';return report
    def provider():
        if settings.get('config_ref'):
            path=Path(settings['config_ref'])
            if stat.S_IMODE(path.stat().st_mode)&0o077:raise ValueError()
        config_text(settings.get('config_ref'))
    if not check('provider',provider,'PROVIDER_CONFIG_INVALID'):return report
    try:generator=Generator(settings)
    except Exception:
        report['state']='NOT_INSTALLED';report['checks'].append({'id':'interpreter','state':'FAIL','code':'COMPONENT_ENVIRONMENT_INVALID'});return report
    report['checks'].append({'id':'interpreter','state':'PASS','code':'OK'})
    def cli():
        with tempfile.TemporaryDirectory() as directory:
            raw=run(generator.command()+['--help'],directory,generator.environment(directory)|generator.network,30,limit=131072).decode()
        if any(flag not in raw for flag in REQUIRED_FLAGS):raise ValueError()
    if not check('cli',cli,'COMPONENT_CLI_UNSUPPORTED'):return report
    def tools():
        value=media_settings or ({'ffmpeg':str(generator.ffmpeg),'ffmpeg_sha256':generator.ffmpeg_hash,'ffprobe':str(generator.probe),'ffprobe_sha256':generator.probe_hash} if generator.ffmpeg else None)
        if not value or set(value)!={'ffmpeg','ffmpeg_sha256','ffprobe','ffprobe_sha256'}:raise ValueError()
        with tempfile.TemporaryDirectory() as directory:
            for name in ('ffmpeg','ffprobe'):
                if file_digest(value[name])!=value[name+'_sha256']:raise ValueError()
                run([value[name],'-version'],directory,{'PATH':'/usr/bin:/bin','LANG':'C.UTF-8'},10)
    if not check('media_tools',tools,'MEDIA_TOOLS_INVALID'):return report
    with zipfile.ZipFile(settings['wheel']) as archive:
        fonts=any(name.startswith('app/_assets/resource/fonts/') for name in archive.namelist())
    report['checks'].append({'id':'resources','state':'PASS' if fonts else 'WARN','code':'OK' if fonts else 'COMPONENT_RESOURCES_UNVERIFIED'})
    report['state']='CALLABLE';report['callable']=True
    return report
