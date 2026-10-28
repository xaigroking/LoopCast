// SPDX-License-Identifier: Apache-2.0
import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, mkdir, writeFile, rm, stat, readFile, readdir } from 'node:fs/promises';
import { spawn, execFile } from 'node:child_process';
import { promisify } from 'node:util';
import http from 'node:http';
import { tmpdir } from 'node:os';
import { join, dirname } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { createHash } from 'node:crypto';
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
    const trace = join(root, `native-lifecycle-${applications.length}.json`);
    const app = await launchElectron({ executable, directory, manifest: filename, env, trace, ...options });
    app.lifecycleTrace = trace;
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
  const pythonVersion = (await promisify(execFile)(item.config.python.path, ['-I', '--version'], { timeout: 5000 })).stdout.trim();
  // Record verified resource identity without paths, manifests or capabilities.
  console.log('Native resource identity', JSON.stringify({ platform: process.platform, arch: process.arch, pythonVersion,
    revisions: { host: item.config.hostRevision, api: item.config.apiRevision, next: item.config.webRevision },
    sha256: { python: item.config.python.sha256, node: item.config.node.sha256, treeModule: item.config.treeModule.sha256 } }));
});

test('native maintenance menu uses the production coordinator and CLI, preserves the original profile and returns only on explicit selection', { timeout:120000 }, async t => {
  const item=await fixture(t), app=await item.start(); let page=await app.firstPage();
  await page.locator('.heading .primary').waitFor();
  await page.evaluate(()=>{localStorage.setItem('maintenance-profile-canary','retained');});
  const packagePath=join(item.root,'native-package'),proofPath=join(item.root,'independent-proof.json'),restoredPath=join(item.root,'native-restored');
  const originalOwner=await app.evaluate(({BrowserWindow})=>BrowserWindow.getAllWindows()[0].webContents.session.cookies.get({name:'loopcast_owner'}));
  // Controlled native-dialog result injection through the external inspector,
  // never a product flag, renderer bridge or replacement maintenance handler.
  await app.evaluate(({dialog},paths)=>{
    globalThis.maintenanceDialogEvidence={plans:0,results:[],saves:0,opens:0};
    const saves=[paths.packagePath,paths.proofPath,paths.restoredPath];
    const opens=[paths.packagePath,paths.proofPath,paths.packagePath,paths.proofPath];
    dialog.showSaveDialog=async()=>{globalThis.maintenanceDialogEvidence.saves++;return{canceled:false,filePath:saves.shift()};};
    dialog.showOpenDialog=async()=>{globalThis.maintenanceDialogEvidence.opens++;return{canceled:false,filePaths:[opens.shift()]};};
    dialog.showMessageBox=async options=>{
      if(options.type==='question'){globalThis.maintenanceDialogEvidence.plans++;return{response:0};}
      globalThis.maintenanceDialogEvidence.results.push({type:options.type,guarded:options.message.includes('barrier')});
      return{response:options.buttons.length===2?1:0};
    };
  },{packagePath,proofPath,restoredPath});
  async function click(id){await app.evaluate(({Menu},id)=>{const item=Menu.getApplicationMenu().getMenuItemById(id);if(!item?.enabled)throw new Error('Menu action disabled');item.click();},id);}
  async function available(id){return app.evaluate(({Menu},id)=>Menu.getApplicationMenu().getMenuItemById(id)?.enabled,id);}
  await click('maintenance-create');
  await until(async()=>{try{await stat(proofPath);return available('maintenance-verify');}catch{return false;}},30000);
  assert.equal(page.isClosed(),true); await assert.rejects(stat(join(item.data,'desktop-runtime.guard.v1')));
  await assert.rejects(stat(join(item.data,'desktop-maintenance.guard.v1')));
  const utility=await app.evaluate(({BrowserWindow})=>{
    const window=BrowserWindow.getAllWindows()[0],prefs=window.webContents.getLastWebPreferences();
    return{count:BrowserWindow.getAllWindows().length,local:window.webContents.getURL().startsWith('data:'),sandbox:prefs.sandbox,contextIsolation:prefs.contextIsolation,webSecurity:prefs.webSecurity,node:prefs.nodeIntegration,persistent:window.webContents.session.isPersistent()};
  });
  assert.deepEqual(utility,{count:1,local:true,sandbox:true,contextIsolation:true,webSecurity:true,node:false,persistent:false});
  const proof=JSON.parse(await readFile(proofPath,'utf8'));assert.match(proof.manifest_sha256,/^[a-f0-9]{64}$/);
  await click('maintenance-verify');await until(()=>available('maintenance-restore'),30000);
  await click('maintenance-restore');
  await until(async()=>{try{await stat(join(restoredPath,'restore-manifest.json'));return available('maintenance-return');}catch{return false;}},30000);
  await assert.rejects(stat(join(restoredPath,'desktop-runtime.guard.v1')));
  assert.equal((await app.evaluate(()=>globalThis.maintenanceDialogEvidence.results)).length,3);
  await click('maintenance-return');page=await app.firstPage();await page.locator('.heading .primary').waitFor();
  assert.equal(await page.evaluate(()=>localStorage.getItem('maintenance-profile-canary')),'retained');
  const owner=await app.evaluate(({BrowserWindow})=>BrowserWindow.getAllWindows()[0].webContents.session.cookies.get({name:'loopcast_owner'}));
  assert.equal(owner[0].value,originalOwner[0].value);
  const evidence=await app.evaluate(()=>globalThis.maintenanceDialogEvidence);
  assert.equal(evidence.plans,3);assert.equal(evidence.saves,3);assert.equal(evidence.opens,4);
  assert.ok(evidence.results.every(value=>value.type==='info'));assert.equal(evidence.results[2].guarded,true);
  assert.equal((await app.quit()).code,0);
  console.log('Controlled native-dialog results -> real maintenance menu/coordinator/ProcessTree/installed CLI/explicit original-profile Return PASS');
});

