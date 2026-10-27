from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from loopcast.backup import create_backup
from loopcast.restore import restore_backup
from loopcast.storage import Store
from loopcast.ownership import DirectoryLock, directory
from loopcast import workspace_inspection as w
from loopcast import desktop_diagnostics as d

class WorkspaceInspectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.source = self.root/'original'
        Store(self.source)
        self.package = self.root/'package'; self.created = create_backup(self.source, self.package)
        self.target = self.root/'restored'; self.restored = restore_backup(self.package, self.created['manifest_sha256'], self.target)
        self.expected = dict(expected_digest=self.created['manifest_sha256'], expected_backup_id=self.created['backup_id'], expected_restore_id=self.restored['restore_id'])

    def observe(self, path=None, **options):
        return w.inspect_restored(path or self.target, **(self.expected | options))

    def unchanged(self):
        # Reads may advance atime; business contents, mode, mtime and membership must not change.
        return {str(p.relative_to(self.root)):(p.read_bytes(),p.stat().st_mode,p.stat().st_mtime_ns)
                for p in self.root.rglob('*') if p.is_file() and not p.is_symlink()}

    def test_actual_restore_observation_is_read_only_and_never_admission(self):
        before=self.unchanged(); value=self.observe()
        self.assertEqual(value['state'],'OBSERVED_GUARDED')
        self.assertEqual(value['restore_id'],self.restored['restore_id'])
        self.assertFalse(value['start_authorized']);self.assertFalse(value['upload_allowed'])
        self.assertEqual(value['identity_scope'],'KERNEL_BOOT')
        self.assertEqual(before,self.unchanged())
        self.assertNotIn(str(self.root),json.dumps(value))
        self.assertEqual(set(value),{'format','observation_scope','state','start_authorized','upload_allowed','backup_id','manifest_sha256','restore_id','execution_epoch','guard_version','scope','schema_version','identity_scope','filesystem','boot_sha256','object_sha256','object_family_sha256'})

    def test_missing_sources_wrong_proof_guard_and_epoch_refuse_without_repairs(self):
        for key,value in [('expected_digest',None),('expected_backup_id','invalid'),('expected_restore_id','0'*32)]:
            before=self.unchanged()
            with self.assertRaises(d.DiagnosticError):self.observe(**{key:value})
            self.assertEqual(before,self.unchanged())
        with self.assertRaises(d.DiagnosticError):self.observe(expected_digest='0'*64)
        with closing(sqlite3.connect(self.target/'state.sqlite3')) as db:
            db.execute("UPDATE operations_state SET execution_epoch=?",('0'*32,));db.commit()
        before=self.unchanged()
        with self.assertRaises(d.DiagnosticError) as error:self.observe()
        self.assertEqual(error.exception.code,'RESTORE_SOURCE_MISMATCH')
        self.assertEqual(before,self.unchanged())
        missing=self.root/'missing'
        with self.assertRaises(FileNotFoundError):self.observe(missing)
        self.assertFalse(missing.exists())
        (self.target/'service.lock').unlink();before=self.unchanged()
        with self.assertRaises(d.DiagnosticError) as error:self.observe()
        self.assertEqual(error.exception.code,'LOCK_MISSING');self.assertEqual(before,self.unchanged())

    def test_existing_owner_and_each_execution_barrier_refuse(self):
        with DirectoryLock(self.target):
            with self.assertRaises(d.DiagnosticError) as error:self.observe()
            self.assertEqual(error.exception.code,'OWNER_HELD')
        for name,code in [('desktop-runtime.guard.v1','DESKTOP_CLEANUP_UNCONFIRMED'),('desktop-maintenance.guard.v1','MAINTENANCE_UNCONFIRMED')]:
            guard=self.target/name
            guard.write_text(json.dumps({'format':'loopcast.desktop-guard.v1','identity':'a'*32}));guard.chmod(0o600)
            before=self.unchanged()
            with self.assertRaises(d.DiagnosticError) as error:self.observe()
            self.assertEqual(error.exception.code,code);self.assertEqual(before,self.unchanged());guard.unlink()
        with closing(sqlite3.connect(self.target/'state.sqlite3')) as db:
            db.execute('DELETE FROM restore_guard');db.commit()
        with self.assertRaises(d.DiagnosticError) as error:self.observe()
        self.assertEqual(error.exception.code,'RESTORE_GUARD_MISMATCH')

    def test_symlink_copy_and_recreation_do_not_inherit_the_entity(self):
        original=self.observe()['object_sha256']
        alias=self.root/'alias';alias.symlink_to(self.target,target_is_directory=True)
        with self.assertRaises(OSError):self.observe(alias)
        copy=self.root/'copy';shutil.copytree(self.target,copy)
        self.assertNotEqual(original,self.observe(copy)['object_sha256'])
        shutil.rmtree(self.target);shutil.copytree(copy,self.target)
        self.assertNotEqual(original,self.observe()['object_sha256'])

    def test_committed_wal_is_observed_without_source_sidecars_or_mutation(self):
        with closing(sqlite3.connect(self.target/'state.sqlite3')) as writer:
            writer.execute('PRAGMA journal_mode=WAL');writer.execute('PRAGMA wal_autocheckpoint=0')
            writer.execute('UPDATE restore_guard SET version=version+1');writer.commit()
            # Remove shm while there is no SQLite reader; the observer must not create it.
            shm=self.target/'state.sqlite3-shm'
            if shm.exists():shm.unlink()
            before=self.unchanged();value=self.observe()
            self.assertEqual(value['guard_version'],2);self.assertEqual(before,self.unchanged());self.assertFalse(shm.exists())

    def test_replacement_during_snapshot_is_rejected_and_external_canary_retained(self):
        original=d.database_snapshot
        from contextlib import contextmanager
        @contextmanager
        def changed(fd,budget):
            with original(fd,budget) as value:
                self.target.rename(self.root/'held-restored')
                self.target.mkdir(mode=0o700);(self.target/'external-canary').write_bytes(b'keep')
                yield value
        with patch.object(d,'database_snapshot',changed):
            with self.assertRaises(d.DiagnosticError):self.observe()
        self.assertEqual((self.target/'external-canary').read_bytes(),b'keep')

    def test_cancellation_budget_and_missing_kernel_capability_never_fall_back(self):
        before=self.unchanged()
        for options in [{'cancelled':lambda:True},{'max_bytes':1}]:
            with self.assertRaises(d.DiagnosticError):self.observe(**options)
        with patch.object(w.platform,'machine',return_value='aarch64'):
            with self.assertRaises(d.DiagnosticError) as error:self.observe()
            self.assertEqual(error.exception.code,'PLATFORM_UNSUPPORTED')
        with patch.object(w.ctypes,'CDLL') as library:
            library.return_value.fstatfs.return_value=-1
            with self.assertRaises(d.DiagnosticError) as error:self.observe()
            self.assertEqual(error.exception.code,'ENTITY_IDENTITY_UNAVAILABLE')
        self.assertEqual(before,self.unchanged())

    def test_missing_unique_mount_identity_does_not_fall_back_to_reusable_mount_id(self):
        real=w.ctypes.CDLL(None,use_errno=True)
        class NoUnique:
            def __call__(self,*_args): return 0  # Zero result mask: field unavailable.
        class Library:
            statx=NoUnique()
            def __getattr__(self,name): return getattr(real,name)
        with patch.object(w.ctypes,'CDLL',return_value=Library()):
            with self.assertRaises(d.DiagnosticError) as error:self.observe()
            self.assertEqual(error.exception.code,'ENTITY_IDENTITY_UNAVAILABLE')
