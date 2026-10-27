"""Unconnected read-only candidate observations, never workspace admission.

No Store, migration, owner creation, profile assignment or launch entry point.
Independent expected identifiers must come from the trusted caller's records.
"""
import ctypes
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import sys
from . import desktop_diagnostics as files
from .backup import MAX_MANIFEST
from .ownership import directory

FORMAT = 'loopcast.restored-observation.candidate.v1'
HEX32 = re.compile(r'[a-f0-9]{32}\Z')
HEX64 = re.compile(r'[a-f0-9]{64}\Z')
SCOPE = 'ALL_STUDIO_UPLOADS'

def require(value, pattern):
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise files.DiagnosticError('RESTORE_SOURCE_REQUIRED')
    return value

def directory_identity(fd):
    """Boot-scoped generation-aware identity; never fall back to dev+ino."""
    if sys.platform != 'linux' or platform.machine() != 'x86_64':
        raise files.DiagnosticError('PLATFORM_UNSUPPORTED')
    files.private(os.fstat(fd), 'directory')
    library = ctypes.CDLL(None, use_errno=True)
    buffer = ctypes.create_string_buffer(256)
    library.fstatfs.argtypes = [ctypes.c_int, ctypes.c_void_p]
    library.fstatfs.restype = ctypes.c_int
    if library.fstatfs(fd, buffer): raise files.DiagnosticError('ENTITY_IDENTITY_UNAVAILABLE')
    filesystem_type = ctypes.c_long.from_buffer(buffer).value
    class Handle(ctypes.Structure):
        _fields_ = [('length', ctypes.c_uint), ('kind', ctypes.c_int), ('value', ctypes.c_ubyte * 128)]
    handle = Handle(); handle.length = 128
    mount = ctypes.c_int()
    try: function = library.name_to_handle_at
    except AttributeError: raise files.DiagnosticError('ENTITY_IDENTITY_UNAVAILABLE') from None
    function.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.POINTER(Handle), ctypes.POINTER(ctypes.c_int), ctypes.c_int]
    function.restype = ctypes.c_int
    if function(fd, b'', ctypes.byref(handle), ctypes.byref(mount), 0x1000) or not 1 <= handle.length <= 128 or mount.value <= 0:
        raise files.DiagnosticError('ENTITY_IDENTITY_UNAVAILABLE')
    # ext2/ext3 share ext4's magic. Require the kernel's actual mount type too.
    with open('/proc/self/mountinfo') as stream: mounts = stream.read(1048577)
    if len(mounts) > 1048576: raise files.DiagnosticError('ENTITY_IDENTITY_UNAVAILABLE')
    types = []
    for line in mounts.splitlines():
        left, separator, right = line.partition(' - ')
        if separator and left.split()[0] == str(mount.value): types.append(right.split()[0])
    filesystem = {'ext4':(0xEF53,'EXT4'), 'tmpfs':(0x01021994,'TMPFS')}.get(types[0]) if len(types) == 1 else None
    if filesystem is None or filesystem_type != filesystem[0]: raise files.DiagnosticError('ENTITY_IDENTITY_UNAVAILABLE')
    # Ordinary mount IDs may be recycled. Linux 6.8+ provides a boot-unique ID.
    try: statx = library.statx
    except AttributeError: raise files.DiagnosticError('ENTITY_IDENTITY_UNAVAILABLE') from None
    statx.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_uint, ctypes.c_void_p]
    statx.restype = ctypes.c_int
    metadata = ctypes.create_string_buffer(256)
    if statx(fd, b'', 0x1000, 0x4000, metadata): raise files.DiagnosticError('ENTITY_IDENTITY_UNAVAILABLE')
    mask = ctypes.c_uint32.from_buffer(metadata).value
    unique_mount = ctypes.c_uint64.from_buffer(metadata, 144).value
    if not mask & 0x4000 or unique_mount == 0: raise files.DiagnosticError('ENTITY_IDENTITY_UNAVAILABLE')
    boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    if not re.fullmatch('[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}', boot):
        raise files.DiagnosticError('ENTITY_IDENTITY_UNAVAILABLE')
    boot_hash = hashlib.sha256(b'loopcast-kernel-boot\0' + boot.encode()).hexdigest()
    raw = b'loopcast-directory-object\0' + boot_hash.encode() + b'\0' + str(unique_mount).encode() + b'\0' + str(handle.kind).encode() + b'\0' + bytes(handle.value[:handle.length])
    return {'identity_scope':'KERNEL_BOOT', 'filesystem':filesystem[1], 'boot_sha256':boot_hash,
            'object_sha256':hashlib.sha256(raw).hexdigest()}

