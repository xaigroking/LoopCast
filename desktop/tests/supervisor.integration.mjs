// SPDX-License-Identifier: Apache-2.0
import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp,mkdir,writeFile,readFile,rm,stat,rename,symlink,readdir,cp } from 'node:fs/promises';
import { renameSync,symlinkSync,writeFileSync } from 'node:fs';
import { createServer } from 'node:net';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { prepare } from '../scripts/prepare.mjs';
import { DesktopSupervisor } from '../src/supervisor.mjs';
import { applicationFixture } from './application-fixture.mjs';
import { launchElectron } from './electron-driver.mjs';
import { Budget } from '../src/errors.mjs';
import { hash } from '../src/private-files.mjs';

const executable=process.env.LOOPCAST_ELECTRON_BINARY;
if(!executable||!process.env.LOOPCAST_DESKTOP_PYTHON)throw new Error('An actual verified Electron and installed same-source candidate are required');
async function until(work,timeout=30000){const budget=new Budget(timeout);while(true){const value=await budget.run(Promise.resolve().then(work));if(value)return value;await budget.run(new Promise(resolve=>setTimeout(resolve,20)));}}

async function fixture(t,{pauseTarget=false,locale='en'}={}){
  const cleanupApplication=await applicationFixture(),root=await mkdtemp(join(tmpdir(),'loopcast-supervisor-'));
  const data=join(root,'data'),profile=join(root,'original-profile'),home=join(root,'home');
  for(const path of [data,profile,home,join(home,'config')])await mkdir(path,{mode:0o700});
  const config=await prepare({python:process.env.LOOPCAST_DESKTOP_PYTHON,node:process.execPath,electron:executable,
    next:process.env.LOOPCAST_DESKTOP_NEXT,data,session:profile,port:'28789',revision:process.env.LOOPCAST_DESKTOP_REVISION,locale});
  const wrapper=join(root,'inspected-owned-port.mjs'),production=new URL('../src/linux-tree.mjs',import.meta.url).href;
  // External test tooling adds debugger listeners only. Production ownership,
  // cleanup, default sandbox, private pipes and every maintenance handler run.
  await writeFile(wrapper,`import{createTreePort as production}from ${JSON.stringify(production)};let workers=0,observer;
export function observeLaunch(fn){observer=fn;}
export function createTreePort(config){const tree=production(config),launch=tree.launch.bind(tree);tree.launch=spec=>{const owned=launch(spec.executable===${JSON.stringify(executable)}?{...spec,argv:[${pauseTarget}&&workers++>0?'--inspect-brk=0':'--inspect=0','--remote-debugging-port=0',...spec.argv]}:spec);observer?.(owned,spec);return owned;};return tree;}`,{mode:0o600});
  config.treeModule={path:wrapper,sha256:hash(await readFile(wrapper))};
  const filename=join(root,'desktop.json');await writeFile(filename,JSON.stringify(config),{mode:0o600});
  const environment={PATH:'/usr/bin:/bin',LANG:'C.UTF-8',HOME:home,XDG_CONFIG_HOME:join(home,'config'),DISPLAY:process.env.DISPLAY};
  for(const key of ['XAUTHORITY','LD_LIBRARY_PATH','DBUS_SESSION_BUS_ADDRESS'])if(process.env[key])environment[key]=process.env[key];
  const supervisor=new DesktopSupervisor(filename,{environment}),applications=[];
  const holdReadyLine=(await readFile(new URL('../src/main.mjs',import.meta.url),'utf8')).split('\n').findIndex(line=>line.includes("const ready = await entryBudget.run(channel.request('ready'"));
  supervisor.on('phase',event=>{
    if(event.phase!=='TARGET_ADMITTED')return;
    const record=supervisor.worker;
    const connected=launchElectron({existingChild:record.owned.child,observe:false,env:environment,
      holdReadyLine:pauseTarget&&applications.length>0?holdReadyLine:undefined});connected.catch(()=>{});
    applications.push({binding:record.binding,connected});
  });
  t.after(async()=>{
    const closing=supervisor.close();
    for(const item of applications){
      const app=await item.connected.catch(()=>null);
      if(app&&!app.status){await until(()=>app.debuggerWaiting||app.status,15000).catch(()=>{});await app.detach().catch(()=>{});}
    }
    await closing.catch(()=>{});await cleanupApplication();await rm(root,{recursive:true,force:true});
  });
  await supervisor.start();
  async function current(){
    const item=applications.at(-1),app=await item.connected,page=await app.firstPage();
    await page.locator('.heading .primary').waitFor();
    await until(()=>app.evaluate(({BrowserWindow})=>BrowserWindow.getAllWindows().some(window=>window.isVisible())));
    return{app,page,binding:item.binding};
  }
  async function transfer(before,action){
    const count=applications.length;await action();
    await until(()=>before.debuggerWaiting||before.status,30000);await before.detach();
    await until(()=>applications.length>count,30000);
    await until(()=>supervisor.state==='READY'&&supervisor.current.id===applications.at(-1).binding.id,45000);
    return current();
  }
  return{root,data,profile,config,filename,supervisor,applications,current,transfer,treeObserver:await import(wrapper)};
}

