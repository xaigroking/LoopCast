import json
import tempfile
import threading
import time
import unittest
import urllib.request
from loopcast.api import Service
from support import ServerProcess


def stream(url, after, last=None):
    headers = {'Last-Event-ID': str(last)} if last is not None else {}
    return urllib.request.urlopen(urllib.request.Request(url + '?after=' + str(after), headers=headers), timeout=10)


def snapshots(response):
    values = []
    for raw in response:
        line = raw.decode().strip()
        if line.startswith('data: '):
            values.append(json.loads(line[6:]))
    return values


class EventTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.server = ServerProcess(self.temp.name).start()
        self.addCleanup(self.server.stop)

    def test_snapshot_subscription_gap_and_restart_replay(self):
        _, initial = self.server.request('/v1/runs', {'delay_ms': 100}, 'new')
        final = self.server.wait(initial['run_id'], {'SUCCEEDED'})
        self.server.stop()
        self.server.start()
        path = self.server.url + '/v1/runs/' + initial['run_id'] + '/events'
        with stream(path, initial['last_event_id']) as response:
            events = snapshots(response)
        self.assertEqual(events[-1], final)
        versions = [x['state_version'] for x in events]
        self.assertEqual(versions, sorted(versions))
        with stream(path, 0, last=initial['last_event_id']) as response:
            self.assertEqual(snapshots(response)[-1], final)
        for bad in ['-1', 'abc', str(final['last_event_id'] + 1), '']:
            self.assertEqual(self.server.request('/v1/runs/' + initial['run_id'] + '/events?after=' + bad)[0], 409)
        _, other = self.server.request('/v1/runs', {}, 'other')
        self.assertEqual(self.server.request('/v1/runs/' + initial['run_id'] + '/events?after=' + str(other['last_event_id']))[0], 409)

    def test_disconnects_release_streams_without_writes(self):
        self.server.stop()
        service = Service('127.0.0.1', 0, self.temp.name)
        thread = threading.Thread(target=service.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(service.server_close)
        self.addCleanup(service.shutdown)
        self.server.url = 'http://127.0.0.1:' + str(service.server_address[1])
        _, run = self.server.request('/v1/runs', {'scenario': 'slow', 'delay_ms': 2000}, 'slow')
        url = self.server.url + '/v1/runs/' + run['run_id'] + '/events'
        for _ in range(30):
            current = service.store.snapshot(run['run_id'])
            with stream(url, current['last_event_id']) as response:
                response.readline()
            deadline = time.monotonic() + 2
            while service.stream_count and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertEqual(service.stream_count, 0)
        end = time.monotonic() + 3
        while service.stream_count and time.monotonic() < end:
            time.sleep(.05)
        self.assertEqual(service.stream_count, 0)
        values = service.store.list_runs()['items']
        self.assertEqual(len(values), 1)
        self.assertEqual(len(values[0]['attempts']), 1)
