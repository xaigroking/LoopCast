"""Pinned isolated social execution and explicit per-platform capabilities."""
import hashlib
import json
import os
import tempfile
import threading
from pathlib import Path
import re
import shutil
import sys
import time
from urllib.parse import urlsplit
from .domain import Problem, uid
from .media import file_digest
from .network import proxy_environment
from .runner import run, RunnerError

SAU_SOURCE = 'https://github.com/dreammis/social-auto-upload'
SAU_REVISION = '3847fff01f5434a8a32829833163cd6723cabc59'
PLATFORMS = ('douyin', 'xiaohongshu', 'bilibili', 'youtube')
IDENTITY_HOSTS = {'douyin': {'creator.douyin.com'}, 'xiaohongshu': {'creator.xiaohongshu.com'}, 'bilibili': {'api.bilibili.com'}, 'youtube': {'studio.youtube.com'}}


def source_digest(root):
    value = hashlib.sha256()
    for path in sorted(root.rglob('*.py')):
        if '.git' in path.parts or '__pycache__' in path.parts: continue
        if path.is_symlink(): raise ValueError('Component source contains symlinks')
        value.update(path.relative_to(root).as_posix().encode() + b'\0' + path.read_bytes() + b'\0')
    return value.hexdigest()


def validate_probe(platform, probe):
    if not isinstance(probe, dict) or set(probe) != {'url', 'id_path', 'label_path', 'success_path', 'success_value'}:
        raise ValueError('Invalid own-account identity probe')
    parsed = urlsplit(probe['url'])
    if parsed.scheme != 'https' or parsed.hostname not in IDENTITY_HOSTS[platform] or parsed.port not in (None, 443) or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('Identity probe must use an allowed first-party origin')
    for key in ('id_path', 'label_path', 'success_path'):
        if not isinstance(probe[key], list) or not 1 <= len(probe[key]) <= 8 or not all(isinstance(p, str) and re.fullmatch(r'[A-Za-z0-9_]{1,80}', p) for p in probe[key]):
            raise ValueError('Invalid profile response path')
    if type(probe['success_value']) not in (bool, int, str): raise ValueError('Invalid identity success condition')
    if probe['id_path'][-1] not in {'uid', 'user_id', 'userId', 'id', 'mid', 'sec_uid', 'secUid', 'channel_id', 'channelId'}:
        raise ValueError('Probe must select a stable account identity')
    if probe['label_path'][-1] not in {'nickname', 'name', 'uname', 'display_name', 'userName'}:
        raise ValueError('Probe must select a public account label')
    return probe


