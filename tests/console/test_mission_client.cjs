/* The desk page's rendering and frames without a browser (D068): workspaces, reasons, roles, routing and escaping.
   不依赖浏览器验证任务台页面的渲染与帧（D068）：工作区、原因、角色、路由与转义。
   Run: node --test tests/console/test_mission_client.cjs */
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const root = path.resolve(__dirname, '../..');
const html = fs.readFileSync(path.join(root, 'src/drone_agent/console/mission.html'), 'utf8');
const source = fs.readFileSync(path.join(root, 'src/drone_agent/console/mission.js'), 'utf8');

function setup({answer = 'reason'} = {}) {
  const nodes = new Map();
  class Element {
    constructor() { this.textContent = ''; this.hidden = false; this.value = ''; this.disabled = false; this.html = ''; this.dataset = {}; this.attrs = {}; this.className = ''; }
    set innerHTML(value) { this.html = value; for (const m of value.matchAll(/id="([^"]+)"/g)) nodes.set(m[1], new Element()); }
    get innerHTML() { return this.html; }
    querySelectorAll() { return []; }
    setAttribute(k, v) { this.attrs[k] = String(v); }
    removeAttribute(k) { delete this.attrs[k]; }
    hasAttribute(k) { return k in this.attrs; }
  }
  for (const m of html.matchAll(/id="([^"]+)"/g)) nodes.set(m[1], new Element());
  const sent = [], copied = [];
  const listeners = {};
  class Socket { constructor() { this.readyState = 1; } send(text) { sent.push(JSON.parse(text)); } }
  const location = {protocol: 'https:', host: 'desk.test', hash: ''};
  const context = vm.createContext({
    document: {getElementById: id => nodes.get(id) || (nodes.set(id, new Element()), nodes.get(id)),
      addEventListener: (kind, fn) => { listeners[kind] = fn; }, visibilityState: 'visible', title: ''},
    window: {addEventListener: (kind, fn) => { listeners['window:' + kind] = fn; }},
    WebSocket: Socket, location, crypto: {randomUUID: () => '0123456789abcdef0123456789abcdef'},
    navigator: {clipboard: {writeText: async value => copied.push(value)}},
    setTimeout: () => 0, clearTimeout: () => {}, setInterval: () => 0, prompt: () => answer, console, JSON, Date,
  });
  vm.runInContext(source, context);
  return {nodes, sent, copied, location, listeners, run: code => vm.runInContext(code, context), html: id => nodes.get(id).innerHTML};
}

const HELLO = {type: 'hello', protocol: 'hri.v0', identity: 'tailnet:ops', can_write: true, source_sha: 'a'.repeat(40), volumes: [], assets: [], planner: 'scripted',
  projects: [{project_id: 'campus_s1', name: 'Campus / 园区', roles: ['admin', 'approver', 'operator', 'reviewer'], robots: ['uav_01'], legacy: false, scheduling: true, business: true}]};

const resourcesView = {
  project_id: 'campus_s1', roles: ['admin', 'approver', 'operator'], evaluated_at: '2026-09-26T00:00:00+00:00',
  catalog: {catalog_id: 'p1_s1_v1', policy: {version: 'p1-dispatch-v1'}},
  sites: [{site_id: 'site_s1', docks: [{dock_id: 'dock_s1<script>', source: 'logical_sim', pad: 'uncertain', holder: 'mission:m-1:v1', serves: ['uav_01'],
    status: {link: 'online', fresh: false, age_s: 4.2, session: 'active', lid: 'closed', aircraft: 'present',
      energy: {state: 'charging', charge_fraction: 0.62}, environment: {state: 'permitted', wind_mps: 2}, upkeep: 'normal',
      lock: {state: 'maintenance'}}}],
    robots: [{robot_id: 'uav_01', execution_backend: 'px4_sitl', capability_source: 'published', status: null,
      eligibility: {verdict: 'blocked', reasons: ['dock.status_stale', 'energy.charging', 'dock.maintenance']}}]}],
};

test('online is never shown as dispatchable and every reason is named', () => {
  const ui = setup();
  ui.run(`hello = ${JSON.stringify(HELLO)}; projectId = 'campus_s1'; resources = ${JSON.stringify(resourcesView)}; renderResources();`);
  const list = ui.html('resourceList'), cards = ui.html('fleetOverview');
  assert.match(list, /在线/);
  assert.match(list, /不可派遣/);
  assert.match(list, /机场状态过期（&gt;3 s）；充电中；维护锁定/);
  assert.match(list, /占用待对账 · 过期 · 4\.2 s 前/);
  assert.match(cards, /过期 · 4\.2 s 前/);
  assert.match(cards, /锁定：维护/);
  assert.match(cards, /解除维护锁（管理员）/);
  assert.match(cards, /“在线”不等于可派遣/);
  for (const out of [list, cards]) assert.doesNotMatch(out, /<script>/, 'values from the service are escaped');
  ui.run(`resources.roles = ['operator']; renderResources();`);
  assert.doesNotMatch(ui.html('fleetOverview'), /解除维护锁/, 'only an admin is offered the lock release');
});

function missionView(overrides = {}) {
  return {
    mission: {mission_id: 'm-000000000001', status: 'queued', current_version: 1, replans: 0},
    request: {text: 'inspect', channel: 'console', requested_by: 'tailnet:ops'},
    versions: [{version: 1, status: 'queued', origin: 'console', package_hash: 'ab', package: null, provenance: {},
      journals: {}, events: [], spec: null, planner: null, admission: null, approval: null, diff: null}],
    live: null, evidence: [], operations: [], report: null, issues: [], facts: [], cloud: null,
    binding: {project_id: 'campus_s1', site_id: 'site_s1', dock_id: 'dock_s1', robot_id: 'uav_01', execution_backend: 'px4_sitl'},
    dispatch: {decisions: [{version: 1, stage: 'claim', verdict: 'blocked', reasons: ['dock.lid_not_open'], created_at: 'x'}],
      reservations: [{mission_version: 1, state: 'reserved', reason: 'reserved'}], claims: [], cancel: null, dock_actions: [], events: []},
    ...overrides,
  };
}

test('the mission detail shows the binding, dispatch decisions and a pre-claim cancel', () => {
  const ui = setup();
  ui.run(`hello = {can_write: true, projects: []}; nav = {ws: 'missions', id: 'm-000000000001', kind: null};
    current = ${JSON.stringify(missionView())}; renderMission();`);
  const out = ui.html('detail');
  assert.equal(ui.nodes.get('detail').hidden, false);
  assert.match(out, /派遣（P1）/);
  assert.match(out, /campus_s1 \/ site_s1/);
  assert.match(out, /领取.*不可派遣.*舱盖未打开/s);
  assert.match(out, /id="cancelMission"/);
  ui.run(`current.dispatch.cancel = {requested_by: 'tailnet:ops', requested_at: '2026-09-26T00:00:01+00:00', relayed_request_id: null}; renderMission();`);
  assert.doesNotMatch(ui.html('detail'), /id="cancelMission"/, 'a persisted cancel is not offered twice');
});

test('legacy missions are read-only history without a dispatch section', () => {
  const ui = setup();
  ui.run(`hello = {can_write: true, projects: []}; nav = {ws: 'missions', id: 'm-000000000002', kind: null};
    current = ${JSON.stringify(missionView({mission: {mission_id: 'm-000000000002', status: 'completed', current_version: 1, replans: 0},
      binding: {project_id: 'legacy_m2', legacy: true}, dispatch: null}))}; renderMission();`);
  const out = ui.html('detail');
  assert.match(out, /历史任务（只读）/);
  assert.doesNotMatch(out, /id="cancelMission"/);
});

test('approval binds the package hash and needs the approver role of the mission\'s project', () => {
  const ui = setup();
  const view = missionView({mission: {mission_id: 'm-000000000003', status: 'awaiting_approval', current_version: 1, replans: 0},
    versions: [{version: 1, status: 'awaiting_approval', origin: 'console', package_hash: 'f'.repeat(64), package: {nodes: []}, provenance: {},
      journals: {}, events: [], spec: {goal: 'Inspect <img src=x onerror=alert(1)>'}, planner: null, admission: null, approval: null, diff: null}]});
  ui.run(`hello = ${JSON.stringify(HELLO)}; projectId = 'campus_s1'; nav = {ws: 'missions', id: 'm-000000000003', kind: null};
    current = ${JSON.stringify(view)}; renderMission();`);
  let out = ui.html('detail');
  assert.match(out, /id="approve"[^>]*data-hash="f{64}"/);
  assert.doesNotMatch(out, /id="approve"[^>]*disabled/);
  assert.doesNotMatch(out, /<img src=x/, 'the model goal is escaped');
  assert.match(out, /<li class="now warn" aria-current="step">审批<\/li>/, 'the lifecycle marks the approval stage');
  ui.run(`hello.projects[0].roles = ['operator']; renderMission();`);
  out = ui.html('detail');
  assert.match(out, /id="approve"[^>]*disabled/, 'an operator without the approver role cannot sign');
  ui.run(`act('approve', {id: 'm-000000000003', v: '1', hash: '${'f'.repeat(64)}'});`);
  assert.deepEqual(ui.sent.at(-1), {type: 'approve', mission_id: 'm-000000000003', version: 1, package_hash: 'f'.repeat(64)});
});

test('a report is provisional while the mission runs and leads the detail only once it ended', () => {
  const ui = setup();
  const report = {targets: {asset_red: 'not_completed'}, summary: {completed: 0, not_completed: 1, uncertain: 0}, rows: [], facts: []};
  ui.run(`hello = {can_write: true, projects: []}; nav = {ws: 'missions', id: 'm-000000000005', kind: null};
    current = ${JSON.stringify(missionView({mission: {mission_id: 'm-000000000005', status: 'running', current_version: 1, replans: 0}, report}))}; renderMission();`);
  let out = ui.html('detail');
  assert.match(out, /任务尚未结束：以下为当前记录/);
  assert.ok(out.indexOf('stack plan') < out.indexOf('stack outcome'), 'the plan leads while flying');
  assert.match(out, /class="rc completed zero"/, 'an empty column is not coloured');
  ui.run(`current.mission.status = 'completed'; current.report.summary = {completed: 1, not_completed: 0, uncertain: 0}; renderMission();`);
  out = ui.html('detail');
  assert.doesNotMatch(out, /任务尚未结束/);
  assert.ok(out.indexOf('stack outcome') < out.indexOf('stack plan'), 'the outcome leads once the mission ended');
});

test('the lifecycle only mirrors the service status', () => {
  const ui = setup();
  const done = ui.run(`stepper('completed')`), declined = ui.run(`stepper('declined')`), rejected = ui.run(`stepper('rejected')`);
  assert.equal((done.match(/class="done"/g) || []).length, 5);
  assert.match(done, /<li class="ok" aria-current="step">结果<\/li>/);
  assert.match(declined, /<li class="stop" aria-current="step">审批<\/li>/);
  assert.match(rejected, /<li class="stop" aria-current="step">准入<\/li>/);
  assert.match(ui.run(`stepper('incomplete')`), /<li class="stop warn" aria-current="step">结果<\/li>/, 'incomplete is never shown as done');
});

test('the mission list names each request and its channel and counts the filters', () => {
  const ui = setup();
  ui.run(`hello = {can_write: true, projects: []}; missions = [
    {mission_id: 'm-1', status: 'awaiting_approval', current_version: 1, updated_at: '2026-09-26T00:00:00+00:00', text: 'photo <b>red</b>', channel: 'console', robot_id: 'uav_01'},
    {mission_id: 'm-2', status: 'running', current_version: 2, updated_at: '2026-09-26T00:00:00+00:00', text: 'Workflow asset_check', channel: 'workflow'},
    {mission_id: 'm-3', status: 'completed', current_version: 1, updated_at: '2026-09-26T00:00:00+00:00', text: '', channel: 'scheduler'}];
    renderMissionList();`);
  const list = ui.html('missionList');
  assert.match(list, /photo &lt;b&gt;red&lt;\/b&gt;/);
  assert.match(list, /任务台/);
  assert.match(list, /工作流/);
  assert.match(list, />m-3</, 'a mission without text falls back to its id');
  assert.match(ui.html('missionFilters'), /待审批<b>1<\/b>.*进行中<b>1<\/b>.*已结束<b>1<\/b>/s);
  ui.run(`act('filter', {v: 'pending'});`);
  assert.doesNotMatch(ui.html('missionList'), /m-2/);
});

// P2 workflows (D057): waits and failures are named, reviews need the role, drafts are shown as inactive.
// P2 工作流（D057）：等待与失败有名字，复核需要角色，草案显示为未生效。
const workflowsView = {
  project_id: 'campus_s1', roles: ['operator', 'approver'], catalog: {catalog_id: 'p2_s1_v1', sha256: 'x'},
  templates: [{workflow_id: 'asset_check', version: 1, title: 'Asset check<script> / 资产检查<script>', sha256: 'x', nodes: [{node_id: 'inspect', activity: 'submit_mission'}],
    inputs: {asset: {kind: 'asset_id', choices: ['asset_red', 'asset_blue'], required: true}},
    triggers: [{trigger_id: 'manual', kind: 'manual'},
      {trigger_id: 'daily_0900', kind: 'schedule', schedule: {every: 'day', at: '09:00', timezone: 'Asia/Shanghai'}, state: {state: 'disabled'}},
      {trigger_id: 'asset_alarm', kind: 'event', source: 'event:campus-alarm', event_type: 'asset.alarm'}]}],
  runs: [{run_id: 'wr-1', workflow_id: 'asset_check', version: 1, state: 'waiting', trigger_source: 'manual:x',
    created_at: '2026-09-26T00:00:00+00:00', updated_at: '2026-09-26T00:00:01+00:00', waiting: ['approval']}],
  orders: [{order_id: 'wo-1', asset_id: 'asset_red', state: 'open', reinspection_run: null}],
};

test('the workflow workspace names templates, schedules, waits and open orders', () => {
  const ui = setup();
  ui.run(`hello = {can_write: true, projects: []}; workflows = ${JSON.stringify(workflowsView)}; renderWorkflows();`);
  assert.match(ui.html('wfStart'), /启动运行/);
  assert.match(ui.html('wfStart'), /资产检查&lt;script&gt;/, 'the Chinese half of the title is shown, escaped');
  const schedules = ui.html('wfSchedules');
  assert.match(schedules, /排班 每天 09:00 Asia\/Shanghai · 已停用/);
  assert.match(schedules, /启用排班/);
  assert.match(schedules, /事件触发：event:campus-alarm/);
  for (const id of ['wfStart', 'workflowTemplates']) assert.doesNotMatch(ui.html(id), /<script>/, 'template titles are escaped');
  assert.match(ui.html('workflowRuns'), /等待审批/);
  assert.match(ui.html('workflowOrders'), /记录维修反馈/);
  ui.run(`act('start', {id: 'asset_check'});`);
  assert.deepEqual(ui.sent.at(-1), {type: 'workflow_start', project_id: 'campus_s1', workflow_id: 'asset_check', request_id: 'ui-0123456789abcdef', inputs: {}});
  ui.run(`workflows.roles = ['viewer']; renderWorkflows();`);
  assert.match(ui.html('wfStart'), /data-act="start"[^>]*disabled/, 'a viewer cannot start a run');
});

const runView = {
  run: {run_id: 'wr-1', project_id: 'campus_s1', workflow_id: 'asset_check', version: 1, trigger_source: 'manual:tailnet:ops',
    started_by: 'tailnet:ops', state: 'waiting', cancel: null},
  template: {title: 'Asset check', order: []}, waiting: ['review'],
  nodes: [{node_id: 'inspect', activity: 'submit_mission', state: 'completed', reason: null, result: {mission_id: 'm-1'}, detail: null},
    {node_id: 'await_inspection', activity: 'await_mission', state: 'completed', reason: null,
      result: {evidence_id: 'image:0123456789abcdef0123', mission_id: 'm-1'}, detail: null},
    {node_id: 'analyze', activity: 'analyze_evidence', state: 'completed', reason: null,
      result: {suspected: true, source: 'scripted', confidence: 0.82}, detail: null},
    {node_id: 'review', activity: 'human_review', state: 'waiting', reason: 'review', result: null, detail: null},
    {node_id: 'work_order', activity: 'create_work_order', state: 'pending', reason: null, result: null, detail: null},
    {node_id: 'blue', activity: 'await_mission', state: 'failed', reason: 'mission.declined', result: null, detail: null},
    {node_id: 'skip', activity: 'analyze_evidence', state: 'skipped', reason: 'upstream_failed', result: null, detail: null}],
  missions: [{mission_id: 'm-1', node_id: 'inspect', status: 'completed', reservations: []}],
  analyses: [{asset_id: 'asset_red', analyzer: 'scripted_fixture_v1', source: 'scripted', verdict: 'suspected'}],
  orders: [], children: [], events: [],
};

test('a run shows every wait and failure reason, and only reviewers get review buttons', () => {
  const ui = setup();
  ui.run(`hello = {can_write: true, projects: []}; workflows = ${JSON.stringify({...workflowsView, roles: ['operator']})};
    run = ${JSON.stringify(runView)}; nav = {ws: 'workflows', id: 'wr-1', kind: null}; renderRun();`);
  let out = ui.html('runDetail');
  assert.equal(ui.nodes.get('runOverview').hidden, true);
  assert.match(out, /等待人工复核/);
  assert.match(out, /疑似异常 · <span class="src script"[^>]*><span aria-hidden="true">⌘<\/span>脚本回答/);
  assert.match(out, /mission\.declined/);
  assert.match(out, /上游失败/);
  assert.match(out, /脚本 \/ 确定性结果不是模型识别/);
  assert.match(out, /id="cancelRun"/);
  assert.match(out, /data-node="review" data-decision="confirmed" disabled/, 'an operator without the reviewer role cannot review');
  ui.run(`workflows.roles = ['reviewer']; renderRun();`);
  out = ui.html('runDetail');
  assert.doesNotMatch(out, /data-decision="confirmed" disabled/);
  assert.match(out, /id="cancelRun"[^>]*disabled/, 'a reviewer cannot cancel');
  ui.run(`run.run.cancel = {requested_by: 'tailnet:ops', requested_at: '2026-09-26T00:00:02+00:00', reason: 'stop'}; run.run.state = 'cancelling'; renderRun();`);
  out = ui.html('runDetail');
  assert.match(out, /取消收尾中/);
  assert.doesNotMatch(out, /id="cancelRun"/);
  assert.doesNotMatch(out, /data-act="review"/, 'no review after a cancel');
});

test('cancelling the reason dialog sends nothing, and required reasons stay required', async () => {
  const ui = setup({answer: null});
  ui.run(`hello = {can_write: true, projects: []}; workflows = ${JSON.stringify({...workflowsView, roles: ['reviewer', 'operator']})};
    current = ${JSON.stringify(missionView())}; resources = ${JSON.stringify(resourcesView)};`);
  const before = ui.sent.length;
  await ui.run(`act('review', {id: 'wr-1', node: 'review', decision: 'confirmed'})`);
  await ui.run(`act('decline', {id: 'm-000000000001', v: '1'})`);
  await ui.run(`act('cancelRun', {id: 'wr-1'})`);
  assert.equal(ui.sent.length, before, 'a cancelled dialog is not a decision');
  const empty = setup({answer: '   '});
  empty.run(`hello = {can_write: true, projects: []}; resources = ${JSON.stringify(resourcesView)}; workflows = ${JSON.stringify(workflowsView)};`);
  await empty.run(`act('release', {id: 'dock_s1'})`);
  assert.equal(empty.sent.length, 0, 'an empty reason is not sent where one is required');
  await empty.run(`act('review', {id: 'wr-1', node: 'review', decision: 'dismissed'})`);
  assert.deepEqual(empty.sent.at(-1), {type: 'workflow_review', project_id: 'campus_s1', run_id: 'wr-1', node_id: 'review', decision: 'dismissed',
    request_id: 'ui-0123456789abcdef', note: ''}, 'an optional note may be empty');
});

test('a draft is shown as inactive with its source', () => {
  const ui = setup();
  ui.run(`renderDraft(${JSON.stringify({status: 'planned', use: {source: 'live_model'}, errors: [], spec_sha256: 'abcdef0123456789ff',
    note: 'drafts never run', spec: {nodes: [{node_id: 'review_1', activity: 'human_review'}]}})});`);
  const out = ui.html('draftResult');
  assert.match(out, /已生成（未生效）/);
  assert.match(out, /模型实调/);
  assert.match(out, /人工复核/);
});

// P3 scheduling (D059): queue, waits and exclusions are named, the scheduler chooses, approvals stay per mission.
// P3 调度（D059）：队列、等待与排除都有名字，由调度器选机，审批仍逐任务进行。
const tasksView = {
  project_id: 'campus_s1', roles: ['operator', 'approver'],
  catalog: {catalog_id: 'p3_desk_v1', sha256: 'x', ranking: 'p3-rank-v1', policy: 'p3-sched-v1'},
  assets: {asset_red: {volume_id: 'campus_training', robots: ['uav_01']}, 'asset_<b>': {volume_id: 'campus_training', robots: ['uav_01']}},
  tasks: [{task_id: 'tk-1', project_id: 'campus_s1', asset_id: 'asset_red', volume_id: 'campus_training', candidates: [], priority: 0,
    state: 'queued', epoch: 0, robot_id: null, mission_id: null, reason: null, source: 'operator', created_at: '2026-09-26T00:00:00+00:00',
    updated_at: '2026-09-26T00:00:01+00:00', not_after: null, waiting: {uav_01: ['energy.charging', 'airspace.cell_held']}, decision: null},
    {task_id: 'tk-2', project_id: 'campus_s1', asset_id: 'asset_west', volume_id: 'campus_training', candidates: [], priority: 0,
    state: 'rejected', epoch: 0, robot_id: null, mission_id: null, reason: 'asset.unregistered', source: 'operator',
    created_at: '2026-09-26T00:00:00+00:00', updated_at: '2026-09-26T00:00:01+00:00', not_after: null, waiting: null, decision: null}],
  robots: [{robot_id: 'uav_01', site_id: 'site_s1', dock_id: 'dock_s1', assignments: [{task_id: 'tk-0', epoch: 1, mission_id: 'm-1'}],
    eligibility: {verdict: 'blocked', reasons: ['robot.airborne']}}],
  airspace: {frame: 'campus', cell_m: 4, holds: [{activity: 'mission:m-1:v1', mission_id: 'm-1', robot_id: 'uav_01', state: 'occupied',
    cells: ['air.campus.0.0', 'air.campus.0.1']}], envelopes: [{activity: 'mission:m-9:v1', cells: ['air.campus.-1.-1']}]},
};

test('the scheduling workspace names every wait and rejection and offers only registered assets', () => {
  const ui = setup();
  ui.run(`hello = {can_write: true, projects: []}; tasks = ${JSON.stringify(tasksView)}; renderTasks();`);
  const queue = ui.html('taskQueue');
  assert.match(queue, /排队中/);
  assert.match(queue, /uav_01：充电中；航迹单元被其他活动持有/);
  assert.match(queue, /已拒绝/);
  assert.match(queue, /该机站点未登记此资产/);
  const robots = ui.html('taskRobots');
  assert.match(robots, /不可派遣/);
  assert.match(robots, /飞行器在空中/);
  const airspace = ui.html('taskAirspace');
  assert.match(airspace, /2 个单元（4 m）/);
  assert.match(airspace, /失联包络/);
  assert.equal((airspace.match(/<rect x=/g) || []).length, 3, 'two held cells and one envelope cell are drawn');
  const options = ui.html('taskAsset');
  assert.match(options, /asset_red · uav_01/);
  assert.doesNotMatch(options, /<b>/, 'asset ids are escaped');
  assert.equal(ui.nodes.get('taskSubmit').disabled, false);
  ui.run(`tasks.roles = ['viewer']; renderTasks();`);
  assert.equal(ui.nodes.get('taskSubmit').disabled, true, 'a viewer cannot submit');
  assert.deepEqual(JSON.parse(JSON.stringify(ui.run(`[cellXY('air.campus_s1.-1.0'), cellXY('air.x.y'), cellXY('bad')]`))), [{x: -1, y: 0}, null, null]);
});

test('a task shows its replayable decisions and assignment epochs and never approves', () => {
  const ui = setup();
  const view = {
    task: {...tasksView.tasks[0], state: 'assigned', robot_id: 'uav_02', epoch: 2, mission_id: 'm-2', requested_by: 'tailnet:ops',
      excluded: [], cancel: null, outcome: null},
    assignments: [{epoch: 1, robot_id: 'uav_01', mission_id: 'm-1', state: 'withdrawn', reason: 'dock.maintenance', blocked_since: 'x',
      created_at: 'x', updated_at: 'x', mission_status: 'withdrawn'},
      {epoch: 2, robot_id: 'uav_02', mission_id: 'm-2', state: 'active', reason: null, blocked_since: null, created_at: 'x',
      updated_at: 'x', mission_status: 'awaiting_approval'}],
    decisions: [{decision_id: 'sd-1', created_at: '2026-09-26T00:00:03+00:00', epoch: 2, verdict: 'assign', robot_id: 'uav_02',
      order: ['uav_02'], snapshot_sha256: '0123456789abcdef', ranking_version: 'p3-rank-v1', policy_version: 'p3-sched-v1',
      candidates: [{robot_id: 'uav_01', verdict: 'blocked', reasons: ['dock.maintenance'], permanent: false, eta_s: 23.2, usage: 0,
        held: [], envelope: []}, {robot_id: 'uav_02', verdict: 'eligible', reasons: [], permanent: false, eta_s: 24, usage: 0, held: [],
        envelope: []}]}],
    events: [{created_at: '2026-09-26T00:00:02+00:00', kind: 'assignment.withdrawn', body: {robot_id: 'uav_01', epoch: 1, reason: 'dock.maintenance'}}],
  };
  ui.run(`hello = {can_write: true, projects: []}; tasks = ${JSON.stringify(tasksView)}; task = ${JSON.stringify(view)};
    nav = {ws: 'tasks', id: 'tk-1', kind: null}; renderTask();`);
  const out = ui.html('taskDetail');
  assert.match(out, /已撤回（未领取） · 维护锁定/);
  assert.match(out, /分配 → uav_02/);
  assert.match(out, /快照 0123456789ab…/);
  const candidateRow = [...out.matchAll(/<tr>(.*?)<\/tr>/gs)].map(m => m[1]).find(row => row.startsWith('<td>—</td><td>uav_01</td>'));
  assert.ok(candidateRow, 'the recorded candidate is still present');
  assert.deepEqual([...candidateRow.matchAll(/<td>(.*?)<\/td>/gs)].map(m => m[1].replace(/<[^>]+>/g, '')),
    ['—', 'uav_01', 'site_s1 / dock_s1', '23.2', '0', '不可派遣', '维护锁定']);
  assert.match(out, /待审批/);
  assert.match(out, /id="cancelTask"/);
  assert.doesNotMatch(out, /id="approve/, 'approval stays in the mission view');
});

test('only a scheduling project subscribes to the queue', () => {
  const ui = setup();
  // A browser selects the first option; the stand-in element needs it set. / 浏览器会选中首项；替身元素需显式设置。
  ui.nodes.get('project').value = 'campus_s1';
  ui.run(`onHello({protocol: 'hri.v0', identity: 'tailnet:ops', can_write: true, volumes: [], assets: [], planner: 'scripted',
    projects: [{project_id: 'campus_s1', roles: ['operator'], robots: ['uav_01'], legacy: false, scheduling: false}]});`);
  assert.ok(ui.sent.some(m => m.type === 'workflows' && m.project_id === 'campus_s1'));
  assert.ok(!ui.sent.some(m => m.type === 'tasks_watch'), 'no queue without a scheduling catalog');
  assert.equal(ui.nodes.get('navTasks').hidden, true);
  ui.run(`hello.projects[0].scheduling = true; selectProject();`);
  assert.deepEqual(ui.sent.filter(m => m.type === 'tasks_watch'), [{type: 'tasks_watch', project_id: 'campus_s1'}]);
});

test('the address names the watched object and every watch carries the project', () => {
  const ui = setup();
  ui.nodes.get('project').value = 'campus_s1';
  ui.run(`onHello(${JSON.stringify(HELLO)});`);
  const cases = [['#missions/m-000000000009', {type: 'watch', mission_id: 'm-000000000009'}],
    ['#tasks/tk-9', {type: 'task_watch', project_id: 'campus_s1', task_id: 'tk-9'}],
    ['#fleet/dock_s1', {type: 'resource', project_id: 'campus_s1', resource_id: 'dock_s1'}],
    ['#business/finding/fd-9', {type: 'finding_watch', project_id: 'campus_s1', finding_id: 'fd-9'}],
    ['#business/order/ord-9', {type: 'order_watch', project_id: 'campus_s1', order_id: 'ord-9'}],
    ['#workflows/wr-9', {type: 'workflow_watch', project_id: 'campus_s1', run_id: 'wr-9'}]];
  for (const [hash, frame] of cases) {
    ui.location.hash = hash;
    ui.run('route()');
    assert.deepEqual(ui.sent.at(-1), frame, hash);
  }
  assert.equal(ui.nodes.get('ws-workflows').hidden, false);
  assert.equal(ui.nodes.get('ws-overview').hidden, true);
  ui.location.hash = '#nowhere';
  ui.run('route()');
  assert.equal(ui.nodes.get('ws-overview').hidden, false, 'an unknown address shows the overview');
});

test('the overview lists only facts the service reported, each linked to its detail', () => {
  const ui = setup();
  ui.nodes.get('project').value = 'campus_s1';
  ui.run(`onHello(${JSON.stringify(HELLO)});
    missions = [{mission_id: 'm-1', project_id: 'campus_s1', status: 'awaiting_approval', current_version: 1, updated_at: '2026-09-26T00:00:00+00:00', text: 'photo'}];
    workflows = ${JSON.stringify({...workflowsView, runs: [{...workflowsView.runs[0], waiting: ['review']}]})};
    resources = ${JSON.stringify(resourcesView)};
    business = {project_id: 'campus_s1', roles: ['reviewer'], catalog: {catalog_id: 'p4'}, report: {columns: {closed: [], open: ['x'], unknown: []}},
      findings: [{finding_id: 'fd-1', asset_key: 'campus_s1/*/asset_red', family: 'appearance', state: 'candidate', jobs: 2, updated_at: 'x'}],
      orders: [{order_id: 'ord-1', asset_key: 'campus_s1/*/asset_red', state: 'reinspection_unknown', round: 2, updated_at: 'x'}],
      jobs: [], references: [], reuse: {analyzers: []}};
    renderOverview();`);
  const out = ui.html('ovAttention') + '<h2>资源提示</h2>' + ui.html('ovHints');
  for (const href of ['#missions/m-1', '#workflows/wr-1', '#business/finding/fd-1', '#business/order/ord-1', '#fleet/uav_01', '#fleet/dock_s1&lt;script&gt;']) {
    assert.ok(out.includes(`href="${href}"`), href);
  }
  assert.match(out, /待审批 · photo/);
  assert.match(out, /复检不确定·待再修/);
  assert.doesNotMatch(out, /<script>/);
  assert.match(out, /资源提示[\s\S]*不可派遣 · uav_01/, 'a blocked robot is a hint, listed after the actions');
  assert.doesNotMatch(ui.html('ovAttention'), /不可派遣 · uav_01/, 'resource hints have a separate region');
  // approval, review, finding, business order, simulated order and dock lock; the robot is a hint.
  // 审批、复核、发现、业务工单、模拟工单与机场锁；机器人只是提示。
  assert.equal(ui.nodes.get('countOverview').textContent, '6', 'hints are not counted as pending actions');
  assert.equal(ui.nodes.get('countMissions').textContent, '1');
  assert.equal(ui.nodes.get('countBusiness').textContent, '1');
  assert.match(ui.html('ovKpis'), /待审批任务/);
});

test('evidence images are fetched once and only when the service holds one', () => {
  const ui = setup();
  const view = missionView({mission: {mission_id: 'm-000000000004', status: 'completed', current_version: 1, replans: 0},
    evidence: [{evidence_id: 'capture:1', version: 1, step_id: 'inspect_asset_red', media: true, captured_at: '2026-09-26T00:00:00Z',
      verification: {final_verdict: 'verified', agrees: true}, provenance: {source: 'sim_render'}},
      {evidence_id: 'capture:2', version: 1, step_id: 'inspect_asset_blue', media: false, captured_at: '2026-09-26T00:00:00Z', verification: null}]});
  ui.run(`hello = {can_write: true, projects: []}; nav = {ws: 'missions', id: 'm-000000000004', kind: null}; current = ${JSON.stringify(view)}; renderMission(); renderMission();`);
  assert.deepEqual(ui.sent.filter(m => m.type === 'media'), [{type: 'media', mission_id: 'm-000000000004', evidence_id: 'capture:1'}]);
  ui.run(`receive({type: 'media', evidence_id: 'capture:1', png: 'javascript:alert(1)'});`);
  assert.doesNotMatch(ui.html('detail'), /javascript:/, 'only a PNG data URI is shown');
  ui.run(`receive({type: 'media', evidence_id: 'capture:1', png: 'data:image/png;base64,AA=='});`);
  assert.match(ui.html('detail'), /src="data:image\/png;base64,AA=="/);
  assert.match(ui.html('detail'), /待复核/, 'unverified evidence says so');
});

test('the script names no control or injection frames', () => {
  for (const word of ['inject', 'docks.report', 'simulator', '"arm"', 'takeoff']) assert.ok(!source.includes(word), word);
});

// Visual v2 preserves service facts and exact human decisions (D074). / 视觉 v2 保留服务事实与人的确切决定（D074）。
test('grouped hashes retain all 64 characters for copy and per-version approval', async () => {
  const ui = setup(), hash = '0123456789abcdef'.repeat(4);
  const block = ui.run(`hashBlock('${hash}')`);
  const groups = [...block.matchAll(/<span>([a-f\d]{8})<\/span>/g)].map(m => m[1]);
  assert.equal(groups.length, 8);
  assert.equal(groups.join(''), hash);
  assert.match(block, new RegExp(`data-hash="${hash}"`));
  await ui.run(`act('copyHash', {hash: '${hash}'})`);
  assert.deepEqual(ui.copied, [hash]);
  assert.equal(ui.sent.length, 0, 'copy never sends a service frame');
  ui.run(`act('approve', {id: 'm-1', v: '2', hash: '${hash}'})`);
  assert.deepEqual(ui.sent.at(-1), {type: 'approve', mission_id: 'm-1', version: 2, package_hash: hash});
  assert.doesNotMatch(ui.run(`hashBlock('<img src=x>')`), /<img/, 'nonstandard values are escaped, never truncated');
  assert.match(ui.run(`hashBlock(null)`), /disabled/);
});

test('unknown report values stay neutral and no task safety conclusion is inferred', () => {
  const ui = setup(), view = missionView({report: {
    targets: {asset_red: 'uncertain'}, summary: {completed: 0, uncertain: 1, not_completed: 0}, facts: [],
    rows: [{mission_version: 1, step_id: 'step1', column: 'uncertain', execution_status: 'unknown', effect_verdict: 'unknown'}],
  }});
  view.versions[0].events = [{journal: 'guardian', kind: 'step_outcome', data: {outcome: {safety_verdict: 'proceed'}}}];
  ui.run(`current = ${JSON.stringify(view)};`);
  const out = ui.run('reportCard(current)');
  assert.match(out, /执行 · 逐步骤/);
  assert.match(out, /效果 · 逐步骤/);
  assert.match(out, /安全 · 任务级<\/h4><span class="badge unknown">服务未提供<\/span>/);
  assert.equal((out.match(/class="badge unknown"/g) || []).length, 3);
  assert.doesNotMatch(out, /proceed|guardian 干预|已证实步骤/);
  assert.match(ui.run(`badge(VERDICT.unknown, VTONE.unknown)`), /class="badge unknown"/);
  assert.match(ui.run(`wchip('outcome_unknown')`), /class="badge unknown"/);
});

test('environment labels show single, mixed and real backends without choosing one', () => {
  const ui = setup();
  assert.match(ui.run(`environmentTag([{robot_id:'r1',execution_backend:'px4_sitl'}])`), /仿真 · PX4 SITL/);
  const mixed = ui.run(`environmentTag([{robot_id:'r1',execution_backend:'px4_sitl'},{robot_id:'r2',execution_backend:'logical_sim'},{robot_id:'r3',execution_backend:'vendor_protocol_sim'}])`);
  assert.match(mixed, /仿真 · 混合后端/);
  for (const name of ['r1', 'r2', 'r3', 'PX4 SITL', '逻辑模拟', '厂商协议模拟']) assert.ok(mixed.includes(name));
  assert.doesNotMatch(mixed, /<select|data-act=/, 'the backend list is read-only');
  assert.match(ui.run(`environmentTag([{robot_id:'r1',execution_backend:'real_device'},{robot_id:'r2',execution_backend:'px4_sitl'}])`), /class="env real"[^>]*>.*真机 · 实飞/);
  assert.equal(ui.sent.length, 0);
});

test('missing or stale project provenance never presents a known simulated environment', () => {
  const ui = setup();
  for (const robots of [[], [{robot_id:'r1'}], [{robot_id:'r1',execution_backend:'future_backend'}]]) {
    assert.match(ui.run(`environmentTag(${JSON.stringify(robots)})`), /class="env missing"/);
  }
  assert.match(ui.run(`environmentTag([{robot_id:'r1',execution_backend:'px4_sitl'}], ['r1','r2'])`), /class="env missing"/);
  assert.match(ui.run(`environmentTag([{robot_id:'r1',execution_backend:'real_device'}], ['r1','r2'])`), /真机 · 实飞/);
  ui.run(`hello = ${JSON.stringify(HELLO)}; projectId = 'campus_s1'; resources = ${JSON.stringify(resourcesView)}; renderShell();`);
  assert.match(ui.html('environment'), /仿真 · PX4 SITL/);
  ui.run(`projectId = 'another_project'; renderShell();`);
  assert.match(ui.html('environment'), /class="env missing"/);
  assert.doesNotMatch(ui.html('environment'), /uav_01|仿真 · PX4/);
});

test('source chips distinguish models, scripts, unknown sources and execution backends', () => {
  const ui = setup();
  for (const [value, style] of [['px4_sitl','sim'], ['real_device','device'], ['live_model','model'], ['scripted','script'], ['legacy_unknown','missing']]) {
    assert.match(ui.run(`sourceChip('${value}')`), new RegExp(`class="src ${style}"`));
  }
  assert.doesNotMatch(ui.run(`sourceChip('sim_render')`), /class="src sim"/, 'media sources are not execution backends');
  assert.doesNotMatch(ui.run(`sourceChip('<img src=x>')`), /<img/);
});

test('event vocabulary preserves original codes and escapes unknown codes', () => {
  const ui = setup();
  assert.match(ui.run(`eventName('safety_intervention')`), /安全监督介入<code class="code">safety_intervention<\/code>/);
  assert.equal(ui.run(`eventName('new.event')`), '<code>new.event</code>');
  assert.equal(ui.run(`eventName('constructor')`), '<code>constructor</code>');
  assert.doesNotMatch(ui.run(`eventName('<img src=x>')`), /<img/);
});

test('audit filter counts are bounded to the loaded page and denied rows keep their meaning', () => {
  const ui = setup();
  ui.run(`audit = {entries:[{at:'2026-10-08T00:00:00Z',action:'access.denied',actor:'viewer',object:null,detail:{}},
    {at:'2026-10-08T00:01:00Z',action:'mission.approved',actor:'approver',object:{kind:'mission',id:'m-1'},detail:{}}],next:'cursor'}; renderAudit();`);
  assert.match(ui.nodes.get('auditSub').textContent, /已加载页 2 条（筛选仅统计本页）/);
  assert.match(ui.html('auditFilters'), /全部<b>2<\/b>/);
  assert.match(ui.html('auditList'), /<tr class="denied">.*越权尝试（已拒绝）/s);
  ui.run(`act('auditFilter', {v:'denied'})`);
  assert.doesNotMatch(ui.html('auditList'), /审批签名/);
  assert.equal(ui.sent.length, 0, 'filtering does not request the whole audit history');
});

test('an acknowledged dock action is never drawn as completed', () => {
  const ui = setup();
  const out = ui.run(`dockActionTrack({state:'acked', requested_at:'t0', acked_at:'t1', ack_accepted:true})`);
  assert.match(out, /class="now">✓ 已受理/);
  assert.match(out, /class="pending">○ 已完成/);
  assert.doesNotMatch(out, /✓ 已完成/);
  assert.match(ui.run(`dockActionTrack({state:'rejected',requested_at:'t0',acked_at:'t1',ack_accepted:false})`), /被拒/);
  assert.match(ui.run(`dockActionTrack({state:'completed',requested_at:'t0',acked_at:'t1',ack_accepted:1})`), /✓ 已受理/);
});
