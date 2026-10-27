"""Maintenance must never clean a replacement object under an old stage name."""
from pathlib import Path
import hashlib
import tempfile
import unittest
from unittest.mock import patch
from loopcast.backup import create_backup
from loopcast.restore import restore_backup
from loopcast.domain import uid
from loopcast.storage import Store

class MaintenancePathTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.store=Store(self.root/'source')
        run,_=self.store.create({'title':'Owned fixture'},uid());attempt=self.store.claim()
        name=uid()+'.txt';raw=b'owned fixture';(self.store.artifact_dir/name).write_bytes(raw)
        self.store.finish(run['run_id'],attempt['token'],'SUCCEEDED',artifact={'id':uid(),'size':len(raw),'sha256':hashlib.sha256(raw).hexdigest(),'storage_ref':name})
        self.parent=self.root/'destinations';self.parent.mkdir(mode=0o700)

    def test_backup_parent_replacement_preserves_the_foreign_same_name_stage(self):
        canary=[]
        def replace(_source,relative,destination,_expected,**_options):
            stage=Path(destination).parents[1]
            self.parent.rename(self.root/'owned-parent')
            self.parent.mkdir(mode=0o700)
            foreign=self.parent/stage.name;foreign.mkdir(mode=0o700)
            path=foreign/'external-canary';path.write_bytes(b'keep external object');canary.append(path)
            raise OSError('controlled copy interruption')
        with patch('loopcast.backup.copy_reference',side_effect=replace):
            with self.assertRaises(OSError):create_backup(self.store.directory,self.parent/'backup')
        self.assertTrue(bool(canary) and canary[0].exists(),'Cleanup removed a replacement directory')
        self.assertEqual(canary[0].read_bytes(),b'keep external object')
        self.assertFalse((self.parent/'backup').exists())

    def test_restore_stage_replacement_preserves_the_foreign_object(self):
        package=self.root/'package';created=create_backup(self.store.directory,package);canary=[]
        def replace(store,*_args,**_kwargs):
            stage=Path(store.directory)
            stage.rename(self.root/'owned-restore-stage')
            stage.mkdir(mode=0o700)
            path=stage/'external-canary';path.write_bytes(b'keep external object');canary.append(path)
            raise RuntimeError('controlled restore interruption')
        with patch.object(Store,'prepare_restored_state',replace):
            with self.assertRaises(RuntimeError):restore_backup(package,created['manifest_sha256'],self.parent/'restored')
        self.assertTrue(bool(canary) and canary[0].exists(),'Cleanup removed a replacement directory')
        self.assertEqual(canary[0].read_bytes(),b'keep external object')
        self.assertFalse((self.parent/'restored').exists())
