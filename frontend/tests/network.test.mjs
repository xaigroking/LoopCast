import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createServer } from 'node:http';
import { readFile } from 'node:fs/promises';
import ts from 'typescript';

const urls = new Map();
async function moduleURL(name) {
  if (urls.has(name)) return urls.get(name);
  const source = await readFile(new URL(`../src/lib/${name}.ts`, import.meta.url), 'utf8');
  let { outputText } = ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 } });
  if (outputText.includes("'./http'")) outputText = outputText.replace("'./http'", JSON.stringify(await moduleURL('http')));
  const url = 'data:text/javascript;base64,' + Buffer.from(outputText).toString('base64');
  urls.set(name, url); return url;
}
async function load(name) { return import(await moduleURL(name)); }
const { requestJSON, TransportError } = await load('http');
const { apiRequest, ApiError } = await load('api');
const { subscribeEvents } = await load('events');
const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
async function until(predicate) {
  const deadline = performance.now() + 2000;
  while (!predicate()) { assert.ok(performance.now() < deadline, 'condition timed out'); await pause(5); }
}
async function server(t, handler) {
  const instance = createServer(handler);
  await new Promise((resolve) => instance.listen(0, '127.0.0.1', resolve));
  t.after(async () => { instance.closeAllConnections(); await new Promise((resolve) => instance.close(resolve)); });
  return `http://127.0.0.1:${instance.address().port}`;
}
const policy = { totalMs: 500, attemptMs: 100, attempts: 3, backoffMs: 5 };

test('safe reads retry transient errors finitely; permanent errors are returned once', async (t) => {
  let calls = 0;
  const url = await server(t, (req, res) => { calls++; res.writeHead(calls < 3 ? 503 : 200, { 'Content-Type': 'application/json' }); res.end('{"ok":true}'); });
  const { response, data } = await requestJSON(url, {}, policy);
  assert.equal(response.status, 200); assert.equal(data.ok, true); assert.equal(calls, 3);
  calls = 0;
  const permanent = await server(t, (req, res) => { calls++; res.writeHead(409); res.end('{"error":{"code":"VERSION_CONFLICT"}}'); });
  assert.equal((await requestJSON(permanent, {}, policy)).response.status, 409); assert.equal(calls, 1);
});

test('unknown write outcomes never replay, including dispatch with an idempotency key', async (t) => {
  let calls = 0;
  const url = await server(t, (req, res) => { calls++; req.socket.destroy(); });
  await assert.rejects(requestJSON(url + '/dispatch', { method: 'POST', headers: { 'Idempotency-Key': 'fixed' }, body: '{}' }, policy), (error) => error instanceof TransportError && error.code === 'NETWORK_ERROR');
  assert.equal(calls, 1);
});

test('ambiguous write responses retain transport semantics; conflicts retain the support request ID', async (t) => {
  for (const status of [200, 500]) {
    let calls = 0;
    const url = await server(t, (req, res) => { calls++; res.writeHead(status); res.end(status === 200 ? '{' : '{"error":{"code":"INTERNAL_ERROR"}}'); });
    await assert.rejects(apiRequest(url, { method: 'POST', body: '{}' }), (error) => error instanceof TransportError);
    assert.equal(calls, 1);
  }
  const url = await server(t, (req, res) => { res.writeHead(409); res.end('{"error":{"code":"VERSION_CONFLICT","request_id":"support-id"}}'); });
  await assert.rejects(apiRequest(url, { method: 'POST', body: '{}' }), (error) => error instanceof ApiError && error.code === 'VERSION_CONFLICT' && error.requestId === 'support-id');
});

test('headers and body stalls share a total deadline', async (t) => {
  for (const bodyStarted of [false, true]) {
    let calls = 0;
    const url = await server(t, (req, res) => { calls++; if (bodyStarted) { res.writeHead(200, { 'Content-Type': 'application/json' }); res.write('{'); } });
    const started = performance.now();
    await assert.rejects(requestJSON(url, {}, { ...policy, totalMs: 130, attemptMs: 45 }), (error) => error.code === 'REQUEST_TIMEOUT');
    assert.ok(performance.now() - started < 1000); assert.ok(calls >= 1 && calls <= 3);
  }
});

