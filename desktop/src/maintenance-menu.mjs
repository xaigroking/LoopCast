// SPDX-License-Identifier: Apache-2.0
import { dirname, basename } from 'node:path';
import { holdDirectory, writeNew } from './private-files.mjs';

const text = {
  en: { menu:'Maintenance', create:'Create backup…', verify:'Verify backup…', restore:'Restore to a new directory…', cancel:'Cancel maintenance', back:'Return to original workspace',
    target:'Choose a new private directory name', backup:'Select the backup directory', proof:'Select an independent digest record outside the backup',
    title:'Review maintenance plan', stop:'The current workspace and active tasks will stop normally. Completion leaves it offline until you explicitly return.',
    readonly:'Verification reads the package without stopping the workspace.', execute:'Execute plan', dismiss:'Cancel plan',
    cancelled:'Cancelled before the maintenance writer started. Return to the workspace explicitly if it is offline.',
    stages:{STOPPING:'Stopping the workspace',OFFLINE:'Workspace offline',EXECUTING:'Running maintenance',CHECKING:'Checking the result',COMPLETE:'Maintenance finished',QUARANTINED:'Execution remains blocked',STARTING:'Starting the original workspace'},
    result:'Maintenance result', complete:'The operation completed and its private receipt was saved.', unknown:'The result is unconfirmed. Preserve the target and evidence; do not repeat a write or overwrite the target.',
    restored:'Restored to a new directory. Uploads remain blocked by the recovery barrier. The restored workspace has not been started.',
    offline:'Workspace offline', progress:'Offline maintenance', close:'Close', export:'Save independent digest record…', saved:'Digest record saved.',
    original:'Original workspace', digest:'Expected manifest SHA256', source:'Source', output:'Target', provenance:'Digest source', audit:'Backup audit' },
  'zh-Hans': { menu:'维护', create:'创建备份…', verify:'校验备份…', restore:'恢复到全新目录…', cancel:'取消维护', back:'返回原工作区',
    target:'选择全新的私有目录名称', backup:'选择备份目录', proof:'选择备份包外的独立摘要记录',
    title:'核对维护计划', stop:'将正常停止当前工作区及活动任务。完成后保持离线，需由你显式返回原工作区。',
    readonly:'校验只读取备份包，不停止当前工作区。', execute:'执行此计划', dismiss:'取消计划',
    cancelled:'已在维护写入执行者启动前取消。如工作区已离线，需显式返回。',
    stages:{STOPPING:'正在正常停止工作区',OFFLINE:'工作区已离线',EXECUTING:'正在执行维护',CHECKING:'正在核对结果',COMPLETE:'维护已结束',QUARANTINED:'执行仍受屏障限制',STARTING:'正在启动原工作区'},
    result:'维护结果', complete:'操作完成，已保存私有回执。', unknown:'结果尚未确认。请保留目标及证据，不要重复写入或覆盖目标。',
    restored:'已恢复到全新目录，上传仍受恢复屏障限制。尚未启动恢复工作区。',
    offline:'工作区已离线', progress:'离线维护', close:'关闭', export:'另存独立摘要记录…', saved:'已保存独立摘要记录。',
    original:'原工作区', digest:'预期清单 SHA256', source:'来源', output:'目标', provenance:'摘要来源', audit:'备份审计' },
};

