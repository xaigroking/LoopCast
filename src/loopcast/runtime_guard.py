"""Anchored runtime guard validation; no ownership or business dependencies."""
import json
import os
import re
import stat

NAME = 'desktop-runtime.guard.v1'
FORMAT = 'loopcast.desktop-guard.v1'

def read_guard(fd):
    try: handle = os.open(NAME, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    except FileNotFoundError: return None
    try:
        info = os.fstat(handle)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_size > 256: raise ValueError('UNSAFE_GUARD')
        value = json.loads(os.read(handle, 257))
        if set(value) != {'format', 'identity'} or value['format'] != FORMAT or not re.fullmatch('[a-f0-9]{32}', value['identity']): raise ValueError('UNSAFE_GUARD')
        return value, (info.st_dev, info.st_ino)
    finally: os.close(handle)

def check(fd, identity=None):
    guard = read_guard(fd)
    if guard is not None and (identity is None or guard[0]['identity'] != identity): raise ValueError('DESKTOP_CLEANUP_UNCONFIRMED')
    if identity is not None and guard is None: raise ValueError('DESKTOP_GUARD_MISSING')