async function click(app,id){await app.evaluate(({Menu},id)=>{const item=Menu.getApplicationMenu().getMenuItemById(id);if(!item?.enabled)throw new Error('Native menu action is unavailable');item.click();},id);}
async function readyMenu(app,id){return app.evaluate(({Menu},id)=>Menu.getApplicationMenu().getMenuItemById(id)?.enabled,id);}
async function owner(app){return app.evaluate(({BrowserWindow})=>BrowserWindow.getAllWindows().find(window=>window.webContents.getURL().startsWith('http:')).webContents.session.cookies.get({name:'loopcast_owner'}));}
async function profileCanary(page,value){return page.evaluate(async value=>{
  localStorage.setItem('workspace-isolation-canary',value);
  await new Promise((resolve,reject)=>{const request=indexedDB.open('workspace-isolation-canary',1);request.onupgradeneeded=()=>request.result.createObjectStore('value');request.onerror=reject;request.onsuccess=()=>{const db=request.result,transaction=db.transaction('value','readwrite');transaction.objectStore('value').put(value,'identity');transaction.oncomplete=()=>{db.close();resolve();};};});
},value);}
async function idbValue(page){return page.evaluate(async()=>{
  if(!(await indexedDB.databases()).some(value=>value.name==='workspace-isolation-canary'))return null;
  return new Promise((resolve,reject)=>{const request=indexedDB.open('workspace-isolation-canary',1);request.onerror=reject;request.onsuccess=()=>{const db=request.result,get=db.transaction('value').objectStore('value').get('identity');get.onsuccess=()=>{resolve(get.result);db.close();};};});
});}
const fixedKey='8424e367-f6a1-4a8d-b9c2-317fd316d5ea';
async function intercept(app,page,lose){
  const evidence={keys:[],committed:[],token:null},cdp=await app.context.newCDPSession(page);
  await cdp.send('Network.enable');
  cdp.on('Network.requestWillBeSentExtraInfo',event=>{
    const token=Object.entries(event.headers).find(([name])=>name.toLowerCase()==='x-loopcast-desktop-session')?.[1];if(token)evidence.token=token;
  });
  await cdp.send('Fetch.enable',{patterns:[{urlPattern:'*/v1/runs',requestStage:'Response'}]});
  cdp.on('Fetch.requestPaused',async event=>{
    if(event.request.method!=='POST'){await cdp.send('Fetch.continueResponse',{requestId:event.requestId});return;}
    evidence.keys.push(Object.entries(event.request.headers).find(([name])=>name.toLowerCase()==='idempotency-key')?.[1]);
    const body=await cdp.send('Fetch.getResponseBody',{requestId:event.requestId}),value=JSON.parse(body.base64Encoded?Buffer.from(body.body,'base64').toString():body.body);
    evidence.committed.push(value.run_id);
    if(lose)await cdp.send('Fetch.failRequest',{requestId:event.requestId,errorReason:'Aborted'});else await cdp.send('Fetch.continueResponse',{requestId:event.requestId});
  });
  await page.evaluate(async()=>(await fetch('/healthz')).json());return evidence;
}
async function loseWrite(app,page){
  const evidence=await intercept(app,page,true);
  await page.evaluate(key=>{crypto.randomUUID=()=>key;},fixedKey);
  await page.locator('.heading .primary').click();await page.locator('input[name=title]').fill('Same name across independent workspaces');
  await page.locator('#create-form button[type=submit]').click();
  await until(()=>evidence.committed.length===1);await until(()=>page.locator('#form-error').textContent().then(Boolean));
  assert.deepEqual(evidence.keys,[fixedKey]);assert.ok(evidence.committed[0]);return evidence;
}

