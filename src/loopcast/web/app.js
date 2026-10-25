import {requestKey} from './requests.js';
import {t, registry, number, date, locale, errorText, packError} from './localization.js';
const app = document.querySelector('#app'), sync = document.querySelector('#sync'), dialog = document.querySelector('#create-dialog');
const preferences = window.workspacePreferences;
const terminal = new Set(['SUCCEEDED','FAILED','CANCELLED']);
const settled = run => terminal.has(run.state) && !(run.publications || []).some(p => ['PREPARED','SENDING'].includes(p.state));
const entities = new Map(), pending = new Map(), drafts = new Map();
let view=0, subscription=null, timer=null, pageCursor=null, listing=[], paged=false, createIdentity=null, generatorAvailable=false;
let syncKey='syncing', createOpener=null;
export const runURL = id => `/runs/${encodeURIComponent(id)}`;
export function accept(snapshot) {
  if (!snapshot || !(/^[a-f0-9]{32}$/).test(snapshot.run_id) || !Number.isInteger(snapshot.state_version) || !Number.isInteger(snapshot.last_event_id)) return false;
  const old=entities.get(snapshot.run_id);
  if (old && (snapshot.state_version<old.state_version || snapshot.last_event_id<old.last_event_id)) return false;
  entities.set(snapshot.run_id,snapshot); return true;
}
function el(tag,text,cls) { const node=document.createElement(tag); if(text!==undefined)node.textContent=text; if(cls)node.className=cls; return node; }
function button(text,action,cls='') { const node=el('button',text,cls);node.type='button';node.addEventListener('click',action);return node; }
function link(text,href) {const node=el('a',text);node.href=href;if(href==='/' || href==='/settings' || href.startsWith('/runs/'))node.dataset.route='';return node;}
function isolated(tag,text,cls='') {const node=el(tag,text,cls);node.dir='auto';return node;}
function code(text) {const node=el('bdi',text);node.dir='ltr';return node;}
function status(state) {const node=el('span',t('state:'+state),'state');node.dataset.state=state;return node;}
function notice(message) { const node=document.querySelector('#notice');node.replaceChildren(el('span',message),button(t('dismiss'),()=>node.hidden=true,'quiet'));node.hidden=false; }
async function request(path,options={}) {
  const response=await fetch(path,{cache:'no-store',...options});const data=await response.json();
  if(!response.ok) { if(data.error?.snapshot)accept(data.error.snapshot);const error=new Error();error.status=response.status;error.code=data.error?.code;throw error; }
  return data;
}
function clean(){if(subscription)subscription.close();subscription=null;clearTimeout(timer);timer=null;}
function setSync(key){syncKey=key;sync.textContent=t(key);}
function field(node,key,value){node.dataset.draftKey=key;node.value=drafts.has(key)?drafts.get(key):value;return node;}
function replaceApp(...nodes){
  const active=document.activeElement, focus=active?.dataset.focusKey || active?.dataset.draftKey;
  const selection=active && 'selectionStart' in active ? [active.selectionStart,active.selectionEnd] : null;
  app.replaceChildren(...nodes);
  if(focus){const next=app.querySelector(`[data-focus-key="${CSS.escape(focus)}"],[data-draft-key="${CSS.escape(focus)}"]`);next?.focus({preventScroll:true});if(selection && next?.type==='text')next.setSelectionRange(...selection);}
}
document.addEventListener('input',event=>{if(event.target.dataset.draftKey)drafts.set(event.target.dataset.draftKey,event.target.value);});
document.addEventListener('change',event=>{if(event.target.dataset.draftKey)drafts.set(event.target.dataset.draftKey,event.target.value);});
function openCreate(){createOpener=document.activeElement;document.querySelector('#form-error').textContent='';dialog.showModal();dialog.querySelector('input').focus();}
dialog.addEventListener('close',()=>{const target=createOpener?.isConnected?createOpener:document.querySelector('.heading .primary');target?.focus({preventScroll:true});});
function actionButtons(run,menu=false){
 const group=el('div',undefined,menu?'menu':'row-actions');
 for(const action of ['cancel','retry']){const policy=run.allowed_actions[action];const node=button(t(action),()=>command(run.run_id,action),action==='cancel'?'danger':'');
 node.disabled=!policy.enabled||pending.get(`${run.run_id}:${action}`)?.busy===true;node.title=policy.enabled?t(action):t('unavailable');node.dataset.action=action;node.dataset.focusKey=run.run_id+action+(menu?'menu':'');group.append(node);}return group;
}
function heading(title,subtitle,withCreate=false){const node=el('section',undefined,'heading');const text=el('div');text.append(el('div',t('workspace'),'eyebrow'),isolated('h1',title),el('p',subtitle));node.append(text);if(withCreate)node.append(button(t('new'),openCreate,'primary'));return node;}
function panel(title){const node=el('section',undefined,'panel');node.append(el('h2',title));return node;}
function renderList(){
 document.querySelector('.badge').textContent=t('fixtureBadge');
 const runs=listing.map(id=>entities.get(id)).filter(Boolean);
 replaceApp(heading(t('console'),t('subtitle'),true));
 const stats=el('div',undefined,'stats');
 for(const [key,count] of [['loaded',runs.length],['active',runs.filter(r=>!settled(r)).length],['completed',runs.filter(r=>r.state==='SUCCEEDED').length]]){
 const node=el('div',undefined,'stat');node.append(el('strong',number(count)),el('span',t(key)));stats.append(node);}app.append(stats);
 const section=el('div',undefined,'section-head');section.append(el('h2',t('recent')),el('small',t('taskCount',{count:runs.length})));app.append(section);
 if(!runs.length){const empty=el('section',undefined,'empty');empty.append(el('div','▤','empty-icon'),el('h2',t('empty')),el('p',t('fixtureNotice')) );app.append(empty);}
 const cards=el('div',undefined,'cards');
 for(const run of runs){const card=el('article',undefined,'run-card');card.dataset.runId=run.run_id;const main=el('div',undefined,'run-main');
 const name=link(run.input.title,runURL(run.run_id));name.dir='auto';name.className='run-title';
 main.append(name,el('div',`${t(run.mode==='fixture'?'fixture':'unverified')} · ${t('attemptVersion',{number:number(run.attempts.length),version:number(run.state_version)})}`,'run-meta'));
 const direct=actionButtons(run);direct.classList.add('direct-action');const menu=el('details');menu.open=drafts.get(run.run_id+':menu')===true;menu.addEventListener('toggle',()=>drafts.set(run.run_id+':menu',menu.open));
 const summary=el('summary','•••');summary.setAttribute('aria-label',t('settings')+' · '+run.input.title);menu.append(summary,actionButtons(run,true));
 const controls=el('div',undefined,'row-actions');controls.append(direct,menu);card.append(el('div','▤','run-icon'),main,status(run.state),controls);cards.append(card);}
 app.append(cards);if(pageCursor){const more=button(t('older'),()=>loadList(true));more.id='load-more';app.append(more);}
}
function renderDetail(run){
 document.querySelector('.badge').textContent=t(run.mode==='fixture'?'fixtureBadge':'unverified');
 const back=link(t('back'),'/');back.className='back';
 const title=heading(run.input.title,t('attemptVersion',{number:number(run.attempts.length),version:number(run.state_version)}));title.classList.add('detail-title');
 const controls=el('div',undefined,'row-actions');controls.append(status(run.state),actionButtons(run));title.append(controls);
 const grid=el('div',undefined,'detail-grid'),left=el('div'),right=el('div'),stages=panel(t('stages'));
 run.tasks.forEach((task,index)=>{const node=el('div',undefined,'stage');node.append(el('span',number(index+1),'stage-number'),el('span',t('state:'+task.stage)),status(task.state));stages.append(node);});
 if(run.state==='CANCEL_REQUESTED')stages.append(el('p',t('cancelConfirm')));left.append(stages);
 const history=panel(t('history'));
 for(const attempt of [...run.attempts].reverse()){const node=el('div',undefined,'attempt');node.append(el('strong',t('attempt',{number:number(attempt.number)})),status(attempt.state),el('div',attempt.started_at?date(attempt.started_at):t('waiting'),'label'));
 if(attempt.error)node.append(el('div',errorText({code:attempt.error}),'error'));if(attempt.evidence)node.append(isolated('small',attempt.evidence,'evidence'));history.append(node);}left.append(history,publicationPanel(run));
 const info=panel(t('information'));
 for(const [key,value] of [['identity',run.run_id],['mode',t(run.mode==='fixture'?'fixture':'unverified')],['scenario',run.input.scenario?t(({success:'success',fail_once:'failOnce',fail:'fail',slow:'slow'})[run.input.scenario]):'—'],['created',date(run.created_at)],['updated',date(run.updated_at)],['cursor',number(run.last_event_id)]])info.append(el('div',t(key),'label'),isolated('div',value,'value'));
 right.append(info);const reports=panel(t(run.mode==='fixture'?'reports':'media'));
 if(run.component)reports.append(el('p',t('unverified')),code(run.component.revision));
 for(const artifact of run.artifacts){const node=link(`${t(artifact.media_type==='video/mp4'?'openVideo':'openReport')} · ${t('bytes',{size:number(artifact.size)})}`,`/v1/artifacts/${encodeURIComponent(artifact.id)}/content`);
 if(artifact.media_type==='video/mp4'){const video=el('video');video.controls=true;video.preload='metadata';video.src=node.href;video.setAttribute('aria-label',t('openVideo'));reports.append(video);}
 node.className='report';node.target='_blank';node.rel='noopener';reports.append(node,code('SHA256 '+artifact.sha256));}
 if(!run.artifacts.length)reports.append(el('p',t('noArtifact')));right.append(reports);grid.append(left,right);replaceApp(back,title,grid);
}
function publicationPanel(run){
 const node=panel(t('publication'));node.append(el('p',t('subtitle')));
 const accountLabel=el('label',t('account'));const account=field(el('input'),run.run_id+':account','demo');account.maxLength=64;accountLabel.append(account);
 const create=button(t('addTarget'),()=>publicationCommand(run,null,null,{platform:'simulator',account:account.value}));create.disabled=!run.allowed_actions.add_simulated_target?.enabled||pending.get(`${run.run_id}:target`)?.busy===true;node.append(accountLabel,create);
 for(const intent of run.publications||[]){const target=el('div',undefined,'attempt');target.dataset.intentId=intent.id;target.append(isolated('strong',intent.platform+' / '+intent.account),status(intent.state));
 const label=el('label',t('outcome')),scenario=el('select');scenario.setAttribute('aria-label',t('outcome'));
 for(const [value,key] of [['success','confirmed'],['submitted','submitted'],['lost_response','lost'],['not_sent','notSent'],['slow','slow']]){const option=el('option',t(key));option.value=value;scenario.append(option);}field(scenario,intent.id+':scenario','success');label.append(scenario);target.append(label);
 for(const [action,key] of [['dispatch','dispatch'],['verify','verify']]){const node=button(t(key),()=>publicationCommand(run,intent.id,action,action==='dispatch'?{scenario:scenario.value}:{}));node.disabled=!intent.allowed_actions[action].enabled||pending.get(`${intent.id}:${action}`)?.busy===true;node.title=intent.allowed_actions[action].enabled?t(key):t('unknownHint');node.dataset.focusKey=intent.id+action;target.append(node);}
 if(intent.state==='UNKNOWN')target.append(el('p',t('unknownHint')));
 for(const attempt of intent.attempts){const item=el('small');item.append(code(attempt.id),document.createTextNode(' · '+t('state:'+attempt.state)));target.append(item);}
 for(const receipt of intent.receipts){const item=el('p');item.append(code(receipt.source),document.createTextNode(' · '+t('state:'+receipt.outcome)+' · '),code(receipt.work_id||'—'),isolated('span',receipt.evidence,'evidence'));target.append(item);}node.append(target);}return node;
}
function renderSettings(){
 const title=heading(t('settings'),t('preferencesHint')),content=panel(t('language'));
 const search=field(el('input'),'language-search','');search.type='search';search.setAttribute('aria-label',t('searchLanguage'));search.placeholder=t('searchLanguage');
 const label=el('label',t('language')),select=el('select');select.id='language-select';select.dataset.focusKey='language-select';
 function options(){const query=search.value.toLocaleLowerCase(locale());const saved=preferences.value.language;select.replaceChildren();
 const auto=el('option',t('autoLanguage'));auto.value='auto';select.append(auto);
 const names=new Intl.DisplayNames([locale()],{type:'language'});
 for(const item of registry){const text=item.name+' · '+names.of(item.code);if(!text.toLocaleLowerCase(locale()).includes(query)&&item.code!==saved)continue;const option=el('option',text);option.value=item.code;select.append(option);}select.value=registry.some(x=>x.code===saved)?saved:'auto';}
 options();search.addEventListener('input',options);select.addEventListener('change',()=>preferences.set({language:select.value}));label.append(select);content.append(search,label);
 const appearance=panel(t('appearance')),themeLabel=el('label',t('appearance')),theme=el('select');theme.id='theme-select';theme.dataset.focusKey='theme-select';
 for(const value of ['system','light','dark']){const option=el('option',t(value));option.value=value;theme.append(option);}theme.value=preferences.value.theme;theme.addEventListener('change',()=>preferences.set({theme:theme.value}));themeLabel.append(theme);appearance.append(themeLabel);
 if(!preferences.available)appearance.append(el('p',t('storageHint')));if(packError)content.append(el('p',t('packError'),'error'));
 const grid=el('div',undefined,'settings-grid');grid.append(content,appearance);replaceApp(link(t('back'),'/'),title,grid);
}
function showError(error){const node=el('section',undefined,'empty');node.append(el('h2',t(error.status===404?'missing':'failedLoad')),el('p',errorText(error)),link(t('back'),'/'));replaceApp(node);setSync(error.status?'requestError':'offline');}
function route(){++view;clean();replaceApp(el('p',t('loading')));setSync(navigator.onLine?'syncing':'offline');if(location.pathname==='/settings')return renderSettings();if(location.pathname==='/')return loadList();const match=location.pathname.match(/^\/runs\/([a-f0-9]{32})$/);if(match)return loadDetail(match[1]);showError({status:404,code:'NOT_FOUND'});}

