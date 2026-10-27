// SPDX-License-Identifier: Apache-2.0
import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';
const fixture = fileURLToPath(new URL('./service-child.mjs', import.meta.url));
export function config(overrides = {}) {
  return { format: 'loopcast.desktop.v1', mode: 'development', hostRevision: 'a'.repeat(40), apiRevision: 'a'.repeat(40), webRevision: 'a'.repeat(40),
    node: { path: process.execPath }, python: { path: '/fixture/python' }, nextRoot: '/fixture/Next 中文 folder',
    sessionDir: process.cwd(), dataDir: '/fixture/data', configs: {}, nodeVersion: '24.22.1', locale: 'en',
    gatewayPort: 0, startTimeoutMs: 3000, stopTimeoutMs: 400, ...overrides };
}
export function fixturePort(options = {}) {
  const records = [];
  return {
    format: 'loopcast.process-tree.v1', records,
    async environment() { if (options.environment) await options.environment; return { api: {}, web: {} }; },
    launch(spec) {
      let args, role;
      if (spec.argv.includes('platform-info')) { role = 'info'; args = [fixture, role, options.info ?? 'normal']; }
      else if (spec.argv.includes('serve')) { role = 'api'; args = [fixture, role, options.api ?? 'normal']; }
      else if (spec.argv[0] === '--version') { role = 'version'; args = options.wrongNode ? [fixture, 'version'] : ['--version']; }
      else { role = 'web'; args = [fixture, role, options.web ?? 'normal']; }
      const child = spawn(process.execPath, args, { cwd: spec.cwd, env: spec.env, stdio: spec.stdio });
      let status;
      const exited = new Promise(resolve => child.once('exit', code => { status = code; resolve(); }));
      const record = { spec, child, role, get status() { return status; } }; records.push(record);
      return { child, async closeTree(deadline) {
        // TEST ONLY: each fixture is known to contain no descendants. Production
        // must implement the platform's complete ProcessTree, not copy this port.
        if (status === undefined) {
          const timer = setTimeout(() => child.kill('SIGKILL'), Math.max(0, deadline - performance.now() - 5));
          await exited; clearTimeout(timer);
        }
        return { confirmed: !options.unconfirmedTree };
      } };
    },
  };
}