test('actual supervised maintenance, guarded workspace succession and two profile round trips preserve separate same-key lost responses', {timeout:240000},async t=>{
  const item=await fixture(t);let{app,page,binding}=await item.current();const original=binding;
  await profileCanary(page,'original');const originalOwner=await owner(app);
  const packagePath=join(item.root,'package'),proofPath=join(item.root,'proof.json'),restoredPath=join(item.root,'restored');
  await app.evaluate(({dialog},paths)=>{
    globalThis.nativeWorkspaceOpen=dialog.showOpenDialog.bind(dialog);
    const saves=[paths.packagePath,paths.proofPath,paths.restoredPath],opens=[paths.packagePath,paths.proofPath];
    dialog.showSaveDialog=async()=>({canceled:false,filePath:saves.shift()});dialog.showOpenDialog=async()=>({canceled:false,filePaths:[opens.shift()]});
    dialog.showMessageBox=async options=>({response:options.type==='question'?0:options.buttons.length===2?1:0});
  },{packagePath,proofPath,restoredPath});
  await click(app,'maintenance-create');await until(async()=>{try{await stat(proofPath);return readyMenu(app,'maintenance-restore');}catch{return false;}},30000);
  await click(app,'maintenance-restore');await until(()=>readyMenu(app,'workspace-open'),30000);
  assert.equal(item.supervisor.receipts.records().filter(receipt=>receipt.action==='restore'&&receipt.state==='COMPLETE').at(-1).target_entity.identity_scope,'KERNEL_BOOT');
  // Snapshot first: permanent deduplication records in a backup must survive.
  // Each lost response below belongs to a write made after the snapshot.
  await click(app,'maintenance-return');
  await until(()=>readyMenu(app,'workspace-open'));
  ({app,page}=await item.current());
  const first=await loseWrite(app,page);
  async function configureOpen(current){await current.evaluate(({dialog},path)=>{dialog.showMessageBox=async()=>({response:0});dialog.showOpenDialog=async()=>({canceled:false,filePaths:[path]});},restoredPath);}
  await configureOpen(app);
  ({app,page,binding}=await item.transfer(app,()=>click(app,'workspace-open')));const restored=binding;
  assert.equal(restored.role,'RESTORED');assert.notEqual(restored.id,original.id);assert.notEqual(restored.origin_port,original.origin_port);
  assert.notEqual(restored.session_dir,original.session_dir);assert.equal(await page.evaluate(()=>localStorage.getItem('workspace-isolation-canary')),null);
  assert.equal(await idbValue(page),null);assert.equal(await page.evaluate(()=>Object.keys(localStorage).filter(key=>key.startsWith('loopcast.run.create.pending.v1.')).length),0);
  const restoredOwner=await owner(app);assert.notEqual(restoredOwner[0].value,originalOwner[0].value);
  const guard=await page.evaluate(async()=>(await(await fetch('/v1/operations/status')).json()));assert.equal(guard.restore_guard.state,'ACTIVE');assert.equal(guard.upload_allowed_by_recovery,false);
  const prefs=await app.evaluate(({BrowserWindow})=>{const window=BrowserWindow.getAllWindows()[0],p=window.webContents.getLastWebPreferences();return{sandbox:p.sandbox,contextIsolation:p.contextIsolation,webSecurity:p.webSecurity,node:p.nodeIntegration,unsafe:process.argv.includes('--no-sandbox')};});
  assert.deepEqual(prefs,{sandbox:true,contextIsolation:true,webSecurity:true,node:false,unsafe:false});
  await profileCanary(page,'restored');const second=await loseWrite(app,page);assert.notEqual(second.committed[0],first.committed[0]);
  await app.evaluate(({dialog})=>{dialog.showMessageBox=async()=>({response:0});});
  ({app,page,binding}=await item.transfer(app,()=>click(app,'maintenance-return')));
  assert.equal(binding.id,original.id);assert.equal(new URL(page.url()).port,String(original.origin_port));
  assert.equal(await page.evaluate(()=>localStorage.getItem('workspace-isolation-canary')),'original');assert.equal(await idbValue(page),'original');
  assert.equal((await owner(app))[0].value,originalOwner[0].value);
  await page.locator('#create-dialog[open]').waitFor();const originalRetry=await intercept(app,page,false);assert.equal(originalRetry.keys.length,0);
  await app.evaluate(({BrowserWindow})=>BrowserWindow.getAllWindows()[0].webContents.session.clearCache());await page.reload();await page.locator('#create-dialog[open]').waitFor();
  assert.equal((await owner(app))[0].value,originalOwner[0].value);assert.equal(await idbValue(page),'original');
  assert.equal(originalRetry.keys.length,0);await page.locator('#create-form button[type=submit]').click();await page.waitForURL('**/runs/'+first.committed[0]);assert.deepEqual(originalRetry.keys,[fixedKey]);
  assert.equal(await page.evaluate(async()=>(await(await fetch('/v1/runs?limit=100')).json()).items.length),1);
  assert.ok(first.token,'Observe the actual gateway token before its retirement');
  assert.equal((await fetch(`http://127.0.0.1:${original.origin_port}/healthz`,{headers:{'X-LoopCast-Desktop-Session':first.token}})).status,403);
  await configureOpen(app);({app,page,binding}=await item.transfer(app,()=>click(app,'workspace-open')));
  assert.equal(binding.id,restored.id);assert.equal(new URL(page.url()).port,String(restored.origin_port));assert.equal(await idbValue(page),'restored');
  assert.equal((await owner(app))[0].value,restoredOwner[0].value);await page.locator('#create-dialog[open]').waitFor();
  const restoredRetry=await intercept(app,page,false);assert.equal(restoredRetry.keys.length,0);
  await app.evaluate(({BrowserWindow})=>BrowserWindow.getAllWindows()[0].webContents.session.clearCache());await page.reload();await page.locator('#create-dialog[open]').waitFor();
  assert.equal(restoredRetry.keys.length,0);assert.equal((await owner(app))[0].value,restoredOwner[0].value);assert.equal(await idbValue(page),'restored');
  await page.locator('#create-form button[type=submit]').click();await page.waitForURL('**/runs/'+second.committed[0]);
  assert.deepEqual(restoredRetry.keys,[fixedKey]);assert.equal(await page.evaluate(async()=>(await(await fetch('/v1/runs?limit=100')).json()).items.length),1);
  assert.equal((await page.evaluate(async()=>(await(await fetch('/v1/operations/status')).json()))).restore_guard.state,'ACTIVE');
  await app.evaluate(({dialog})=>{dialog.showMessageBox=async()=>({response:0});});({app,page,binding}=await item.transfer(app,()=>click(app,'maintenance-return')));
  assert.equal(binding.id,original.id);assert.equal(await idbValue(page),'original');
  assert.equal(await page.evaluate(()=>Object.keys(localStorage).filter(key=>key.startsWith('loopcast.run.create.pending.v1.')).length),0);
  await rename(restored.session_dir,join(item.root,'missing-restored-profile'));
  await configureOpen(app);
  await app.evaluate(({dialog})=>{globalThis.missingProfileRejected=false;dialog.showMessageBox=async options=>{if(options.type==='error')globalThis.missingProfileRejected=true;return{response:0};};});
  await click(app,'workspace-open');await until(()=>app.evaluate(()=>globalThis.missingProfileRejected));
  assert.equal(item.supervisor.current.id,original.id);assert.equal(item.applications.length,5);
  await assert.rejects(stat(restored.session_dir));assert.equal(item.supervisor.pendingOperation??null,null);
  const proof=JSON.parse(await readFile(proofPath,'utf8'));
  console.log('Owned Electron succession evidence',JSON.stringify({os:process.platform,arch:process.arch,source_revision:item.config.hostRevision,
    workspace_processes:item.applications.length,manifest_sha256:proof.manifest_sha256,backup_id:proof.backup_id,restore_id:restored.restore.restore_id,
    independent_profiles:true,same_key_explicit_retries:true,restore_guard:'ACTIVE',old_token_observed:!!first.token}));
});