test('real native maintenance shortcut opens a save selector and native Escape cancels without stopping the owner', {timeout:70000}, async t=>{
  const item=await fixture(t,{locale:'zh-Hans'}),app=await item.start(),page=await app.firstPage();await page.locator('.heading .primary').waitFor();
  await app.evaluate(({dialog,BrowserWindow})=>{
    globalThis.nativeMaintenanceSelection={opened:false,cancelled:false};
    const original=dialog.showSaveDialog.bind(dialog);
    dialog.showSaveDialog=async options=>{globalThis.nativeMaintenanceSelection.opened=true;const result=await original(options);globalThis.nativeMaintenanceSelection.cancelled=result.canceled;return result;};
    BrowserWindow.getAllWindows()[0].focus();
  });
  const nativeWindow=await app.evaluate(({BrowserWindow})=>BrowserWindow.getAllWindows()[0].getNativeWindowHandle().readUInt32LE(0));
  await promisify(execFile)('xdotool',['windowactivate','--sync',String(nativeWindow)],{timeout:7000});
  await until(()=>app.evaluate(({BrowserWindow})=>BrowserWindow.getAllWindows()[0].isFocused()));
  await promisify(execFile)('xdotool',['key','--clearmodifiers','ctrl+shift+b'],{timeout:3000});
  await stage('MAINTENANCE_SELECTOR_REQUEST',()=>until(()=>app.evaluate(()=>globalThis.nativeMaintenanceSelection.opened)));
  const title=item.config.locale==='zh-Hans'?'选择全新的私有目录名称':'Choose a new private directory name';
  const picker=await stage('MAINTENANCE_SELECTOR_MAPPING',()=>until(async()=>{
    const active=(await promisify(execFile)('xdotool',['getactivewindow'],{timeout:3000})).stdout.trim();
    if(Number(active)===nativeWindow)return false;
    const name=(await promisify(execFile)('xdotool',['getwindowname',active],{timeout:3000})).stdout.trim();
    return name===title?active:false;
  }));
  await promisify(execFile)('xdotool',['windowactivate','--sync',picker],{timeout:7000});
  assert.equal((await promisify(execFile)('xdotool',['getactivewindow'],{timeout:3000})).stdout.trim(),picker);
  await promisify(execFile)('xdotool',['key','--clearmodifiers','Escape'],{timeout:3000});
  await stage('MAINTENANCE_SELECTOR_CANCEL',()=>until(()=>app.evaluate(()=>globalThis.nativeMaintenanceSelection.cancelled)));
  await until(()=>app.evaluate(({Menu})=>Menu.getApplicationMenu().getMenuItemById('maintenance-create').enabled));
  assert.equal(page.isClosed(),false);assert.equal((await page.evaluate(async()=>(await fetch('/healthz')).status)),200);
  assert.ok(await stat(join(item.data,'desktop-runtime.guard.v1')));await assert.rejects(stat(join(item.data,'desktop-maintenance.guard.v1')));
  assert.equal((await app.quit()).code,0);
  console.log('Actual X11 maintenance shortcut/native selector/Escape cancellation PASS');
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
  const create = page.locator('.heading .primary');
  await create.waitFor({ state: 'visible' });
  const nativeWindow = await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].getNativeWindowHandle().readUInt32LE(0));
  assert.ok(Number.isSafeInteger(nativeWindow) && nativeWindow > 0);
  await promisify(execFile)('xdotool', ['windowactivate','--sync',String(nativeWindow)], { timeout:7000 });
  assert.equal(Number((await promisify(execFile)('xdotool',['getactivewindow'],{timeout:3000})).stdout.trim()),nativeWindow);
  await until(() => app.evaluate(({ BrowserWindow }) => {
    const window=BrowserWindow.getAllWindows()[0];return window.isFocused() && window.webContents.isFocused();
  }));
  await create.focus();
  await until(() => create.evaluate(element => document.hasFocus() && document.activeElement === element));
  await create.evaluate(element => {
    window.nativeKeyboardAudit = [];
    window.nativeKeyboardListeners = ['keydown','keypress','keyup','click'].map(type => {
      const listener = event => {
        if (window.nativeKeyboardAudit.length < 20) window.nativeKeyboardAudit.push({ type, enter: event.key === 'Enter', button: event.target === element, trusted: event.isTrusted });
      };
      document.addEventListener(type, listener, true); return [type, listener];
    });
  });
  // XTEST delivers one Return through the actual focused X11 window. No CDP
  // key dispatch, DOM click, handler replacement or fallback activation.
  await promisify(execFile)('xdotool',['key','--clearmodifiers','Return'],{timeout:3000});
  try { await page.locator('#create-dialog[open]').waitFor({ state: 'visible', timeout: 5000 }); }
  catch {
    const evidence = await create.evaluate(element => ({ events: window.nativeKeyboardAudit, focused: document.hasFocus(), activeButton: document.activeElement === element, disabled: element.disabled, dialogOpen: Boolean(document.querySelector('#create-dialog')?.open) }));
    throw new Error('NATIVE_KEYBOARD_ACTIVATION_FAILED '+JSON.stringify(evidence));
  }
  const keys = await page.evaluate(() => {
    for (const [type, listener] of window.nativeKeyboardListeners) document.removeEventListener(type, listener, true);
    return window.nativeKeyboardAudit;
  });
  assert.ok(keys.some(event => event.type === 'keydown' && event.enter && event.button && event.trusted));
  assert.equal(keys.filter(event => event.type === 'click' && event.button && event.trusted).length, 1);
  console.log('Native keyboard activation PASS', JSON.stringify(keys));
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
  const rendererPid = await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].webContents.getOSProcessId());
  assert.ok(Number.isInteger(rendererPid) && rendererPid > 1 && rendererPid !== process.pid && rendererPid !== app.child.pid);
  // Inject a real OS process failure. Electron's Linux crash-request API may
  // return without terminating its renderer; that does not exercise shutdown.
  process.kill(rendererPid, 'SIGKILL');
  await stage('RENDERER_SIGNAL', () => until(async () => JSON.parse(await readFile(app.lifecycleTrace, 'utf8')).some(value => value.startsWith('renderer-gone-'))));
  await stage('RENDERER_DETACH', () => app.detach());
  let crashedExit;
  try { crashedExit = await new Budget(20000).run(app.exited); }
  catch { throw new Error('NATIVE_LIFECYCLE_RENDERER_EXIT_FAILED '+await readFile(app.lifecycleTrace, 'utf8')); }
  assert.equal(crashedExit.code, 0);
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
  for (const [locale, code] of [['en', 'RESOURCE_DIGEST_MISMATCH'], ['zh-Hans', 'RESOURCE_DIGEST_MISMATCH'], ['en', 'VERSION_MISMATCH']]) {
    const item = await fixture(t, { locale });
    if (code === 'RESOURCE_DIGEST_MISMATCH') item.config.node.sha256 = '0'.repeat(64);
    else item.config.webRevision = 'f'.repeat(40);
    await writeFile(item.filename, JSON.stringify(item.config), { mode: 0o600 });
    const app = await item.start(); await until(() => app.evaluate(() => globalThis.windowAudit.dialogs.length > 0));
    const dialogs = await app.evaluate(() => globalThis.windowAudit.dialogs);
    assert.equal(dialogs[0].message, message(code, locale));
    assert.ok(dialogs[0].detail.includes(code) && dialogs[0].detail.includes('desktop-diagnostics'));
    assert.equal(dialogs[0].detail.includes(item.root), false);
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
    try {
      await until(async () => app.status || app.debuggerWaiting || await app.evaluate(() => globalThis.windowAudit.dialogs.length > 0).catch(() => false), item.config.startTimeoutMs + item.config.stopTimeoutMs + 1000);
    } catch { throw new Error('NATIVE_OWNER_REJECTION_FAILED '+await readFile(app.lifecycleTrace, 'utf8')); }
    const ownerDialogs = await app.evaluate(() => globalThis.windowAudit.dialogs);
    assert.equal(ownerDialogs[0].message, message('OWNER_HELD'));
    assert.ok(ownerDialogs[0].detail.includes('OWNER_HELD'));
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
    const refused = await second.start();
    try { await until(() => refused.evaluate(() => globalThis.windowAudit.dialogs.length > 0), second.config.startTimeoutMs + second.config.stopTimeoutMs + 1000); }
    catch { throw new Error('NATIVE_PORT_REJECTION_FAILED '+await readFile(refused.lifecycleTrace, 'utf8')); }
    const portDialogs = await refused.evaluate(() => globalThis.windowAudit.dialogs);
    assert.equal(portDialogs[0].message, message('ENTRY_PORT_BUSY'));
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


test('native cleanup diagnostics distinguish existing and damaged markers without starting an owner', { timeout: 70000 }, async t => {
  for (const [locale, damaged] of [['en', false], ['zh-Hans', true]]) {
    const item = await fixture(t, { locale }), marker = join(item.data, 'desktop-runtime.guard.v1');
    const content = damaged ? 'private-guard-secret-canary' : JSON.stringify({ format: 'loopcast.desktop-guard.v1', identity: '1'.repeat(32) });
    await writeFile(marker, content, { mode: 0o600 });
    const app = await item.start(); await until(() => app.evaluate(() => globalThis.windowAudit.dialogs.length > 0));
    const code = damaged ? 'GUARD_UNSAFE' : 'DESKTOP_CLEANUP_UNCONFIRMED';
    const dialogs = await app.evaluate(() => globalThis.windowAudit.dialogs);
    assert.equal(dialogs[0].message, message(code, locale));
    assert.ok(dialogs[0].detail.includes(code) && dialogs[0].detail.includes('desktop-diagnostics'));
    assert.equal(dialogs[0].detail.includes(item.root), false);
    assert.equal(dialogs[0].detail.includes('private-guard-secret-canary'), false);
    assert.equal(await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows().length), 0);
    assert.equal((await app.quit()).code, 1);
    assert.equal(await readFile(marker, 'utf8'), content);
    await assert.rejects(stat(join(item.data, 'state.sqlite3')));
  }
});