def absent(fd, name, code):
    try: os.stat(name, dir_fd=fd, follow_symlinks=False)
    except FileNotFoundError: return
    raise files.DiagnosticError(code)

def admission_observations(fd):
    # Probe only an existing lock; this creates neither a lock nor a business owner.
    code, _ = files.owner_probe(fd)
    if code != 'OWNER_NOT_HELD': raise files.DiagnosticError(code)
    code, _ = files.guard_probe(fd)
    if code != 'GUARD_ABSENT': raise files.DiagnosticError(code)
    absent(fd, 'desktop-maintenance.guard.v1', 'MAINTENANCE_UNCONFIRMED')

def inspect_restored(path, *, expected_digest, expected_backup_id, expected_restore_id,
                     timeout_ms=2000, max_bytes=268435456, cancelled=lambda: False):
    """Observe an existing guarded restore using independently supplied evidence.

    A successful observation is NOT permission to start. Final ownership, global
    handoff state, resources and profile provenance need separate revalidation.
    """
    require(expected_digest, HEX64); require(expected_backup_id, HEX32); require(expected_restore_id, HEX32)
    budget = files.Budget(timeout_ms, max_bytes, cancelled); budget.check()
    path = files.absolute(path); _, fd = directory(path)
    try:
        files.private(os.fstat(fd), 'directory')
        entity = directory_identity(fd)
        admission_observations(fd)
        names = ('state.sqlite3','state.sqlite3-wal','restore-manifest.json','service.lock')
        def snapshot():
            result = {}
            for name in names:
                try: info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                except FileNotFoundError: result[name] = None
                else: result[name] = files.version(info)
            return result
        before = snapshot()
        raw = files.small_file(path/'restore-manifest.json', budget, MAX_MANIFEST)
        if hashlib.sha256(raw).hexdigest() != expected_digest:
            raise files.DiagnosticError('RESTORE_SOURCE_MISMATCH')
        try: manifest = json.loads(raw, object_pairs_hook=files.pairs)
        except (ValueError, TypeError): raise files.DiagnosticError('RESTORE_SOURCE_MISMATCH') from None
        required = {'format','backup_id','created_at','schema_version','credentials_policy','application','boundary','counts','files'}
        if not isinstance(manifest, dict) or set(manifest) != required or manifest['format'] != 'loopcast.backup.v1' or manifest['credentials_policy'] != 'excluded_reauthorize' or manifest['backup_id'] != expected_backup_id or type(manifest['schema_version']) is not int or manifest['schema_version'] not in {6,7,8,9}:
            raise files.DiagnosticError('RESTORE_SOURCE_MISMATCH')
        with files.database_snapshot(fd, budget) as (db, schema):
            if schema not in {7,8,9}: raise files.DiagnosticError('SCHEMA_UNSUPPORTED')
            guard = db.execute('SELECT restore_id,version,state FROM restore_guard WHERE id=1').fetchone()
            if not guard or guard['state'] != 'ACTIVE' or guard['restore_id'] != expected_restore_id or type(guard['version']) is not int or guard['version'] < 1:
                raise files.DiagnosticError('RESTORE_GUARD_MISMATCH')
            event = db.execute('SELECT backup_id,manifest_sha256,execution_epoch,scope,source_schema,backup_created_at FROM restore_events WHERE id=?', (expected_restore_id,)).fetchone()
            epoch = db.execute('SELECT execution_epoch FROM operations_state WHERE id=1').fetchone()
            if not event or not epoch or event['backup_id'] != expected_backup_id or event['manifest_sha256'] != expected_digest or event['scope'] != SCOPE or event['source_schema'] != manifest['schema_version'] or event['backup_created_at'] != manifest['created_at'] or event['execution_epoch'] != epoch[0]:
                raise files.DiagnosticError('RESTORE_SOURCE_MISMATCH')
            require(epoch[0], HEX32)
            result = {'format':FORMAT, 'observation_scope':'NON_ATOMIC_OBSERVATIONS', 'state':'OBSERVED_GUARDED',
                      'start_authorized':False, 'upload_allowed':False, 'backup_id':expected_backup_id,
                      'manifest_sha256':expected_digest, 'restore_id':expected_restore_id,
                      'execution_epoch':epoch[0], 'guard_version':guard['version'], 'scope':SCOPE,
                      'schema_version':schema, **entity}
        budget.check(); admission_observations(fd)
        files.same_directory(path, fd)
        if before != snapshot() or directory_identity(fd) != entity:
            raise files.DiagnosticError('STATE_CHANGED')
        return result
    finally: os.close(fd)