async function restoredFixture(t,options){
  const item=await fixture(t,options),{app}=await item.current();
  const paths={packagePath:join(item.root,'package'),proofPath:join(item.root,'proof.json'),restoredPath:join(item.root,'restored')};
  await app.evaluate(({dialog},paths)=>{
    globalThis.nativeWorkspaceOpen=dialog.showOpenDialog.bind(dialog);
    const saves=[paths.packagePath,paths.proofPath,paths.restoredPath],opens=[paths.packagePath,paths.proofPath];
    dialog.showSaveDialog=async()=>({canceled:false,filePath:saves.shift()});
    dialog.showOpenDialog=async()=>({canceled:false,filePaths:[opens.shift()]});
    dialog.showMessageBox=async options=>({response:options.type==='question'?0:options.buttons.length===2?1:0});
  },paths);
  await click(app,'maintenance-create');
  await until(async()=>{try{await stat(paths.proofPath);return readyMenu(app,'maintenance-restore');}catch{return false;}});
  await click(app,'maintenance-restore');await until(()=>readyMenu(app,'workspace-open'));
  await app.evaluate(({dialog},path)=>{dialog.showMessageBox=async()=>({response:0});dialog.showOpenDialog=async()=>({canceled:false,filePaths:[path]});},paths.restoredPath);
  return{...item,...paths,app,original:item.supervisor.current,pid:await app.evaluate(()=>process.pid)};
}
async function assertFenced(item,{committed=false}={}){
  const root=item.supervisor.lease.root;
  assert.ok((await stat(join(root,'pending'))).isFile());
  assert.equal(item.supervisor.current.id===item.original.id,!committed);
  assert.ok((await stat(item.profile)).isDirectory());
  const unrelated=join(item.root,'unrelated');await mkdir(unrelated,{mode:0o700});
  const {spawn}=await import('node:child_process');
  const child=spawn(item.config.python.path,['-I','-m','loopcast','serve','--data-dir',unrelated,'--port','0'],{env:{PATH:'/usr/bin:/bin',LANG:'C.UTF-8'},stdio:'ignore'});
  const code=await new Budget(5000).run(new Promise(resolve=>child.once('exit',resolve)));
  assert.notEqual(code,0);assert.deepEqual(await readdir(unrelated),[]);
}

