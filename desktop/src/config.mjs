// SPDX-License-Identifier: Apache-2.0
import { readFile, realpath, stat } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import { isAbsolute, join, relative, resolve, sep } from 'node:path';
import { pathToFileURL } from 'node:url';
import { HostError } from './errors.mjs';

const sha = /^[a-f0-9]{64}$/;
const revision = /^[a-f0-9]{40}$/;
const absolute = value => typeof value === 'string' && isAbsolute(value) && !value.includes('\0');
export function validateConfig(value) {
  if (!value || value.format !== 'loopcast.desktop.v1' ||
      !['development', 'release'].includes(value.mode) ||
      !revision.test(value.apiRevision) || !revision.test(value.webRevision) ||
      !revision.test(value.hostRevision) || value.nodeVersion !== '24.22.1' ||
      !['en', 'zh-Hans'].includes(value.locale) ||
      !Number.isInteger(value.gatewayPort) || value.gatewayPort < 1 || value.gatewayPort > 65535 ||
      !['python', 'node', 'treeModule'].every(k => absolute(value[k]?.path) && sha.test(value[k]?.sha256)) ||
      !['nextRoot', 'dataDir', 'sessionDir'].every(k => absolute(value[k])) ||
      !['generator', 'media', 'social', 'schedule', 'inspection'].every(k => value.configs?.[k] === undefined || absolute(value.configs[k])) ||
      !Number.isInteger(value.startTimeoutMs) || value.startTimeoutMs < 100 || value.startTimeoutMs > 120000 ||
      !Number.isInteger(value.stopTimeoutMs) || value.stopTimeoutMs < 100 || value.stopTimeoutMs > 60000) {
    throw new HostError('CONFIG_INVALID');
  }
  return value;
}

const nested = (a, b) => { const p = relative(a, b); return p === '' || (!p.startsWith(`..${sep}`) && p !== '..' && !isAbsolute(p)); };
export async function readConfig(filename, { packaged = false } = {}) {
  try {
    if (!absolute(filename) || (await stat(filename)).size > 65536) throw new Error();
    const config = validateConfig(JSON.parse(await readFile(filename, 'utf8')));
    if (packaged !== (config.mode === 'release')) throw new Error();
    // The manifest is trusted main-process input, never renderer input. Resolve
    // resource identities before hashing; the platform port validates data ACLs.
    for (const key of ['python', 'node', 'treeModule']) {
      const item = config[key]; item.path = await realpath(item.path);
      if (!(await stat(item.path)).isFile()) throw new Error();
      const hash = createHash('sha256');
      const { createReadStream } = await import('node:fs');
      for await (const chunk of createReadStream(item.path)) hash.update(chunk);
      if (hash.digest('hex') !== item.sha256) throw new Error();
    }
    for (const key of ['nextRoot', 'dataDir', 'sessionDir']) {
      config[key] = await realpath(config[key]);
      if (!(await stat(config[key])).isDirectory()) throw new Error();
    }
    if (nested(config.dataDir, config.sessionDir) || nested(config.sessionDir, config.dataDir) ||
        nested(config.nextRoot, config.dataDir) || nested(config.nextRoot, config.sessionDir) ||
        nested(config.dataDir, config.nextRoot) || nested(config.sessionDir, config.nextRoot)) throw new Error();
    for (const path of ['server.js', '.next/BUILD_ID']) if (!(await stat(join(config.nextRoot, path))).isFile()) throw new Error();
    for (const path of ['.next/static', 'public']) if (!(await stat(join(config.nextRoot, path))).isDirectory()) throw new Error();
    if ((await readFile(join(config.nextRoot, '.next/BUILD_ID'), 'utf8')).trim() !== config.webRevision) {
      throw new HostError('VERSION_MISMATCH');
    }
    return config;
  } catch (error) {
    if (error instanceof HostError) throw error;
    throw new HostError('CONFIG_INVALID');
  }
}

export async function loadTreePort(config) {
  try {
    const { treePort } = await import(pathToFileURL(config.treeModule.path).href);
    if (treePort?.format !== 'loopcast.process-tree.v1' ||
        typeof treePort.launch !== 'function' || typeof treePort.environment !== 'function') throw new Error();
    return treePort;
  } catch { throw new HostError('PLATFORM_UNAVAILABLE'); }
}

export function configPath(argv, { packaged, resourcesPath }) {
  if (packaged) return join(resourcesPath, 'loopcast', 'desktop.json');
  const offset = argv.indexOf('--desktop-config');
  if (offset < 0 || !absolute(argv[offset + 1])) throw new HostError('CONFIG_INVALID');
  return resolve(argv[offset + 1]);
}
