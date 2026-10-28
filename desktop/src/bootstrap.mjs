// SPDX-License-Identifier: Apache-2.0
import { constants, openSync, closeSync, fstatSync, readSync, realpathSync, statSync, mkdirSync, lstatSync } from 'node:fs';
import { createHash } from 'node:crypto';
import { join, relative, isAbsolute, sep } from 'node:path';
import { validateConfig } from './config.mjs';
import { HostError } from './errors.mjs';

function privateDirectory(path) {
  const actual = realpathSync(path), info = statSync(actual);
  if (!info.isDirectory() || (process.getuid && (info.uid !== process.getuid() || (info.mode & 0o077)))) throw new HostError('DIRECTORY_UNSAFE');
  return actual;
}
const contains = (a, b) => { const path = relative(a, b); return path === '' || (!path.startsWith(`..${sep}`) && path !== '..' && !isAbsolute(path)); };

// Complete synchronously before Electron can emit ready. Full runtime hashing
// remains asynchronous and must verify this exact manifest snapshot.
export function readBootstrap(filename, { packaged = false, binding } = {}) {
  let handle;
  let missing = 'MANIFEST_MISSING';
  try {
    handle = openSync(filename, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK);
    const info = fstatSync(handle);
    if (!info.isFile() || info.nlink !== 1 || info.size > 65536 || (process.getuid && (info.uid !== process.getuid() || (info.mode & 0o077)))) throw new HostError('FILE_UNSAFE');
    const bytes = Buffer.alloc(65537), size = readSync(handle, bytes, 0, bytes.length, 0);
    if (size > 65536) throw new Error();
    const raw = bytes.subarray(0, size); let config = validateConfig(JSON.parse(raw.toString('utf8')));
    if (binding) {
      if (binding.source_digest !== createHash('sha256').update(raw).digest('hex') || !['ORIGINAL','RESTORED'].includes(binding.role)) throw new HostError('MANIFEST_CHANGED');
      config = validateConfig({ ...config, dataDir:binding.data_dir, sessionDir:binding.session_dir, gatewayPort:binding.origin_port,
        configs:binding.role === 'ORIGINAL' ? config.configs : {} });
    }
    if (packaged !== (config.mode === 'release')) throw new Error();
    missing = 'DIRECTORY_MISSING';
    const sessionDir = privateDirectory(config.sessionDir), dataDir = privateDirectory(config.dataDir);
    if (!binding) {
      try { lstatSync(join(dataDir,'restore-manifest.json')); throw new HostError('HANDOFF_UNCONFIRMED'); }
      catch (error) { if (error.code !== 'ENOENT') throw error; }
    }
    missing = 'RESOURCE_MISSING';
    const nextRoot = realpathSync(config.nextRoot);
    if (contains(dataDir, sessionDir) || contains(sessionDir, dataDir) || contains(nextRoot, sessionDir) || contains(sessionDir, nextRoot)) throw new HostError('PATH_UNSAFE');
    return { config, sessionDir, digest: createHash('sha256').update(raw).digest('hex') };
  } catch (error) {
    if (error instanceof HostError && error.code !== 'CONFIG_INVALID') throw error;
    throw new HostError(error?.code === 'ENOENT' ? missing : ['EACCES','EPERM'].includes(error?.code) ? 'PERMISSION_DENIED' : error?.code === 'ELOOP' ? 'PATH_UNSAFE' : 'MANIFEST_INVALID');
  }
  finally { if (handle !== undefined) closeSync(handle); }
}

export function instanceDirectory(appData) {
  const path = join(appData, 'LoopCast-instance');
  mkdirSync(path, { recursive: true, mode: 0o700 });
  return privateDirectory(path);
}
