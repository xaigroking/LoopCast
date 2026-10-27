// SPDX-License-Identifier: Apache-2.0
import { constants, openSync, closeSync, fstatSync, readSync, realpathSync, statSync, mkdirSync } from 'node:fs';
import { createHash } from 'node:crypto';
import { join, relative, isAbsolute, sep } from 'node:path';
import { validateConfig } from './config.mjs';
import { HostError } from './errors.mjs';

function privateDirectory(path) {
  const actual = realpathSync(path), info = statSync(actual);
  if (!info.isDirectory() || (process.getuid && (info.uid !== process.getuid() || (info.mode & 0o077)))) throw new Error();
  return actual;
}
const contains = (a, b) => { const path = relative(a, b); return path === '' || (!path.startsWith(`..${sep}`) && path !== '..' && !isAbsolute(path)); };

// Complete synchronously before Electron can emit ready. Full runtime hashing
// remains asynchronous and must verify this exact manifest snapshot.
export function readBootstrap(filename, { packaged = false } = {}) {
  let handle;
  try {
    handle = openSync(filename, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK);
    const info = fstatSync(handle);
    if (!info.isFile() || info.nlink !== 1 || info.size > 65536 || (process.getuid && (info.uid !== process.getuid() || (info.mode & 0o077)))) throw new Error();
    const bytes = Buffer.alloc(65537), size = readSync(handle, bytes, 0, bytes.length, 0);
    if (size > 65536) throw new Error();
    const raw = bytes.subarray(0, size), config = validateConfig(JSON.parse(raw.toString('utf8')));
    if (packaged !== (config.mode === 'release')) throw new Error();
    const sessionDir = privateDirectory(config.sessionDir), dataDir = privateDirectory(config.dataDir), nextRoot = realpathSync(config.nextRoot);
    if (contains(dataDir, sessionDir) || contains(sessionDir, dataDir) || contains(nextRoot, sessionDir) || contains(sessionDir, nextRoot)) throw new Error();
    return { config, sessionDir, digest: createHash('sha256').update(raw).digest('hex') };
  } catch { throw new HostError('CONFIG_INVALID'); }
  finally { if (handle !== undefined) closeSync(handle); }
}

export function instanceDirectory(appData) {
  const path = join(appData, 'LoopCast-instance');
  mkdirSync(path, { recursive: true, mode: 0o700 });
  return privateDirectory(path);
}
