// SPDX-License-Identifier: Apache-2.0
import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp,mkdir,writeFile,readFile,rm,stat } from 'node:fs/promises';
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

async function fixture(t){
  const cleanupApplication=await applicationFixture(),root=await mkdtemp(join(tmpdir(),'loopcast-supervisor-'));
  const data=join(root,'data'),profile=join(root,'original-profile'),home=join(root,'home');
  for(const path of [data,profile,home,join(home,'config')])await mkdir(path,{mode:0o700});
  const config=await prepare({python:process.env.LOOPCAST_DESKTOP_PYTHON,node:process.execPath,electron:executable,
    next:process.env.LOOPCAST_DESKTOP_NEXT,data,session:profile,port:'28789',revision:process.env.LOOPCAST_DESKTOP_REVISION});
  const wrapper=join(root,'inspected-owned-port.mjs'),production=new URL('../src/linux-tree.mjs',import.meta.url).href;
  // External test tooling adds debugger listeners only. Production ownership,
  // cleanup, default sandbox, private pipes and every maintenance handler run.
  await writeFile(wrapper,`import{createTreePort as production}from ${JSON.stringify(production)};
export function createTreePort(config){const tree=production(config),launch=tree.launch.bind(tree);tree.launch=spec=>launch(spec.executable===${JSON.stringify(executable)}?{...spec,argv:['--inspect=0','--remote-debugging-port=0',...spec.argv]}:spec);return tree;}`,{mode:0o600});
  config.treeModule={path:wrapper,sha256:hash(await readFile(wrapper))};
  const filename=join(root,'desktop.json');await writeFile(filename,JSON.stringify(config),{mode:0o600});
  const environment={PATH:'/usr/bin:/bin',LANG:'C.UTF-8',HOME:home,XDG_CONFIG_HOME:join(home,'config'),DISPLAY:process.env.DISPLAY};
  for(const key of ['XAUTHORITY','LD_LIBRARY_PATH','DBUS_SESSION_BUS_ADDRESS'])if(process.env[key])environment[key]=process.env[key];
  const supervisor=new DesktopSupervisor(filename,{environment}),applications=[];
  supervisor.on('phase',event=>{
    if(event.phase!=='TARGET_ADMITTED')return;
    const record=supervisor.worker;
    const connected=launchElectron({existingChild:record.owned.child,observe:false,env:environment});connected.catch(()=>{});
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
  return{root,data,profile,config,supervisor,applications,current,transfer};
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
  await profileCanary(page,'original');const originalOwner=await owner(app),first=await loseWrite(app,page);
  const packagePath=join(item.root,'package'),proofPath=join(item.root,'proof.json'),restoredPath=join(item.root,'restored');
  await app.evaluate(({dialog},paths)=>{
    const saves=[paths.packagePath,paths.proofPath,paths.restoredPath],opens=[paths.packagePath,paths.proofPath];
    dialog.showSaveDialog=async()=>({canceled:false,filePath:saves.shift()});dialog.showOpenDialog=async()=>({canceled:false,filePaths:[opens.shift()]});
    dialog.showMessageBox=async options=>({response:options.type==='question'?0:options.buttons.length===2?1:0});
  },{packagePath,proofPath,restoredPath});
  await click(app,'maintenance-create');await until(async()=>{try{await stat(proofPath);return readyMenu(app,'maintenance-restore');}catch{return false;}},30000);
  await click(app,'maintenance-restore');await until(()=>readyMenu(app,'workspace-open'),30000);
  assert.equal(item.supervisor.receipts.records().filter(receipt=>receipt.action==='restore'&&receipt.state==='COMPLETE').at(-1).target_entity.identity_scope,'KERNEL_BOOT');
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
  assert.equal(originalRetry.keys.length,0);await page.locator('#create-form button[type=submit]').click();await page.waitForURL('**/runs/'+first.committed[0]);assert.deepEqual(originalRetry.keys,[fixedKey]);
  assert.equal(await page.evaluate(async()=>(await(await fetch('/v1/runs?limit=100')).json()).items.length),1);
  if(first.token){assert.equal((await fetch(`http://127.0.0.1:${original.origin_port}/healthz`,{headers:{'X-LoopCast-Desktop-Session':first.token}})).status,403);}
  await configureOpen(app);({app,page,binding}=await item.transfer(app,()=>click(app,'workspace-open')));
  assert.equal(binding.id,restored.id);assert.equal(new URL(page.url()).port,String(restored.origin_port));assert.equal(await idbValue(page),'restored');
  assert.equal((await owner(app))[0].value,restoredOwner[0].value);await page.locator('#create-dialog[open]').waitFor();
  const restoredRetry=await intercept(app,page,false);assert.equal(restoredRetry.keys.length,0);await page.locator('#create-form button[type=submit]').click();await page.waitForURL('**/runs/'+second.committed[0]);
  assert.deepEqual(restoredRetry.keys,[fixedKey]);assert.equal(await page.evaluate(async()=>(await(await fetch('/v1/runs?limit=100')).json()).items.length),2);
  assert.equal((await page.evaluate(async()=>(await(await fetch('/v1/operations/status')).json()))).restore_guard.state,'ACTIVE');
  await app.evaluate(({dialog})=>{dialog.showMessageBox=async()=>({response:0});});({app,page,binding}=await item.transfer(app,()=>click(app,'maintenance-return')));
  assert.equal(binding.id,original.id);assert.equal(await idbValue(page),'original');
  assert.equal(await page.evaluate(()=>Object.keys(localStorage).filter(key=>key.startsWith('loopcast.run.create.pending.v1.')).length),0);
  const proof=JSON.parse(await readFile(proofPath,'utf8'));
  console.log('Owned Electron succession evidence',JSON.stringify({os:process.platform,arch:process.arch,source_revision:item.config.hostRevision,
    workspace_processes:item.applications.length,manifest_sha256:proof.manifest_sha256,backup_id:proof.backup_id,restore_id:restored.restore.restore_id,
    independent_profiles:true,same_key_explicit_retries:true,restore_guard:'ACTIVE',old_token_observed:!!first.token}));
});
