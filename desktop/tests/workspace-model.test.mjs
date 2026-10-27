// SPDX-License-Identifier: Apache-2.0
import test from 'node:test';
import assert from 'node:assert/strict';
import { bindingCandidate, restoredObservation, reviewRestoredOpen } from '../src/workspace-model.mjs';
const h = char => char.repeat(64), id = char => char.repeat(32);
function setup() {
  const source={format:'loopcast.workspace-binding.candidate.v1',id:h('1'),role:'ORIGINAL',boot_sha256:h('a'),object_sha256:h('2'),object_family_sha256:h('b'),profile_sha256:h('3'),origin_port:28786,restore_id:null};
  const target={...source,id:h('4'),role:'RESTORED',object_sha256:h('5'),object_family_sha256:h('c'),profile_sha256:h('6'),origin_port:28787,restore_id:id('7')};
  const observation={format:'loopcast.restored-observation.candidate.v1',observation_scope:'NON_ATOMIC_OBSERVATIONS',state:'OBSERVED_GUARDED',start_authorized:false,upload_allowed:false,
    backup_id:id('8'),manifest_sha256:h('9'),restore_id:id('7'),execution_epoch:id('b'),guard_version:1,scope:'ALL_STUDIO_UPLOADS',schema_version:9,
    identity_scope:'KERNEL_BOOT',filesystem:'EXT4',boot_sha256:h('a'),object_sha256:h('5'),object_family_sha256:h('c')};
  return {operationId:id('c'),source,target,observation,bindings:[source,target],currentBoot:h('a'),sourceProfile:h('3'),targetProfile:h('6'),resourcesRevalidated:true};
}
const code = name => error => error.code === name;
test('known restored entity reviews its own profile, preserving the source binding',()=>{
  const input=setup(),before=JSON.stringify(input);const plan=reviewRestoredOpen(input);
  assert.equal(plan.state,'REVIEW_ONLY');assert.equal(plan.start_authorized,false);assert.equal(plan.upload_allowed,false);
  assert.equal(plan.profile_action,'REUSE_OWN_PRIVATE_PROFILE');assert.notEqual(plan.source_origin_port,plan.target_origin_port);
  assert.equal(JSON.stringify(input),before);assert.ok(Object.isFrozen(plan));
  assert.equal(JSON.stringify(plan).includes('profile_sha256'),false);
});
test('first review plans a new profile only after commit and rejects inferred old provenance',()=>{
  const input=setup();input.target=null;input.bindings=[input.source];input.targetProfile=null;input.newBindingId=h('d');input.newOriginPort=28787;
  assert.equal(reviewRestoredOpen(input).profile_action,'CREATE_PRIVATE_PROFILE_AFTER_COMMIT');
  assert.throws(()=>reviewRestoredOpen({...input,targetProfile:h('6')}),code('BINDING_CONFLICT'));
  assert.throws(()=>reviewRestoredOpen({...input,newOriginPort:28786}),code('BINDING_CONFLICT'));
});
test('missing or replaced profiles, copied entities and changed boots cannot inherit a binding',()=>{
  const input=setup();
  for(const change of [{sourceProfile:null},{sourceProfile:h('f')},{targetProfile:null},{targetProfile:h('f')}])
    assert.throws(()=>reviewRestoredOpen({...input,...change}),code('PROFILE_MISSING_OR_CHANGED'));
  assert.throws(()=>reviewRestoredOpen({...input,currentBoot:h('f')}),code('ENTITY_SCOPE_CHANGED'));
  assert.throws(()=>reviewRestoredOpen({...input,observation:{...input.observation,object_sha256:h('e')}}),code('TARGET_BINDING_MISMATCH'));
  assert.throws(()=>reviewRestoredOpen({...input,observation:{...input.observation,object_sha256:input.source.object_sha256}}),code('WORKSPACE_ALIAS'));
});
test('unknown execution globally blocks review, even when a different entity is selected',()=>{
  const input=setup();
  for(const executionState of ['STOPPING','UNKNOWN_TREE','PENDING','COMPLETE_WITHOUT_PROOF'])
    assert.throws(()=>reviewRestoredOpen({...input,executionState}),code('HANDOFF_UNCONFIRMED'));
  assert.throws(()=>reviewRestoredOpen({...input,resourcesRevalidated:false}),code('RESOURCE_REVALIDATION_REQUIRED'));
});
test('mount aliases and an already bound restore cannot be registered again as a fresh workspace',()=>{
  const input=setup();input.target=null;input.targetProfile=null;input.newBindingId=h('d');input.newOriginPort=28788;
  input.observation={...input.observation,object_sha256:h('e')};
  assert.throws(()=>reviewRestoredOpen(input),code('BINDING_CONFLICT'));
  input.observation={...input.observation,restore_id:id('e')};
  assert.throws(()=>reviewRestoredOpen(input),code('BINDING_CONFLICT'));
  input.observation={...input.observation,object_family_sha256:input.source.object_family_sha256};
  assert.throws(()=>reviewRestoredOpen(input),code('WORKSPACE_ALIAS'));
});
test('conflicting bindings, forged source facts and extra capability or path fields are rejected',()=>{
  const input=setup();
  assert.throws(()=>reviewRestoredOpen({...input,bindings:[...input.bindings,{...input.target,id:h('f')}]}),code('BINDING_CONFLICT'));
  assert.throws(()=>reviewRestoredOpen({...input,source:{...input.source,profile_sha256:h('f')},sourceProfile:h('f')}),code('BINDING_INVALID'));
  for(const change of [{path:'/private/canary'},{cookie:'never-record'},{start_authorized:true},{guard_version:0},{schema_version:6}])
    assert.throws(()=>restoredObservation({...input.observation,...change}),code('RESTORE_OBSERVATION_INVALID'));
  assert.throws(()=>bindingCandidate({...input.source,token:'never-record'}),code('BINDING_INVALID'));
});
