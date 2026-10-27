// SPDX-License-Identifier: Apache-2.0
import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, mkdir, writeFile, rm, stat, readFile, readdir } from 'node:fs/promises';
import { spawn, execFile } from 'node:child_process';
import { promisify } from 'node:util';
import http from 'node:http';
import { tmpdir } from 'node:os';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { prepare } from '../scripts/prepare.mjs';
import { launchElectron } from './electron-driver.mjs';
import { Budget } from '../src/errors.mjs';
import { message } from '../src/errors.mjs';
import { createTreePort } from '../src/linux-tree.mjs';
import { readiness, observeExit } from '../src/protocol.mjs';

const directory = fileURLToPath(new URL('../', import.meta.url));
const executable = process.env.LOOPCAST_ELECTRON_BINARY;
if (!executable || !process.env.LOOPCAST_DESKTOP_PYTHON) throw new Error('Verified Electron and an installed same-source Python candidate are required; no silent skip.');

async function fixture(t, { locale = 'en' } = {}) {
  const root = await mkdtemp(join(tmpdir(), 'loopcast Electron 中文 '));
  const data = join(root, '业务 data'), session = join(root, '窗口 session'), home = join(root, 'home'), appData = join(home, 'config');
  for (const path of [data, session, home, appData]) await mkdir(path, { mode: 0o700 });
  const config = await prepare({ python: process.env.LOOPCAST_DESKTOP_PYTHON, node: process.execPath,
    next: process.env.LOOPCAST_DESKTOP_NEXT, data, session, port: '28788', revision: process.env.LOOPCAST_DESKTOP_REVISION, locale });
  config.stopTimeoutMs = 6000;
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

async function until(fn, timeout = 15000) {
  const budget = new Budget(timeout);
  while (true) {
    const value = await budget.run(Promise.resolve().then(fn)); if (value) return value;
    await budget.run(new Promise(resolve => setTimeout(resolve, 25)));
  }
}

async function stage(name, work) {
  try { return await work(); }
  catch { throw new Error(`NATIVE_LIFECYCLE_${name}_FAILED`); }
}

async function managed(app) {
  return app.evaluate(() => {
    const fs = process.getBuiltinModule('fs'), pending = [process.pid], result = [];
    while (pending.length) {
      const parent = pending.pop(); let children = [];
      try { children = fs.readFileSync(`/proc/${parent}/task/${parent}/children`, 'utf8').trim().split(/\s+/).filter(Boolean).map(Number); } catch {}
      for (const pid of children) {
        pending.push(pid);
        try {
          const argv = fs.readFileSync(`/proc/${pid}/cmdline`, 'utf8').split('\0');
          if (argv.includes('loopcast.process_tree') || argv.includes('loopcast') && argv.includes('serve') || argv.some(arg => arg.endsWith('/next-child.cjs'))) {
            result.push({ pid, guardian: argv.includes('loopcast.process_tree'), api: argv.includes('loopcast') && argv.includes('serve'),
              start: fs.readFileSync(`/proc/${pid}/stat`, 'utf8').slice(fs.readFileSync(`/proc/${pid}/stat`, 'utf8').lastIndexOf(')') + 2).split(' ')[19] });
          }
        } catch {}
      }
    }
    return result;
  });
}

async function treesGone(records) {
  await until(async () => {
    for (const { pid, start } of records) {
      try { const raw = await readFile(`/proc/${pid}/stat`, 'utf8'); if (raw.slice(raw.lastIndexOf(')') + 2).split(' ')[19] === start) return false; } catch {}
    }
    return true;
  });
}

async function secondary(item, manifest = item.filename, args = []) {
  const child = spawn(executable, [directory, '--desktop-config', manifest, ...args], { env: item.env, stdio: 'ignore' });
  try { return await new Budget(10000).run(new Promise(resolve => child.once('exit', resolve))); }
  finally { if (child.exitCode === null && child.signalCode === null) child.kill('SIGKILL'); }
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

test('real native requests, navigation, media, resources and hostile contexts use only the production policy', { timeout: 90000 }, async t => {
  const item = await fixture(t), app = await item.start(), page = await app.firstPage();
  await page.locator('.heading .primary').focus(); await page.keyboard.press('Enter');
  await page.locator('input[name=title]').fill('Native Electron request ownership');
  await page.locator('#create-form button[type=submit]').click(); await page.waitForURL('**/runs/*');
  const id = new URL(page.url()).pathname.split('/').at(-1), origin = new URL(page.url()).origin;
  assert.equal(await page.evaluate(async id => {
    const response = await fetch(`/v1/runs/${id}/events?after=0`); const reader = response.body.getReader();
    const first = await reader.read(); await reader.cancel(); return response.status === 200 && first.value.length > 0;
  }, id), true);
  await page.locator('aside nav a[href="/library"]').click();
  await page.locator('#media-file').setInputFiles(process.env.LOOPCAST_DESKTOP_SAMPLE);
  await page.locator('#media-source').fill('Owned native Electron contract clip');
  await page.getByRole('button', { name: 'Import media', exact: true }).click(); await page.locator('.asset-card').waitFor();
  assert.equal(await page.evaluate(async () => {
    const assets = await (await fetch('/v1/studio/assets')).json();
    const asset = assets.items[0];
    const response = await fetch(`/v1/studio/assets/${asset.id}/content`, { headers: { Range: 'bytes=0-15' } });
    return response.status === 206 && (await response.arrayBuffer()).byteLength === 16;
  }), true);
  await page.locator('aside nav a[href="/settings"]').click();
  await page.locator('#language-select').selectOption('zh-Hans'); await page.locator('#theme-select').selectOption('dark');
  await until(() => page.locator('html').getAttribute('lang').then(value => value === 'zh-Hans'));
  await page.locator('#language-select').selectOption('ar'); await until(() => page.locator('html').getAttribute('dir').then(value => value === 'rtl'));
  await app.evaluate(({ BrowserWindow }) => { BrowserWindow.getAllWindows()[0].setSize(390, 844); });
  assert.ok(await page.evaluate(() => document.fonts.ready.then(() => document.fonts.status === 'loaded')));
  await page.locator('#language-select').selectOption('en');
  await app.evaluate(({ BrowserWindow }) => { BrowserWindow.getAllWindows()[0].setSize(1280, 860); });
  await page.locator('aside nav a[href="/create"]').click(); await page.locator('#project-title').fill('Native client navigation');
  await page.getByRole('button', { name: 'New project', exact: true }).click(); await page.waitForURL('**/create/*');
  const audit = await app.evaluate(() => globalThis.windowAudit);
  for (const type of ['mainFrame', 'script', 'stylesheet', 'font', 'xhr']) assert.ok(audit.decisions.some(value => value.type === type && value.capability && !value.cancel), type);
  assert.ok(audit.decisions.some(value => value.path.includes('/v1/runs/') && value.path.endsWith('/events') && value.capability));
  assert.ok(audit.decisions.some(value => value.path.startsWith('/_next/') && value.capability));
  assert.equal(audit.decisions.some(value => value.cancel), false, 'A legitimate native request was rejected before adversarial probes');
  const bare = await fetch(origin+'/healthz'); assert.equal(bare.status, 403);
  assert.equal((await fetch(origin+'/healthz', { headers: { 'X-LoopCast-Desktop-Session': 'forged' } })).status, 403);
  const windowsBefore = await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows().length);
  await page.evaluate(() => { window.open('https://example.com/'); });
  await page.evaluate(() => { window.location.href = 'https://example.com/'; });
  await page.waitForTimeout(200);
  assert.equal(new URL(page.url()).origin, origin);
  assert.equal(await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows().length), windowsBefore);
  assert.equal(await page.evaluate(() => Notification.requestPermission()), 'denied');
  let contacted = false;
  const stranger = http.createServer((_request, response) => { contacted = true; response.end('untrusted'); });
  await new Promise(resolve => stranger.listen(0, '127.0.0.1', resolve));
  try {
    assert.equal(await page.evaluate(async url => { try { await fetch(url); return false; } catch { return true; } }, `http://127.0.0.1:${stranger.address().port}/`), true);
    assert.equal(contacted, false);
    const interception = await app.context.newCDPSession(page);
    await interception.send('Fetch.enable', { patterns: [{ urlPattern: '*/native-redirect-contract', requestStage: 'Response' }] });
    interception.on('Fetch.requestPaused', event => interception.send('Fetch.fulfillRequest', {
      requestId: event.requestId, responseCode: 302,
      responseHeaders: [...(event.responseHeaders ?? []).filter(header => !['content-length', 'location'].includes(header.name.toLowerCase())), { name: 'Location', value: `http://127.0.0.1:${stranger.address().port}/` }], body: '',
    }).catch(() => {}));
    await page.evaluate(() => { window.location.href = '/native-redirect-contract'; });
    await page.waitForTimeout(200); assert.equal(new URL(page.url()).origin, origin); assert.equal(contacted, false);
    await interception.send('Fetch.disable'); await interception.detach();
  } finally { await new Promise(resolve => stranger.close(resolve)); }
  assert.equal(await app.evaluate(async ({ BrowserWindow }, origin) => {
    const source = BrowserWindow.getAllWindows()[0], other = new BrowserWindow({ show: false, webPreferences: { session: source.webContents.session, sandbox: true, contextIsolation: true, nodeIntegration: false, webSecurity: true } });
    try { return await other.loadURL(origin).then(() => false, () => true); } finally { other.destroy(); }
  }, origin), true);
  await app.evaluate(({ BrowserWindow }) => {
    const current = BrowserWindow.getAllWindows()[0].webContents.session;
    current.once('will-download', (_event, item) => setImmediate(() => {
      try { globalThis.windowAudit.downloadDenied = item.getState() === 'cancelled'; }
      catch { globalThis.windowAudit.downloadDenied = true; }
    }));
  });
  await page.evaluate(async () => {
    const assets = await (await fetch('/v1/studio/assets')).json();
    const link = document.createElement('a'); link.href = `/v1/studio/assets/${assets.items[0].id}/content`;
    link.download = 'native-contract.mp4'; document.body.append(link); link.click(); link.remove();
  });
  await until(() => app.evaluate(() => globalThis.windowAudit.downloadDenied === true));
  await page.evaluate(() => {
    window.nativeFrameViolations = 0;
    document.addEventListener('securitypolicyviolation', event => { if (event.violatedDirective === 'frame-src') window.nativeFrameViolations++; });
    const child = document.createElement('iframe'); child.name = 'untrusted-native-frame'; document.body.append(child);
  });
  await page.waitForTimeout(200);
  const frame = page.frame({ name: 'untrusted-native-frame' });
  if (frame) assert.equal(await frame.evaluate(async origin => { try { return (await fetch(origin+'/healthz')).status === 200; } catch { return false; } }, origin).catch(() => false), false);
  assert.ok(await app.evaluate(() => globalThis.windowAudit.decisions.some(value => value.type === 'xhr' && value.cancel && !value.capability)) || await page.evaluate(() => window.nativeFrameViolations > 0));
  await page.evaluate(() => document.querySelector('iframe[name="untrusted-native-frame"]').remove());
  assert.equal(await page.evaluate(async () => { try { return (await fetch('/healthz')).status === 200; } catch { return false; } }), false);
  assert.equal((await app.quit()).code, 0);
});

