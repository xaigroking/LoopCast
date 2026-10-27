// SPDX-License-Identifier: Apache-2.0
import test from 'node:test';
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { mkdtemp, mkdir, writeFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { readiness, observeExit } from '../src/protocol.mjs';
import { Budget } from '../src/errors.mjs';

test('standalone wrapper reports the bound loopback port on its private pipe and handles STOP', async t => {
  const root = await mkdtemp(join(tmpdir(), 'loopcast Next 中文 '));
  t.after(() => rm(root, { recursive: true, force: true }));
  await mkdir(join(root, '.next'));
  const revision = 'a'.repeat(40); await writeFile(join(root, '.next/BUILD_ID'), revision);
  // This is a generated-server substitute, not an actual built Next candidate.
  await writeFile(join(root, 'server.js'), 'require("node:http").createServer((req,res)=>res.end("page")).listen(3000,"0.0.0.0");');
  const child = spawn(process.execPath, [fileURLToPath(new URL('../src/next-child.cjs', import.meta.url)), root, revision], { env: {}, stdio: ['pipe', 'ignore', 'ignore', 'pipe'] });
  t.after(() => { if (child.exitCode === null) child.kill(); });
  const protocol = readiness(child.stdio[3]), exit = observeExit(child), budget = new Budget(3000);
  const origin = await budget.run(protocol.ready);
  assert.match(origin, /^http:\/\/127\.0\.0\.1:\d+$/);
  assert.equal(await (await fetch(origin)).text(), 'page');
  child.stdin.end('STOP\n');
  assert.equal((await budget.run(exit.exited)).code, 0);
});
