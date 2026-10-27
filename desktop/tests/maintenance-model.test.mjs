// SPDX-License-Identifier: Apache-2.0
import test from 'node:test';
import assert from 'node:assert/strict';
import { operationIdentity, parseCreatedBackup, parseVerifiedBackup, parseRestoredBackup, backupProof, parseIndependentProof, resultReceipt } from '../src/maintenance-model.mjs';
const created = { format:'loopcast.backup.v1', backup_id:'1'.repeat(32), manifest_sha256:'2'.repeat(64), schema_version:9, files:3, state:'VERIFIED_PRIVATE_BACKUP', credentials_policy:'excluded_reauthorize', audit:'unavailable' };
const encoded = value => Buffer.from(JSON.stringify(value)+'\n');
const complete = () => resultReceipt({operationId:'0'.repeat(32),action:'create',state:'COMPLETE',sourceIdentity:'6'.repeat(64),configDigest:'7'.repeat(64),sourceRevision:'5'.repeat(40),treeConfirmed:true,created});
test('existing maintenance result parsers require exact bounded safe outcomes', () => {
  assert.deepEqual(parseCreatedBackup(encoded(created)),created);
  for (const change of [{ files:0 },{ schema_version:99 },{ manifest_sha256:'self-signed-canary' },{ credentials_policy:'include' },{ audit:'private-error-canary' },{ path:'/private/path-canary' }]) assert.throws(()=>parseCreatedBackup(encoded({...created,...change})),/PROTOCOL_INVALID/);
  assert.throws(()=>parseCreatedBackup(Buffer.alloc(65537,65)),/PROTOCOL_INVALID/);
  assert.throws(()=>parseCreatedBackup(Buffer.from(JSON.stringify(created).replace('"files":3','"files":2,"files":3')+'\n')),/PROTOCOL_INVALID/);
  const verified={state:'VERIFIED_PRIVATE_BACKUP',backup_id:created.backup_id,schema_version:9,files:3};
  assert.equal(parseVerifiedBackup(encoded(verified),created.backup_id).backup_id,created.backup_id);
  assert.throws(()=>parseVerifiedBackup(encoded(verified),'3'.repeat(32)),/PROTOCOL_INVALID/);
  const restored={state:'RESTORED_GUARDED',restore_id:'4'.repeat(32),guard_version:1,scope:'ALL_STUDIO_UPLOADS',backup_id:created.backup_id,upload_allowed:false,credentials_policy:'excluded_reauthorize'};
  assert.equal(parseRestoredBackup(encoded(restored),created.backup_id).upload_allowed,false);
  assert.throws(()=>parseRestoredBackup(encoded({...restored,upload_allowed:true}),created.backup_id),/PROTOCOL_INVALID/);
});
test('proof input never promotes a backup manifest or arbitrary file to an independent record', () => {
  assert.equal(parseIndependentProof(encoded(backupProof(complete()))).manifest_sha256,created.manifest_sha256);
  assert.equal(parseIndependentProof(Buffer.from(created.manifest_sha256+'\n')).backup_id,null);
  assert.throws(()=>parseIndependentProof(encoded(created)),/PROTOCOL_INVALID/);
  assert.throws(()=>backupProof({...complete(),state:'UNKNOWN_RESULT'}),/PROTOCOL_INVALID/);
  assert.throws(()=>backupProof({...complete(),tree_confirmed:false}),/PROTOCOL_INVALID/);
  assert.throws(()=>parseIndependentProof(Buffer.from('private-record-canary')),/PROTOCOL_INVALID/);
  assert.throws(()=>parseIndependentProof(Buffer.alloc(64,0xe1)),/PROTOCOL_INVALID/);
});
test('candidate receipts are allowlisted and cannot convert unknown trees to completion', () => {
  const capability=Buffer.alloc(32,7),operationId=operationIdentity(capability);
  const input={operationId,action:'create',state:'COMPLETE',sourceIdentity:'6'.repeat(64),configDigest:'7'.repeat(64),sourceRevision:'8'.repeat(40),treeConfirmed:true,created,
    path:'/private/path-canary',token:'private-token-canary',stderr:'private-error-canary'};
  const receipt=resultReceipt(input),raw=JSON.stringify(receipt);
  for (const secret of ['/private/path-canary','private-token-canary','private-error-canary',capability.toString('hex')]) assert.equal(raw.includes(secret),false);
  assert.throws(()=>resultReceipt({...input,treeConfirmed:false}),/PROTOCOL_INVALID/);
  assert.equal(resultReceipt({...input,state:'UNKNOWN_TREE',treeConfirmed:false,created:null}).package,null);
});
