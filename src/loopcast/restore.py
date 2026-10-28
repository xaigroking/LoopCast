"""Restore into a fresh private directory, with the send barrier durable first."""
from contextlib import closing
import os
from pathlib import Path
import shutil
import sqlite3
import stat
from .backup import verify_backup, new_target, regular, copy_reference, readonly, private_staging
from .ownership import DirectoryLock, directory
from .storage import Store
from .operations_storage import ReadOnlyOperations


class RestoreConnection(sqlite3.Connection):
    """One private in-memory preparation shared by the existing Store methods."""
    def close(self): pass  # Store's short connection scopes do not discard it.
    def finish(self): super().close()


def restore_backup(backup, expected_digest, destination, *, expected_parent=None):
    manifest = verify_backup(backup, expected_digest)
    source, source_fd = directory(backup)
    stage_owner = None
    try:
        target = new_target(destination, source)
        with private_staging(target, '.loopcast-restore-', expected_parent) as (staging, staging_fd, check, publish):
            entries = {entry['path']: (entry['size'], entry['sha256']) for entry in manifest['files']}
            with os.fdopen(regular(source_fd, 'state.sqlite3'), 'rb') as origin:
                with os.fdopen(os.open('state.sqlite3', os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=staging_fd), 'w+b') as copied:
                    shutil.copyfileobj(origin, copied); copied.flush(); os.fsync(copied.fileno())
                    for name, expected in entries.items():
                        if name != 'state.sqlite3': copy_reference(source_fd, name, staging / name, expected)
                    with os.fdopen(regular(source_fd, 'manifest.json'), 'rb') as stream: raw = stream.read()
                    with os.fdopen(os.open('manifest.json', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=staging_fd), 'wb') as stream:
                        stream.write(raw); stream.flush(); os.fsync(stream.fileno())
                    verify_backup(staging, expected_digest, directory_fd=staging_fd)
                    stage_owner = DirectoryLock.from_directory(staging, staging_fd)
                    os.rename('manifest.json', 'restore-manifest.json', src_dir_fd=staging_fd, dst_dir_fd=staging_fd)
                    # SQLite must never re-resolve a replaceable staging pathname
                    # for migration/guard writes. Apply the same Store operations
                    # to a private snapshot, then serialize to the held DB file.
                    connection = sqlite3.connect(':memory:', factory=RestoreConnection, isolation_level=None)
                    try:
                        copied.seek(0); connection.deserialize(copied.read())
                        connection.row_factory = sqlite3.Row; connection.execute('PRAGMA foreign_keys=ON')
                        store = Store(Path(os.readlink(staging)), _restore_connection=connection)
                        status = store.prepare_restored_state(manifest, expected_digest)
                        connection.execute('VACUUM')
                        prepared = connection.serialize()
                    finally: connection.finish()
                    check()
                    named = os.stat('state.sqlite3', dir_fd=staging_fd, follow_symlinks=False)
                    held = os.fstat(copied.fileno())
                    if not stat.S_ISREG(named.st_mode) or named.st_nlink != 1 or (held.st_dev, held.st_ino) != (named.st_dev, named.st_ino): raise ValueError('STAGING_CHANGED')
                    copied.seek(0); copied.write(prepared); copied.truncate(); copied.flush(); os.fsync(copied.fileno())
            # Fixed media directories stay under the held stage.
            try: os.mkdir('artifacts', 0o700, dir_fd=staging_fd)
            except FileExistsError: pass
            for folder in ('artifacts', 'library'):
                try: fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=staging_fd)
                except FileNotFoundError: continue
                try: os.fsync(fd)
                finally: os.close(fd)
            publish()
        return {'state': 'RESTORED_GUARDED', 'restore_id': status['restore']['id'],
                'guard_version': status['restore_guard']['version'], 'scope': status['restore']['scope'],
                'backup_id': manifest['backup_id'], 'upload_allowed': False, 'credentials_policy': 'excluded_reauthorize'}
    finally:
        if stage_owner is not None: stage_owner.close()
        os.close(source_fd)


def reconciliation_report(path):
    # This report never initializes Store, migrates, or contacts an adapter.
    _, handle = directory(path)
    try:
        with closing(readonly(handle)) as db:
            schemas = [row[0] for row in db.execute('SELECT version FROM schema_version')]
            if schemas not in ([7], [8], [9]):raise ValueError('SCHEMA_UNSUPPORTED')
            db.execute('BEGIN')
            return ReadOnlyOperations(db).operations_reconciliation()
    finally:
        os.close(handle)
