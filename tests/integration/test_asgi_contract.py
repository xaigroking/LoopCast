import json
import re
import sqlite3
import tempfile
import unittest
import urllib.error
import urllib.request
from loopcast.domain import digest

from support import ServerProcess
from asgi_support import ASGIThread


class ASGIContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.server = ServerProcess(self.temp.name).start()
        self.addCleanup(self.server.stop)

    def post(self, path, body, key="contract", *, origin=None, content_type="application/json", traceparent=None):
        headers = {"Content-Type": content_type, "Idempotency-Key": key}
        headers["Origin"] = self.server.url if origin is None else origin
        if traceparent is not None:
            headers["traceparent"] = traceparent
        request = urllib.request.Request(self.server.url + path, data=body if isinstance(body, bytes) else json.dumps(body).encode(), headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, response.headers, response.read()
        except urllib.error.HTTPError as error:
            return error.code, error.headers, error.read()

    def test_openapi_describes_typed_contract_and_error_envelopes(self):
        status, schema = self.server.request('/openapi.json')
        self.assertEqual(status, 200)
        paths = schema['paths']
        self.assertIn('/v1/runs/{run_id}/cancel', paths)
        self.assertIn('/v1/runs/{run_id}/retry', paths)
        self.assertIn('/v1/runs/{run_id}/publications/{intent_id}/dispatch', paths)
        self.assertIn('/v1/runs/{run_id}/publications/{intent_id}/verify', paths)
        body = paths['/v1/runs']['post']['requestBody']['content']['application/json']['schema']
        self.assertEqual({item['$ref'].rsplit('/', 1)[-1] for item in body['anyOf']}, {'FixtureCreate', 'MPTCreate'})
        self.assertFalse(schema['components']['schemas']['FixtureCreate']['additionalProperties'])
        self.assertEqual(paths['/v1/runs/{run_id}/events']['get']['responses']['200']['content']['text/event-stream']['schema']['type'], 'string')
        for route, operations in paths.items():
            if route.startswith('/v1/'):
                for operation in operations.values():
                    for code in ('400', '403', '404', '409', '415', '422', '429', '500'):
                        self.assertIn(code, operation['responses'], (route, code))
                    self.assertIn('ErrorEnvelope', str(operation['responses']['400']))

    def test_strict_json_host_origin_and_structured_not_found(self):
        for body in (
            {'delay_ms': True},
            {'title': None},
            {'extra': 'unknown'},
        ):
            status, _, content = self.post('/v1/runs', body)
            self.assertEqual(status, 400, content)
            self.assertEqual(json.loads(content)['error']['code'], 'INVALID_INPUT')
        status, _, content = self.post('/v1/runs', {'mode': 'live'})
        self.assertEqual(status, 422, content)
        self.assertEqual(json.loads(content)['error']['code'], 'CAPABILITY_UNAVAILABLE')
        status, _, _ = self.post('/v1/runs', {}, origin='http://attacker.invalid')
        self.assertEqual(status, 403)
        status, _, _ = self.post('/v1/runs', {}, content_type='text/plain')
        self.assertEqual(status, 415)
        status, _, content = self.post('/v1/runs/unknown/unknown-action', {})
        self.assertEqual(status, 404, content)
        self.assertEqual(json.loads(content)['error']['code'], 'NOT_FOUND')
        status, value = self.server.request('/v1')
        self.assertEqual(status, 404)
        self.assertEqual(value['error']['code'], 'NOT_FOUND')
        status, value = self.server.request('/v1/unrecognized')
        self.assertEqual(status, 404)
        self.assertEqual(value['error']['code'], 'NOT_FOUND')
        status, value = self.server.request('/docs')
        self.assertEqual(status, 404)
        self.assertEqual(value['error']['code'], 'NOT_FOUND')

    def test_missing_null_boolean_unknown_and_body_size_boundaries(self):
        for body in (
            {'expected_version': True},
            {'expected_version': None},
            {'expected_version': 0, 'unknown': False},
        ):
            status, _, content = self.post('/v1/runs/' + '0' * 32 + '/cancel', body)
            self.assertEqual(status, 400, content)
        status, _, content = self.post('/v1/runs', b'{"title":"' + b'x' * 17000 + b'"}')
        self.assertEqual(status, 400, content)
        status, _, content = self.post('/v1/runs', b'{"title":', key='malformed')
        self.assertEqual(status, 400, content)
        self.assertEqual(json.loads(content)['error']['code'], 'INVALID_INPUT')

    def test_defaults_and_nulls_preserve_domain_validation_without_coercion(self):
        status, _, content = self.post('/v1/runs', {}, key='defaults')
        self.assertEqual(status, 202)
        run = json.loads(content)
        self.assertEqual(run['input'], {'mode': 'fixture', 'title': 'Untitled fixture', 'scenario': 'success', 'delay_ms': 150})
        self.assertEqual(digest(run['input']), run['input_digest'])
        for body in ({'title': None}, {'delay_ms': None}, {'delay_ms': '150'}, {'delay_ms': 150.0}, {'scenario': None}):
            status, _, content = self.post('/v1/runs', body, key='strict')
            self.assertEqual(status, 400, content)
            self.assertEqual(json.loads(content)['error']['code'], 'INVALID_INPUT')
        path = '/v1/runs/' + run['run_id'] + '/cancel'
        for body in ({}, {'expected_version': None}, {'expected_version': '1'}):
            self.assertEqual(self.post(path, body)[0], 400)

    def test_trace_context_is_validated_returned_and_linked_to_async_run(self):
        parent_trace = '1234567890abcdef1234567890abcdef'
        parent_span = '1234567890abcdef'
        status, headers, content = self.post('/v1/runs', {'title': 'trace linked'}, key='trace-link', traceparent=f'00-{parent_trace}-{parent_span}-01')
        self.assertEqual(status, 202, content)
        response_trace = headers['traceparent']
        self.assertRegex(response_trace, re.compile(r'^00-' + parent_trace + r'-[a-f0-9]{16}-01$'))
        self.assertNotEqual(response_trace.split('-')[2], parent_span)
        self.assertRegex(headers['X-Request-ID'], r'^[a-f0-9]{32}$')
        run = json.loads(content)
        self.assertNotIn('trace_id', run)
        with sqlite3.connect(self.temp.name + '/state.sqlite3') as db:
            trace_id, attempt_id, operation = db.execute('SELECT trace_id,attempt_id,operation FROM trace_links WHERE run_id=?', (run['run_id'],)).fetchone()
        self.assertEqual((trace_id, operation), (parent_trace, 'run.create'))
        self.assertRegex(attempt_id, r'^[a-f0-9]{32}$')
        request = urllib.request.Request(self.server.url + '/healthz', headers={'traceparent': '00-' + '0' * 32 + '-' + '0' * 16 + '-01'})
        with urllib.request.urlopen(request, timeout=5) as response:
            generated = response.headers['traceparent']
        self.assertRegex(generated, r'^00-(?!0{32})[a-f0-9]{32}-(?!0{16})[a-f0-9]{16}-00$')

    def test_request_logs_are_structured_and_omit_secrets_and_raw_paths(self):
        self.server.stop()
        service = ASGIThread(self.temp.name).start()
        self.addCleanup(service.stop)
        request = urllib.request.Request(service.url + '/healthz', headers={'Cookie': 'session=private-canary'})
        with self.assertLogs('loopcast.request', level='INFO') as captured:
            with urllib.request.urlopen(request, timeout=5) as response:
                self.assertEqual(response.status, 200)
        entry = json.loads(captured.output[-1].split(':', 2)[-1])
        self.assertEqual(entry['event'], 'request.completed')
        self.assertEqual(entry['route'], '/healthz')
        self.assertRegex(entry['trace_id'], r'^[a-f0-9]{32}$')
        self.assertRegex(entry['request_id'], r'^[a-f0-9]{32}$')
        self.assertNotIn('private-canary', captured.output[-1])


if __name__ == '__main__':
    unittest.main()
