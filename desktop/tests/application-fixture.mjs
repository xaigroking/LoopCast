// SPDX-License-Identifier: Apache-2.0
// Disposable CI fixtures restore only their own application-state tail. The
// product has no corresponding cleanup, pending-reset or profile-delete API.
import { readdir,readFile,lstat,unlink,rm } from 'node:fs/promises';
import { join } from 'node:path';
import assert from 'node:assert/strict';
import { applicationRoot } from '../src/application-state.mjs';

async function names(path){try{return(await readdir(path)).sort();}catch(error){if(error.code==='ENOENT')return[];throw error;}}
export async function applicationFixture(){
  const root=applicationRoot(),started=Date.now();
  for(const name of ['active','pending']){
    try{await lstat(join(root,name));throw new Error('An existing application marker must not be touched by a test fixture');}
    catch(error){if(error.code!=='ENOENT')throw error;}
  }
  const baseline={};
  for(const directory of ['events','maintenance','profiles']){
    baseline[directory]=await names(join(root,directory));
  }
  const files=new Map();
  for(const directory of ['events','maintenance'])for(const name of baseline[directory])files.set(`${directory}/${name}`,await readFile(join(root,directory,name)));
  return async()=>{
    for(const [name,bytes]of files)assert.deepEqual(await readFile(join(root,name)),bytes,'A fixture changed a pre-existing application record');
    for(const name of ['active','pending']){
      let info;try{info=await lstat(join(root,name));}catch(error){if(error.code==='ENOENT')continue;throw error;}
      assert.ok(info.isFile()&&info.uid===process.getuid()&&info.nlink===1&&(info.mode&0o077)===0&&info.mtimeMs>=started-2);
      const value=JSON.parse(await readFile(join(root,name),'utf8'));assert.match(value.identity,/^[a-f0-9]{32}$/);
      await unlink(join(root,name));
    }
    for(const directory of ['events','maintenance','profiles']){
      const current=await names(join(root,directory));
      if(directory==='events')assert.deepEqual(current.slice(0,baseline[directory].length),baseline[directory]);
      for(const name of current.filter(name=>!baseline[directory].includes(name))){
        if(['key'].includes(name))continue;
        assert.ok(directory==='profiles'?/^[a-f0-9]{64}$/.test(name):directory==='events'?/^\d{12}-[a-f0-9]{32}-[A-Z_]+\.json$/.test(name):/^[a-f0-9]{32}-[A-Z_]+\.json$/.test(name));
        await rm(join(root,directory,name),{recursive:directory==='profiles'});
      }
    }
  };
}
