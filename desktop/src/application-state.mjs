// SPDX-License-Identifier: Apache-2.0
import { spawn } from 'node:child_process';
import { createHmac, randomBytes, timingSafeEqual } from 'node:crypto';
import { mkdirSync, readFileSync, fsyncSync } from 'node:fs';
import { isAbsolute, join } from 'node:path';
import { userInfo } from 'node:os';
import { Budget, HostError } from './errors.mjs';
import { readConfig, loadTreePort } from './config.mjs';
import { capture, observeExit } from './protocol.mjs';
import { hash, holdDirectory, readPrivate, writeNew } from './private-files.mjs';

export const applicationRoot = () => join(userInfo().homedir, '.local/state/loopcast-desktop');
const hex = (value, length=64) => typeof value === 'string' && new RegExp(`^[a-f0-9]{${length}}$`).test(value);

export async function privateCommand(config, module, request, { timeout=config.startTimeoutMs } = {}) {
  if (!['loopcast.desktop_admission','loopcast.desktop_workspace','loopcast.desktop_maintenance'].includes(module)) throw new HostError('PROTOCOL_INVALID');
  const tree=await loadTreePort(config), budget=new Budget(timeout);
  const owned=tree.launch({executable:config.python.path,argv:['-I','-m',module],cwd:config.sessionDir,env:tree.maintenanceEnvironment(),
    stdio:['pipe','pipe','pipe'],deadline:budget.end});
  owned.child.stdin.on('error',()=>{});owned.child.stdin.end(JSON.stringify(request));
  let output,error;
  try{output=await capture(owned,budget,65536);}catch(value){error=value;}
  const cleanup=new Budget(config.stopTimeoutMs);
  let result;
  try{result=await cleanup.run(owned.closeTree(cleanup.end));}catch{}
  if(!result?.confirmed){config.onUnconfirmedTree?.();throw new HostError('STOP_UNCONFIRMED');}
  if(error)throw error;
  return JSON.parse(output);
}

export class StateJournal {
  constructor(root) {
    this.root=holdDirectory(root);
    try{mkdirSync(`${this.root.anchor}/events`,{mode:0o700});}catch(error){if(error.code!=='EEXIST')throw error;}
    fsyncSync(this.root.fd);
    this.events=holdDirectory(join(root,'events'));
    try{this.key=readPrivate(join(root,'state-key'),32);}
    catch(error){if(error.code!=='ENOENT')throw error;this.key=randomBytes(32);writeNew(this.root,'state-key',this.key);}
    if(this.key.length!==32)throw new HostError('HANDOFF_UNCONFIRMED');
    this.load();
  }
  load() {
    const {readdirSync}=process.getBuiltinModule('fs');
    this.root.check();this.events.check();
    const names=readdirSync(this.events.anchor).sort();
    if(names.length>20000||names.some(name=>!/^\d{12}-[a-f0-9]{32}-[A-Z_]+\.json$/.test(name)))throw new HostError('HANDOFF_UNCONFIRMED');
    let previous='0'.repeat(64),sequence=0;
    const records=[];
    for(const name of names){
      const raw=readPrivate(join(this.events.path,name)),envelope=JSON.parse(raw),record=envelope.record;
      if(!raw.equals(Buffer.from(JSON.stringify(envelope)))||Object.keys(envelope).sort().join(',')!=='hmac,record'||!hex(envelope.hmac)||!record||
        Object.keys(record).sort().join(',')!=='format,operation_id,payload,phase,previous,sequence'||record.format!=='loopcast.application-event.v1'||
        record.sequence!==++sequence||record.previous!==previous||!hex(record.operation_id,32)||!/^[_A-Z]+$/.test(record.phase)||
        name!==`${String(sequence).padStart(12,'0')}-${record.operation_id}-${record.phase}.json`||
        !timingSafeEqual(createHmac('sha256',this.key).update(JSON.stringify(record)).digest(),Buffer.from(envelope.hmac,'hex')))
        throw new HostError('HANDOFF_UNCONFIRMED');
      previous=hash(raw);records.push(record);
    }
    this.records=records;this.sequence=sequence;this.previous=previous;return records;
  }
  append(operation,phase,payload) {
    this.load();
    if(!hex(operation,32)||!/^[_A-Z]+$/.test(phase))throw new HostError('PROTOCOL_INVALID');
    const record={format:'loopcast.application-event.v1',sequence:this.sequence+1,previous:this.previous,operation_id:operation,phase,payload};
    const raw=JSON.stringify({record,hmac:createHmac('sha256',this.key).update(JSON.stringify(record)).digest('hex')});
    if(Buffer.byteLength(raw)>65536)throw new HostError('PROTOCOL_INVALID');
    writeNew(this.events,`${String(record.sequence).padStart(12,'0')}-${operation}-${phase}.json`,raw);
    this.load();return record;
  }
  close(){this.events.close();this.root.close();this.key.fill(0);}
}

