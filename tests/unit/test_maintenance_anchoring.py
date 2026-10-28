import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from loopcast.backup import create_backup, code_identity
from loopcast.restore import restore_backup
from loopcast.storage import Store
from loopcast.ownership import DirectoryLock


class MaintenanceAnchoringTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.source = self.root / 'source'
        Store(self.source); self.source.chmod(0o700)
        self.outside = self.root / 'outside'; self.outside.mkdir(mode=0o700)
        self.canary = self.outside / 'state.sqlite3'; self.canary.write_bytes(b'external-canary'); self.canary.chmod(0o600)

    def assert_canary(self):
        self.assertEqual(self.canary.read_bytes(), b'external-canary')
        self.assertEqual(sorted(p.name for p in self.outside.iterdir()), ['state.sqlite3'])

    def test_backup_stage_replacement_cannot_redirect_manifest_or_database_writes(self):
        def substitute():
            stage = next(self.root.glob('.loopcast-backup-*'))
            stage.rename(self.root / 'held-stage'); stage.symlink_to(self.outside, target_is_directory=True)
            return code_identity()
        with patch('loopcast.backup.code_identity', side_effect=substitute):
            with self.assertRaisesRegex(ValueError, 'STAGING_CHANGED'):
                create_backup(self.source, self.root / 'package')
        self.assert_canary(); self.assertFalse((self.root / 'package').exists())
        self.assertTrue((self.root / 'held-stage' / 'manifest.json').is_file())
        self.assertTrue(next(self.root.glob('.loopcast-backup-*')).is_symlink())

    def test_parent_replacement_never_publishes_in_the_replacement_directory(self):
        parent = self.root / 'destination'; parent.mkdir(mode=0o700)
        original = code_identity
        def substitute():
            parent.rename(self.root / 'held-parent'); parent.mkdir(mode=0o700)
            (parent / 'keep').write_text('replacement')
            return original()
        with patch('loopcast.backup.code_identity', side_effect=substitute):
            with self.assertRaisesRegex(ValueError, 'PARENT_CHANGED'):
                create_backup(self.source, parent / 'package')
        self.assertEqual(sorted(p.name for p in parent.iterdir()), ['keep'])
        self.assertEqual((parent / 'keep').read_text(), 'replacement')

    def test_restore_stage_and_database_replacement_cannot_redirect_store_migration_or_guard_writes(self):
        package = self.root / 'package'; created = create_backup(self.source, package)
        before = {p.relative_to(package): p.read_bytes() for p in package.rglob('*') if p.is_file()}
        prepare = Store.prepare_restored_state
        for kind in ['directory', 'database']:
            with self.subTest(kind=kind):
                def substitute(store, manifest, digest):
                    stage = next(p for p in self.root.glob('.loopcast-restore-*') if not p.is_symlink())
                    if kind == 'directory':
                        stage.rename(self.root / 'held-restore'); stage.symlink_to(self.outside, target_is_directory=True)
                    else:
                        (stage / 'state.sqlite3').rename(stage / 'held.sqlite3')
                        (stage / 'state.sqlite3').symlink_to(self.canary)
                    return prepare(store, manifest, digest)
                with patch.object(Store, 'prepare_restored_state', substitute):
                    with self.assertRaisesRegex(ValueError, 'STAGING_CHANGED'):
                        restore_backup(package, created['manifest_sha256'], self.root / ('restored-' + kind))
                self.assert_canary()
                self.assertFalse((self.root / ('restored-' + kind)).exists())
        self.assertEqual(before, {p.relative_to(package): p.read_bytes() for p in package.rglob('*') if p.is_file()})

    def test_restore_keeps_the_existing_directory_owner_during_guard_preparation(self):
        package = self.root / 'package'; created = create_backup(self.source, package)
        prepare = Store.prepare_restored_state
        observed = []
        def require_owner(store, manifest, digest):
            with self.assertRaises(BlockingIOError): DirectoryLock(store.directory)
            observed.append(True)
            return prepare(store, manifest, digest)
        with patch.object(Store, 'prepare_restored_state', require_owner):
            restore_backup(package, created['manifest_sha256'], self.root / 'restored')
        self.assertEqual(observed, [True])
        with DirectoryLock(self.root / 'restored'): pass


    def test_snapshot_and_prepared_guard_sync_failures_never_publish_a_target(self):
        package = self.root / 'package'; created = create_backup(self.source, package)
        fsync = os.fsync
        for action in ['backup', 'restore']:
            with self.subTest(action=action):
                target = self.root / ('failed-' + action); count = []
                def fail_at_database(handle):
                    path = os.readlink('/proc/self/fd/' + str(handle))
                    prefix = '.loopcast-backup-' if action == 'backup' else '.loopcast-restore-'
                    if prefix in path and path.endswith('/state.sqlite3'):
                        count.append(path)
                        if len(count) == (1 if action == 'backup' else 2):
                            raise OSError(5, 'Controlled database fsync failure')
                    return fsync(handle)
                with patch('os.fsync', side_effect=fail_at_database):
                    with self.assertRaisesRegex(OSError, 'fsync failure'):
                        if action == 'backup': create_backup(self.source, target)
                        else: restore_backup(package, created['manifest_sha256'], target)
                self.assertEqual(len(count), 1 if action == 'backup' else 2)
                self.assertFalse(target.exists()); self.assertEqual(list(self.root.glob('.loopcast-*')), [])
                self.assert_canary()
        with DirectoryLock(self.source): pass

    def test_parent_sync_failure_after_rename_retains_published_package_and_active_restore_guard(self):
        package = self.root / 'package'; created = create_backup(self.source, package)
        fsync = os.fsync
        for action in ['backup', 'restore']:
            with self.subTest(action=action):
                target = self.root / ('published-' + action); failed = []
                def fail_after_publication(handle):
                    if os.readlink('/proc/self/fd/' + str(handle)) == str(self.root) and target.exists():
                        failed.append(True); raise OSError(5, 'Controlled parent fsync failure')
                    return fsync(handle)
                with patch('os.fsync', side_effect=fail_after_publication):
                    with self.assertRaisesRegex(OSError, 'fsync failure'):
                        if action == 'backup': create_backup(self.source, target)
                        else: restore_backup(package, created['manifest_sha256'], target)
                self.assertEqual(failed, [True]); self.assertTrue(target.is_dir())
                before = {p.relative_to(target): p.read_bytes() for p in target.rglob('*') if p.is_file()}
                with self.assertRaises(ValueError):
                    if action == 'backup': create_backup(self.source, target)
                    else: restore_backup(package, created['manifest_sha256'], target)
                self.assertEqual(before, {p.relative_to(target): p.read_bytes() for p in target.rglob('*') if p.is_file()})
                if action == 'restore':
                    status = Store(target).operations_status()
                    self.assertEqual(status['restore_guard']['state'], 'ACTIVE')
                    self.assertFalse(status['upload_allowed_by_recovery'])
                self.assert_canary()
