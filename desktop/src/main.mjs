// SPDX-License-Identifier: Apache-2.0
import { app, BrowserWindow, dialog, session } from 'electron';
import { configPath, loadTreePort, readConfig } from './config.mjs';
import { DesktopHost } from './host.mjs';
import { Budget, HostError, message, safeError } from './errors.mjs';
import { secureWindow, webPreferences } from './window-policy.mjs';

let window, host, config, quitting = false, exiting = false;
if (!app.requestSingleInstanceLock()) app.exit(0);
else {
  app.on('second-instance', () => { if (window && !window.isDestroyed()) { if (window.isMinimized()) window.restore(); window.focus(); } });
  app.on('activate', () => { if (window && !window.isDestroyed()) window.focus(); else if (!quitting) app.quit(); });
  app.on('window-all-closed', () => app.quit());
  app.on('before-quit', event => {
    if (exiting) return;
    event.preventDefault();
    if (quitting) return;
    quitting = true;
    if (window && !window.isDestroyed()) window.destroy();
    void (async () => {
      const result = await host?.stop();
      if (result && !result.confirmed) dialog.showErrorBox('LoopCast', message('STOP_UNCONFIRMED', config?.locale));
      exiting = true; app.exit(result?.confirmed === false ? 1 : 0);
    })();
  });
  await app.whenReady();
  try {
    const entryStarted = performance.now(), entryBudget = new Budget(120000);
    if (process.versions.electron !== '45.0.0') throw new HostError('VERSION_MISMATCH');
    config = await entryBudget.run(readConfig(configPath(process.argv, { packaged: app.isPackaged, resourcesPath: process.resourcesPath }), { packaged: app.isPackaged }));
    entryBudget.end = Math.min(entryBudget.end, entryStarted + config.startTimeoutMs);
    app.setPath('userData', config.sessionDir);
    app.setPath('sessionData', config.sessionDir);
    if (quitting) throw new Error();
    const treePort = await entryBudget.run(loadTreePort(config));
    if (quitting) throw new Error();
    host = new DesktopHost(config, treePort, { startupDeadline: entryBudget.end });
    host.on('fault', () => { if (!quitting) app.quit(); });
    const origin = await host.start();
    if (quitting) throw new Error();
    // Keep stable browser write identities across restarts. HTTP cache cleanup
    // must never call clearStorageData or touch the Python data directory.
    const browserSession = session.fromPartition('persist:loopcast-desktop');
    window = new BrowserWindow({ show: false, width: 1280, height: 860, webPreferences: { ...webPreferences, session: browserSession } });
    window.removeMenu();
    secureWindow(window, browserSession, origin, host.gatewayToken);
    window.on('close', event => { if (!quitting) { event.preventDefault(); app.quit(); } });
    window.webContents.on('render-process-gone', () => app.quit());
    await entryBudget.run(window.loadURL(origin));
    if (!quitting && !window.isDestroyed()) window.show();
  } catch (error) {
    if (!quitting) dialog.showErrorBox('LoopCast', message(safeError(error).code, config?.locale));
    app.quit();
  }
}
