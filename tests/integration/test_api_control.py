from concurrent.futures import ThreadPoolExecutor
import tempfile
import time
import unittest
from support import ServerProcess


class APITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.server = ServerProcess(self.temp.name).start()
        self.addCleanup(self.server.stop)

    def test_concurrent_creation_and_restart_identity(self):
        with ThreadPoolExecutor(20) as pool:
            result = list(pool.map(lambda _: self.server.request('/v1/runs', {'title': 'concurrent'}, 'same'), range(100)))
        self.assertEqual(sum(code == 202 for code, _ in result), 1)
        self.assertTrue(all(code in (200, 202) for code, _ in result))
        ids = {data['run_id'] for _, data in result}
        self.assertEqual(len(ids), 1)
        self.assertEqual(self.server.request('/v1/runs', {'title': 'changed'}, 'same')[0], 409)
        self.server.stop()
        self.server.start()
        code, replay = self.server.request('/v1/runs', {'title': 'concurrent'}, 'same')
        self.assertEqual(code, 200)
        self.assertIn(replay['run_id'], ids)
        self.assertEqual(len(self.server.request('/v1/runs')[1]['items']), 1)
        self.assertEqual(len(replay['attempts']), 1)

    def test_failed_concurrent_retry(self):
        _, run = self.server.request('/v1/runs', {'scenario': 'fail_once'}, 'new')
        failed = self.server.wait(run['run_id'], {'FAILED'})
        path = '/v1/runs/' + run['run_id'] + '/retry'
        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(lambda key: self.server.request(path, {'expected_version': failed['state_version']}, key), ['a', 'b']))
        self.assertEqual(sorted(x[0] for x in results), [202, 409])
        done = self.server.wait(run['run_id'], {'SUCCEEDED'})
        self.assertEqual(len(done['attempts']), 2)
        winner = 'a' if results[0][0] == 202 else 'b'
        code, replay = self.server.request(path, {'expected_version': failed['state_version']}, winner)
        self.assertEqual(code, 200)
        self.assertEqual(replay, done)
        artifact_id = done['artifacts'][0]['id']
        self.assertIn(b'fixture report', self.server.request('/v1/artifacts/' + artifact_id + '/content')[1])
        self.assertEqual(self.server.request('/v1/artifacts/' + '0' * 32 + '/content')[0], 404)
        self.assertEqual(self.server.request('/v1/artifacts/../../etc/passwd/content')[0], 404)

    def test_slow_does_not_block_and_kill_recovery(self):
        _, run = self.server.request('/v1/runs', {'scenario': 'slow', 'delay_ms': 2000}, 'slow')
        self.server.wait(run['run_id'], {'RUNNING'})
        started = time.monotonic()
        self.assertEqual(self.server.request('/healthz'), (200, {'ready': True}))
        self.assertEqual(self.server.request('/v1/runs/' + run['run_id'])[0], 200)
        elapsed = time.monotonic() - started
        print(f'Fixture health+snapshot latency during slow execution: {elapsed:.4f}s')
        self.assertLess(elapsed, 1.5)
        self.server.stop(kill=True)
        self.server.start()
        recovered = self.server.request('/v1/runs/' + run['run_id'])[1]
        self.assertEqual(recovered['state'], 'FAILED')
        self.assertEqual(recovered['attempts'][0]['error'], 'INTERRUPTED')
        self.assertEqual(self.server.request('/v1/runs', {'scenario': 'slow', 'delay_ms': 2000}, 'slow')[1], recovered)

    def test_validation_origin_and_pagination(self):
        self.assertEqual(self.server.request('/v1/runs', {}, 'x', origin=False)[0], 403)
        self.assertEqual(self.server.request('/v1/runs', {'mode': 'live'}, 'live')[0], 422)
        self.assertEqual(self.server.request('/v1/runs', {}, None)[0], 400)
        self.assertEqual(self.server.request('/v1/runs', {'delay_ms': True}, 'bad')[0], 400)
        self.assertEqual(self.server.request('/v1/runs?limit=0')[0], 400)
        for n in range(4):
            self.server.request('/v1/runs', {'title': str(n)}, str(n))
        page = self.server.request('/v1/runs?limit=2')[1]
        next_page = self.server.request('/v1/runs?limit=2&before=' + str(page['next_cursor']))[1]
        self.assertEqual([x['input']['title'] for x in page['items'] + next_page['items']], ['3', '2', '1', '0'])
        self.assertIsNone(next_page['next_cursor'])
        self.assertEqual(self.server.request('/v1/nonexistent')[0], 404)

    def test_success_report_survives_process_restart_and_lock(self):
        _, run = self.server.request('/v1/runs', {}, 'report')
        done = self.server.wait(run['run_id'], {'SUCCEEDED'})
        path = '/v1/artifacts/' + done['artifacts'][0]['id'] + '/content'
        content = self.server.request(path)
        from loopcast.api import Service
        with self.assertRaises(RuntimeError):
            Service('127.0.0.1', 0, self.temp.name)
        self.server.stop(kill=True)
        self.server.start()
        self.assertEqual(self.server.request(path), content)
        self.assertEqual(self.server.request('/v1/runs/' + run['run_id'])[1], done)

    def test_cancel_completion_races_have_one_actual_result(self):
        for n in range(8):
            _, run = self.server.request('/v1/runs', {'delay_ms': 0}, 'race' + str(n))
            current = self.server.wait(run['run_id'], {'RUNNING', 'SUCCEEDED'})
            code, response = self.server.request('/v1/runs/' + run['run_id'] + '/cancel', {'expected_version': current['state_version']}, 'cancel')
            self.assertIn(code, (202, 409))
            done = self.server.wait(run['run_id'], {'SUCCEEDED', 'CANCELLED'})
            self.assertEqual(done['attempts'][0]['state'], done['state'])
            self.assertIsNotNone(done['attempts'][0]['ended_at'])
            self.assertEqual(bool(done['artifacts']), done['state'] == 'SUCCEEDED')