test('two actual processes ignore the second config and keep a single native window and owner', { timeout: 70000 }, async t => {
  const item = await fixture(t), app = await item.start(), page = await app.firstPage(); await page.locator('.heading .primary').waitFor();
  const before = await managed(app); assert.equal(before.filter(value => value.guardian).length, 2);
  await app.evaluate(({ BrowserWindow }) => { BrowserWindow.getAllWindows()[0].minimize(); });
  assert.equal(await secondary(item, join(item.root, 'not-readable-do-not-use.json'), ['file:///must-not-execute', 'https://example.com/untrusted']), 0);
  await until(() => app.evaluate(({ BrowserWindow }) => !BrowserWindow.getAllWindows()[0].isMinimized()));
  assert.deepEqual((await managed(app)).map(value => value.pid).sort(), before.map(value => value.pid).sort());
  assert.equal(await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows().length), 1);
  assert.equal((await app.quit()).code, 0); await treesGone(before);
});

test('real response loss, reload, cache-only clear and native restart preserve two explicit retry identities', { timeout: 120000 }, async t => {
  const item = await fixture(t); let app = await item.start(), page = await app.firstPage();
  const committed = [], keys = [];
  async function intercept(page, lose) {
    const cdp = await app.context.newCDPSession(page);
    await cdp.send('Fetch.enable', { patterns: [{ urlPattern: '*/v1/runs', requestStage: 'Response' }] });
    cdp.on('Fetch.requestPaused', async event => {
      if (event.request.method !== 'POST') { await cdp.send('Fetch.continueResponse', { requestId: event.requestId }); return; }
      keys.push(Object.entries(event.request.headers).find(([name]) => name.toLowerCase() === 'idempotency-key')?.[1]);
      if (lose) {
        const body = await cdp.send('Fetch.getResponseBody', { requestId: event.requestId });
        const value = JSON.parse(body.base64Encoded ? Buffer.from(body.body, 'base64').toString('utf8') : body.body);
        committed.push(value.run_id);
        await cdp.send('Fetch.failRequest', { requestId: event.requestId, errorReason: 'Aborted' });
      } else await cdp.send('Fetch.continueResponse', { requestId: event.requestId });
    });
  }
  await intercept(page, true);
  await page.locator('.heading .primary').click();
  for (const [index, title] of ['First unresolved native write', 'Second unresolved native write'].entries()) {
    await page.locator('input[name=title]').fill(title); await page.locator('#create-form button[type=submit]').click();
    await until(() => committed.length === index + 1);
    await until(() => page.locator('#form-error').textContent().then(Boolean));
  }
  assert.ok(committed.length === 2 && committed.every(Boolean));
  assert.equal(await page.evaluate(() => Object.keys(localStorage).filter(key => key.startsWith('loopcast.run.create.pending.v1.')).length), 2);
  await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].webContents.session.clearCache());
  const originalOwner = await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].webContents.session.cookies.get({ name: 'loopcast_owner' }));
  assert.equal((await app.quit()).code, 0);
  app = await item.start(); page = await app.firstPage(); await intercept(page, false);
  await page.locator('#create-dialog[open]').waitFor(); assert.equal(keys.length, 2);
  const frozen = await page.locator('input[name=title]').inputValue();
  const firstIndex = frozen.startsWith('First') ? 0 : 1;
  const owner = await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].webContents.session.cookies.get({ name: 'loopcast_owner' }));
  assert.ok(owner[0].value === originalOwner[0].value);
  await page.locator('#create-form button[type=submit]').click(); await page.waitForURL('**/runs/'+committed[firstIndex]);
  assert.ok(keys[2] === keys[firstIndex]);
  await page.reload(); await page.locator('#create-dialog[open]').waitFor();
  assert.equal(keys.length, 3);
  await page.locator('#create-form button[type=submit]').click(); await page.waitForURL('**/runs/'+committed[1-firstIndex]);
  assert.ok(keys[3] === keys[1-firstIndex]);
  assert.equal(await page.evaluate(() => Object.keys(localStorage).filter(key => key.startsWith('loopcast.run.create.pending.v1.')).length), 0);
  const count = await page.evaluate(async () => (await (await fetch('/v1/runs?limit=100')).json()).items.length); assert.equal(count, 2);
  assert.equal((await app.quit()).code, 0);
});

