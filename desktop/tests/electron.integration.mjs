// SPDX-License-Identifier: Apache-2.0
import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, mkdir, writeFile, rm, stat, readFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { prepare } from '../scripts/prepare.mjs';
import { launchElectron } from './electron-driver.mjs';

const directory = fileURLToPath(new URL('../', import.meta.url));
const executable = process.env.LOOPCAST_ELECTRON_BINARY;
if (!executable || !process.env.LOOPCAST_DESKTOP_PYTHON) throw new Error('Verified Electron and an installed same-source Python candidate are required; no silent skip.');

async function fixture(t, { locale = 'en' } = {}) {
  const root = await mkdtemp(join(tmpdir(), 'loopcast Electron 中文 '));
  const data = join(root, '业务 data'), session = join(root, '窗口 session'), home = join(root, 'home'), appData = join(home, 'config');
  for (const path of [data, session, home, appData]) await mkdir(path, { mode: 0o700 });
  const config = await prepare({ python: process.env.LOOPCAST_DESKTOP_PYTHON, node: process.execPath,
    next: process.env.LOOPCAST_DESKTOP_NEXT, data, session, port: '28788', revision: process.env.LOOPCAST_DESKTOP_REVISION, locale });
  if (process.env.LOOPCAST_DESKTOP_MEDIA_CONFIG) config.configs.media = process.env.LOOPCAST_DESKTOP_MEDIA_CONFIG;
  const filename = join(root, 'desktop.json');
  await writeFile(filename, JSON.stringify(config), { mode: 0o600 });
  const env = { PATH: '/usr/bin:/bin', LANG: 'C.UTF-8', HOME: home, XDG_CONFIG_HOME: appData, DISPLAY: process.env.DISPLAY };
  for (const key of ['XAUTHORITY', 'LD_LIBRARY_PATH', 'DBUS_SESSION_BUS_ADDRESS']) if (process.env[key]) env[key] = process.env[key];
  const applications = [];
  async function start(options = {}) {
    const app = await launchElectron({ executable, directory, manifest: filename, env, ...options });
    applications.push(app); return app;
  }
  t.after(async () => {
    for (const app of applications) if (!app.status) await app.quit().catch(() => app.child.kill('SIGKILL'));
    await rm(root, { recursive: true, force: true });
  });
  return { root, data, session, home, appData, filename, env, config, start };
}

async function runtime(app, page) {
  const values = await app.evaluate(({ app, BrowserWindow }, origin) => {
    const window = BrowserWindow.getAllWindows().find(window => window.webContents.getURL().startsWith(origin));
    return { versions: { electron: process.versions.electron, chromium: process.versions.chrome, node: process.versions.node },
      paths: { userData: app.getPath('userData'), sessionData: app.getPath('sessionData'), storage: window.webContents.session.getStoragePath() },
      prefs: window.webContents.getLastWebPreferences(), visible: window.isVisible(), pid: window.webContents.getOSProcessId(),
      unsafeFlags: ['no-sandbox', 'disable-web-security', 'disable-setuid-sandbox'].some(flag => app.commandLine.hasSwitch(flag)) };
  }, new URL(page.url()).origin);
  assert.equal(values.versions.electron, '45.0.0'); assert.equal(values.unsafeFlags, false);
  for (const key of ['sandbox', 'contextIsolation', 'webSecurity']) assert.equal(values.prefs[key], true);
  assert.equal(values.prefs.nodeIntegration, false); assert.equal(values.visible, true);
  const status = await readFile(`/proc/${values.pid}/status`, 'utf8');
  assert.match(status, /^NoNewPrivs:\s+1$/m); assert.match(status, /^Seccomp:\s+2$/m);
  assert.equal(await page.evaluate(() => typeof window.require), 'undefined');
  assert.equal(await page.evaluate(() => typeof window.process), 'undefined');
  return values;
}

test('actual entry sets native ready/session paths and loads its isolated production window', { timeout: 70000 }, async t => {
  const item = await fixture(t), app = await item.start(), page = await app.firstPage();
  await page.locator('.heading .primary').waitFor();
  const values = await runtime(app, page);
  assert.equal(values.paths.userData, item.session); assert.equal(values.paths.sessionData, item.session);
  assert.ok(values.paths.storage.startsWith(item.session+'/'));
  const audit = await app.evaluate(() => globalThis.windowAudit);
  assert.equal(audit.ready.userData, item.session); assert.equal(audit.ready.sessionData, item.session);
  assert.equal(audit.dialogs.length, 0);
  assert.ok(audit.decisions.some(value => value.type === 'mainFrame' && value.capability && !value.cancel));
  assert.ok(audit.decisions.some(value => value.type === 'script' && value.capability && !value.cancel));
  await page.evaluate(() => localStorage.setItem('window-profile-canary', 'retained'));
  const owner = await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].webContents.session.cookies.get({ name: 'loopcast_owner' }));
  assert.ok(owner.length === 1 && owner[0].httpOnly);
  assert.equal((await app.quit()).code, 0);
  await assert.rejects(stat(join(item.data, 'desktop-runtime.guard.v1')));
  const restarted = await item.start(), next = await restarted.firstPage();
  await next.locator('.heading .primary').waitFor();
  assert.equal(await next.evaluate(() => localStorage.getItem('window-profile-canary')), 'retained');
  const nextOwner = await restarted.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].webContents.session.cookies.get({ name: 'loopcast_owner' }));
  assert.ok(nextOwner.length === 1 && nextOwner[0].value === owner[0].value);
  assert.equal((await restarted.quit()).code, 0);
  console.log('Electron native entry/session/isolation and disk restart PASS', JSON.stringify(values.versions));
});

test('unpaused executable startup also uses the configured profile and exits through the real window', { timeout: 60000 }, async t => {
  const item = await fixture(t), app = await item.start({ observe: false }), page = await app.firstPage();
  await page.locator('.heading .primary').waitFor();
  const values = await runtime(app, page);
  assert.equal(values.paths.sessionData, item.session);
  await app.evaluate(({ BrowserWindow }) => { BrowserWindow.getAllWindows()[0].close(); });
  await app.detach(); assert.equal((await app.exited).code, 0);
  await assert.rejects(stat(join(item.data, 'desktop-runtime.guard.v1')));
});