async function loadList(more = false) {
  const generation = view;
  try {
    const data = await request('/v1/runs?limit=30' + (more && pageCursor ? `&before=${pageCursor}` : ''));
    if (generation !== view || location.pathname !== '/') return;
    data.items.forEach(accept);
    listing = more ? [...new Set([...listing, ...data.items.map(r => r.run_id)])] : [...new Set([...data.items.map(r => r.run_id), ...listing])];
    if (more || !paged) pageCursor = data.next_cursor;
    if (more) paged = true;
    renderList(); setSync(navigator.onLine ? 'polling' : 'offline');
  } catch (error) { setSync('offline'); if (!listing.length) showError(error); }
  if (generation === view && location.pathname === '/') { clearTimeout(timer); timer = setTimeout(() => loadList(), 1500); }
}
function follow(run, generation) {
  clean();
  if (settled(run)) { setSync('settled'); return; }
  if (!navigator.onLine) { setSync('offline'); return; }
  const source = new EventSource(`/v1/runs/${run.run_id}/events?after=${run.last_event_id}`);
  subscription = source;
  source.onopen = () => { if (view === generation) setSync('live'); };
  const receive = event => {
    if (generation !== view || subscription !== source) return;
    let snapshot;
    try { snapshot = JSON.parse(event.data); } catch { source.close(); return; }
    if (snapshot.run_id !== run.run_id) return;
    if (event.lastEventId && Number(event.lastEventId) !== snapshot.last_event_id) return;
    if (accept(snapshot)) renderDetail(entities.get(run.run_id));
    if (settled(entities.get(run.run_id))) { clean(); setSync('settled'); }
  };
  source.addEventListener('snapshot', receive);
  source.addEventListener('complete', receive);
  source.onerror = () => {
    if (generation !== view || subscription !== source) return;
    clean(); setSync('offline');
    timer = setTimeout(() => loadDetail(run.run_id, generation), 1200);
  };
}
async function loadDetail(id, generation = view) {
  try {
    const snapshot = await request(`/v1/runs/${id}`);
    if (generation !== view) return;
    if (snapshot.run_id !== id) throw Object.assign(new Error(),{code:'INTERNAL_ERROR'});
    accept(snapshot); const run = entities.get(id); renderDetail(run); follow(run, generation);
  } catch (error) {
    if (generation !== view) return;
    showError(error);
    if (!error.status) timer = setTimeout(() => loadDetail(id, generation), 1500);
  }
}
async function command(id, action) {
  const run = entities.get(id), identity = `${id}:${action}`;
  let operation = pending.get(identity);
  if (operation?.busy || !run.allowed_actions[action].enabled) return;
  if (!operation && action === 'cancel' && !confirm(t('cancelConfirm'))) return;
  try { operation ||= {key: requestKey(), version: run.state_version}; }
  catch (error) { notice(errorText(error)); return; }
  operation.busy = true; pending.set(identity, operation);
  const generation = view;
  if (location.pathname === '/') renderList(); else renderDetail(run);
  try {
    const result = await request(`/v1/runs/${id}/${action}`, {method: 'POST', headers: {'Content-Type': 'application/json', 'Idempotency-Key': operation.key}, body: JSON.stringify({expected_version: operation.version})});
    if (result.run_id !== id) throw Object.assign(new Error(),{code:'INTERNAL_ERROR'});
    accept(result); pending.delete(identity);
    if (generation === view) { if (location.pathname === '/') renderList(); else { const current = entities.get(id); renderDetail(current); follow(current, generation); } }
  } catch (error) {
    if (error.status) pending.delete(identity); else operation.busy = false;
    notice(error.status ? errorText(error) : t('retrySafe'));
    if (generation === view) { if (location.pathname === '/') loadList(); else loadDetail(id, generation); }
  }
}
async function publicationCommand(run, intentId, action, extra) {
  const identity = `${intentId || run.run_id}:${action || 'target'}`;
  let operation = pending.get(identity);
  if (operation?.busy) return;
  const signature = JSON.stringify(extra);
  try { if (!operation || operation.signature !== signature) operation = {key: requestKey(), version: run.state_version, signature}; }
  catch (error) { notice(errorText(error)); return; }
  operation.busy = true; pending.set(identity, operation);
  const path = `/v1/runs/${run.run_id}/publications` + (intentId ? `/${intentId}/${action}` : '');
  try {
    const result = await request(path, {method: 'POST', headers: {'Content-Type':'application/json','Idempotency-Key':operation.key}, body:JSON.stringify({...extra, expected_version:operation.version})});
    accept(result); pending.delete(identity);
    if (location.pathname === runURL(run.run_id)) { renderDetail(entities.get(run.run_id)); follow(entities.get(run.run_id), view); }
  } catch (error) {
    if (error.status) pending.delete(identity); else operation.busy = false;
    notice(errorText(error)); if (location.pathname === runURL(run.run_id)) loadDetail(run.run_id);
  }
}