function bootstrapValue(value,root) {
  if(!value||Object.keys(value).sort().join(',')!=='base_python,electron,format,lease_script,lease_sha256,python_sha256'||
    value.format!=='loopcast.application-bootstrap.v1'||!isAbsolute(value.base_python)||!hex(value.python_sha256)||!hex(value.lease_sha256)||
    value.lease_script!==join(root,'lease.py')||!isAbsolute(value.electron?.path)||!hex(value.electron?.sha256))throw new HostError('HANDOFF_UNCONFIRMED');
  if(hash(readFileSync(value.base_python))!==value.python_sha256||hash(readPrivate(value.lease_script,32768))!==value.lease_sha256||
    hash(readFileSync(value.electron.path))!==value.electron.sha256)throw new HostError('RESOURCE_DIGEST_MISMATCH');
  return value;
}

export class ApplicationLease {
  static cached() {
    const root=applicationRoot();
    try{return bootstrapValue(JSON.parse(readPrivate(join(root,'bootstrap.json'))),root);}
    catch(error){if(error.code==='ENOENT')return null;throw error;}
  }
  static async acquire({filename,config:provided}={}) {
    const root=applicationRoot(),cached=this.cached();
    let config=provided,script,base,digest,electron;
    if(cached){({lease_script:script,base_python:base,python_sha256:digest,electron}=cached);}
    else{
      config??=await readConfig(filename);
      if(!config.electron)throw new HostError('CAPABILITY_UNAVAILABLE');
      const identity=await privateCommand(config,'loopcast.desktop_admission',{action:'state'});
      if(identity.format!=='loopcast.application-state.v1'||identity.root!==root||!isAbsolute(identity.lease_script)||
        !isAbsolute(identity.base_python)||!hex(identity.lease_sha256))throw new HostError('PROTOCOL_INVALID');
      script=identity.lease_script;base=identity.base_python;digest=config.python.sha256;electron=config.electron;
      if(hash(readFileSync(script))!==identity.lease_sha256||hash(readFileSync(base))!==digest||hash(readFileSync(electron.path))!==electron.sha256)throw new HostError('RESOURCE_DIGEST_MISMATCH');
    }
    const capability=randomBytes(32).toString('hex');
    // This fixed stdlib lease helper cannot fork. EOF retains the durable active
    // marker; only STOP after all owned trees have ended releases it normally.
    const child=spawn(base,['-I','-S',script,'lease'],{env:{PATH:'/usr/bin:/bin',LANG:'C.UTF-8'},stdio:['pipe','pipe','pipe']});
    const exit=observeExit(child);child.stderr.resume();child.stdin.on('error',()=>{});
    let buffer='';
    const ready=new Promise((resolve,reject)=>{
      child.stdout.on('data',chunk=>{buffer+=chunk;if(buffer.length>4096)reject(new HostError('PROTOCOL_INVALID'));else if(buffer.endsWith('\n'))resolve(buffer);});
      child.stdout.on('error',()=>reject(new HostError('PROTOCOL_INVALID')));
      exit.exited.then(status=>reject(new HostError(status.code===3?'OWNER_HELD':'HANDOFF_UNCONFIRMED')));
    });
    child.stdin.write(JSON.stringify({format:'loopcast.application-lease-request.v1',capability})+'\n');
    try{
      const response=JSON.parse(await new Budget(10000).run(ready));
      if(response.format!=='loopcast.application-lease.v1'||response.root!==root||response.identity!==hash(Buffer.from(capability,'hex')).slice(0,32))throw new HostError('PROTOCOL_INVALID');
      const lease=new ApplicationLease();Object.assign(lease,{root,child,exit,capability,electron});
      if(!cached){
        const directory=holdDirectory(root);
        try{
          const bytes=readFileSync(script);writeNew(directory,'lease.py',bytes);
          writeNew(directory,'bootstrap.json',JSON.stringify({format:'loopcast.application-bootstrap.v1',base_python:base,python_sha256:digest,
            lease_script:join(root,'lease.py'),lease_sha256:hash(bytes),electron}));
        }finally{directory.close();}
      }
      return lease;
    }catch(error){child.stdin.destroy();await new Budget(5000).run(exit.exited).catch(()=>child.kill('SIGKILL'));throw error;}
  }
  async close({confirmed=true}={}){
    if(this.closed)return this.closed;
    this.closed=(async()=>{if(confirmed)this.child.stdin.end('STOP\n');else this.child.stdin.destroy();
      const status=await new Budget(5000).run(this.exit.exited);this.capability=null;return{confirmed:confirmed&&status.code===0};})();
    return this.closed;
  }
}
