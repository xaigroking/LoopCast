// SPDX-License-Identifier: Apache-2.0
import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { DesktopSupervisor, safeEnvironment } from '../src/supervisor.mjs';
import { ApplicationLease } from '../src/application-state.mjs';
import { configPath } from '../src/config.mjs';
import { Budget, safeError } from '../src/errors.mjs';

const supervisor=new DesktopSupervisor(()=>configPath(process.argv,{packaged:false}));
let finishing=false;
async function finish(confirmed){if(finishing)return;finishing=true;const result=await supervisor.close({confirmed}).catch(()=>({confirmed:false}));process.exitCode=result.confirmed?0:1;}
for(const signal of ['SIGINT','SIGTERM'])process.once(signal,()=>{void finish(true);});
supervisor.on('stopped',()=>{void finish(true);});
supervisor.on('fault',()=>{void finish(false);});
try{await supervisor.start();}
catch(error){
  if(error?.code==='OWNER_HELD'){
    const cached=ApplicationLease.cached();
    const child=spawn(cached.electron.path,[fileURLToPath(new URL('../',import.meta.url)),'--focus-only'],{stdio:'ignore',env:safeEnvironment()});
    try{await new Budget(10000).run(new Promise(resolve=>child.once('exit',resolve)));}finally{if(child.exitCode===null)child.kill('SIGKILL');}
  }else{process.stderr.write(safeError(error).code+'\n');process.exitCode=1;}
}
