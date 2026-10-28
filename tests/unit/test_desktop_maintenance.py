import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from loopcast.desktop_maintenance import execute, identity
from loopcast.ownership import DirectoryLock
from loopcast.storage import Store
from loopcast.backup import create_backup


class DesktopMaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.source = self.root / 'data'
        Store(self.source); self.source.chmod(0o700)
        self.capability = os.urandom(32).hex()
        self.request = {'source': str(self.source), 'source_identity': identity(self.source.stat()), 'capability': self.capability}

    def call(self, action, **values):
        return execute(dict(self.request, action=action, **values))

    def test_marker_fences_ordinary_owner_and_existing_backup_until_explicit_finish(self):
        value = self.call('begin')
        self.assertEqual(value['identity'], hashlib.sha256(bytes.fromhex(self.capability)).hexdigest()[:32])
        with self.assertRaisesRegex(ValueError, 'MAINTENANCE_UNCONFIRMED'):
            DirectoryLock(self.source)
        with self.assertRaisesRegex(ValueError, 'MAINTENANCE_UNCONFIRMED'):
            create_backup(self.source, self.root / 'rejected')
        created = self.call('create', target=str(self.root / 'package'))
        self.assertEqual(created['state'], 'VERIFIED_PRIVATE_BACKUP')
        self.assertNotIn(self.capability, json.dumps(created))
        self.assertNotIn(str(self.source), json.dumps(created))
        self.call('finish')
        with DirectoryLock(self.source): pass

    def test_identity_is_not_completion_capability_and_bad_source_retains_marker(self):
        value = self.call('begin')
        with self.assertRaisesRegex(ValueError, 'CAPABILITY_REQUIRED'):
            execute(dict(self.request, action='finish', capability=value['identity']))
        with self.assertRaisesRegex(ValueError, 'MAINTENANCE_UNCONFIRMED'):
            execute(dict(self.request, action='finish', capability='1' * 64))
        moved = self.root / 'moved'; self.source.rename(moved); self.source.mkdir(mode=0o700)
        with self.assertRaises(ValueError): self.call('create', target=str(self.root / 'must-not-create'))
        self.assertFalse((self.root / 'must-not-create').exists())
        self.assertTrue((moved / 'desktop-maintenance.guard.v1').exists())

    def test_restore_keeps_source_fenced_and_target_guarded_without_starting_it(self):
        self.call('begin')
        created = self.call('create', target=str(self.root / 'package'))
        restored = self.call('restore', backup=str(self.root / 'package'), digest=created['manifest_sha256'], target=str(self.root / 'restored'))
        self.assertFalse(restored['upload_allowed'])
        self.assertEqual(restored['state'], 'RESTORED_GUARDED')
        self.assertTrue((self.source / 'desktop-maintenance.guard.v1').exists())
        with self.assertRaisesRegex(ValueError, 'DESTINATION_EXISTS'):
            self.call('restore', backup=str(self.root / 'package'), digest=created['manifest_sha256'], target=str(self.root / 'restored'))
        self.call('finish')

    def test_source_owner_or_desktop_guard_prevents_begin(self):
        with DirectoryLock(self.source):
            with self.assertRaises(BlockingIOError): self.call('begin')
        (self.source / 'desktop-runtime.guard.v1').write_text(json.dumps({'format': 'loopcast.desktop-guard.v1', 'identity': 'a' * 32}))
        (self.source / 'desktop-runtime.guard.v1').chmod(0o600)
        with self.assertRaisesRegex(ValueError, 'DESKTOP_CLEANUP_UNCONFIRMED'): self.call('begin')
        self.assertFalse((self.source / 'desktop-maintenance.guard.v1').exists())
