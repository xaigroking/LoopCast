// SPDX-License-Identifier: Apache-2.0
// Requires an installed same-source wheel and an actual Next standalone build.
import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, mkdir, writeFile, readFile, rm, stat } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { prepare } from '../scripts/prepare.mjs';
import { readConfig, loadTreePort } from '../src/config.mjs';
import { DesktopHost } from '../src/host.mjs';
import { createTreePort } from '../src/linux-tree.mjs';
import { Budget } from '../src/errors.mjs';
import { capture } from '../src/protocol.mjs';

const python = process.env.LOOPCAST_DESKTOP_PYTHON;
if (!python) throw new Error('An installed Python candidate is required; this integration test cannot silently skip.');
async function workspace(t) {
  const root = await mkdtemp(join(tmpdir(), 'loopcast 真实 desktop '));
  const dataDir = join(root, '业务 data'), sessionDir = join(root, '窗口 session');
  await mkdir(dataDir, { mode: 0o700 }); await mkdir(sessionDir, { mode: 0o700 });
  t.after(() => rm(root, { recursive: true, force: true }));
  return { root, dataDir, sessionDir };
}
async function gone(pid) {
  for (let index = 0; index < 300; index++) {
    try { await stat(`/proc/${pid}`); } catch { return; }
    await new Promise(resolve => setTimeout(resolve, 10));
  }
  assert.fail('An owned descendant remained after complete-tree cleanup.');
}

test('production subreaper collects a real setsid descendant after its leader exits', async t => {
  const paths = await workspace(t), port = createTreePort({ python: { path: python }, ...paths }), budget = new Budget(8000);
  const env = await port.environment({ ...paths, deadline: budget.end });
  const script = 'import subprocess,sys; p=subprocess.Popen([sys.executable,"-c","import time; time.sleep(60)"],start_new_session=True); print(p.pid,flush=True)';
  const owned = port.launch({ executable: python, argv: ['-I', '-c', script], cwd: paths.sessionDir, env: env.api, stdio: ['pipe', 'pipe', 'pipe'], deadline: budget.end });
  const pid = Number((await capture(owned, budget)).trim());
  assert.ok(Number.isInteger(pid) && pid > 1); assert.equal((await owned.closeTree(budget.end)).confirmed, true); await gone(pid);
});

test('output limit terminates the real tree and closes inherited pipes', async t => {
  const paths = await workspace(t), port = createTreePort({ python: { path: python }, ...paths }), budget = new Budget(3000);
  const env = await port.environment({ ...paths, deadline: budget.end });
  const script = 'import sys,time; sys.stdout.write("x"*100000); sys.stdout.flush(); time.sleep(60)';
  const owned = port.launch({ executable: python, argv: ['-I', '-c', script], cwd: paths.sessionDir, env: env.api, stdio: ['pipe', 'pipe', 'pipe'], deadline: budget.end });
  await assert.rejects(capture(owned, budget, 1024), /PROTOCOL_INVALID/);
  assert.equal((await owned.closeTree(budget.end)).confirmed, true);
});

async function manifest(t) {
  const paths = await workspace(t);
  const value = await prepare({ python, node: process.execPath, next: process.env.LOOPCAST_DESKTOP_NEXT,
    data: paths.dataDir, session: paths.sessionDir, port: '28786', revision: process.env.LOOPCAST_DESKTOP_REVISION });
  value.configs.media = process.env.LOOPCAST_DESKTOP_MEDIA_CONFIG;
  const filename = join(paths.root, 'desktop.json'); await writeFile(filename, JSON.stringify(value), { mode: 0o600 });
  return { ...paths, filename, config: await readConfig(filename) };
}