test('native cancelled and duplicate handoff plans never consume admission or create a target profile',{timeout:90000},async t=>{
  const item=await restoredFixture(t);let duplicate;
  item.supervisor.on('phase',event=>{if(event.phase==='PLANNED'&&item.supervisor.plan){duplicate=item.supervisor.prepare({return_original:true});duplicate.catch(()=>{});}});
  await item.app.evaluate(({dialog})=>{dialog.showMessageBox=async options=>({response:options.title==='Review workspace handoff'?1:0});});
  await click(item.app,'workspace-open');await until(()=>Boolean(duplicate));
  await assert.rejects(duplicate,/HANDOFF_UNCONFIRMED/);await until(()=>!item.supervisor.plan);
  assert.equal(item.applications.length,1);assert.equal(item.supervisor.pendingOperation??null,null);
  assert.equal(item.supervisor.current.id,item.original.id);
});

for(const stage of ['SOURCE_STOPPED','TARGET_ADMITTED','TARGET_READY','COMMITTED','FINISHED']){
  test(`actual native process death at ${stage} preserves durable admission and prevents automatic recovery`,{timeout:100000},async t=>{
    const target=stage!=='SOURCE_STOPPED',item=await restoredFixture(t,{pauseTarget:target});
    let pid=item.pid,fired=false;
    item.supervisor.on('phase',event=>{
      if(event.phase===stage&&((target&&item.applications.length===2)||(!target&&item.supervisor.plan?.accepted))){
        if(stage==='TARGET_ADMITTED')return;
        fired=true;process.kill(pid,'SIGKILL');
      }
    });
    await click(item.app,'workspace-open');
    if(target){
      await until(()=>item.app.debuggerWaiting||item.app.status);await item.app.detach();
      await until(()=>item.applications.length===2);
      const app=await item.applications.at(-1).connected;
      await new Budget(30000).run(app.readyPause);pid=await app.pausedPid();
      if(stage==='TARGET_ADMITTED'){fired=true;process.kill(pid,'SIGKILL');}else await app.resumeReady();
    }
    await until(()=>fired&&item.supervisor.state==='QUARANTINED',45000);
    await assertFenced(item,{committed:['COMMITTED','FINISHED'].includes(stage)});
    assert.equal(item.applications.length,target?2:1);
  });
}

