"""Read-only installed source and platform capabilities; no business imports."""
from importlib.resources import files
import json
import os
import platform
import sqlite3
import sys

def source_revision():
    try: return json.loads(files('loopcast').joinpath('resources/source.json').read_text())['revision']
    except (OSError, ValueError, KeyError): return 'unavailable'

def supported():
    return sys.platform == 'linux' and platform.machine() == 'x86_64' and hasattr(os, 'pidfd_open')

def platform_info():
    status = 'implemented' if supported() else 'unsupported'
    return {'format': 'loopcast.platform.v1', 'source_revision': source_revision(),
            'os': sys.platform, 'arch': platform.machine(), 'python': platform.python_version(),
            'sqlite': sqlite3.sqlite_version,
            'capabilities': {key: status for key in ('file_access', 'owner_lock', 'sqlite_access', 'process_tree', 'managed_stop', 'desktop_session')},
            'verification': 'implementation_only'}
