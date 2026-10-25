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
                    after = [tuple(r) for r in db.execute('SELECT * FROM ' + table)]
                    if table == 'runs':
                        after = [row[:8] for row in after]
                    self.assertEqual(after, before[table])
                self.assertEqual(db.execute('PRAGMA foreign_keys').fetchone()[0], 1)
                self.assertEqual(db.execute('SELECT version FROM schema_version').fetchone()[0], 4)
                self.assertIsNone(db.execute("SELECT trace_id FROM runs WHERE id='run'").fetchone()[0])
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

    def test_trace_upgrade_preserves_v2_publication_unknown_receipt_and_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.sqlite3'
            db = sqlite3.connect(path)
            db.executescript(migrations.VERSION_ONE + migrations.VERSION_TWO)
            db.execute("INSERT INTO runs VALUES('run','fixture','{}','digest','SUCCEEDED',1,'time','time')")
            db.execute("INSERT INTO attempts VALUES('attempt','run',1,'token','SUCCEEDED','time','time',NULL,NULL)")
            db.execute("INSERT INTO contents VALUES('content','run')")
            db.execute("INSERT INTO publish_intents VALUES('intent','run','content','simulator','demo','UNKNOWN',2,'time')")
            db.execute("INSERT INTO publish_attempts VALUES('publish-attempt','intent','publish-token','UNKNOWN',123.0,'time','time')")
            db.execute("INSERT INTO account_locks VALUES('simulator','demo','publish-token')")
            db.execute("INSERT INTO receipts VALUES('receipt','intent','publish-attempt','time','simulator','UNKNOWN',NULL,'uncertain canary')")
            db.execute("INSERT INTO publish_requests VALUES('intent-scope','intent-key','request-digest','intent')")
            before = {table: db.execute('SELECT * FROM ' + table).fetchall() for table in ['publish_intents','publish_attempts','account_locks','receipts','publish_requests']}
            db.commit()
            db.close()
            store = Store(directory)
            snapshot = store.snapshot('run')
            self.assertEqual(snapshot['publications'][0]['state'], 'UNKNOWN')
            self.assertEqual(snapshot['publications'][0]['receipts'][0]['evidence'], 'uncertain canary')
            with store.transaction() as db:
                self.assertEqual(db.execute('SELECT version FROM schema_version').fetchone()[0], 4)
                for table, rows in before.items():
                    self.assertEqual([tuple(row) for row in db.execute('SELECT * FROM ' + table)], rows)

    def test_manifest_rejects_unlocked_versions_and_capabilities(self):
        value = dict(source=MPT_SOURCE, revision=MPT_REVISION, artifact_sha256='a'*64,
                     contract_major=1, verification='contract_tested',
                     capabilities={k: k == 'generate' for k in CAPABILITIES})
        self.assertEqual(manifest(value), value)
        for key, bad in [('revision', 'main'), ('contract_major', 2), ('artifact_sha256', ''),
                         ('verification', 'supported'), ('capabilities', {'generate': True})]:
            with self.assertRaises(ValueError):
                manifest(dict(value, **{key: bad}))

    def test_context_upgrade_preserves_queued_v3_trace_and_rolls_back_on_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.sqlite3'
            db = sqlite3.connect(path)
            db.executescript(migrations.VERSION_ONE + migrations.VERSION_TWO + migrations.VERSION_THREE)
            trace = '1234567890abcdef1234567890abcdef'
            db.execute("INSERT INTO runs VALUES('run','fixture','{}','digest','QUEUED',1,'time','time',?)", (trace,))
            db.execute("INSERT INTO attempts VALUES('attempt','run',1,'token','QUEUED',NULL,NULL,NULL,NULL)")
            db.execute("INSERT INTO trace_links VALUES('link',?,'run','attempt','run.create','time')", (trace,))
            db.commit(); db.close()
            with patch.object(migrations, 'VERSION_FOUR', migrations.VERSION_FOUR + '; INVALID SQL'):
                with self.assertRaises(sqlite3.OperationalError): Store(directory)
            with sqlite3.connect(path) as db:
                self.assertEqual(db.execute('SELECT version FROM schema_version').fetchone()[0], 3)
                self.assertNotIn('span_id', [row[1] for row in db.execute('PRAGMA table_info(trace_links)')])
            store = Store(directory)
            attempt = store.claim()
            self.assertEqual(attempt['trace']['trace_id'], trace)
            with store.transaction() as db:
                self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(), [])
                self.assertEqual(db.execute('SELECT version FROM schema_version').fetchone()[0], 4)
