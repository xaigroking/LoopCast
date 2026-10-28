// SPDX-License-Identifier: Apache-2.0
import { app, BrowserWindow, dialog, session, Menu } from 'electron';
import { configPath, loadTreePort, readConfig } from './config.mjs';
import { readBootstrap, instanceDirectory } from './bootstrap.mjs';
import { DesktopHost } from './host.mjs';
import { Budget, HostError, message, safeError, failureDetails, failureText } from './errors.mjs';
import { secureWindow, webPreferences } from './window-policy.mjs';
import { ReceiptJournal } from './private-files.mjs';
import { MaintenanceCoordinator } from './maintenance.mjs';
import { installMaintenanceMenu } from './maintenance-menu.mjs';
import { readFileSync } from 'node:fs';
import { Socket } from 'node:net';
import { hash, contains } from './private-files.mjs';
import { ApplicationLease, applicationRoot } from './application-state.mjs';
import { PrivateChannel } from './private-channel.mjs';
import { WorkerAdmission } from './worker-admission.mjs';
import { nativeWorkspaceActions } from './workspace-native.mjs';
import { validateDirectWorkspace } from './supervisor.mjs';

const entryStarted = performance.now();
let window, host, config, browserSession, quitting = false, exiting = false, exitCode = 0, errorDialog, rendererGone = false;
let primaryError;
let coordinator, journal, instancePath, utility, replacingWindow = false;
let supervision, channel, lease, admission;
let privateTreeUnknown = false;

function showOffline(title, label) {
  if (quitting) return;
  if (!utility || utility.isDestroyed()) {
    const utilitySession = session.fromPartition('loopcast-maintenance-utility');
    utilitySession.setPermissionRequestHandler((_contents, _permission, callback) => callback(false));
    utilitySession.setPermissionCheckHandler(() => false);
    utilitySession.webRequest.onBeforeRequest({ urls:['*://*/*'] }, (_details, callback) => callback({ cancel:true }));
    utility = new BrowserWindow({ width:680, height:240, webPreferences:{ ...webPreferences, session:utilitySession } });
    utility.webContents.setWindowOpenHandler(() => ({ action:'deny' }));
    utility.webContents.on('will-navigate', event => event.preventDefault());
    utility.on('close', event => { if (!quitting && !replacingWindow) { event.preventDefault(); app.quit(); } });
    utility.webContents.on('render-process-gone', () => { rendererGone = true; app.quit(); });
  }
  utility.setTitle(title);
  // Static local content has no script, profile, IPC or gateway capability.
  void utility.loadURL('data:text/html;charset=utf-8,' + encodeURIComponent(`<html><head><meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'"></head><body style="font:18px system-ui;padding:28px"><h1>LoopCast</h1><p>${label.replaceAll('&','&amp;').replaceAll('<','&lt;')}</p></body></html>`)).catch(error => {
    if (error?.code !== 'ERR_ABORTED' && !quitting && !replacingWindow) { rendererGone = true; app.quit(); }
  });
}

async function showWorkspace(current, origin) {
  host = current;
  host.on('fault', error => { if (host.state === 'READY' && !quitting) { primaryError ??= safeError(error); app.quit(); } });
  browserSession = session.fromPartition('persist:loopcast-desktop');
  window = new BrowserWindow({ show:false, width:1280, height:860, webPreferences:{ ...webPreferences, session:browserSession } });
  secureWindow(window, browserSession, origin, host.gatewayToken);
  window.on('close', event => { if (!quitting && !replacingWindow) { event.preventDefault(); app.quit(); } });
  window.webContents.on('render-process-gone', () => { rendererGone = true; app.quit(); });
  await new Budget(config.startTimeoutMs).run(window.loadURL(origin));
  replacingWindow = true;
  try { utility?.destroy(); utility = undefined; } finally { replacingWindow = false; }
  if (!quitting && !window.isDestroyed() && (!supervision || supervision.committed)) window.show();
  if (supervision?.binding.role === 'RESTORED') window.setTitle(config.locale === 'zh-Hans' ? 'LoopCast · 受控恢复工作区 · 上传仍受恢复屏障限制' : 'LoopCast · Controlled restored workspace · Uploads remain guarded');
}

async function freezeWorkspace() {
  showOffline(config.locale === 'zh-Hans' ? '离线维护' : 'Offline maintenance', config.locale === 'zh-Hans' ? '正在正常停止工作区…' : 'Stopping the workspace normally…');
  replacingWindow = true;
  try { window?.destroy(); window = undefined; } finally { replacingWindow = false; }
  browserSession?.flushStorageData();
  await new Budget(config.stopTimeoutMs).run(browserSession?.cookies.flushStore());
}

