"""One bounded executor; SQLite is the durable queue, never an HTTP request."""
import hashlib
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone

from .storage import STAGES, uid, digest

_logger = logging.getLogger("loopcast.worker")
_logger.setLevel(logging.INFO)


def _log_attempt(event, attempt, **extra):
    _logger.info(json.dumps({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "level": "INFO",
        "component": "worker",
        "event": event,
        "trace_id": attempt.get("trace_id"),
        "run_id": attempt.get("run_id"),
        "attempt_id": attempt.get("id"),
        "attempt_number": attempt.get("number"),
        **extra,
    }, separators=(",", ":")))


class Worker:
    def __init__(self, store, timeout=8, generator=None):
        self.generator = generator
        self.store, self.timeout = store, timeout
        self.stop_event = threading.Event()
        self.wake = threading.Event()
        self.thread = None
        self.active_process = None

    def start(self):
        self.store.recover()
        self.thread = threading.Thread(target=self._loop, name='fixture-worker', daemon=True)
        self.thread.start()

    def close(self):
        self.stop_event.set()
        self.wake.set()
        if self.thread:
            self.thread.join(self.timeout + 3)
            if self.thread.is_alive():
                raise RuntimeError('Executor did not stop')

    def _loop(self):
        while not self.stop_event.is_set():
            attempt = self.store.claim()
            if attempt is None:
                self.wake.wait(.1)
                self.wake.clear()
                continue
            started = time.monotonic()
            _log_attempt("attempt.started", attempt)
            try:
                self._execute(attempt)
            except Exception:
                # Preserve a safe failure fact; do not return success or host details.
                self.store.finish(attempt['run_id'], attempt['token'], 'FAILED', 'EXECUTOR_ERROR')
            try:
                state = self.store.snapshot(attempt['run_id'])['state']
            except Exception:
                state = 'UNAVAILABLE'
            _log_attempt("attempt.completed", attempt, result=state, duration_ms=round((time.monotonic() - started) * 1000, 2))

    def _execute(self, attempt):
        if attempt['input']['mode'] == 'mpt':
            return self._generate(attempt)
        run_id, token = attempt['run_id'], attempt['token']
        deadline = time.monotonic() + self.timeout
        output = b''
        for stage in STAGES:
            if self.stop_event.is_set():
                self.store.finish(run_id, token, 'FAILED', 'INTERRUPTED')
                return
            if self.store.snapshot(run_id)['state'] == 'CANCEL_REQUESTED':
                self.store.finish(run_id, token, 'CANCELLED')
                return
            self.store.stage(run_id, token, stage, 'RUNNING')
            proc = subprocess.Popen([sys.executable, '-I', '-m', 'loopcast.fixture'], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.active_process = proc
            try:
                proc.stdin.write(json.dumps({'stage': stage, 'input': attempt['input'], 'number': attempt['number']}).encode())
                proc.stdin.close()
                reason = None
                while proc.poll() is None:
                    if self.stop_event.is_set():
                        reason = 'INTERRUPTED'
                    elif time.monotonic() >= deadline:
                        reason = 'TIMEOUT'
                    elif self.store.snapshot(run_id)['state'] == 'CANCEL_REQUESTED':
                        reason = 'CANCELLED'
                    if reason:
                        proc.terminate()
                        try:
                            proc.wait(timeout=1)
                        except subprocess.TimeoutExpired:
                            proc.kill()
                            proc.wait(timeout=1)
                        break
                    self.stop_event.wait(.02)
                output = proc.stdout.read(16384)
                proc.stderr.read(16384)
                if reason:
                    self.store.finish(run_id, token, 'CANCELLED' if reason == 'CANCELLED' else 'FAILED', None if reason == 'CANCELLED' else reason)
                    return
                if proc.returncode:
                    self.store.finish(run_id, token, 'FAILED', 'FIXTURE_FAILED' if proc.returncode == 2 else 'EXECUTOR_EXITED')
                    return
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()
                proc.stdout.close()
                proc.stderr.close()
                self.active_process = None
            self.store.stage(run_id, token, stage, 'SUCCEEDED')
        artifact_id = uid()
        path = self.store.artifact_dir / (artifact_id + '.txt')
        # Exclusive creation and immutable metadata: never overwrite an old report.
        with path.open('xb') as stream:
            stream.write(output)
            stream.flush()
            os.fsync(stream.fileno())
        metadata = {'id': artifact_id, 'size': len(output), 'sha256': hashlib.sha256(output).hexdigest(), 'storage_ref': path.name}
        if not self.store.finish(run_id, token, 'SUCCEEDED', artifact=metadata):
            path.unlink(missing_ok=True)

    def _generate(self, attempt):
        from .runner import RunnerError
        import shutil
        run_id, token = attempt['run_id'], attempt['token']
        if self.generator is None or attempt['input']['component_id'] != digest(self.generator.manifest):
            self.store.finish(run_id, token, 'FAILED', 'COMPONENT_UNAVAILABLE')
            return
        work = self.store.directory / 'work' / attempt['id']
        artifact_path = None
        def cancelled():
            return self.stop_event.is_set() or self.store.snapshot(run_id)['state'] == 'CANCEL_REQUESTED'
        try:
            self.store.stage(run_id, token, 'prepare', 'RUNNING')
            self.store.stage(run_id, token, 'prepare', 'SUCCEEDED')
            self.store.stage(run_id, token, 'execute', 'RUNNING')
            source, media = self.generator.generate(attempt['input'], work, cancelled)
            self.store.stage(run_id, token, 'execute', 'SUCCEEDED')
            self.store.stage(run_id, token, 'report', 'RUNNING')
            identity = uid()
            artifact_path = self.store.artifact_dir / (identity + '.mp4')
            with source.open('rb') as origin, artifact_path.open('xb') as target:
                shutil.copyfileobj(origin, target)
                target.flush()
                os.fsync(target.fileno())
            content = artifact_path.read_bytes()
            metadata = {'id': identity, 'size': len(content), 'sha256': hashlib.sha256(content).hexdigest(),
                        'storage_ref': artifact_path.name, 'media_type': 'video/mp4', 'media': media}
            self.store.stage(run_id, token, 'report', 'SUCCEEDED')
            if self.store.finish(run_id, token, 'SUCCEEDED', artifact=metadata):
                artifact_path = None
        except RunnerError as error:
            stopped = self.stop_event.is_set()
            self.store.finish(run_id, token, 'CANCELLED' if error.code == 'CANCELLED' and not stopped else 'FAILED',
                              None if error.code == 'CANCELLED' and not stopped else 'INTERRUPTED' if stopped else error.code)
        finally:
            if artifact_path: artifact_path.unlink(missing_ok=True)
            shutil.rmtree(work, ignore_errors=True)
