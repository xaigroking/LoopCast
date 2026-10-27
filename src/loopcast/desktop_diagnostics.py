"""Bounded offline observations. Never starts an owner or executes manifest tools."""
import argparse
import errno
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import signal
import sqlite3
import stat
import sys
import tempfile
import time
import uuid
from .migrations import CURRENT_VERSION
from .platform_info import source_revision, supported
from .runtime_guard import NAME, read_guard
if sys.platform == 'linux':
    import fcntl
    from .backup import regular, readonly, publish_entry
    from .ownership import directory

FORMAT = 'loopcast.desktop-diagnostics.v1'
IDS = ('manifest', 'resource_python', 'resource_node', 'resource_tree', 'resource_next',
       'data_directory', 'session_directory', 'owner_lock', 'desktop_cleanup_guard',
       'restore_publication_guard', 'unknown_publications', 'installed_source',
       'platform_capabilities', 'sandbox_runtime', 'upstream_runtime')
CODES = frozenset(('OK MANIFEST_MISSING MANIFEST_INVALID MANIFEST_CHANGED PATH_UNSAFE RESOURCE_MISSING '
    'RESOURCE_DIGEST_MISMATCH RESOURCE_CHANGED VERSION_MISMATCH PLATFORM_UNSUPPORTED CAPABILITY_UNAVAILABLE '
    'DIRECTORY_MISSING DIRECTORY_UNSAFE FILE_UNSAFE PERMISSION_DENIED ENTRY_PORT_BUSY OWNER_HELD OWNER_NOT_HELD LOCK_MISSING '
    'DESKTOP_CLEANUP_UNCONFIRMED GUARD_ABSENT GUARD_UNSAFE READ_STATE_UNAVAILABLE DATABASE_MISSING '
    'SCHEMA_UNSUPPORTED STATE_CHANGED BUDGET_EXCEEDED CANCELLED DIAGNOSTICS_UNAVAILABLE START_FAILED '
    'START_TIMEOUT STOP_UNCONFIRMED RUNTIME_UNVERIFIED EXPORT_REFUSED').split())
SOURCES = frozenset(('not_observed manifest_file resource_file next_build_id directory_metadata '
    'existing_lock_probe desktop_guard_file sqlite_private_read_transaction installed_resource '
    'implementation_metadata runtime_not_exercised').split())
VALUES = {'manifest':{'VALID'},'resource_next':{'CONSISTENT'},'data_directory':{'PRIVATE'},
          'session_directory':{'PRIVATE'},'owner_lock':{'HELD','NOT_HELD','MISSING'},
          'desktop_cleanup_guard':{'PRESENT','ABSENT'},'restore_publication_guard':{'CLEAR','ACTIVE'},
          'platform_capabilities':{'SUPPORTED_IMPLEMENTATION','UNSUPPORTED'}}
HEX40 = re.compile(r'[a-f0-9]{40}\Z')
HEX64 = re.compile(r'[a-f0-9]{64}\Z')

class DiagnosticError(Exception):
    def __init__(self, code): self.code = code

class Budget:
    def __init__(self, milliseconds=2000, max_bytes=268435456, cancelled=lambda: False):
        if type(milliseconds) is not int or not 1 <= milliseconds <= 10000 or type(max_bytes) is not int or not 1 <= max_bytes <= 536870912:
            raise DiagnosticError('BUDGET_EXCEEDED')
        self.end = time.monotonic() + milliseconds / 1000
        self.left = max_bytes
        self.cancelled = cancelled
    def check(self):
        if self.cancelled(): raise DiagnosticError('CANCELLED')
        if time.monotonic() >= self.end: raise DiagnosticError('BUDGET_EXCEEDED')
    def read(self, fd, size):
        self.check()
        if self.left <= 0: raise DiagnosticError('BUDGET_EXCEEDED')
        chunk = os.read(fd, min(size, self.left, 1048576)); self.left -= len(chunk)
        self.check(); return chunk

def stamp(): return datetime.now(timezone.utc).isoformat()
def identity(info): return info.st_dev, info.st_ino
def version(info): return identity(info), info.st_size, info.st_mtime_ns, info.st_ctime_ns

