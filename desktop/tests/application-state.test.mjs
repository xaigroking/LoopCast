// SPDX-License-Identifier: Apache-2.0
import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync,rmSync,readFileSync,writeFileSync,readdirSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { StateJournal } from '../src/application-state.mjs';

test('application stages form an authenticated no-replace chain with a committed pointer distinct from pending evidence',t=>{
  const root=mkdtempSync(join(tmpdir(),'loopcast-application-'));t.after(()=>rmSync(root,{recursive:true,force:true}));
  const journal=new StateJournal(root);t.after(()=>journal.close());const operation='a'.repeat(32);
  journal.append(operation,'PLANNED',{source:'original',target:'restored'});
  journal.append(operation,'PENDING_DURABLE',{});journal.append(operation,'TARGET_READY',{upload_allowed:false});
  assert.equal(journal.load().some(record=>record.phase==='COMMITTED'),false);
  journal.append(operation,'COMMITTED',{group:'g',binding:'restored'});
  const records=journal.load();assert.deepEqual(records.map(record=>record.sequence),[1,2,3,4]);
  assert.equal(records.at(-1).payload.binding,'restored');
  const name=join(root,'events',readdirSync(join(root,'events')).sort()[1]),raw=readFileSync(name,'utf8');
  writeFileSync(name,raw.replace('PENDING_DURABLE','FINISHED'));
  assert.throws(()=>journal.load(),/HANDOFF_UNCONFIRMED/);
  writeFileSync(name,raw);rmSync(name);assert.throws(()=>journal.load(),/HANDOFF_UNCONFIRMED/);
});