for(const stage of ['SOURCE_EXITED','BINDING','TARGET_BOUND']){
  test(`closing the actual supervisor at ${stage} cannot launch another Electron`,{timeout:90000},async t=>{
    const item=await restoredFixture(t);let closing;
    item.supervisor.on('phase',event=>{if(event.phase===stage)closing=item.supervisor.close();});
    await click(item.app,'workspace-open');await until(()=>item.app.debuggerWaiting||item.app.status);await item.app.detach();
    await until(()=>Boolean(closing));await closing;
    assert.equal(item.applications.length,1);assert.equal(item.supervisor.state,'QUARANTINED');await assertFenced(item);
  });
}

for(const mutation of ['directory','profile','resource','port']){
  test(`actual successor refuses ${mutation} replacement or collision without changing origin or pointer`,{timeout:100000},async t=>{
    const item=await restoredFixture(t),canary=join(item.root,'canary');await mkdir(canary,{mode:0o700});
    await writeFile(join(canary,'untouched'),'retain',{mode:0o600});
    let changed=false,server;
    t.after(async()=>{if(server)await new Promise(resolve=>server.close(resolve));});
    item.supervisor.on('phase',event=>{
      if(changed||event.phase!==(mutation==='directory'?'SOURCE_EXITED':'TARGET_BOUND'))return;
      changed=true;const plan=item.supervisor.plan;
      if(mutation==='directory'){renameSync(item.restoredPath,item.restoredPath+'.saved');symlinkSync(canary,item.restoredPath);}
      if(mutation==='profile'){renameSync(plan.target.session_dir,join(item.root,'saved-profile'));symlinkSync(canary,plan.target.session_dir);}
      if(mutation==='resource')writeFileSync(item.filename,JSON.stringify({...item.config,locale:'zh-Hans'}),{mode:0o600});
      if(mutation==='port'){server=createServer();server.listen(plan.target.origin_port,'127.0.0.1');}
    });
    await click(item.app,'workspace-open');await until(()=>item.app.debuggerWaiting||item.app.status);await item.app.detach();
    if(mutation==='port'){
      await until(()=>item.applications.length===2);const next=await item.applications.at(-1).connected;
      await next.evaluate(({dialog})=>{dialog.showMessageBox=async()=>({response:0});});
      await until(()=>next.debuggerWaiting||next.status,45000);await next.detach();
    }
    await until(()=>changed&&item.supervisor.state==='QUARANTINED',45000);await assertFenced(item);
    assert.deepEqual(await readdir(canary),['untouched']);assert.equal(await readFile(join(canary,'untouched'),'utf8'),'retain');
    if(mutation!=='port')assert.equal(item.applications.length,1);
  });
}


test('native cancellation after durable permission completes STOP and retains pending without starting a target',{timeout:90000},async t=>{
  const item=await restoredFixture(t);let cancelling;
  item.supervisor.on('phase',event=>{
    if(event.phase==='PENDING_DURABLE'&&item.supervisor.plan?.accepted)cancelling=item.app.evaluate(({Menu})=>{
      const item=Menu.getApplicationMenu().items[0].submenu.items.find(item=>item.label==='Cancel maintenance');
      if(!item?.enabled)throw new Error('Native cancel is unavailable');item.click();
    });
  });
  await click(item.app,'workspace-open');await until(()=>Boolean(cancelling));await cancelling;
  await until(()=>item.supervisor.plan?.cancelled,30000);
  assert.equal(item.applications.length,1);await assertFenced(item);
  assert.equal(await readyMenu(item.app,'workspace-open'),false);
});

