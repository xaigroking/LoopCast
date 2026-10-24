"""Local single-user HTTP control plane with explicit same-origin writes."""
import fcntl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
import json
import re
import threading
from urllib.parse import urlsplit, parse_qs

from .storage import Store, Problem, uid, TERMINAL
from .worker import Worker

ID = r'[a-f0-9]{32}'


class Service(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 128

    def __init__(self, host, port, directory):
        self.store = Store(directory)
        self.lock = (self.store.directory / 'service.lock').open('a')
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.lock.close()
            raise RuntimeError('The data directory is already in use') from None
        self.stopping = threading.Event()
        self.stream_slots = threading.BoundedSemaphore(16)
        self.stream_lock = threading.Lock()
        self.stream_count = 0
        self.worker = Worker(self.store)
        try:
            super().__init__((host, port), Handler)
            self.worker.start()
        except BaseException:
            self.lock.close()
            raise

    def server_close(self):
        self.stopping.set()
        try:
            self.worker.close()
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
            return self.send_json(200, {'ready': not self.server.stopping.is_set()})
        if not write and path == '/v1/capabilities':
            return self.send_json(200, {'mode': 'fixture', 'video_generation': False, 'publishing': False, 'single_user': True})
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
        raise Problem('NOT_FOUND', 'This address does not exist.', 404)

    def asset(self, name, media_type):
        try:
            content = files('loopcast').joinpath('web', name).read_bytes()
        except FileNotFoundError:
            raise Problem('NOT_FOUND', 'Web resources are not installed.', 404) from None
        self._headers(200, media_type, len(content))
        self.wfile.write(content)

    def events(self, run_id, query):
        raise Problem('CAPABILITY_UNAVAILABLE', 'Event stream is not available.', 501, run_id)
