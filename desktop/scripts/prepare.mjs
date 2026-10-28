// SPDX-License-Identifier: Apache-2.0
// Read-only plan: emit a manifest; never create/migrate data or download runtimes.
import { createHash } from 'node:crypto';
import { createReadStream } from 'node:fs';
import { readFile, realpath, stat } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { isAbsolute, join, resolve } from 'node:path';
import { createTreePort } from '../src/linux-tree.mjs';
import { Budget } from '../src/errors.mjs';
import { capture } from '../src/protocol.mjs';

export async function prepare(values) {
  for (const key of ['python', 'node', 'next', 'data', 'session']) if (!isAbsolute(values[key] ?? '')) throw new Error('Absolute trusted resource paths required.');
  async function resource(path) {
    const actual = await realpath(path); if (!(await stat(actual)).isFile()) throw new Error('Missing resource.');
    const hash = createHash('sha256'); for await (const chunk of createReadStream(actual)) hash.update(chunk);
    return { path: resolve(path), sha256: hash.digest('hex') };
  }
  const python = await resource(values.python), node = await resource(values.node);
  // platform-info is read-only; use a bounded capture and a minimal environment.
  const port = createTreePort({ python, sessionDir: values.session });
  const budget = new Budget(10000);
  const environments = await port.environment({ dataDir: values.data, sessionDir: values.session, deadline: budget.end });
  const owned = port.launch({ executable: python.path, argv: ['-I', '-m', 'loopcast', 'platform-info'], cwd: values.session, env: environments.api, stdio: ['pipe', 'pipe', 'pipe'], deadline: budget.end });
  let info;
  try { info = JSON.parse(await capture(owned, budget)); }
  finally { if (!(await owned.closeTree(budget.end)).confirmed) throw new Error('Platform preflight shutdown unconfirmed.'); }
  const revision = (await readFile(join(values.next, '.next/BUILD_ID'), 'utf8')).trim();
  const result = { format: 'loopcast.desktop.v1', mode: 'development',
    hostRevision: values.revision, apiRevision: info.source_revision, webRevision: revision,
    python, node, treeModule: await resource(fileURLToPath(new URL('../src/linux-tree.mjs', import.meta.url))),
    nextRoot: values.next, dataDir: values.data, sessionDir: values.session,
    gatewayPort: Number(values.port), nodeVersion: '24.22.1', locale: values.locale ?? 'en',
    configs: {}, startTimeoutMs: 30000, stopTimeoutMs: 15000 };
  if (values.electron) result.electron = await resource(values.electron);
  const missing = Object.entries(info.capabilities ?? {}).filter(([, status]) => status !== 'implemented').map(([name]) => name);
  if (info.format !== 'loopcast.platform.v1' || missing.length || !/^[a-f0-9]{40}$/.test(info.source_revision) || revision !== values.revision || info.source_revision !== values.revision) throw new Error('Source or platform capabilities do not match the requested combination.');
  return result;
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    const values = {};
    for (let index = 2; index < process.argv.length; index += 2) values[process.argv[index].replace(/^--/, '')] = process.argv[index + 1];
    process.stdout.write(JSON.stringify(await prepare(values), null, 2) + '\n');
  } catch { process.stderr.write('Desktop plan unavailable. Check trusted absolute resources, versions and capabilities.\n'); process.exitCode = 1; }
}