test('same-source actual backend, Next, host and production port start/stop/restart with stable write identity', { timeout: 60000 }, async t => {
  const item = await manifest(t);
  async function start() { const host = new DesktopHost(item.config, await loadTreePort(item.config)); t.after(() => host.stop()); await host.start(); return host; }
  let host = await start(); const origin = host.gateway.origin;
  const auth = () => ({ 'X-LoopCast-Desktop-Session': host.gatewayToken });
  const page = await fetch(origin, { headers: auth() }); assert.equal(page.status, 200);
  assert.ok(page.headers.get('content-security-policy')?.includes("nonce-"));
  const health = await fetch(origin+'/healthz', { headers: auth() }); assert.equal(health.status, 200);
  const competitor = new DesktopHost(item.config, await loadTreePort(item.config));
  await assert.rejects(competitor.start()); await competitor.stop();
  assert.equal((await fetch(origin+'/healthz', { headers: auth() })).status, 200);
  assert.equal((await fetch(origin+'/v1/runs')).status, 403);
  const key = 'same-lost-response-write';
  const input = { title: 'Managed restart fixture', mode: 'fixture', scenario: 'success', delay_ms: 20 };
  const headers = { ...auth(), Origin: origin, 'Content-Type': 'application/json', 'Idempotency-Key': key };
  const first = await fetch(origin+'/v1/runs', { method: 'POST', headers, body: JSON.stringify(input) });
  assert.equal(first.status, 202); const result = await first.json();
  const cookie = first.headers.get('set-cookie').split(';')[0];
  const id = result.run_id ?? result.id ?? result.run?.id;
  assert.ok(typeof id === 'string');
  assert.equal((await fetch(origin+`/runs/${id}`, { headers: auth() })).status, 200);
  const events = await fetch(origin+`/v1/runs/${id}/events?after=${result.last_event_id}`, { headers: auth(), signal: AbortSignal.timeout(5000) });
  assert.equal(events.status, 200); assert.ok(events.headers.get('content-type').includes('text/event-stream'));
  const reader = events.body.getReader(); assert.ok((await reader.read()).value.length > 0); await reader.cancel();
  const media = await readFile(process.env.LOOPCAST_DESKTOP_SAMPLE);
  const imported = await fetch(origin+'/v1/studio/assets/import?kind=video', { method: 'POST',
    headers: { ...auth(), Origin: origin, 'Content-Type': 'application/octet-stream', 'Idempotency-Key': 'desktop-real-media', 'X-File-Name': 'sample.mp4', 'X-Source': 'Owned generated contract clip' }, body: media });
  assert.equal(imported.status, 200); const asset = await imported.json();
  const ranged = await fetch(origin+`/v1/studio/assets/${asset.id}/content`, { headers: { ...auth(), Range: 'bytes=0-15' } });
  assert.equal(ranged.status, 206); assert.ok(ranged.headers.get('content-range')?.startsWith('bytes 0-15/'));
  assert.deepEqual(Buffer.from(await ranged.arrayBuffer()), media.subarray(0, 16));
  const snapshot = async () => (await fetch(origin+`/v1/runs/${id}`, { headers: auth() })).json();
  async function waitFor(predicate) {
    const deadline = performance.now() + 8000;
    while (performance.now() < deadline) {
      const value = await snapshot(); if (predicate(value)) return value;
      await new Promise(resolve => setTimeout(resolve, 20));
    }
    assert.fail('Real fixture did not reach its expected durable state.');
  }
  async function write(path, body, key) {
    const response = await fetch(origin+path, { method: 'POST', headers: { ...auth(), Origin: origin, 'Content-Type': 'application/json', 'Idempotency-Key': key }, body: JSON.stringify(body) });
    assert.ok(response.status === 200 || response.status === 202); return response.json();
  }
  let state = await waitFor(value => value.state === 'SUCCEEDED');
  const publicationPath = `/v1/runs/${id}/publications`;
  for (const [account, scenario, expected] of [['unknown', 'lost_response', 'UNKNOWN'], ['published', 'success', 'PUBLISHED']]) {
    state = await write(publicationPath, { platform: 'simulator', account, expected_version: state.state_version }, `intent-${account}`);
    const target = state.publications.find(value => value.account === account);
    await write(`${publicationPath}/${target.id}/dispatch`, { expected_version: state.state_version, scenario }, `dispatch-${account}`);
    state = await waitFor(value => value.publications.find(item => item.id === target.id)?.state === expected);
  }
  const durable = state.publications.map(({ id, state, attempts, receipts }) => ({ id, state, attempts, receipts }));
  assert.equal((await host.stop()).confirmed, true);
  await assert.rejects(stat(join(item.dataDir, 'desktop-runtime.guard.v1')));
  host = await start(); assert.equal(host.gateway.origin, origin);
  const retried = await fetch(origin+'/v1/runs', { method: 'POST', headers: { ...headers, ...auth(), Cookie: cookie }, body: JSON.stringify(input) });
  assert.equal(retried.status, 200); const again = await retried.json();
  assert.equal(again.run_id ?? again.id ?? again.run?.id, id);
  const recovered = await snapshot();
  assert.deepEqual(recovered.publications.map(({ id, state, attempts, receipts }) => ({ id, state, attempts, receipts })), durable);
  assert.ok(recovered.publications.every(target => target.allowed_actions.dispatch.enabled === false));
  assert.equal((await host.stop()).confirmed, true);
});

