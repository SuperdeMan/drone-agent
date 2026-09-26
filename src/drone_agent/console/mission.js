"use strict";
// hri.v0 mission console client. Every value that came from a model or a robot is escaped before display.
// hri.v0 任务控制台客户端。来自模型或机器人的每个值在显示前都经过转义。
const byId = id => document.getElementById(id);
const esc = v => String(v ?? "").replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
const STATUS = {planning: "规划中", verifying: "证据同步中", awaiting_approval: "待审批", approving: "策略审批中", approved: "已批准·待投递", queued: "已排队投递",
  delivered: "机载已接收", delivery_rejected: "机载拒收", running: "执行中", finished: "本版本结束", completed: "已完成",
  incomplete: "未完成", refused: "模型拒答", rejected: "准入拒绝", declined: "已驳回", planning_failed: "规划失败",
  cancelled: "已取消（未交付）", dispatch_expired: "审批过期未交付", withdrawn: "已作废（未交付）"};
// P1 dispatch reasons (D055); a verdict is a time-limited judgment, never an authorization. / P1 派遣原因；判定有时效，不是授权。
const REASON = {"dock.status_missing": "机场无状态", "dock.status_stale": "机场状态过期（>3 s）", "dock.session_reconciling": "机场新会话对账中",
  "dock.link_unknown": "机场链路未知", "dock.lid_unknown": "舱盖状态未知", "dock.presence_unknown": "机位在位未知", "energy.unknown": "电量未知",
  "environment.unknown": "环境未知", "robot.capability_unknown": "能力未知", "robot.phase_unknown": "飞行阶段未知", "dock.offline": "机场离线",
  "dock.maintenance": "维护锁定", "dock.fault": "故障锁定", "dock.lid_jammed": "舱盖卡滞", "dock.lid_not_open": "舱盖未打开",
  "dock.lid_open_unconfirmed": "开盖未经回执证实", "dock.aircraft_absent": "飞行器不在机位", "presence.conflict": "机场在位与飞行器空中冲突",
  "energy.idle": "未补能", "energy.charging": "充电中", "energy.cooling": "冷却中", "energy.fault": "补能故障", "energy.insufficient": "电量低于任务需求",
  "environment.deferred": "环境推迟", "environment.wind": "风速超限", "robot.airborne": "飞行器在空中", "robot.failsafe": "飞控失效保护中",
  "robot.unbound": "未绑定", "capability.missing_skill": "缺少所需技能", "reservation.conflict": "资源被其他任务占用",
  "reservation.missing": "本任务未持有资源", "window.expired": "任务窗口已过"};
const VERDICT = {eligible: "可派遣", blocked: "不可派遣", unknown: "未知·不派遣"};
const VTONE = {eligible: "ok", blocked: "bad", unknown: "warn"};
const DIM = {online: "在线", offline: "离线", closed: "舱盖关", opening: "开盖中", open: "舱盖开", closing: "关盖中", jammed: "舱盖卡滞",
  present: "在位", absent: "不在位", idle: "未补能", charging: "充电中", cooling: "冷却中", ready: "补能就绪", fault: "故障",
  permitted: "环境许可", deferred: "环境推迟", normal: "运维正常", maintenance: "维护", unknown: "未知"};
const PAD = {free: "机位空闲", reserved: "软预约", occupied: "占用", uncertain: "占用待对账"};
const TONE = {completed: "ok", delivered: "ok", running: "ok", awaiting_approval: "warn", incomplete: "warn", approved: "warn",
  queued: "warn", refused: "bad", rejected: "bad", declined: "bad", delivery_rejected: "bad", planning_failed: "bad"};
const COLUMN = {completed: "已完成", not_completed: "未完成", uncertain: "不确定"};
// Sources are read-only service records, never a backend selector. / 来源只读自服务记录，不是后端选择器。
const SOURCE = {none: "未连接执行后端", logical_sim: "逻辑模拟", px4_sitl: "PX4 SITL", vendor_protocol_sim: "厂商协议模拟",
  real_device: "真实设备", sim_render: "仿真渲染", recorded_real: "真实素材回放", live_sensor: "实时传感器",
  test_fixture: "测试素材", live_model: "模型实调", recorded_model: "录制 / 缓存回放", scripted: "脚本回答",
  deterministic: "确定性代码", not_run: "未运行", unknown: "未知", legacy_unknown: "历史来源未记录"};
// Simulation supervisor records (D035): shown as recorded, never used to decide anything here.
// 仿真监管者记录（D035）：按记录原样展示，这里不据此做任何决定。
const HOST = {idle: "空闲，等待已审批任务", waiting_for_workspace: "等待云端工作区（其他仿真或部署占用）",
  waiting_for_memory: "等待共享主机内存", preparing: "启动仿真并预热（地勤换电）", flying: "飞行中",
  landing: "仿真操作员降落收尾", restoring: "收尾并恢复空闲仿真", judging: "独立裁判中", error: "监管者异常"};
const FLIGHT = {preparing: "准备中", flying: "飞行中", finished: "落地结束", skipped: "未起飞", overrun: "超时，由仿真操作员降落",
  failed: "失败", interrupted: "中断"};
const JUDGE = {completed: "完成", not_completed: "未完成", unsafe_or_incorrect: "不安全或不正确"};
let socket = null, hello = null, current = null, selected = null, retry = 500, photos = {}, host = null, planning = null;
let resources = null, resourceDetail = null;
// P2 workflows (D057): runs, nodes and reasons as the service reports them. / P2 工作流：按服务记录展示运行、节点与原因。
let workflows = null, run = null, mode = "mission";
const WSTATE = {pending: "待开始", running: "进行中", waiting: "等待中", completed: "已完成", skipped: "已跳过", failed: "失败",
  outcome_unknown: "结果未知", cancelled: "已取消", cancel_requested: "已请求取消", cancelling: "取消收尾中"};
const WTONE = {completed: "ok", running: "ok", waiting: "warn", cancel_requested: "warn", cancelling: "warn", failed: "bad",
  outcome_unknown: "bad", cancelled: "bad"};
const WAIT = {approval: "等待审批", device: "等待设备", environment: "等待环境", flight: "飞行中", evidence: "等待证据复核",
  review: "等待人工复核", repair: "等待维修反馈", delivery: "投递中"};
const SKIP = {condition_false: "条件不满足", upstream_skipped: "上游已跳过", upstream_failed: "上游失败", upstream_unknown: "上游结果未知",
  cancelled: "随运行取消"};
const ACTIVITY = {submit_mission: "提交任务", await_mission: "等待任务结果", analyze_evidence: "分析证据", human_review: "人工复核",
  create_work_order: "模拟工单", await_repair: "等待维修反馈", request_reinspection: "请求复检", build_report: "生成报告"};
