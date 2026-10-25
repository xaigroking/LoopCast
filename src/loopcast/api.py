"""Local single-user HTTP control plane with explicit same-origin writes."""
import fcntl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
import json
from pathlib import Path
import re
import threading
import time
from urllib.parse import urlsplit, parse_qs

from .storage import Store, Problem, uid, TERMINAL
from .worker import Worker

ID = r'[a-f0-9]{32}'


class Service(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 128

    def __init__(self, host, port, directory, generator_settings=None):
        from .generation import Generator
        self.generator = Generator(generator_settings) if generator_settings is not None else None
        directory = Path(directory).resolve()
        directory.mkdir(parents=True, exist_ok=True)
        self.lock = (directory / 'service.lock').open('a')
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.lock.close()
            raise RuntimeError('The data directory is already in use') from None
        try:
            self.store = Store(directory, self.generator.manifest if self.generator else None)
        except BaseException:
            self.lock.close()
            raise
        self.stopping = threading.Event()
        self.stream_slots = threading.BoundedSemaphore(16)
        self.stream_lock = threading.Lock()
        self.stream_count = 0
        self.worker = Worker(self.store, generator=self.generator)
        self.publisher = None
        try:
            super().__init__((host, port), Handler)
            self.worker.start()
            from .publisher import Publisher
            self.publisher = Publisher(self.store)
        except BaseException:
            self.lock.close()
            raise

    def server_close(self):
        self.stopping.set()
        try:
            self.worker.close()
            if self.publisher: self.publisher.close()
        finally:
            super().server_close()
            self.lock.close()


class Handler(BaseHTTPRequestHandler):
    server_version = 'LoopCast'

    def log_message(self, *_args):
        pass

    def _headers(self, status, media_type, length=None):
        self.send_response(status)
        self.send_header('Content-Type', media_type)
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        if length is not None:
            self.send_header('Content-Length', str(length))
        self.end_headers()

    def send_json(self, status, data):
        content = json.dumps(data, ensure_ascii=False).encode()
        self._headers(status, 'application/json; charset=utf-8', len(content))
        self.wfile.write(content)

    def do_GET(self):
        self._handle(False)

    def do_POST(self):
        self._handle(True)

    def _handle(self, write):
        self.request_id = uid()
        try:
            self._host_check(write)
            self.route(write)
        except Problem as error:
            payload = {'code': error.code, 'message': str(error), 'request_id': self.request_id, 'run_id': error.run_id}
            if error.run_id and error.status == 409:
                try:
                    payload['snapshot'] = self.server.store.snapshot(error.run_id)
                except Problem:
                    pass
            self.send_json(error.status, {'error': payload})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            self.send_json(500, {'error': {'code': 'INTERNAL_ERROR', 'message': 'The request could not be completed.', 'request_id': self.request_id, 'run_id': None}})

    def _host_check(self, write):
        port = self.server.server_address[1]
        host = self.headers.get('Host', '')
        if host not in {f'127.0.0.1:{port}', f'localhost:{port}'}:
            raise Problem('INVALID_HOST', 'Use the local listening address.', 403)
        if write:
            if self.headers.get('Origin') != 'http://' + host or self.headers.get('Sec-Fetch-Site') == 'cross-site':
                raise Problem('ORIGIN_REJECTED', 'Writes require the exact local Origin.', 403)
            if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                raise Problem('INVALID_INPUT', 'Use application/json.', 415)

    def _body(self):
        try:
            length = int(self.headers.get('Content-Length', '-1'))
            if not 0 <= length <= 16384:
                raise ValueError()
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict):
                raise ValueError()
            return data
        except (ValueError, UnicodeError):
            raise Problem('INVALID_INPUT', 'Provide a JSON object of at most 16 KiB.', 400) from None

    def route(self, write):
        url = urlsplit(self.path)
        path = url.path
        store = self.server.store
        if not write and path == '/healthz':
            with store.transaction() as db:
                db.execute('SELECT 1 FROM schema_version').fetchone()
            ready = not self.server.stopping.is_set() and self.server.worker.thread.is_alive()
            return self.send_json(200 if ready else 503, {'ready': ready})
        if not write and path == '/v1/capabilities':
            return self.send_json(200, {'mode': 'fixture', 'video_generation': self.server.generator is not None, 'publishing': False, 'single_user': True, 'generation_verification': 'contract_tested_live_unverified' if self.server.generator else 'unavailable', 'real_platform_sending': False})
        if path == '/v1/runs':
            if write:
                snapshot, created = store.create(self._body(), self.headers.get('Idempotency-Key'))
                self.server.worker.wake.set()
                return self.send_json(202 if created else 200, snapshot)
            query = parse_qs(url.query)
            try:
                limit = int(query.get('limit', ['30'])[0])
                before = int(query['before'][0]) if 'before' in query else None
                if not 1 <= limit <= 100 or (before is not None and before < 1):
                    raise ValueError()
            except ValueError:
                raise Problem('INVALID_INPUT', 'Use limit 1–100 and a positive before cursor.', 400) from None
            return self.send_json(200, store.list_runs(before, limit))
        match = re.fullmatch('/v1/runs/(' + ID + ')/publications(?:/(' + ID + ')/(dispatch|verify))?', path)
        if write and match:
            run_id, intent_id, action = match.groups()
            body = self._body()
            key = self.headers.get('Idempotency-Key')
            if intent_id is None:
                if set(body) != {'platform','account','expected_version'}:
                    raise Problem('INVALID_INPUT','Provide platform, account and expected_version.',400,run_id)
                version = body.pop('expected_version')
                return self.send_json(200,store.publication_create(run_id,body,key,version))
            if action == 'verify':
                if set(body) != {'expected_version'}:
                    raise Problem('INVALID_INPUT','Provide expected_version only; evidence comes from the adapter query.',400,run_id)
                return self.send_json(200,store.publication_verify(run_id,intent_id,key,body['expected_version']))
            if set(body) - {'expected_version','scenario'} or 'expected_version' not in body or body.get('scenario','success') not in {'success','submitted','lost_response','not_sent','slow'}:
                raise Problem('INVALID_INPUT','Provide expected_version and a known simulator scenario.',400,run_id)
            snapshot,job = store.publication_dispatch(run_id,intent_id,key,body['expected_version'],body.get('scenario','success'))
            if job: self.server.publisher.submit(job,body.get('scenario','success'))
            return self.send_json(202 if job else 200,snapshot)
        match = re.fullmatch('/v1/runs/(' + ID + ')(?:/(cancel|retry|events))?', path)
        if match:
            run_id, action = match.groups()
            if write and action in ('cancel', 'retry'):
                body = self._body()
                if set(body) != {'expected_version'}:
                    raise Problem('INVALID_INPUT', 'Provide expected_version only.', 400, run_id)
                snapshot, accepted = store.command(run_id, action, self.headers.get('Idempotency-Key'), body['expected_version'])
                self.server.worker.wake.set()
                return self.send_json(202 if accepted else 200, snapshot)
            if not write and action is None:
                return self.send_json(200, store.snapshot(run_id))
            if not write and action == 'events':
                return self.events(run_id, url.query)
        match = re.fullmatch('/v1/artifacts/(' + ID + ')/content', path)
        if not write and match:
            info, content = store.artifact(match[1])
            self._headers(200, info['media_type'], len(content))
            return self.wfile.write(content)
        if not write and (path == '/' or re.fullmatch('/runs/' + ID, path)):
            return self.asset('index.html', 'text/html; charset=utf-8')
        if not write and path in ('/app.js', '/style.css'):
            return self.asset(path[1:], 'text/javascript; charset=utf-8' if path.endswith('.js') else 'text/css; charset=utf-8')
        if not write and not path.startswith('/v1/') and '.' not in path:
            return self.asset('index.html', 'text/html; charset=utf-8')
        raise Problem('NOT_FOUND', 'This address does not exist.', 404)

    def asset(self, name, media_type):
        try:
            content = files('loopcast').joinpath('web', name).read_bytes()
        except FileNotFoundError:
            raise Problem('NOT_FOUND', 'Web resources are not installed.', 404) from None
        self._headers(200, media_type, len(content))
        self.wfile.write(content)

    def events(self, run_id, query):
        params = parse_qs(query, keep_blank_values=True)
        raw = self.headers.get('Last-Event-ID', params.get('after', [''])[0])
        try:
            after = int(raw)
        except (ValueError, TypeError):
            raise Problem('RESYNC_REQUIRED', 'Fetch a snapshot and supply its event cursor.', 409, run_id) from None
        self.server.store.events(run_id, after)
        if not self.server.stream_slots.acquire(blocking=False):
            raise Problem('STREAM_LIMIT', 'Too many open event streams. Close an older view.', 429, run_id)
        with self.server.stream_lock:
            self.server.stream_count += 1
        try:
            self.connection.settimeout(2)
            self._headers(200, 'text/event-stream; charset=utf-8')
            heartbeat = 0
            while not self.server.stopping.is_set():
                events = self.server.store.events(run_id, after)
                for event in events:
                    frame = f"id: {event['id']}\nevent: snapshot\ndata: {event['payload']}\n\n"
                    self.wfile.write(frame.encode())
                    self.wfile.flush()
                    after = event['id']
                current = self.server.store.snapshot(run_id)
                if after == current['last_event_id'] and current['state'] in TERMINAL and not any(p['state'] in ('PREPARED','SENDING') for p in current['publications']):
                    # A terminal snapshot is also sent to subscriptions opened at its cursor.
                    self.wfile.write(('event: complete\ndata: ' + json.dumps(current) + '\n\n').encode())
                    self.wfile.flush()
                    break
                if time.monotonic() - heartbeat >= .1:
                    self.wfile.write(b': heartbeat\n\n')
                    self.wfile.flush()
                    heartbeat = time.monotonic()
                self.server.stopping.wait(.05)
        except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError):
            pass
        finally:
            with self.server.stream_lock:
                self.server.stream_count -= 1
            self.server.stream_slots.release()
