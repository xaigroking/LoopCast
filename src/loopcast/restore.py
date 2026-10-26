"""Restore into a fresh private directory, with the send barrier durable first."""
from contextlib import closing
import os
from pathlib import Path
import shutil
import tempfile
from .backup import verify_backup, new_target, regular, copy_reference, sync_directory, publish_directory, readonly
from .ownership import DirectoryLock, directory
from .storage import Store
from .operations_storage import ReadOnlyOperations


def restore_backup(backup, expected_digest, destination):
    manifest = verify_backup(backup, expected_digest)
    source, source_fd = directory(backup)
    staging = None
    try:
        target = new_target(destination, source)
        staging = Path(tempfile.mkdtemp(prefix='.loopcast-restore-', dir=target.parent))
        entries = {entry['path']: (entry['size'], entry['sha256']) for entry in manifest['files']}
        with os.fdopen(regular(source_fd, 'state.sqlite3'), 'rb') as origin:
            with os.fdopen(os.open(staging / 'state.sqlite3', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb') as copied:
                shutil.copyfileobj(origin, copied); copied.flush(); os.fsync(copied.fileno())
        for name, expected in entries.items():
            if name != 'state.sqlite3':
                copy_reference(source_fd, name, staging / name, expected)
        with os.fdopen(regular(source_fd, 'manifest.json'), 'rb') as origin:
            raw = origin.read()
        with os.fdopen(os.open(staging / 'manifest.json', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb') as copied:
            copied.write(raw); copied.flush(); os.fsync(copied.fileno())
        verify_backup(staging, expected_digest)
        (staging / 'manifest.json').rename(staging / 'restore-manifest.json')
        with DirectoryLock(staging):
            store = Store(staging)
            status = store.prepare_restored_state(manifest, expected_digest)
            with closing(store.connect()) as db:
                db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            for path in staging.rglob('*'):
                if path.is_dir():path.chmod(0o700)
                elif path.is_file():
                    path.chmod(0o600)
                    with path.open('rb') as stream:os.fsync(stream.fileno())
            for path in (staging / 'artifacts', staging / 'library'):
                if path.is_dir():sync_directory(path)
            sync_directory(staging)
            publish_directory(staging, target)
        return {'state': 'RESTORED_GUARDED', 'restore_id': status['restore']['id'],
                'guard_version': status['restore_guard']['version'], 'scope': status['restore']['scope'],
                'backup_id': manifest['backup_id'], 'upload_allowed': False, 'credentials_policy': 'excluded_reauthorize'}
    finally:
        os.close(source_fd)
        if staging is not None and staging.exists():shutil.rmtree(staging)


def reconciliation_report(path):
    # This report never initializes Store, migrates, or contacts an adapter.
    _, handle = directory(path)
    try:
        with closing(readonly(handle)) as db:
            schemas = [row[0] for row in db.execute('SELECT version FROM schema_version')]
            if schemas != [7]:raise ValueError('SCHEMA_UNSUPPORTED')
            db.execute('BEGIN')
            return ReadOnlyOperations(db).operations_reconciliation()
    finally:
        os.close(handle)