test('native renderer crash and host death preserve the STOP/guard boundary without replacement', { timeout: 100000 }, async t => {
  const item = await fixture(t); let app = await stage('RENDERER_BOOT', () => item.start()), page = await app.firstPage(); await page.locator('.heading .primary').waitFor();
  const normal = await managed(app);
  await stage('RENDERER_TRIGGER', () => app.evaluate(({ BrowserWindow }) => { BrowserWindow.getAllWindows()[0].webContents.forcefullyCrashRenderer(); }));
  await stage('RENDERER_DETACH', () => app.detach());
  assert.equal((await stage('RENDERER_EXIT', () => new Budget(20000).run(app.exited))).code, 0);
  await stage('RENDERER_TREES', () => treesGone(normal));
  await assert.rejects(stat(join(item.data, 'desktop-runtime.guard.v1')));
  app = await stage('HOST_BOOT', () => item.start()); page = await app.firstPage(); await page.locator('.heading .primary').waitFor();
  const abnormal = await managed(app); app.child.kill('SIGKILL');
  await stage('HOST_DETACH', () => app.detach()); await app.exited;
  await stage('HOST_TREES', () => treesGone(abnormal));
  assert.ok(await stat(join(item.data, 'desktop-runtime.guard.v1')));
  const refused = await stage('GUARD_BOOT', () => item.start());
  await stage('GUARD_DIALOG', () => until(() => refused.evaluate(() => globalThis.windowAudit.dialogs.length > 0)));
  assert.equal(await refused.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows().length), 0);
  assert.equal((await stage('GUARD_EXIT', () => refused.quit())).code, 1);
  assert.ok(await stat(join(item.data, 'desktop-runtime.guard.v1')));
});

