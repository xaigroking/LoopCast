"""Bounded process-group runner. Child output never becomes an error message."""
import os
import selectors
import signal
import subprocess
import time


class RunnerError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def run(argv, cwd, env, timeout=30, cancelled=lambda: False, limit=1048576):
    process = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    outputs = [bytearray(), bytearray()]
    deadline = time.monotonic() + timeout
    try:
        with selectors.DefaultSelector() as selector:
            for index, pipe in enumerate((process.stdout, process.stderr)):
                os.set_blocking(pipe.fileno(), False)
                selector.register(pipe, selectors.EVENT_READ, index)
            while selector.get_map() or process.poll() is None:
                if cancelled():
                    raise RunnerError('CANCELLED')
                if time.monotonic() >= deadline:
                    raise RunnerError('TIMEOUT')
                for key, _ in selector.select(.02):
                    chunk = os.read(key.fileobj.fileno(), 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                    else:
                        outputs[key.data].extend(chunk)
                        if sum(map(len, outputs)) > limit:
                            raise RunnerError('OUTPUT_LIMIT')
        if process.wait() != 0:
            raise RunnerError('UPSTREAM_FAILED')
        return bytes(outputs[0])
    finally:
        # Kill the group even if the leader exited but left descendants alive.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=5)
        process.stdout.close()
        process.stderr.close()
