import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from loopcast import migrations
from loopcast.contracts import manifest, MPT_SOURCE, MPT_REVISION, CAPABILITIES
from loopcast.storage import Store


class MigrationTests(unittest.TestCase):
    def test_previous_database_preserved_and_replayable(self):
        with tempfile.TemporaryDirectory() as directory:
            # Build with the previous release schema, then seed real old-format records.
            db = sqlite3.connect(Path(directory) / 'state.sqlite3')
            db.executescript(migrations.VERSION_ONE)
            db.execute("INSERT INTO runs VALUES('run','fixture','{}','digest','SUCCEEDED',1,'time','time')")
            db.execute("INSERT INTO attempts VALUES('attempt','run',1,'token','SUCCEEDED','time','time',NULL,NULL)")
            db.execute("INSERT INTO artifacts VALUES('artifact','run','attempt','text/plain',0,'digest','report.txt')")
            db.execute("INSERT INTO events VALUES(1,'run',1,'finished','time','{}')")
            db.execute("INSERT INTO idempotency VALUES('create','key','digest','run')")
            db.commit()
            tables = ['runs', 'attempts', 'artifacts', 'events', 'idempotency']
            before = {t: db.execute('SELECT * FROM ' + t).fetchall() for t in tables}
            db.close()
            store = Store(directory)
            Store(directory)
            with store.transaction() as db:
                for table in tables:
                    self.assertEqual([tuple(r) for r in db.execute('SELECT * FROM ' + table)], before[table])
                self.assertEqual(db.execute('PRAGMA foreign_keys').fetchone()[0], 1)
            self.assertEqual(store.events('run', 0)[0]['id'], 1)

    def test_failed_upgrade_rolls_back_and_unknown_version_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.sqlite3'
            db = sqlite3.connect(path)
            db.executescript(migrations.VERSION_ONE)
            db.close()
            with patch.object(migrations, 'VERSION_TWO', migrations.VERSION_TWO + '; INVALID SQL'):
                with self.assertRaises(sqlite3.OperationalError):
                    Store(directory)
            db = sqlite3.connect(path)
            self.assertEqual(db.execute('SELECT version FROM schema_version').fetchone()[0], 1)
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='publish_intents'").fetchone())
            db.execute('UPDATE schema_version SET version=999')
            db.commit()
            db.close()
            with self.assertRaises(RuntimeError):
                Store(directory)

    def test_manifest_rejects_unlocked_versions_and_capabilities(self):
        value = dict(source=MPT_SOURCE, revision=MPT_REVISION, artifact_sha256='a'*64,
                     contract_major=1, verification='contract_tested',
                     capabilities={k: k == 'generate' for k in CAPABILITIES})
        self.assertEqual(manifest(value), value)
        for key, bad in [('revision', 'main'), ('contract_major', 2), ('artifact_sha256', ''),
                         ('verification', 'supported'), ('capabilities', {'generate': True})]:
            with self.assertRaises(ValueError):
                manifest(dict(value, **{key: bad}))
