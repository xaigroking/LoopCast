// SPDX-License-Identifier: Apache-2.0
// Pure candidate models. The native workflow connects only after adoption.
import { createHash } from 'node:crypto';
import { HostError } from './errors.mjs';
const hex32 = value => typeof value === 'string' && /^[a-f0-9]{32}$/.test(value);
const hex64 = value => typeof value === 'string' && /^[a-f0-9]{64}$/.test(value);
const revision = value => typeof value === 'string' && /^[a-f0-9]{40}$/.test(value);
const integer = (value, min = 0, max = Number.MAX_SAFE_INTEGER) => Number.isSafeInteger(value) && value >= min && value <= max;
const exact = (value, keys) => value && typeof value === 'object' && !Array.isArray(value) && Object.keys(value).sort().join(',') === [...keys].sort().join(',');
const schema = value => [6, 7, 8, 9].includes(value);
function invalid() { throw new HostError('PROTOCOL_INVALID'); }
function flat(bytes, max = 65536) {
  try {
    if (!Buffer.isBuffer(bytes) || bytes.length > max || bytes.includes(92) || bytes.some(value => value > 126 || value < 32 && value !== 10)) invalid();
    const raw = bytes.toString('ascii'), value = JSON.parse(raw);
    if (!value || typeof value !== 'object' || Array.isArray(value)) invalid();
    const names = [...raw.matchAll(/"([a-z0-9_]+)"\s*:/g)].map(match => match[1]);
    if (names.length !== Object.keys(value).length || new Set(names).size !== names.length) invalid();
    return value;
  } catch { invalid(); }
}
export function operationIdentity(capability) {
  if (!Buffer.isBuffer(capability) || capability.length !== 32) invalid();
  return createHash('sha256').update(capability).digest('hex').slice(0, 32);
}
export function parseCreatedBackup(bytes) {
  const value = flat(bytes);
  if (!exact(value, ['format','backup_id','manifest_sha256','schema_version','files','state','credentials_policy','audit']) ||
      value.format !== 'loopcast.backup.v1' || !hex32(value.backup_id) || !hex64(value.manifest_sha256) || !schema(value.schema_version) ||
      !integer(value.files, 1, 100000) || value.state !== 'VERIFIED_PRIVATE_BACKUP' || value.credentials_policy !== 'excluded_reauthorize' ||
      !['recorded','unavailable'].includes(value.audit)) invalid();
  return Object.freeze(value);
}
export function parseVerifiedBackup(bytes, expectedId) {
  const value = flat(bytes);
  if (!exact(value, ['state','backup_id','schema_version','files']) || value.state !== 'VERIFIED_PRIVATE_BACKUP' ||
      !hex32(value.backup_id) || expectedId !== undefined && value.backup_id !== expectedId || !schema(value.schema_version) || !integer(value.files,1,100000)) invalid();
  return Object.freeze(value);
}
export function parseRestoredBackup(bytes, expectedId) {
  const value = flat(bytes);
  if (!exact(value, ['state','restore_id','guard_version','scope','backup_id','upload_allowed','credentials_policy']) ||
      value.state !== 'RESTORED_GUARDED' || !hex32(value.restore_id) || !integer(value.guard_version,1) || value.scope !== 'ALL_STUDIO_UPLOADS' ||
      !hex32(value.backup_id) || value.backup_id !== expectedId || value.upload_allowed !== false || value.credentials_policy !== 'excluded_reauthorize') invalid();
  return Object.freeze(value);
}
const time = value => typeof value === 'string' && /^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?(?:Z|\+00:00)$/.test(value) && Number.isFinite(Date.parse(value));
function proofFields(created, sourceRevision, createdAt) {
  if (!hex32(created?.backup_id) || !hex64(created?.manifest_sha256) || !schema(created?.schema_version) || !revision(sourceRevision) || !time(createdAt)) invalid();
  return Object.freeze({ format:'loopcast.backup-proof.v1', backup_id:created.backup_id, manifest_sha256:created.manifest_sha256,
    schema_version:created.schema_version, source_revision:sourceRevision, created_at:createdAt });
}
export function backupProof(receipt) {
  if (receipt?.format !== 'loopcast.maintenance-receipt.v1' || receipt.state !== 'COMPLETE' || receipt.action !== 'create' || receipt.tree_confirmed !== true) invalid();
  return proofFields(receipt.package,receipt.source_revision,receipt.observed_at);
}
export function parseIndependentProof(bytes) {
  if (!Buffer.isBuffer(bytes) || bytes.length > 4096 || bytes.some(value => value > 126 || value < 32 && ![10,13].includes(value))) invalid();
  const raw = bytes.toString('ascii');
  if (/^[a-f0-9]{64}(?:\r?\n)?$/.test(raw)) return Object.freeze({ manifest_sha256:raw.trim(), backup_id:null, origin:'USER_INDEPENDENT_RECORD' });
  const value = flat(bytes,4096);
  if (!exact(value,['format','backup_id','manifest_sha256','schema_version','source_revision','created_at']) || value.format !== 'loopcast.backup-proof.v1') invalid();
  const checked = proofFields(value,value.source_revision,value.created_at);
  return Object.freeze({ ...checked, origin:'USER_INDEPENDENT_RECORD' });
}
export function resultReceipt({ operationId, action, state, sourceIdentity, configDigest, sourceRevision, treeConfirmed, code = 'OK', created = null, verified = null, expectedDigest = null, restored = null, targetEntity = null, observedAt = new Date().toISOString() }) {
  if (!hex32(operationId) || !['create','verify','restore'].includes(action) ||
      !['PREPARED','COMPLETE','CANCELLED_BEFORE_EXECUTION','UNKNOWN_RESULT','UNKNOWN_TREE','REJECTED'].includes(state) ||
      !hex64(sourceIdentity) || !hex64(configDigest) || !revision(sourceRevision) || typeof treeConfirmed !== 'boolean' ||
      !['OK','CANCELLED','STOP_UNCONFIRMED','PROTOCOL_INVALID','CONFIG_INVALID','START_TIMEOUT'].includes(code) || !time(observedAt)) invalid();
  if (state === 'COMPLETE' && !treeConfirmed || state === 'UNKNOWN_TREE' && treeConfirmed) invalid();
  let packageInfo = created ? {
    backup_id:created.backup_id, manifest_sha256:created.manifest_sha256, schema_version:created.schema_version,
    files:created.files, audit:created.audit,
  } : null;
  if (created) parseCreatedBackup(Buffer.from(JSON.stringify({ ...packageInfo, format:'loopcast.backup.v1', state:'VERIFIED_PRIVATE_BACKUP', credentials_policy:'excluded_reauthorize' })+'\n'));
  if (verified) {
    if (created || !hex64(expectedDigest)) invalid();
    parseVerifiedBackup(Buffer.from(JSON.stringify(verified)+'\n'));
    packageInfo={backup_id:verified.backup_id,manifest_sha256:expectedDigest,schema_version:verified.schema_version,files:verified.files,audit:'not_applicable'};
  }
  const restoreInfo = restored ? { restore_id:restored.restore_id, guard_version:restored.guard_version, backup_id:restored.backup_id, upload_allowed:restored.upload_allowed } : null;
  if (restored) parseRestoredBackup(Buffer.from(JSON.stringify({ ...restoreInfo, state:'RESTORED_GUARDED', scope:'ALL_STUDIO_UPLOADS', credentials_policy:'excluded_reauthorize' })+'\n'),restored.backup_id);
  if (state === 'COMPLETE' && (!packageInfo || action === 'create' && !created || action === 'restore' && (!restoreInfo || restoreInfo.backup_id !== packageInfo.backup_id))) invalid();
  if (packageInfo) Object.freeze(packageInfo);
  if (restoreInfo) Object.freeze(restoreInfo);
  if (targetEntity !== null) {
    if (!exact(targetEntity,['identity_scope','filesystem','boot_sha256','object_sha256','object_family_sha256']) ||
        targetEntity.identity_scope !== 'KERNEL_BOOT' || !['EXT4','TMPFS'].includes(targetEntity.filesystem) ||
        !['boot_sha256','object_sha256','object_family_sha256'].every(key=>hex64(targetEntity[key]))) invalid();
    targetEntity=Object.freeze({...targetEntity});
  }
  return Object.freeze({ format:'loopcast.maintenance-receipt.v1', operation_id:operationId, action, state,
    source_identity:sourceIdentity, config_digest:configDigest, source_revision:sourceRevision,
    tree_confirmed:treeConfirmed, code, observed_at:observedAt, package:packageInfo, restore:restoreInfo, target_entity:targetEntity });
}
