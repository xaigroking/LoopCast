"""Fixed read-only production workspace observations for the native supervisor."""
import json
import os
import sys
from .desktop_admission import load_private, unique
from .ownership import directory
from .desktop_diagnostics import same_directory
from .workspace_inspection import directory_identity, inspect_restored


def observe(request):
    if request.get('action') == 'entity' and set(request) == {'action', 'path'}:
        path, fd = directory(request['path'])
        try:
            value = directory_identity(fd)
            same_directory(path, fd)
            return value
        finally: os.close(fd)
    if request.get('action') == 'restored' and set(request) == {'action', 'path', 'manifest_sha256', 'backup_id', 'restore_id'}:
        return inspect_restored(request['path'], expected_digest=request['manifest_sha256'], expected_backup_id=request['backup_id'], expected_restore_id=request['restore_id'])
    raise ValueError('INVALID_WORKSPACE_OBSERVATION')


def main():
    try:
        load_private(close=True)
        raw = sys.stdin.buffer.read(8193)
        if len(raw) > 8192: raise ValueError('INVALID_WORKSPACE_OBSERVATION')
        print(json.dumps(observe(json.loads(raw, object_pairs_hook=unique)), sort_keys=True))
        return 0
    except Exception:
        sys.stderr.write('Workspace identity or recovery evidence is unavailable.\n'); return 1


if __name__ == '__main__': sys.exit(main())