document.addEventListener('click', event => {
  const target = event.target.closest('a[data-route]');
  if (!target || event.ctrlKey || event.metaKey || event.shiftKey || event.button !== 0) return;
  event.preventDefault(); history.pushState(null, '', target.href); route();
});
window.addEventListener('popstate', route);
window.addEventListener('offline', () => { clean(); setSync('offline'); });
window.addEventListener('online', route);
window.addEventListener('pagehide', clean);
window.addEventListener('focus', () => { if (!dialog.open) route(); });
document.querySelector('#refresh').addEventListener('click', route);
document.querySelector('#dismiss').addEventListener('click', () => dialog.close());
document.querySelector('#create-form').addEventListener('submit', async event => {
  event.preventDefault();
  const form = event.currentTarget, submit = form.querySelector('button[type=submit]');
  if (submit.disabled) return;
  const body = form.elements.mode.value === 'mpt' ? {mode: 'mpt', title: form.elements.title.value} : {mode: 'fixture', title: form.elements.title.value, scenario: form.elements.scenario.value, delay_ms: Number(form.elements.delay_ms.value)};
  const signature = JSON.stringify(body);
  try { if (createIdentity?.signature !== signature) createIdentity = {signature, key: requestKey()}; }
  catch (error) { document.querySelector('#form-error').textContent = errorText(error); return; }
  submit.disabled = true; submit.textContent = t('creating'); document.querySelector('#form-error').textContent = '';
  try {
    const result = await request('/v1/runs', {method: 'POST', headers: {'Content-Type': 'application/json', 'Idempotency-Key': createIdentity.key}, body: signature});
    accept(result); createIdentity = null; dialog.close(); form.reset(); form.elements.mode.dispatchEvent(new Event('change'));
    history.pushState(null, '', runURL(result.run_id)); route();
  } catch (error) { document.querySelector('#form-error').textContent = error.status ? errorText(error) : t('retrySafe'); }
  finally { submit.disabled = false; submit.textContent = t(form.elements.mode.value === 'mpt' ? 'generate' : 'create'); }
});

