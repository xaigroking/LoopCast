// SPDX-License-Identifier: Apache-2.0
// Run with the independent pinned Node, never Electron's embedded Node.
const http = require('node:http');
const fs = require('node:fs');
const path = require('node:path');
const { createRequire } = require('node:module');
const root = process.argv[2], revision = process.argv[3];
if (process.versions.node !== '24.22.1' || !path.isAbsolute(root) ||
    fs.readFileSync(path.join(root, '.next/BUILD_ID'), 'utf8').trim() !== revision) process.exit(1);
process.env.HOSTNAME = '127.0.0.1';
process.env.PORT = '0';
process.env.NODE_ENV = 'production';
process.env.NEXT_TELEMETRY_DISABLED = '1';
let server, stopping = false;
const original = http.Server.prototype.listen;
http.Server.prototype.listen = function (...args) {
  if (server) throw new Error('Unexpected additional listener');
  server = this;
  this.once('listening', () => {
    const address = this.address();
    if (stopping || !address || address.address !== '127.0.0.1') process.exit(1);
    fs.writeSync(3, `READY http://127.0.0.1:${address.port}\n`);
  });
  // standalone uses parseInt(PORT) || 3000, so PORT=0 alone cannot reserve an
  // ephemeral port. Override the actual bind, preserving its listening callback.
  const callback = typeof args.at(-1) === 'function' ? args.at(-1) : undefined;
  return original.call(this, { host: '127.0.0.1', port: 0, exclusive: true }, callback);
};
function stop() {
  if (stopping) return;
  stopping = true;
  if (!server) process.exit(0);
  server.closeAllConnections();
  server.close(() => process.exit(0));
}
let input = '';
process.stdin.on('data', chunk => {
  input += chunk.toString('ascii');
  if (input.length > 64 || (input.includes('\n') && input !== 'STOP\n')) { stop(); return; }
  if (input === 'STOP\n') stop();
});
process.stdin.on('end', stop);
process.stdin.on('error', stop);
createRequire(path.join(root, 'server.js'))('./server.js');