class SocialAdapter:
    """No live behavior is enabled without an explicit server-side configuration."""
    def __init__(self, directory, settings=None):
        self.root = Path(directory) / 'social'
        self.root.mkdir(mode=0o700, exist_ok=True)
        self.credentials = self.root / 'credentials'
        self.credentials.mkdir(mode=0o700, exist_ok=True)
        self.sessions = self.root / 'sessions'
        self.sessions.mkdir(mode=0o700, exist_ok=True)
        self._control_lock = threading.Lock()
        self.settings = settings or {}
        self.backend = self.settings.get('backend', 'disabled')
        self.mode = 'contract' if self.backend == 'contract' else 'live'
        self.sending_enabled = self.settings.get('sending_enabled', False) is True
        self.timeout = self.settings.get('timeout', 300)
        if type(self.timeout) not in (int, float) or not 5 <= self.timeout <= 600:
            raise ValueError('Invalid social execution deadline')
        self.network = proxy_environment(self.settings.get('network'))
        if not isinstance(self.settings.get('identity_probes', {}), dict) or set(self.settings.get('identity_probes', {})) - set(PLATFORMS):
            raise ValueError('Unknown identity probe platform')
        self.probes = {p: validate_probe(p, v) for p, v in self.settings.get('identity_probes', {}).items()}
        if self.backend not in {'disabled', 'sau', 'contract'}: raise ValueError('Unknown social backend')
        allowed = {'backend', 'sending_enabled', 'timeout', 'network', 'python', 'source_root', 'source_revision', 'source_sha256',
                   'identity_probes', 'browser_path', 'browser_sha256', 'biliup', 'biliup_sha256', 'contract_profiles', 'contract_results'}
        if set(self.settings) - allowed: raise ValueError('Undeclared social configuration')
        if self.backend == 'sau':
            for key in ('python', 'source_root', 'source_revision', 'source_sha256', 'browser_path', 'browser_sha256'):
                if key not in self.settings: raise ValueError('Incomplete social component configuration')
            if self.settings['source_revision'] != SAU_REVISION: raise ValueError('Unsupported social revision')
            self.python = str(Path(self.settings['python']).absolute())
            self.source_root = Path(self.settings['source_root']).resolve(strict=True)
            self.verify()

    def verify(self):
        if self.backend != 'sau': return
        if source_digest(self.source_root) != self.settings['source_sha256']:
            raise Problem('COMPONENT_UNAVAILABLE', 'Social component source changed.', 409)
        if file_digest(self.settings['browser_path']) != self.settings['browser_sha256']:
            raise Problem('COMPONENT_UNAVAILABLE', 'Browser executable changed.', 409)
        if self.settings.get('biliup') and file_digest(self.settings['biliup']) != self.settings.get('biliup_sha256'):
            raise Problem('COMPONENT_UNAVAILABLE', 'Bilibili executable changed.', 409)

    def capabilities(self):
        values = []
        for platform in PLATFORMS:
            configured = self.backend == 'contract' or (self.backend == 'sau' and (platform == 'youtube' or platform == 'bilibili' and bool(self.settings.get('biliup')) or platform in self.probes))
            values.append({'platform': platform, 'login_methods': ['browser'] if platform == 'youtube' else ['qr'] if platform == 'bilibili' else ['qr', 'browser'],
                'configured': configured, 'verification': 'contract_tested_live_unverified' if configured else 'unavailable',
                'video': True, 'query': self.backend == 'contract', 'schedule': False, 'ai_disclosure': platform == 'douyin',
                'thumbnail': True, 'title_limit': {'douyin': 30, 'xiaohongshu': 20, 'youtube': 100, 'bilibili': 80}[platform],
                'requires_category': platform == 'bilibili', 'visibility_options': ['public', 'unlisted', 'private'] if platform == 'youtube' else ['public']})
        return values

    def require_platform(self, platform, method=None):
        capability = next((p for p in self.capabilities() if p['platform'] == platform), None)
        if not capability or not capability['configured']:
            raise Problem('CAPABILITY_UNAVAILABLE', 'This platform adapter is not configured.', 422)
        if method and method not in capability['login_methods']:
            raise Problem('CAPABILITY_UNAVAILABLE', 'This login method is unavailable.', 422)

    def session_directory(self, identity):
        if not re.fullmatch(r'[a-f0-9]{32}', identity): raise ValueError('Invalid session identity')
        path = self.sessions / identity
        path.mkdir(mode=0o700, exist_ok=True)
        return path

    def interaction(self, session_id, key, value):
        if not isinstance(key, str) or not key.isascii() or not 1 <= len(key) <= 128:
            raise Problem('IDEMPOTENCY_REQUIRED', 'Supply an Idempotency-Key.', 400)
        work = self.session_directory(session_id)
        controls = work / 'controls'; controls.mkdir(mode=0o700, exist_ok=True)
        if len(list(controls.glob('*.json'))) >= 64:
            raise Problem('QUEUE_FULL', 'Wait for pending authorization controls.', 429)
        name = hashlib.sha256(key.encode()).hexdigest() + '.json'
        path = controls / name
        # The private control payload lasts only for this authorization session.
        # It is never stored in business events, request records or diagnostics.
        with self._control_lock:
            if path.exists() or (work / ('done-' + name)).exists(): return
            payload = dict(value, sequence=time.time_ns())
            # Only complete private JSON becomes visible to either consumer.
            handle, temporary = tempfile.mkstemp(prefix='.control-', suffix='.pending', dir=controls)
            try:
                with os.fdopen(handle, 'w') as stream:
                    json.dump(payload, stream);stream.flush();os.fsync(stream.fileno())
                if (work / ('done-' + name)).exists(): return
                try:os.link(temporary, path)
                except FileExistsError:pass
            finally:Path(temporary).unlink(missing_ok=True)

    def screen(self, identity):
        path = self.session_directory(identity) / 'frame.png'
        if not path.is_file() or path.is_symlink() or path.stat().st_size > 4 * 1024 * 1024:
            raise Problem('FRAME_PENDING', 'Authorization screen is not available yet.', 404)
        return path.read_bytes()

    def credential_path(self, reference):
        if not isinstance(reference, str) or not re.fullmatch(r'[a-f0-9]{32}', reference):
            raise Problem('ACCOUNT_DISCONNECTED', 'Account credentials are unavailable.', 409)
        path = self.credentials / (reference + '.json')
        if path.is_symlink() or not path.is_file(): raise Problem('ACCOUNT_DISCONNECTED', 'Account credentials are unavailable.', 409)
        return path

    def save_credentials(self, path):
        if path.is_symlink() or not 0 < path.stat().st_size <= 2 * 1024 * 1024:
            raise RunnerError('INVALID_CREDENTIAL_RESULT')
        json.loads(path.read_text())
        reference = uid()
        target = self.credentials / (reference + '.json')
        with path.open('rb') as source, target.open('xb') as destination:
            target.chmod(0o600); shutil.copyfileobj(source, destination)
        return reference

    def remove_credentials(self, reference):
        if reference and re.fullmatch(r'[a-f0-9]{32}', reference):
            (self.credentials / (reference + '.json')).unlink(missing_ok=True)

    def cleanup_session(self, identity):
        path = self.sessions / identity
        if path.parent == self.sessions and not path.is_symlink(): shutil.rmtree(path, ignore_errors=True)

    def invoke(self, operation, platform, work, payload, cancelled):
        self.require_platform(platform)
        self.verify()
        if self.backend == 'contract':
            return self._contract(operation, platform, work, payload, cancelled)
        settings = {'source_root': str(self.source_root), 'browser_path': self.settings['browser_path'],
                    'identity_probe': self.probes.get(platform), 'biliup': self.settings.get('biliup'), 'proxy_url': self.network.get('HTTPS_PROXY')}
        request = {'operation': operation, 'platform': platform, 'payload': payload, 'settings': settings}
        path = work / 'request.json'
        with path.open('w') as stream:
            path.chmod(0o600); json.dump(request, stream)
        env = {'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8', 'HOME': str(work), 'TMPDIR': str(work), **self.network}
        bridge = Path(__file__).with_name('social_bridge.py')
        try:
            raw = run([self.python, '-I', str(bridge), str(path)], work, env, self.timeout if operation in {'authorize', 'upload'} else 60, cancelled)
            result = json.loads(raw)
            if not isinstance(result, dict): raise ValueError()
            return result
        except (ValueError, KeyError, TypeError):
            raise RunnerError('INVALID_RESULT') from None

    def _contract(self, operation, platform, work, payload, cancelled):
        """Explicitly configured isolated test backend, never the default service."""
        profiles = self.settings.get('contract_profiles', {})
        choices = profiles.get(platform, [])
        if isinstance(choices, dict): choices = [choices]
        if not isinstance(choices, list) or not choices: raise RunnerError('FIXTURE_PROFILE_MISSING')
        if operation == 'authorize':
            deadline = time.monotonic() + self.timeout
            while time.monotonic() < deadline:
                if cancelled(): raise RunnerError('CANCELLED')
                controls = work / 'controls'
                pending = list(controls.glob('*.json')) if controls.exists() else []
                for path in pending:
                    try:value = json.loads(path.read_text())
                    except (OSError, ValueError):continue
                    (work / ('done-' + path.name)).touch(mode=0o600)
                    path.unlink(missing_ok=True)
                    if value.get('kind') == 'confirm':
                        profile = next((p for p in choices if p['stable_subject'] == value.get('text')), None) if value.get('text') else choices[0]
                        if profile is None: raise RunnerError('FIXTURE_PROFILE_MISSING')
                        credential = work / 'credentials.json'
                        credential.write_text(json.dumps({'fixture_secret': 'private-credential-canary', 'stable_subject': profile['stable_subject']})); credential.chmod(0o600)
                        return {'profile': profile}
                time.sleep(.02)
            raise RunnerError('TIMEOUT')
        credential = json.loads((work / 'credentials.json').read_text())
        profile = next((p for p in choices if p['stable_subject'] == credential.get('stable_subject')), None)
        if profile is None:
            return {'valid': False}
        if operation == 'check': return {'valid': True, 'profile': profile}
        scenario = self.settings.get('contract_results', {}).get(profile['stable_subject'], 'published')
        receipts = self.root / 'contract-receipts'; receipts.mkdir(mode=0o700, exist_ok=True)
        receipt_path = receipts / (payload.get('attempt_id', 'absent') + '.json')
        if operation == 'query':
            if receipt_path.is_file():
                value = json.loads(receipt_path.read_text())
                if value.get('subject') == profile['stable_subject'] and value.get('platform') == platform:
                    return {'outcome': 'PUBLISHED', 'confirmed': True, 'work_id': value['work_id']}
            return {'outcome': 'UNKNOWN'}
        if operation == 'upload':
            if scenario == 'not_sent': return {'outcome': 'NOT_SENT', 'boundary_crossed': False}
            if scenario in {'published', 'lost_response', 'timeout'}:
                receipt_path.write_text(json.dumps({'subject': profile['stable_subject'], 'platform': platform, 'work_id': 'contract-' + payload['attempt_id']})); receipt_path.chmod(0o600)
            if scenario == 'lost_response': return {'outcome': 'UNKNOWN'}
            if scenario == 'submitted': return {'outcome': 'SUBMITTED'}
            if scenario == 'timeout':
                while not cancelled(): time.sleep(.02)
                raise RunnerError('TIMEOUT')
            return {'outcome': 'PUBLISHED', 'confirmed': True, 'work_id': 'contract-' + payload['attempt_id']}
        raise RunnerError('CAPABILITY_UNAVAILABLE')
