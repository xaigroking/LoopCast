// SPDX-License-Identifier: Apache-2.0
import { spawn } from 'node:child_process';
import { isAbsolute } from 'node:path';
import { Budget, HostError } from './errors.mjs';
import { parsePreflight } from './preflight.mjs';

export function createTreePort(config) {
  if (process.platform !== 'linux' || process.arch !== 'x64') throw new HostError('PLATFORM_UNSUPPORTED');
  const records = [];
  let paths;
  const env = { PATH: '/usr/bin:/bin', LANG: 'C.UTF-8', HOME: config.sessionDir, TMPDIR: config.sessionDir };
  const port = {
    format: 'loopcast.process-tree.v1',
    async environment(value) {
      if (records.some(record => record.status === undefined)) throw new HostError('PLATFORM_UNAVAILABLE');
      records.length = 0;
      paths = value;
      await command(['-I', '-m', 'loopcast.desktop_guard', 'validate-result', value.dataDir, value.sessionDir], value.deadline, true);
      return { api: { ...env }, web: { ...env } };
    },
    maintenanceEnvironment() { return { ...env }; },
    launch(spec) {
      if (!isAbsolute(spec.executable) || !Array.isArray(spec.argv) || spec.argv.length > 128 ||
          [spec.executable, ...spec.argv].some(value => typeof value !== 'string' || value.includes('\0')) ||
          performance.now() >= spec.deadline || spec.signal?.aborted) throw new HostError('START_TIMEOUT');
      const stdio = ['pipe', spec.stdio[1], 'pipe', 'pipe', 'pipe', 'pipe'];
      const child = spawn(config.python.path, ['-I', '-m', 'loopcast.process_tree', spec.executable, ...spec.argv], {
        cwd: spec.cwd, env: spec.env, stdio, detached: true, windowsHide: true,
      });
      let status, witness, buffer = '', malformed = false;
      const exited = new Promise(resolve => {
        child.once('exit', code => { status = code; resolve(code); });
        child.once('error', () => { status = null; resolve(null); });
      });
      const observed = new Promise(resolve => {
        child.stdio[5].on('data', chunk => {
          buffer += chunk.toString('utf8');
          if (buffer.length > 512) { malformed = true; buffer = ''; }
        });
        child.stdio[5].once('end', () => {
          try {
            if (malformed) throw new Error();
            witness = JSON.parse(buffer);
            if (witness.format !== 'loopcast.tree-stopped.v1' || witness.confirmed !== true) witness = null;
          } catch { witness = null; }
          resolve();
        });
        child.stdio[5].once('error', () => { witness = null; resolve(); });
      });
      child.stdio[4].on('error', () => {});
      const record = { child, exited, observed, api: spec.argv.includes('--desktop-session-stdin'),
        get status() { return status; }, get witness() { return witness; } };
      records.push(record);
      let closing;
      const owned = { child, closeTree(deadline) {
        if (!closing) closing = (async () => {
          if (status === undefined) child.stdio[4].end(`TERMINATE ${Math.max(0, Math.floor(deadline - performance.now()) - 10)}\n`);
          try {
            await new Budget(Math.max(1, deadline - performance.now())).run(Promise.all([exited, observed]));
            return { confirmed: witness?.confirmed === true };
          } catch { return { confirmed: false }; }
        })();
        return closing;
      } };
      record.closeTree = owned.closeTree;
      return owned;
    },
    async finish({ confirmed, deadline }) {
      const evidence = await Promise.all(records.map(record => record.closeTree(deadline)));
      if (evidence.some(value => !value.confirmed)) return { confirmed: false };
      const api = records.find(record => record.api);
      if (!api) return { confirmed };
      if (!confirmed || api.status !== 0 || !api.witness?.identity || records.some(record => !record.witness?.confirmed)) return { confirmed: false };
      await command(['-I', '-m', 'loopcast.desktop_guard', 'finalize', paths.dataDir, api.witness.identity], deadline);
      return { confirmed: true };
    },
  };
  async function command(argv, deadline, structured = false) {
    const owned = port.launch({ executable: config.python.path, argv, cwd: config.sessionDir, env,
      stdio: ['pipe', 'pipe', 'pipe'], deadline });
    owned.child.stdin.end(); owned.child.stderr.resume();
    let chunks = [], size = 0, malformed = false;
    const output = new Promise((resolve, reject) => {
      owned.child.stdout.on('data', chunk => {
        size += chunk.length;
        if (size > 512) { malformed = true; chunks = []; }
        else if (structured) chunks.push(chunk);
      });
      owned.child.stdout.once('end', resolve);
      owned.child.stdout.once('error', () => reject(new HostError('DIAGNOSTICS_UNAVAILABLE')));
    });
    output.catch(() => {});
    let status, result;
    try {
      [status] = await new Budget(Math.max(1, deadline - performance.now())).run(Promise.all([new Promise(resolve => {
        owned.child.once('exit', resolve); owned.child.once('error', () => resolve(null));
      }), output]));
    } finally { result = await owned.closeTree(deadline); }
    if (!result.confirmed) throw new HostError('STOP_UNCONFIRMED');
    if (structured) {
      if (malformed) throw new HostError('DIAGNOSTICS_UNAVAILABLE');
      const code = parsePreflight(Buffer.concat(chunks), status);
      if (code !== 'OK') throw new HostError(code);
    } else if (status !== 0) throw new HostError('PLATFORM_UNAVAILABLE');
  }
  return port;
}