test('killing a real host leaves a durable barrier that rejects the next host and ordinary CLI', { timeout: 45000 }, async t => {
  const item = await manifest(t);
  const child = spawn(process.execPath, [fileURLToPath(new URL('../scripts/run-local.mjs', import.meta.url)), item.filename], { env: {}, stdio: ['pipe', 'ignore', 'pipe', 'pipe'] });
  t.after(() => { if (child.exitCode === null && child.signalCode === null) child.kill('SIGKILL'); });
  let buffer = '';
  await new Budget(15000).run(new Promise((resolve, reject) => {
    child.stdio[3].on('data', chunk => { buffer += chunk; if (buffer.includes('\n')) resolve(); });
    child.once('exit', () => reject(new Error('Real host failed to become ready.'))); child.stderr.resume();
  }));
  const exited = new Promise(resolve => child.once('exit', resolve)); child.kill('SIGKILL'); await exited;
  assert.ok(await stat(join(item.dataDir, 'desktop-runtime.guard.v1')));
  const host = new DesktopHost(item.config, await loadTreePort(item.config));
  await assert.rejects(host.start()); await host.stop();
  const service = spawn(python, ['-I', '-m', 'loopcast', 'serve', '--port', '0', '--data-dir', item.dataDir], { env: {}, stdio: ['ignore', 'pipe', 'ignore'] });
  let output = ''; service.stdout.on('data', chunk => { output += chunk; });
  const code = await new Budget(10000).run(new Promise(resolve => service.once('exit', resolve)));
  assert.notEqual(code, 0); assert.equal(output.includes('READY'), false);
  assert.ok(await stat(join(item.dataDir, 'desktop-runtime.guard.v1')));
});

test('real stopped workspace backup uses the production tree and independent digest without changing business facts', { timeout: 70000 }, async t => {
  const item = await manifest(t);
  const { parseCreatedBackup, parseVerifiedBackup } = await import('../src/maintenance-model.mjs');
  const { mkdir: makeDirectory } = await import('node:fs/promises');
  const packagePath = join(item.root, 'independent-package'), rejected = join(item.root, 'must-not-create');
  let host = new DesktopHost(item.config, await loadTreePort(item.config)); t.after(() => host.stop());
  await host.start();
  const origin = host.gateway.origin;
  const auth = () => ({ 'X-LoopCast-Desktop-Session':host.gatewayToken });
  const media = await readFile(process.env.LOOPCAST_DESKTOP_SAMPLE);
  const imported = await fetch(origin+'/v1/studio/assets/import?kind=video', { method:'POST',
    headers:{...auth(),Origin:origin,'Content-Type':'application/octet-stream','Idempotency-Key':'backup-registered-media','X-File-Name':'sample.mp4','X-Source':'Owned maintenance fixture'},body:media });
  assert.equal(imported.status,200);
  await makeDirectory(join(item.dataDir,'social','credentials'),{recursive:true,mode:0o700});
  await writeFile(join(item.dataDir,'social','credentials','excluded.json'),'credential-fixture-canary',{mode:0o600});
  const created = await fetch(origin+'/v1/runs', { method:'POST', headers:{...auth(),Origin:origin,'Content-Type':'application/json','Idempotency-Key':'backup-active-task'},
    body:JSON.stringify({title:'Active maintenance fixture',mode:'fixture',scenario:'slow',delay_ms:3000}) });
  assert.equal(created.status,202);const run=await created.json();
  const events=await fetch(origin+`/v1/runs/${run.run_id}/events?after=0`,{headers:auth()});
  const stream=events.body.getReader();await stream.read();
  const env={PATH:'/usr/bin:/bin',LANG:'C.UTF-8',HOME:item.sessionDir,TMPDIR:item.sessionDir};
  async function command(argv) {
    const port=createTreePort(item.config),budget=new Budget(12000);
    const owned=port.launch({executable:item.config.python.path,argv:['-I',...argv],cwd:item.sessionDir,env,stdio:['pipe','pipe','pipe'],deadline:budget.end});
    try { return await capture(owned,budget,32768); }
    finally { assert.equal((await owned.closeTree(budget.end)).confirmed,true); }
  }
  await assert.rejects(command(['-m','loopcast','backup','create','--data-dir',item.dataDir,'--output',rejected]));
  await assert.rejects(stat(rejected));
  const streamClosed=(async()=>{while(!(await stream.read()).done){} })().catch(()=>{});
  assert.equal((await host.stop()).confirmed,true);await new Budget(3000).run(streamClosed);
  await assert.rejects(stat(join(item.dataDir,'desktop-runtime.guard.v1')));
  const digestScript = `import hashlib,json,sqlite3,sys
from contextlib import closing
with closing(sqlite3.connect('file:'+sys.argv[1]+'/state.sqlite3?mode=ro',uri=True)) as db:
 db.execute('BEGIN')
 rows={name:db.execute('SELECT * FROM "'+name.replace('"','""')+'"').fetchall() for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name!='backup_audit' ORDER BY name")}
 encoded=json.dumps(rows,sort_keys=True,default=lambda value:hashlib.sha256(value).hexdigest() if isinstance(value,bytes) else None).encode()
 print(json.dumps({'facts':hashlib.sha256(encoded).hexdigest(),'audit':db.execute('SELECT COUNT(*) FROM backup_audit').fetchone()[0]}))`;
  const before=JSON.parse(await command(['-c',digestScript,item.dataDir]));
  const result=parseCreatedBackup(Buffer.from(await command(['-m','loopcast','backup','create','--data-dir',item.dataDir,'--output',packagePath])));
  const after=JSON.parse(await command(['-c',digestScript,item.dataDir]));
  assert.equal(after.facts,before.facts);
  if(result.audit==='recorded')assert.equal(after.audit,before.audit+1);
  else assert.ok(after.audit===before.audit || after.audit===before.audit+1);
  await assert.rejects(stat(join(packagePath,'social')));
  await assert.rejects(stat(join(packagePath,'service.lock')));
  const packageManifest=JSON.parse(await readFile(join(packagePath,'manifest.json'),'utf8'));
  assert.ok(packageManifest.files.some(value=>value.path.startsWith('library/')));
  // The expected digest is the authoritative create result, never the package's own hash.
  host=new DesktopHost(item.config,await loadTreePort(item.config));await host.start();
  const verified=parseVerifiedBackup(Buffer.from(await command(['-m','loopcast','backup','verify','--backup',packagePath,'--expected-manifest-sha256',result.manifest_sha256])),result.backup_id);
  assert.equal(verified.backup_id,result.backup_id);
  assert.equal((await fetch(origin+'/healthz',{headers:auth()})).status,200);
  assert.equal((await host.stop()).confirmed,true);
});