// Electron objects are supplied only by the main entry. There is no renderer
// bridge or product test injection surface for dialogs, paths or commands.
export function installMaintenanceMenu({ Menu, dialog, app, coordinator, locale, report, showOffline, workspace }) {
  const t = text[locale] ?? text.en;
  let presenting = false;
  async function action(name) {
    if (coordinator.busy || presenting) return;
    presenting = true;
    try {
      coordinator.reserve();
      let target, backup, proofPath;
      if (name !== 'create') {
        const chosen = await dialog.showOpenDialog({ title:t.backup, properties:['openDirectory','dontAddToRecent'] });
        if (chosen.canceled || coordinator.cancelled) { coordinator.abandon(); return; }
        backup = chosen.filePaths[0];
        const proof = await dialog.showOpenDialog({ title:t.proof, properties:['openFile','dontAddToRecent'] });
        if (proof.canceled || coordinator.cancelled) { coordinator.abandon(); return; }
        proofPath = proof.filePaths[0];
      }
      if (name !== 'verify') {
        const chosen = await dialog.showSaveDialog({ title:t.target, defaultPath:name === 'create' ? 'loopcast-backup' : 'loopcast-restored', properties:['dontAddToRecent'] });
        if (chosen.canceled || coordinator.cancelled) { coordinator.abandon(); return; }
        target = chosen.filePath;
      }
      const plan = coordinator.prepare(name, { target, backup, proofPath });
      const detail = [`${t.source}: ${plan.source}`, plan.backup && `${t.backup}: ${plan.backup}`, plan.target && `${t.output}: ${plan.target}`,
        plan.expected_digest && `${t.digest}: ${plan.expected_digest}`, plan.digest_origin && `${t.provenance}: ${plan.digest_origin}`,
        `Source revision: ${plan.source_revision}`, `Operation: ${plan.operation_id}`].filter(Boolean).join('\n');
      const accepted = await dialog.showMessageBox({ type:'question', title:t.title, message:plan.stops_workspace ? t.stop : t.readonly,
        detail, buttons:[t.execute,t.dismiss], defaultId:1, cancelId:1, noLink:true });
      if (accepted.response !== 0 || coordinator.cancelled) { coordinator.abandon(); return; }
      const receipt = await coordinator.execute();
      const complete = receipt?.state === 'COMPLETE' && coordinator.state !== 'QUARANTINED';
      const result = await dialog.showMessageBox({ type:complete ? 'info' : 'warning', title:t.result,
        message:complete ? name === 'restore' ? t.restored : t.complete : receipt?.state === 'CANCELLED_BEFORE_EXECUTION' ? t.cancelled : t.unknown,
        detail:`State: ${receipt?.state ?? 'UNKNOWN_TREE'}\n${receipt?.package?.audit ? `${t.audit}: ${receipt.package.audit}\n` : ''}Operation: ${receipt?.operation_id ?? plan.operation_id}`,
        buttons:complete && name === 'create' ? [t.close,t.export] : [t.close], noLink:true });
      if (complete && name === 'create' && result.response === 1) {
        const selected = await dialog.showSaveDialog({ title:t.export, defaultPath:'loopcast-backup-proof.json', properties:['dontAddToRecent'] });
        if (!selected.canceled) {
          coordinator.protect(selected.filePath);
          if (selected.filePath.startsWith(target+'/')) throw new Error('Protected package');
          const parent = holdDirectory(dirname(selected.filePath));
          try { writeNew(parent, basename(selected.filePath), coordinator.exportProof(receipt.operation_id)); } finally { parent.close(); }
        }
      }
    } catch (error) {
      if (coordinator.busy && !coordinator.running) coordinator.abandon();
      await report(error);
    } finally { presenting = false; rebuild(); }
  }
  function rebuild() {
    const ready = ['READY','OFFLINE','COMPLETE'].includes(coordinator.state) && !coordinator.busy && !presenting;
    Menu.setApplicationMenu(Menu.buildFromTemplate([{ label:t.menu, submenu:[
      ...['create','verify','restore'].map(name => ({ id:`maintenance-${name}`, label:t[name], accelerator:{create:'CommandOrControl+Shift+B',verify:'CommandOrControl+Shift+V',restore:'CommandOrControl+Shift+R'}[name], enabled:ready, click:() => { void action(name); } })),
      ...(workspace ? [{id:'workspace-open',label:locale==='zh-Hans'?'打开已恢复工作区…':'Open restored workspace…',
        accelerator:'CommandOrControl+Shift+O',enabled:ready&&workspace.role==='ORIGINAL',click:()=>{void workspace.open();}}] : []),
      { type:'separator' }, { label:t.cancel, enabled:coordinator.busy, click:() => coordinator.cancel() },
      { id:'maintenance-return', label:t.back, enabled:!presenting && !coordinator.busy && (workspace?.role==='RESTORED'?['READY','OFFLINE','COMPLETE']:['OFFLINE','COMPLETE']).includes(coordinator.state),
        accelerator:'CommandOrControl+Shift+U',
        click:() => { void (workspace?.role==='RESTORED'?workspace.returnOriginal():coordinator.returnOriginal()).catch(report); } },
      { type:'separator' }, { role:'quit', label:locale === 'zh-Hans' ? '退出' : 'Quit' },
    ] }]));
    if (!['READY','SELECTING','PLANNED','VERIFYING'].includes(coordinator.state)) showOffline(t.progress, t.stages[coordinator.state] ?? t.progress);
  }
  coordinator.on('state', rebuild); rebuild();
  return () => coordinator.off('state', rebuild);
}
