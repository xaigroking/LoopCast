// SPDX-License-Identifier: Apache-2.0
import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, chmodSync, writeFileSync, readFileSync, renameSync, mkdirSync, rmSync, symlinkSync } from 'node:fs';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import { ReceiptJournal, holdDirectory, readPrivate } from '../src/private-files.mjs';

function fixture(t) { const root = mkdtempSync(join(tmpdir(),'loopcast-private-')); t.after(() => rmSync(root,{recursive:true,force:true})); return root; }
test('private receipt journal authenticates immutable stages and rejects unfinished or unknown trees', t => {
  const root = fixture(t), journal = new ReceiptJournal(root); t.after(() => journal.close());
  const operation_id = 'a'.repeat(32);
  journal.append({operation_id,state:'PREPARED'});
  assert.throws(() => journal.assertClear(), /MAINTENANCE_UNCONFIRMED/);
  journal.append({operation_id,state:'COMPLETE'}); journal.assertClear();
  assert.throws(() => journal.append({operation_id,state:'COMPLETE'}), /EEXIST/);
  journal.append({operation_id,state:'UNKNOWN_TREE'});
  assert.throws(() => journal.assertClear(), /MAINTENANCE_UNCONFIRMED/);
});
test('receipt edits, a missing key, permissive files and directory substitutions fail closed', t => {
  const root = fixture(t), journal = new ReceiptJournal(root); t.after(() => journal.close());
  const name = join(root,'maintenance',`${'b'.repeat(32)}-COMPLETE.json`);
  journal.append({operation_id:'b'.repeat(32),state:'COMPLETE'});
  const raw = readFileSync(name,'utf8'); writeFileSync(name,raw.replace('COMPLETE','UNKNOWN_RESULT'));
  assert.throws(() => journal.records(), /MAINTENANCE_UNCONFIRMED/);
  writeFileSync(name,raw); chmodSync(name,0o644);
  assert.throws(() => journal.records(), /FILE_UNSAFE/);
  chmodSync(name,0o600);
  renameSync(join(root,'maintenance'),join(root,'held')); mkdirSync(join(root,'maintenance'),{mode:0o700});
  assert.throws(() => journal.records(), /PATH_UNSAFE/);
});
test('held private paths reject symlink ancestors and replaced directory objects', t => {
  const root = fixture(t), path = join(root,'folder'); mkdirSync(path,{mode:0o700});
  const held = holdDirectory(path); t.after(() => held.close());
  writeFileSync(join(path,'proof'),'a'.repeat(64),{mode:0o600});
  assert.equal(readPrivate(join(path,'proof')).length,64);
  renameSync(path,join(root,'moved')); symlinkSync(join(root,'moved'),path);
  assert.throws(() => held.check()); assert.throws(() => readPrivate(join(path,'proof')));
});
