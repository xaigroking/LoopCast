"""Private runtime admission marker, independent of the permanent business ledger."""
import json
import os
import re
import stat
import sys
from .ownership import DirectoryLock, directory

from .runtime_guard import NAME, FORMAT, read_guard, check

def create(path, identity):
    with DirectoryLock(path, create=True) as lock:
        handle = os.open(NAME, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=lock.directory_fd)
        try:
            os.write(handle, json.dumps({'format': FORMAT, 'identity': identity}).encode()); os.fsync(handle)
        finally: os.close(handle)
        os.fsync(lock.directory_fd)

def finalize(path, identity):
    with DirectoryLock(path, desktop_identity=identity) as lock:
        value = read_guard(lock.directory_fd)
        info = os.stat(NAME, dir_fd=lock.directory_fd, follow_symlinks=False)
        if value is None or value[0]['identity'] != identity or value[1] != (info.st_dev, info.st_ino): raise ValueError('GUARD_CHANGED')
        os.unlink(NAME, dir_fd=lock.directory_fd); os.fsync(lock.directory_fd)

def validate(paths):
    for path in paths:
        _, fd = directory(path)
        try:
            info = os.fstat(fd)
            if info.st_uid != os.getuid() or info.st_mode & 0o077: raise ValueError('PRIVATE_DIRECTORY_REQUIRED')
            check(fd)
        finally: os.close(fd)

if __name__ == '__main__':
    if sys.argv[1:2] == ['validate-result']:
        from .desktop_diagnostics import preflight
        code = preflight(sys.argv[2:]) if len(sys.argv) == 4 else 'DIAGNOSTICS_UNAVAILABLE'
        print(json.dumps({'format':'loopcast.desktop-preflight.v1','code':code},separators=(',',':')))
        sys.exit(0 if code == 'OK' else 1)
    try:
        if sys.argv[1] == 'validate': validate(sys.argv[2:])
        elif sys.argv[1] == 'finalize' and len(sys.argv) == 4: finalize(sys.argv[2], sys.argv[3])
        else: raise ValueError('INVALID_OPERATION')
    except (OSError, ValueError, KeyError, TypeError):
        sys.exit('Managed directory validation failed.')