def private(info, kind):
    valid = stat.S_ISDIR(info.st_mode) if kind == 'directory' else stat.S_ISREG(info.st_mode) and info.st_nlink == 1
    if not valid or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise DiagnosticError('DIRECTORY_UNSAFE' if kind == 'directory' else 'FILE_UNSAFE')

def absolute(path):
    if not isinstance(path, (str, Path)) or not Path(path).is_absolute() or '..' in Path(path).parts or '\0' in str(path):
        raise DiagnosticError('PATH_UNSAFE')
    return Path(path)

def anchored_file(path):
    path = absolute(path); _, parent = directory(path.parent)
    try: return parent, safe_regular(parent, path.name)
    except BaseException: os.close(parent); raise

def safe_regular(parent, name):
    try: return regular(parent, name)
    except ValueError: raise DiagnosticError('FILE_UNSAFE') from None
    except OSError as error:
        if error.errno in {errno.ELOOP,errno.ENOTDIR}: raise DiagnosticError('FILE_UNSAFE') from None
        raise

def stable(parent, name, fd, before):
    if version(os.fstat(fd)) != version(before) or identity(os.stat(name, dir_fd=parent, follow_symlinks=False)) != identity(before):
        raise DiagnosticError('STATE_CHANGED')

def same_directory(path, fd):
    _, current = directory(path)
    try:
        if identity(os.fstat(current)) != identity(os.fstat(fd)): raise DiagnosticError('STATE_CHANGED')
    finally: os.close(current)

def small_file(path, budget, maximum=65536, require_private=True):
    parent, fd = anchored_file(path)
    try:
        before = os.fstat(fd)
        if require_private: private(before, 'file')
        if before.st_size > maximum: raise DiagnosticError('FILE_UNSAFE')
        result = bytearray()
        while len(result) < before.st_size:
            chunk = budget.read(fd, before.st_size - len(result))
            if not chunk: break
            result.extend(chunk)
        if len(result) != before.st_size: raise DiagnosticError('STATE_CHANGED')
        stable(parent, Path(path).name, fd, before)
        same_directory(Path(path).parent, parent)
        return bytes(result)
    finally: os.close(fd); os.close(parent)

def reason(error, missing='READ_STATE_UNAVAILABLE'):
    if isinstance(error, DiagnosticError): return error.code
    if isinstance(error, FileNotFoundError): return missing
    if isinstance(error, PermissionError): return 'PERMISSION_DENIED'
    if isinstance(error, OSError) and error.errno in {errno.ELOOP,errno.ENOTDIR}: return 'PATH_UNSAFE'
    if isinstance(error, KeyboardInterrupt): return 'CANCELLED'
    if isinstance(error, (OSError, ValueError, TypeError, KeyError, sqlite3.Error)): return 'READ_STATE_UNAVAILABLE'
    return 'DIAGNOSTICS_UNAVAILABLE'

def pairs(values):
    result = {}
    for key, value in values:
        if key in result: raise DiagnosticError('MANIFEST_INVALID')
        result[key] = value
    return result

def manifest(path, budget):
    try: value = json.loads(small_file(path, budget), object_pairs_hook=pairs)
    except FileNotFoundError: raise DiagnosticError('MANIFEST_MISSING') from None
    except (ValueError, TypeError): raise DiagnosticError('MANIFEST_INVALID') from None
    if not isinstance(value, dict) or value.get('format') != 'loopcast.desktop.v1' or value.get('mode') not in {'development','release'}:
        raise DiagnosticError('MANIFEST_INVALID')
    if value.get('locale') not in {'en','zh-Hans'} or type(value.get('gatewayPort')) is not int or not 1<=value['gatewayPort']<=65535:
        raise DiagnosticError('MANIFEST_INVALID')
    for key,maximum in [('startTimeoutMs',120000),('stopTimeoutMs',60000)]:
        if type(value.get(key)) is not int or not 100<=value[key]<=maximum:raise DiagnosticError('MANIFEST_INVALID')
    if not isinstance(value.get('configs'),dict):raise DiagnosticError('MANIFEST_INVALID')
    for key in ('generator','media','social','schedule','inspection'):
        if key in value['configs']:absolute(value['configs'][key])
    for key in ('hostRevision','apiRevision','webRevision'):
        if not isinstance(value.get(key), str) or not HEX40.fullmatch(value[key]): raise DiagnosticError('MANIFEST_INVALID')
    for key in ('dataDir','sessionDir','nextRoot'): absolute(value.get(key))
    for key in ('python','node','treeModule'):
        item = value.get(key)
        if not isinstance(item, dict): raise DiagnosticError('MANIFEST_INVALID')
        absolute(item.get('path'))
        if not isinstance(item.get('sha256'), str) or not HEX64.fullmatch(item['sha256']): raise DiagnosticError('MANIFEST_INVALID')
    if value.get('nodeVersion') != '24.22.1': raise DiagnosticError('VERSION_MISMATCH')
    paths = [Path(value[key]) for key in ('dataDir','sessionDir','nextRoot')]
    if any(a == b or a.is_relative_to(b) or b.is_relative_to(a) for i,a in enumerate(paths) for b in paths[i+1:]):
        raise DiagnosticError('PATH_UNSAFE')
    return value

