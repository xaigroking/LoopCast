// SPDX-License-Identifier: Apache-2.0
import { randomBytes } from 'node:crypto';
import { lstatSync } from 'node:fs';
import { dirname } from 'node:path';
import { EventEmitter } from 'node:events';
import { Budget, HostError } from './errors.mjs';
import { capture } from './protocol.mjs';
import { readConfig, loadTreePort } from './config.mjs';
import { DesktopHost } from './host.mjs';
import { holdDirectory, readPrivate, contains, hash, sameInode } from './private-files.mjs';
import { operationIdentity, parseCreatedBackup, parseVerifiedBackup, parseRestoredBackup, parseIndependentProof, resultReceipt, backupProof } from './maintenance-model.mjs';

export class MaintenanceCoordinator extends EventEmitter {
  constructor({ config, filename, configDigest, host, journal, freeze, showWorkspace, admission }) {
    super(); Object.assign(this, { config, filename, configDigest, host, journal, freeze, showWorkspace, admission });
    this.state = host.state === 'READY' ? 'READY' : 'OFFLINE';
    this.source = holdDirectory(config.dataDir); this.profile = holdDirectory(config.sessionDir);
    this.sourceIdentity = hash(JSON.stringify(this.source.identity));
    journal.assertClear();
  }
  transition(state) { this.state = state; this.emit('state', state); }
  reserve() {
    if (this.busy || !['READY', 'OFFLINE', 'COMPLETE'].includes(this.state)) throw new HostError('MAINTENANCE_UNCONFIRMED');
    this.journal.assertClear(); this.busy = true; this.cancelled = false; this.previousState = this.host.state === 'READY' ? 'READY' : 'OFFLINE';
    this.transition('SELECTING');
  }
  release() { this.plan?.capability.fill(0); this.plan?.parent?.close(); this.plan?.packageDirectory?.close(); this.plan = null; this.busy = false; this.emit('state', this.state); }
  abandon() { this.release(); this.transition(this.previousState); }
  protect(path) {
    const protectedPaths = [this.config.dataDir, this.config.sessionDir, this.config.nextRoot, dirname(this.journal.root.path), this.filename,
      ...['python', 'node', 'treeModule'].map(key => this.config[key].path), ...Object.values(this.config.configs ?? {})];
    if (protectedPaths.some(item => contains(item, path) || contains(path, item))) throw new HostError('PATH_UNSAFE');
  }
  prepare(action, { target, backup, proofPath, trustedReceipt } = {}) {
    if (!this.busy || this.state !== 'SELECTING' || !['create', 'verify', 'restore'].includes(action)) throw new HostError('PROTOCOL_INVALID');
    const capability = randomBytes(32), operationId = operationIdentity(capability);
    const plan = { action, operationId, capability, target, backup, parent: null, packageDirectory: null };
    try {
      this.source.check(); this.profile.check();
      if (action !== 'create') {
        this.protect(backup); plan.packageDirectory = holdDirectory(backup);
        if (trustedReceipt) {
          const known = this.journal.records().find(record => record.operation_id === trustedReceipt && record.state === 'COMPLETE' && ['create','verify'].includes(record.action));
          if (!known || !known.tree_confirmed || !known.package) throw new HostError('PROTOCOL_INVALID');
          plan.proof = { ...known.package, origin: 'PRIVATE_AUTHENTICATED_RECEIPT' };
        } else {
          this.protect(proofPath);
          if (contains(backup, proofPath)) throw new HostError('PATH_UNSAFE');
          plan.proof = parseIndependentProof(readPrivate(proofPath, 4096));
        }
      }
      if (action !== 'verify') {
        this.protect(target);
        if (backup && (contains(backup, target) || contains(target, backup))) throw new HostError('PATH_UNSAFE');
        plan.parent = holdDirectory(dirname(target));
        try { lstatSync(target); throw new HostError('PATH_UNSAFE'); } catch (error) { if (error.code !== 'ENOENT') throw error; }
      }
      this.plan = plan; this.transition('PLANNED');
      // Native main-process preview only. This object never crosses a renderer.
      return Object.freeze({ format: 'loopcast.maintenance-plan.v1', operation_id: operationId, action,
        source: this.config.dataDir, target, backup, expected_digest: plan.proof?.manifest_sha256,
        digest_origin: plan.proof?.origin, stops_workspace: action !== 'verify', source_revision: this.config.apiRevision });
    } catch (error) { plan.parent?.close(); plan.packageDirectory?.close(); capability.fill(0); throw error; }
  }
  cancel() { this.cancelled = true; this.controller?.abort(); }
  receipt(state, values = {}) {
    return resultReceipt({ operationId: this.plan.operationId, action: this.plan.action, state,
      sourceIdentity: this.sourceIdentity, configDigest: this.configDigest, sourceRevision: this.config.apiRevision,
      treeConfirmed: true, ...values });
  }
  async command(request, { cancellable = true } = {}) {
    const tree = await loadTreePort(this.config);
    if (typeof tree.maintenanceEnvironment !== 'function') throw new HostError('CAPABILITY_UNAVAILABLE');
    const budget = new Budget(this.config.startTimeoutMs, cancellable ? this.controller?.signal : undefined);
    const handle = tree.launch({ executable: this.config.python.path, argv: ['-I', '-m', 'loopcast.desktop_maintenance'],
      cwd: this.config.sessionDir, env: tree.maintenanceEnvironment(), stdio: ['pipe','pipe','pipe'], deadline: budget.end, signal: budget.signal });
    handle.child.stdin.on('error', () => {}); handle.child.stdin.end(JSON.stringify(request));
    let text, failure;
    try { text = await capture(handle, budget, 65536); } catch (error) { failure = error; }
    const cleanup = new Budget(this.config.stopTimeoutMs);
    let stopped;
    try { stopped = await cleanup.run(handle.closeTree(cleanup.end)); } catch { stopped = { confirmed: false }; }
    if (!stopped?.confirmed) { this.treeUnknown = true; throw new HostError('STOP_UNCONFIRMED'); }
    if (failure) throw failure;
    return Buffer.from(text);
  }
  async marker(action) {
    const value = JSON.parse(await this.command({ action, source: this.config.dataDir, source_identity: this.source.identity,
      capability: this.plan.capability.toString('hex') }, { cancellable: false }));
    if (Object.keys(value).sort().join(',') !== 'format,identity,state' || value.format !== 'loopcast.maintenance-marker.v1' ||
        value.identity !== this.plan.operationId || value.state !== action.toUpperCase()) throw new HostError('PROTOCOL_INVALID');
  }
  execute() {
    if (!this.busy || this.state !== 'PLANNED' || this.running) throw new HostError('PROTOCOL_INVALID');
    if (this.plan.action !== 'verify') this.host.gateway?.suspend();
    this.running = this.executeInternal().finally(() => { this.running = null; this.controller = null; this.plan?.capability.fill(0); this.release(); });
    return this.running;
  }
  async executeInternal() {
    const plan = this.plan; this.controller = new AbortController(); this.treeUnknown = false;
    let marked = false, started = false, terminal, applicationStarted = false;
    try {
      await readConfig(this.filename, { packaged: this.config.mode === 'release', expectedDigest: this.configDigest });
      const implementation = JSON.parse(await this.command({ action:'identity' }));
      if (implementation.format !== 'loopcast.platform.v1' || implementation.source_revision !== this.config.apiRevision ||
          this.config.apiRevision !== this.config.hostRevision || this.config.apiRevision !== this.config.webRevision) throw new HostError('VERSION_MISMATCH');
      this.source.check(); this.profile.check(); plan.parent?.check(); plan.packageDirectory?.check();
      this.journal.append(this.receipt('PREPARED'));
      if (plan.action !== 'verify') {
        if (this.admission) {
          try { await this.admission.begin(plan.operationId); applicationStarted = true; }
          catch (error) { this.treeUnknown = true; throw error; }
        }
        this.transition('STOPPING');
        this.host.gateway?.suspend();
        const stopped = this.host.stop();
        let flushError;
        try { await this.freeze(); } catch (e) { flushError = e; }
        const result = await stopped;
        if (!result?.confirmed || flushError) { this.treeUnknown = true; throw new HostError('STOP_UNCONFIRMED'); }
        this.transition('OFFLINE');
        if (this.cancelled) { terminal = this.receipt('CANCELLED_BEFORE_EXECUTION', { code: 'CANCELLED' }); return terminal; }
        // A missing response may mean begin already published its marker.
        marked = true; await this.marker('begin');
      }
      if (this.cancelled) { terminal = this.receipt('CANCELLED_BEFORE_EXECUTION', { code: 'CANCELLED' }); return terminal; }
      this.transition(plan.action === 'verify' ? 'VERIFYING' : 'EXECUTING');
      this.source.check(); plan.parent?.check(); plan.packageDirectory?.check();
      const request = plan.action === 'verify' ? { action: 'verify', backup: plan.backup, digest: plan.proof.manifest_sha256 } : {
        action: plan.action, source: this.config.dataDir, source_identity: this.source.identity,
        capability: plan.capability.toString('hex'), target: plan.target,
        parent_identity: plan.parent.identity,
        ...(plan.action === 'restore' ? { backup: plan.backup, digest: plan.proof.manifest_sha256 } : {}),
      };
      started = true; const bytes = await this.command(request);
      this.transition('CHECKING'); this.source.check(); this.profile.check(); plan.parent?.check(); plan.packageDirectory?.check();
      if (plan.action === 'create') {
        const created = parseCreatedBackup(bytes);
        const verified = parseVerifiedBackup(await this.command({ action: 'verify', backup: plan.target, digest: created.manifest_sha256 }), created.backup_id);
        if (verified.schema_version !== created.schema_version || verified.files !== created.files) throw new HostError('PROTOCOL_INVALID');
        terminal = this.receipt('COMPLETE', { created });
      } else {
        const verified = parseVerifiedBackup(await this.command({ action: 'verify', backup: plan.backup, digest: plan.proof.manifest_sha256 }), plan.proof.backup_id ?? undefined);
        const restored = plan.action === 'restore' ? parseRestoredBackup(bytes, verified.backup_id) : null;
        if (plan.action === 'verify') parseVerifiedBackup(bytes, verified.backup_id);
        let targetEntity = null;
        if (restored) {
          try { targetEntity = JSON.parse(await this.command({ action:'entity', path:plan.target })); }
          catch (error) { if (this.treeUnknown) throw error; }
        }
        terminal = this.receipt('COMPLETE', { verified, expectedDigest: plan.proof.manifest_sha256, restored, targetEntity });
      }
      return terminal;
    } catch (error) {
      if (plan.action !== 'verify' && this.host.state === 'READY') {
        try {
          const stopped = this.host.stop(); await this.freeze();
          if (!(await stopped)?.confirmed) this.treeUnknown = true;
        } catch { this.treeUnknown = true; }
      }
      terminal = this.receipt(this.treeUnknown ? 'UNKNOWN_TREE' : started || marked ? 'UNKNOWN_RESULT' : 'REJECTED',
        { treeConfirmed: !this.treeUnknown, code: this.treeUnknown ? 'STOP_UNCONFIRMED' : 'PROTOCOL_INVALID' });
      return terminal;
    } finally {
      try {
        if (terminal) this.journal.append(terminal);
        if (marked && !this.treeUnknown && terminal) await this.marker('finish');
        if (applicationStarted && terminal) await this.admission.finish(terminal);
        this.transition(this.treeUnknown ? 'QUARANTINED' : this.host.state === 'READY' ? 'READY' : 'COMPLETE');
      } catch { this.treeUnknown = true; try { if (terminal?.state !== 'UNKNOWN_TREE') this.journal.append(this.receipt('UNKNOWN_TREE', { treeConfirmed:false, code:'STOP_UNCONFIRMED' })); } catch {} this.transition('QUARANTINED'); }
    }
  }
  async returnOriginal() {
    if (this.busy || !['COMPLETE','OFFLINE'].includes(this.state)) throw new HostError('MAINTENANCE_UNCONFIRMED');
    this.journal.assertClear(); this.busy = true;
    try {
      this.source.check(); this.profile.check();
      const config = await readConfig(this.filename, { packaged: this.config.mode === 'release', expectedDigest: this.configDigest });
      if (config.dataDir !== this.source.path || config.sessionDir !== this.profile.path) throw new HostError('MANIFEST_CHANGED');
      Object.assign(this.config, config);
      this.transition('STARTING');
      this.host = new DesktopHost(this.config, await loadTreePort(this.config));
      const origin = await this.host.start(); await this.showWorkspace(this.host, origin);
      this.transition('READY');
    } catch (error) { const result = await this.host.stop(); this.transition(result?.confirmed ? 'OFFLINE' : 'QUARANTINED'); throw error; }
    finally { this.busy = false; this.emit('state', this.state); }
  }
  async stopForHandoff() {
    if (!this.busy || this.state !== 'SELECTING') throw new HostError('PROTOCOL_INVALID');
    this.transition('STOPPING'); this.host.gateway?.suspend();
    const stopped = this.host.stop(); let failure;
    try { await this.freeze(); } catch (error) { failure = error; }
    const result = await stopped;
    if (!result?.confirmed || failure) { this.treeUnknown = true; this.transition('QUARANTINED'); throw new HostError('STOP_UNCONFIRMED'); }
    this.transition('OFFLINE'); return { confirmed:true };
  }
  exportProof(operationId) {
    const receipt = this.journal.records().find(value => value.operation_id === operationId && value.state === 'COMPLETE');
    return Buffer.from(JSON.stringify(backupProof(receipt)) + '\n');
  }
  async close() { this.cancel(); await this.running; this.release(); const result = await this.host.stop(); this.source.close(); this.profile.close(); return { confirmed:result?.confirmed === true && this.treeUnknown !== true }; }
}
