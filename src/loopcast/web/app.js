const app = document.querySelector('#app');
const sync = document.querySelector('#sync');
const dialog = document.querySelector('#create-dialog');
const terminal = new Set(['SUCCEEDED', 'FAILED', 'CANCELLED']);
const settled = run => terminal.has(run.state) && !(run.publications || []).some(p => ['PREPARED', 'SENDING'].includes(p.state));
const entities = new Map();
const pending = new Map();
let view = 0, subscription = null, timer = null, pageCursor = null, listing = [], paged = false;
let createIdentity = null;

export const runURL = id => `/runs/${encodeURIComponent(id)}`;
export function accept(snapshot) {
  if (!snapshot || !(/^[a-f0-9]{32}$/).test(snapshot.run_id) || !Number.isInteger(snapshot.state_version) || !Number.isInteger(snapshot.last_event_id)) return false;
  const previous = entities.get(snapshot.run_id);
  if (previous && (snapshot.state_version < previous.state_version || snapshot.last_event_id < previous.last_event_id)) return false;
  entities.set(snapshot.run_id, snapshot);
  return true;
}
function el(tag, text, cls) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (cls) node.className = cls;
  return node;
}
function button(text, fn, cls = '') {
  const node = el('button', text, cls);
  node.addEventListener('click', fn);
  return node;
}
function link(text, href) {
  const node = el('a', text); node.href = href;
  if (href.startsWith('/runs/') || href === '/') node.dataset.route = '';
  return node;
}
function status(state) {
  const node = el('span', state.replaceAll('_', ' '), 'state');
  node.dataset.state = state;
  return node;
}
function notice(message) {
  const node = document.querySelector('#notice');
  node.replaceChildren(el('span', message), button('Dismiss', () => { node.hidden = true; }, 'quiet'));
  node.hidden = false;
}
async function request(path, options = {}) {
  const response = await fetch(path, {cache: 'no-store', ...options});
  const data = await response.json();
  if (!response.ok) {
    if (data.error?.snapshot) accept(data.error.snapshot);
    const error = new Error(data.error?.message || 'Request failed.');
    error.status = response.status; error.code = data.error?.code;
    throw error;
  }
  return data;
}
function clean() {
  if (subscription) subscription.close();
  subscription = null;
  clearTimeout(timer); timer = null;
}
function setSync(text) { sync.textContent = text; }
function actionButtons(run, menu = false) {
  const group = el('div', undefined, menu ? 'menu' : 'row-actions');
  for (const action of ['cancel', 'retry']) {
    const policy = run.allowed_actions[action];
    const node = button(action === 'cancel' ? 'Cancel' : 'Retry', () => command(run.run_id, action), action === 'cancel' ? 'danger' : '');
    node.disabled = !policy.enabled || pending.get(`${run.run_id}:${action}`)?.busy === true;
    node.title = policy.reason_code;
    node.dataset.action = action;
    group.append(node);
  }
  return group;
}
function openCreate() {
  document.querySelector('#form-error').textContent = '';
  dialog.showModal();
  dialog.querySelector('input').focus();
}
function heading(eyebrow, title, subtitle, withCreate = false) {
  const node = el('section', undefined, 'heading');
  const text = el('div');
  text.append(el('div', eyebrow, 'eyebrow'), el('h1', title), el('p', subtitle));
  node.append(text);
  if (withCreate) node.append(button('+ New fixture', openCreate, 'primary'));
  return node;
}
function renderList() {
  document.querySelector('.badge').textContent = 'FIXTURE MODE';
  const runs = listing.map(id => entities.get(id)).filter(Boolean);
  app.replaceChildren(heading('WORKSPACE / TASKS', 'Your task console', 'A clear view of every local fixture run.', true));
  const stats = el('div', undefined, 'stats');
  for (const [label, count] of [['Loaded tasks', runs.length], ['In progress', runs.filter(r => !terminal.has(r.state)).length], ['Completed', runs.filter(r => r.state === 'SUCCEEDED').length]]) {
    const node = el('div', undefined, 'stat'); node.append(el('strong', String(count)), el('span', label)); stats.append(node);
  }
  app.append(stats);
  const title = el('div', undefined, 'section-head'); title.append(el('h2', 'Recent activity'), el('small', 'Newest first · durable local history')); app.append(title);
  if (!runs.length) {
    const empty = el('section', undefined, 'empty');
    empty.append(el('div', '◇', 'big'), el('h2', 'A fresh start.'), el('p', 'Create a fixture to watch real stages execute, explore retry and cancellation, and open the generated report.'), button('Create your first fixture', openCreate, 'primary'));
    app.append(empty);
  }
  const cards = el('div', undefined, 'cards');
  for (const run of runs) {
    const card = el('article', undefined, 'run-card'); card.dataset.runId = run.run_id;
    const main = el('div', undefined, 'run-main');
    const name = link(run.input.title, runURL(run.run_id)); name.className = 'run-title';
    main.append(name, el('div', `${run.mode === 'fixture' ? 'Fixture' : 'MPT · live unverified'} · Attempt ${run.attempts.length} · v${run.state_version}`, 'run-meta'));
    const direct = actionButtons(run); direct.classList.add('direct-action');
    const menu = el('details'); menu.append(el('summary', '•••'), actionButtons(run, true));
    const controls = el('div', undefined, 'row-actions'); controls.append(direct, menu);
    card.append(el('div', '◇', 'run-icon'), main, status(run.state), controls); cards.append(card);
  }
  app.append(cards);
  if (pageCursor) { const more = button('Load older tasks', () => loadList(true)); more.id = 'load-more'; app.append(more); }
}
function panel(title) { const node = el('section', undefined, 'panel'); node.append(el('h2', title)); return node; }
function renderDetail(run) {
  document.querySelector('.badge').textContent = run.mode === 'fixture' ? 'FIXTURE MODE' : 'GENERATION · LIVE UNVERIFIED';
  const back = link('← All tasks', '/'); back.className = 'back';
  const title = heading(run.mode === 'fixture' ? 'FIXTURE / TASK DETAIL' : 'GENERATION / TASK DETAIL', run.input.title, `Attempt ${run.attempts.length} · State version ${run.state_version}`);
  title.classList.add('detail-title');
  const controls = el('div', undefined, 'row-actions'); controls.append(status(run.state), actionButtons(run)); title.append(controls);
  const grid = el('div', undefined, 'detail-grid');
  const left = el('div'), right = el('div');
  const stages = panel('Execution stages');
  run.tasks.forEach((task, index) => {
    const node = el('div', undefined, 'stage'); node.append(el('span', String(index + 1).padStart(2, '0'), 'stage-number'), el('span', task.stage), status(task.state)); stages.append(node);
  });
  if (run.state === 'CANCEL_REQUESTED') stages.append(el('p', 'Stopping execution. Cancellation is not complete until the executor exits.'));
  left.append(stages);
  const history = panel('Attempt history');
  for (const attempt of [...run.attempts].reverse()) {
    const node = el('div', undefined, 'attempt');
    node.append(el('strong', `Attempt ${attempt.number} `), status(attempt.state), el('div', attempt.started_at ? new Date(attempt.started_at).toLocaleString() : 'Waiting for the executor', 'label'));
    if (attempt.error) node.append(el('div', attempt.error, 'error'));
    if (attempt.evidence) node.append(el('small', attempt.evidence));
    history.append(node);
  }
  left.append(history, publicationPanel(run));
  const info = panel('Run information');
  for (const [key, value] of [['Run identity', run.run_id], ['Mode', run.mode === 'fixture' ? 'Fixture only' : 'MPT · contract tested, live unverified'], ['Scenario', run.input.scenario], ['Created', new Date(run.created_at).toLocaleString()], ['Last activity', new Date(run.updated_at).toLocaleString()], ['Event cursor', String(run.last_event_id)]]) info.append(el('div', key, 'label'), el('div', value, 'value'));
  right.append(info);
  const reports = panel(run.mode === 'fixture' ? 'Reports' : 'Generated media');
  if (run.component) reports.append(el('p', `Component ${run.component.revision} · ${run.component.verification} · real operation unverified`));
  for (const artifact of run.artifacts) {
    const node = link(`${artifact.media_type === 'video/mp4' ? '↗ Open video' : '↗ Open fixture report'} · ${artifact.size} bytes`, `/v1/artifacts/${encodeURIComponent(artifact.id)}/content`);
    if (artifact.media_type === 'video/mp4') { const video = el('video'); video.controls = true; video.preload = 'metadata'; video.src = node.href; video.style.width = '100%'; reports.append(video); }
    node.className = 'report'; node.target = '_blank'; node.rel = 'noopener'; reports.append(node, el('small', `SHA256 ${artifact.sha256}`));
  }
  if (!run.artifacts.length) reports.append(el('p', 'No report is ready. A successful fixture creates a text report here.'));
  right.append(reports); grid.append(left, right);
  app.replaceChildren(back, title, grid);
}
async function loadList(more = false) {
  const generation = view;
  try {
    const data = await request('/v1/runs?limit=30' + (more && pageCursor ? `&before=${pageCursor}` : ''));
    if (generation !== view || location.pathname !== '/') return;
    data.items.forEach(accept);
    listing = more ? [...new Set([...listing, ...data.items.map(r => r.run_id)])] : [...new Set([...data.items.map(r => r.run_id), ...listing])];
    if (more || !paged) pageCursor = data.next_cursor;
    if (more) paged = true;
    renderList(); setSync(navigator.onLine ? 'Synced · polling' : 'Offline · stale');
  } catch (error) { setSync('Disconnected · stale'); if (!listing.length) showError(error); }
  if (generation === view && location.pathname === '/') { clearTimeout(timer); timer = setTimeout(() => loadList(), 1500); }
}
function follow(run, generation) {
  clean();
  if (settled(run)) { setSync('Settled · refresh to sync'); return; }
  if (!navigator.onLine) { setSync('Offline · stale'); return; }
  const source = new EventSource(`/v1/runs/${run.run_id}/events?after=${run.last_event_id}`);
  subscription = source;
  source.onopen = () => { if (view === generation) setSync('Live · connected'); };
  const receive = event => {
    if (generation !== view || subscription !== source) return;
    let snapshot;
    try { snapshot = JSON.parse(event.data); } catch { source.close(); return; }
    if (snapshot.run_id !== run.run_id) return;
    if (event.lastEventId && Number(event.lastEventId) !== snapshot.last_event_id) return;
    if (accept(snapshot)) renderDetail(entities.get(run.run_id));
    if (settled(entities.get(run.run_id))) { clean(); setSync('Settled · refresh to sync'); }
  };
  source.addEventListener('snapshot', receive);
  source.addEventListener('complete', receive);
  source.onerror = () => {
    if (generation !== view || subscription !== source) return;
    clean(); setSync('Disconnected · stale; reconnecting');
    timer = setTimeout(() => loadDetail(run.run_id, generation), 1200);
  };
}
async function loadDetail(id, generation = view) {
  try {
    const snapshot = await request(`/v1/runs/${id}`);
    if (generation !== view) return;
    if (snapshot.run_id !== id) throw new Error('Response identity did not match this task.');
    accept(snapshot); const run = entities.get(id); renderDetail(run); follow(run, generation);
  } catch (error) {
    if (generation !== view) return;
    showError(error);
    if (!error.status) timer = setTimeout(() => loadDetail(id, generation), 1500);
  }
}
function showError(error) {
  const node = el('section', undefined, 'empty'); node.append(el('h2', error.status === 404 ? 'Task or address not found' : 'Unable to load this view'), el('p', error.message), link('← Return to tasks', '/'));
  app.replaceChildren(node); setSync(error.status ? 'Request failed' : 'Offline · stale');
}
function route() {
  ++view; clean(); app.replaceChildren(el('p', 'Loading workspace…'));
  setSync(navigator.onLine ? 'Syncing…' : 'Offline · stale');
  if (location.pathname === '/') return loadList();
  const match = location.pathname.match(/^\/runs\/([a-f0-9]{32})$/);
  if (match) return loadDetail(match[1]);
  showError({status: 404, message: 'This address does not exist.'});
}
async function command(id, action) {
  const run = entities.get(id), identity = `${id}:${action}`;
  let operation = pending.get(identity);
  if (operation?.busy || !run.allowed_actions[action].enabled) return;
  if (!operation && action === 'cancel' && !confirm('Request cancellation of this fixture? Execution must stop before it is marked cancelled.')) return;
  operation ||= {key: crypto.randomUUID(), version: run.state_version};
  operation.busy = true; pending.set(identity, operation);
  const generation = view;
  if (location.pathname === '/') renderList(); else renderDetail(run);
  try {
    const result = await request(`/v1/runs/${id}/${action}`, {method: 'POST', headers: {'Content-Type': 'application/json', 'Idempotency-Key': operation.key}, body: JSON.stringify({expected_version: operation.version})});
    if (result.run_id !== id) throw new Error('Response identity did not match this task.');
    accept(result); pending.delete(identity);
    if (generation === view) { if (location.pathname === '/') renderList(); else { const current = entities.get(id); renderDetail(current); follow(current, generation); } }
  } catch (error) {
    if (error.status) pending.delete(identity); else operation.busy = false;
    notice(error.status ? error.message : 'Response lost. Retry the same action safely to confirm its result.');
    if (generation === view) { if (location.pathname === '/') loadList(); else loadDetail(id, generation); }
  }
}
document.addEventListener('click', event => {
  const target = event.target.closest('a[data-route]');
  if (!target || event.ctrlKey || event.metaKey || event.shiftKey || event.button !== 0) return;
  event.preventDefault(); history.pushState(null, '', target.href); route();
});
window.addEventListener('popstate', route);
window.addEventListener('offline', () => { clean(); setSync('Offline · stale'); });
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
  if (createIdentity?.signature !== signature) createIdentity = {signature, key: crypto.randomUUID()};
  submit.disabled = true; submit.textContent = 'Creating…'; document.querySelector('#form-error').textContent = '';
  try {
    const result = await request('/v1/runs', {method: 'POST', headers: {'Content-Type': 'application/json', 'Idempotency-Key': createIdentity.key}, body: signature});
    accept(result); createIdentity = null; dialog.close(); form.reset(); form.elements.mode.dispatchEvent(new Event('change'));
    history.pushState(null, '', runURL(result.run_id)); route();
  } catch (error) { document.querySelector('#form-error').textContent = error.message + ' You can retry safely.'; }
  finally { submit.disabled = false; submit.textContent = form.elements.mode.value === 'mpt' ? 'Generate video →' : 'Create fixture →'; }
});
route();

