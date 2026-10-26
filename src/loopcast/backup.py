"""Offline, verified private backups. No credentials or executable workspaces."""
from contextlib import closing
import ctypes
from datetime import datetime, timezone
import hashlib
from importlib.metadata import version
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import stat
import tempfile
import uuid
from .migrations import CURRENT_VERSION
from .ownership import DirectoryLock, directory

FORMAT = 'loopcast.backup.v1'
MAX_MANIFEST = 8 * 1024 * 1024


def checksum(path):
    with open(path, 'rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def regular(parent_fd, name):
    handle = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent_fd)
    info = os.fstat(handle)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        os.close(handle)
        raise ValueError('UNSAFE_FILE')
    return handle


def readonly(root_fd):
    handle = regular(root_fd, 'state.sqlite3')
    os.close(handle)
    db = sqlite3.connect(f'file:/proc/self/fd/{root_fd}/state.sqlite3?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA query_only=ON')
    return db


def check_database(db):
    schemas = db.execute('SELECT version FROM schema_version').fetchall()
    if len(schemas) != 1 or schemas[0][0] not in {6, CURRENT_VERSION}:
        raise ValueError('SCHEMA_UNSUPPORTED')
    if db.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
        # Row factories differ between backup and inspection connections.
        if [tuple(row) for row in db.execute('PRAGMA integrity_check')] != [('ok',)]:
            raise ValueError('DATABASE_INVALID')
    if db.execute('PRAGMA foreign_key_check').fetchone():
        raise ValueError('DATABASE_REFERENCES_INVALID')
    return schemas[0][0]


def references(db):
    result = {}
    for table, folder in (('artifacts', 'artifacts'), ('studio_assets', 'library')):
        for row in db.execute('SELECT storage_ref,size,sha256 FROM ' + table):
            name, size, digest = tuple(row)
            if not isinstance(name, str) or not name or PurePosixPath(name).name != name or '\\' in name or name in {'.', '..'}:
                raise ValueError('REFERENCE_PATH_INVALID')
            if type(size) is not int or size < 0 or not hex_digest(digest):
                raise ValueError('REFERENCE_INVALID')
            key = folder + '/' + name
            if key in result and result[key] != (size, digest):
                raise ValueError('REFERENCE_CONFLICT')
            result[key] = (size, digest)
    return result


def hex_digest(value):
    return isinstance(value, str) and len(value) == 64 and all(c in '0123456789abcdef' for c in value)


def code_identity():
    value = hashlib.sha256()
    for path in sorted(Path(__file__).parent.glob('*.py')):
        value.update(path.name.encode() + b'\0' + path.read_bytes() + b'\0')
    return {'version': {'value': version('loopcast'), 'status': 'unverified', 'source': 'distribution_metadata'},
            'installed_code_sha256': {'value': value.hexdigest(), 'status': 'verified', 'source': 'installed_python_files'},
            'build_source': {'value': None, 'status': 'unavailable', 'source': 'not_recorded'}}


def sync_directory(path):
    handle = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(handle)
    finally:
        os.close(handle)


def publish_directory(staging, target):
    _, handle = directory(target.parent)
    try:
        rename = ctypes.CDLL(None, use_errno=True).renameat2
        rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        rename.restype = ctypes.c_int
        if rename(handle, os.fsencode(staging.name), handle, os.fsencode(target.name), 1):
            code = ctypes.get_errno()
            raise OSError(code, 'Unable to publish private directory')
        os.fsync(handle)
    finally:
        os.close(handle)


def copy_reference(source_fd, relative, destination, expected):
    folder, name = relative.split('/')
    parent = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=source_fd)
    try:
        handle = regular(parent, name)
        with os.fdopen(handle, 'rb') as origin:
            before = os.fstat(origin.fileno())
            destination.parent.mkdir(mode=0o700, exist_ok=True)
            out = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(out, 'wb') as target:
                shutil.copyfileobj(origin, target)
                target.flush()
                os.fsync(target.fileno())
            after = os.fstat(origin.fileno())
            if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise ValueError('SOURCE_CHANGED')
        if (destination.stat().st_size, checksum(destination)) != expected:
            raise ValueError('FILE_INTEGRITY_FAILED')
    finally:
        os.close(parent)