const ORDER = {open: "待维修", repair_reported: "已反馈维修·待复检", reinspection_requested: "已请求复检"};
const FINAL = ["completed", "failed", "outcome_unknown", "cancelled"];
// P3 scheduling (D059): the scheduler's queue, decisions and holds as the service records them.
// P3 调度（D059）：按服务记录展示调度器的队列、判定与持有。
let tasks = null, task = null;
Object.assign(REASON, {"asset.unregistered": "该机站点未登记此资产", "volume.unapproved": "体积不是已批准的仿真体积",
  "task.robot_excluded": "本任务已排除该机（此前失败）", "airspace.cell_held": "航迹单元被其他活动持有",
  "airspace.envelope": "落入失联飞行器的包络", "airspace.hold_missing": "本活动未持有航迹单元"});
const TSTATE = {queued: "排队中", assigned: "已分配", completed: "已完成", failed: "失败", outcome_unknown: "结果未知",
  rejected: "已拒绝", cancel_requested: "已请求取消", cancelling: "取消收尾中", cancelled: "已取消"};
const TTONE = {completed: "ok", assigned: "ok", queued: "warn", cancel_requested: "warn", cancelling: "warn", failed: "bad",
  outcome_unknown: "bad", rejected: "bad", cancelled: "bad"};
const ASTATE = {active: "有效", withdrawn: "已撤回（未领取）", ended: "已结束"};
const TVERDICT = {assign: "分配", wait: "等待", reject: "拒绝"};

function notice(text) { byId("notice").textContent = text || ""; byId("notice").hidden = !text; }
function chip(status) { return `<span class="chip ${TONE[status] || ""}">${esc(STATUS[status] || status)}</span>`; }
function send(message) {
  if (socket && socket.readyState === 1) socket.send(JSON.stringify(message));
  else notice("连接中断，请等待状态重新同步后操作。");
}
function rid() { return crypto.randomUUID().replace(/-/g, "").slice(0, 16); }

