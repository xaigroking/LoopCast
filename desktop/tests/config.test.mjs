// SPDX-License-Identifier: Apache-2.0
import test from 'node:test';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { mkdtemp, mkdir, writeFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { configPath, loadTreePort, readConfig, validateConfig } from '../src/config.mjs';
import { config } from './fixture-port.mjs';

async function resources(t) {
  const root = await mkdtemp(join(tmpdir(), 'loopcast 桌面 paths '));
  t.after(() => rm(root, { recursive: true, force: true }));
  const value = config({ gatewayPort: 18786, sessionDir: join(root, '窗口 会话'), dataDir: join(root, '业务 数据'), nextRoot: join(root, 'Next 资源') });
  for (const path of [value.sessionDir, value.dataDir, join(value.nextRoot, '.next/static'), join(value.nextRoot, 'public')]) await mkdir(path, { recursive: true });
  await writeFile(join(value.nextRoot, '.next/BUILD_ID'), value.webRevision);
  await writeFile(join(value.nextRoot, 'server.js'), '// test resource');
  for (const key of ['python', 'node', 'treeModule']) {
    const content = key === 'treeModule' ? 'export const treePort = { format: "loopcast.process-tree.v1", launch() {}, async environment() {} };' : 'test bytes';
    const path = join(root, key === 'treeModule' ? '平台 端口.mjs' : key);
    await writeFile(path, content);
    value[key] = { path, sha256: createHash('sha256').update(content).digest('hex') };
  }
  const filename = join(root, 'desktop.json');
  async function save() { await writeFile(filename, JSON.stringify(value)); }
  await save(); return { value, filename, save };
}
test('trusted manifests accept Chinese/space paths, verify digests and load the declared port contract', async t => {
  const { filename } = await resources(t);
  const parsed = await readConfig(filename);
  assert.equal((await loadTreePort(parsed)).format, 'loopcast.process-tree.v1');
  assert.equal(configPath(['electron', '.', '--desktop-config', filename], { packaged: false }), filename);
  assert.equal(configPath(['--desktop-config', '/ignored'], { packaged: true, resourcesPath: '/resources' }), '/resources/loopcast/desktop.json');
});
test('PATH tools, unfixed origins, missing hashes and arbitrary config shapes fail closed', async t => {
  const { value } = await resources(t);
  for (const change of [{ node: { path: 'node', sha256: '0'.repeat(64) } }, { gatewayPort: 0 }, { nodeVersion: 'latest' }, { treeModule: { path: '/port.mjs' } }, { startTimeoutMs: Infinity }, { locale: 'ar' }]) {
    assert.throws(() => validateConfig({ ...value, ...change }), /CONFIG_INVALID/);
  }
  assert.throws(() => configPath(['--desktop-config', 'relative.json'], { packaged: false }), /PATH_UNSAFE/);
});
test('changed resources, wrong build IDs and release/development mismatch are rejected', async t => {
  const { value, filename } = await resources(t);
  await assert.rejects(readConfig(filename, { packaged: true }), /CONFIG_INVALID/);
  await writeFile(join(value.nextRoot, '.next/BUILD_ID'), 'wrong');
  await assert.rejects(readConfig(filename), /VERSION_MISMATCH/);
  await writeFile(join(value.nextRoot, '.next/BUILD_ID'), value.webRevision);
  await writeFile(value.node.path, 'tampered');
  await assert.rejects(readConfig(filename), /RESOURCE_DIGEST_MISMATCH/);
});
test('window cache/session storage cannot overlap business data or Next resources', async t => {
  const { value, filename, save } = await resources(t);
  value.sessionDir = value.dataDir; await save();
  await assert.rejects(readConfig(filename), /CONFIG_INVALID/);
});

test('missing production components fail before any process is started', async t => {
  const { value, filename } = await resources(t);
  await rm(join(value.nextRoot, 'server.js'));
  await assert.rejects(readConfig(filename), /RESOURCE_MISSING/);
});
