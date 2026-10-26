"""Coordinate public storage ports and isolated adapters, with one job owner."""
import json
import hashlib
from pathlib import Path
import shutil
import tempfile
import time
from .domain import Problem
from .runner import RunnerError
from .diagnostics import child_context, log_event


class StudioControl:
    def __init__(self, store, library, social, generator=None):
        self.store, self.library, self.social, self.generator = store, library, social, generator
        self.inspections = None

    @property
    def platform_map(self):
        return {p['platform']: p for p in self.social.capabilities()}

    @property
    def query_platforms(self):
        return [p for p, c in self.platform_map.items() if c['query']]

    def capabilities(self):
        return {'generation': self.generator is not None, 'media': self.library.settings is not None,
                'sending_enabled': self.social.sending_enabled and self.store.operations_status()['upload_allowed_by_recovery'], 'evidence_mode': self.social.mode,
                'platforms': self.social.capabilities(), 'voices': self.generator.voices if self.generator else [],
                'sources': self.generator.sources if self.generator else ['local'], 'script_generation': bool(self.generator and self.generator.script_generation)}

    def generate(self, project_id, revision_id, stage, expected_version, key, trace):
        if not self.generator or stage != 'script' and not self.library.settings:
            raise Problem('CAPABILITY_UNAVAILABLE', 'Generation tools are not configured for this stage.', 422)
        project = self.store.studio_project(project_id)
        revision = next((r for r in project['revisions'] if r['id'] == revision_id), None)
        if revision:
            draft = revision['draft']
            if stage == 'script' and not draft['script'].strip() and not draft['scenes'] and not self.generator.script_generation:
                raise Problem('CAPABILITY_UNAVAILABLE', 'Configure a script provider or supply a script.', 422)
            if draft['settings']['source'] not in self.generator.sources or draft['settings']['voice'] not in self.generator.voices:
                raise Problem('CAPABILITY_UNAVAILABLE', 'The selected provider or voice is not configured.', 422)
        return self.store.studio_generate(project_id, revision_id, stage, expected_version, key, trace)

    def authorize(self, platform, account_id, method, owner_hash, key, trace):
        self.social.require_platform(platform, method)
        return self.store.studio_authorize(platform, account_id, method, owner_hash, key, trace, ttl=min(300, self.social.timeout))

    def interact(self, identity, owner_hash, key, value):
        session = self.store.studio_authorization(identity, owner_hash)
        if session['state'] not in {'PENDING', 'WAITING', 'VERIFYING'}:
            raise Problem('AUTHORIZATION_CLOSED', 'The authorization session is closed.', 409)
        self.social.interaction(identity, key, value)
        return session

    def frame(self, identity, owner_hash):
        session = self.store.studio_authorization(identity, owner_hash)
        if session['state'] not in {'WAITING', 'VERIFYING'}:
            raise Problem('AUTHORIZATION_CLOSED', 'The authorization session is closed.', 409)
        return self.social.screen(identity)

    def disconnect(self, identity, expected_version, key):
        account = self.store.studio_private_account(identity)
        result = self.store.studio_account_update(identity, account['label'], json.loads(account['tags']), expected_version, key, disconnect=True)
        self.social.remove_credentials(account['credential_ref'])
        return result

    def preflight(self, value):
        self.library.path(value['asset_id'])
        if value.get('cover_id'): self.library.path(value['cover_id'])
        return self.store.studio_preflight(value, self.platform_map, self.social.mode)

    def create_release(self, value, preflight_digest, key):
        self.library.path(value['asset_id'])
        if value.get('cover_id'): self.library.path(value['cover_id'])
        return self.store.studio_create_release(value, preflight_digest, key, self.platform_map, self.social.mode)

    def execute_job(self, job, stopping):
        started = time.monotonic()
        context = child_context(job['trace'])
        kind, identity = job['kind'], job['subject_id']
        fields = {'job_id': job['id'], {'authorize': 'session_id', 'check': 'account_id', 'dispatch': 'target_id', 'verify': 'target_id'}[kind]: identity}
        log_event('social', 'job.started', context, **fields)
        result_state, crossed = 'FAILED', False
        inspection_deadline,check_timed_out=None,False
        work = self.social.session_directory(identity) if kind == 'authorize' else self.social.root / 'jobs' / job['id']
        work.mkdir(mode=0o700, parents=True, exist_ok=True)
        last_check, active, phase = 0, True, None
        def cancelled():
            nonlocal last_check, active, phase, check_timed_out
            if inspection_deadline is not None and time.monotonic()>=inspection_deadline:
                check_timed_out=True
                return True
            if stopping.is_set(): return True
            if time.monotonic() - last_check > .1:
                active = self.store.studio_job_active(job)
                last_check = time.monotonic()
                if kind == 'authorize':
                    try:
                        value = json.loads((work / 'progress.json').read_text()).get('state')
                        if value != phase:
                            self.store.studio_authorization_progress(job, value); phase = value
                    except (OSError, ValueError): pass
            return not active
        try:
            if kind == 'authorize':
                session = self.store.studio_authorization_job(identity)
                result = self.social.invoke('authorize', session['platform'], work, {}, cancelled)
                profile = result.get('profile', {})
                old = next((a for a in self.store.studio_accounts()['items'] if a['platform'] == session['platform'] and a['stable_subject'] == profile.get('stable_subject') and a['evidence_mode'] == self.social.mode), None)
                old_ref = self.store.studio_private_account(old['id'])['credential_ref'] if old else None
                reference = self.social.save_credentials(work / 'credentials.json')
                accepted = self.store.studio_authorization_result(job, profile, reference, self.social.mode)
                if accepted:
                    self.social.remove_credentials(old_ref)
                    result_state = 'SUCCEEDED'
                else:
                    self.social.remove_credentials(reference)
                    result_state = 'IGNORED'
            elif kind == 'check':
                if self.inspections:
                    self.inspections.refresh()
                    self.store.inspection_running(job)
                    inspection_deadline=self.store.inspection_account_deadline(job)
                if cancelled():raise RunnerError('CANCELLED')
                account = self.store.studio_private_account(identity)
                shutil.copyfile(self.social.credential_path(account['credential_ref']), work / 'credentials.json')
                (work / 'credentials.json').chmod(0o600)
                if cancelled():raise RunnerError('CANCELLED')
                if self.inspections:self.inspections.account_invoked(job)
                result = self.social.invoke('check', account['platform'], work, {}, cancelled)
                if self.inspections:self.inspections.before_account_result(job)
                observation=self.inspections.account_outcome(result,account) if self.inspections else None
                self.store.studio_check_result(job,result.get('valid')is True,result.get('profile',{}).get('stable_subject'),observation)
                result_state = 'SUCCEEDED' if result.get('valid') is True else 'EXPIRED'
            else:
                release = self.store.studio_release_job(job)
                account, batch = release['account'], release['batch']
                source = self.social.credential_path(account['credential_ref'])
                credential = work / 'credentials.json'; shutil.copyfile(source, credential); credential.chmod(0o600)
                payload = {'credential_file': str(credential), 'attempt_id': job['payload']['attempt_id']}
                if kind == 'dispatch':
                    self.store.studio_require_upload(job)
                    if not self.social.sending_enabled: raise Problem('SENDING_DISABLED', 'Sending is disabled.', 409)
                    asset = self.library.path(batch['asset_id'])
                    video = work / asset.name; shutil.copyfile(asset, video)
                    with video.open('rb') as stream:
                        if hashlib.file_digest(stream,'sha256').hexdigest() != json.loads(release['frozen'])['asset_sha256']:
                            raise Problem('FROZEN_INPUT_CHANGED', 'The copied video differs from the confirmed input.', 409)
                    payload.update({'video_file': str(video), 'title': batch['title'], 'description': batch['description'], 'fields': release['fields']})
                    if batch.get('cover_id'):
                        cover = self.library.path(batch['cover_id']); destination = work / cover.name; shutil.copyfile(cover, destination)
                        with destination.open('rb') as stream:
                            if hashlib.file_digest(stream,'sha256').hexdigest() != self.store.studio_asset_file(batch['cover_id'])['sha256']:
                                raise Problem('FROZEN_INPUT_CHANGED', 'The copied cover differs from its registered identity.', 409)
                        payload['cover_file'] = str(destination)
                    self.social.verify()
                    held = True
                    self.store.admission_lock.acquire()
                    def started_upload():
                        nonlocal held, crossed
                        crossed = True
                        held = False
                        self.store.admission_lock.release()
                    try:
                        self.store.studio_validate_dispatch(job, self.platform_map, self.social.mode)
                        if cancelled() or not self.store.studio_sending(job):
                            self.store.studio_release_result(job, {'outcome': 'NOT_SENT', 'boundary_crossed': False}, 'control-preflight')
                            return
                        self.store.studio_require_upload(job)
                        result = self.social.invoke('upload', release['platform'], work, payload, cancelled, on_started=started_upload)
                    finally:
                        if held:self.store.admission_lock.release()
                    self.store.studio_release_result(job, result, self.social.mode + ':sau')
                else:
                    result = self.social.invoke('query', release['platform'], work, payload, cancelled)
                    self.store.studio_verification_result(job, result, self.social.mode + ':query')
                result_state = result.get('outcome', 'UNKNOWN')
        except Exception as error:
            code = 'CHECK_TIMEOUT' if check_timed_out else 'INTERRUPTED' if stopping.is_set() else error.code if isinstance(error, (RunnerError, Problem)) else 'ADAPTER_FAILED'
            if kind == 'dispatch':
                result = {'outcome': 'UNKNOWN'} if crossed else {'outcome': 'NOT_SENT', 'boundary_crossed': False}
                self.store.studio_release_result(job, result, 'control:' + self.social.mode)
                result_state = result['outcome']
            elif kind == 'verify':
                self.store.studio_verification_result(job, {'outcome': 'UNKNOWN'}, 'control:' + self.social.mode)
            else:
                self.store.studio_fail_job(job, code)
        finally:
            self.store.studio_complete_job(job)
            shutil.rmtree(work, ignore_errors=True)
            log_event('social', 'job.completed', context, result=result_state, duration_ms=round((time.monotonic()-started)*1000, 2), **fields)
