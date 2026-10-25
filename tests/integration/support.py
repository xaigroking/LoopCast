import json
import os
from pathlib import Path
import selectors
import subprocess
import sys
import threading
import time
import urllib.request
import urllib.error


class ServerProcess:
    def __init__(self, directory, generator_config=None):
        self.generator_config = generator_config
        self.directory = directory
        self.process = None
        self.diagnostics = []

    def start(self):
        self.process = subprocess.Popen([sys.executable, '-I', '-m', 'loopcast', 'serve', '--port', '0', '--data-dir', str(self.directory)] + (['--generator-config',str(self.generator_config)] if self.generator_config else []), cwd='/tmp', stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        # Continuously drain diagnostics so concurrency tests cannot block on a
        # full subprocess stderr pipe. Retain real emitted logs for assertions.
        self.log_thread = threading.Thread(target=self._read_logs, args=(self.process.stderr,), daemon=True)
        self.log_thread.start()
        selector = selectors.DefaultSelector()
        selector.register(self.process.stdout, selectors.EVENT_READ)
        ready = selector.select(10)
        selector.close()
        if not ready:
            self.stop()
            raise AssertionError('No server readiness signal')
        line = self.process.stdout.readline().strip()
        if not line.startswith('READY '):
            raise AssertionError('Service did not start: ' + line)
        self.url = line.split(' ', 1)[1]
        return self

    def _read_logs(self, stream):
        for line in stream:
            self.diagnostics.append(line.rstrip())

    def stop(self, kill=False):
        if self.process:
            if self.process.poll() is None:
                self.process.kill() if kill else self.process.terminate()
                self.process.wait(timeout=12)
            self.log_thread.join(timeout=2)
            self.process.stdout.close()
            self.process.stderr.close()
            self.process = None

    def request(self, path, body=None, key=None, origin=True):
        headers = {}
        if body is not None:
            headers['Content-Type'] = 'application/json'
            if origin:
                headers['Origin'] = self.url
        if key is not None:
            headers['Idempotency-Key'] = key
        req = urllib.request.Request(self.url + path, data=json.dumps(body).encode() if body is not None else None, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=10) as response:
                data = response.read()
                return response.status, json.loads(data) if 'application/json' in response.headers.get('Content-Type', '') else data
        except urllib.error.HTTPError as error:
            return error.code, json.load(error)

    def wait(self, run_id, states, timeout=12):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            status, run = self.request('/v1/runs/' + run_id)
            if run.get('state') in states:
                return run
            time.sleep(.02)
        raise AssertionError(f'Task did not reach {states}: {run}')
