// SPDX-License-Identifier: Apache-2.0
// Requires an installed same-source wheel and an actual Next standalone build.
import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, mkdir, writeFile, readFile, rm, stat } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { prepare } from '../scripts/prepare.mjs';
import { readConfig, loadTreePort } from '../src/config.mjs';
import { DesktopHost } from '../src/host.mjs';
import { createTreePort } from '../src/linux-tree.mjs';
import { Budget } from '../src/errors.mjs';
import { capture } from '../src/protocol.mjs';

const python = process.env.LOOPCAST_DESKTOP_PYTHON;
if (!python) throw new Error('An installed Python candidate is required; this integration test cannot silently skip.');
async function workspace(t) {
  const root = await mkdtemp(join(tmpdir(), 'loopcast 真实 desktop '));
  const dataDir = join(root, '业务 data'), sessionDir = join(root, '窗口 session');
  await mkdir(dataDir, { mode: 0o700 }); await mkdir(sessionDir, { mode: 0o700 });
  t.after(() => rm(root, { recursive: true, force: true }));
  return { root, dataDir, sessionDir };
}
async function gone(pid) {
  for (let index = 0; index < 300; index++) {
    try { await stat(`/proc/${pid}`); } catch { return; }
    await new Promise(resolve => setTimeout(resolve, 10));
  }
  assert.fail('An owned descendant remained after complete-tree cleanup.');
}

test('production subreaper collects a real setsid descendant after its leader exits', async t => {
  const paths = await workspace(t), port = createTreePort({ python: { path: python }, ...paths }), budget = new Budget(8000);
  const env = await port.environment({ ...paths, deadline: budget.end });
  const script = 'import subprocess,sys; p=subprocess.Popen([sys.executable,"-c","import time; time.sleep(60)"],start_new_session=True); print(p.pid,flush=True)';
  const owned = port.launch({ executable: python, argv: ['-I', '-c', script], cwd: paths.sessionDir, env: env.api, stdio: ['pipe', 'pipe', 'pipe'], deadline: budget.end });
  const pid = Number((await capture(owned, budget)).trim());
  assert.ok(Number.isInteger(pid) && pid > 1); assert.equal((await owned.closeTree(budget.end)).confirmed, true); await gone(pid);
});

test('output limit terminates the real tree and closes inherited pipes', async t => {
  const paths = await workspace(t), port = createTreePort({ python: { path: python }, ...paths }), budget = new Budget(3000);
  const env = await port.environment({ ...paths, deadline: budget.end });
  const script = 'import sys,time; sys.stdout.write("x"*100000); sys.stdout.flush(); time.sleep(60)';
  const owned = port.launch({ executable: python, argv: ['-I', '-c', script], cwd: paths.sessionDir, env: env.api, stdio: ['pipe', 'pipe', 'pipe'], deadline: budget.end });
  await assert.rejects(capture(owned, budget, 1024), /PROTOCOL_INVALID/);
  assert.equal((await owned.closeTree(budget.end)).confirmed, true);
});

async function manifest(t) {
  const paths = await workspace(t);
  const value = await prepare({ python, node: process.execPath, next: process.env.LOOPCAST_DESKTOP_NEXT,
    data: paths.dataDir, session: paths.sessionDir, port: '28786', revision: process.env.LOOPCAST_DESKTOP_REVISION });
  value.configs.media = process.env.LOOPCAST_DESKTOP_MEDIA_CONFIG;
  const filename = join(paths.root, 'desktop.json'); await writeFile(filename, JSON.stringify(value), { mode: 0o600 });
  return { ...paths, filename, config: await readConfig(filename) };
}

