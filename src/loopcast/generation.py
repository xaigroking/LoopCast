"""Pinned MPT CLI adapter; only trusted local configuration selects executables."""
import hashlib
import json
from pathlib import Path
import sys
import tomllib
import uuid

from .contracts import manifest
from .runner import run, RunnerError

# Check installed files against the exact wheel before loading third-party code.
VERIFY = r'''
import hashlib, importlib.metadata, json, pathlib, sys, zipfile
wheel, expected = sys.argv[1:]
if hashlib.sha256(pathlib.Path(wheel).read_bytes()).hexdigest() != expected: raise SystemExit(2)
d = importlib.metadata.distribution('mpt')
with zipfile.ZipFile(wheel) as z:
    for name in z.namelist():
        if name.endswith('/') or name.endswith('.dist-info/RECORD'): continue
        if name.startswith('/') or '..' in pathlib.PurePosixPath(name).parts or '.data' in name.split('/')[0]: raise SystemExit(2)
        p = pathlib.Path(d.locate_file(name))
        if p.is_symlink() or not p.is_file() or hashlib.sha256(p.read_bytes()).digest() != hashlib.sha256(z.read(name)).digest(): raise SystemExit(2)
    metadata = json.loads(d.read_text('direct_url.json') or '{}')
    # A separately built wheel is identified by its pinned digest; revision is operator attestation.
print(json.dumps({'prefix': sys.prefix}))
'''

APP_KEYS = {'llm_provider', 'openai_api_key', 'openai_base_url', 'openai_model_name',
            'pexels_api_keys', 'pixabay_api_keys', 'azure_api_key', 'azure_model_name',
            'azure_base_url', 'upload_post_enabled', 'upload_post_auto_upload'}


def config_text(path):
    value = tomllib.loads(Path(path).read_text()) if path else {'app': {}}
    if set(value) - {'app'} or not isinstance(value.get('app', {}), dict):
        raise ValueError('Only generation provider configuration is allowed')
    app = value.get('app', {})
    if set(app) - APP_KEYS:
        raise ValueError('Undeclared provider setting')
    for key in ('upload_post_enabled', 'upload_post_auto_upload'):
        if app.get(key, False) is not False:
            raise ValueError('Automatic publication is forbidden')
        app[key] = False
    def literal(v):
        if isinstance(v, str) or type(v) in (bool, int): return json.dumps(v, ensure_ascii=False)
        if isinstance(v, list) and all(isinstance(x, str) for x in v): return json.dumps(v, ensure_ascii=False)
        raise ValueError('Invalid provider setting')
    return '[app]\n' + '\n'.join(k + ' = ' + literal(v) for k, v in sorted(app.items())) + '\n'


class Generator:
    def __init__(self, settings):
        required = {'manifest', 'python', 'wheel', 'ffprobe', 'ffprobe_sha256'}
        if not isinstance(settings, dict) or set(settings) - (required | {'config_ref', 'timeout'}) or not required <= set(settings):
            raise ValueError('Invalid generator configuration')
        self.manifest = manifest(settings['manifest'])
        self.python = Path(settings['python']).resolve(strict=True)
        # Resolve symlinks for inspection only: a venv Python must retain its invocation path.
        self.python = str(Path(settings['python']).absolute())
        self.wheel = Path(settings['wheel']).resolve(strict=True)
        self.probe = Path(settings['ffprobe']).resolve(strict=True)
        if hashlib.sha256(self.probe.read_bytes()).hexdigest() != settings['ffprobe_sha256']:
            raise ValueError('Media probe digest mismatch')
        self.probe_hash = settings['ffprobe_sha256']
        self.config_ref = settings.get('config_ref')
        config_text(self.config_ref)  # Validate without exposing secrets or storing them in state.
        self.timeout = settings.get('timeout', 300)
        if type(self.timeout) not in (int, float) or not 1 <= self.timeout <= 3600:
            raise ValueError('Invalid execution deadline')
        self.verify()

    @staticmethod
    def environment(work):
        return {'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8', 'HOME': str(work),
                'MPT_HOME': str(work), 'TMPDIR': str(work)}

    def verify(self):
        if hashlib.sha256(self.wheel.read_bytes()).hexdigest() != self.manifest['artifact_sha256']:
            raise ValueError('Component wheel digest mismatch')
        raw = run([self.python, '-I', '-c', VERIFY, str(self.wheel), self.manifest['artifact_sha256']],
                  str(self.wheel.parent), {'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'}, timeout=30)
        if Path(json.loads(raw)['prefix']).resolve() == Path(sys.prefix).resolve():
            raise ValueError('Use an independent component environment')

    def generate(self, value, work, cancelled):
        self.verify()
        work = Path(work).resolve()
        work.mkdir(mode=0o700, parents=True, exist_ok=False)
        config = work / 'config.toml'
        config.touch(mode=0o600)
        try:
            config.write_text(config_text(self.config_ref))
            task_id = str(uuid.uuid4())
            argv = [self.python, '-I', '-c', 'from app.launcher import main; raise SystemExit(main())',
                    'cli', '--video-subject', value['title'], '--task-id', task_id]
            raw = run(argv, work, self.environment(work), self.timeout, cancelled)
            try:
                result = json.loads(raw)
                videos = result['result']['videos']
                if result['task_id'] != task_id or not isinstance(videos, list) or len(videos) != 1 or not isinstance(videos[0], str):
                    raise ValueError()
            except (ValueError, KeyError, TypeError):
                raise RunnerError('INVALID_RESULT') from None
            return self.inspect(videos[0], work, cancelled)
        finally:
            config.unlink(missing_ok=True)

    def inspect(self, name, work, cancelled=lambda: False):
        root = Path(work).resolve()
        path = Path(name)
        if not path.is_absolute(): path = root / path
        if not path.resolve().is_relative_to(root) or any(p.is_symlink() for p in [path, *path.parents] if p != root.parent):
            raise RunnerError('INVALID_ARTIFACT')
        if not path.is_file() or not 0 < path.stat().st_size <= 128 * 1024 * 1024:
            raise RunnerError('INVALID_ARTIFACT')
        if hashlib.sha256(self.probe.read_bytes()).hexdigest() != self.probe_hash:
            raise RunnerError('PROBE_CHANGED')
        raw = run([str(self.probe), '-v', 'error', '-protocol_whitelist', 'file', '-show_streams',
                   '-show_format', '-of', 'json', str(path)], root, self.environment(root), 15, cancelled)
        try:
            data = json.loads(raw)
            video = next(s for s in data['streams'] if s['codec_type'] == 'video')
            duration = float(data['format']['duration'])
            if not 0 < duration <= 3600 or int(video['width']) <= 0 or int(video['height']) <= 0:
                raise ValueError()
            if 'mp4' not in data['format']['format_name'].split(','):
                raise ValueError()
        except (ValueError, KeyError, StopIteration, TypeError):
            raise RunnerError('INVALID_MEDIA') from None
        return path, {'duration': duration, 'width': int(video['width']), 'height': int(video['height']),
                      'codec': video['codec_name'], 'probe_sha256': self.probe_hash}
