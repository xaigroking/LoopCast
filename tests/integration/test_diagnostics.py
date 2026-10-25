import json
import sqlite3
import tempfile
import time
import unittest
import urllib.error
import urllib.request

from support import ServerProcess


class DiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.server = ServerProcess(self.temp.name).start()
        self.addCleanup(self.server.stop)

    def post(self, path, body, key, trace='1' * 32):
        request = urllib.request.Request(self.server.url + path, data=json.dumps(body).encode(), headers={
            'Origin': self.server.url, 'Content-Type': 'application/json', 'Idempotency-Key': key,
            'traceparent': f'00-{trace}-1234567890abcdef-01',
            'Cookie': 'session=cookie-canary', 'Authorization': 'Bearer token-canary',
            'tracestate': 'private=config-canary', 'baggage': 'private=baggage-canary',
        })
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, response.headers, json.load(response)
        except urllib.error.HTTPError as response:
            return response.code, response.headers, json.load(response)

    def logs(self):
        return [json.loads(line) for line in self.server.diagnostics if line.startswith('{')]

    def wait_log(self, event, **identifiers):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            matches = [entry for entry in self.logs() if entry['event'] == event and all(entry.get(k) == v for k, v in identifiers.items())]
            if matches:
                return matches[-1]
            time.sleep(.01)
        self.fail('Expected structured event was not emitted')

    def assert_private_values_absent(self):
        content = '\n'.join(self.server.diagnostics)
        for private in ('cookie-canary', 'token-canary', 'config-canary', 'baggage-canary', 'title-canary', 'account_canary'):
            self.assertNotIn(private, content)

    def test_failed_run_retry_and_request_failure_keep_distinct_correlations(self):
        body = {'title': 'title-canary', 'scenario': 'fail_once', 'delay_ms': 10}
        status, first_headers, initial = self.post('/v1/runs', body, 'first')
        self.assertEqual(status, 202)
        failed = self.server.wait(initial['run_id'], {'FAILED'})
        first = self.wait_log('attempt.completed', run_id=initial['run_id'], result='FAILED')
        self.assertEqual(first['trace_id'], '1' * 32)
        self.assertEqual(first['request_id'], first_headers['X-Request-ID'])
        self.assertEqual(first['parent_span_id'], first_headers['traceparent'].split('-')[2])
        self.assertNotEqual(first['span_id'], first['parent_span_id'])
        status, replay_headers, replay = self.post('/v1/runs', body, 'first', '2' * 32)
        self.assertEqual((status, replay['run_id']), (200, initial['run_id']))
        status, retry_headers, retried = self.post('/v1/runs/' + initial['run_id'] + '/retry', {'expected_version': failed['state_version']}, 'retry', '3' * 32)
        self.assertEqual(status, 202)
        done = self.server.wait(initial['run_id'], {'SUCCEEDED'})
        self.assertEqual(len(done['attempts']), 2)
        second = self.wait_log('attempt.completed', attempt_id=retried['attempts'][-1]['id'])
        self.assertEqual(second['trace_id'], '3' * 32)
        self.assertEqual(second['request_id'], retry_headers['X-Request-ID'])
        self.assertNotEqual(second['request_id'], replay_headers['X-Request-ID'])
        status, headers, conflict = self.post('/v1/runs/' + initial['run_id'] + '/cancel', {'expected_version': 0}, 'conflict', '4' * 32)
        self.assertEqual(status, 409)
        entry = self.wait_log('request.completed', request_id=headers['X-Request-ID'])
        self.assertEqual(entry['run_id'], initial['run_id'])
        self.assertEqual(entry['route'], '/v1/runs/{run_id}/cancel')
        self.assertEqual(entry['request_id'], conflict['error']['request_id'])
        with sqlite3.connect(self.temp.name + '/state.sqlite3') as db:
            links = db.execute('SELECT trace_id,request_id,span_id,attempt_id,operation FROM trace_links ORDER BY rowid').fetchall()
        self.assertEqual([row[-1] for row in links], ['run.create', 'create.replay', 'run.retry'])
        self.assertEqual(links[0][1], first_headers['X-Request-ID'])
        self.assertEqual(links[2][3], second['attempt_id'])
        self.assert_private_values_absent()

    def test_publication_unknown_is_correlated_and_never_automatically_resent(self):
        _, _, run = self.post('/v1/runs', {'delay_ms': 0}, 'generation')
        run = self.server.wait(run['run_id'], {'SUCCEEDED'})
        path = '/v1/runs/' + run['run_id'] + '/publications'
        status, _, run = self.post(path, {'platform': 'simulator', 'account': 'account_canary', 'expected_version': run['state_version']}, 'intent', '2' * 32)
        self.assertEqual(status, 200)
        intent = run['publications'][0]['id']
        status, headers, _ = self.post(path + '/' + intent + '/dispatch', {'expected_version': run['state_version'], 'scenario': 'lost_response'}, 'dispatch', '3' * 32)
        self.assertEqual(status, 202)
        entry = self.wait_log('publication.completed', intent_id=intent, result='UNKNOWN')
        self.assertEqual(entry['request_id'], headers['X-Request-ID'])
        self.assertEqual(entry['trace_id'], '3' * 32)
        self.assertEqual(entry['parent_span_id'], headers['traceparent'].split('-')[2])
        time.sleep(.2)
        _, current = self.server.request('/v1/runs/' + run['run_id'])
        self.assertEqual(len(current['publications'][0]['attempts']), 1)
        self.assertEqual(current['publications'][0]['state'], 'UNKNOWN')
        status, _, blocked = self.post(path + '/' + intent + '/dispatch', {'expected_version': current['state_version']}, 'blocked', '4' * 32)
        self.assertEqual(status, 409)
        self.assertEqual(blocked['error']['code'], 'PUBLICATION_NOT_RETRYABLE')
        status, verify_headers, verified = self.post(path + '/' + intent + '/verify', {'expected_version': current['state_version']}, 'verify', '5' * 32)
        self.assertEqual((status, verified['publications'][0]['state']), (200, 'PUBLISHED'))
        with sqlite3.connect(self.temp.name + '/state.sqlite3') as db:
            link = db.execute("SELECT request_id,intent_id,publication_attempt_id,traceparent FROM trace_links WHERE operation='publication.dispatch'").fetchone()
            self.assertEqual(link, (headers['X-Request-ID'], intent, entry['publication_attempt_id'], headers['traceparent']))
            self.assertEqual(db.execute("SELECT request_id FROM trace_links WHERE operation='publication.verify'").fetchone()[0], verify_headers['X-Request-ID'])
        self.assert_private_values_absent()

    def test_invalid_external_context_and_private_paths_are_not_echoed(self):
        valid_trace = '1234567890abcdef1234567890abcdef'
        for header in ('garbage-canary', f'ff-{valid_trace}-1234567890abcdef-01', f'00-{valid_trace}-0000000000000000-01', f'00-{valid_trace.upper()}-1234567890abcdef-01', f'00-{valid_trace}-1234567890abcdef-01-extra', 'x' * 300):
            request = urllib.request.Request(self.server.url + '/private-path-canary?token=query-canary', headers={'traceparent': header})
            try:
                urllib.request.urlopen(request, timeout=5)
            except urllib.error.HTTPError as response:
                self.assertEqual(response.code, 404)
                generated = response.headers['traceparent']
                self.assertRegex(generated, r'^00-[a-f0-9]{32}-[a-f0-9]{16}-00$')
                self.assertNotEqual(generated.split('-')[1], valid_trace)
                log = self.wait_log('request.completed', request_id=response.headers['X-Request-ID'])
                self.assertEqual(log['route'], '/{unknown:path}')
        content = '\n'.join(self.server.diagnostics)
        for private in ('garbage-canary', 'private-path-canary', 'query-canary'):
            self.assertNotIn(private, content)
