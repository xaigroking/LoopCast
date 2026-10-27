// SPDX-License-Identifier: Apache-2.0
// Independent subprocess fixture. It has no descendants, business store or sender.
import http from 'node:http';
import { writeSync } from 'node:fs';
import { equalProof, healthProof } from '../src/authentication.mjs';
const [mode, behavior = 'normal', revision = 'a'.repeat(40)] = process.argv.slice(2);
if (process.env.DESKTOP_SECRET_CANARY) process.exit(90);
if (mode === 'info') {
  console.log(JSON.stringify({ format: 'loopcast.platform.v1', source_revision: behavior === 'wrong-revision' ? 'b'.repeat(40) : revision,
    capabilities: Object.fromEntries(['file_access', 'owner_lock', 'sqlite_access', 'process_tree', 'managed_stop', 'desktop_session'].map(k => [k, behavior === 'unsupported' ? 'unsupported' : 'implemented'])) }));
} else if (mode === 'version') console.log('v0.0.0');
else {
  let server, token, stopped = false, input = '';
  function stop() {
    if (behavior === 'ignore-stop' || stopped) return;
    stopped = true;
    const finish = () => { if (mode === 'api' && behavior !== 'no-stopped') writeSync(1, 'STOPPED\n'); process.exit(0); };
    if (server) { server.closeAllConnections(); server.close(finish); } else finish();
  }
  function serve() {
    if (behavior === 'early-exit') process.exit(2);
    if (behavior === 'timeout') return;
    server = http.createServer((req, res) => {
      if (mode === 'api') {
        const nonce = req.headers['x-loopcast-desktop-probe'];
        if (nonce) {
          const apiOrigin = `http://127.0.0.1:${server.address().port}`, publicOrigin = `http://${req.headers.host}`;
          if (!equalProof(req.headers['x-loopcast-desktop-proof'], healthProof(token, 'request', nonce, apiOrigin, publicOrigin, revision))) { res.writeHead(403); res.end(); return; }
          if (behavior !== 'wrong-health-proof') res.setHeader('x-loopcast-desktop-proof', healthProof(token, 'response', nonce, apiOrigin, publicOrigin, revision));
          res.end(JSON.stringify({ ready: true })); return;
        }
        if (req.headers['x-loopcast-desktop-session'] !== token || req.headers.host !== req.headers['x-forwarded-host']) {
          res.writeHead(403); res.end(); return;
        }
        res.setHeader('content-type', 'application/json'); res.end(JSON.stringify({ ready: true }));
      } else {
        res.setHeader('content-security-policy', "default-src 'self'; script-src 'self' 'nonce-fixture'"); res.end('fixture page');
      }
    });
    server.listen({ host: '127.0.0.1', port: 0 }, () => {
      const fd = mode === 'web' ? 3 : 1;
      const port = behavior.startsWith('stranger-') ? behavior.slice(9) : server.address().port;
      const line = `READY http://127.0.0.1:${port}\n`;
      writeSync(fd, behavior === 'malformed' ? 'READY http://0.0.0.0:1234\n' : behavior === 'flood' ? 'x'.repeat(300) : line);
      if (behavior === 'duplicate') writeSync(fd, line);
      if (behavior === 'crash') setTimeout(() => process.exit(3), 200);
    });
  }
  process.stdin.on('data', chunk => {
    input += chunk;
    while (input.includes('\n')) {
      const at = input.indexOf('\n'); const line = input.slice(0, at); input = input.slice(at + 1);
      if (line === 'STOP') stop();
      else if (mode === 'api' && !token) { token = JSON.parse(line).token; serve(); }
      else process.exit(4);
    }
  });
  process.stdin.on('end', stop);
  if (mode === 'web') serve();
}
