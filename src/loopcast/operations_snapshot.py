"""Read-only, allowlisted observations; never exports configuration or exceptions."""
from contextlib import closing
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import sqlite3
import tempfile
import uuid
from .backup import readonly, regular, code_identity
from .migrations import CURRENT_VERSION
from .ownership import directory
from .runner import run


def observation(value, source, status='verified'):
    return {'value': value, 'status': status, 'source': source}


def unavailable(source):return observation(None, source, 'unavailable')


def media_version(settings, name):
    if not isinstance(settings, dict):return unavailable('media_settings_unavailable')
    try:
        path = Path(settings[name]); expected = settings[name + '_sha256']
        with path.open('rb') as stream:
            if hashlib.file_digest(stream, 'sha256').hexdigest() != expected:return unavailable('media_digest_mismatch')
        with tempfile.TemporaryDirectory() as work:
            raw = run([str(path), '-version'], work, {'PATH':'/usr/bin:/bin','LANG':'C.UTF-8'}, 2, limit=8192)
        match = re.match(rb'^' + name.encode() + rb' version ([0-9]+(?:\.[0-9]+){0,3}(?:[-+][a-zA-Z0-9.]+)?)(?:\s|$)', raw)
        if not match:return unavailable('media_version_unrecognized')
        return observation(match[1].decode('ascii'), 'verified_media_tool_version')
    except Exception:
        return unavailable('media_probe_unavailable')


def component_identity(settings):
    fields = {'declared_revision': unavailable('component_not_observed'), 'wheel_sha256': unavailable('component_not_observed')}
    if not isinstance(settings, dict):return fields
    manifest = settings.get('manifest')
    if not isinstance(manifest, dict):return fields
    revision = manifest.get('revision')
    if isinstance(revision, str) and re.fullmatch('[a-f0-9]{40}', revision):
        fields['declared_revision'] = observation(revision, 'configured_component_manifest', 'unverified')
    try:
        with Path(settings['wheel']).open('rb') as stream:
            digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        if digest == manifest.get('artifact_sha256'):
            fields['wheel_sha256'] = observation(digest, 'measured_component_wheel')
        else:fields['wheel_sha256'] = unavailable('component_digest_mismatch')
    except Exception:
        fields['wheel_sha256'] = unavailable('component_wheel_unavailable')
    return fields


def snapshot(path, media_settings=None, generator_settings=None, service_ready=None):
    _, handle = directory(path)
    try:
        with closing(readonly(handle)) as db:
            db.execute('BEGIN')
            versions = [r[0] for r in db.execute('SELECT version FROM schema_version')]
            if len(versions) != 1 or versions[0] not in {6, 7, CURRENT_VERSION}:
                raise ValueError('SCHEMA_UNSUPPORTED')
            schema = versions[0]
            counts = {name: db.execute('SELECT COUNT(*) FROM ' + name).fetchone()[0]
                      for name in ('runs','attempts','release_targets','feedback_imports','feedback_snapshots','feedback_decisions')}
            counts['unknown_publications'] = db.execute("SELECT COUNT(*) FROM release_targets WHERE state IN ('UNKNOWN','SUBMITTED')").fetchone()[0]
            guard = db.execute('SELECT restore_id,version,state FROM restore_guard WHERE id=1').fetchone() if schema >= 7 else None
        owner = unavailable('owner_lock_not_observed')
        try:
            with os.fdopen(regular(handle, 'service.lock'), 'rb') as lock:
                try:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    owner = observation('not_held', 'directory_lock_probe')
                except BlockingIOError:owner = observation('held', 'directory_lock_probe')
        except (OSError,ValueError):
            pass
        try:application = code_identity()
        except Exception:
            application = {key:unavailable('application_identity_unavailable') for key in ('version','installed_code_sha256','build_source')}
        return {'format':'loopcast.snapshot.v1','observed_at':datetime.now(timezone.utc).isoformat(),
                'application':application,
                'runtime':{'python':observation(platform.python_version(),'current_interpreter'),
                           'sqlite':observation(sqlite3.sqlite_version,'linked_sqlite_runtime'),
                           'owner_lock':owner,
                           'service_ready':observation(service_ready,'owner_runtime') if type(service_ready) is bool else unavailable('service_not_contacted')},
                'database':{'schema':observation(schema,'read_transaction'),
                            'compatible_schema_min':observation(6,'supported_schema_set'),
                            'compatible_schema_max':observation(CURRENT_VERSION,'supported_schema_set'),
                            'forward_policy':observation('COMPATIBLE_FORWARD_FIX_REQUIRED','application_policy'),
                            'counts':observation(counts,'read_transaction')},
                'media':{name:media_version(media_settings,name) for name in ('ffmpeg','ffprobe')},
                'generator':component_identity(generator_settings),
                'recovery':{'guard':observation(guard['state'] if guard else 'CLEAR','read_transaction') if schema >= 7 else unavailable('schema_has_no_restore_guard'),
                            'restore_id':observation(guard['restore_id'],'read_transaction') if guard else unavailable('not_restored'),
                            'guard_version':observation(guard['version'],'read_transaction') if guard else unavailable('not_restored')}}
    finally:
        os.close(handle)


def render_snapshot(report, format='json'):
    if format == 'json':return json.dumps(report,sort_keys=True,indent=2)+'\n'
    if format != 'markdown':raise ValueError('FORMAT_UNSUPPORTED')
    lines = ['# LoopCast observed runtime', '', 'Observed at: '+report['observed_at'], '', '| Field | Value | Status | Source |','|---|---|---|---|']
    for group in sorted(set(report)-{'format','observed_at'}):
        for key,item in sorted(report[group].items()):
            value = json.dumps(item['value'],sort_keys=True,ensure_ascii=True).replace('|','&#124;')
            lines.append(f"| {group}.{key} | {value} | {item['status']} | {item['source']} |")
    return '\n'.join(lines)+'\n'


def write_report(output, content, protected=()):
    path = Path(output).expanduser()
    if '..' in path.parts:raise ValueError('UNSAFE_OUTPUT')
    parent, handle = directory(path.absolute().parent)
    target = parent / path.name
    try:
        protected = [Path(__file__).parent, *map(Path,protected)]
        if any(target == item.expanduser().absolute() or target.is_relative_to(item.expanduser().absolute()) for item in protected):
            raise ValueError('PROTECTED_OUTPUT')
        if target.is_symlink() or target.exists() and (not target.is_file() or target.stat().st_nlink != 1):
            raise ValueError('UNSAFE_OUTPUT')
        name = '.loopcast-report-' + uuid.uuid4().hex
        fd = os.open(name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=handle)
        try:
            with os.fdopen(fd,'w',encoding='utf-8') as stream:
                stream.write(content);stream.flush();os.fsync(stream.fileno())
            os.replace(name,target.name,src_dir_fd=handle,dst_dir_fd=handle)
            os.fsync(handle)
        finally:
            try:os.unlink(name,dir_fd=handle)
            except FileNotFoundError:pass
    finally:
        os.close(handle)
