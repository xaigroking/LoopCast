"""Per-user application admission. No business state, sender or recovery bypass.

This file also runs with an isolated stdlib interpreter as the fixed boot lease
helper, before a second invocation is allowed to inspect its launch arguments.
"""
import errno
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import stat
import sys
if sys.platform == 'linux':
    import fcntl
    import pwd

FORMAT = 'loopcast.application-pending.v1'
BOOTSTRAP = 'loopcast.private-admission.v1'
_capability = None
_lease_capability = None


def state_path():
    if sys.platform != 'linux': raise ValueError('PLATFORM_UNSUPPORTED')
    return Path(pwd.getpwuid(os.getuid()).pw_dir) / '.local/state/loopcast-desktop'


def private(info, directory=False):
    if not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode) and info.st_nlink == 1) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError('HANDOFF_UNCONFIRMED')


def root_fd(create=False):
    target = state_path()
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in target.parts[1:]:
            if create:
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                    os.fsync(fd)
                except FileExistsError: pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd); fd = child
        private(os.fstat(fd), True)
        return fd
    except BaseException:
        os.close(fd); raise


def unique(items):
    value = {}
    for key, item in items:
        if key in value: raise ValueError('HANDOFF_UNCONFIRMED')
        value[key] = item
    return value


def record(fd, name='pending', format=FORMAT):
    try: handle = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    except FileNotFoundError: return None
    try:
        info = os.fstat(handle); private(info)
        if info.st_size > 512: raise ValueError('HANDOFF_UNCONFIRMED')
        value = json.loads(os.read(handle, 513), object_pairs_hook=unique)
        if not isinstance(value, dict) or set(value) != {'format', 'identity', 'commitment'} or value['format'] != format or not isinstance(value['identity'], str) or not isinstance(value['commitment'], str) or not re.fullmatch('[a-f0-9]{32}', value['identity']) or not re.fullmatch('[a-f0-9]{64}', value['commitment']):
            raise ValueError('HANDOFF_UNCONFIRMED')
        return value, (info.st_dev, info.st_ino)
    finally: os.close(handle)


def pending(fd): return record(fd)


def check(capability=None):
    capability = _capability if capability is None else capability
    if sys.platform != 'linux':
        if capability is not None: raise ValueError('PLATFORM_UNSUPPORTED')
        return
    try: fd = root_fd()
    except FileNotFoundError:
        if capability is not None: raise ValueError('HANDOFF_CAPABILITY_EXPIRED')
        return
    try:
        active = record(fd, 'active', 'loopcast.application-active.v1')
        if active is not None and (not isinstance(_lease_capability, str) or not hmac.compare_digest(active[0]['commitment'], hashlib.sha256(bytes.fromhex(_lease_capability)).hexdigest())):
            raise ValueError('HANDOFF_UNCONFIRMED')
        if active is None and _lease_capability is not None: raise ValueError('HANDOFF_CAPABILITY_EXPIRED')
        value = pending(fd)
        if value is None:
            if capability is not None: raise ValueError('HANDOFF_CAPABILITY_EXPIRED')
            return
        if not isinstance(capability, str) or not re.fullmatch('[a-f0-9]{64}', capability) or not hmac.compare_digest(value[0]['commitment'], hashlib.sha256(bytes.fromhex(capability)).hexdigest()):
            raise ValueError('HANDOFF_UNCONFIRMED')
    finally: os.close(fd)


def _load_private(fd=6):
    global _capability, _lease_capability
    if sys.platform != 'linux': return None
    try:
        raw = b''
        while True:
            part = os.read(fd, 257 - len(raw))
            if not part: break
            raw += part
            if len(raw) > 256: raise ValueError('HANDOFF_UNCONFIRMED')
    except OSError as error:
        if error.errno != errno.EBADF: raise
        _capability = None; _lease_capability = None
        return None
    value = json.loads(raw, object_pairs_hook=unique)
    if set(value) != {'format', 'capability', 'lease'} or value['format'] != BOOTSTRAP:
        raise ValueError('HANDOFF_UNCONFIRMED')
    capability = value['capability']
    if capability is not None and (not isinstance(capability, str) or not re.fullmatch('[a-f0-9]{64}', capability)):
        raise ValueError('HANDOFF_UNCONFIRMED')
    lease_capability = value['lease']
    if lease_capability is not None and (not isinstance(lease_capability, str) or not re.fullmatch('[a-f0-9]{64}', lease_capability)): raise ValueError('HANDOFF_UNCONFIRMED')
    _capability, _lease_capability = capability, lease_capability
    if capability is not None or lease_capability is not None: check()
    return raw


