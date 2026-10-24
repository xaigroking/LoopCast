import hashlib
import tempfile
import time
import unittest
from loopcast.storage import Store
from loopcast.worker import Worker


def wait(store, run_id, states, timeout=10):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        run = store.snapshot(run_id)
        if run['state'] in states:
            return run
        time.sleep(.02)
    raise AssertionError(f'Timed out waiting: {run}')


class ExecutorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.worker = Worker(self.store)
        self.worker.start()
        self.addCleanup(self.worker.close)

    def create(self, **value):
        return self.store.create(value, str(time.time_ns()))[0]

    def test_success_and_report_integrity(self):
        run = self.create(title='Real fixture')
        done = wait(self.store, run['run_id'], {'SUCCEEDED'})
        self.assertTrue(all(t['state'] == 'SUCCEEDED' for t in done['tasks']))
        info, content = self.store.artifact(done['artifacts'][0]['id'])
        self.assertIn(b'Real fixture', content)
        self.assertEqual(hashlib.sha256(content).hexdigest(), info['sha256'])
        self.assertEqual(Store(self.temp.name).snapshot(run['run_id']), done)
        (self.store.artifact_dir / info['storage_ref']).write_bytes(b'changed')
        from loopcast.storage import Problem
        with self.assertRaises(Problem):
            self.store.artifact(info['id'])

    def test_failure_retry_and_history(self):
        run = self.create(scenario='fail_once')
        failed = wait(self.store, run['run_id'], {'FAILED'})
        self.store.command(run['run_id'], 'retry', 'retry', failed['state_version'])
        done = wait(self.store, run['run_id'], {'SUCCEEDED'})
        self.assertEqual(len(done['attempts']), 2)
        self.assertEqual(done['attempts'][0], failed['attempts'][0])

    def test_cancel_queue_and_running(self):
        slow = self.create(scenario='slow', delay_ms=1500)
        running = wait(self.store, slow['run_id'], {'RUNNING'})
        queued = self.create()
        cancelled, _ = self.store.command(queued['run_id'], 'cancel', 'cancel-q', queued['state_version'])
        self.assertEqual(cancelled['state'], 'CANCELLED')
        running = self.store.snapshot(slow['run_id'])
        stopping, _ = self.store.command(slow['run_id'], 'cancel', 'cancel-r', running['state_version'])
        self.assertEqual(stopping['state'], 'CANCEL_REQUESTED')
        done = wait(self.store, slow['run_id'], {'CANCELLED', 'SUCCEEDED'})
        self.assertIsNotNone(done['attempts'][0]['ended_at'])

    def test_timeout_and_killed_executor(self):
        self.worker.timeout = .2
        run = self.create(scenario='slow', delay_ms=1000)
        failed = wait(self.store, run['run_id'], {'FAILED'})
        self.assertEqual(failed['attempts'][0]['error'], 'TIMEOUT')
        self.worker.timeout = 8
        run = self.create(scenario='slow', delay_ms=2000)
        end = time.monotonic() + 5
        while time.monotonic() < end:
            if self.worker.active_process and self.store.snapshot(run['run_id'])['tasks'][1]['state'] == 'RUNNING':
                self.worker.active_process.kill()
                break
            time.sleep(.01)
        failed = wait(self.store, run['run_id'], {'FAILED'})
        self.assertEqual(failed['attempts'][0]['error'], 'EXECUTOR_EXITED')

    def test_restart_retires_execution_token(self):
        self.worker.close()
        run = self.create()
        old = self.store.claim()
        reopened = Store(self.temp.name)
        reopened.recover()
        failed = reopened.snapshot(run['run_id'])
        self.assertEqual(failed['attempts'][0]['error'], 'INTERRUPTED')
        reopened.command(run['run_id'], 'retry', 'retry', failed['state_version'])
        current = reopened.snapshot(run['run_id'])
        self.assertFalse(reopened.finish(run['run_id'], old['token'], 'SUCCEEDED'))
        self.assertEqual(reopened.snapshot(run['run_id']), current)

    def test_readiness_reports_dead_worker(self):
        from loopcast.api import Service
        from support import ServerProcess
        import threading
        self.worker.close()
        service = Service('127.0.0.1', 0, self.temp.name)
        thread = threading.Thread(target=service.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(service.server_close)
        self.addCleanup(service.shutdown)
        service.worker.close()
        client = ServerProcess(self.temp.name)
        client.url = 'http://127.0.0.1:' + str(service.server_address[1])
        self.assertEqual(client.request('/healthz'), (503, {'ready': False}))