test('actual native error dialogs are localized and quit cancels them within the bound', { timeout: 70000 }, async t => {
  for (const [locale, code] of [['en', 'CONFIG_INVALID'], ['zh-Hans', 'CONFIG_INVALID'], ['en', 'VERSION_MISMATCH']]) {
    const item = await fixture(t, { locale });
    if (code === 'CONFIG_INVALID') item.config.node.sha256 = '0'.repeat(64);
    else item.config.webRevision = 'f'.repeat(40);
    await writeFile(item.filename, JSON.stringify(item.config), { mode: 0o600 });
    const app = await item.start(); await until(() => app.evaluate(() => globalThis.windowAudit.dialogs.length > 0));
    const dialogs = await app.evaluate(() => globalThis.windowAudit.dialogs);
    assert.equal(dialogs[0].message, message(code, locale));
    assert.equal(await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows().length), 0);
    await until(async () => (await promisify(execFile)('xwininfo', ['-root', '-tree'])).stdout.includes('"LoopCast"'));
    assert.equal((await app.quit()).code, 1);
    await assert.rejects(stat(join(item.data, 'state.sqlite3')));
    await assert.rejects(stat(join(item.data, 'desktop-runtime.guard.v1')));
  }
});

test('an existing managed CLI owner and an occupied entry port are never attached or replaced', { timeout: 100000 }, async t => {
  const item = await fixture(t), port = createTreePort(item.config), budget = new Budget(30000);
  const env = await port.environment({ dataDir: item.data, sessionDir: item.session, deadline: budget.end });
  const owned = port.launch({ executable: item.config.python.path, argv: ['-I', '-m', 'loopcast', 'serve', '--port', '0', '--data-dir', item.data, '--managed-stdin'],
    env: env.api, cwd: item.session, stdio: ['pipe', 'pipe', 'pipe'], deadline: budget.end });
  const exit = observeExit(owned.child), protocol = readiness(owned.child.stdout); owned.child.stderr.resume();
  const cliOrigin = await budget.run(protocol.ready);
  try {
    const app = await item.start();
    await until(async () => app.status || await app.evaluate(() => globalThis.windowAudit.dialogs.length > 0).catch(() => false));
    if (!app.status) assert.equal((await app.quit()).code, 1);
    else assert.equal(app.status.code, 1);
    assert.equal((await fetch(cliOrigin+'/healthz')).status, 200);
    assert.equal(exit.result, undefined);
  } finally {
    owned.child.stdin.end('STOP\n'); await new Budget(10000).run(Promise.all([protocol.stopped, exit.exited]));
    assert.equal((await owned.closeTree(performance.now()+5000)).confirmed, true);
  }
  const second = await fixture(t); let requests = 0;
  const stranger = http.createServer((_request, response) => { requests++; response.end('not the application'); });
  await new Promise(resolve => stranger.listen(second.config.gatewayPort, '127.0.0.1', resolve));
  try {
    const refused = await second.start(); await until(() => refused.evaluate(() => globalThis.windowAudit.dialogs.length > 0));
    assert.equal((await refused.quit()).code, 1); assert.equal(requests, 0);
    await assert.rejects(stat(join(second.data, 'state.sqlite3')));
  } finally { await new Promise(resolve => stranger.close(resolve)); }
});

test('missing configuration and startup-time exit create no fallback profile or second owner', { timeout: 60000 }, async t => {
  const item = await fixture(t);
  assert.equal(await secondary(item, join(item.root, 'missing.json')), 1);
  await assert.rejects(stat(join(item.data, 'state.sqlite3')));
  assert.equal((await readdir(item.session)).length, 0);
  const lock = await readdir(join(item.appData, 'LoopCast-instance'));
  assert.equal(lock.some(name => /Cache|Cookies|Local Storage|Partitions/.test(name)), false);
  const app = await item.start();
  await app.evaluate(({ app }) => { app.quit(); app.quit(); });
  await app.detach(); assert.equal((await new Budget(20000).run(app.exited)).code, 0);
  await assert.rejects(stat(join(item.data, 'desktop-runtime.guard.v1')));
});