def load_private(fd=6, *, close=False):
    try: return _load_private(fd)
    finally:
        if close and sys.platform == 'linux':
            try: os.close(fd)
            except OSError as error:
                if error.errno != errno.EBADF: raise


def begin(identity, capability):
    if not re.fullmatch('[a-f0-9]{32}', identity) or not re.fullmatch('[a-f0-9]{64}', capability): raise ValueError('HANDOFF_UNCONFIRMED')
    fd = root_fd(True)
    try:
        if pending(fd) is not None: raise ValueError('HANDOFF_UNCONFIRMED')
        handle = os.open('pending', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
        try:
            os.write(handle, json.dumps({'format': FORMAT, 'identity': identity, 'commitment': hashlib.sha256(bytes.fromhex(capability)).hexdigest()}).encode())
            os.fsync(handle)
        finally: os.close(handle)
        os.fsync(fd)
    finally: os.close(fd)


def finish(identity, capability):
    check(capability)
    fd = root_fd()
    try:
        value = pending(fd)
        named = os.stat('pending', dir_fd=fd, follow_symlinks=False)
        if value is None or value[0]['identity'] != identity or value[1] != (named.st_dev, named.st_ino): raise ValueError('HANDOFF_UNCONFIRMED')
        os.unlink('pending', dir_fd=fd); os.fsync(fd)
    finally: os.close(fd)


def lease():
    global _lease_capability
    request = json.loads(sys.stdin.buffer.readline(257), object_pairs_hook=unique)
    if set(request) != {'format', 'capability'} or request['format'] != 'loopcast.application-lease-request.v1' or not re.fullmatch('[a-f0-9]{64}', request['capability']): raise ValueError('HANDOFF_UNCONFIRMED')
    fd = root_fd(True)
    handle = None
    try:
        handle = os.open('application.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=fd)
        private(os.fstat(handle)); fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        check()
        identity = hashlib.sha256(bytes.fromhex(request['capability'])).hexdigest()
        active = os.open('active', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
        try:
            os.write(active, json.dumps({'format': 'loopcast.application-active.v1', 'identity': identity[:32], 'commitment': identity}).encode()); os.fsync(active)
        finally: os.close(active)
        os.fsync(fd)
        _lease_capability = request['capability']
        print(json.dumps({'format': 'loopcast.application-lease.v1', 'root': str(state_path()), 'identity': identity[:32]}), flush=True)
        raw = sys.stdin.buffer.readline(33)
        if raw != b'STOP\n' or pending(fd) is not None: return 1
        value = record(fd, 'active', 'loopcast.application-active.v1')
        info = os.stat('active', dir_fd=fd, follow_symlinks=False)
        if value is None or value[0]['commitment'] != identity or value[1] != (info.st_dev, info.st_ino): return 1
        os.unlink('active', dir_fd=fd); os.fsync(fd)
        return 0
    finally:
        if handle is not None: os.close(handle)
        os.close(fd)


def main():
    try:
        if sys.argv[1:] == ['lease']: return lease()
        load_private(close=True)
        raw = sys.stdin.buffer.read(1025)
        if len(raw) > 1024: raise ValueError('HANDOFF_UNCONFIRMED')
        value = json.loads(raw, object_pairs_hook=unique)
        if value == {'action': 'state'}:
            print(json.dumps({'format': 'loopcast.application-state.v1', 'root': str(state_path()), 'lease_script': str(Path(__file__).resolve()), 'lease_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), 'base_python': str(Path(sys.executable).resolve())})); return 0
        if set(value) != {'action', 'identity', 'capability'}: raise ValueError('HANDOFF_UNCONFIRMED')
        if value['action'] == 'begin': begin(value['identity'], value['capability'])
        elif value['action'] == 'finish': finish(value['identity'], value['capability'])
        else: raise ValueError('HANDOFF_UNCONFIRMED')
        print(json.dumps({'format': 'loopcast.application-stage.v1', 'identity': value['identity'], 'action': value['action']}))
        return 0
    except BlockingIOError: return 3
    except Exception:
        sys.stderr.write('Application admission is unconfirmed. Preserve existing evidence.\n'); return 1


if __name__ == '__main__': sys.exit(main())
