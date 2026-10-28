"""Linux subreaper guardian. Private fd4 controls; fd5 proves complete tree exit."""
import ctypes
import json
import os
from pathlib import Path
import selectors
import signal
import subprocess
import sys
import time
import uuid
from .platform_info import supported
from .desktop_guard import create

def reap(deadline):
    """Kernel reparenting precedes waitpid: ECHILD is a complete subreaper proof."""
    while True:
        try:
            while True:
                pid, _ = os.waitpid(-1, os.WNOHANG)
                if pid == 0: break
        except ChildProcessError: return True
        for text in Path(f'/proc/self/task/{os.getpid()}/children').read_text().split():
            try:
                fd = os.pidfd_open(int(text))
                try: signal.pidfd_send_signal(fd, signal.SIGKILL)
                finally: os.close(fd)
            except ProcessLookupError: pass
        if time.monotonic() >= deadline: return False
        time.sleep(.005)

def main():
    if not supported(): return 2
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(36, 1, 0, 0, 0) != 0: return 2  # PR_SET_CHILD_SUBREAPER
    from .desktop_admission import load_private
    private_bootstrap = load_private(close=True)
    argv = sys.argv[1:]
    identity = None
    if '--desktop-session-stdin' in argv:
        data = argv[argv.index('--data-dir') + 1]
        identity = uuid.uuid4().hex
        create(data, identity)
        argv += ['--desktop-guard-identity', identity]
    child = None
    forced = False
    deadline = None
    try:
        private_child = private_bootstrap is not None and len(argv) >= 4 and os.path.realpath(argv[0]) == os.path.realpath(sys.executable) and argv[1:3] == ['-I', '-m'] and argv[3] in {'loopcast', 'loopcast.desktop_guard', 'loopcast.desktop_admission', 'loopcast.desktop_maintenance', 'loopcast.desktop_workspace'}
        if private_child:
            read_fd, write_fd = os.pipe()
            try:
                os.write(write_fd, private_bootstrap)
                os.dup2(read_fd, 6)
            finally:
                os.close(write_fd)
                if read_fd != 6: os.close(read_fd)
        child = subprocess.Popen(argv, close_fds=True, pass_fds=(3, 6) if private_child else (3,), start_new_session=True)
        if private_child: os.close(6)
        os.set_blocking(4, False)
        with selectors.DefaultSelector() as selector:
            selector.register(4, selectors.EVENT_READ)
            buffer = b''
            while child.poll() is None:
                for _, _ in selector.select(.02):
                    chunk = os.read(4, 128)
                    buffer += chunk
                    if not chunk or len(buffer) > 64 or b'\n' in buffer:
                        forced = True
                        milliseconds = 1000
                        try:
                            line = buffer.decode('ascii').strip().split(' ')
                            if len(line) == 2 and line[0] == 'TERMINATE': milliseconds = min(60000, max(0, int(line[1])))
                        except (ValueError, UnicodeError): pass
                        deadline = time.monotonic() + milliseconds / 1000
                        break
                if forced: break
        code = child.poll()
        # A normal leader exit still requires reaping every adopted descendant.
        confirmed = reap(deadline if deadline is not None else time.monotonic() + 5)
        if not confirmed: return 3
        os.write(5, (json.dumps({'format': 'loopcast.tree-stopped.v1', 'identity': identity, 'confirmed': True}) + '\n').encode())
        return code if code is not None and code >= 0 else 1
    finally:
        # Exceptions never leak a live owned tree or emit a false witness.
        if child is not None: reap(time.monotonic() + 1)

if __name__ == '__main__':
    try: result = main()
    except (OSError, ValueError, RuntimeError, KeyError, IndexError): result = 2
    sys.exit(result)
