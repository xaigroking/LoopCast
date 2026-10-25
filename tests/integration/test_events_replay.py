import json
import tempfile
import time
import unittest
import urllib.request
import sqlite3
from pathlib import Path
from loopcast import migrations
from loopcast.domain import canonical, digest
from support import ServerProcess
from asgi_support import ASGIThread


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
        service = ASGIThread(self.temp.name).start()
        self.addCleanup(service.stop)
        self.server.url = service.url
        _, run = self.server.request('/v1/runs', {'scenario': 'slow', 'delay_ms': 2000}, 'slow')
        url = self.server.url + '/v1/runs/' + run['run_id'] + '/events'
        for _ in range(30):
            current = service.runtime.store.snapshot(run['run_id'])
            with stream(url, current['last_event_id']) as response:
                response.readline()
            deadline = time.monotonic() + 2
            while service.runtime.stream_count and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertEqual(service.runtime.stream_count, 0)
        end = time.monotonic() + 3
        while service.runtime.stream_count and time.monotonic() < end:
            time.sleep(.05)
        self.assertEqual(service.runtime.stream_count, 0)
        values = service.runtime.store.list_runs()['items']
        self.assertEqual(len(values), 1)
        self.assertEqual(len(values[0]['attempts']), 1)

    def test_idle_stream_emits_application_heartbeats_without_business_writes(self):
        self.server.stop()
        service = ASGIThread(self.temp.name).start()
        self.addCleanup(service.stop)
        service.runtime.worker.close()
        run, _ = service.runtime.store.create({}, 'idle-heartbeat')
        path = service.url + '/v1/runs/' + run['run_id'] + '/events'
        started = time.monotonic()
        with stream(path, run['last_event_id']) as response:
            self.assertEqual(response.readline(), b': connected\n')
            self.assertLess(time.monotonic() - started, 1)
            while response.readline() != b'event: heartbeat\n':
                self.assertLess(time.monotonic() - started, 8)
            self.assertEqual(response.readline(), b'data: {}\n')
        deadline = time.monotonic() + 2
        while service.runtime.stream_count and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertEqual(service.runtime.stream_count, 0)
        self.assertEqual(service.runtime.store.snapshot(run['run_id']), run)

    def test_previous_fixture_history_replays_through_installed_api_without_rewriting_it(self):
        self.server.stop()
        # Use a separate real v1 database and the original fixture snapshot shape.
        directory = Path(self.temp.name) / 'previous'
        directory.mkdir()
        run_id, attempt_id = '1' * 32, '2' * 32
        value = {'mode': 'fixture', 'title': 'historical fixture', 'scenario': 'fail', 'delay_ms': 10}
        historical = {
            'run_id': run_id, 'mode': 'fixture', 'input': value, 'input_digest': digest(value),
            'state': 'FAILED', 'state_version': 1, 'created_at': 'time', 'updated_at': 'time',
            'last_event_id': 1, 'tasks': [], 'artifacts': [],
            'attempts': [{'id': attempt_id, 'number': 1, 'state': 'FAILED', 'started_at': 'time', 'ended_at': 'time', 'error': 'FIXTURE_FAILED', 'evidence': None}],
            'allowed_actions': {'cancel': {'enabled': False, 'reason_code': 'CANCEL_NOT_APPLICABLE'}, 'retry': {'enabled': True, 'reason_code': 'AVAILABLE'}},
        }
        payload = canonical(historical)
        with sqlite3.connect(directory / 'state.sqlite3') as db:
            db.executescript(migrations.VERSION_ONE)
            db.execute('INSERT INTO runs VALUES(?,?,?,?,?,?,?,?)', (run_id, 'fixture', canonical(value), digest(value), 'FAILED', 1, 'time', 'time'))
            db.execute('INSERT INTO attempts VALUES(?,?,?,?,?,?,?,?,?)', (attempt_id, run_id, 1, '3' * 32, 'FAILED', 'time', 'time', 'FIXTURE_FAILED', None))
            db.execute('INSERT INTO events VALUES(?,?,?,?,?,?)', (1, run_id, 1, 'finished', 'time', payload))
            db.execute('INSERT INTO idempotency VALUES(?,?,?,?)', ('create', 'historical-key', digest(value), run_id))
        previous = ServerProcess(directory).start()
        self.addCleanup(previous.stop)
        status, current = previous.request('/v1/runs/' + run_id)
        self.assertEqual(status, 200)
        self.assertEqual(current['input_digest'], digest(current['input']))
        self.assertEqual(previous.request('/v1/runs', value, 'historical-key'), (200, current))
        with stream(previous.url + '/v1/runs/' + run_id + '/events', 0) as response:
            replayed = snapshots(response)
        self.assertEqual(replayed[0]['attempts'], historical['attempts'])
        self.assertEqual(replayed[0]['state_version'], 1)
        self.assertEqual(replayed[0]['publications'], [])
        self.assertEqual(replayed[-1], current)
        with sqlite3.connect(directory / 'state.sqlite3') as db:
            self.assertEqual(db.execute('SELECT payload FROM events WHERE id=1').fetchone()[0], payload)

    def test_shutdown_cancels_an_idle_stream_and_releases_the_directory_owner(self):
        self.server.stop()
        service = ASGIThread(self.temp.name).start()
        self.addCleanup(service.stop)
        service.runtime.worker.close()
        run, _ = service.runtime.store.create({}, 'shutdown-stream')
        response = stream(service.url + '/v1/runs/' + run['run_id'] + '/events', run['last_event_id'])
        self.addCleanup(response.close)
        self.assertEqual(response.readline(), b': connected\n')
        started = time.monotonic()
        service.stop()
        self.assertLess(time.monotonic() - started, 8)
        self.assertEqual(service.runtime.stream_count, 0)
        from loopcast.runtime import ApplicationRuntime
        owner = ApplicationRuntime(self.temp.name)
        owner.close()
