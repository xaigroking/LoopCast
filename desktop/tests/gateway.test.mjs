// SPDX-License-Identifier: Apache-2.0
import test from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import { createHash } from 'node:crypto';
import { createGateway, apiRoute } from '../src/gateway.mjs';

const gatewayToken = 'a'.repeat(64), apiToken = 'b'.repeat(64);
async function listen(handler) {
  const server = http.createServer(handler);
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  return { server, origin: `http://127.0.0.1:${server.address().port}` };
}
function call(origin, path, { method = 'GET', headers = {}, body } = {}) {
  return new Promise((resolve, reject) => {
    const req = http.request(origin, { method, path, headers: { 'x-loopcast-desktop-session': gatewayToken, ...headers } }, response => {
      const chunks = []; response.on('data', chunk => chunks.push(chunk)); response.on('error', reject);
      response.on('end', () => resolve({ status: response.statusCode, headers: response.headers, body: Buffer.concat(chunks) }));
    });
    req.on('error', reject); req.end(body);
  });
}
async function setup(t, apiHandler) {
  const api = await listen(apiHandler ?? ((req, res) => { res.setHeader('content-type', 'application/json'); res.end(JSON.stringify(req.headers)); }));
  const web = await listen((req, res) => { res.setHeader('x-web-path', req.url); res.setHeader('x-token-leaked', String(Boolean(req.headers['x-loopcast-desktop-session']))); res.end('page'); });
  const gateway = await createGateway({ gatewayToken, apiToken });
  gateway.setTargets({ api: api.origin, web: web.origin });
  t.after(async () => { await gateway.close(); for (const item of [api, web]) { item.server.closeAllConnections(); await new Promise(resolve => item.server.close(resolve)); } });
  return { gateway, api, web };
}

test('fixed routes preserve dynamic pages and reject arbitrary upstreams', async t => {
  const { gateway } = await setup(t);
  for (const path of ['/v1', '/v1/runs/new-id', '/healthz', '/openapi.json']) {
    assert.equal(apiRoute(path), true); assert.equal((await call(gateway.origin, path)).headers['content-type'], 'application/json');
  }
  for (const path of ['/runs/new-after-build', '/create/new-id', '/v10', '/healthz/sub', '/openapi.json/sub']) {
    const result = await call(gateway.origin, path);
    assert.equal(result.headers['x-web-path'], path); assert.equal(result.headers['x-token-leaked'], 'false');
  }
  assert.throws(() => gateway.setTargets({ api: 'http://example.com:80', web: 'http://127.0.0.1:1234' }), /PROTOCOL_INVALID/);
});

test('capability, exact Host/Origin, cross-site and ambiguous path rejections', async t => {
  const { gateway } = await setup(t);
  for (const headers of [{ 'x-loopcast-desktop-session': '' }, { 'x-loopcast-desktop-session': 'wrong' }, { host: 'localhost:1' }, { 'sec-fetch-site': 'cross-site' }]) {
    assert.equal((await call(gateway.origin, '/v1', { headers })).status, 403);
  }
  for (const path of ['//example.com/v1', 'http://example.com/v1', '/v1/../other', '/v1%2fsecret', '/v1/%2e%2e/page', '/v1\\evil']) {
    assert.equal((await call(gateway.origin, path)).status, 403);
  }
  for (const origin of [undefined, 'http://localhost:1234', 'https://example.com']) {
    assert.equal((await call(gateway.origin, '/v1/runs', { method: 'POST', headers: origin ? { origin } : {} })).status, 403);
  }
  assert.equal((await call(gateway.origin, '/v1', { method: 'TRACE' })).status, 405);
});

test('only API receives its capability; forwarded spoofing and hop headers are removed', async t => {
  const { gateway } = await setup(t);
  const result = await call(gateway.origin, '/v1/runs', { method: 'POST', headers: {
    origin: gateway.origin, 'content-type': 'application/json', cookie: 'loopcast_owner=fake-stable-identity',
    'idempotency-key': 'same-write', forwarded: 'for=attacker', 'x-forwarded-host': 'attacker', 'x-forwarded-proto': 'https',
    'x-forwarded-for': 'attacker', connection: 'x-spoof', 'x-spoof': 'remove',
  }, body: '{}' });
  assert.equal(result.status, 200);
  const received = JSON.parse(result.body);
  assert.equal(received['x-loopcast-desktop-session'], apiToken);
  assert.equal(received.host, new URL(gateway.origin).host);
  assert.equal(received['x-forwarded-host'], received.host); assert.equal(received['x-forwarded-proto'], 'http');
  assert.equal(received['x-forwarded-for'], '127.0.0.1'); assert.equal(received.forwarded, undefined);
  assert.equal(received['x-spoof'], undefined); assert.equal(received['idempotency-key'], 'same-write');
  assert.equal(received.cookie, 'loopcast_owner=fake-stable-identity');
});

test('binary upload streams intact and media range/status/headers survive', async t => {
  const data = Buffer.alloc(2 * 1024 * 1024, 0x85);
  const digest = createHash('sha256').update(data).digest('hex');
  const { gateway } = await setup(t, (req, res) => {
    if (req.method === 'POST') {
      const hash = createHash('sha256'); req.on('data', chunk => hash.update(chunk));
      req.on('end', () => res.end(hash.digest('hex')));
    } else {
      assert.equal(req.headers.range, 'bytes=1-3');
      res.writeHead(206, { 'content-type': 'video/mp4', 'content-range': 'bytes 1-3/4', 'content-length': 3 });
      res.end(Buffer.from([0, 128, 255]));
    }
  });
  const upload = await call(gateway.origin, '/v1/studio/assets/import', { method: 'POST', headers: { origin: gateway.origin, 'content-type': 'application/octet-stream' }, body: data });
  assert.equal(upload.body.toString(), digest);
  const media = await call(gateway.origin, '/v1/assets/id/content', { headers: { range: 'bytes=1-3' } });
  assert.equal(media.status, 206); assert.equal(media.headers['content-range'], 'bytes 1-3/4');
  assert.deepEqual(media.body, Buffer.from([0, 128, 255]));
});

test('SSE arrives before completion and downstream disconnect cancels upstream', { timeout: 3000 }, async t => {
  let resolveClosed;
  const closed = new Promise(resolve => { resolveClosed = resolve; });
  const { gateway } = await setup(t, (_req, res) => {
    res.writeHead(200, { 'content-type': 'text/event-stream', 'cache-control': 'no-store' });
    res.write('id: event-1\ndata: ready\n\n');
    res.on('close', resolveClosed);
  });
  await new Promise((resolve, reject) => {
    const req = http.get(`${gateway.origin}/v1/events`, { headers: { 'x-loopcast-desktop-session': gatewayToken, 'last-event-id': 'event-0' } }, response => {
      response.once('data', chunk => { assert.match(chunk.toString(), /event-1/); response.destroy(); resolve(); });
    });
    req.on('error', reject);
  });
  await closed;
});

test('suspending the entry closes streams and rejects further writes', async t => {
  const { gateway } = await setup(t); gateway.suspend();
  assert.equal((await call(gateway.origin, '/v1/runs', { method: 'POST', headers: { origin: gateway.origin } })).status, 503);
});