def new_target(path, source=None):
    path = Path(path).expanduser()
    if '..' in path.parts or not path.name:
        raise ValueError('UNSAFE_DESTINATION')
    parent, handle = directory(path.absolute().parent)
    os.close(handle)
    target = parent / path.name
    if target.exists() or target.is_symlink():
        raise ValueError('DESTINATION_EXISTS')
    if source and (target.is_relative_to(source) or source.is_relative_to(target)):
        raise ValueError('OVERLAPPING_DIRECTORIES')
    return target


def create_backup(source, output):
    with DirectoryLock(source) as owner:
        target = new_target(output, owner.path)
        staging = Path(tempfile.mkdtemp(prefix='.loopcast-backup-', dir=target.parent))
        try:
            with closing(readonly(owner.directory_fd)) as origin:
                check_database(origin)
                with closing(sqlite3.connect(staging / 'state.sqlite3')) as copied:
                    origin.backup(copied)
                    copied.execute('PRAGMA journal_mode=DELETE')
                    schema = check_database(copied)
                    refs = references(copied)
                    counts = {row[0]: copied.execute('SELECT COUNT(*) FROM "' + row[0].replace('"', '""') + '"').fetchone()[0]
                              for row in copied.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")}
                    cursor = copied.execute('SELECT COALESCE(MAX(id),0) FROM events').fetchone()[0]
            (staging / 'state.sqlite3').chmod(0o600)
            for relative, expected in sorted(refs.items()):
                copy_reference(owner.directory_fd, relative, staging / relative, expected)
            paths = ['state.sqlite3', *sorted(refs)]
            manifest = {'format': FORMAT, 'backup_id': uuid.uuid4().hex,
                        'created_at': datetime.now(timezone.utc).isoformat(), 'schema_version': schema,
                        'credentials_policy': 'excluded_reauthorize', 'application': code_identity(),
                        'boundary': {'last_event_id': cursor, 'external_publication_coverage': 'unknown'}, 'counts': counts,
                        'files': [{'path': name, 'size': (staging / name).stat().st_size, 'sha256': checksum(staging / name)} for name in sorted(paths)]}
            raw = (json.dumps(manifest, sort_keys=True, indent=2) + '\n').encode()
            with os.fdopen(os.open(staging / 'manifest.json', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb') as stream:
                stream.write(raw); stream.flush(); os.fsync(stream.fileno())
            digest = hashlib.sha256(raw).hexdigest()
            verify_backup(staging, digest)
            for folder in sorted((p for p in staging.iterdir() if p.is_dir())):
                sync_directory(folder)
            with (staging / 'state.sqlite3').open('rb') as stream:
                os.fsync(stream.fileno())
            sync_directory(staging)
            publish_directory(staging, target)
            return {'format': FORMAT, 'backup_id': manifest['backup_id'], 'manifest_sha256': digest,
                    'schema_version': schema, 'files': len(paths), 'state': 'VERIFIED_PRIVATE_BACKUP',
                    'credentials_policy': 'excluded_reauthorize'}
        finally:
            if staging.exists():
                shutil.rmtree(staging)


def verify_backup(backup, expected_digest):
    if not hex_digest(expected_digest):
        raise ValueError('MANIFEST_DIGEST_REQUIRED')
    root, handle = directory(backup)
    try:
        if os.fstat(handle).st_mode & 0o077:
            raise ValueError('BACKUP_NOT_PRIVATE')
        with os.fdopen(regular(handle, 'manifest.json'), 'rb') as stream:
            if os.fstat(stream.fileno()).st_mode & 0o077:
                raise ValueError('BACKUP_NOT_PRIVATE')
            raw = stream.read(MAX_MANIFEST + 1)
        if len(raw) > MAX_MANIFEST or hashlib.sha256(raw).hexdigest() != expected_digest:
            raise ValueError('MANIFEST_INTEGRITY_FAILED')
        manifest = json.loads(raw)
        required = {'format', 'backup_id', 'created_at', 'schema_version', 'credentials_policy', 'application', 'boundary', 'counts', 'files'}
        if not isinstance(manifest, dict) or set(manifest) != required or manifest['format'] != FORMAT or manifest['credentials_policy'] != 'excluded_reauthorize':
            raise ValueError('BACKUP_FORMAT_UNSUPPORTED')
        identity = manifest['backup_id']
        if not isinstance(identity, str) or len(identity) != 32 or any(c not in '0123456789abcdef' for c in identity) or type(manifest['schema_version']) is not int:
            raise ValueError('MANIFEST_INVALID')
        observed = datetime.fromisoformat(manifest['created_at'])
        if observed.tzinfo is None:
            raise ValueError('MANIFEST_INVALID')
        files = manifest['files']
        if not isinstance(files, list) or not 1 <= len(files) <= 100000:
            raise ValueError('MANIFEST_INVALID')
        entries = {}
        for entry in files:
            if not isinstance(entry, dict) or set(entry) != {'path', 'size', 'sha256'} or type(entry['size']) is not int or entry['size'] < 0 or not hex_digest(entry['sha256']):
                raise ValueError('MANIFEST_INVALID')
            name = entry['path']
            if not isinstance(name, str) or name in entries or PurePosixPath(name).as_posix() != name or (name != 'state.sqlite3' and (len(PurePosixPath(name).parts) != 2 or PurePosixPath(name).parts[0] not in {'artifacts', 'library'})) or '\\' in name or '..' in PurePosixPath(name).parts:
                raise ValueError('REFERENCE_PATH_INVALID')
            entries[name] = (entry['size'], entry['sha256'])
        expected_names = {'manifest.json', *entries}
        expected_folders = {name.split('/')[0] for name in entries if '/' in name}
        actual = set()
        for parent, folders, names in os.walk(root, followlinks=False):
            for name in folders + names:
                path = Path(parent) / name
                if path.is_symlink():
                    raise ValueError('UNSAFE_FILE')
            for name in folders:
                if (Path(parent) / name).relative_to(root).as_posix() not in expected_folders:
                    raise ValueError('BACKUP_FILE_SET_MISMATCH')
            for name in names:
                relative = (Path(parent) / name).relative_to(root).as_posix()
                actual.add(relative)
        if actual != expected_names:
            raise ValueError('BACKUP_FILE_SET_MISMATCH')
        for name, expected in entries.items():
            folder, _, filename = name.rpartition('/')
            parent_fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=handle) if folder else os.dup(handle)
            try:
                with os.fdopen(regular(parent_fd, filename), 'rb') as stream:
                    if os.fstat(stream.fileno()).st_mode & 0o077:
                        raise ValueError('BACKUP_NOT_PRIVATE')
                    if (os.fstat(stream.fileno()).st_size, hashlib.file_digest(stream, 'sha256').hexdigest()) != expected:
                        raise ValueError('FILE_INTEGRITY_FAILED')
            finally:
                os.close(parent_fd)
        with closing(readonly(handle)) as db:
            db.execute('BEGIN')
            schema = check_database(db)
            if schema != manifest['schema_version'] or {'state.sqlite3', *references(db)} != set(entries):
                raise ValueError('DATABASE_MANIFEST_MISMATCH')
            for name, expected in references(db).items():
                if entries[name] != expected:
                    raise ValueError('DATABASE_MANIFEST_MISMATCH')
        return manifest
    finally:
        os.close(handle)