test('installed backup and restore CLI feed an unconnected read-only workspace review', { timeout: 30000 }, async t => {
  const item=await manifest(t), packagePath=join(item.root,'package'), restoredPath=join(item.root,'restored');
  const {parseCreatedBackup,parseRestoredBackup}=await import('../src/maintenance-model.mjs');
  const {restoredObservation,reviewRestoredOpen}=await import('../src/workspace-model.mjs');
  const env={PATH:'/usr/bin:/bin',LANG:'C.UTF-8',HOME:item.sessionDir,TMPDIR:item.sessionDir};
  async function command(argv) {
    const port=createTreePort(item.config),budget=new Budget(10000);
    const owned=port.launch({executable:item.config.python.path,argv:['-I',...argv],cwd:item.sessionDir,env,stdio:['pipe','pipe','pipe'],deadline:budget.end});
    try{return await capture(owned,budget,32768);}
    finally{assert.equal((await owned.closeTree(budget.end)).confirmed,true);}
  }
  await command(['-c','from loopcast.storage import Store; import sys; Store(sys.argv[1])',item.dataDir]);
  const created=parseCreatedBackup(Buffer.from(await command(['-m','loopcast','backup','create','--data-dir',item.dataDir,'--output',packagePath])));
  const restored=parseRestoredBackup(Buffer.from(await command(['-m','loopcast','restore','--backup',packagePath,'--expected-manifest-sha256',created.manifest_sha256,'--data-dir',restoredPath])),created.backup_id);
  const script=`import json,os,sys
from loopcast.workspace_inspection import inspect_restored,directory_identity
from loopcast.ownership import directory
result=inspect_restored(sys.argv[1],expected_digest=sys.argv[2],expected_backup_id=sys.argv[3],expected_restore_id=sys.argv[4])
identities=[]
for path in sys.argv[5:]:
 _,fd=directory(path)
 try: identities.append(directory_identity(fd))
 finally: os.close(fd)
print(json.dumps({'observation':result,'identities':identities}))`;
  const value=JSON.parse(await command(['-c',script,restoredPath,created.manifest_sha256,created.backup_id,restored.restore_id,item.dataDir,item.sessionDir]));
  const observation=restoredObservation(value.observation);
  const source={format:'loopcast.workspace-binding.candidate.v1',id:'1'.repeat(64),role:'ORIGINAL',boot_sha256:value.identities[0].boot_sha256,
    object_sha256:value.identities[0].object_sha256,object_family_sha256:value.identities[0].object_family_sha256,profile_sha256:value.identities[1].object_sha256,origin_port:28786,restore_id:null};
  const plan=reviewRestoredOpen({operationId:'2'.repeat(32),source,observation,bindings:[source],currentBoot:source.boot_sha256,
    sourceProfile:source.profile_sha256,newBindingId:'3'.repeat(64),newOriginPort:28787,resourcesRevalidated:true});
  assert.equal(plan.start_authorized,false);assert.equal(plan.upload_allowed,false);assert.equal(plan.profile_action,'CREATE_PRIVATE_PROFILE_AFTER_COMMIT');
  assert.equal(plan.restore_id,restored.restore_id);
  await assert.rejects(stat(join(restoredPath,'session')));
});