function localizeShell(){
 document.querySelectorAll('[data-i18n]').forEach(node=>node.textContent=t(node.dataset.i18n));
 document.title='LoopCast · '+t(location.pathname==='/settings'?'settings':'console');
 document.querySelector('#language-shortcut').textContent=registry.find(x=>x.code===locale())?.name||'English';
 sync.textContent=t(syncKey);localizeForm();
}
function localizeForm(){
 const form=document.querySelector('#create-form'),generation=form.elements.mode.value==='mpt';
 document.querySelector('#form-title').textContent=t(generation?'generate':'new');
 document.querySelector('#form-description').textContent=t(generation?'unverified':'fixtureNotice');
 const option=form.querySelector('option[value=mpt]');option.disabled=!generatorAvailable;option.textContent=t(generatorAvailable?'unverified':'unavailable');
 for(const name of ['scenario','delay_ms'])form.querySelector(`[name=${name}]`).closest('label').hidden=generation;
 form.querySelector('button[type=submit]').textContent=t(generation?'generate':'create');
}
dialog.querySelector('select[name=mode]').addEventListener('change',localizeForm);
document.querySelector('#dialog-settings').addEventListener('click',()=>{dialog.close();history.pushState(null,'','/settings');route();});
function rerender(){localizeShell();if(location.pathname==='/settings')renderSettings();else if(location.pathname==='/')renderList();else{const run=entities.get(location.pathname.split('/')[2]);if(run)renderDetail(run);}}
window.addEventListener('localechange',rerender);
request('/v1/capabilities').then(caps=>{generatorAvailable=caps.video_generation;localizeForm();}).catch(()=>{});
dialog.querySelectorAll('input').forEach(input=>{input.addEventListener('invalid',()=>input.setCustomValidity(t('invalid')));input.addEventListener('input',()=>input.setCustomValidity(''));});
localizeShell();route();