def owner_probe(fd):
    try: lock = safe_regular(fd, 'service.lock')
    except FileNotFoundError: return 'LOCK_MISSING', 'MISSING'
    try:
        before = os.fstat(lock); private(before, 'file')
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            code, value = 'OWNER_NOT_HELD', 'NOT_HELD'
        except BlockingIOError: code, value = 'OWNER_HELD', 'HELD'
        finally: fcntl.flock(lock, fcntl.LOCK_UN)
        stable(fd, 'service.lock', lock, before)
        return code, value
    finally: os.close(lock)

def guard_probe(fd):
    try:
        before = os.stat(NAME, dir_fd=fd, follow_symlinks=False)
    except FileNotFoundError:
        # Check again so a marker appearing during observation is not CLEAR.
        try: os.stat(NAME, dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError: return 'GUARD_ABSENT', 'ABSENT'
        raise DiagnosticError('STATE_CHANGED')
    try: value = read_guard(fd)
    except (OSError, ValueError, TypeError): raise DiagnosticError('GUARD_UNSAFE') from None
    after = os.stat(NAME, dir_fd=fd, follow_symlinks=False)
    if value is None or version(before) != version(after) or value[1] != identity(before): raise DiagnosticError('STATE_CHANGED')
    return 'DESKTOP_CLEANUP_UNCONFIRMED', 'PRESENT'

def preflight(paths):
    """Safe shared startup preflight. No files, owners, DB or service are created."""
    try:
        for index, path in enumerate(paths):
            _, fd = directory(absolute(path))
            try:
                private(os.fstat(fd), 'directory')
                code, _ = guard_probe(fd)
                if code != 'GUARD_ABSENT': return code
                if index == 0:
                    code, _ = owner_probe(fd)
                    if code == 'OWNER_HELD': return code
                same_directory(absolute(path), fd)
            finally: os.close(fd)
        return 'OK'
    except BaseException as error:
        code = reason(error, 'DIRECTORY_MISSING')
        if code == 'PATH_UNSAFE': code = 'DIRECTORY_UNSAFE'
        return code if code in {'DIRECTORY_MISSING','DIRECTORY_UNSAFE','FILE_UNSAFE','PERMISSION_DENIED','GUARD_UNSAFE'} else 'DIAGNOSTICS_UNAVAILABLE'

def resource(item, budget):
    # Match the existing venv resource policy, but never execute it.
    declared = absolute(item['path']); declared_info = declared.lstat()
    path = declared.resolve(strict=True)
    parent, fd = anchored_file(path)
    try:
        before = os.fstat(fd); digest = hashlib.sha256()
        if before.st_mode & 0o022: raise DiagnosticError('FILE_UNSAFE')
        if before.st_size > budget.left: raise DiagnosticError('BUDGET_EXCEEDED')
        consumed = 0
        while consumed < before.st_size:
            chunk = budget.read(fd, before.st_size-consumed)
            if not chunk: raise DiagnosticError('RESOURCE_CHANGED')
            digest.update(chunk); consumed += len(chunk)
        stable(parent, path.name, fd, before)
        same_directory(path.parent, parent)
        if version(declared.lstat()) != version(declared_info) or declared.resolve(strict=True) != path: raise DiagnosticError('RESOURCE_CHANGED')
        actual = digest.hexdigest()
        if actual != item['sha256']: raise DiagnosticError('RESOURCE_DIGEST_MISMATCH')
        return actual
    finally: os.close(fd); os.close(parent)

@contextmanager
def database_snapshot(fd, budget):
    """Only copied, stable source bytes reach SQLite; source sidecars stay untouched."""
    opened = {}; before = {}
    names = ('state.sqlite3', 'state.sqlite3-wal')
    try:
        try: os.stat('state.sqlite3-journal', dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError: pass
        else: raise DiagnosticError('READ_STATE_UNAVAILABLE')
        for name in names:
            try: handle = safe_regular(fd, name)
            except FileNotFoundError:
                if name == 'state.sqlite3': raise DiagnosticError('DATABASE_MISSING') from None
                before[name] = None; continue
            opened[name] = handle; before[name] = os.fstat(handle)
            # SQLite inherits normal read bits; the anchored parent is private.
            # Reject shared writes, links and another user's source objects.
            info = before[name]
            if info.st_uid != os.getuid() or info.st_mode & 0o022: raise DiagnosticError('FILE_UNSAFE')
        with tempfile.TemporaryDirectory(prefix='loopcast-diagnostics-') as work:
            for name, handle in opened.items():
                with open(Path(work)/name, 'xb') as target:
                    os.chmod(target.name, 0o600)
                    consumed = 0
                    while consumed < before[name].st_size:
                        chunk = budget.read(handle, before[name].st_size-consumed)
                        if not chunk: raise DiagnosticError('STATE_CHANGED')
                        target.write(chunk); consumed += len(chunk)
            for name in names:
                try: current = os.stat(name, dir_fd=fd, follow_symlinks=False)
                except FileNotFoundError: current = None
                if (current is None) != (before[name] is None): raise DiagnosticError('STATE_CHANGED')
                if current is not None: stable(fd, name, opened[name], before[name])
            try: os.stat('state.sqlite3-journal', dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError: pass
            else: raise DiagnosticError('STATE_CHANGED')
            _, copied = directory(work)
            try:
                with closing(readonly(copied)) as db:
                    db.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 65536)
                    db.execute('PRAGMA trusted_schema=OFF')
                    db.set_progress_handler(lambda: int(time.monotonic() >= budget.end or budget.cancelled()), 1000)
                    db.execute('BEGIN')
                    rows = db.execute('SELECT version FROM schema_version LIMIT 2').fetchall()
                    if len(rows) != 1 or rows[0][0] not in {6,7,8,CURRENT_VERSION}: raise DiagnosticError('SCHEMA_UNSUPPORTED')
                    yield db, rows[0][0]
                    budget.check()
            finally: os.close(copied)
    finally:
        for handle in opened.values(): os.close(handle)

def database_facts(fd, budget):
    with database_snapshot(fd, budget) as (db, schema):
        guard = db.execute('SELECT state FROM restore_guard WHERE id=1').fetchone() if schema >= 7 else None
        state = guard[0] if guard else 'CLEAR' if schema >= 7 else None
        if state not in {None, 'CLEAR', 'ACTIVE'}: raise DiagnosticError('READ_STATE_UNAVAILABLE')
        count = db.execute("SELECT COUNT(*) FROM release_targets WHERE state IN ('UNKNOWN','SUBMITTED')").fetchone()[0]
        budget.check()
        return state, count

def collect(manifest_path=None, data_dir=None, *, timeout_ms=2000, max_bytes=268435456, cancelled=lambda: False, _context=None):
    os_name = sys.platform if sys.platform in {'linux','win32','darwin'} else 'other'
    arch = platform.machine(); arch = arch if arch in {'x86_64','AMD64','aarch64','arm64'} else 'other'
    rev = source_revision(); rev = rev if isinstance(rev, str) and HEX40.fullmatch(rev) else None
    report = {'format':FORMAT, 'started_at':stamp(), 'observed_at':None, 'platform':{'os':os_name,'arch':arch,'revision':rev}, 'scope':'NON_ATOMIC_OBSERVATIONS', 'checks':[]}
    checks = {}
    def put(key, code, source, value=None, status='verified', revision=rev):
        checks[key] = dict(id=key,status=status,code=code,source=source,observed_at=stamp(),os=os_name,arch=arch,revision=revision,value=value)
    def attempt(key, source, work, missing='READ_STATE_UNAVAILABLE'):
        try:
            budget.check(); value = work(); put(key,'OK',source,value)
        except BaseException as error: put(key,reason(error,missing),source,status='unavailable')
    try:
        budget = Budget(timeout_ms,max_bytes,cancelled)
        if _context is not None: _context['budget'] = budget
        if sys.platform != 'linux': raise DiagnosticError('PLATFORM_UNSUPPORTED')
        config = None
        if manifest_path is None: put('manifest','MANIFEST_MISSING','manifest_file',status='unavailable')
        else:
            try:
                config = manifest(manifest_path,budget)
                if _context is not None: _context['config'] = config
                put('manifest','OK','manifest_file','VALID',revision=config['hostRevision'])
            except BaseException as error: put('manifest',reason(error,'MANIFEST_MISSING'),'manifest_file',status='unavailable')
        put('installed_source','OK' if rev else 'READ_STATE_UNAVAILABLE','installed_resource',revision=rev,status='verified' if rev else 'unavailable')
        platform_code = 'OK' if supported() else 'CAPABILITY_UNAVAILABLE' if sys.platform == 'linux' and platform.machine() == 'x86_64' else 'PLATFORM_UNSUPPORTED'
        put('platform_capabilities',platform_code,'implementation_metadata', 'SUPPORTED_IMPLEMENTATION' if supported() else 'UNSUPPORTED',status='unverified')
        for key in ('sandbox_runtime','upstream_runtime'): put(key,'RUNTIME_UNVERIFIED','runtime_not_exercised',status='unverified')
        if config:
            if rev and config['apiRevision'] != rev: put('installed_source','VERSION_MISMATCH','installed_resource',revision=rev)
            for key,name in [('python','resource_python'),('node','resource_node'),('treeModule','resource_tree')]:
                attempt(name,'resource_file',lambda key=key:resource(config[key],budget),'RESOURCE_MISSING')
            def next_identity():
                raw = small_file(Path(config['nextRoot'])/'.next/BUILD_ID',budget,256,False).decode('ascii').strip()
                if raw != config['webRevision']: raise DiagnosticError('VERSION_MISMATCH')
                return 'CONSISTENT'
            attempt('resource_next','next_build_id',next_identity,'RESOURCE_MISSING')
        data = data_dir if data_dir is not None else config['dataDir'] if config else None
        for path,key in [(data,'data_directory'),(config['sessionDir'] if config else None,'session_directory')]:
            if path is None: continue
            fd = None
            try:
                budget.check(); _, fd = directory(absolute(path)); private(os.fstat(fd),'directory'); put(key,'OK','directory_metadata','PRIVATE')
                if key == 'data_directory':
                    for name,source,probe in [('owner_lock','existing_lock_probe',owner_probe),('desktop_cleanup_guard','desktop_guard_file',guard_probe)]:
                        try:
                            budget.check(); code,value = probe(fd);put(name,code,source,value)
                        except BaseException as error: put(name,reason(error),source,status='unavailable')
                    try:
                        state,count = database_facts(fd,budget)
                        put('restore_publication_guard','OK' if state else 'READ_STATE_UNAVAILABLE','sqlite_private_read_transaction',state,status='verified' if state else 'unavailable')
                        put('unknown_publications','OK','sqlite_private_read_transaction',count)
                    except BaseException as error:
                        code=reason(error)
                        for name in ('restore_publication_guard','unknown_publications'): put(name,code,'sqlite_private_read_transaction',status='unavailable')
                try: same_directory(absolute(path),fd)
                except BaseException:
                    affected = (key,'owner_lock','desktop_cleanup_guard','restore_publication_guard','unknown_publications') if key == 'data_directory' else (key,)
                    for name in affected:
                        source = checks[name]['source'] if name in checks else 'not_observed'
                        put(name,'STATE_CHANGED',source,status='unavailable')
            except BaseException as error: put(key,reason(error,'DIRECTORY_MISSING'),'directory_metadata',status='unavailable')
            finally:
                if fd is not None: os.close(fd)
    except BaseException as error:
        code = reason(error)
        for key in IDS:
            if key not in checks: put(key,code,'not_observed',status='unavailable')
    try: budget.check(); remaining_code = 'READ_STATE_UNAVAILABLE'
    except BaseException as error: remaining_code = reason(error)
    for key in IDS:
        if key not in checks: put(key,remaining_code,'not_observed',status='unavailable')
    report['checks']=[checks[key] for key in IDS]; report['observed_at']=stamp()
    return report

def validate_report(report):
    if set(report) != {'format','started_at','observed_at','platform','scope','checks'} or report['format'] != FORMAT or report['scope'] != 'NON_ATOMIC_OBSERVATIONS': raise DiagnosticError('DIAGNOSTICS_UNAVAILABLE')
    if set(report['platform']) != {'os','arch','revision'}: raise DiagnosticError('DIAGNOSTICS_UNAVAILABLE')
    if [check.get('id') for check in report['checks']] != list(IDS): raise DiagnosticError('DIAGNOSTICS_UNAVAILABLE')
    for time_value in (report['started_at'],report['observed_at'],*[x['observed_at'] for x in report['checks']]):
        if not isinstance(time_value,str) or not re.fullmatch(r'[0-9T:+.Z-]{20,40}',time_value): raise DiagnosticError('DIAGNOSTICS_UNAVAILABLE')
        parsed=datetime.fromisoformat(time_value)
        if parsed.tzinfo is None or parsed.utcoffset().total_seconds()!=0 or parsed.isoformat()!=time_value:raise DiagnosticError('DIAGNOSTICS_UNAVAILABLE')
    for check in [report['platform'],*report['checks']]:
        if check['os'] not in {'linux','win32','darwin','other'} or check['arch'] not in {'x86_64','AMD64','aarch64','arm64','other'}: raise DiagnosticError('DIAGNOSTICS_UNAVAILABLE')
        if check['revision'] is not None and not HEX40.fullmatch(check['revision']): raise DiagnosticError('DIAGNOSTICS_UNAVAILABLE')
    for check in report['checks']:
        if set(check) != {'id','status','code','source','observed_at','os','arch','revision','value'} or check['code'] not in CODES or check['source'] not in SOURCES or check['status'] not in {'verified','unverified','unavailable'}: raise DiagnosticError('DIAGNOSTICS_UNAVAILABLE')
        value=check['value']
        if check['status']=='unavailable' and value is not None:raise DiagnosticError('DIAGNOSTICS_UNAVAILABLE')
        if value is not None and not (type(value) is int and check['id']=='unknown_publications' and 0<=value<=2**53-1) and not (isinstance(value,str) and (value in VALUES.get(check['id'],set()) or check['id'] in {'resource_python','resource_node','resource_tree'} and HEX64.fullmatch(value))): raise DiagnosticError('DIAGNOSTICS_UNAVAILABLE')
    return report

GUIDANCE = 'Preserve the existing data and execution evidence. This report cannot confirm safe recovery or authorize sending. Check trusted same-source resources; close the original instance normally. Unresolved cleanup or publication barriers require operator investigation. No barrier-clearing or automatic replay operation is provided.'

def render(report, format='json'):
    validate_report(report)
    if format == 'json': return json.dumps(report,sort_keys=True,indent=2)+'\n'
    if format != 'text': raise DiagnosticError('DIAGNOSTICS_UNAVAILABLE')
    return 'LoopCast desktop diagnostics\nScope: NON_ATOMIC_OBSERVATIONS\n'+ '\n'.join(f"{x['id']}: {x['status']} {x['code']} {x['value'] if x['value'] is not None else 'UNKNOWN'}" for x in report['checks'])+'\n\n'+GUIDANCE+'\n'

def export_report(output, report, manifest_path, config, data_dir=None, format='json', budget=None):
    # Refuse unknown protection boundaries instead of guessing a safe directory.
    if not config or not manifest_path: raise DiagnosticError('EXPORT_REFUSED')
    content=render(report,format)
    if budget: budget.check()
    target=absolute(output); parent_path,fd=directory(target.parent); staged=None; published=False; own=None
    try:
        private(os.fstat(fd),'directory')
        protected=[Path(__file__).resolve().parent,Path(manifest_path).parent,*[Path(config[k]) for k in ('dataDir','sessionDir','nextRoot')]]
        if data_dir:protected.append(absolute(data_dir))
        for key in ('python','node','treeModule'):protected.extend([Path(config[key]['path']).parent,Path(config[key]['path']).resolve().parent])
        for value in config.get('configs',{}).values():
            if isinstance(value,str):protected.append(absolute(value).parent)
        for item in protected:
            if parent_path == item or parent_path.is_relative_to(item) or item.is_relative_to(parent_path): raise DiagnosticError('EXPORT_REFUSED')
        try: os.stat(target.name,dir_fd=fd,follow_symlinks=False)
        except FileNotFoundError:pass
        else:raise DiagnosticError('EXPORT_REFUSED')
        before=identity(os.fstat(fd)); staged='.loopcast-report-'+uuid.uuid4().hex
        file=os.open(staged,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=fd)
        with os.fdopen(file,'w',encoding='utf-8') as stream:
            own=identity(os.fstat(stream.fileno()));stream.write(content);stream.flush();os.fsync(stream.fileno())
        _,fresh=directory(parent_path)
        try:
            if identity(os.fstat(fresh))!=before:raise DiagnosticError('EXPORT_REFUSED')
        finally:os.close(fresh)
        publish_entry(fd,staged,target.name);published=True
        _,fresh=directory(parent_path)
        try:
            if identity(os.fstat(fresh))!=before:raise DiagnosticError('EXPORT_REFUSED')
        finally:os.close(fresh)
        final=os.stat(target.name,dir_fd=fd,follow_symlinks=False);private(final,'file')
        if identity(final)!=own:raise DiagnosticError('EXPORT_REFUSED')
        if budget: budget.check()
    except BaseException:
        # rename may succeed before its fsync raises; remove only our inode.
        if own is not None:
            try:
                if identity(os.stat(target.name,dir_fd=fd,follow_symlinks=False))==own:os.unlink(target.name,dir_fd=fd)
            except FileNotFoundError:pass
        raise
    finally:
        if staged:
            try:os.unlink(staged,dir_fd=fd)
            except FileNotFoundError:pass
        os.close(fd)

class SafeParser(argparse.ArgumentParser):
    def error(self, message): self.exit(2,'Desktop diagnostics arguments are invalid. Use --help.\n')

def cli(argv):
    parser=SafeParser(prog='loopcast desktop-diagnostics',description='Offline read-only evidence. Never authorizes recovery or sending.')
    parser.add_argument('--manifest',type=Path);parser.add_argument('--data-dir',type=Path)
    parser.add_argument('--format',choices=['json','text'],default='json');parser.add_argument('--output',type=Path)
    parser.add_argument('--timeout-ms',type=int,default=2000);parser.add_argument('--max-bytes',type=int,default=268435456)
    args=parser.parse_args(argv)
    previous={number:signal.getsignal(number) for number in (signal.SIGTERM,signal.SIGINT)}
    cancelled=False
    def interrupt(_signal, _frame):
        nonlocal cancelled
        cancelled=True
        raise KeyboardInterrupt()
    for number in previous: signal.signal(number,interrupt)
    try:
        context={}
        report=collect(args.manifest,args.data_dir,timeout_ms=args.timeout_ms,max_bytes=args.max_bytes,cancelled=lambda:cancelled,_context=context)
        content=render(report,args.format)
        if args.output:
            config=context.get('config');budget=context.get('budget')
            if cancelled: raise DiagnosticError('CANCELLED')
            if config is None or budget is None: raise DiagnosticError('EXPORT_REFUSED')
            if manifest(args.manifest,budget) != config: raise DiagnosticError('MANIFEST_CHANGED')
            export_report(args.output,report,args.manifest,config,args.data_dir,args.format,budget)
        else:sys.stdout.write(content)
        return 1 if any(x['code'] in {'CANCELLED','BUDGET_EXCEEDED','DIAGNOSTICS_UNAVAILABLE'} for x in report['checks']) else 0
    except (Exception,KeyboardInterrupt) as error:
        code=reason(error)
        sys.stderr.write(code+': Desktop diagnostics unavailable or export refused. Existing application state was not changed.\n');return 1
    finally:
        for number, handler in previous.items(): signal.signal(number,handler)
