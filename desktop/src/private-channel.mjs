// SPDX-License-Identifier: Apache-2.0
import { randomBytes } from 'node:crypto';
import { Budget, HostError, safeError } from './errors.mjs';

export class PrivateChannel {
  constructor(stream,{timeout=30000,onRequest=async()=>{throw new HostError('PROTOCOL_INVALID');},onClosed=()=>{}}={}) {
    Object.assign(this,{stream,timeout,onRequest,onClosed});this.pending=new Map();this.buffer=Buffer.alloc(0);this.queue=Promise.resolve();
    stream.on('error',()=>this.close());stream.on('end',()=>this.close());stream.on('close',()=>this.close());
    stream.on('data',chunk=>{
      this.buffer=Buffer.concat([this.buffer,chunk]);
      while(this.buffer.includes(10)){
        const end=this.buffer.indexOf(10),raw=this.buffer.subarray(0,end);this.buffer=this.buffer.subarray(end+1);
        try{
          if(raw.length>65536)throw new Error();const value=JSON.parse(raw.toString('utf8'));
          if(!raw.equals(Buffer.from(JSON.stringify(value)))||!value||typeof value!=='object'||!/^[_a-z-]+$/.test(value.type)||!/^[a-f0-9]{32}$/.test(value.id)||
            Object.keys(value).sort().join(',')!=='id,payload,type')throw new Error();
          if(value.type==='result'||value.type==='error'){
            const task=this.pending.get(value.id);if(!task)throw new Error();this.pending.delete(value.id);
            if(value.type==='error')task.reject(safeError(new HostError(value.payload?.code)));else task.resolve(value.payload);
          }else{
            this.queue=this.queue.then(async()=>{try{const payload=await this.onRequest(value.type,value.payload);this.send('result',value.id,payload);}catch(error){this.send('error',value.id,{code:safeError(error).code});}}).catch(()=>this.close());
          }
        }catch{this.close();return;}
      }
      if(this.buffer.length>65536)this.close();
    });
  }
  send(type,id,payload){if(this.closed)throw new HostError('HANDOFF_UNCONFIRMED');this.stream.write(JSON.stringify({type,id,payload})+'\n');}
  async request(type,payload={}){
    if(this.closed||this.pending.size>=32)throw new HostError('HANDOFF_UNCONFIRMED');
    const id=randomBytes(16).toString('hex');
    try{return await new Budget(this.timeout).run(new Promise((resolve,reject)=>{this.pending.set(id,{resolve,reject});this.send(type,id,payload);}));}
    catch(error){this.pending.delete(id);this.close();throw error;}
  }
  close(){if(this.closed)return;this.closed=true;for(const task of this.pending.values())task.reject(new HostError('HANDOFF_UNCONFIRMED'));this.pending.clear();this.stream.destroy();this.onClosed();}
}