async function report(error, fatal = true) {
  const shown = fatal ? (primaryError ??= safeError(error)) : safeError(error);
  if (fatal) exitCode = 1;
  const controller = new AbortController(); errorDialog = controller;
  const timer = setTimeout(() => controller.abort(), 30000);
  try {
    await dialog.showMessageBox({ type: 'error', title: 'LoopCast', message: message(shown.code, config?.locale),
      detail: failureDetails(shown, config?.locale, error),
      buttons: [config?.locale === 'zh-Hans' ? '关闭' : 'Close'], noLink: true, signal: controller.signal });
  } finally { clearTimeout(timer); if (errorDialog === controller) errorDialog = undefined; }
}

function initialize() {
  // This application-wide directory contains lock metadata, never a profile.
  // Acquire before reading a second instance's config, file or URL arguments.
  instancePath = instanceDirectory(app.getPath('appData'));
  app.setPath('userData', instancePath);
  if (!app.requestSingleInstanceLock()) { app.exit(0); return; }
  if (process.argv.includes('--focus-only')) { app.exit(0); return; }
  let filename, bootstrap;
  try {
    if (process.argv.includes('--supervised-worker')) {
      const raw = readFileSync(0);
      if (raw.length > 65536) throw new HostError('PROTOCOL_INVALID');
      supervision = JSON.parse(raw.toString('utf8')); raw.fill(0);
      if (Object.keys(supervision).sort().join(',') !== 'admission,binding,challenge,config_digest,filename,format,journal_root,lease' ||
          supervision.format !== 'loopcast.supervised-worker.v1' || supervision.journal_root !== applicationRoot() ||
          !/^[a-f0-9]{64}$/.test(supervision.challenge) || !/^[a-f0-9]{64}$/.test(supervision.lease)) throw new HostError('PROTOCOL_INVALID');
      filename = supervision.filename;
      channel = new PrivateChannel(new Socket({ fd:3, readable:true, writable:true }), { onClosed:() => { if (!exiting) app.quit(); },
        onRequest:async(type,payload) => { if (type !== 'quit' || payload.challenge !== supervision.challenge) throw new HostError('PROTOCOL_INVALID'); app.quit(); return {}; } });
    } else filename = configPath(process.argv, { packaged: app.isPackaged, resourcesPath: process.resourcesPath });
    bootstrap = readBootstrap(filename, { packaged: app.isPackaged, binding:supervision?.binding });
    if (supervision && bootstrap.digest !== supervision.config_digest) throw new HostError('MANIFEST_CHANGED');
    config = bootstrap.config;
    if (app.isReady()) throw new HostError('CONFIG_INVALID');
    app.setPath('userData', bootstrap.sessionDir);
    app.setPath('sessionData', bootstrap.sessionDir);
  } catch (error) {
    // Before ready, Electron's Linux error box reports safe text to stderr and
    // does not initialize a fallback browser profile or business owner.
    dialog.showErrorBox('LoopCast', failureText(error, config?.locale));
    app.exit(1); return;
  }
  app.on('second-instance', () => { const current=window??utility; if (current && !current.isDestroyed()) { if (current.isMinimized()) current.restore(); current.focus(); } });
  app.on('activate', () => { const current=window??utility; if (current && !current.isDestroyed()) current.focus(); else if (!quitting) app.quit(); });
  app.on('window-all-closed', () => { if (!replacingWindow) app.quit(); });
  app.on('before-quit', event => {
    if (exiting) return;
    event.preventDefault();
    errorDialog?.abort();
    if (quitting) return;
    quitting = true;
    if (window && !window.isDestroyed()) window.destroy();
    utility?.destroy();
    void (async () => {
      // Start STOP before awaiting Chromium; a stalled profile flush must never
      // leave the business owner running. Both operations have finite bounds.
      const stopped = coordinator ? coordinator.close() : host?.stop();
      let confirmed = !privateTreeUnknown;
      try {
        browserSession?.flushStorageData();
        await new Budget(config.stopTimeoutMs).run(browserSession?.cookies.flushStore());
      } catch { exitCode = 1; confirmed = false; }
      try {
        const result = await stopped;
        if (result?.confirmed === false) { exitCode = 1; confirmed = false; await report(new HostError('STOP_UNCONFIRMED')); }
      } catch { exitCode = 1; confirmed = false; await report(new HostError('STOP_UNCONFIRMED')); }
      if (channel) {
        try { await channel.request('stopped', { challenge:supervision.challenge, confirmed }); } catch { exitCode = 1; confirmed = false; }
      }
      admission?.close();
      journal?.close();
      if (lease) { try { if (!(await lease.close({ confirmed })).confirmed) exitCode = 1; } catch { exitCode = 1; } }
      exiting = true;
      channel?.close();
      if (exitCode || rendererGone) app.exit(exitCode);
      else app.quit(); // Let Chromium finish its normal persistent-profile shutdown.
    })();
  });
  void (async () => {
    try {
      await app.whenReady();
      const entryBudget = new Budget(Math.max(1, entryStarted + config.startTimeoutMs - performance.now()));
      if (process.versions.electron !== '45.0.0') throw new HostError('VERSION_MISMATCH');
      const resources = await entryBudget.run(readConfig(filename, { packaged: app.isPackaged, expectedDigest: bootstrap.digest }));
      config = supervision ? { ...resources, dataDir:bootstrap.config.dataDir, sessionDir:bootstrap.sessionDir,
        gatewayPort:supervision.binding.origin_port, configs:supervision.binding.role === 'ORIGINAL' ? resources.configs : {},
        admission:supervision.admission, leaseCapability:supervision.lease } : resources;
      config.onUnconfirmedTree = () => { privateTreeUnknown = true; host?.gateway?.suspend(); if (coordinator) coordinator.treeUnknown = true; app.quit(); };
      if (quitting || config.sessionDir !== bootstrap.sessionDir) throw new Error();
      if (!supervision) {
        config.electron ??= { path:process.execPath, sha256:hash(readFileSync(process.execPath)) };
        lease = await entryBudget.run(ApplicationLease.acquire({ config })); config.leaseCapability = lease.capability;
        if ([config.dataDir,config.sessionDir,config.nextRoot].some(path=>contains(path,lease.root)||contains(lease.root,path))) throw new HostError('PATH_UNSAFE');
      }
      const treePort = await entryBudget.run(loadTreePort(config));
      if (quitting) throw new Error();
      journal = new ReceiptJournal(supervision?.journal_root ?? lease.root); journal.assertClear();
      admission = new WorkerAdmission(config, { lease, channel, challenge:supervision?.challenge, receipts:journal });
      if (!channel) await entryBudget.run(validateDirectWorkspace(config,bootstrap.digest,admission.journal));
      host = new DesktopHost(config, treePort, { startupDeadline: entryBudget.end });
      host.on('fault', error => {
        // Startup catch retains its original cause even if cleanup also fails.
        if (host.state === 'READY' && !quitting) { primaryError ??= safeError(error); app.quit(); }
      });
      const origin = await host.start();
      if (quitting) throw new Error();
      await entryBudget.run(showWorkspace(host, origin));
      coordinator = new MaintenanceCoordinator({ config, filename, configDigest:bootstrap.digest, host, journal,
        freeze:freezeWorkspace, showWorkspace, admission });
      if (channel) {
        let guard = null;
        if (supervision.binding.role === 'RESTORED') {
          const response = await entryBudget.run(fetch(origin+'/v1/operations/status', { headers:{'X-LoopCast-Desktop-Session':host.gatewayToken} }));
          const status = await entryBudget.run(response.json());
          if (response.status !== 200 || status.restore_guard?.state !== 'ACTIVE' || status.upload_allowed_by_recovery !== false) throw new HostError('HANDOFF_UNCONFIRMED');
          guard = { state:status.restore_guard.state, restore_id:status.restore_guard.restore_id, version:status.restore_guard.version,
            execution_epoch:status.restore.execution_epoch, upload_allowed:false };
        }
        const ready = await entryBudget.run(channel.request('ready', { challenge:supervision.challenge, guard }));
        if (ready.committed !== true) throw new HostError('HANDOFF_UNCONFIRMED');
        config.admission = null; supervision.admission = null; supervision.committed = true; window.show();
      }
      const workspace = channel ? nativeWorkspaceActions({ app, dialog, channel, challenge:supervision.challenge,
        config, coordinator, role:supervision.binding.role, report:error=>report(error,false) }) : undefined;
      installMaintenanceMenu({ Menu, dialog, app, coordinator, locale:config.locale, report:error=>report(error,false), showOffline, workspace });
    } catch (error) {
      if (!quitting) await report(error);
      app.quit();
    }
  })();
}

try { initialize(); }
catch (error) { dialog.showErrorBox('LoopCast', failureText(error)); app.exit(1); }
