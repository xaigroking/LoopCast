// SPDX-License-Identifier: Apache-2.0
import { randomBytes } from 'node:crypto';
import { mkdirSync, lstatSync, fsyncSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { EventEmitter } from 'node:events';
import { Budget, HostError } from './errors.mjs';
import { readConfig, loadTreePort } from './config.mjs';
import { observeExit } from './protocol.mjs';
import { ApplicationLease, StateJournal, privateCommand } from './application-state.mjs';
import { ReceiptJournal, holdDirectory, readPrivate, hash, contains } from './private-files.mjs';
import { PrivateChannel } from './private-channel.mjs';
import { restoredObservation } from './workspace-model.mjs';

const entry=fileURLToPath(new URL('../',import.meta.url));
const id=()=>randomBytes(16).toString('hex');
const bindingId=()=>randomBytes(32).toString('hex');
const equal=(a,b)=>JSON.stringify(a)===JSON.stringify(b);
const entityKeys=['identity_scope','filesystem','boot_sha256','object_sha256','object_family_sha256'];
function sameEntity(a,b){return entityKeys.every(key=>a?.[key]===b?.[key]);}
export async function validateWorkspaceBinding(config,binding,{group,digest,boot}){
  if(!binding||binding.format!=='loopcast.workspace-binding.v1'||binding.group!==group||binding.source_digest!==digest||
    !['ORIGINAL','RESTORED'].includes(binding.role)||!/^[a-f0-9]{64}$/.test(binding.id))throw new HostError('HANDOFF_UNCONFIRMED');
  const heldData=holdDirectory(binding.data_dir),heldProfile=holdDirectory(binding.session_dir);
  try{
    const data=await privateCommand(config,'loopcast.desktop_workspace',{action:'entity',path:binding.data_dir});
    const profile=await privateCommand(config,'loopcast.desktop_workspace',{action:'entity',path:binding.session_dir});
    if(!sameEntity(data,binding.entity)||!sameEntity(profile,binding.profile)||data.boot_sha256!==boot)throw new HostError('HANDOFF_UNCONFIRMED');
    heldData.check();heldProfile.check();
  }finally{heldData.close();heldProfile.close();}
}
export async function validateDirectWorkspace(config,digest,journal){
  const known=journal.load().filter(record=>record.phase==='BINDING').map(record=>record.payload.binding);
  if(!known.length)return;
  const matches=known.filter(binding=>binding.data_dir===config.dataDir||binding.session_dir===config.sessionDir);
  if(matches.length!==1||matches[0].role!=='ORIGINAL'||matches[0].data_dir!==config.dataDir||matches[0].session_dir!==config.sessionDir||matches[0].origin_port!==config.gatewayPort)throw new HostError('HANDOFF_UNCONFIRMED');
  const commits=journal.load().filter(record=>record.phase==='COMMITTED'&&record.payload.group===matches[0].group);
  if(commits.length&&commits.at(-1).payload.binding!==matches[0].id)throw new HostError('HANDOFF_UNCONFIRMED');
  await validateWorkspaceBinding(config,matches[0],{group:hash(config.dataDir),digest,boot:matches[0].entity.boot_sha256});
}
export function safeEnvironment(provided=process.env){
  const result={PATH:'/usr/bin:/bin',LANG:'C.UTF-8'};
  for(const key of ['HOME','DISPLAY','XAUTHORITY','XDG_CONFIG_HOME','XDG_RUNTIME_DIR','DBUS_SESSION_BUS_ADDRESS','LD_LIBRARY_PATH'])if(typeof provided[key]==='string')result[key]=provided[key];
  return result;
}

export class DesktopSupervisor extends EventEmitter {
  constructor(filename,{environment}={}){super();this.filename=filename;this.environment=safeEnvironment(environment);this.owned=[];this.holds=[];this.state='STOPPED';}
  phase(operation,phase,payload={}){this.journal.append(operation,phase,payload);this.emit('phase',{operation,phase});}
  async entity(path){return privateCommand(this.config,'loopcast.desktop_workspace',{action:'entity',path});}
  async beginPending(operation){
    if(this.pendingOperation)throw new HostError('HANDOFF_UNCONFIRMED');
    const capability=bindingId();
    await privateCommand(this.config,'loopcast.desktop_admission',{action:'begin',identity:operation,capability});
    this.config.admission=capability;this.pendingOperation=operation;return capability;
  }
  async finishPending(operation){
    if(operation!==this.pendingOperation)throw new HostError('HANDOFF_UNCONFIRMED');
    this.phase(operation,'FINISHED');
    await privateCommand(this.config,'loopcast.desktop_admission',{action:'finish',identity:operation,capability:this.config.admission});
    this.config.admission=null;this.pendingOperation=null;
  }
  bindings(){return this.journal.load().filter(record=>record.phase==='BINDING').map(record=>record.payload.binding);}
  async validateBinding(binding){
    await validateWorkspaceBinding(this.config,binding,{group:this.group,digest:this.digest,boot:this.original.entity.boot_sha256});
  }
  async observe(bindingOrPath,facts){
    const path=typeof bindingOrPath==='string'?bindingOrPath:bindingOrPath.data_dir;
    const observation=restoredObservation(await privateCommand(this.config,'loopcast.desktop_workspace',{action:'restored',path,
      manifest_sha256:facts.manifest_sha256,backup_id:facts.backup_id,restore_id:facts.restore_id}));
    if(observation.guard_version!==facts.guard_version||facts.execution_epoch&&observation.execution_epoch!==facts.execution_epoch)throw new HostError('HANDOFF_UNCONFIRMED');
    return observation;
  }
  async start(){
    if(typeof this.filename==='function'&&!ApplicationLease.cached())this.filename=this.filename();
    this.lease=await ApplicationLease.acquire({filename:typeof this.filename==='function'?undefined:this.filename});
    try{
      if(typeof this.filename==='function')this.filename=this.filename();
      this.config=await readConfig(this.filename);this.digest=hash(readPrivate(this.filename));this.group=hash(this.config.dataDir);
      if([this.config.dataDir,this.config.sessionDir,this.config.nextRoot].some(path=>contains(path,this.lease.root)||contains(this.lease.root,path)))throw new HostError('PATH_UNSAFE');
      try{lstatSync(join(this.config.dataDir,'restore-manifest.json'));throw new HostError('RESTORE_SOURCE_REQUIRED');}
      catch(error){if(error.code!=='ENOENT')throw error;}
      if(!this.config.electron)throw new HostError('CAPABILITY_UNAVAILABLE');
      this.config.leaseCapability=this.lease.capability;this.config.admission=null;
      this.config.onUnconfirmedTree=()=>{
        this.unknownTree=true;this.state='QUARANTINED';
        try{if(this.journal)this.phase(this.worker?.operation??id(),'UNKNOWN_TREE',{scope:'SUPERVISOR_HELPER'});}catch{}
        this.emit('fault',new HostError('STOP_UNCONFIRMED'));void this.close({confirmed:false}).catch(()=>{});
      };
      this.journal=new StateJournal(this.lease.root);this.receipts=new ReceiptJournal(this.lease.root);this.receipts.assertClear();
      const known=this.bindings();this.original=known.find(binding=>binding.group===this.group&&binding.role==='ORIGINAL');
      if(!this.original){
        const entity=await this.entity(this.config.dataDir),profile=await this.entity(this.config.sessionDir);
        if(known.some(binding=>binding.entity.object_family_sha256===entity.object_family_sha256||binding.data_dir===this.config.dataDir||
          binding.profile.object_family_sha256===profile.object_family_sha256||binding.session_dir===this.config.sessionDir))throw new HostError('HANDOFF_UNCONFIRMED');
        this.original={format:'loopcast.workspace-binding.v1',id:bindingId(),group:this.group,role:'ORIGINAL',source_digest:this.digest,
          data_dir:this.config.dataDir,session_dir:this.config.sessionDir,origin_port:this.config.gatewayPort,entity,profile,original_binding:null,restore:null};
        this.phase(id(),'BINDING',{binding:this.original});
      }
      await this.validateBinding(this.original);
      const commits=this.journal.load().filter(record=>record.phase==='COMMITTED'&&record.payload.group===this.group);
      this.current=commits.length?this.bindings().find(binding=>binding.id===commits.at(-1).payload.binding):this.original;
      await this.validateBinding(this.current);
      if(this.current.role==='RESTORED')await this.observe(this.current,this.current.restore);
      this.state='STARTING';
      await this.launch(this.current);this.state='READY';return this;
    }catch(error){await this.close({confirmed:this.owned.length===0&&!this.pendingOperation&&!this.unknownTree}).catch(()=>{});throw error;}
  }
  configFor(binding){return{...this.config,dataDir:binding.data_dir,sessionDir:binding.session_dir,gatewayPort:binding.origin_port,
    configs:binding.role==='ORIGINAL'?{...this.config.configs}:{}};}
  async launch(binding){
    await readConfig(this.filename,{expectedDigest:this.digest});await this.validateBinding(binding);
    const tree=await loadTreePort(this.config),budget=new Budget(this.config.startTimeoutMs),challenge=bindingId(),operation=id();
    const owned=tree.launch({executable:this.config.electron.path,argv:[entry,'--supervised-worker'],cwd:this.config.sessionDir,
      env:this.environment,stdio:['pipe','ignore','pipe','pipe'],deadline:budget.end});
    const exit=observeExit(owned.child);owned.child.stderr.resume();owned.child.stdin.on('error',()=>{});
    let resolveReady,rejectReady;
    const ready=new Promise((resolve,reject)=>{resolveReady=resolve;rejectReady=reject;});ready.catch(()=>{});
    const record={owned,exit,binding,challenge,operation,ready,resolveReady,rejectReady,tree};this.owned.push(record);this.worker=record;
    record.channel=new PrivateChannel(owned.child.stdio[3],{timeout:this.config.startTimeoutMs,
      onRequest:(type,payload)=>this.request(record,type,payload),onClosed:()=>{if(!record.finished)record.rejectReady(new HostError('HANDOFF_UNCONFIRMED'));}});
    this.phase(operation,'TARGET_ADMITTED',{binding:binding.id,group:this.group,challenge_commitment:hash(challenge)});
    owned.child.stdin.end(JSON.stringify({format:'loopcast.supervised-worker.v1',filename:this.filename,config_digest:this.digest,
      binding,challenge,admission:this.config.admission,lease:this.config.leaseCapability,journal_root:this.lease.root}));
    exit.exited.then(status=>{void this.exited(record,status);});
    return budget.run(ready).catch(async error=>{record.rejectReady(error);throw error;});
  }
  async request(record,type,payload){
    if(record!==this.worker||payload.challenge!==record.challenge)throw new HostError('HANDOFF_UNCONFIRMED');
    if(this.unknownTree&&type!=='stopped')throw new HostError('STOP_UNCONFIRMED');
    if(type==='ready'){
      if(record.readyReceived)throw new HostError('HANDOFF_UNCONFIRMED');record.readyReceived=true;
      await this.validateBinding(record.binding);
      if(record.binding.role==='RESTORED'){
        const facts=record.binding.restore,guard=payload.guard;
        if(!guard||guard.state!=='ACTIVE'||guard.restore_id!==facts.restore_id||guard.version!==facts.guard_version||
          guard.execution_epoch!==facts.execution_epoch||guard.upload_allowed!==false)throw new HostError('HANDOFF_UNCONFIRMED');
      }
      this.phase(record.operation,'TARGET_READY',{binding:record.binding.id,group:this.group,guard:payload.guard??null});
      this.phase(record.operation,'COMMITTED',{binding:record.binding.id,group:this.group});this.current=record.binding;
      if(this.plan?.accepted){
        await this.finishPending(this.plan.operation);this.disposePlan();
      }
      this.state='READY';record.resolveReady();this.emit('ready',{binding:this.current.id,role:this.current.role});
      return{admission:null,committed:true};
    }
    if(type==='maintenance-begin'){
      if(this.plan||this.pendingOperation)throw new HostError('HANDOFF_UNCONFIRMED');
      if(!/^[a-f0-9]{32}$/.test(payload.operation))throw new HostError('PROTOCOL_INVALID');
      this.phase(payload.operation,'PLANNED',{action:'MAINTENANCE',binding:this.current.id});
      const admission=await this.beginPending(payload.operation);this.phase(payload.operation,'PENDING_DURABLE');
      return{admission};
    }
    if(type==='maintenance-finish'){
      const outcomes=this.receipts.records().filter(value=>value.operation_id===payload.operation&&value.state!=='PREPARED');
      const receipt=outcomes.find(value=>value.state!=='UNKNOWN_TREE');
      if(!receipt||outcomes.some(value=>value.state==='UNKNOWN_TREE')||receipt.tree_confirmed!==true||receipt.state==='UNKNOWN_TREE'||payload.operation!==this.pendingOperation){
        this.phase(payload.operation,'UNKNOWN_TREE');throw new HostError('HANDOFF_UNCONFIRMED');
      }
      await this.finishPending(payload.operation);return{admission:null};
    }
    if(type==='restores'){
      const offset=payload.offset??0;if(!Number.isSafeInteger(offset)||offset<0||offset>10000)throw new HostError('PROTOCOL_INVALID');
      const records=this.receipts.records().filter(value=>value.action==='restore'&&value.state==='COMPLETE'&&value.tree_confirmed).sort((a,b)=>b.observed_at.localeCompare(a.observed_at));
      return{items:records.slice(offset,offset+8).map(value=>({operation_id:value.operation_id,backup_id:value.package.backup_id,restore_id:value.restore.restore_id,observed_at:value.observed_at})),next:offset+8<records.length?offset+8:null};
    }
    if(type==='prepare')return this.prepare(payload);
    if(type==='cancel'){
      if(!this.plan||this.plan.accepted||this.plan.operation!==payload.operation)throw new HostError('HANDOFF_UNCONFIRMED');
      this.phase(this.plan.operation,'CANCELLED');this.disposePlan();return{};
    }
    if(type==='accept'){
      if(!this.plan||this.plan.accepted||this.plan.operation!==payload.operation)throw new HostError('HANDOFF_UNCONFIRMED');
      await this.revalidatePlan();const admission=await this.beginPending(this.plan.operation);this.plan.accepted=true;
      this.phase(this.plan.operation,'PENDING_DURABLE');this.state='HANDOFF';return{admission};
    }
    if(type==='source-stopped'){
      if(!this.plan?.accepted||this.plan.operation!==payload.operation||payload.confirmed!==true)throw new HostError('STOP_UNCONFIRMED');
      record.sourceStopped=true;this.phase(this.plan.operation,'SOURCE_STOPPED',{binding:record.binding.id});return{};
    }
    if(type==='cancel-after-stop'){
      if(!this.plan?.accepted||this.plan.operation!==payload.operation||!record.sourceStopped)throw new HostError('HANDOFF_UNCONFIRMED');
      this.plan.cancelled=true;this.state='QUARANTINED';this.phase(this.plan.operation,'CANCELLED_BEFORE_TARGET',{binding:record.binding.id});return{};
    }
    if(type==='stopped'){
      record.stopped=payload.confirmed===true;
      if(!record.stopped)this.phase(record.operation,'UNKNOWN_TREE');return{};
    }
    throw new HostError('PROTOCOL_INVALID');
  }
  async prepare(payload){
    if(this.plan||this.pendingOperation||this.state!=='READY')throw new HostError('HANDOFF_UNCONFIRMED');
    await this.validateBinding(this.current);let target,newBinding=false;
    if(payload.return_original===true){
      if(this.current.role!=='RESTORED')throw new HostError('PROTOCOL_INVALID');target=this.original;await this.validateBinding(target);
    }else{
      const receipt=this.receipts.records().find(value=>value.operation_id===payload.receipt&&value.action==='restore'&&value.state==='COMPLETE'&&value.tree_confirmed);
      if(!receipt||receipt.source_revision!==this.config.apiRevision||receipt.config_digest!==this.digest)throw new HostError('HANDOFF_UNCONFIRMED');
      const facts={...receipt.package,...receipt.restore},observation=await this.observe(payload.path,facts),entity=await this.entity(payload.path);
      if(!receipt.target_entity||!sameEntity(entity,receipt.target_entity)||!sameEntity(entity,observation)||entity.boot_sha256!==this.original.entity.boot_sha256)throw new HostError('HANDOFF_UNCONFIRMED');
      const known=this.bindings(),matched=known.filter(binding=>binding.entity.object_family_sha256===entity.object_family_sha256||binding.restore?.restore_id===facts.restore_id||binding.data_dir===payload.path);
      if(matched.length){
        if(matched.length!==1||matched[0].role!=='RESTORED'||matched[0].data_dir!==payload.path||!sameEntity(matched[0].entity,entity))throw new HostError('HANDOFF_UNCONFIRMED');
        target=matched[0];await this.validateBinding(target);
      }else{
        const identity=bindingId(),port=30000+Number.parseInt(identity.slice(0,8),16)%30000;
        if(known.some(binding=>binding.origin_port===port)||entity.object_family_sha256===this.original.entity.object_family_sha256)throw new HostError('HANDOFF_UNCONFIRMED');
        target={format:'loopcast.workspace-binding.v1',id:identity,group:this.group,role:'RESTORED',source_digest:this.digest,
          data_dir:payload.path,session_dir:join(this.lease.root,'profiles',identity),origin_port:port,entity,profile:null,
          original_binding:this.original.id,restore:{manifest_sha256:facts.manifest_sha256,backup_id:facts.backup_id,restore_id:facts.restore_id,
            guard_version:observation.guard_version,execution_epoch:observation.execution_epoch}};newBinding=true;
      }
    }
    if(target.id===this.current.id)throw new HostError('HANDOFF_UNCONFIRMED');
    const sourceHold=holdDirectory(this.current.data_dir),targetHold=holdDirectory(target.data_dir),profileHold=holdDirectory(this.current.session_dir);
    this.plan={operation:id(),source:this.current,target,newBinding,sourceHold,targetHold,profileHold};
    this.phase(this.plan.operation,'PLANNED',{source:this.current.id,target:target.id,group:this.group,new_binding:newBinding,
      source_entity:this.current.entity.object_sha256,target_entity:target.entity.object_sha256,source_digest:this.digest});
    return{operation:this.plan.operation,source:this.current.data_dir,target:target.data_dir,role:target.role,
      origin:`http://127.0.0.1:${target.origin_port}`,new_profile:newBinding,restore:target.restore};
  }
  async revalidatePlan(){
    if(!this.plan)throw new HostError('HANDOFF_UNCONFIRMED');
    await readConfig(this.filename,{expectedDigest:this.digest});await this.validateBinding(this.plan.source);
    this.plan.sourceHold.check();this.plan.targetHold.check();this.plan.profileHold.check();
    if(!sameEntity(await this.entity(this.plan.target.data_dir),this.plan.target.entity))throw new HostError('HANDOFF_UNCONFIRMED');
    if(this.plan.target.role==='RESTORED')await this.observe(this.plan.target,this.plan.target.restore);
    if(!this.plan.newBinding)await this.validateBinding(this.plan.target);
  }
  disposePlan(){if(!this.plan)return;for(const name of ['sourceHold','targetHold','profileHold'])this.plan[name]?.close();this.plan=null;}
  async exited(record,status){
    record.finished=true;record.rejectReady(new HostError('COMPONENT_EXITED'));
    const budget=new Budget(this.config.stopTimeoutMs);let result;
    try{result=await budget.run(record.owned.closeTree(budget.end));}catch{}
    record.confirmed=result?.confirmed===true&&status.code===0&&record.stopped===true;
    record.channel.close();
    if(this.closing)return;
    if(!record.confirmed){this.state='QUARANTINED';this.phase(record.operation,'UNKNOWN_TREE');this.emit('fault',new HostError('STOP_UNCONFIRMED'));return;}
    if(this.plan?.accepted&&record.sourceStopped&&!this.plan.cancelled){
      try{
        await this.revalidatePlan();
        if(record.binding.role==='RESTORED')await this.observe(record.binding,record.binding.restore);
        this.phase(this.plan.operation,'SOURCE_EXITED',{binding:record.binding.id});
        const target=this.plan.target;
        if(this.plan.newBinding){
          const root=holdDirectory(this.lease.root);
          try{try{mkdirSync(`${root.anchor}/profiles`,{mode:0o700});}catch(error){if(error.code!=='EEXIST')throw error;}fsyncSync(root.fd);}finally{root.close();}
          const profiles=holdDirectory(join(this.lease.root,'profiles'));
          try{mkdirSync(`${profiles.anchor}/${target.id}`,{mode:0o700});fsyncSync(profiles.fd);}finally{profiles.close();}
          target.profile=await this.entity(target.session_dir);this.phase(this.plan.operation,'BINDING',{binding:target});
        }
        this.phase(this.plan.operation,'TARGET_BOUND',{binding:target.id});await this.launch(target);
      }catch(error){this.state='QUARANTINED';this.phase(this.plan?.operation??record.operation,'UNKNOWN_RESULT');this.emit('fault',new HostError('HANDOFF_UNCONFIRMED'));}
    }else{this.state='STOPPED';this.emit('stopped');}
  }
  async close({confirmed=true}={}){
    if(this.closed)return this.closed;this.closing=true;
    this.closed=(async()=>{
      let safe=confirmed&&!this.unknownTree;
      for(const record of this.owned){
        if(!record.finished){await record.channel.request('quit',{challenge:record.challenge}).catch(()=>{});}
        const budget=new Budget(this.config?.stopTimeoutMs??10000);
        try{const status=await budget.run(record.exit.exited);const result=await budget.run(record.owned.closeTree(budget.end));if(!result?.confirmed||!record.stopped||status.code!==0)safe=false;}catch{safe=false;}
        record.channel.close();
      }
      if(this.pendingOperation)safe=false;
      this.disposePlan();this.receipts?.close();this.journal?.close();
      const lease=await this.lease?.close({confirmed:safe});this.state=safe&&lease?.confirmed?'STOPPED':'QUARANTINED';
      return{confirmed:this.state==='STOPPED'};
    })();return this.closed;
  }
}
