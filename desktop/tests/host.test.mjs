// SPDX-License-Identifier: Apache-2.0
import test from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import { DesktopHost } from '../src/host.mjs';
import { Budget, HostError, message } from '../src/errors.mjs';
import { config, fixturePort } from './fixture-port.mjs';

test('independent children: ordered readiness, one owner, repeated start/stop and private capability', async () => {
  process.env.DESKTOP_SECRET_CANARY = 'must not reach children';
  const port = fixturePort(), host = new DesktopHost(config(), port), states = [];
  host.on('state', state => states.push(state));
  try {
    const first = host.start(); assert.equal(host.start(), first);
    const origin = await first;
    assert.equal(host.state, 'READY'); assert.equal(await host.start(), origin);
    assert.equal(port.records.filter(r => r.role === 'api').length, 1);
    for (const { spec } of port.records) {
      assert.equal(spec.env.DESKTOP_SECRET_CANARY, undefined);
      assert.equal(JSON.stringify(spec).includes(host.apiToken), false);
      assert.equal(JSON.stringify(spec).includes(host.gatewayToken), false);
    }
    const stop = host.stop(); assert.equal(host.stop(), stop);
    assert.deepEqual(await stop, { confirmed: true });
    assert.deepEqual(states, ['STARTING', 'READY', 'STOPPING', 'STOPPED']);
    assert.ok(port.records.every(r => r.status === 0));
  } finally { delete process.env.DESKTOP_SECRET_CANARY; await host.stop(); }
});

for (const behavior of ['malformed', 'duplicate', 'flood', 'early-exit', 'timeout']) {
  test(`startup rejects ${behavior} and cleans only its children`, async () => {
    const port = fixturePort({ api: behavior }), host = new DesktopHost(config({ startTimeoutMs: 700 }), port);
    await assert.rejects(host.start(), HostError);
    await host.stop();
    assert.ok(port.records.every(r => r.status !== undefined));
    assert.equal(port.records.some(r => r.role === 'web'), false);
    assert.equal(host.state === 'READY', false);
  });
}

test('a service claiming healthy without this child session proof is rejected', async () => {
  const port = fixturePort({ api: 'wrong-health-proof' }), host = new DesktopHost(config(), port);
  await assert.rejects(host.start(), /START_FAILED/);
  assert.equal(port.records.some(r => r.role === 'web'), false);
  assert.ok(port.records.every(r => r.status !== undefined));
});

test('a syntactically valid READY for a stranger port cannot receive the bearer or pass health', async () => {
  let bearer, contacted = false;
  const stranger = http.createServer((req, res) => {
    contacted = true; bearer = req.headers['x-loopcast-desktop-session'];
    res.end(JSON.stringify({ ready: true }));
  });
  await new Promise(resolve => stranger.listen(0, '127.0.0.1', resolve));
  const port = fixturePort({ api: `stranger-${stranger.address().port}` }), host = new DesktopHost(config(), port);
  try {
    await assert.rejects(host.start(), /START_FAILED/);
    assert.equal(contacted, true); assert.equal(bearer, undefined);
    assert.equal(port.records.some(r => r.role === 'web'), false);
  } finally { await host.stop(); await new Promise(resolve => stranger.close(resolve)); }
});

for (const options of [{ wrongNode: true }, { info: 'wrong-revision' }, { info: 'unsupported' }]) {
  test(`resource or capability mismatch ${JSON.stringify(options)} does not launch owner`, async () => {
    const port = fixturePort(options), host = new DesktopHost(config(), port);
    await assert.rejects(host.start(), HostError);
    assert.equal(port.records.some(r => r.role === 'api'), false);
    assert.equal(host.state, 'STOPPED');
  });
}

test('exit during startup aborts the shared operation and does not launch later children', async () => {
  let resolve;
  const port = fixturePort({ environment: new Promise(r => { resolve = r; }) });
  const host = new DesktopHost(config(), port);
  const started = host.start();
  const rejected = assert.rejects(started, HostError);
  assert.deepEqual(await host.stop(), { confirmed: true });
  resolve(); await rejected;
  assert.equal(port.records.length, 0); assert.equal(host.state, 'STOPPED');
});

for (const options of [{ api: 'ignore-stop' }, { api: 'no-stopped' }, { unconfirmedTree: true }]) {
  test(`unconfirmed shutdown blocks replacement ${JSON.stringify(options)}`, async () => {
    const port = fixturePort(options), host = new DesktopHost(config(), port);
    if (options.unconfirmedTree) await assert.rejects(host.start(), HostError);
    else await host.start();
    const before = port.records.length;
    assert.equal((await host.stop()).confirmed, false);
    assert.equal(host.state, 'STOPPING');
    await assert.rejects(host.start(), /STOP_UNCONFIRMED/);
    assert.equal(port.records.length, before);
    assert.equal(host.error.code, 'STOP_UNCONFIRMED');
  });
}

test('runtime component crash shuts down the combination without restarting', async () => {
  const port = fixturePort({ web: 'crash' }), host = new DesktopHost(config(), port);
  await host.start();
  await new Promise(resolve => host.once('fault', resolve));
  assert.equal((await host.stop()).confirmed, true);
  assert.equal(port.records.filter(r => r.role === 'api').length, 1);
  assert.equal(host.error.code, 'COMPONENT_EXITED');
});

test('an occupied stable entry port fails rather than attaching to a stranger', async () => {
  let contacted = false;
  const stranger = http.createServer((_req, res) => { contacted = true; res.end('stranger'); });
  await new Promise(resolve => stranger.listen(0, '127.0.0.1', resolve));
  const port = fixturePort(), host = new DesktopHost(config({ gatewayPort: stranger.address().port }), port);
  try {
    await assert.rejects(host.start(), HostError);
    assert.equal(contacted, false); assert.equal(port.records.some(r => r.role === 'api'), false);
  } finally { await new Promise(resolve => stranger.close(resolve)); }
});

test('monotonic budget and safe localized messages contain no raw diagnostic output', () => {
  let mono = 100;
  const budget = new Budget(50, undefined, () => mono);
  assert.equal(budget.remaining(), 50); mono = 151;
  assert.throws(() => budget.remaining(), /START_TIMEOUT/);
  assert.match(message('STOP_UNCONFIRMED', 'zh-Hans'), /尚未确认/);
  assert.equal(message('START_FAILED', 'ar'), message('START_FAILED', 'en'));
});

test('an already resolved late result cannot bypass a monotonic deadline', async () => {
  let mono = 0;
  const budget = new Budget(10, undefined, () => mono);
  const result = Promise.resolve().then(() => { mono = 11; return 'late success'; });
  await assert.rejects(budget.run(result), /START_TIMEOUT/);
  await assert.rejects(budget.run(Promise.resolve('already late')), /START_TIMEOUT/);
  await assert.rejects(budget.run(Promise.reject(new HostError('START_FAILED'))), /START_TIMEOUT/);
  const port = fixturePort(), host = new DesktopHost(config(), port, { startupDeadline: performance.now() - 1 });
  await assert.rejects(host.start(), /START_TIMEOUT/);
  assert.equal(port.records.length, 0);
});
