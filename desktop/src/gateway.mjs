// SPDX-License-Identifier: Apache-2.0
import http from 'node:http';
import { timingSafeEqual } from 'node:crypto';
import { HostError } from './errors.mjs';

const sessionHeader = 'x-loopcast-desktop-session';
const hopHeaders = new Set(['connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization', 'te', 'trailer', 'transfer-encoding', 'upgrade', 'proxy-connection']);
function headersWithoutHop(headers) {
  const excluded = new Set([...hopHeaders, ...(headers.connection ?? '').toLowerCase().split(',').map(s => s.trim())]);
  return Object.fromEntries(Object.entries(headers).filter(([key]) => !excluded.has(key.toLowerCase())));
}
function authenticated(actual, expected) {
  if (typeof actual !== 'string' || actual.length !== expected.length) return false;
  const value = Buffer.from(actual);
  const secret = Buffer.from(expected);
  return value.length === secret.length && timingSafeEqual(value, secret);
}
export function apiRoute(path) {
  // Same route table as deploy/Caddyfile.example, including exact health/openapi.
  return path === '/v1' || path.startsWith('/v1/') || path === '/healthz' || path === '/openapi.json';
}
function requestPath(raw) {
  if (!raw?.startsWith('/') || raw.startsWith('//') || /[\\\x00-\x20\x7f]/.test(raw) || /%(?:2f|5c|2e|0[0-9a-f]|1[0-9a-f]|7f)/i.test(raw.split('?')[0])) return null;
  const path = raw.split('?')[0];
  if (new URL(raw, 'http://127.0.0.1').pathname !== path) return null;
  return path;
}
const loopback = origin => /^http:\/\/127\.0\.0\.1:[1-9][0-9]{0,4}$/.test(origin) && Number(new URL(origin).port) <= 65535;

export async function createGateway({ gatewayToken, apiToken, port = 0 }) {
  let targets, accepting = true, origin;
  const sockets = new Set(), upstreams = new Set();
  const server = http.createServer({ maxHeaderSize: 16384 }, (request, response) => {
    const reject = (status) => { response.writeHead(status, { 'content-type': 'text/plain', 'cache-control': 'no-store', connection: 'close' }); response.end('Local request unavailable.'); };
    if (!accepting || !targets) { reject(503); return; }
    const path = requestPath(request.url);
    if (!path || request.headers.host !== new URL(origin).host || !authenticated(request.headers[sessionHeader], gatewayToken)) { reject(403); return; }
    if (!['GET', 'HEAD', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS'].includes(request.method)) { reject(405); return; }
    if (request.headers['sec-fetch-site'] === 'cross-site' ||
        (!['GET', 'HEAD', 'OPTIONS'].includes(request.method) && request.headers.origin !== origin)) { reject(403); return; }
    const api = apiRoute(path);
    const target = new URL(api ? targets.api : targets.web);
    const headers = headersWithoutHop(request.headers);
    for (const key of Object.keys(headers)) {
      if (key.startsWith('x-loopcast-desktop-') || key === 'forwarded' || key.startsWith('x-forwarded-') || key.startsWith('proxy-')) delete headers[key];
    }
    headers.host = api ? new URL(origin).host : target.host;
    headers['x-forwarded-host'] = new URL(origin).host;
    headers['x-forwarded-proto'] = 'http';
    headers['x-forwarded-for'] = '127.0.0.1';
    if (api) headers[sessionHeader] = apiToken;
    const upstream = http.request({ hostname: '127.0.0.1', port: target.port, method: request.method, path: request.url, headers, agent: false }, incoming => {
      const outputHeaders = headersWithoutHop(incoming.headers);
      response.writeHead(incoming.statusCode, outputHeaders);
      incoming.pipe(response); // Streams uploads, SSE and media with backpressure.
      incoming.on('error', () => response.destroy());
    });
    upstreams.add(upstream);
    upstream.on('close', () => upstreams.delete(upstream));
    upstream.on('error', () => { if (!response.headersSent) reject(502); else response.destroy(); });
    request.on('aborted', () => upstream.destroy());
    response.on('close', () => upstream.destroy());
    request.pipe(upstream);
  });
  server.on('connection', socket => { sockets.add(socket); socket.on('close', () => sockets.delete(socket)); });
  server.on('upgrade', (_request, socket) => socket.destroy());
  server.on('connect', (_request, socket) => socket.destroy());
  server.on('clientError', (_error, socket) => socket.destroy());
  server.requestTimeout = 0; // Long SSE/media responses use component lifecycle cancellation.
  server.headersTimeout = 10000;
  await new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen({ host: '127.0.0.1', port, exclusive: true }, resolve);
  });
  origin = `http://127.0.0.1:${server.address().port}`;
  return {
    origin,
    setTargets(value) {
      if (!loopback(value.api) || !loopback(value.web)) throw new HostError('PROTOCOL_INVALID');
      targets = Object.freeze({ ...value });
    },
    suspend() { accepting = false; for (const upstream of upstreams) upstream.destroy(); for (const socket of sockets) socket.destroy(); },
    async close() {
      accepting = false;
      for (const upstream of upstreams) upstream.destroy();
      for (const socket of sockets) socket.destroy();
      await new Promise(resolve => server.close(resolve));
    },
  };
}