test('cancellation aborts a live read and stops all retries', async (t) => {
  let calls = 0;
  const url = await server(t, () => { calls++; });
  const controller = new AbortController();
  const pending = requestJSON(url, { signal: controller.signal }, policy);
  await until(() => calls === 1); controller.abort();
  await assert.rejects(pending, (error) => error.name === 'AbortError');
  await pause(30); assert.equal(calls, 1);
});

function streams(extra = {}) {
  const sources = [], states = [], urls = [];
  let cursor = 1;
  const subscription = subscribeEvents({
    url: () => `/events?after=${cursor}`, idleMs: 35, reconnectMs: 2, reconnectBudgetMs: 150, maxReconnects: 2,
    state: (value) => states.push(value), receive: (event) => { cursor = Number(event.data); return cursor === 99; },
    source: (url) => {
      urls.push(url);
      const source = { closed: false, listeners: {}, onerror: null, onopen: null,
        addEventListener(name, callback) { this.listeners[name] = callback; },
        close() { this.closed = true; },
        emit(name, data) { this.listeners[name]?.({ data: String(data), lastEventId: String(data) }); },
      };
      sources.push(source); return source;
    }, ...extra,
  });
  return { subscription, sources, states, urls };
}

test('SSE reconnect resumes the accepted cursor and closes each failed source', async () => {
  const { subscription, sources, states, urls } = streams();
  try {
    sources[0].emit('snapshot', 7); sources[0].onerror();
    await until(() => sources.length === 2);
    assert.ok(sources[0].closed); assert.equal(urls[1], '/events?after=7');
    sources[1].emit('complete', 99);
    assert.ok(sources[1].closed); assert.equal(states.at(-1), 'settled');
    await pause(50); assert.equal(sources.length, 2);
  } finally { subscription.close(); }
});

test('idle SSE is detected and reconnect attempts stop at the budget', async () => {
  const { subscription, sources, states } = streams({ idleMs: 10 });
  try {
    await until(() => states.at(-1) === 'requestError');
    assert.equal(sources.length, 3); assert.ok(sources.every((source) => source.closed));
    await pause(30); assert.equal(sources.length, 3);
  } finally { subscription.close(); }
});

test('healthy heartbeats keep a stream open past the reconnect budget', async () => {
  const { subscription, sources, states } = streams({ reconnectBudgetMs: 25, idleMs: 30 });
  try {
    for (let n = 0; n < 8; n++) { await pause(10); sources[0].emit('heartbeat', {}); }
    assert.equal(sources.length, 1); assert.equal(sources[0].closed, false); assert.equal(states.at(-1), 'live');
  } finally { subscription.close(); }
});

test('opening without application traffic cannot evade the outage deadline', async () => {
  const { subscription, sources, states } = streams({ idleMs: 12, reconnectBudgetMs: 25, maxReconnects: 100 });
  try {
    await until(() => states.at(-1) === 'requestError');
    assert.ok(sources.every((source) => source.closed)); assert.ok(sources.length <= 4);
  } finally { subscription.close(); }
});

test('closing during backoff cancels reconnection and ignores stale events', async () => {
  const { subscription, sources, states } = streams({ reconnectMs: 50 });
  sources[0].onerror(); subscription.close();
  sources[0].emit('snapshot', 99);
  await pause(80); assert.equal(sources.length, 1); assert.equal(states.at(-1), 'offline');
});

test('invalid event processing stops instead of entering a reconnect loop', async () => {
  const { subscription, sources, states } = streams({ receive: () => { throw Error('invalid'); } });
  sources[0].emit('snapshot', 1);
  assert.equal(states.at(-1), 'requestError'); assert.ok(sources[0].closed);
  await pause(40); assert.equal(sources.length, 1); subscription.close();
});
