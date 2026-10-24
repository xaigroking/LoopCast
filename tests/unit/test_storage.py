from concurrent.futures import ThreadPoolExecutor
import sqlite3
import tempfile
import unittest
from loopcast.storage import Store, Problem, actions


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)

    def test_concurrent_identity_and_conflict(self):
        with ThreadPoolExecutor(16) as pool:
            results = list(pool.map(lambda _: self.store.create({'title': 'same'}, 'shared'), range(100)))
        self.assertEqual(len({r[0]['run_id'] for r in results}), 1)
        self.assertEqual(sum(r[1] for r in results), 1)
        with self.assertRaises(Problem) as err:
            self.store.create({'title': 'different'}, 'shared')
        self.assertEqual(err.exception.code, 'IDEMPOTENCY_CONFLICT')
        snapshot = results[0][0]
        self.assertEqual(len(snapshot['tasks']), 3)
        self.assertEqual(len(snapshot['attempts']), 1)
        self.assertEqual(Store(self.temp.name).snapshot(snapshot['run_id']), snapshot)

    def test_atomic_rollback_and_foreign_keys(self):
        run, _ = self.store.create({}, 'first')
        with self.assertRaises(RuntimeError):
            with self.store.transaction(True) as db:
                db.execute("UPDATE runs SET state='FAILED' WHERE id=?", (run['run_id'],))
                self.store._event(db, run['run_id'], 'test')
                raise RuntimeError('transaction abort')
        self.assertEqual(self.store.snapshot(run['run_id']), run)
        with self.assertRaises(sqlite3.IntegrityError):
            with self.store.transaction(True) as db:
                db.execute("INSERT INTO tasks VALUES('orphan','absent','prepare','QUEUED',1)")

    def test_action_matrix_and_versions(self):
        for state in ['QUEUED', 'RUNNING', 'CANCEL_REQUESTED', 'SUCCEEDED', 'FAILED', 'CANCELLED']:
            self.assertEqual(actions(state)['retry']['enabled'], state == 'FAILED')
            self.assertEqual(actions(state)['cancel']['enabled'], state in ('QUEUED', 'RUNNING'))
        run, _ = self.store.create({}, 'create')
        with self.assertRaises(Problem) as err:
            self.store.command(run['run_id'], 'cancel', 'cancel', 0)
        self.assertEqual(err.exception.code, 'VERSION_CONFLICT')
        cancelled, _ = self.store.command(run['run_id'], 'cancel', 'cancel', run['state_version'])
        self.assertEqual(cancelled['state'], 'CANCELLED')
        self.assertEqual(self.store.command(run['run_id'], 'cancel', 'cancel', run['state_version'])[0], cancelled)
        with self.assertRaises(Problem):
            self.store.command(run['run_id'], 'retry', 'retry', cancelled['state_version'])

    def test_retry_history_and_late_result(self):
        run, _ = self.store.create({}, 'create')
        attempt = self.store.claim()
        self.store.finish(run['run_id'], attempt['token'], 'FAILED', 'controlled failure')
        failed = self.store.snapshot(run['run_id'])
        with ThreadPoolExecutor(2) as pool:
            def retry(key):
                try:
                    return self.store.command(run['run_id'], 'retry', key, failed['state_version'])
                except Problem as err:
                    return err.code
            result = list(pool.map(retry, ['a', 'b']))
        self.assertEqual(sum(isinstance(r, tuple) for r in result), 1)
        current = self.store.snapshot(run['run_id'])
        self.assertEqual(len(current['attempts']), 2)
        self.assertEqual(current['attempts'][0], failed['attempts'][0])
        self.assertEqual([t['id'] for t in current['tasks']], [t['id'] for t in run['tasks']])
        self.assertFalse(self.store.finish(run['run_id'], attempt['token'], 'SUCCEEDED'))
        self.assertEqual(self.store.snapshot(run['run_id']), current)
        self.assertEqual(self.store.create({}, 'create')[0], current)
        self.assertEqual(self.store.events(run['run_id'], 0)[-1]['version'], current['state_version'])


if __name__ == '__main__':
    unittest.main()