request('/v1/capabilities').then(caps => { const option = dialog.querySelector('option[value=mpt]'); option.disabled = !caps.video_generation; option.textContent = caps.video_generation ? 'MPT generation · real operation unverified' : 'Generation · configure runner first'; }).catch(() => {});

function publicationPanel(run) {
  const node = panel('Publication simulator');
  node.append(el('p', 'Controlled local receipts only. Real platform sending is unavailable.'));
  const accountLabel = el('label', 'Simulator account');
  const account = el('input'); account.value = 'demo'; account.maxLength = 64; accountLabel.append(account);
  const create = button('Add simulated target', () => publicationCommand(run, null, null, {platform: 'simulator', account: account.value}));
  create.disabled = !run.allowed_actions.add_simulated_target?.enabled;
  node.append(accountLabel, create);
  for (const intent of run.publications || []) {
    const target = el('div', undefined, 'attempt'); target.dataset.intentId = intent.id;
    target.append(el('strong', `${intent.platform} / ${intent.account} · simulated `), status(intent.state));
    const label = el('label', 'Simulator outcome');
    const scenario = el('select'); scenario.setAttribute('aria-label', 'Simulator outcome');
    for (const [value, text] of [['success','Confirmed simulator receipt'], ['submitted','Submitted without receipt'], ['lost_response','Receipt saved, response lost'], ['not_sent','Rejected before sending'], ['slow','Interrupted local sender']]) {
      const option = el('option', text); option.value = value; scenario.append(option);
    }
    label.append(scenario); target.append(label);
    for (const [action, text] of [['dispatch','Dispatch simulation'], ['verify','Check simulator receipt']]) {
      const actionButton = button(text, () => publicationCommand(run, intent.id, action, action === 'dispatch' ? {scenario: scenario.value} : {}));
      actionButton.disabled = !intent.allowed_actions[action].enabled || pending.get(`${intent.id}:${action}`)?.busy === true;
      actionButton.title = intent.allowed_actions[action].reason_code; target.append(actionButton);
    }
    if (intent.state === 'UNKNOWN') target.append(el('p', 'Result unknown. Another send is blocked until evidence resolves the previous publisher.'));
    for (const attempt of intent.attempts) target.append(el('small', `Dispatch ${attempt.id}: ${attempt.state}`));
    for (const receipt of intent.receipts) target.append(el('p', `${receipt.source} · ${receipt.outcome} · ${receipt.work_id || 'no work identity'} · ${receipt.evidence}`));
    node.append(target);
  }
  return node;
}
async function publicationCommand(run, intentId, action, extra) {
  const identity = `${intentId || run.run_id}:${action || 'target'}`;
  let operation = pending.get(identity);
  if (operation?.busy) return;
  const signature = JSON.stringify(extra);
  if (!operation || operation.signature !== signature) operation = {key: crypto.randomUUID(), version: run.state_version, signature};
  operation.busy = true; pending.set(identity, operation);
  const path = `/v1/runs/${run.run_id}/publications` + (intentId ? `/${intentId}/${action}` : '');
  try {
    const result = await request(path, {method: 'POST', headers: {'Content-Type':'application/json','Idempotency-Key':operation.key}, body:JSON.stringify({...extra, expected_version:operation.version})});
    accept(result); pending.delete(identity);
    if (location.pathname === runURL(run.run_id)) { renderDetail(entities.get(run.run_id)); follow(entities.get(run.run_id), view); }
  } catch (error) {
    if (error.status) pending.delete(identity); else operation.busy = false;
    notice(error.message); if (location.pathname === runURL(run.run_id)) loadDetail(run.run_id);
  }
}

dialog.querySelector('select[name=mode]').addEventListener('change', event => {
  const generation = event.target.value === 'mpt';
  dialog.querySelector('h2').textContent = generation ? 'Generate a video.' : 'Give your workflow a test run.';
  dialog.querySelector('.eyebrow').textContent = generation ? 'ISOLATED GENERATION' : 'NEW FIXTURE';
  dialog.querySelector('form > p').textContent = generation ? 'Uses your configured local MPT environment and generation provider. Real platform sending is unavailable; real operation remains unverified.' : 'Run fixed local stages and generate a real text report. No models or publishing services are called.';
  for (const name of ['scenario', 'delay_ms']) dialog.querySelector(`[name=${name}]`).closest('label').hidden = generation;
  dialog.querySelector('button[type=submit]').textContent = generation ? 'Generate video →' : 'Create fixture →';
});
