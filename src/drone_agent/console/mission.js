"use strict";
// hri.v0 mission console client. Every value that came from a model or a robot is escaped before display.
// hri.v0 任务控制台客户端。来自模型或机器人的每个值在显示前都经过转义。
const byId = id => document.getElementById(id);
const esc = v => String(v ?? "").replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
const STATUS = {planning: "规划中", verifying: "证据同步中", awaiting_approval: "待审批", approving: "策略审批中", approved: "已批准·待投递", queued: "已排队投递",
  delivered: "机载已接收", delivery_rejected: "机载拒收", running: "执行中", finished: "本版本结束", completed: "已完成",
  incomplete: "未完成", refused: "模型拒答", rejected: "准入拒绝", declined: "已驳回", planning_failed: "规划失败"};
const TONE = {completed: "ok", delivered: "ok", running: "ok", awaiting_approval: "warn", incomplete: "warn", approved: "warn",
  queued: "warn", refused: "bad", rejected: "bad", declined: "bad", delivery_rejected: "bad", planning_failed: "bad"};
const COLUMN = {completed: "已完成", not_completed: "未完成", uncertain: "不确定"};
// Simulation supervisor records (D035): shown as recorded, never used to decide anything here.
// 仿真监管者记录（D035）：按记录原样展示，这里不据此做任何决定。
const HOST = {idle: "空闲，等待已审批任务", waiting_for_workspace: "等待云端工作区（其他仿真或部署占用）",
  waiting_for_memory: "等待共享主机内存", preparing: "启动仿真并预热（地勤换电）", flying: "飞行中",
  landing: "仿真操作员降落收尾", restoring: "收尾并恢复空闲仿真", judging: "独立裁判中", error: "监管者异常"};
const FLIGHT = {preparing: "准备中", flying: "飞行中", finished: "落地结束", skipped: "未起飞", overrun: "超时，由仿真操作员降落",
  failed: "失败", interrupted: "中断"};
const JUDGE = {completed: "完成", not_completed: "未完成", unsafe_or_incorrect: "不安全或不正确"};
let socket = null, hello = null, current = null, selected = null, retry = 500, photos = {}, host = null, planning = null;

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
    else if (message.type === "mission") { if (planning) planned(); current = message.view; render(); }
    else if (message.type === "media") { photos[message.evidence_id] = message.png; renderEvidence(); }
    else if (message.type === "host") { host = message.status; renderHost(); }
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
}

byId("submit").onclick = () => {
  const text = byId("text").value.trim();
  if (!text) return notice("请先写下任务目标。");
  notice("");
  const assets = [...byId("assets").querySelectorAll("input:checked")].map(i => i.value);
  byId("submit").disabled = true;
  byId("submit").textContent = "规划中…（实调模型可能需要数十秒）";
  // Re-enable on the planned mission or an error; a live model can take a while. / 收到规划结果或错误时恢复按钮。
  planning = setTimeout(planned, 180000);
  send({type: "text", rid: rid(), text, volume_id: byId("volume").value, asset_ids: assets});
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
  for (const button of byId("missions").querySelectorAll("button")) button.onclick = () => { selected = null; send({type: "watch", mission_id: button.dataset.id}); };
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