test('same-source actual backend, Next, host and production port start/stop/restart with stable write identity', { timeout: 60000 }, async t => {
  const item = await manifest(t);
  async function start() { const host = new DesktopHost(item.config, await loadTreePort(item.config)); t.after(() => host.stop()); await host.start(); return host; }
  let host = await start(); const origin = host.gateway.origin;
  const auth = () => ({ 'X-LoopCast-Desktop-Session': host.gatewayToken });
  const page = await fetch(origin, { headers: auth() }); assert.equal(page.status, 200);
  assert.ok(page.headers.get('content-security-policy')?.includes("nonce-"));
  const health = await fetch(origin+'/healthz', { headers: auth() }); assert.equal(health.status, 200);
  const competitor = new DesktopHost(item.config, await loadTreePort(item.config));
  await assert.rejects(competitor.start()); await competitor.stop();
  assert.equal((await fetch(origin+'/healthz', { headers: auth() })).status, 200);
  assert.equal((await fetch(origin+'/v1/runs')).status, 403);
  const key = 'same-lost-response-write';
  const input = { title: 'Managed restart fixture', mode: 'fixture', scenario: 'success', delay_ms: 20 };
  const headers = { ...auth(), Origin: origin, 'Content-Type': 'application/json', 'Idempotency-Key': key };
  const first = await fetch(origin+'/v1/runs', { method: 'POST', headers, body: JSON.stringify(input) });
  assert.equal(first.status, 202); const result = await first.json();
  const cookie = first.headers.get('set-cookie').split(';')[0];
  const id = result.run_id ?? result.id ?? result.run?.id;
  assert.ok(typeof id === 'string');
  assert.equal((await fetch(origin+`/runs/${id}`, { headers: auth() })).status, 200);
  const events = await fetch(origin+`/v1/runs/${id}/events?after=${result.last_event_id}`, { headers: auth(), signal: AbortSignal.timeout(5000) });
  assert.equal(events.status, 200); assert.ok(events.headers.get('content-type').includes('text/event-stream'));
  const reader = events.body.getReader(); assert.ok((await reader.read()).value.length > 0); await reader.cancel();
  const media = await readFile(process.env.LOOPCAST_DESKTOP_SAMPLE);
  const imported = await fetch(origin+'/v1/studio/assets/import?kind=video', { method: 'POST',
    headers: { ...auth(), Origin: origin, 'Content-Type': 'application/octet-stream', 'Idempotency-Key': 'desktop-real-media', 'X-File-Name': 'sample.mp4', 'X-Source': 'Owned generated contract clip' }, body: media });
  assert.equal(imported.status, 200); const asset = await imported.json();
  const ranged = await fetch(origin+`/v1/studio/assets/${asset.id}/content`, { headers: { ...auth(), Range: 'bytes=0-15' } });
  assert.equal(ranged.status, 206); assert.ok(ranged.headers.get('content-range')?.startsWith('bytes 0-15/'));
  assert.deepEqual(Buffer.from(await ranged.arrayBuffer()), media.subarray(0, 16));
  assert.equal((await host.stop()).confirmed, true);
  await assert.rejects(stat(join(item.dataDir, 'desktop-runtime.guard.v1')));
  host = await start(); assert.equal(host.gateway.origin, origin);
  const retried = await fetch(origin+'/v1/runs', { method: 'POST', headers: { ...headers, ...auth(), Cookie: cookie }, body: JSON.stringify(input) });
  assert.equal(retried.status, 200); const again = await retried.json();
  assert.equal(again.run_id ?? again.id ?? again.run?.id, id);
  assert.equal((await host.stop()).confirmed, true);
});

test('killing a real host leaves a durable barrier that rejects the next host and ordinary CLI', { timeout: 45000 }, async t => {
  const item = await manifest(t);
  const child = spawn(process.execPath, [fileURLToPath(new URL('../scripts/run-local.mjs', import.meta.url)), item.filename], { env: {}, stdio: ['pipe', 'ignore', 'pipe', 'pipe'] });
  t.after(() => { if (child.exitCode === null && child.signalCode === null) child.kill('SIGKILL'); });
  let buffer = '';
  await new Budget(15000).run(new Promise((resolve, reject) => {
    child.stdio[3].on('data', chunk => { buffer += chunk; if (buffer.includes('\n')) resolve(); });
    child.once('exit', () => reject(new Error('Real host failed to become ready.'))); child.stderr.resume();
  }));
  const exited = new Promise(resolve => child.once('exit', resolve)); child.kill('SIGKILL'); await exited;
  assert.ok(await stat(join(item.dataDir, 'desktop-runtime.guard.v1')));
  const host = new DesktopHost(item.config, await loadTreePort(item.config));
  await assert.rejects(host.start()); await host.stop();
  const service = spawn(python, ['-I', '-m', 'loopcast', 'serve', '--port', '0', '--data-dir', item.dataDir], { env: {}, stdio: ['ignore', 'pipe', 'ignore'] });
  let output = ''; service.stdout.on('data', chunk => { output += chunk; });
  const code = await new Budget(10000).run(new Promise(resolve => service.once('exit', resolve)));
  assert.notEqual(code, 0); assert.equal(output.includes('READY'), false);
  assert.ok(await stat(join(item.dataDir, 'desktop-runtime.guard.v1')));
});
