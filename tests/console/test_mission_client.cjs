/* The desk page's P1 rendering without a browser: resources, dispatch detail, pre-claim cancel and escaping.
   不依赖浏览器验证任务台页面的 P1 渲染：资源、派遣详情、领取前取消与转义。
   Run: node --test tests/console/test_mission_client.cjs */
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const root = path.resolve(__dirname, '../..');
const html = fs.readFileSync(path.join(root, 'src/drone_agent/console/mission.html'), 'utf8');
const source = fs.readFileSync(path.join(root, 'src/drone_agent/console/mission.js'), 'utf8');

function setup() {
  const nodes = new Map();
  class Element {
    constructor() { this.textContent = ''; this.hidden = false; this.value = ''; this.disabled = false; this.html = ''; this.dataset = {}; }
    set innerHTML(value) { this.html = value; for (const m of value.matchAll(/id="([^"]+)"/g)) nodes.set(m[1], new Element()); }
    get innerHTML() { return this.html; }
    querySelectorAll() { return []; }
    setAttribute() {}
  }
  for (const m of html.matchAll(/id="([^"]+)"/g)) nodes.set(m[1], new Element());
  const sent = [];
  class Socket { constructor() { this.readyState = 1; } send(text) { sent.push(JSON.parse(text)); } }
  const context = vm.createContext({
    document: {getElementById: id => nodes.get(id) || (nodes.set(id, new Element()), nodes.get(id))},
    WebSocket: Socket, location: {protocol: 'https:', host: 'desk.test'}, crypto: {randomUUID: () => '0123456789abcdef0123456789abcdef'},
    setTimeout: () => 0, clearTimeout: () => {}, prompt: () => 'reason', console, JSON,
  });
  vm.runInContext(source, context);
  return {nodes, sent, run: code => vm.runInContext(code, context)};
}

const resourcesView = {
  project_id: 'campus_s1', roles: ['admin', 'approver', 'operator'], evaluated_at: '2026-09-26T00:00:00+00:00',
  catalog: {catalog_id: 'p1_s1_v1', policy: {version: 'p1-dispatch-v1'}},
  sites: [{site_id: 'site_s1', docks: [{dock_id: 'dock_s1<script>', source: 'logical_sim', pad: 'uncertain', holder: 'mission:m-1:v1',
    status: {link: 'online', fresh: false, age_s: 4.2, session: 'active', lid: 'closed', aircraft: 'present',
      energy: {state: 'charging', charge_fraction: 0.62}, environment: {state: 'permitted', wind_mps: 2}, upkeep: 'normal',
      lock: {state: 'maintenance'}}}],
    robots: [{robot_id: 'uav_01', execution_backend: 'px4_sitl', capability_source: 'published', status: null,
      eligibility: {verdict: 'blocked', reasons: ['dock.status_stale', 'energy.charging', 'dock.maintenance']}}]}],
};

test('online is never shown as dispatchable and every reason is named', () => {
  const ui = setup();
  ui.run(`resources = ${JSON.stringify(resourcesView)}; renderResources();`);
  const out = ui.nodes.get('resourceList').innerHTML;
  assert.match(out, /在线/);
  assert.match(out, /不可派遣/);
  assert.match(out, /机场状态过期（&gt;3 s）；充电中；维护锁定/);
  assert.match(out, /占用待对账/);
  assert.match(out, /过期 · 4\.2 s 前/);
  assert.match(out, /解除维护锁（管理员）/);
  assert.doesNotMatch(out, /<script>/, 'values from the service are escaped');
});

test('the mission detail shows the binding, dispatch decisions and a pre-claim cancel', () => {
  const ui = setup();
  ui.run(`hello = {can_write: true, projects: []};`);
  const view = {
    mission: {mission_id: 'm-000000000001', status: 'queued', current_version: 1, replans: 0},
    request: {text: 'inspect', channel: 'console', requested_by: 'tailnet:ops'},
    versions: [{version: 1, status: 'queued', origin: 'console', package_hash: 'ab', package: null, provenance: {},
      journals: {}, events: [], spec: null, planner: null, admission: null, approval: null, diff: null}],
    live: null, evidence: [], operations: [], report: null, issues: [], facts: [], cloud: null,
    binding: {project_id: 'campus_s1', site_id: 'site_s1', dock_id: 'dock_s1', robot_id: 'uav_01', execution_backend: 'px4_sitl'},
    dispatch: {decisions: [{version: 1, stage: 'claim', verdict: 'blocked', reasons: ['dock.lid_not_open'], created_at: 'x'}],
      reservations: [{mission_version: 1, state: 'reserved', reason: 'reserved'}], claims: [], cancel: null, dock_actions: [], events: []},
  };
  ui.run(`current = ${JSON.stringify(view)}; render();`);
  const out = ui.nodes.get('detail').innerHTML;
  assert.match(out, /派遣（P1）/);
  assert.match(out, /campus_s1 \/ site_s1/);
  assert.match(out, /领取.*不可派遣.*舱盖未打开/s);
  assert.match(out, /id="cancelMission"/);
  ui.run(`current.dispatch.cancel = {requested_by: 'tailnet:ops', requested_at: '2026-09-26T00:00:01+00:00', relayed_request_id: null}; render();`);
  assert.doesNotMatch(ui.nodes.get('detail').innerHTML, /id="cancelMission"/, 'a persisted cancel is not offered twice');
});

test('legacy missions are read-only history without a dispatch section', () => {
  const ui = setup();
  ui.run(`hello = {can_write: true, projects: []};`);
  const view = {
    mission: {mission_id: 'm-000000000002', status: 'completed', current_version: 1, replans: 0},
    request: {text: 'old', channel: 'console', requested_by: 'tailnet:ops'},
    versions: [{version: 1, status: 'finished', origin: 'console', package_hash: 'ab', package: null, provenance: {},
      journals: {}, events: [], spec: null, planner: null, admission: null, approval: null, diff: null}],
    live: null, evidence: [], operations: [], report: null, issues: [], facts: [], cloud: null,
    binding: {project_id: 'legacy_m2', legacy: true}, dispatch: null,
  };
  ui.run(`current = ${JSON.stringify(view)}; render();`);
  const out = ui.nodes.get('detail').innerHTML;
  assert.match(out, /历史任务（只读）/);
  assert.doesNotMatch(out, /id="cancelMission"/);
});

// P2 workflows (D057): waits and failures are named, reviews need the role, drafts are shown as inactive.
// P2 工作流（D057）：等待与失败有名字，复核需要角色，草案显示为未生效。
const workflowsView = {
  project_id: 'campus_s1', roles: ['operator', 'approver'], catalog: {catalog_id: 'p2_s1_v1', sha256: 'x'},
  templates: [{workflow_id: 'asset_check', version: 1, title: 'Asset check<script>', sha256: 'x', nodes: [],
    inputs: {asset: {kind: 'asset_id', choices: ['asset_red', 'asset_blue'], required: true}},
    triggers: [{trigger_id: 'manual', kind: 'manual'},
      {trigger_id: 'daily_0900', kind: 'schedule', schedule: {every: 'day', at: '09:00', timezone: 'Asia/Shanghai'}, state: {state: 'disabled'}},
      {trigger_id: 'asset_alarm', kind: 'event', source: 'event:campus-alarm', event_type: 'asset.alarm'}]}],
  runs: [{run_id: 'wr-1', workflow_id: 'asset_check', version: 1, state: 'waiting', trigger_source: 'manual:x',
    created_at: '2026-09-26T00:00:00+00:00', updated_at: '2026-09-26T00:00:01+00:00', waiting: ['approval']}],
  orders: [{order_id: 'wo-1', asset_id: 'asset_red', state: 'open', reinspection_run: null}],
};

test('the workflow panel names templates, schedules, waits and open orders', () => {
  const ui = setup();
  ui.run(`hello = {can_write: true, projects: []}; workflows = ${JSON.stringify(workflowsView)}; renderWorkflows();`);
  const templates = ui.nodes.get('workflowTemplates').innerHTML;
  assert.match(templates, /启动运行/);
  assert.match(templates, /每天 09:00 Asia\/Shanghai · 已停用/);
  assert.match(templates, /启用排班/);
  assert.match(templates, /事件触发：event:campus-alarm/);
  assert.doesNotMatch(templates, /<script>/, 'template titles are escaped');
  assert.match(ui.nodes.get('workflowRuns').innerHTML, /等待审批/);
  assert.match(ui.nodes.get('workflowOrders').innerHTML, /记录维修反馈/);
});

test('a run shows every wait and failure reason, and only reviewers get review buttons', () => {
  const ui = setup();
  const run = {
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
  ui.run(`hello = {can_write: true, projects: []}; workflows = ${JSON.stringify({...workflowsView, roles: ['operator']})}; run = ${JSON.stringify(run)}; mode = 'workflow'; renderRun();`);
  let out = ui.nodes.get('detail').innerHTML;
  assert.match(out, /等待人工复核/);
  assert.match(out, /疑似异常 · 脚本回答/);
  assert.match(out, /mission\.declined/);
  assert.match(out, /上游失败/);
  assert.match(out, /脚本 \/ 确定性结果不是模型识别/);
  assert.match(out, /id="cancelRun"/);
  assert.match(out, /data-review="review" data-decision="confirmed" disabled/, 'an operator without the reviewer role cannot review');
  ui.run(`workflows.roles = ['reviewer']; renderRun();`);
  out = ui.nodes.get('detail').innerHTML;
  assert.doesNotMatch(out, /data-decision="confirmed" disabled/);
  assert.match(out, /id="cancelRun" disabled/, 'a reviewer cannot cancel');
  ui.run(`run.run.cancel = {requested_by: 'tailnet:ops', requested_at: '2026-09-26T00:00:02+00:00', reason: 'stop'}; run.run.state = 'cancelling'; renderRun();`);
  out = ui.nodes.get('detail').innerHTML;
  assert.match(out, /取消收尾中/);
  assert.doesNotMatch(out, /id="cancelRun"/);
  assert.doesNotMatch(out, /data-review=/, 'no review after a cancel');
});

test('a draft is shown as inactive with its source', () => {
  const ui = setup();
  ui.run(`renderDraft(${JSON.stringify({status: 'planned', use: {source: 'live_model'}, errors: [], spec_sha256: 'abcdef0123456789ff',
    note: 'drafts never run', spec: {nodes: [{node_id: 'review_1', activity: 'human_review'}]}})});`);
  const out = ui.nodes.get('draftResult').innerHTML;
  assert.match(out, /已生成（未生效）/);
  assert.match(out, /模型实调/);
  assert.match(out, /人工复核/);
});
