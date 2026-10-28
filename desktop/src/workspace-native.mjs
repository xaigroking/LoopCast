// SPDX-License-Identifier: Apache-2.0
import { HostError } from './errors.mjs';
export function nativeWorkspaceActions({app,dialog,channel,challenge,config,coordinator,role,report}){
  const zh=config.locale==='zh-Hans';
  async function change(returnOriginal){
    if(coordinator.busy)return;
    let operation,accepted=false;
    try{
      coordinator.reserve();let path,receipt;
      if(!returnOriginal){
        let offset=0;
        while(!receipt){
          const batch=await channel.request('restores',{challenge,offset}),choices=batch.items;
          if(!choices.length)throw new HostError('RESTORE_SOURCE_REQUIRED');
          const buttons=[...choices.map(value=>`${value.restore_id.slice(0,12)} · ${value.observed_at}`),...(batch.next!==null?[zh?'更早的回执':'Older receipts']:[]),zh?'取消':'Cancel'];
          const selected=await dialog.showMessageBox({type:'question',title:zh?'选择可信恢复回执':'Choose a trusted restore receipt',
            message:zh?'核对独立备份来源与恢复标识。上传屏障将保持有效。':'Review the independent backup source and restore identity. Uploads remain guarded.',
            buttons,defaultId:buttons.length-1,cancelId:buttons.length-1,noLink:true});
          if(selected.response===buttons.length-1||coordinator.cancelled){coordinator.abandon();return;}
          if(selected.response===choices.length){offset=batch.next;continue;}
          receipt=choices[selected.response].operation_id;
        }
        const selectedPath=await dialog.showOpenDialog({title:zh?'选择已恢复的私有工作区':'Choose the restored private workspace',properties:['openDirectory','dontAddToRecent']});
        if(selectedPath.canceled||coordinator.cancelled){coordinator.abandon();return;}path=selectedPath.filePaths[0];
      }
      const plan=await channel.request('prepare',{challenge,return_original:returnOriginal,path,receipt});operation=plan.operation;
      const reviewed=await dialog.showMessageBox({type:'question',title:zh?'核对工作区交接计划':'Review workspace handoff',
        message:zh?'将正常停止此工作区并退出当前窗口，再启动独立工作区进程。未决写入不会自动重投，上传屏障不会解除。':'This workspace will stop normally and its window will exit before a separate workspace process starts. Pending writes will not replay; upload barriers remain in force.',
        detail:`${zh?'原工作区':'Source'}: ${plan.source}\n${zh?'目标工作区':'Target'}: ${plan.target}\n${zh?'固定入口':'Stable origin'}: ${plan.origin}\n${zh?'独立浏览器配置':'Independent profile'}: ${plan.new_profile?(zh?'首次建立':'Create once'):(zh?'复用原绑定':'Reuse its binding')}\n${zh?'操作标识':'Operation'}: ${operation}`,
        buttons:[zh?'执行此计划':'Execute plan',zh?'取消计划':'Cancel plan'],defaultId:1,cancelId:1,noLink:true});
      if(reviewed.response!==0||coordinator.cancelled){await channel.request('cancel',{challenge,operation});coordinator.abandon();return;}
      const permission=await channel.request('accept',{challenge,operation});accepted=true;config.admission=permission.admission;
      await coordinator.stopForHandoff();
      await channel.request('source-stopped',{challenge,operation,confirmed:true});
      if(coordinator.cancelled){
        await channel.request('cancel-after-stop',{challenge,operation});coordinator.transition('QUARANTINED');return;
      }
      app.quit();
    }catch(error){
      if(!accepted){if(operation)await channel.request('cancel',{challenge,operation}).catch(()=>{});if(coordinator.busy)coordinator.abandon();}
      else coordinator.transition('QUARANTINED');
      await report(error);
    }
  }
  return{role,open:()=>change(false),returnOriginal:()=>change(true)};
}
