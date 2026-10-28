"""Fixed private maintenance helper. Capabilities arrive on stdin, never argv."""
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from .ownership import DirectoryLock, directory
from .runtime_guard import MAINTENANCE_NAME, MAINTENANCE_FORMAT, read_guard
from .backup import create_owned_backup, verify_backup
from .restore import restore_backup
from .platform_info import platform_info


def identity(info):
    return [str(info.st_dev), str(info.st_ino)]


def same(path, fd, expected):
    _, fresh = directory(path)
    try:
        if identity(os.fstat(fresh)) != identity(os.fstat(fd)) or identity(os.fstat(fd)) != expected:
            raise ValueError('STATE_CHANGED')
    finally:
        os.close(fresh)


def execute(request):
    action = request['action']
    if action == 'identity' and set(request) == {'action'}: return platform_info()
    if action == 'verify':
        if set(request) != {'action', 'backup', 'digest'}: raise ValueError('INVALID_REQUEST')
        value = verify_backup(request['backup'], request['digest'])
        return {'state': 'VERIFIED_PRIVATE_BACKUP', 'backup_id': value['backup_id'],
                'schema_version': value['schema_version'], 'files': len(value['files'])}
    fields = {'action', 'source', 'source_identity', 'capability'}
    if action in {'create', 'restore'}: fields.add('target')
    if 'parent_identity' in request and action in {'create', 'restore'}: fields.add('parent_identity')
    if action == 'restore': fields.update({'backup', 'digest'})
    if set(request) != fields or action not in {'begin', 'finish', 'create', 'restore'}:
        raise ValueError('INVALID_REQUEST')
    capability = request['capability']
    if not isinstance(capability, str) or not re.fullmatch('[a-f0-9]{64}', capability):
        raise ValueError('CAPABILITY_REQUIRED')
    operation = hashlib.sha256(bytes.fromhex(capability)).hexdigest()[:32]
    with DirectoryLock(request['source'], maintenance_identity=None if action == 'begin' else operation) as owner:
        info = os.fstat(owner.directory_fd)
        if info.st_uid != os.getuid() or info.st_mode & 0o077: raise ValueError('PRIVATE_DIRECTORY_REQUIRED')
        same(request['source'], owner.directory_fd, request['source_identity'])
        if action == 'begin':
            fd = os.open(MAINTENANCE_NAME, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=owner.directory_fd)
            try:
                os.write(fd, json.dumps({'format': MAINTENANCE_FORMAT, 'identity': operation}).encode())
                os.fsync(fd)
            finally: os.close(fd)
            os.fsync(owner.directory_fd)
        elif action == 'finish':
            marker = read_guard(owner.directory_fd, MAINTENANCE_NAME, MAINTENANCE_FORMAT)
            named = os.stat(MAINTENANCE_NAME, dir_fd=owner.directory_fd, follow_symlinks=False)
            if marker is None or marker[0]['identity'] != operation or marker[1] != (named.st_dev, named.st_ino):
                raise ValueError('STATE_CHANGED')
            os.unlink(MAINTENANCE_NAME, dir_fd=owner.directory_fd)
            os.fsync(owner.directory_fd)
        elif action == 'create':
            result = create_owned_backup(owner, request['target'], expected_parent=request.get('parent_identity'))
            same(request['source'], owner.directory_fd, request['source_identity'])
            return result
        else:
            # The source lock stays held throughout restore. The existing core
            # owns only its new staging directory, never a second source owner.
            source = Path(request['source'])
            backup = Path(request['backup'])
            target = Path(request['target'])
            if any(a == b or a in b.parents or b in a.parents for a, b in [(source, backup), (source, target)]):
                raise ValueError('PATH_OVERLAP')
            result = restore_backup(backup, request['digest'], target, expected_parent=request.get('parent_identity'))
            same(request['source'], owner.directory_fd, request['source_identity'])
            return result
        same(request['source'], owner.directory_fd, request['source_identity'])
        return {'format': 'loopcast.maintenance-marker.v1', 'identity': operation, 'state': action.upper()}


def main():
    try:
        raw = sys.stdin.buffer.read(16385)
        if len(raw) > 16384: raise ValueError('INVALID_REQUEST')
        def unique(items):
            value = {}
            for key, item in items:
                if key in value: raise ValueError('INVALID_REQUEST')
                value[key] = item
            return value
        result = execute(json.loads(raw, object_pairs_hook=unique))
        print(json.dumps(result, sort_keys=True))
        return 0
    except Exception:
        # No paths, capability, child output or business facts on stderr.
        sys.stderr.write('Maintenance result unavailable. Preserve existing evidence.\n')
        return 1


if __name__ == '__main__':
    sys.exit(main())