test('a copied restored directory and a receipt-free plan cannot acquire successor authority',{timeout:90000},async t=>{
  const item=await restoredFixture(t),copy=join(item.root,'copied');await cp(item.restoredPath,copy,{recursive:true});
  const receipt=item.supervisor.receipts.records().find(value=>value.action==='restore'&&value.state==='COMPLETE');
  await assert.rejects(item.supervisor.prepare({path:item.restoredPath}),/HANDOFF_UNCONFIRMED/);
  await assert.rejects(item.supervisor.prepare({path:copy,receipt:receipt.operation_id}),/HANDOFF_UNCONFIRMED/);
  assert.equal(item.supervisor.pendingOperation??null,null);assert.equal(item.applications.length,1);
});

test('killing the actual outer Node supervisor leaves durable admission after all owned descendants stop',{timeout:90000},async t=>{
  const item=await fixture(t),{app}=await item.current();
  const closing=item.supervisor.close();await until(()=>app.debuggerWaiting||app.status);await app.detach();await closing;
  const {StateJournal}=await import('../src/application-state.mjs'),{spawn}=await import('node:child_process');
  const journal=new StateJournal(item.supervisor.lease.root),before=journal.load().length;
  t.after(()=>journal.close());
  const child=spawn(process.execPath,[new URL('../scripts/start.mjs',import.meta.url).pathname,'--desktop-config',item.filename],{
    env:item.supervisor.environment,stdio:['ignore','ignore','ignore']});
  const exited=new Promise(resolve=>child.once('exit',resolve));t.after(async()=>{if(child.exitCode===null&&child.signalCode===null)child.kill('SIGKILL');await exited;});
  await until(()=>journal.load().slice(before).some(record=>record.phase==='COMMITTED'),40000);
  const {execFileSync}=await import('node:child_process');
  const processes=execFileSync('/bin/ps',['-eo','pid=,ppid='],{encoding:'utf8'}).trim().split('\n').map(line=>line.trim().split(/\s+/).map(Number));
  const descendants=new Set([child.pid]);let changed=true;
  while(changed){changed=false;for(const[pid,parent]of processes)if(descendants.has(parent)&&!descendants.has(pid)){descendants.add(pid);changed=true;}}
  assert.ok(descendants.size>5);child.kill('SIGKILL');await exited;
  await until(async()=>{
    for(const pid of descendants){try{const text=await readFile(`/proc/${pid}/stat`,'utf8');if(text.slice(text.lastIndexOf(')')+2).split(' ')[0]!=='Z')return false;}catch(error){if(!['ENOENT','ESRCH'].includes(error.code))throw error;}}
    return true;
  },25000);
  assert.ok((await stat(join(item.supervisor.lease.root,'active'))).isFile());
  const unrelated=join(item.root,'unrelated');await mkdir(unrelated,{mode:0o700});
  const cli=spawn(item.config.python.path,['-I','-m','loopcast','serve','--data-dir',unrelated,'--port','0'],{env:{PATH:'/usr/bin:/bin',LANG:'C.UTF-8'},stdio:'ignore'});
  assert.notEqual(await new Budget(5000).run(new Promise(resolve=>cli.once('exit',resolve))),0);assert.deepEqual(await readdir(unrelated),[]);
});


test('lost actual helper witness during target binding fences every workspace and retains pending',{timeout:90000},async t=>{
  const item=await restoredFixture(t);let destroyed=false;
  item.supervisor.on('phase',event=>{
    if(event.phase!=='SOURCE_EXITED')return;
    item.treeObserver.observeLaunch((owned,spec)=>{
      if(destroyed||!spec.argv.includes('loopcast.desktop_workspace'))return;
      destroyed=true;owned.child.stdout.once('data',()=>owned.child.stdio[5].destroy(new Error('External witness-pipe loss')));
    });
  });
  await click(item.app,'workspace-open');await until(()=>item.app.debuggerWaiting||item.app.status);await item.app.detach();
  await until(()=>destroyed&&item.supervisor.unknownTree&&item.supervisor.closed,45000);await item.supervisor.closed;
  assert.equal(item.supervisor.state,'QUARANTINED');assert.equal(item.applications.length,1);await assertFenced(item);
});


