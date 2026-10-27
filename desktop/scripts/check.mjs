// SPDX-License-Identifier: Apache-2.0
import { readdirSync } from 'node:fs';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
for (const directory of ['src', 'tests', 'scripts']) {
  for (const name of readdirSync(new URL(`../${directory}/`, import.meta.url))) {
    if (!/\.(mjs|cjs)$/.test(name)) continue;
    const result = spawnSync(process.execPath, ['--check', fileURLToPath(new URL(`../${directory}/${name}`, import.meta.url))], { stdio: 'inherit' });
    if (result.status !== 0) process.exit(1);
  }
}
