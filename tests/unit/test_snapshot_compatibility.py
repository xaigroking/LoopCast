from contextlib import closing
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from loopcast.backup import create_backup, create_owned_backup
from loopcast.ownership import DirectoryLock
from loopcast.restore import restore_backup
from loopcast.storage import Store


class SnapshotCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.source = self.root / 'source'; self.store = Store(self.source)

    def test_existing_cli_can_publish_a_private_package_under_a_shared_parent(self):
        parent = self.root / 'parent'; parent.mkdir(mode=0o755); parent.chmod(0o755)
        created = create_backup(self.source, parent / 'package')
        restored = restore_backup(parent / 'package', created['manifest_sha256'], parent / 'restored')
        self.assertFalse(restored['upload_allowed'])
        for folder in ['package', 'restored']:
            self.assertEqual((parent / folder).stat().st_mode & 0o077, 0)
            for path in (parent / folder).rglob('*'): self.assertEqual(path.stat().st_mode & 0o077, 0)
        # Native plans still impose their stronger selected-parent requirement.
        info = parent.stat()
        with DirectoryLock(self.source) as owner:
            with self.assertRaisesRegex(ValueError, 'PRIVATE_DIRECTORY_REQUIRED'):
                create_owned_backup(owner, parent / 'native-rejected', expected_parent=[str(info.st_dev), str(info.st_ino)])
        self.assertFalse((parent / 'native-rejected').exists())

    def test_snapshot_and_restore_preserve_implicit_rowids_and_committed_wal_gaps(self):
        db = self.store.connect(); self.addCleanup(db.close)
        db.execute('PRAGMA wal_autocheckpoint=0')
        for identity in ['removed-first', 'kept-second', 'kept-third']:
            db.execute('INSERT INTO studio_projects VALUES(?,?,?,?,?)', (identity, '{}', 1, 'created', 'updated'))
        db.execute("DELETE FROM studio_projects WHERE id='removed-first'")
        expected = [tuple(row) for row in db.execute('SELECT rowid,* FROM studio_projects ORDER BY rowid')]
        self.assertEqual([row[0] for row in expected], [2, 3])
        created = create_backup(self.source, self.root / 'package')
        restore_backup(self.root / 'package', created['manifest_sha256'], self.root / 'restored')
        for folder in ['package', 'restored']:
            with closing(sqlite3.connect(self.root / folder / 'state.sqlite3')) as copied:
                self.assertEqual(copied.execute('SELECT rowid,* FROM studio_projects ORDER BY rowid').fetchall(), expected)
            self.assertEqual((self.root / folder / 'state.sqlite3').read_bytes()[18:20], b'\1\1')