test('Chinese native workspace shortcut uses a real selector and Escape, then explicit guarded narrow-window round trip',{timeout:120000},async t=>{
  const item=await restoredFixture(t,{locale:'zh-Hans'});let app=item.app;
  await app.evaluate(({dialog,BrowserWindow})=>{
    globalThis.workspaceSelection={opened:false,cancelled:false,plans:[]};
    dialog.showOpenDialog=async options=>{globalThis.workspaceSelection.opened=true;const result=await globalThis.nativeWorkspaceOpen(options);globalThis.workspaceSelection.cancelled=result.canceled;return result;};
    dialog.showMessageBox=async()=>({response:0});BrowserWindow.getAllWindows()[0].focus();
  });
  const nativeWindow=await app.evaluate(({BrowserWindow})=>BrowserWindow.getAllWindows()[0].getNativeWindowHandle().readUInt32LE(0));
  const native=promisify(execFile);
  await native('xdotool',['windowactivate','--sync',String(nativeWindow)],{timeout:7000});
  await until(()=>app.evaluate(({BrowserWindow})=>BrowserWindow.getAllWindows()[0].isFocused()));
  await native('xdotool',['key','--clearmodifiers','ctrl+shift+o'],{timeout:3000});
  await until(()=>app.evaluate(()=>globalThis.workspaceSelection.opened));
  const picker=await until(async()=>{
    const active=(await native('xdotool',['getactivewindow'],{timeout:3000})).stdout.trim();
    if(Number(active)===nativeWindow)return false;
    return(await native('xdotool',['getwindowname',active],{timeout:3000})).stdout.trim()==='选择已恢复的私有工作区'?active:false;
  });
  await native('xdotool',['windowactivate','--sync',picker],{timeout:7000});
  assert.equal((await native('xdotool',['getactivewindow'],{timeout:3000})).stdout.trim(),picker);
  await native('xdotool',['key','--clearmodifiers','Escape'],{timeout:3000});
  await until(()=>app.evaluate(()=>globalThis.workspaceSelection.cancelled));await until(()=>readyMenu(app,'workspace-open'));
  assert.equal(item.supervisor.pendingOperation??null,null);assert.equal(item.applications.length,1);
  await app.evaluate(({dialog},path)=>{
    dialog.showOpenDialog=async()=>({canceled:false,filePaths:[path]});
    dialog.showMessageBox=async options=>{if(options.title==='核对工作区交接计划'){
      if(!options.message.includes('上传屏障')||!options.detail.includes('原工作区')||options.buttons[0]!=='执行此计划')throw new Error('Missing localized handoff');
      globalThis.workspaceSelection.plans.push({message:options.message,detail:options.detail,buttons:options.buttons});
    }return{response:0};};
  },item.restoredPath);
  const departing=app;
  const changing=item.transfer(app,()=>click(app,'workspace-open'));
  await until(()=>departing.debuggerWaiting||departing.status);
  const next=await changing;app=next.app;
  assert.equal(next.binding.role,'RESTORED');
  const state=await app.evaluate(({BrowserWindow,Menu})=>{const window=BrowserWindow.getAllWindows()[0];window.setSize(480,640);return{
    title:window.getTitle(),back:Menu.getApplicationMenu().getMenuItemById('maintenance-return').label,
    width:window.getSize()[0],sandbox:window.webContents.getLastWebPreferences().sandbox};});
  assert.match(state.title,/受控恢复工作区/);assert.equal(state.back,'返回原工作区');assert.equal(state.width,480);assert.equal(state.sandbox,true);
  assert.equal((await next.page.evaluate(async()=>(await(await fetch('/v1/operations/status')).json()))).restore_guard.state,'ACTIVE');
  await app.evaluate(({dialog})=>{dialog.showMessageBox=async options=>{if(!options.message.includes('正常停止'))throw new Error('Missing localized handoff');return{response:0};};});
  const original=await item.transfer(app,()=>click(app,'maintenance-return'));assert.equal(original.binding.id,item.original.id);
  console.log('Actual X11 restored selector/shortcut/Escape and Chinese guarded workspace/explicit Return PASS');
});
