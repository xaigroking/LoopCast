"""Bounded local simulator jobs; never a real uploader."""
import json
import queue
import sys
import threading
import time

from .runner import run, RunnerError
from .diagnostics import child_context, log_event


class Publisher:
    def __init__(self, store):
        self.store = store
        self.stopping = threading.Event()
        self.jobs = queue.Queue(maxsize=32)
        self.thread = threading.Thread(target=self._loop, name='publication-simulator', daemon=True)
        self.directory = store.directory / 'simulator'
        self.directory.mkdir(exist_ok=True)
        self.store.publication_recover()
        self.thread.start()

    def close(self):
        self.stopping.set()
        self.thread.join(5)
        if self.thread.is_alive(): raise RuntimeError('Publisher did not stop')
        self.store.publication_recover()

    def submit(self, job, scenario):
        try: self.jobs.put_nowait((job, scenario))
        except queue.Full:
            self.store.publication_result(job, 'NOT_SENT', evidence='Simulator queue was full; no process started')

    def _loop(self):
        while not self.stopping.is_set():
            try: job, scenario = self.jobs.get(timeout=.1)
            except queue.Empty: continue
            context = child_context(job.get('trace'))
            fields = {'run_id': job['run_id'], 'intent_id': job['intent_id'], 'publication_attempt_id': job['attempt_id']}
            started = time.monotonic()
            log_event('publisher', 'publication.started', context, **fields)
            try:
                if scenario == 'not_sent':
                    self.store.publication_result(job, 'NOT_SENT', evidence='Simulator rejected before the send boundary')
                    continue
                if not self.store.publication_sending(job):
                    self.store.publication_abandon_prepared(job)
                    continue
                try:
                    raw = run([sys.executable, '-I', '-m', 'loopcast.publisher_cli', str(self.directory), job['attempt_id'], scenario],
                              self.directory, {'PATH':'/usr/bin:/bin','LANG':'C.UTF-8'}, timeout=2,
                              cancelled=self.stopping.is_set)
                    value = json.loads(raw)
                    self.store.publication_result(job, 'PUBLISHED' if value.get('work_id') else 'UNKNOWN',value.get('work_id'),
                                                  evidence='Controlled simulator result; no real platform sending')
                except (RunnerError, ValueError):
                    self.store.publication_result(job, 'UNKNOWN',evidence='Simulator result interrupted or ambiguous; no real platform sending')
            except Exception:
                self.store.publication_result(job,'UNKNOWN',evidence='Simulator execution could not be completed')
            finally:
                # Never log account names, capability tokens, output or exception text.
                state = 'UNAVAILABLE'
                try:
                    state = next(p['state'] for p in self.store.snapshot(job['run_id'])['publications'] if p['id'] == job['intent_id'])
                except Exception:
                    pass
                log_event('publisher', 'publication.completed', context, result=state,
                          duration_ms=round((time.monotonic() - started) * 1000, 2), **fields)
                self.jobs.task_done()
