// SPDX-License-Identifier: Apache-2.0
import { randomBytes } from 'node:crypto';
import { StateJournal, privateCommand } from './application-state.mjs';
import { HostError } from './errors.mjs';

export class WorkerAdmission {
  constructor(config,{lease,channel,challenge,receipts}){Object.assign(this,{config,lease,channel,challenge,receipts});if(!channel)this.journal=new StateJournal(lease.root);}
  async begin(operation){
    if(this.operation)throw new HostError('HANDOFF_UNCONFIRMED');this.operation=operation;
    if(this.channel){const response=await this.channel.request('maintenance-begin',{challenge:this.challenge,operation});this.config.admission=response.admission;}
    else{
      const capability=randomBytes(32).toString('hex');this.journal.append(operation,'PLANNED',{action:'MAINTENANCE'});
      await privateCommand(this.config,'loopcast.desktop_admission',{action:'begin',identity:operation,capability});this.config.admission=capability;
      this.journal.append(operation,'PENDING_DURABLE',{});
    }
  }
  async finish(receipt){
    if(!this.operation||receipt.operation_id!==this.operation)throw new HostError('HANDOFF_UNCONFIRMED');
    const authenticated=this.receipts.records().find(value=>value.operation_id===this.operation&&value.state===receipt.state);
    if(!authenticated||!authenticated.tree_confirmed||authenticated.state==='UNKNOWN_TREE')throw new HostError('HANDOFF_UNCONFIRMED');
    if(this.channel){await this.channel.request('maintenance-finish',{challenge:this.challenge,operation:this.operation});}
    else{
      this.journal.append(this.operation,'FINISHED',{});
      await privateCommand(this.config,'loopcast.desktop_admission',{action:'finish',identity:this.operation,capability:this.config.admission});
    }
    this.config.admission=null;this.operation=null;
  }
  close(){this.journal?.close();}
}
