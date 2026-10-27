// SPDX-License-Identifier: Apache-2.0
// The actual production host coordinator without a window, for dev integration.
import { readConfig, loadTreePort } from '../src/config.mjs';
import { DesktopHost } from '../src/host.mjs';
import { safeError } from '../src/errors.mjs';
import { writeSync } from 'node:fs';
let host, stopping = false;
async function stop() {
  if (stopping) return; stopping = true;
  const result = await host?.stop(); process.exitCode = result?.confirmed === false ? 1 : 0;
}
process.on('SIGINT', stop); process.on('SIGTERM', stop);
process.stdin.on('data', stop); process.stdin.on('end', stop); process.stdin.resume();
try {
  const config = await readConfig(process.argv[2]);
  if (stopping) process.exit(0);
  host = new DesktopHost(config, await loadTreePort(config));
  host.on('fault', () => { void stop(); });
  const origin = await host.start();
  // Private test/developer pipe, never a URL token or public error log.
  const descriptor = Number(process.argv[3] ?? 3);
  if (!Number.isInteger(descriptor) || descriptor < 3) throw new Error();
  writeSync(descriptor, JSON.stringify({ origin, gatewayToken: host.gatewayToken }) + '\n');
  process.stdout.write(`READY ${origin}\n`);
} catch (error) {
  process.stderr.write(safeError(error).code + '\n'); await stop(); process.exitCode = 1;
}