test('native diagnostic failure has safe guidance and bounded repeated exit without a second owner', { timeout: 60000 }, async t => {
  const item = await fixture(t), path = join(item.root, 'diagnostic-fixture.mjs');
  const source = `import { createTreePort as original } from ${JSON.stringify(pathToFileURL(join(directory,'src/linux-tree.mjs')).href)};
import { HostError } from ${JSON.stringify(pathToFileURL(join(directory,'src/errors.mjs')).href)};
export function createTreePort(config) { const port=original(config);return {...port,async environment(value) { await port.environment(value);throw new HostError('DIAGNOSTICS_UNAVAILABLE'); }}; }`;
  await writeFile(path, source, { mode: 0o600 });
  item.config.treeModule = { path, sha256: createHash('sha256').update(source).digest('hex') };
  await writeFile(item.filename, JSON.stringify(item.config), { mode: 0o600 });
  const app = await item.start(); await until(() => app.evaluate(() => globalThis.windowAudit.dialogs.length > 0));
  const dialogs = await app.evaluate(() => globalThis.windowAudit.dialogs);
  assert.equal(dialogs[0].message, message('DIAGNOSTICS_UNAVAILABLE'));
  assert.ok(dialogs[0].detail.includes('DIAGNOSTICS_UNAVAILABLE'));
  assert.equal(dialogs[0].detail.includes(item.root), false);
  assert.equal(await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows().length), 0);
  await app.evaluate(({ app }) => { app.quit(); app.quit(); });
  await app.detach(); assert.equal((await new Budget(20000).run(app.exited)).code, 1);
  await assert.rejects(stat(join(item.data, 'state.sqlite3')));
  await assert.rejects(stat(join(item.data, 'desktop-runtime.guard.v1')));
});

test('real pre-ready failure gives safe offline instructions without initializing a fallback profile', { timeout: 40000 }, async t => {
  const item = await fixture(t);
  item.config.sessionDir = join(item.root, 'absent-session');
  await writeFile(item.filename, JSON.stringify(item.config), { mode: 0o600 });
  const child = spawn(executable, [directory, '--desktop-config', item.filename], { env: item.env, stdio: ['ignore','ignore','pipe'] });
  let output = ''; child.stderr.on('data', chunk => { if (output.length < 65536) output += chunk.toString(); });
  try {
    assert.equal(await new Budget(15000).run(new Promise(resolve => child.once('exit', resolve))), 1);
    assert.ok(output.includes('DIRECTORY_MISSING') && output.includes('desktop-diagnostics'));
    assert.equal(output.includes(item.root), false);
    assert.equal((await readdir(item.session)).length, 0);
    await assert.rejects(stat(join(item.data, 'state.sqlite3')));
  } finally { if (child.exitCode === null && child.signalCode === null) child.kill('SIGKILL'); }
});
