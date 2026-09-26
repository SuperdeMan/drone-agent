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
