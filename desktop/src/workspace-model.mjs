// SPDX-License-Identifier: Apache-2.0
// Unconnected candidate models: neither trusted persistence nor launch admission.
const hex = (value, length) => typeof value === 'string' && new RegExp(`^[a-f0-9]{${length}}$`).test(value);
const port = value => Number.isInteger(value) && value >= 1 && value <= 65535;
const plain = value => value && Object.getPrototypeOf(value) === Object.prototype;
const exact = (value, keys) => plain(value) && Object.keys(value).sort().join(',') === [...keys].sort().join(',');
export class WorkspaceReviewError extends Error {
  constructor(code) { super(code); this.code = code; }
}
function refuse(code) { throw new WorkspaceReviewError(code); }
const observedKeys = ['format','observation_scope','state','start_authorized','upload_allowed','backup_id','manifest_sha256',
  'restore_id','execution_epoch','guard_version','scope','schema_version','identity_scope','filesystem','boot_sha256','object_sha256'];
export function restoredObservation(value) {
  if (!exact(value,observedKeys) || value.format !== 'loopcast.restored-observation.candidate.v1' ||
      value.observation_scope !== 'NON_ATOMIC_OBSERVATIONS' || value.state !== 'OBSERVED_GUARDED' ||
      value.start_authorized !== false || value.upload_allowed !== false || value.scope !== 'ALL_STUDIO_UPLOADS' ||
      ![7,8,9].includes(value.schema_version) || !Number.isSafeInteger(value.guard_version) || value.guard_version < 1 ||
      value.identity_scope !== 'KERNEL_BOOT' || !['EXT4','TMPFS'].includes(value.filesystem) ||
      !['backup_id','restore_id','execution_epoch'].every(key => hex(value[key],32)) ||
      !['manifest_sha256','boot_sha256','object_sha256'].every(key => hex(value[key],64))) refuse('RESTORE_OBSERVATION_INVALID');
  return Object.freeze({...value});
}
const bindingKeys = ['format','id','role','boot_sha256','object_sha256','profile_sha256','origin_port','restore_id'];
export function bindingCandidate(value) {
  if (!exact(value,bindingKeys) || value.format !== 'loopcast.workspace-binding.candidate.v1' ||
      !hex(value.id,64) || !['ORIGINAL','RESTORED'].includes(value.role) ||
      !['boot_sha256','object_sha256','profile_sha256'].every(key => hex(value[key],64)) || !port(value.origin_port) ||
      (value.role === 'ORIGINAL' ? value.restore_id !== null : !hex(value.restore_id,32))) refuse('BINDING_INVALID');
  return Object.freeze({...value});
}

export function reviewRestoredOpen({ operationId, source, target = null, observation, bindings,
  currentBoot, sourceProfile, targetProfile = null, newBindingId = null, newOriginPort = null,
  executionState = 'CLEAR', resourcesRevalidated = false }) {
  // Callers supply authenticated records in a future production integration.
  // This pure function cannot authenticate an object or clear an execution state.
  if (!hex(operationId,32) || !Array.isArray(bindings) || bindings.length > 1024) refuse('PLAN_INVALID');
  if (executionState !== 'CLEAR') refuse('HANDOFF_UNCONFIRMED');
  if (resourcesRevalidated !== true) refuse('RESOURCE_REVALIDATION_REQUIRED');
  source = bindingCandidate(source); observation = restoredObservation(observation);
  if (source.role !== 'ORIGINAL') refuse('ORIGINAL_BINDING_REQUIRED');
  if (!hex(currentBoot,64) || source.boot_sha256 !== currentBoot || observation.boot_sha256 !== currentBoot) refuse('ENTITY_SCOPE_CHANGED');
  if (!hex(sourceProfile,64) || sourceProfile !== source.profile_sha256) refuse('PROFILE_MISSING_OR_CHANGED');
  if (source.object_sha256 === observation.object_sha256) refuse('WORKSPACE_ALIAS');
  const known = bindings.map(bindingCandidate);
  const ids = new Set(), objects = new Set(), profiles = new Set(), ports = new Set();
  for (const item of known) {
    if (ids.has(item.id) || objects.has(item.object_sha256) || profiles.has(item.profile_sha256) || ports.has(item.origin_port)) refuse('BINDING_CONFLICT');
    ids.add(item.id); objects.add(item.object_sha256); profiles.add(item.profile_sha256); ports.add(item.origin_port);
  }
  const recordedSource = known.find(value => value.id === source.id);
  if (!recordedSource || bindingKeys.some(key => recordedSource[key] !== source[key])) refuse('BINDING_INVALID');
  let targetId, originPort, profileAction;
  if (target !== null) {
    target = bindingCandidate(target);
    const recorded = known.find(value => value.id === target.id);
    if (!recorded || bindingKeys.some(key => recorded[key] !== target[key]) || target.role !== 'RESTORED' ||
        target.object_sha256 !== observation.object_sha256 || target.boot_sha256 !== currentBoot ||
        target.restore_id !== observation.restore_id) refuse('TARGET_BINDING_MISMATCH');
    if (targetProfile !== target.profile_sha256) refuse('PROFILE_MISSING_OR_CHANGED');
    targetId=target.id; originPort=target.origin_port; profileAction='REUSE_OWN_PRIVATE_PROFILE';
  } else {
    // Unknown provenance is not a request to erase/rebind an old profile.
    if (objects.has(observation.object_sha256) || !hex(newBindingId,64) || ids.has(newBindingId) ||
        !port(newOriginPort) || ports.has(newOriginPort) || targetProfile !== null) refuse('BINDING_CONFLICT');
    targetId=newBindingId; originPort=newOriginPort; profileAction='CREATE_PRIVATE_PROFILE_AFTER_COMMIT';
  }
  return Object.freeze({ format:'loopcast.workspace-open-plan.candidate.v1', state:'REVIEW_ONLY',
    operation_id:operationId, source_binding:source.id, target_binding:targetId,
    source_entity:source.object_sha256, target_entity:observation.object_sha256,
    source_origin_port:source.origin_port, target_origin_port:originPort, profile_action:profileAction,
    backup_id:observation.backup_id, manifest_sha256:observation.manifest_sha256, restore_id:observation.restore_id,
    execution_epoch:observation.execution_epoch, guard_version:observation.guard_version,
    start_authorized:false, upload_allowed:false });
}
