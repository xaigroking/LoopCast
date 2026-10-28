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

const entryStarted = performance.now();
let window, host, config, browserSession, quitting = false, exiting = false, exitCode = 0, errorDialog, rendererGone = false;
let primaryError;
let coordinator, journal, instancePath, utility, replacingWindow = false;

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
  if (!quitting && !window.isDestroyed()) window.show();
}

async function freezeWorkspace() {
  showOffline(config.locale === 'zh-Hans' ? '离线维护' : 'Offline maintenance', config.locale === 'zh-Hans' ? '正在正常停止工作区…' : 'Stopping the workspace normally…');
  replacingWindow = true;
  try { window?.destroy(); window = undefined; } finally { replacingWindow = false; }
  browserSession?.flushStorageData();
  await new Budget(config.stopTimeoutMs).run(browserSession?.cookies.flushStore());
}

async function report(error) {
  primaryError ??= safeError(error);
  exitCode = 1;
  const controller = new AbortController(); errorDialog = controller;
  const timer = setTimeout(() => controller.abort(), 30000);
  try {
    await dialog.showMessageBox({ type: 'error', title: 'LoopCast', message: message(primaryError.code, config?.locale),
      detail: failureDetails(primaryError, config?.locale, error),
      buttons: [config?.locale === 'zh-Hans' ? '关闭' : 'Close'], noLink: true, signal: controller.signal });
  } finally { clearTimeout(timer); if (errorDialog === controller) errorDialog = undefined; }
}

function initialize() {
  // This application-wide directory contains lock metadata, never a profile.
  // Acquire before reading a second instance's config, file or URL arguments.
  instancePath = instanceDirectory(app.getPath('appData'));
  app.setPath('userData', instancePath);
  if (!app.requestSingleInstanceLock()) { app.exit(0); return; }
  let filename, bootstrap;
  try {
    filename = configPath(process.argv, { packaged: app.isPackaged, resourcesPath: process.resourcesPath });
    bootstrap = readBootstrap(filename, { packaged: app.isPackaged });
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
  app.on('second-instance', () => { if (window && !window.isDestroyed()) { if (window.isMinimized()) window.restore(); window.focus(); } });
  app.on('activate', () => { if (window && !window.isDestroyed()) window.focus(); else if (!quitting) app.quit(); });
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
      try {
        browserSession?.flushStorageData();
        await new Budget(config.stopTimeoutMs).run(browserSession?.cookies.flushStore());
      } catch { exitCode = 1; }
      try {
        const result = await stopped;
        if (result?.confirmed === false) { exitCode = 1; await report(new HostError('STOP_UNCONFIRMED')); }
      } catch { exitCode = 1; await report(new HostError('STOP_UNCONFIRMED')); }
      journal?.close();
      exiting = true;
      if (exitCode || rendererGone) app.exit(exitCode);
      else app.quit(); // Let Chromium finish its normal persistent-profile shutdown.
    })();
  });
  void (async () => {
    try {
      await app.whenReady();
      const entryBudget = new Budget(Math.max(1, entryStarted + config.startTimeoutMs - performance.now()));
      if (process.versions.electron !== '45.0.0') throw new HostError('VERSION_MISMATCH');
      config = await entryBudget.run(readConfig(filename, { packaged: app.isPackaged, expectedDigest: bootstrap.digest }));
      if (quitting || config.sessionDir !== bootstrap.sessionDir) throw new Error();
      const treePort = await entryBudget.run(loadTreePort(config));
      if (quitting) throw new Error();
      journal = new ReceiptJournal(instancePath); journal.assertClear();
      host = new DesktopHost(config, treePort, { startupDeadline: entryBudget.end });
      host.on('fault', error => {
        // Startup catch retains its original cause even if cleanup also fails.
        if (host.state === 'READY' && !quitting) { primaryError ??= safeError(error); app.quit(); }
      });
      const origin = await host.start();
      if (quitting) throw new Error();
      await entryBudget.run(showWorkspace(host, origin));
      coordinator = new MaintenanceCoordinator({ config, filename, configDigest:bootstrap.digest, host, journal,
        freeze:freezeWorkspace, showWorkspace });
      installMaintenanceMenu({ Menu, dialog, app, coordinator, locale:config.locale, report, showOffline });
    } catch (error) {
      if (!quitting) await report(error);
      app.quit();
    }
  })();
}

try { initialize(); }
catch (error) { dialog.showErrorBox('LoopCast', failureText(error)); app.exit(1); }
