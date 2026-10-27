// SPDX-License-Identifier: Apache-2.0
import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, mkdir, writeFile, chmod, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { readBootstrap, instanceDirectory } from '../src/bootstrap.mjs';
import { readConfig } from '../src/config.mjs';

test('synchronous private session snapshot is bound to subsequent full validation', async t => {
  const root = await mkdtemp(join(tmpdir(), 'loopcast 启动 ')); t.after(() => rm(root, { recursive: true, force: true }));
  for (const name of ['data', 'session', 'next']) await mkdir(join(root, name), { mode: 0o700 });
  const file = join(root, 'config.json');
  const value = { format: 'loopcast.desktop.v1', mode: 'development', locale: 'en',
    hostRevision: 'a'.repeat(40), apiRevision: 'a'.repeat(40), webRevision: 'a'.repeat(40),
    nodeVersion: '24.22.1', gatewayPort: 18786, startTimeoutMs: 1000, stopTimeoutMs: 1000,
    dataDir: join(root, 'data'), sessionDir: join(root, 'session'), nextRoot: join(root, 'next'),
    ...Object.fromEntries(['python', 'node', 'treeModule'].map(key => [key, { path: '/trusted/resource', sha256: 'a'.repeat(64) }])) };
  await writeFile(file, JSON.stringify(value), { mode: 0o600 });
  const snapshot = readBootstrap(file);
  assert.equal(snapshot.sessionDir, value.sessionDir);
  await writeFile(file, JSON.stringify({ ...value, locale: 'zh-Hans' }));
  await assert.rejects(readConfig(file, { expectedDigest: snapshot.digest }), /MANIFEST_CHANGED/);
  await chmod(value.sessionDir, 0o755); assert.throws(() => readBootstrap(file), /DIRECTORY_UNSAFE/);
  assert.equal(instanceDirectory(root), join(root, 'LoopCast-instance'));
});