function connect() {
  socket = new WebSocket((location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws/session");
  socket.onopen = () => { retry = 500; if (current) send({type: "watch", mission_id: current.mission.mission_id}); };
  socket.onclose = () => {
    hello = null; byId("submit").disabled = true;
    if (current) render();
    byId("who").className = "who"; byId("whoText").textContent = "连接中断，历史状态仅供查看";
    setTimeout(connect, retry = Math.min(retry * 2, 8000));
  };
  socket.onmessage = event => {
    const message = JSON.parse(event.data);
    if (message.type === "hello") onHello(message);
    else if (message.type === "missions") renderList(message.items);
    else if (message.type === "mission") { if (planning) planned(); current = message.view; if (!resourceDetail && mode === "mission") render(); }
    else if (message.type === "workflows") { workflows = message.view; renderWorkflows(); }
    else if (message.type === "workflow") { run = message.view; if (mode === "workflow") renderRun(); }
    else if (message.type === "workflow_draft") renderDraft(message.result);
    else if (message.type === "tasks") { tasks = message.view; renderTasks(); }
    else if (message.type === "task") { task = message.view; if (mode === "task") renderTask(); }
    else if (message.type === "media") { photos[message.evidence_id] = message.png; renderEvidence(); }
    else if (message.type === "host") { host = message.status; renderHost(); }
    else if (message.type === "resources") { resources = message.view; renderResources(); }
    else if (message.type === "resource") { resourceDetail = message.detail; renderResourceDetail(); }
    else if (message.type === "error") { if (planning) planned(); notice((message.issue?.code ? message.issue.code + " · " : "") + message.message); }
  };
}

function onHello(message) {
  hello = message;
  byId("who").className = "who good";
  byId("whoText").textContent = message.identity ? message.identity : "只读会话（无 tailnet 身份）";
  const planner = message.planner || "";
  byId("whoMode").textContent = message.protocol + " · 规划 " + (planner === "scripted" ? "脚本回答（非模型，只认场景集原文）"
    : planner.startsWith("live:") ? planner.slice(5) : planner.startsWith("unavailable") ? "不可用" : planner || "未知");
  byId("volume").innerHTML = message.volumes.map(v => `<option value="${esc(v.volume_id)}">${esc(v.volume_id)} · ${esc(v.airspace_mode)}</option>`).join("");
  byId("assets").innerHTML = message.assets.map(a => `<label><input type="checkbox" value="${esc(a.asset_id)}">${esc(a.asset_id)}</label>`).join("") +
    '<p>不勾选表示体积内任意登记资产。</p>';
  byId("submit").disabled = !message.can_write;
  if (!message.can_write) notice("当前会话没有 tailnet 身份，只能查看；提交、审批与操作需要已识别的操作者。");
  const projects = (message.projects || []).filter(p => !p.legacy);
  byId("resources").hidden = byId("resourcesEyebrow").hidden = byId("robotRow").hidden = !projects.length;
  byId("project").innerHTML = projects.map(p => `<option value="${esc(p.project_id)}">${esc(p.project_id)} · ${esc(p.roles.join("/"))}</option>`).join("");
  if (projects.length) selectProject();
}

function selectProject() {
  const project = (hello?.projects || []).find(p => p.project_id === byId("project").value);
  byId("robot").innerHTML = (project?.robots || []).map(r => `<option value="${esc(r)}">${esc(r)}</option>`).join("");
  resources = null; renderResources();
  send({type: "resources", project_id: byId("project").value});
  workflows = null;
  send({type: "workflows", project_id: byId("project").value});
  tasks = null; renderTasks();
  if (project?.scheduling) send({type: "tasks_watch", project_id: byId("project").value});
}
byId("project").onchange = selectProject;

function chips(values) { return `<div class="chips">${values.map(([v, tone]) => `<span class="chip ${tone || ""}">${esc(v)}</span>`).join("")}</div>`; }
function reasons(list) { return (list || []).map(r => REASON[r] || r).join("；"); }

function dockChips(d) {
  const s = d.status;
  if (!s) return '<div class="why">尚无机场报告：不可派遣。</div>';
  const charge = s.energy.charge_fraction == null ? "" : " " + Math.round(s.energy.charge_fraction * 100) + "%";
  return chips([[DIM[s.link] || s.link, s.link === "online" && s.fresh ? "ok" : "bad"], [DIM[s.lid] || s.lid], [DIM[s.aircraft] || s.aircraft],
    [(DIM[s.energy.state] || s.energy.state) + charge], [DIM[s.environment.state] || s.environment.state],
    [s.lock ? "锁定：" + (DIM[s.lock.state] || s.lock.state) : DIM[s.upkeep] || s.upkeep, s.lock ? "bad" : ""],
    [PAD[d.pad] || d.pad, d.pad === "uncertain" ? "bad" : d.pad === "free" ? "" : "warn"],
    [(s.fresh ? "" : "过期 · ") + s.age_s.toFixed(1) + " s 前 · " + (s.session === "active" ? "会话有效" : "会话对账中"), s.fresh ? "" : "bad"]]);
}

function renderResources() {
  if (!resources) { byId("resourceList").innerHTML = '<div class="empty">加载资源中。</div>'; return; }
  const admin = (resources.roles || []).includes("admin");
  byId("resourceList").innerHTML = resources.sites.map(site => `<div class="res"><b>${esc(site.site_id)}</b>
    ${site.docks.map(d => `<div class="res"><button data-res="${esc(d.dock_id)}">${esc(d.dock_id)}</button> · ${esc(d.source === "logical_sim" ? "逻辑模拟机场" : d.source)}
      ${dockChips(d)}${d.status?.lock && admin ? `<button data-release="${esc(d.dock_id)}">解除维护锁（管理员）</button>` : ""}</div>`).join("")}
    ${site.robots.map(r => `<div class="res"><button data-res="${esc(r.robot_id)}">${esc(r.robot_id)}</button> · ${esc(r.execution_backend === "px4_sitl" ? "PX4 SITL" : "逻辑飞行")}
      ${chips([[VERDICT[r.eligibility.verdict] || r.eligibility.verdict, VTONE[r.eligibility.verdict]],
        [r.status ? (r.status.flight_phase === "grounded" ? "机体在地面" : r.status.flight_phase) + " · " + Math.round(r.status.age_s) + " s 前" : "机体无状态"]])}
      ${r.eligibility.reasons.length ? `<div class="why">${esc(reasons(r.eligibility.reasons))}</div>` : ""}</div>`).join("")}</div>`).join("")
    + `<p>目录 ${esc(resources.catalog.catalog_id)} · 策略 ${esc(resources.catalog.policy.version)} · 判定于 ${esc(resources.evaluated_at.slice(11, 19))}</p>`;
  for (const button of byId("resourceList").querySelectorAll("[data-res]")) button.onclick = () => send({type: "resource", project_id: resources.project_id, resource_id: button.dataset.res});
  for (const button of byId("resourceList").querySelectorAll("[data-release]")) button.onclick = () => {
    const reason = prompt("解除维护锁的原因（将写入审计）") ?? "";
    if (reason) send({type: "maintenance", project_id: resources.project_id, dock_id: button.dataset.release, action: "release", reason});
  };
}

function renderResourceDetail() {
  const d = resourceDetail;
  if (!d) return;
  let html = `<div class="eyebrow">资源 · ${esc(d.kind === "dock" ? "机场" : "机器人")}</div><h2>${esc(d.dock_id || d.robot_id)}</h2>`;
  if (d.kind === "dock") {
    html += `<p>来源：${esc(d.source)}；服务 ${esc(d.serves.join(", "))}；机位 ${esc(PAD[d.pad] || d.pad)}${d.holder ? "（" + esc(d.holder) + "）" : ""}</p>${dockChips(d)}`;
    html += `<h3>动作（ACK 只表示受理，完成以后续状态报告为准）</h3><table><tr><th>动作</th><th>状态</th><th>活动</th><th>请求</th></tr>${d.actions.slice().reverse().map(a =>
      `<tr><td>${esc(a.kind)}</td><td>${esc(a.state)}</td><td><code>${esc(a.activity_key)}</code></td><td>${esc(a.requested_at.slice(11, 19))}</td></tr>`).join("")}</table>`;
    html += `<h3>预约</h3><table>${d.reservations.slice().reverse().map(r => `<tr><td><code>${esc(r.activity_key)}</code></td><td>${esc(r.state)}</td><td>${esc(r.reason)}</td></tr>`).join("")}</table>`;
    html += `<h3>审计事件</h3><div class="events">${d.events.slice().reverse().map(e => `<div class="event ${e.kind === "status.rejected" ? "alert" : ""}"><time>${esc(e.created_at.slice(11, 19))}</time><span>${esc(e.kind)} ${esc(JSON.stringify(e.body).slice(0, 160))}</span></div>`).join("")}</div>`;
  } else {
    html += `<p>${esc(VERDICT[d.eligibility.verdict])}${d.eligibility.reasons.length ? "：" + esc(reasons(d.eligibility.reasons)) : ""}</p><dl><div><dt>能力来源</dt><dd>${esc(d.capability_source)}</dd></div>
      <div><dt>执行后端</dt><dd>${esc(d.execution_backend)}</dd></div><div><dt>判定有效至</dt><dd>${esc(d.eligibility.valid_until)}</dd></div>
      <div><dt>快照摘要</dt><dd>${esc(d.eligibility.snapshot_sha256.slice(0, 16))}…</dd></div></dl>`;
  }
  html += `<p><button class="ghost" id="closeResource">返回任务</button></p>`;
  byId("detail").innerHTML = html;
  byId("closeResource").onclick = () => {
    resourceDetail = null;
    if (current) render(); else byId("detail").innerHTML = '<div class="empty">选择或提交一个任务后显示规格、任务包、审批与进度。</div>';
  };
}

function wchip(state) { return `<span class="chip ${WTONE[state] || ""}">${esc(WSTATE[state] || state)}</span>`; }
function why(node) {
  if (node.state === "waiting") return WAIT[node.reason] || node.reason || "";
  if (node.state === "skipped" || node.state === "cancelled") return SKIP[node.reason] || node.reason || "";
  return node.reason || "";
}
function can(role) { return hello?.can_write && (workflows?.roles || []).includes(role); }

function renderWorkflows() {
  const shown = Boolean(workflows);
  byId("workflows").hidden = byId("workflowsEyebrow").hidden = !shown;
  if (!shown) return;
  const operator = can("operator");
  byId("workflowTemplates").innerHTML = workflows.templates.map(t => {
    const inputs = Object.entries(t.inputs || {});
    const choose = inputs.map(([name, spec]) => `<select data-input="${esc(name)}" data-wf="${esc(t.workflow_id)}">${spec.choices.map(c =>
      `<option value="${esc(c)}">${esc(c)}</option>`).join("")}</select>`).join("");
    const manual = t.triggers.some(x => x.kind === "manual");
    const schedules = t.triggers.filter(x => x.kind === "schedule").map(x => {
      const on = x.state?.state === "enabled";
      const when = x.schedule.every === "day" ? `每天 ${x.schedule.at} ${x.schedule.timezone}` : `每 ${x.schedule.minutes} 分钟`;
      return `<div class="row"><span>排班 ${esc(when)} · ${on ? "已启用，下次 " + esc((x.state.cursor_at || "").slice(0, 16)) : "已停用"}</span>
        <button class="ghost" data-schedule="${esc(t.workflow_id)}" data-trigger="${esc(x.trigger_id)}" data-action="${on ? "disable" : "enable"}" ${operator ? "" : "disabled"}>${on ? "停用排班" : "启用排班"}</button></div>`;
    }).join("");
    const events = t.triggers.filter(x => x.kind === "event").map(x => `<p>事件触发：${esc(x.source)} · ${esc(x.event_type)}</p>`).join("");
    return `<div class="res"><b>${esc(t.workflow_id)} v${esc(t.version)}</b><div>${esc(t.title)}</div>
      ${manual ? `<div class="row">${choose}<button class="ghost" data-start="${esc(t.workflow_id)}" ${operator ? "" : "disabled"}>启动运行</button></div>` : "<p>只由复检内部触发。</p>"}
      ${schedules}${events}</div>`;
  }).join("") || '<div class="empty">本项目没有工作流模板。</div>';
  byId("workflowRuns").innerHTML = workflows.runs.map(r => `<button data-run="${esc(r.run_id)}" aria-current="${r.run_id === run?.run.run_id}">
    <span>${esc(r.workflow_id)} v${esc(r.version)}<br><small>${esc(r.run_id)} · ${esc(r.updated_at.slice(11, 19))}${r.waiting.length ? " · " + esc(r.waiting.map(w => WAIT[w] || w).join("/")) : ""}</small></span>${wchip(r.state)}</button>`).join("")
    || '<div class="empty">暂无运行。</div>';
  byId("workflowOrders").innerHTML = workflows.orders.map(o => `<div class="res"><b>${esc(o.order_id)}</b> · ${esc(o.asset_id)} · ${esc(ORDER[o.state] || o.state)}
    ${o.state === "open" ? `<button data-repair="${esc(o.order_id)}" ${operator ? "" : "disabled"}>记录维修反馈</button>` : ""}
    ${o.reinspection_run ? `<div><button data-run="${esc(o.reinspection_run)}">复检运行 ${esc(o.reinspection_run)}</button></div>` : ""}</div>`).join("")
    || '<div class="empty">暂无工单。</div>';
  byId("draftButton").disabled = !operator;
  const project = workflows.project_id;
  for (const button of byId("workflowTemplates").querySelectorAll("[data-start]")) button.onclick = () => {
    const inputs = {};
    for (const select of byId("workflowTemplates").querySelectorAll(`[data-wf="${button.dataset.start}"]`)) inputs[select.dataset.input] = select.value;
    send({type: "workflow_start", project_id: project, workflow_id: button.dataset.start, request_id: "ui-" + rid(), inputs});
    mode = "workflow";
  };
  for (const button of byId("workflowTemplates").querySelectorAll("[data-schedule]")) button.onclick = () => {
    const reason = prompt(button.dataset.action === "enable" ? "启用排班的原因（写入审计）" : "停用排班的原因（写入审计）") ?? "";
    if (reason) send({type: "workflow_schedule", project_id: project, workflow_id: button.dataset.schedule, trigger_id: button.dataset.trigger, action: button.dataset.action, reason});
  };
  for (const button of byId("workflows").querySelectorAll("[data-run]")) button.onclick = () => watchRun(button.dataset.run);
  for (const button of byId("workflowOrders").querySelectorAll("[data-repair]")) button.onclick = () => {
    const note = prompt("维修反馈说明（只转为待复检，不关单）") ?? "";
    if (note) send({type: "workflow_repair", project_id: project, order_id: button.dataset.repair, request_id: "ui-" + rid(), note});
  };
}

function watchRun(runId) {
  mode = "workflow"; resourceDetail = null;
  send({type: "workflow_watch", project_id: workflows.project_id, run_id: runId});
}

function renderRun() {
  if (!run) return;
  const r = run.run, project = r.project_id;
  const final = FINAL.includes(r.state);
  let html = `<div class="eyebrow">工作流运行 ${esc(r.run_id)} · ${esc(r.trigger_source)} · 发起 ${esc(r.started_by)}</div>
    <h2>${esc(run.template.title)}</h2><div>${wchip(r.state)} <small class="chip">${esc(r.workflow_id)} v${esc(r.version)}</small>
    ${run.waiting.length ? `<span class="chip warn">${esc(run.waiting.map(w => WAIT[w] || w).join(" / "))}</span>` : ""}</div>`;
  if (r.cancel) html += `<p>取消：${esc(r.cancel.requested_by)} · ${esc(r.cancel.requested_at.slice(11, 19))} · ${esc(r.cancel.reason)}；已开始的飞行经原通道收尾，对账前显示“取消收尾中”。</p>`;
  if (!final && !r.cancel) html += `<div class="buttons"><button class="ghost danger" id="cancelRun" ${can("operator") ? "" : "disabled"}>取消本次运行</button></div>
    <p>取消先持久化：未投递的效果作废，已提交的任务写入取消意图；停用排班请在模板处单独操作。</p>`;
  html += `<h3>节点</h3><table><tr><th>节点</th><th>活动</th><th>状态</th><th>原因 / 结果</th></tr>${run.nodes.map(n => {
    const result = n.result || {};
    let detail = esc(why(n));
    if (result.mission_id && n.activity === "submit_mission") detail = `<button class="ghost" data-mission="${esc(result.mission_id)}">${esc(result.mission_id)}</button>`;
    if (n.activity === "await_mission" && n.state === "completed") detail = `已证实 · 证据 ${esc(result.evidence_id.slice(0, 18))}…`;
    if (n.activity === "analyze_evidence" && n.state === "completed") detail = `${result.suspected ? "疑似异常" : "未见异常"} · ${esc(SOURCE[result.source] || result.source)} · ${esc(result.confidence)}`;
    if (n.activity === "human_review" && n.state === "completed") detail = `${result.decision === "confirmed" ? "确认" : "驳回"} · ${esc(result.reviewer)}`;
    if (result.order_id) detail = `工单 ${esc(result.order_id)}`;
    if (n.activity === "request_reinspection" && result.run_id) detail = `<button class="ghost" data-child="${esc(result.run_id)}">复检 ${esc(result.run_id)}</button>`;
    if (n.detail?.late_result) detail += ` · 取消后到达的结果：${esc(WSTATE[n.detail.late_result.state] || n.detail.late_result.state)}（只记录）`;
    const review = n.activity === "human_review" && n.state === "waiting" && !r.cancel
      ? `<div class="buttons"><button class="ghost" data-review="${esc(n.node_id)}" data-decision="confirmed" ${can("reviewer") ? "" : "disabled"}>确认异常</button>
         <button class="ghost danger" data-review="${esc(n.node_id)}" data-decision="dismissed" ${can("reviewer") ? "" : "disabled"}>驳回</button></div>` : "";
    return `<tr><td>${esc(n.node_id)}</td><td>${esc(ACTIVITY[n.activity] || n.activity)}</td><td>${wchip(n.state)}</td><td>${detail}${review}</td></tr>`;
  }).join("")}</table>`;
  if (run.missions.length) html += `<h3>子任务（每个仍需人工审批）</h3><table>${run.missions.map(m => `<tr><td><button class="ghost" data-mission="${esc(m.mission_id)}">${esc(m.mission_id)}</button></td>
    <td>${esc(m.node_id)}</td><td>${chip(m.status)}</td><td>${esc(m.reservations.map(x => x.state).join(", "))}</td></tr>`).join("")}</table>`;
  if (run.analyses.length) html += `<h3>分析（来源已标注；脚本 / 确定性结果不是模型识别）</h3><table>${run.analyses.map(a => `<tr><td>${esc(a.asset_id)}</td>
    <td>${esc(a.analyzer)}</td><td>${esc(SOURCE[a.source] || a.source)}</td><td>${esc(a.verdict)}</td></tr>`).join("")}</table>`;
  if (run.orders.length) html += `<h3>工单</h3><table>${run.orders.map(o => `<tr><td>${esc(o.order_id)}</td><td>${esc(o.asset_id)}</td><td>${esc(ORDER[o.state] || o.state)}</td></tr>`).join("")}</table>`;
  if (run.children.length) html += `<h3>复检运行</h3>${run.children.map(c => `<p><button class="ghost" data-child="${esc(c.run_id)}">${esc(c.run_id)}</button> ${wchip(c.state)}</p>`).join("")}`;
  html += `<h3>时间线</h3><div class="events">${run.events.slice().reverse().slice(0, 40).map(e => `<div class="event"><time>${esc(e.created_at.slice(11, 19))}</time>
    <span>${esc(e.kind)} ${esc(e.body.node_id || "")} ${esc(e.body.state || e.body.to || "")} ${esc(e.body.reason || "")}</span></div>`).join("")}</div>`;
  byId("detail").innerHTML = html;
  if (byId("cancelRun")) byId("cancelRun").onclick = () => {
    const reason = prompt("取消原因（写入审计）") ?? "";
    if (reason) send({type: "workflow_cancel", project_id: project, run_id: r.run_id, request_id: "ui-" + rid(), reason});
  };
  for (const button of byId("detail").querySelectorAll("[data-review]")) button.onclick = () => {
    const note = prompt(button.dataset.decision === "confirmed" ? "确认说明（写入复核记录）" : "驳回说明（写入复核记录）") ?? "";
    send({type: "workflow_review", project_id: project, run_id: r.run_id, node_id: button.dataset.review, decision: button.dataset.decision, request_id: "ui-" + rid(), note});
  };
  for (const button of byId("detail").querySelectorAll("[data-mission]")) button.onclick = () => { mode = "mission"; selected = null; send({type: "watch", mission_id: button.dataset.mission}); };
  for (const button of byId("detail").querySelectorAll("[data-child]")) button.onclick = () => watchRun(button.dataset.child);
}

function tchip(state) { return `<span class="chip ${TTONE[state] || ""}">${esc(TSTATE[state] || state)}</span>`; }
function waiting(t) {
  if (t.state !== "queued" || !t.waiting) return "";
  return Object.entries(t.waiting).map(([robot, list]) => `${robot}：${reasons(list) || "可派遣"}`).join("；");
}

function renderTasks() {
  const shown = Boolean(tasks);
  byId("tasks").hidden = byId("tasksEyebrow").hidden = !shown;
  if (!shown) return;
  const operator = hello?.can_write && (tasks.roles || []).includes("operator");
  const assets = Object.entries(tasks.assets || {});
  const chosen = byId("taskAsset").value;
  byId("taskAsset").innerHTML = assets.map(([id, a]) => `<option value="${esc(id)}" data-volume="${esc(a.volume_id)}">${esc(id)} · ${esc(a.robots.join("/"))}</option>`).join("");
  if (assets.some(([id]) => id === chosen)) byId("taskAsset").value = chosen;
  byId("taskSubmit").disabled = !operator || !assets.length;
  byId("taskRobots").innerHTML = tasks.robots.map(r => `<div class="res"><b>${esc(r.robot_id)}</b> · ${esc(r.dock_id)}
    ${chips([[VERDICT[r.eligibility.verdict] || r.eligibility.verdict, VTONE[r.eligibility.verdict]]])}
    ${r.eligibility.reasons.length ? `<div class="why">${esc(reasons(r.eligibility.reasons))}</div>` : ""}
    ${r.assignments.map(a => `<div><button data-task="${esc(a.task_id)}">${esc(a.task_id)}</button> 代次 ${esc(a.epoch)}${a.mission_id ? " · " + esc(a.mission_id) : ""}</div>`).join("")}</div>`).join("")
    || '<div class="empty">本项目没有机器人。</div>';
  byId("taskQueue").innerHTML = tasks.tasks.slice().reverse().map(t => `<button data-task="${esc(t.task_id)}" aria-current="${t.task_id === task?.task.task_id}">
    <span>${esc(t.asset_id)} · P${esc(t.priority)}<br><small>${esc(t.task_id)} · ${esc(t.updated_at.slice(11, 19))}${t.robot_id ? " · " + esc(t.robot_id) + " 代次 " + esc(t.epoch) : ""}</small>
    ${waiting(t) ? `<div class="why">${esc(waiting(t))}</div>` : ""}${t.reason ? `<div class="why">${esc(reasons(t.reason.split(",")))}</div>` : ""}</span>${tchip(t.state)}</button>`).join("")
    || '<div class="empty">暂无任务单。</div>';
  const holds = tasks.airspace.holds, envelopes = tasks.airspace.envelopes;
  byId("taskAirspace").innerHTML = (holds.map(h => `<div class="res"><b>${esc(h.robot_id)}</b> · ${esc(h.mission_id)} · ${esc(PAD[h.state] || h.state)}
    <div class="why">${esc(h.cells.length)} 个单元（${esc(tasks.airspace.cell_m)} m）</div></div>`).join("")
    + envelopes.map(e => `<div class="res"><b>失联包络</b> · ${esc(e.activity)}<div class="why">${esc(e.cells.length)} 个单元，只增不减，直到对账</div></div>`).join(""))
    || '<div class="empty">无持有。</div>';
  for (const button of byId("tasks").querySelectorAll("[data-task]")) button.onclick = () => watchTask(button.dataset.task);
}

function watchTask(taskId) {
  mode = "task"; resourceDetail = null;
  send({type: "task_watch", project_id: tasks.project_id, task_id: taskId});
}

byId("taskSubmit").onclick = () => {
  const option = byId("taskAsset").selectedOptions[0];
  if (!option || !tasks) return notice("请先选择项目与资产。");
  send({type: "task_submit", project_id: tasks.project_id, asset_id: option.value, volume_id: option.dataset.volume,
        candidates: [], priority: Number(byId("taskPriority").value), request_id: "ui-" + rid()});
  mode = "task";
};

function renderTask() {
  if (!task) return;
  const t = task.task, project = t.project_id;
  const live = !["completed", "failed", "outcome_unknown", "rejected", "cancelled"].includes(t.state);
  let html = `<div class="eyebrow">任务单 ${esc(t.task_id)} · 提交 ${esc(t.requested_by)} · 来源 ${esc(t.source)}</div>
    <h2>${esc(t.asset_id)} · ${esc(t.volume_id)}</h2><div>${tchip(t.state)} <small class="chip">优先级 ${esc(t.priority)}</small>
    ${t.robot_id ? `<small class="chip">${esc(t.robot_id)} · 代次 ${esc(t.epoch)}</small>` : ""}</div>`;
  if (t.reason) html += `<p>原因：${esc(reasons(t.reason.split(",")))}</p>`;
  if (t.outcome) html += `<p>结果：${esc(t.outcome.robot_id || "")} · ${esc(t.outcome.mission_id || "")} · ${esc(t.outcome.status || t.outcome.reason || "")}</p>`;
  if (t.cancel) html += `<p>取消：${esc(t.cancel.requested_by)} · ${esc((t.cancel.requested_at || "").slice(11, 19))} · ${esc(t.cancel.reason)}；已开始的飞行经原通道收尾，对账后才显示已取消。</p>`;
  if (live && !t.cancel) html += `<div class="buttons"><button class="ghost danger" id="cancelTask" ${hello?.can_write && (tasks?.roles || []).includes("operator") ? "" : "disabled"}>取消任务单</button></div>`;
  html += `<h3>分配（代次递增；撤回只发生在领取之前）</h3>${task.assignments.length ? `<table><tr><th>代次</th><th>机器人</th><th>任务</th><th>状态</th></tr>${task.assignments.map(a => `<tr>
    <td>${esc(a.epoch)}</td><td>${esc(a.robot_id)}</td><td>${a.mission_id ? `<button class="ghost" data-mission="${esc(a.mission_id)}">${esc(a.mission_id)}</button> ${a.mission_status ? chip(a.mission_status) : ""}` : ""}</td>
    <td>${esc(ASTATE[a.state] || a.state)}${a.reason ? " · " + esc(reasons(a.reason.split(","))) : ""}</td></tr>`).join("")}</table>` : '<div class="empty">尚未分配。</div>'}`;
  html += `<h3>判定（按录制快照可重放）</h3>${task.decisions.map(d => `<div class="res"><b>${esc(TVERDICT[d.verdict] || d.verdict)}${d.robot_id ? " → " + esc(d.robot_id) : ""}</b>
    · ${esc(d.created_at.slice(11, 19))} · 代次 ${esc(d.epoch)} · ${esc(d.ranking_version)} / ${esc(d.policy_version)} · 快照 ${esc(d.snapshot_sha256.slice(0, 12))}…
    <table><tr><th>候选</th><th>判定</th><th>预计到场 s</th><th>近期使用</th><th>原因</th></tr>${d.candidates.map(c => `<tr><td>${esc(c.robot_id)}</td>
    <td>${esc(VERDICT[c.verdict] || c.verdict)}</td><td>${esc(c.eta_s ?? "—")}</td><td>${esc(c.usage)}</td><td>${esc(reasons(c.reasons))}</td></tr>`).join("")}</table></div>`).join("")
    || '<div class="empty">暂无判定。</div>'}`;
  html += `<h3>时间线</h3><div class="events">${task.events.slice().reverse().slice(0, 40).map(e => `<div class="event"><time>${esc(e.created_at.slice(11, 19))}</time>
    <span>${esc(e.kind)} ${esc(e.body.robot_id || "")} ${esc(e.body.epoch ?? "")} ${esc(e.body.reason || e.body.state || "")}</span></div>`).join("")}</div>`;
  byId("detail").innerHTML = html;
  if (byId("cancelTask")) byId("cancelTask").onclick = () => {
    const reason = prompt("取消原因（写入审计）") ?? "";
    if (reason) send({type: "task_cancel", project_id: project, task_id: t.task_id, request_id: "ui-" + rid(), reason});
  };
  for (const button of byId("detail").querySelectorAll("[data-mission]")) button.onclick = () => { mode = "mission"; selected = null; send({type: "watch", mission_id: button.dataset.mission}); };
}

function renderDraft(result) {
  const spec = result.spec;
  byId("draftResult").innerHTML = `<p>草案 ${esc(result.status === "planned" ? "已生成（未生效）" : result.status === "refused" ? "被拒答" : "无效")} · 来源 ${esc(SOURCE[result.use?.source] || result.use?.source)}
    ${result.errors?.length ? " · " + esc(result.errors.join("；")) : ""}${result.decline_reason ? " · " + esc(result.decline_reason) : ""}</p>
    ${spec ? `<table>${spec.nodes.map(n => `<tr><td>${esc(n.node_id)}</td><td>${esc(ACTIVITY[n.activity] || n.activity)}</td></tr>`).join("")}</table>
    <p>摘要 ${esc(result.spec_sha256.slice(0, 16))}…；${esc(result.note)}</p>` : ""}`;
}

byId("draftButton").onclick = () => {
  const text = byId("draftText").value.trim();
  if (!text || !workflows) return notice("请先写下流程需求并选择项目。");
  send({type: "workflow_draft", project_id: workflows.project_id, text});
  byId("draftResult").innerHTML = '<p>生成草案中…</p>';
};

byId("submit").onclick = () => {
  const text = byId("text").value.trim();
  if (!text) return notice("请先写下任务目标。");
  notice("");
  const assets = [...byId("assets").querySelectorAll("input:checked")].map(i => i.value);
  byId("submit").disabled = true;
  byId("submit").textContent = "规划中…（实调模型可能需要数十秒）";
  // Re-enable on the planned mission or an error; a live model can take a while. / 收到规划结果或错误时恢复按钮。
  planning = setTimeout(planned, 180000);
  const bound = !byId("robotRow").hidden && byId("robot").value ? {project_id: byId("project").value, robot_id: byId("robot").value} : {};
  send({type: "text", rid: rid(), text, volume_id: byId("volume").value, asset_ids: assets, ...bound});
};

function planned() {
  if (planning) clearTimeout(planning);
  planning = null;
  byId("submit").disabled = !hello?.can_write;
  byId("submit").textContent = "规划并提交审批";
}

function renderList(items) {
  byId("missions").innerHTML = items.length ? items.map(m => `<button data-id="${esc(m.mission_id)}" aria-current="${m.mission_id === current?.mission.mission_id}">
    <span>${esc(m.mission_id)}<br><small>v${esc(m.current_version)} · ${esc(m.updated_at.slice(11, 19))}</small></span>${chip(m.status)}</button>`).join("")
    : '<div class="empty">暂无任务。</div>';
  for (const button of byId("missions").querySelectorAll("button")) button.onclick = () => { mode = "mission"; selected = null; resourceDetail = null; send({type: "watch", mission_id: button.dataset.id}); };
}

function version() {
  const versions = current.versions;
  return versions.find(v => v.version === selected) || versions[versions.length - 1];
}

function render() {
  const view = current, v = version(), m = view.mission;
  if (!v) {
    byId("detail").innerHTML = `<h2>${esc(view.request.text)}</h2>${chip(m.status)}<p>等待规划结果。</p>`;
    renderReport(); renderEvidence(); renderIssues();
    return;
  }
  const writable = hello?.can_write;
  let html = `<div class="eyebrow">任务 ${esc(m.mission_id)} · ${esc(view.request.channel)} · ${esc(view.request.requested_by)}</div>
    <h2>${esc(v?.spec?.goal || view.request.text)}</h2><p>原始请求：${esc(view.request.text)}</p><div>${chip(m.status)}</div>
    <div class="versions">${view.versions.map(x => `<button data-v="${x.version}" aria-pressed="${x.version === v.version}">v${x.version} · ${esc(STATUS[x.status] || x.status)} · ${esc(x.origin)}</button>`).join("")}</div>`;
  const provenance = v.provenance || {}, run = provenance.run || {};
  html += `<h3>运行来源</h3><dl><div><dt>执行后端</dt><dd>${esc(SOURCE[provenance.execution_backend] || SOURCE.legacy_unknown)}</dd></div>
    <div><dt>规划来源</dt><dd>${esc(SOURCE[provenance.planning?.source] || SOURCE.legacy_unknown)}</dd></div>
    <div><dt>分析来源</dt><dd>${esc((provenance.analysis_sources || ["legacy_unknown"]).map(s => SOURCE[s] || s).join(" / "))}</dd></div>
    <div><dt>软件版本</dt><dd>${esc(run.software_sha || "未知")}${run.dirty_sha256 ? " · 含未提交代码" : ""}</dd></div></dl>`;
  if (v.planner) html += `<h3>规划</h3><dl><div><dt>模型</dt><dd>${esc(v.planner.provider_id)} / ${esc(v.planner.model_id)}</dd></div>
    <div><dt>提示版本</dt><dd>${esc(v.planner.prompt_version)}</dd></div><div><dt>输入哈希</dt><dd>${esc((v.planner.input_hash || "").slice(0, 16))}…</dd></div>
    <div><dt>尝试 / 通道</dt><dd>${esc(v.planner.attempts)} · ${esc(v.planner.channels.join(","))}</dd></div>
    ${v.planner.decline_reason ? `<div><dt>拒答原因</dt><dd>${esc(v.planner.decline_reason)}</dd></div>` : ""}</dl>`;
  if (v.admission) html += `<h3>准入</h3><dl><div><dt>结果</dt><dd>${v.admission.accepted ? "通过" : "拒绝"}</dd></div>
    <div><dt>能耗上界</dt><dd>${esc(v.admission.energy_upper_fraction ?? "—")}</dd></div>
    ${v.admission.codes.length ? `<div><dt>问题码</dt><dd>${esc(v.admission.codes.join(", "))}</dd></div>` : ""}</dl>`;
  if (v.package) {
    html += `<h3>任务包 <small class="chip">${esc((v.package_hash || "").slice(0, 12))}</small></h3><table><tr><th>步骤</th><th>技能</th><th>参数</th><th>依赖</th></tr>
      ${v.package.nodes.map(n => `<tr><td>${esc(n.task_id)}</td><td><code>${esc(n.skill_id)}</code></td><td><code>${esc(JSON.stringify(n.params))}</code></td><td>${esc(n.depends_on.join(", "))}</td></tr>`).join("")}</table>`;
    if (v.diff && v.version > 1) html += `<p>相对上一版本：新增 ${esc(v.diff.added.join(", ") || "—")}；删除 ${esc(v.diff.removed.join(", ") || "—")}；改动 ${esc(v.diff.changed.join(", ") || "—")}</p>`;
  }
  if (v.status === "awaiting_approval" && v.version === m.current_version) {
    html += `<div class="approval"><b>待审批</b><p>审批绑定下面这个任务包哈希；审批后任何改动都会使签名失效，机载拒收。</p><code>${esc(v.package_hash)}</code>
      <div class="buttons"><button class="ghost" id="approve" ${writable ? "" : "disabled"}>批准并签名</button><button class="ghost danger" id="decline" ${writable ? "" : "disabled"}>驳回</button></div></div>`;
  }
  if (v.approval) html += `<h3>审批</h3><dl><div><dt>审批人</dt><dd>${esc(v.approval.approver)}</dd></div><div><dt>时间</dt><dd>${esc(v.approval.approved_at)}</dd></div>
    <div><dt>签名密钥</dt><dd>${esc(v.approval.signer_key_id)}</dd></div><div><dt>有效至</dt><dd>${esc(v.approval.expires_at)}</dd></div></dl>`;
  if (view.live) {
    const allowed = view.live.allowed_actions;
    html += `<h3>进行中 · ${esc(view.live.step_id)} · ${esc(view.live.state)}</h3><div class="buttons">
      ${["pause", "resume", "cancel"].map(a => `<button class="ghost ${a === "cancel" ? "danger" : ""}" data-op="${a}" ${writable && allowed.includes(a) ? "" : "disabled"}>${{pause: "暂停", resume: "恢复", cancel: "取消并安全收尾"}[a]}</button>`).join("")}</div>
      <p>操作经机载 uplink 写入操作者信箱，由 executive 与 guardian 复核绑定与时效；页面按钮不直达飞控。</p>`;
  }
  if (view.operations.length) html += `<h3>操作请求</h3><table>${view.operations.map(o => `<tr><td>${esc(o.action)}</td><td>${esc(o.requested_by)}</td><td>${o.acked ? (o.accepted ? "已转入信箱" : "被拒：" + esc(o.reason)) : "待拉取"}</td></tr>`).join("")}</table>`;
  const dispatch = view.dispatch, binding = view.binding;
  if (binding) {
    html += `<h3>派遣（P1）</h3><dl><div><dt>项目 / 站点</dt><dd>${esc(binding.project_id)}${binding.legacy ? " · 历史任务（只读）" : " / " + esc(binding.site_id)}</dd></div>
      ${binding.legacy ? "" : `<div><dt>机场 / 机器人</dt><dd>${esc(binding.dock_id)} / ${esc(binding.robot_id)}</dd></div><div><dt>执行后端</dt><dd>${esc(SOURCE[binding.execution_backend] || binding.execution_backend)}</dd></div>`}</dl>`;
    if (dispatch) {
      const latest = {};
      for (const d of dispatch.decisions) latest[d.version + ":" + d.stage] = d;
      html += `<table><tr><th>版本</th><th>阶段</th><th>判定</th><th>原因</th></tr>${Object.values(latest).map(d => `<tr><td>v${esc(d.version)}</td><td>${esc({preview: "预览", prepare: "准备", claim: "领取"}[d.stage] || d.stage)}</td>
        <td><span class="chip ${VTONE[d.verdict] || ""}">${esc(VERDICT[d.verdict] || d.verdict)}</span></td><td>${esc(reasons(d.reasons) || "—")}</td></tr>`).join("")}</table>`;
      html += `<table><tr><th>预约</th><th>状态</th><th>原因</th></tr>${dispatch.reservations.map(r => `<tr><td>v${esc(r.mission_version)}</td><td>${esc(r.state)}</td><td>${esc(r.reason)}</td></tr>`).join("")}</table>`;
      if (dispatch.claims.length) html += `<p>交付：${dispatch.claims.map(c => `v${esc(c.mission_version)} ${c.state === "claimed" ? "已领取" : "已作废（" + esc(c.reason) + "）"}`).join("；")}</p>`;
      if (dispatch.cancel) html += `<p>取消意图：${esc(dispatch.cancel.requested_by)} · ${esc(dispatch.cancel.requested_at.slice(11, 19))}${dispatch.cancel.relayed_request_id ? " · 已转发到飞行" : ""}</p>`;
      const cancellable = ["awaiting_approval", "approving", "approved", "queued", "delivered"].includes(m.status) && !dispatch.cancel && !view.live;
      if (cancellable) html += `<div class="buttons"><button class="ghost danger" id="cancelMission" ${writable ? "" : "disabled"}>取消任务</button></div>
        <p>未领取的交付立即作废并释放预约；已领取的在飞行出现运行步骤时经机载通道取消，之后不再起飞。</p>`;
    }
  }
  const cloud = view.cloud;
  if (cloud) {
    html += `<h3>云端飞行</h3><table><tr><th>版本</th><th>状态</th><th>代次</th><th>开始 / 结束</th><th>说明</th></tr>
      ${(cloud.flights || []).map(f => `<tr><td>v${esc(f.version)}</td><td>${esc(FLIGHT[f.status] || f.status)}${f.manual_cleanup ? " · 仿真操作员降落" : ""}</td>
      <td>${esc(f.epoch ?? "—")}</td><td>${esc((f.started_at || "").slice(11, 19))} / ${esc((f.ended_at || "").slice(11, 19))}</td><td>${esc(f.detail || "")}</td></tr>`).join("")}</table>`;
    const judge = cloud.judge;
    html += judge ? `<h3>独立裁判</h3><dl><div><dt>分类</dt><dd>${esc(JUDGE[judge.classification] || judge.classification)}${judge.passed ? " · 通过" : " · 未通过"}</dd></div>
      <div><dt>错误成功报告</dt><dd>${esc(judge.false_success_reports)}</dd></div><div><dt>在线 / 回放</dt><dd>${judge.replay_agrees ? "一致" : "不一致"}</dd></div>
      <div><dt>飞行版本</dt><dd>${esc((judge.flown_versions || []).join(", "))}</dd></div>
      ${(judge.problems || []).length ? `<div><dt>问题</dt><dd>${esc(judge.problems.join(", "))}</dd></div>` : ""}</dl>
      <p>裁判用仿真真值独立核对飞行与报告；页面只展示它的记录。</p>`
      : `<p>任务到达终态后，监管者用仿真真值运行独立裁判（在线与回放）。</p>`;
  }
  const events = v.events || [];
  html += `<h3>机载事件 <small class="chip">${Object.entries(v.journals || {}).map(([k, j]) => `${esc(k)} ${esc(j.rows)} · ${esc(j.chain)}`).join(" / ") || "尚未同步"}</small></h3>
    <div class="events">${events.length ? events.slice().reverse().map(e => `<div class="event ${["command_rejected", "safety_intervention", "operator_rejected"].includes(e.kind) ? "alert" : ""}">
    <time>${esc(e.timestamp.slice(11, 19))}</time><span>${esc(e.journal)} · ${esc(e.kind)} ${esc(e.data.step_id || e.data.state || "")} ${e.data.outcome ? esc(e.data.outcome.execution_status + " / " + e.data.outcome.effect_verdict) : ""}</span></div>`).join("") : '<div class="empty">飞行开始后显示来自 executive 与 guardian 账本的事件。</div>'}</div>`;
  byId("detail").innerHTML = html;
  for (const button of byId("detail").querySelectorAll(".versions button")) button.onclick = () => { selected = Number(button.dataset.v); render(); };
  if (byId("approve")) byId("approve").onclick = () => send({type: "approve", mission_id: m.mission_id, version: v.version, package_hash: v.package_hash});
  if (byId("decline")) byId("decline").onclick = () => { const reason = prompt("驳回原因（可选）") ?? ""; send({type: "decline", mission_id: m.mission_id, version: v.version, reason}); };
  for (const button of byId("detail").querySelectorAll("[data-op]")) button.onclick = () => send({type: "operate", mission_id: m.mission_id, action: button.dataset.op, request_id: "op-" + rid()});
  if (byId("cancelMission")) byId("cancelMission").onclick = () => send({type: "operate", mission_id: m.mission_id, action: "cancel", request_id: "op-" + rid()});
  renderReport(); renderEvidence(); renderIssues();
  for (const button of byId("missions").querySelectorAll("button")) button.setAttribute("aria-current", button.dataset.id === m.mission_id);
}

function renderReport() {
  const report = current?.report;
  if (!report) { byId("report").innerHTML = '<div class="empty">飞行结束后生成三列报告：已完成只接受「执行成功 ∧ 效果已证实」。</div>'; return; }
  const targets = Object.entries(report.targets);
  byId("report").innerHTML = `<div class="columns">${Object.keys(COLUMN).map(c => `<div class="column ${c}"><b>${COLUMN[c]} · ${esc(report.summary[c] ?? 0)}</b>
    <ul>${targets.filter(([, col]) => col === c).map(([t]) => `<li>${esc(t)}</li>`).join("") || "<li>—</li>"}</ul></div>`).join("")}</div>
    <table>${report.rows.map(r => `<tr><td>v${esc(r.mission_version)} ${esc(r.step_id)}</td><td>${esc(COLUMN[r.column])}</td><td><code>${esc(r.execution_status || "—")} / ${esc(r.effect_verdict || "—")}${r.service_verdict ? " · 服务 " + esc(r.service_verdict) : ""}</code></td></tr>`).join("")}</table>
    ${report.facts.length ? `<p>模型备注（不改变任何判定）：${report.facts.map(f => esc(f.predicate + "=" + JSON.stringify(f.value) + " @" + f.confidence)).join("；")}</p>` : ""}`;
}

function renderEvidence() {
  const items = current?.evidence || [];
  byId("evidence").innerHTML = items.length ? items.map(e => `<button data-id="${esc(e.evidence_id)}">v${esc(e.version)} ${esc(e.step_id)} · ${esc(e.captured_at.slice(11, 19))}
    · ${esc(SOURCE[e.provenance?.source] || SOURCE.legacy_unknown)}
    · ${e.verification ? esc(e.verification.final_verdict) + (e.verification.agrees ? "" : " · 机载/服务不一致") : "待复核"}</button>
    ${photos[e.evidence_id] ? `<div class="photo"><img alt="机载相机证据" src="${esc(photos[e.evidence_id])}"></div>` : ""}`).join("") : '<div class="empty">暂无证据。</div>';
  for (const button of byId("evidence").querySelectorAll("button")) button.onclick = () => send({type: "media", mission_id: current.mission.mission_id, evidence_id: button.dataset.id});
}

function renderHost() {
  if (!host) { byId("host").innerHTML = '<div class="empty">本入口没有仿真飞行监管者：可规划与签名，不起飞。</div>'; return; }
  byId("host").innerHTML = `<dl><div><dt>状态</dt><dd>${esc(HOST[host.state] || host.state)}</dd></div>
    ${host.mission_id ? `<div><dt>任务</dt><dd>${esc(host.mission_id)} v${esc(host.version)}</dd></div>` : ""}
    ${(host.queue || []).length ? `<div><dt>排队</dt><dd>${esc(host.queue.map(q => q.mission_id + " v" + q.version).join(", "))}</dd></div>` : ""}
    <div><dt>更新</dt><dd>${esc((host.updated_at || "").slice(11, 19))}</dd></div></dl>
    ${host.detail ? `<p>${esc(host.detail)}</p>` : ""}<p>批准后由云端仿真飞行：每个版本前飞控断电重启（地勤换电），与其他仿真和部署互斥排队。</p>`;
}

function renderIssues() {
  const issues = current?.issues || [];
  byId("issues").innerHTML = issues.length ? `<table>${issues.map(i => `<tr><td><code>${esc(i.code)}</code></td><td>${esc(i.message)}</td></tr>`).join("")}</table>` : '<div class="empty">暂无问题。</div>';
}

connect();
