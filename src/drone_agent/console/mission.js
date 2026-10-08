"use strict";
// hri.v0 operations desk client (D068): workspaces over one WebSocket session, addressed by the URL hash. Every value
// that came from a model, a robot or the service is escaped before display, and the page decides nothing: it shows the
// service's records and sends the named frames a person chose.
// hri.v0 运营台客户端（D068）：一条 WebSocket 会话上的多个工作区，由地址 hash 定位。来自模型、机器人或服务的每个值在
// 显示前都经过转义；页面不做任何判定，只展示服务的记录，并发送由人选择的具名帧。
const byId = id => document.getElementById(id);
const esc = v => String(v ?? "").replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));

// ── vocabulary / 词汇 ──
const STATUS = {planning: "规划中", verifying: "证据同步中", awaiting_approval: "待审批", approving: "策略审批中", approved: "已批准·待投递", queued: "已排队投递",
  delivered: "机载已接收", delivery_rejected: "机载拒收", running: "执行中", finished: "本版本结束", completed: "已完成",
  incomplete: "未完成", refused: "模型拒答", rejected: "准入拒绝", declined: "已驳回", planning_failed: "规划失败",
  cancelled: "已取消（未交付）", dispatch_expired: "审批过期未交付", withdrawn: "已作废（未交付）"};
const TONE = {planning: "info", verifying: "info", awaiting_approval: "warn", approving: "info", approved: "info", queued: "info",
  delivered: "info", running: "info", finished: "info", completed: "ok", incomplete: "warn", refused: "bad", rejected: "bad",
  declined: "bad", delivery_rejected: "bad", planning_failed: "bad", dispatch_expired: "warn"};
// Where a mission status sits on the lifecycle; display only, the service decides the status. / 任务状态在生命周期中的位置；仅用于显示，状态由服务决定。
const STAGES = ["规划", "准入", "审批", "投递", "飞行", "结果"];
const STAGE = {planning: [0, "now"], planning_failed: [0, "stop"], refused: [0, "stop"], rejected: [1, "stop"],
  awaiting_approval: [2, "now warn"], approving: [2, "now"], declined: [2, "stop"], approved: [3, "now"], queued: [3, "now"],
  cancelled: [3, "stop warn"], dispatch_expired: [3, "stop warn"], withdrawn: [3, "stop warn"], delivery_rejected: [3, "stop"],
  delivered: [4, "now"], running: [4, "now"], finished: [5, "now"], verifying: [5, "now"], completed: [5, "ok"],
  incomplete: [5, "stop warn"]};
const PENDING = ["awaiting_approval"];
const ACTIVE = ["planning", "verifying", "approving", "approved", "queued", "delivered", "running", "finished"];
const CHANNEL = {console: "任务台", a2a: "A2A", workflow: "工作流", scheduler: "调度器", harness: "编排"};
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
const VTONE = {eligible: "ok", blocked: "bad", unknown: "unknown"};
const DIM = {online: "在线", offline: "离线", closed: "舱盖关", opening: "开盖中", open: "舱盖开", closing: "关盖中", jammed: "舱盖卡滞",
  present: "在位", absent: "不在位", idle: "未补能", charging: "充电中", cooling: "冷却中", ready: "补能就绪", fault: "故障",
  permitted: "环境许可", deferred: "环境推迟", normal: "运维正常", maintenance: "维护", unknown: "未知"};
const PAD = {free: "机位空闲", reserved: "软预约", occupied: "占用", uncertain: "占用待对账"};
// Shorter state words inside a dimension tile that already names the dimension. / 维度卡片已写明维度时用的简短状态词。
const SHORT = {online: "在线", offline: "离线", closed: "关闭", opening: "开盖中", open: "开启", closing: "关盖中", jammed: "卡滞",
  present: "在位", absent: "不在位", idle: "未补能", charging: "充电中", cooling: "冷却中", ready: "就绪", fault: "故障",
  permitted: "许可", deferred: "推迟", normal: "正常", maintenance: "维护", unknown: "未知"};
const RESERVATION = {reserved: "软预约", occupied: "占用", uncertain: "占用待对账", released: "已释放"};
const RELEASE = {reserved: "已预约", re_reserved: "重新预约", reconciled: "对账后释放", cancelled: "取消后释放",
  approval_expired: "审批过期", assignment_withdrawn: "分配已撤回", no_longer_eligible: "不再可派遣", admission_rejected: "准入拒绝"};
const DOCK_ACTION = {open_lid: "开盖", close_lid: "关盖", start_charge: "开始补能"};
const ACTION_STATE = {requested: "已请求", acked: "已受理", rejected: "被拒", completed: "已完成", failed: "失败"};
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
const HOST_SHORT = {idle: "空闲", waiting_for_workspace: "等待工作区", waiting_for_memory: "等待内存", preparing: "预热中",
  flying: "飞行中", landing: "降落收尾", restoring: "恢复空闲", judging: "裁判中", error: "异常"};
const HOST_TONE = {idle: "ok", waiting_for_workspace: "warn", waiting_for_memory: "warn", preparing: "info", flying: "info",
  landing: "info", restoring: "info", judging: "info", error: "bad"};
const FLIGHT = {preparing: "准备中", flying: "飞行中", finished: "落地结束", skipped: "未起飞", overrun: "超时，由仿真操作员降落",
  failed: "失败", interrupted: "中断"};
const JUDGE = {completed: "完成", not_completed: "未完成", unsafe_or_incorrect: "不安全或不正确"};
const ALERTS = ["command_rejected", "safety_intervention", "operator_rejected"];
// P2 workflows (D057): runs, nodes and reasons as the service reports them. / P2 工作流：按服务记录展示运行、节点与原因。
const WSTATE = {pending: "待开始", running: "进行中", waiting: "等待中", completed: "已完成", skipped: "已跳过", failed: "失败",
  outcome_unknown: "结果未知", cancelled: "已取消", cancel_requested: "已请求取消", cancelling: "取消收尾中"};
const WTONE = {completed: "ok", running: "info", waiting: "warn", cancel_requested: "warn", cancelling: "warn", failed: "bad",
  outcome_unknown: "unknown", cancelled: "bad"};
const NODE_TONE = {completed: "ok", running: "info", waiting: "warn", cancel_requested: "warn", cancelling: "warn", failed: "bad",
  outcome_unknown: "unknown", cancelled: "neutral", skipped: "neutral"};
const WAIT = {approval: "等待审批", device: "等待设备", environment: "等待环境", flight: "飞行中", evidence: "等待证据复核",
  review: "等待人工复核", repair: "等待维修反馈", delivery: "投递中", assignment: "等待调度分配", analysis: "分析中"};
const SKIP = {condition_false: "条件不满足", upstream_skipped: "上游已跳过", upstream_failed: "上游失败", upstream_unknown: "上游结果未知",
  cancelled: "随运行取消"};
const ACTIVITY = {submit_mission: "提交任务", await_mission: "等待任务结果", analyze_evidence: "分析证据", human_review: "人工复核",
  create_work_order: "模拟工单", await_repair: "等待维修反馈", request_reinspection: "请求复检", build_report: "生成报告",
  settle_reinspection: "复检结算"};
const TRIGGER = {manual: "手动", schedule: "排班", event: "事件", internal: "内部"};
const ORDER = {open: "待维修", repair_reported: "已反馈维修·待复检", reinspection_requested: "已请求复检"};
const FINAL = ["completed", "failed", "outcome_unknown", "cancelled"];
// P3 scheduling (D059): the scheduler's queue, decisions and holds as the service records them.
// P3 调度（D059）：按服务记录展示调度器的队列、判定与持有。
Object.assign(REASON, {"asset.unregistered": "该机站点未登记此资产", "volume.unapproved": "体积不是已批准的仿真体积",
  "task.robot_excluded": "本任务已排除该机（此前失败）", "airspace.cell_held": "航迹单元被其他活动持有",
  "airspace.envelope": "落入失联飞行器的包络", "airspace.hold_missing": "本活动未持有航迹单元",
  "task.approval_expired": "预约有效期内无人审批，分配已撤回", "task.assignments_exhausted": "分配次数用尽",
  "task.no_candidates": "没有候选机器人"});
const TSTATE = {queued: "排队中", assigned: "已分配", completed: "已完成", failed: "失败", outcome_unknown: "结果未知",
  rejected: "已拒绝", cancel_requested: "已请求取消", cancelling: "取消收尾中", cancelled: "已取消"};
const TTONE = {completed: "ok", assigned: "info", queued: "warn", cancel_requested: "warn", cancelling: "warn", failed: "bad",
  outcome_unknown: "unknown", rejected: "bad", cancelled: "bad"};
const TASK_FINAL = ["completed", "failed", "outcome_unknown", "rejected", "cancelled"];
const ASTATE = {active: "有效", withdrawn: "已撤回（未领取）", ended: "已结束"};
const TVERDICT = {assign: "分配", wait: "等待", reject: "拒绝"};
// P4 business loop (D063): findings, orders, analyses and references as the service records them. Model and scripted
// answers are candidates with their source; only a reviewer's decision confirms a finding or a repair.
// P4 业务闭环（D063）：按服务记录展示发现、工单、分析与参考外观。模型与脚本回答只是带来源的候选；只有 reviewer 的决定能
// 确认发现或修复。
const FSTATE = {candidate: "候选·待复核", confirmed: "已确认", dismissed: "已驳回", resolved: "已修复关闭"};
const FTONE = {candidate: "warn", confirmed: "bad", resolved: "ok"};
Object.assign(ORDER, {reinspection_failed: "复检未通过·待再修", reinspection_unknown: "复检不确定·待再修", closed: "已关单"});
const OTONE = {open: "warn", reinspection_requested: "info", repair_reported: "info", reinspection_failed: "bad", reinspection_unknown: "unknown", closed: "ok"};
const RSTATE = {reinspecting: "复检中", passed: "通过", failed: "未通过", unknown: "不确定"};
const JSTATE = {queued: "排队", running: "分析中", completed: "完成", refused: "拒判", cancelled: "已取消"};
const PURPOSE = {inspection: "巡检", reinspection: "复检", reuse: "复用"};
const BVERDICT = {suspected: "疑似异常", normal: "未见异常", refused: "拒判"};
const BTONE = {suspected: "warn", normal: "ok", refused: "neutral"};
const REPAIRABLE = ["open", "reinspection_failed", "reinspection_unknown"];
Object.assign(REASON, {"reinspection.still_anomalous": "复检仍疑似异常", "reinspection.review_dismissed": "复核驳回修复",
  "reinspection.review_missing": "缺少本轮复核", "reinspection.evidence_missing": "缺少复检证据",
  "reinspection.before_feedback": "采集早于维修反馈", "reinspection.not_new_acquisition": "不是新的采集",
  "reinspection.replayed_media": "媒体与旧采集相同（疑似重放）", "reinspection.analysis_refused": "复检分析拒判",
  "reinspection.source_not_allowed": "该分析来源不能用于关单", "reinspection.capture_time_unknown": "采集时间未知",
  "reinspection.run_ended": "复检运行已结束", "quality.blurry": "图像模糊", "quality.exposure": "曝光异常",
  "quality.resolution": "分辨率不足", "quality.media_missing": "媒体缺失", "quality.media_mismatch": "媒体摘要不符",
  "quality.capture_time_unknown": "采集时间未知", "analysis.undeterminable": "无法判定", "analysis.no_reference": "没有登记参考外观",
  "analysis.not_verified": "证据未证实", "analysis.run_cancelled": "运行已取消", "analysis.attempts_exhausted": "尝试次数用尽",
  "analysis.error": "分析异常", "target.mismatch": "与登记目标不符", "model.unavailable": "模型不可用",
  "model.uncalibrated": "模型阈值未标定", "model.timeout": "模型超时", "model.error": "模型调用失败",
  "model.malformed": "模型回答格式不合规", "model.refusal": "模型拒答", "model.budget_exhausted": "当日模型预算用尽",
  "model.profile_mismatch": "画像摘要不符", "model.score_at_or_above_threshold": "分数达到阈值",
  "model.score_below_threshold": "分数低于阈值", "model.blurred": "模型：模糊", "model.dark": "模型：过暗",
  "model.overexposed": "模型：过曝", "model.occluded": "模型：遮挡", "model.out_of_frame": "模型：目标出画",
  "model.other": "模型：其他原因", "reuse.stale": "采集过旧", "reuse.modality_mismatch": "模态不符",
  "reuse.resolution": "分辨率不符", "reuse.source_unknown": "来源未知", "reuse.analyzer_not_allowed": "分析器不允许复用",
  scripted_fixture: "脚本夹具答案"});

// Service issue codes in plain words; the code itself stays visible for tracing. / 服务问题码的中文说明；问题码本身仍显示以便追溯。
const ISSUE = {"service.invalid_request": "请求无效", "service.not_found": "对象不存在或无权查看", "service.degraded": "任务服务暂不可用，稍后刷新",
  "service.idempotency_conflict": "同一请求号已用于不同内容", "auth.project_denied": "没有该项目的相应角色", "auth.identity_missing": "需要已识别的操作者身份",
  "auth.scope_missing": "当前身份没有这项权限", "auth.method_not_allowed": "当前身份不能调用这项操作", "auth.backend_mismatch": "后端身份不匹配",
  "approval.stale_version": "版本或任务包已变化，请按最新版本审批", "approval.identity_missing": "审批需要已识别的身份",
  "approval.not_admitted": "该版本未通过准入，不能审批", "approval.unverified_edge_requires_human": "含未验证的恢复边，只能人工审批",
  "package.hash_mismatch": "任务包哈希不符", "dispatch.reservation_conflict": "资源被其他任务的预约占用", "dispatch.backend_mismatch": "执行后端与目录不符",
  "dispatch.not_locked": "机场没有维护锁", "dispatch.cancelled": "任务已取消", "workflow.order_state": "工单状态不允许该操作",
  "workflow.not_waiting": "节点不在等待中", "workflow.already_decided": "已有人先作出决定", "workflow.schedule_invalid": "排班操作无效",
  "workflow.not_startable": "该模板不能手动启动", "workflow.not_cancellable": "运行已结束，不能取消", "workflow.invalid_inputs": "输入不合法",
  "workflow.invalid_draft": "草案无效", "task.invalid_request": "任务单请求无效", "task.not_cancellable": "任务单已结束，不能取消",
  "task.candidate_outside_project": "候选机器人不属于本项目", "analysis.not_verified": "证据未证实，不能分析", "business.not_reinspection": "不是复检轮次",
  "business.not_confirmed": "发现尚未确认", "business.not_configured": "未配置业务目录", "reuse.analyzer_not_allowed": "分析器不允许复用"};

// ── state / 状态 ──
const WORKSPACES = ["overview", "missions", "workflows", "tasks", "fleet", "business", "audit"];
const NAV = {overview: "navOverview", missions: "navMissions", workflows: "navWorkflows", tasks: "navTasks", fleet: "navFleet",
  business: "navBusiness", audit: "navAudit"};
// P5 project audit (D070): who did what, as the service recorded it; refused attempts of members included.
// P5 项目审计（D070）：按服务记录展示谁做了什么，包括成员被拒的尝试。
const AUDIT = {"mission.submitted": "提交任务", "mission.approved": "审批签名", "mission.auto_approved": "策略批准原样重试",
  "mission.declined": "驳回", "mission.pause": "暂停", "mission.resume": "恢复", "mission.cancel": "取消（经机载通道）",
  "cancel.requested": "请求取消", "cancel.relayed": "转发取消到飞行", "delivery.claimed": "领取闸门交付", "delivery.void": "投递作废",
  "activity.departed": "离开机位", "action.requested": "请求机场动作", "action.ack": "机场受理动作", "action.ack_ignored": "忽略机场回执",
  "lock.set": "设置维护锁", "lock.released": "解除维护锁", "session.new": "机场新会话", "status.rejected": "拒收机场报告",
  "run.started": "启动工作流", "run.cancel_requested": "请求取消运行", "run.state": "运行结束", "review.recorded": "复核节点决定",
  "schedule.enabled": "启用排班", "schedule.disabled": "停用排班", "draft.proposed": "生成模板草案（未生效）", "event.refused": "拒收事件",
  "task.created": "提交任务单", "task.state": "任务单状态", "assignment.created": "分配", "assignment.withdrawn": "撤回分配",
  "assignment.approval_expired": "无人审批，撤回分配", "assignment.refused": "分配被拒", "withdrawal.refused": "撤回被拒",
  "finding.opened": "开启候选发现", "finding.attached": "并入已有发现", "finding.reviewed": "复核发现", "order.created": "建立工单",
  "order.repair_reported": "维修反馈", "order.reinspection_requested": "请求复检", "order.round_reviewed": "复核本轮",
  "order.round_passed": "本轮通过·关单", "order.round_failed": "本轮未通过", "order.round_unknown": "本轮不确定",
  "reference.registered": "登记参考外观", "reference.revoked": "撤销参考外观", "job.queued": "分析排队", "job.completed": "分析完成",
  "job.refused": "分析拒判", "reuse.refused": "复用被拒", "access.denied": "越权尝试（已拒绝）"};
const AUDIT_GROUPS = [["all", "全部"], ["mission", "任务与审批"], ["workflow", "工作流"], ["business", "发现与工单"],
  ["dispatch", "资源与派遣"], ["task", "调度"], ["denied", "越权尝试"]];
// Display vocabulary only; unknown event codes stay verbatim. / 仅用于显示的用词表；缺项保留原始事件码。
const EVENT = {...AUDIT, mission_accepted: "机载接受任务", package_verified: "任务包验签通过", skill_state: "技能状态",
  step_outcome: "步骤结果", command_rejected: "指令被拒", operator_request: "收到操作请求", operator_rejected: "操作被拒",
  safety_intervention: "安全监督介入", recovered_to: "进入恢复状态", mission_result: "任务执行结果",
  "node.state": "节点状态", "lease.taken": "取得运行租约", "status.accepted": "接受机场报告"};
const EXECUTION = {pending: "待执行", accepted: "已接受", running: "执行中", succeeded: "执行成功", failed: "执行失败",
  cancelled: "已取消", timeout: "执行超时", unknown: "未知"};
const EFFECT = {verified: "已证实", unverified: "未证实", refuted: "被推翻", unknown: "未知"};
const EXECUTION_TONE = {succeeded: "ok", failed: "bad", running: "info", accepted: "info", timeout: "unknown", unknown: "unknown"};
const EFFECT_TONE = {verified: "ok", refuted: "bad", unverified: "unknown", unknown: "unknown"};
const SIM_BACKENDS = ["logical_sim", "px4_sitl", "vendor_protocol_sim"];
function auditGroup(e) {
  const a = e.action || "", kind = e.object?.kind;
  if (a === "access.denied") return "denied";
  if (a.startsWith("mission.")) return "mission";
  if (["run.", "review.", "schedule.", "draft.", "event."].some(p => a.startsWith(p))) return "workflow";
  if (["finding.", "order.", "reference.", "job.", "reuse."].some(p => a.startsWith(p))) return "business";
  if (a.startsWith("task.") || a.startsWith("assignment.") || a.startsWith("withdrawal.")) return "task";
  return kind === "mission" || kind === "dock" ? "dispatch" : "mission";
}
function auditObject(o) {
  if (!o) return "—";
  const to = {mission: "missions/", workflow: "workflows/", task: "tasks/", finding: "business/finding/", order: "business/order/", dock: "fleet/"}[o.kind];
  return to ? link(to + o.id, o.id + (o.version ? " v" + o.version : "")) : `${o.kind} ${o.id}`;
}
const VENDOR_STATUS = {sent: "已下发", in_progress: "执行中", paused: "暂停", ok: "完成", partially_done: "部分完成", failed: "失败",
  canceled: "已取消", rejected: "被拒", timeout: "超时", undone: "执行前已撤销", unreachable: "厂商无应答（未执行）",
  prepare_rejected: "厂商拒绝准备（未执行）", execute_rejected: "厂商拒绝执行（未执行）"};
let socket = null, retry = 500, hello = null, projectId = null, planning = null, expect = null;
let missions = [], current = null, selected = null, photos = {}, asked = new Set(), host = null, filter = "all";
let resources = null, resourceDetail = null, workflows = null, run = null, wfChoice = null;
let tasks = null, task = null, business = null, subject = null, selectedEvidence = null;
let audit = null, auditBefore = null, auditFilter = "all";
let nav = {ws: "overview", id: null, kind: null};

// ── small helpers / 小工具 ──
const RAW = Symbol("raw");
const raw = html => ({[RAW]: String(html)});
const show = v => (v !== null && typeof v === "object" && RAW in v) ? v[RAW] : esc(v);
const badge = (label, tone) => `<span class="badge ${tone || ""}">${esc(label)}</span>`;
const tag = v => `<span class="tag">${esc(v)}</span>`;
function sourceChip(source) {
  const missing = !source || ["unknown", "legacy_unknown"].includes(source) || !Object.hasOwn(SOURCE, source);
  const tone = missing ? "missing" : SIM_BACKENDS.includes(source) ? "sim" : source === "real_device" ? "device"
    : ["live_model", "recorded_model"].includes(source) ? "model" : source === "scripted" ? "script" : "";
  const icon = {sim: "◇", device: "◆", model: "○", script: "⌘", missing: "?"}[tone] || "□";
  return `<span class="src ${tone}" title="${esc(source || "legacy_unknown")}"><span aria-hidden="true">${icon}</span>${esc(missing ? "来源未记录" : SOURCE[source])}</span>`;
}
function environmentTag(robots, expected = []) {
  const groups = new Map();
  for (const r of robots) {
    const backend = r.execution_backend || "legacy_unknown";
    if (!groups.has(backend)) groups.set(backend, []);
    groups.get(backend).push(r.robot_id);
  }
  const missing = !robots.length || expected.some(id => !robots.some(r => r.robot_id === id))
    || [...groups.keys()].some(k => !SIM_BACKENDS.includes(k) && k !== "real_device");
  const real = groups.has("real_device"), mixed = groups.size > 1;
  const label = real ? "真机 · 实飞" : missing ? "来源未记录" : mixed ? "仿真 · 混合后端" : "仿真 · " + SOURCE[groups.keys().next().value];
  const tone = real ? "real" : missing ? "missing" : "sim";
  const rows = [...groups].map(([key, ids]) => `<div>${sourceChip(key)}<span class="mono">${ids.map(esc).join(" / ")}</span></div>`).join("");
  return `<details class="env-popover" data-k="environment"><summary class="env ${tone}"><span aria-hidden="true">${real ? "●" : missing ? "?" : "◇"}</span>${esc(label)}</summary>
    <div class="env-list"><b>当前项目 · 执行后端</b>${rows || '<p>尚无机器人来源记录。</p>'}${missing ? '<p>来源未记录的机器人不推定为仿真。</p>' : ""}</div></details>`;
}
function eventName(kind) {
  return Object.hasOwn(EVENT, kind) ? `${esc(EVENT[kind])}<code class="code">${esc(kind)}</code>` : `<code>${esc(kind)}</code>`;
}
function hashBlock(value) {
  const hash = String(value || ""), groups = /^[a-f\d]{64}$/i.test(hash) ? hash.match(/.{8}/g) : [hash || "未提供"];
  return `<div class="hash"><div class="hash-head"><span>任务包 SHA-256 · 完整值</span><button class="btn ghost small" type="button" data-act="copyHash" data-hash="${esc(hash)}" ${hash ? "" : "disabled"}>复制完整哈希</button></div>
    <code class="hash-value" aria-label="${esc(hash)}">${groups.map(g => `<span>${esc(g)}</span>`).join("")}</code></div>`;
}
function chip(status) { return badge(STATUS[status] || status, TONE[status]); }
function wchip(state) { return badge(WSTATE[state] || state, WTONE[state]); }
function tchip(state) { return badge(TSTATE[state] || state, TTONE[state]); }
function bchip(names, tones, state) { return badge(names[state] || state, tones[state]); }
function reasons(list) { return (list || []).map(r => REASON[r] || r).join("；"); }
function rid() { return crypto.randomUUID().replace(/-/g, "").slice(0, 16); }
function kv(rows) {
  const body = rows.filter(Boolean).map(([k, v]) => `<dt>${esc(k)}</dt><dd>${show(v)}</dd>`).join("");
  return body ? `<dl class="kv">${body}</dl>` : "";
}
function table(head, rows, empty, rowClass = () => "") {
  if (!rows.length) return empty === undefined ? "" : `<div class="empty">${esc(empty)}</div>`;
  return `<div class="table-wrap"><table><thead><tr>${head.map(h => `<th>${esc(h)}</th>`).join("")}</tr></thead><tbody>${
    rows.map((r, i) => `<tr${rowClass(i) ? ` class="${esc(rowClass(i))}"` : ""}>${r.map(c => `<td>${show(c)}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`;
}
function card(title, body, sub) {
  return `<section class="card"><div class="card-head"><h3>${show(title)}</h3>${sub ? `<span class="sub">${show(sub)}</span>` : ""}</div>${body}</section>`;
}
const detailBack = (workspace, label) => `<a class="detail-back" href="#${workspace}">← ${label}</a>`;
function sameDay(a, b) { return a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate(); }
function stamp(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return String(iso);
  const time = d.toLocaleTimeString("zh-CN", {hour12: false});
  return sameDay(d, new Date()) ? time : `${d.getMonth() + 1}-${String(d.getDate()).padStart(2, "0")} ${time}`;
}
const when = iso => raw(`<time datetime="${esc(iso)}" title="${esc(iso)}">${esc(stamp(iso))}</time>`);
function ago(iso) {
  const seconds = (Date.now() - new Date(iso).getTime()) / 1000;
  if (!Number.isFinite(seconds)) return "—";
  if (seconds < 45) return "刚刚";
  if (seconds < 3600) return Math.round(seconds / 60) + " 分钟前";
  if (seconds < 86400) return Math.round(seconds / 3600) + " 小时前";
  return Math.round(seconds / 86400) + " 天前";
}
const short = (v, n = 12) => v ? raw(`<span title="${esc(v)}">${esc(String(v).slice(0, n))}…</span>`) : "—";
const link = (hash, label, mono = true) => raw(`<a${mono ? ' class="mono"' : ""} href="#${esc(hash)}">${esc(label)}</a>`);
const pct = f => f == null || !Number.isFinite(Number(f)) ? "—" : Math.round(Number(f) * 100) + "%";
// Catalog names are written "English / 中文"; the page shows the Chinese half and keeps the whole as a tooltip.
// 目录名称写作「English / 中文」；页面显示中文部分，整句留作提示。
function zh(text) { const value = String(text ?? ""), cut = value.lastIndexOf(" / "); return cut > 0 ? value.slice(cut + 3) : value; }
function project() { return (hello?.projects || []).find(p => p.project_id === projectId) || null; }
function can(role, roles) { return !!hello?.can_write && (roles || []).includes(role); }
// A mission's own project decides its roles; without an operations catalog the service decides. / 任务所属项目决定角色；没有运营目录时由服务决定。
function canMission(role, view = current) {
  if (!hello?.can_write) return false;
  const id = view?.binding?.project_id, projects = hello.projects || [];
  if (!id || !projects.length) return true;
  return (projects.find(p => p.project_id === id)?.roles || []).includes(role);
}

// Replace a region only when its markup changed, keeping what a person was doing inside it: the values, scroll
// positions, open sections and focus of elements marked with data-k.
// 只在区域标记变化时替换，并保留人在其中的操作：带 data-k 的元素的值、滚动位置、展开状态与焦点。
const painted = new WeakMap();
function paint(target, html) {
  const el = typeof target === "string" ? byId(target) : target;
  if (!el || painted.get(el) === html) return el;
  const keep = remember(el);
  el.innerHTML = html;
  painted.set(el, html);
  recall(el, keep);
  return el;
}
function remember(el) {
  const keep = {};
  if (typeof el.querySelectorAll !== "function") return keep;
  for (const node of el.querySelectorAll("[data-k]")) {
    keep[node.dataset.k] = {value: node.value, scroll: node.scrollTop, open: node.open, focus: node === document.activeElement};
  }
  return keep;
}
function recall(el, keep) {
  if (typeof el.querySelectorAll !== "function") return;
  for (const node of el.querySelectorAll("[data-k]")) {
    const saved = keep[node.dataset.k];
    if (!saved) continue;
    const offered = node.tagName !== "SELECT" || [...node.options].some(o => o.value === saved.value);
    if (saved.value !== undefined && "value" in node && offered) node.value = saved.value;
    if (saved.scroll) node.scrollTop = saved.scroll;
    if (saved.open !== undefined && "open" in node) node.open = saved.open;
    if (saved.focus && typeof node.focus === "function") node.focus();
  }
}
let noticeText = "";
function notice(text) {
  noticeText = text || "";
  const el = byId("notice");
  el.hidden = !text;
  paint(el, text ? `<span>${esc(text)}</span> <button class="btn link" type="button" data-act="dismiss">关闭</button>` : "");
}
function blank(title, body = "") { return `<div class="blank"><b>${esc(title)}</b>${body ? `<span>${esc(body)}</span>` : ""}</div>`; }

// The reason dialog: cancel sends nothing, and required reasons stay required. / 原因对话框：取消不发送任何东西，必填的原因仍然必填。
function ask({title, hint = "", required = true, ok = "确定"}) {
  const dialog = byId("ask");
  if (!dialog || typeof dialog.showModal !== "function") {
    const value = typeof prompt === "function" ? prompt(hint ? `${title}\n${hint}` : title) : null;
    if (value === null || value === undefined) return Promise.resolve(null);
    return Promise.resolve(required && !String(value).trim() ? null : String(value).trim());
  }
  return new Promise(resolve => {
    const text = byId("askText");
    byId("askTitle").textContent = title;
    byId("askHint").textContent = hint;
    byId("askOk").textContent = ok;
    text.value = "";
    text.placeholder = required ? "必填，写入审计" : "可选";
    text.removeAttribute("aria-invalid");
    const finish = value => { dialog.oncancel = null; if (dialog.open) dialog.close(); resolve(value); };
    byId("askOk").onclick = () => {
      const value = text.value.trim();
      if (required && !value) { text.setAttribute("aria-invalid", "true"); text.focus(); return; }
      finish(value);
    };
    byId("askCancel").onclick = () => finish(null);
    dialog.oncancel = event => { event.preventDefault(); finish(null); };
    dialog.showModal();
    text.focus();
  });
}

// ── connection / 连接 ──
function send(message, quiet = false) {
  if (socket && socket.readyState === 1) { socket.send(JSON.stringify(message)); return true; }
  if (!quiet) notice("连接中断，请等待状态重新同步后操作。");
  return false;
}

function connect() {
  socket = new WebSocket((location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws/session");
  socket.onopen = () => { retry = 500; };
  socket.onclose = () => {
    // Keep showing the last state, read-only, until a new hello arrives. / 在新的 hello 到达前，只读地保留最后的状态。
    if (hello) hello = {...hello, can_write: false};
    planned();
    setConnection(false);
    renderAll();
    setTimeout(connect, retry = Math.min(retry * 2, 8000));
  };
  socket.onmessage = event => receive(JSON.parse(event.data));
}

function receive(message) {
  const kind = message.type;
  if (kind === "hello") onHello(message);
  else if (kind === "missions") { missions = message.items || []; renderMissionList(); renderOverview(); }
  else if (kind === "mission") onMission(message.view);
  else if (kind === "workflows") { workflows = message.view; renderWorkflows(); renderOverview(); }
  else if (kind === "workflow") onRun(message.view);
  else if (kind === "workflow_draft") renderDraft(message.result);
  else if (kind === "tasks") { tasks = message.view; renderTasks(); renderOverview(); }
  else if (kind === "task") onTask(message.view);
  else if (kind === "business") { business = message.view; renderBusiness(); renderOverview(); renderMission(); }
  else if (kind === "finding" || kind === "order") onSubject(kind, message.view);
  else if (kind === "audit") { audit = message.view; renderAudit(); }
  else if (kind === "media") {
    if (typeof message.png === "string" && message.png.startsWith("data:image/png;base64,")) photos[message.evidence_id] = message.png;
    renderMission(); renderSubject();
  }
  else if (kind === "host") { host = message.status; renderHost(); renderOverview(); }
  else if (kind === "resources") { resources = message.view; renderResources(); renderOverview(); }
  else if (kind === "resource") { resourceDetail = message.detail; renderResourceDetail(); }
  else if (kind === "error") {
    if (planning) planned();
    expect = null;
    const code = message.issue?.code;
    notice([ISSUE[code] || REASON[code], code, message.message && message.message !== code ? message.message : ""].filter(Boolean).join(" · "));
  }
}

function setConnection(ok) {
  byId("who").className = "who" + (ok ? " good" : "");
  byId("whoText").textContent = ok ? (hello?.identity || "只读会话（无 tailnet 身份）") : "连接中断，历史状态仅供查看";
}

function plannerLabel(planner) {
  if (planner === "scripted") return "脚本回答（非模型，只认场景集原文）";
  if (planner?.startsWith("live:")) return planner.slice(5);
  if (planner?.startsWith("unavailable")) return "不可用";
  return planner || "未知";
}

function onHello(message) {
  hello = message;
  setConnection(true);
  const planner = message.planner || "";
  const plannerTag = byId("plannerTag");
  plannerTag.textContent = "规划 · " + plannerLabel(planner);
  plannerTag.className = "badge plain " + (planner === "scripted" ? "warn" : planner.startsWith("live:") ? "info" : "bad");
  byId("volume").innerHTML = (message.volumes || []).map(v => `<option value="${esc(v.volume_id)}">${esc(v.volume_id)} · ${esc(v.airspace_mode)}</option>`).join("");
  byId("assets").innerHTML = (message.assets || []).map(a => `<label class="check" title="${esc(a.description)}"><input type="checkbox" value="${esc(a.asset_id)}">${esc(a.asset_id)}</label>`).join("");
  byId("submit").disabled = !message.can_write;
  if (!message.can_write) notice("当前会话没有 tailnet 身份，只能查看；提交、审批与操作需要已识别的操作者。");
  else if (noticeText.startsWith("连接中断")) notice("");
  const projects = (message.projects || []).filter(p => !p.legacy);
  byId("projectBox").hidden = byId("robotRow").hidden = byId("missionScope").hidden = !projects.length;
  byId("project").innerHTML = projects.map(p => `<option value="${esc(p.project_id)}" title="${esc(p.name)}">${esc(p.project_id)} · ${esc(zh(p.name))}</option>`).join("");
  if (projects.some(p => p.project_id === projectId)) byId("project").value = projectId;
  paint("railFoot", `${esc(message.protocol)}${message.source_sha ? " · 版本 " + esc(String(message.source_sha).slice(0, 7)) : ""}<br>规划 ${esc(plannerLabel(planner))}`);
  if (projects.length) selectProject();
  else { projectId = null; resources = resourceDetail = workflows = run = tasks = task = business = subject = null; }
  route(true);
}

function selectProject() {
  const projects = (hello?.projects || []).filter(p => !p.legacy);
  projectId = byId("project").value || projects[0]?.project_id || null;
  const p = project();
  byId("robot").innerHTML = (p?.robots || []).map(r => `<option value="${esc(r)}">${esc(r)}</option>`).join("");
  resources = resourceDetail = null; workflows = run = null; tasks = task = null; business = subject = null;
  audit = null; auditBefore = null;
  if (p) {
    send({type: "resources", project_id: p.project_id});
    send({type: "workflows", project_id: p.project_id});
    if (p.scheduling) send({type: "tasks_watch", project_id: p.project_id});
    if (p.business) send({type: "business_watch", project_id: p.project_id});
    if (nav.ws === "audit") send({type: "audit_watch", project_id: p.project_id, before: null});
  }
  renderAll();
}

function onMission(view) {
  current = view;
  const id = view.mission.mission_id;
  const item = missions.find(m => m.mission_id === id);
  if (item) Object.assign(item, {status: view.mission.status, current_version: view.mission.current_version, updated_at: view.mission.updated_at});
  if (expect?.kind === "mission" && !expect.known.has(id)) { expect = null; planned(); selected = null; go("missions/" + id); }
  renderMission(); renderMissionList(); renderOverview();
}
function onRun(view) {
  run = view;
  if (expect?.kind === "run" && !expect.known.has(view.run.run_id)) { expect = null; go("workflows/" + view.run.run_id); }
  renderRun(); renderWorkflowRuns();
}
function onTask(view) {
  task = view;
  if (expect?.kind === "task" && !expect.known.has(view.task.task_id)) { expect = null; go("tasks/" + view.task.task_id); }
  renderTask(); renderTaskQueue();
}
function onSubject(kind, view) { subject = {kind, ...view}; renderSubject(); }

// ── routing / 路由 ──
function parse(hash) {
  const parts = String(hash || "").replace(/^#\/?/, "").split("/").filter(Boolean).map(p => { try { return decodeURIComponent(p); } catch { return p; } });
  const ws = WORKSPACES.includes(parts[0]) ? parts[0] : "overview";
  if (ws === "business") return {ws, kind: ["finding", "order"].includes(parts[1]) ? parts[1] : null, id: parts[2] || null};
  return {ws, kind: null, id: parts[1] || null};
}
// A workspace appears when the service offers it, on the same conditions as before D068. / 服务提供时才出现工作区，条件与 D068 之前相同。
function offered(ws) {
  if (ws === "overview" || ws === "missions") return true;
  const p = project();
  if (!p) return false;
  if (ws === "fleet") return true;
  if (ws === "workflows") return !!workflows;
  if (ws === "tasks") return !!p.scheduling;
  if (ws === "audit") return true;
  return ws === "business" && !!p.business;
}
function go(hash) {
  if (location.hash !== "#" + hash) location.hash = hash;
  route();
}
function route(force = false) {
  const next = parse(location.hash);
  const changed = force || next.ws !== nav.ws || next.id !== nav.id || next.kind !== nav.kind;
  nav = next;
  if (hello && !offered(nav.ws) && nav.ws !== "workflows") { go("overview"); return; }
  if (changed) {
    if (nav.ws === "missions" && current?.mission.mission_id !== nav.id) selected = null;
    watch();
    if (typeof scrollTo === "function" && nav.id !== "new") scrollTo(0, 0);
  }
  renderAll();
  if (nav.ws === "missions" && nav.id === "new") byId("text").focus?.();
  // On a single column the detail sits below the list; bring it into view. / 单栏时详情在列表下方，滚动到可见。
  if (changed && nav.id && nav.id !== "new" && typeof matchMedia === "function" && matchMedia("(max-width: 980px)").matches) {
    byId("ws-" + nav.ws)?.querySelector?.(".pane-detail")?.scrollIntoView?.({block: "start"});
  }
}
// Subscribe to the object in the address; the service checks every read. / 订阅地址中的对象；每次读取都由服务检查。
function watch() {
  if (!hello) return;
  const p = project();
  if (nav.ws === "missions" && nav.id && nav.id !== "new") send({type: "watch", mission_id: nav.id}, true);
  if (p && nav.ws === "audit") send({type: "audit_watch", project_id: p.project_id, before: auditBefore}, true);
  if (!p || !nav.id) return;
  if (nav.ws === "workflows") send({type: "workflow_watch", project_id: p.project_id, run_id: nav.id}, true);
  if (nav.ws === "tasks") send({type: "task_watch", project_id: p.project_id, task_id: nav.id}, true);
  if (nav.ws === "fleet") send({type: "resource", project_id: p.project_id, resource_id: nav.id}, true);
  if (nav.ws === "business" && nav.kind) send({type: nav.kind + "_watch", project_id: p.project_id, [nav.kind + "_id"]: nav.id}, true);
}

// ── shell and overview / 外壳与总览 ──
function renderAll() {
  renderShell(); renderHost(); renderOverview(); renderMissionList(); renderMission(); renderWorkflows(); renderRun();
  renderTasks(); renderTask(); renderResources(); renderResourceDetail(); renderBusiness(); renderSubject(); renderAudit();
}

function scoped(list) {
  const p = project();
  if (!p || byId("missionScope").value === "all") return list;
  return list.filter(m => !m.project_id || m.project_id === p.project_id);
}
function bucket(status) { return PENDING.includes(status) ? "pending" : ACTIVE.includes(status) ? "active" : "done"; }
function allDocks() { return (resources?.sites || []).flatMap(s => s.docks); }
function allRobots() { return (resources?.sites || []).flatMap(s => s.robots); }

function counts() {
  const list = scoped(missions);
  return {
    missions: list.filter(m => PENDING.includes(m.status)).length,
    workflows: (workflows?.runs || []).filter(r => (r.waiting || []).includes("review")).length,
    tasks: (tasks?.tasks || []).filter(t => t.state === "queued").length,
    business: (business?.findings || []).filter(f => f.state === "candidate").length,
  };
}

function renderShell() {
  const total = attention().filter(i => !i.hint).length, c = counts();
  for (const ws of WORKSPACES) {
    const link = byId(NAV[ws]);
    link.hidden = !offered(ws);
    link.setAttribute("aria-current", nav.ws === ws ? "page" : "false");
    byId("ws-" + ws).hidden = nav.ws !== ws;
  }
  const badges = {countOverview: total, countMissions: c.missions, countWorkflows: c.workflows, countTasks: 0,
    countBusiness: c.business};
  for (const [id, value] of Object.entries(badges)) { const el = byId(id); el.hidden = !value; el.textContent = value ? String(value) : ""; }
  const robots = resources?.project_id === projectId ? allRobots() : [];
  paint("environment", environmentTag(robots, project()?.robots || []));
  document.title = (c.missions ? `(${c.missions}) ` : "") + "drone-agent · 云端飞行台";
}

function renderHost() {
  const pill = byId("hostPill");
  pill.hidden = !host;
  if (!host) return;
  pill.className = "pill " + (HOST_TONE[host.state] || "");
  pill.title = HOST[host.state] || host.state;
  byId("hostText").textContent = "云端飞行 · " + (HOST_SHORT[host.state] || host.state);
}

function kpi(href, label, value, unit, sub, tone) {
  return `<a class="kpi ${tone || ""}" href="${esc(href)}"><small>${esc(label)}</small><strong>${esc(value)}${unit ? `<em>${esc(unit)}</em>` : ""}</strong><span>${esc(sub)}</span></a>`;
}

function attention() {
  const out = [], p = project();
  const need = role => p && !can(role, p.roles) ? " · 需 " + role : "";
  for (const m of scoped(missions).filter(m => PENDING.includes(m.status))) {
    out.push({tone: "warn", icon: "审", title: "待审批 · " + (m.text || m.mission_id), meta: `${m.mission_id} · v${m.current_version} · ${ago(m.updated_at)}`,
      href: "missions/" + m.mission_id});
  }
  for (const r of workflows?.runs || []) {
    if ((r.waiting || []).includes("review")) out.push({tone: "warn", icon: "核", title: `等待人工复核 · ${r.workflow_id} v${r.version}`, meta: r.run_id + need("reviewer"), href: "workflows/" + r.run_id});
  }
  for (const f of business?.findings || []) {
    if (f.state === "candidate") out.push({tone: "warn", icon: "疑", title: "候选发现待复核 · " + f.asset_key, meta: `${f.finding_id} · ${f.jobs} 次分析` + need("reviewer"), href: "business/finding/" + f.finding_id});
  }
  for (const o of business?.orders || []) {
    if (REPAIRABLE.includes(o.state)) out.push({tone: o.state === "open" ? "info" : "bad", icon: "修", title: "待维修反馈 · " + o.asset_key, meta: `${o.order_id} · 第 ${o.round} 轮 · ${ORDER[o.state] || o.state}` + need("operator"), href: "business/order/" + o.order_id});
  }
  for (const o of workflows?.orders || []) {
    if (o.state === "open") out.push({tone: "info", icon: "修", title: "模拟工单待维修反馈 · " + o.asset_id, meta: o.order_id + need("operator"), href: "workflows"});
  }
  for (const d of allDocks()) {
    if (d.status?.lock) out.push({tone: "warn", icon: "锁", title: "机场维护锁定 · " + d.dock_id, meta: DIM[d.status.lock.state] || d.status.lock.state, href: "fleet/" + d.dock_id});
  }
  // Robots that cannot be dispatched are a hint, not an action: a pending approval's soft hold alone blocks one.
  // 不可派遣的机器人是提示而非待办：仅一个待审批任务的软预约就会让它不可派遣。
  for (const r of allRobots()) {
    if (r.eligibility.verdict !== "eligible") out.push({hint: true, tone: "warn", icon: "机", title: `${VERDICT[r.eligibility.verdict] || r.eligibility.verdict} · ${r.robot_id}`, meta: reasons(r.eligibility.reasons) || "—", href: "fleet/" + r.robot_id});
  }
  return out;
}

function missionItem(m) {
  const meta = [m.mission_id, "v" + m.current_version, stamp(m.updated_at), CHANNEL[m.channel] || m.channel, m.robot_id].filter(Boolean).join(" · ");
  return `<a class="item" href="#missions/${esc(m.mission_id)}" aria-current="${m.mission_id === nav.id && nav.ws === "missions"}"><span class="item-main">
    <span class="item-title">${esc(m.text || m.mission_id)}</span><span class="item-meta">${esc(meta)}</span></span>${chip(m.status)}</a>`;
}

function renderOverview() {
  const p = project(), c = counts(), list = scoped(missions);
  byId("ovSub").textContent = !hello ? "连接任务服务中…" : p ? `${zh(p.name) || p.project_id} · 我的角色 ${p.roles.join(" / ") || "无"}`
    : "本入口没有运营目录：提供自然语言任务的规划、审批与飞行。";
  const tiles = [kpi("#missions", "待审批任务", c.missions, "个", `进行中 ${list.filter(m => ACTIVE.includes(m.status)).length} · 最近 ${list.length} 个任务`, c.missions ? "warn" : "")];
  if (host) tiles.push(kpi(host.mission_id ? "#missions/" + host.mission_id : "#missions", "云端飞行", HOST_SHORT[host.state] || host.state, "", host.mission_id ? `${host.mission_id} v${host.version}` + ((host.queue || []).length ? ` · 排队 ${host.queue.length}` : "") : HOST[host.state] || ""));
  if (resources) {
    const robots = allRobots(), docks = allDocks(), ok = robots.filter(r => r.eligibility.verdict === "eligible").length;
    tiles.push(kpi("#fleet", "可派遣机器人", ok, `/ ${robots.length}`, `机场在线 ${docks.filter(d => d.status?.link === "online" && d.status?.fresh).length} / ${docks.length}`));
  }
  if (workflows) {
    const live = workflows.runs.filter(r => !FINAL.includes(r.state)).length;
    tiles.push(kpi("#workflows", "进行中的运行", live, "个", `等待复核 ${c.workflows} · 模板 ${workflows.templates.length}`));
  }
  if (tasks) tiles.push(kpi("#tasks", "排队任务单", c.tasks, "个", `机器人 ${tasks.robots.length} · 空域持有 ${tasks.airspace.holds.length}`));
  if (business) {
    const columns = business.report.columns;
    tiles.push(kpi("#business", "候选发现", c.business, "个", `关单 ${columns.closed.length} · 未关 ${columns.open.length} · 不确定 ${columns.unknown.length}`, c.business ? "warn" : ""));
  }
  paint("ovKpis", tiles.join(""));
  const items = attention(), todo = items.filter(i => !i.hint), hints = items.filter(i => i.hint);
  const entry = (i, go) => `<li><a href="#${esc(i.href)}"><span class="ico ${i.tone}">${esc(i.icon)}</span>
    <span><b>${esc(i.title)}</b><small>${esc(i.meta)}</small></span><span class="go">${go}</span></a></li>`;
  byId("ovAttnSub").textContent = todo.length ? todo.length + " 项" : "";
  paint("ovAttention", todo.length ? `<ul class="attn">${todo.map(i => entry(i, "处理 →")).join("")}</ul>` : '<div class="empty">暂无待处理事项。</div>');
  paint("ovHints", hints.length ? `<ul class="attn">${hints.map(i => entry(i, "查看 →")).join("")}</ul>` : '<div class="empty">暂无资源提示。</div>');
  paint("ovMissions", list.slice(0, 7).map(missionItem).join("") || '<div class="empty">暂无任务。</div>');
  byId("ovFleetCard").hidden = !resources;
  if (resources) paint("ovFleet", resources.sites.map(site => `<div class="site"><div class="site-head">${esc(site.site_id)}</div>
    ${site.docks.map(d => `<div class="fleet-row"><a href="#fleet/${esc(d.dock_id)}">${esc(d.dock_id)}</a>${d.status ? [tag("舱盖 " + (SHORT[d.status.lid] || d.status.lid)),
      tag("补能 " + (SHORT[d.status.energy.state] || d.status.energy.state) + (d.status.energy.charge_fraction == null ? "" : " " + pct(d.status.energy.charge_fraction))),
      tag("环境 " + (SHORT[d.status.environment.state] || d.status.environment.state)), tag(PAD[d.pad] || d.pad), d.status.lock ? badge("维护锁定", "bad") : ""].join("") : ""}<span class="push"></span>
      ${d.status ? badge(d.status.link === "online" && d.status.fresh ? "在线" : SHORT[d.status.link] || d.status.link, d.status.link === "online" && d.status.fresh ? "ok" : "bad") : badge("无状态", "bad")}</div>`).join("")}
    ${site.robots.map(r => `<div class="fleet-row"><a href="#fleet/${esc(r.robot_id)}">${esc(r.robot_id)}</a>${sourceChip(r.execution_backend)}${tag(robotLine(r))}<span class="push"></span>
      ${badge(VERDICT[r.eligibility.verdict] || r.eligibility.verdict, VTONE[r.eligibility.verdict])}${r.eligibility.reasons.length ? `<span class="why">${esc(reasons(r.eligibility.reasons))}</span>` : ""}</div>`).join("")}</div>`).join(""));
  renderShell();
}

// ── missions / 任务 ──
function renderMissionList() {
  const list = scoped(missions);
  const total = {all: list.length, pending: 0, active: 0, done: 0};
  for (const m of list) total[bucket(m.status)] += 1;
  paint("missionFilters", [["all", "全部"], ["pending", "待审批"], ["active", "进行中"], ["done", "已结束"]].map(([key, label]) =>
    `<button type="button" data-act="filter" data-v="${key}" aria-pressed="${filter === key}">${label}<b>${total[key]}</b></button>`).join(""));
  const shown = list.filter(m => filter === "all" || bucket(m.status) === filter);
  paint("missionList", shown.map(missionItem).join("") || `<div class="empty">${list.length ? "没有符合筛选的任务。" : "暂无任务。"}</div>`);
}

function version() {
  const versions = current.versions;
  return versions.find(v => v.version === selected) || versions[versions.length - 1];
}

function stepper(status) {
  const [at, state] = STAGE[status] || [0, "now"];
  return `<ol class="stepper" aria-label="任务生命周期">${STAGES.map((label, i) =>
    `<li class="${i < at ? "done" : i === at ? state : ""}"${i === at ? ' aria-current="step"' : ""}>${esc(label)}</li>`).join("")}</ol>`;
}

function renderMission() {
  const open = nav.ws === "missions" && !!nav.id && nav.id !== "new";
  byId("missionBlank").hidden = open;
  byId("detail").hidden = !open;
  if (!open) return;
  if (!current || current.mission.mission_id !== nav.id) { paint("detail", blank("加载任务 " + nav.id + " …", "任务不存在或无权查看时，这里会一直为空。")); return; }
  paint("detail", detailBack("missions", "任务列表") + missionHtml(current));
  loadMedia(current);
}

function requester(request) {
  const who = String(request.requested_by || "");
  if (who.startsWith("workflow:")) return link("workflows/" + who.slice(9), who);
  return who;
}

function missionHtml(view) {
  const m = view.mission, request = view.request, v = version(), binding = view.binding;
  const scope = binding && !binding.legacy ? ` · ${binding.project_id} / ${binding.robot_id}` : binding?.legacy ? " · 历史任务（只读）" : "";
  let html = `<header class="d-head"><div class="eyebrow">任务 ${esc(m.mission_id)} · ${esc(CHANNEL[request.channel] || request.channel)} · ${show(requester(request))}${esc(scope)}</div>
    <div class="title-row"><h1>${esc(v?.spec?.goal || request.text)}</h1>${missionCancel(view)}</div><blockquote class="lead">原始请求：${esc(request.text)}</blockquote>
    <div class="badges">${chip(m.status)}${tag("v" + m.current_version)}${sourceChip(binding?.execution_backend || v?.provenance?.execution_backend)}${m.replans ? badge("重规划 " + m.replans, "info") : ""}${view.task?.task_id ? " " + show(link("tasks/" + view.task.task_id, "任务单 " + view.task.task_id)) : ""}</div></header>`;
  html += stepper(m.status);
  if (!v) return html + '<p class="hint">等待规划结果。</p>';
  html += `<div class="tabs" role="tablist" aria-label="任务版本">${view.versions.map(x => `<button class="tab" type="button" role="tab" aria-selected="${x.version === v.version}" data-act="version" data-v="${x.version}">v${x.version} · ${esc(STATUS[x.status] || x.status)} · ${esc(x.origin)}</button>`).join("")}</div>`;
  html += missionActions(view, v);
  // Main evidence and package, supporting facts on the right; completed outcomes lead the main column.
  // 主栏展示证据与任务包，右栏展示支持事实；已结束任务的结果在主栏前置。
  const plan = `<div class="stack plan">${packageCard(v)}</div>`;
  const hasEvidence = !!(view.report || view.evidence?.length || v.events?.length || view.cloud);
  const outcome = `<div class="stack outcome">${hasEvidence ? [reportCard(view), evidenceCard(view), eventsCard(v), cloudCard(view)].join("")
    : card("飞行之后", '<p class="hint">飞行开始后展示机载事件；证据同步后展示图像、执行与效果记录及报告。</p>')}</div>`;
  const produced = bucket(view.mission.status) === "done" && !!(view.report || (view.evidence || []).length || view.cloud?.judge);
  return html + `<div class="d-grid"><div class="stack">${produced ? outcome + plan : plan + outcome}${operationsCard(view)}${view.issues?.length ? issuesCard(view) : ""}</div>
    <aside class="stack facts">${[planningCard(v), approvalCard(v), dispatchCard(view), provenanceCard(v), vendorCard(view, v)].join("")}</aside></div>`;
}

function missionCancel(view) {
  const m = view.mission, dispatch = view.dispatch;
  const cancellable = dispatch && ["awaiting_approval", "approving", "approved", "queued", "delivered"].includes(m.status) && !dispatch.cancel && !view.live;
  return cancellable ? `<button class="btn ghost small" type="button" id="cancelMission" data-act="cancelMission" data-once data-id="${esc(m.mission_id)}" ${canMission("operator", view) ? "" : "disabled"}>取消任务</button>` : "";
}

function missionActions(view, v) {
  const m = view.mission;
  let html = "";
  if (v.status === "awaiting_approval" && v.version === m.current_version) {
    const allowed = canMission("approver", view);
    html += `<section class="decision warn" aria-label="待审批"><h3>待审批 · v${esc(v.version)}</h3>
      <p>审批绑定下面这个任务包哈希；审批后任何改动都会使签名失效，机载拒收。</p>${hashBlock(v.package_hash)}
      <div class="buttons"><button class="btn primary" type="button" id="approve" data-act="approve" data-once data-id="${esc(m.mission_id)}" data-v="${esc(v.version)}" data-hash="${esc(v.package_hash)}" ${allowed ? "" : "disabled"}>批准并签名</button>
      <button class="btn danger" type="button" id="decline" data-act="decline" data-id="${esc(m.mission_id)}" data-v="${esc(v.version)}" ${allowed ? "" : "disabled"}>驳回</button></div>
      ${allowed ? "" : '<p class="hint">审批需要该任务所属项目的 approver 角色。</p>'}</section>`;
  }
  if (view.live) {
    const allowed = view.live.allowed_actions, writable = canMission("operator", view);
    html += `<section class="callout info" aria-label="进行中"><h3>进行中 · ${esc(view.live.step_id)} · ${esc(view.live.state)}</h3>
      <div class="buttons">${["pause", "resume", "cancel"].map(a => `<button class="btn ${a === "cancel" ? "danger" : ""}" type="button" data-act="op" data-once data-id="${esc(m.mission_id)}" data-op="${a}" ${writable && allowed.includes(a) ? "" : "disabled"}>${{pause: "暂停", resume: "恢复", cancel: "取消并安全收尾"}[a]}</button>`).join("")}</div>
      <p>操作经机载 uplink 写入操作者信箱，由 executive 与 guardian 复核绑定与时效；页面按钮不直达飞控。</p></section>`;
  }
  return html;
}

function packageCard(v) {
  if (!v.package) return "";
  const nodes = `<ol class="package-list">${v.package.nodes.map(n => `<li><div class="package-title"><b>${esc(n.task_id)}</b><code>${esc(n.skill_id)}</code></div>
    <p>依赖 ${esc(n.depends_on.join(", ") || "—")}</p><div class="param-tags">${Object.entries(n.params || {}).map(([key, value]) => `<span><b>${esc(key)}</b> ${esc(typeof value === "object" ? JSON.stringify(value) : value)}</span>`).join("") || "—"}</div></li>`).join("")}</ol>`;
  const diff = v.diff && v.version > 1 ? `<p class="hint">相对上一版本：新增 ${esc(v.diff.added.join(", ") || "—")}；删除 ${esc(v.diff.removed.join(", ") || "—")}；改动 ${esc(v.diff.changed.join(", ") || "—")}</p>` : "";
  return card("任务包", nodes + diff, raw(`哈希 ${tag((v.package_hash || "").slice(0, 12))}`));
}

function planningCard(v) {
  let body = "";
  if (v.planner) body += `<h4 class="label">规划</h4>` + kv([["模型", `${v.planner.provider_id} / ${v.planner.model_id}`], ["提示版本", v.planner.prompt_version],
    ["输入哈希", short(v.planner.input_hash, 16)], ["尝试 / 通道", `${v.planner.attempts} · ${(v.planner.channels || []).join(",")}`],
    v.planner.decline_reason ? ["拒答原因", v.planner.decline_reason] : null]);
  if (v.admission) {
    const checks = (v.admission.checks || []).map(c => badge(c.check, c.passed ? "ok" : "bad")).join(" ");
    body += `<h4 class="label">准入</h4>` + kv([["结果", raw(badge(v.admission.accepted ? "通过" : "拒绝", v.admission.accepted ? "ok" : "bad"))],
      ["能耗上界", v.admission.energy_upper_fraction == null ? "—" : pct(v.admission.energy_upper_fraction)],
      v.admission.codes.length ? ["问题码", v.admission.codes.join(", ")] : null]) + (checks ? `<div class="badges" style="margin-top:10px">${checks}</div>` : "");
  }
  return body ? card("规划与准入", body) : "";
}

function approvalCard(v) {
  if (!v.approval) return "";
  return card("审批", kv([["审批人", v.approval.approver], ["时间", when(v.approval.approved_at)], ["签名密钥", v.approval.signer_key_id],
    ["有效至", when(v.approval.expires_at)]]) + hashBlock(v.package_hash), "按任务包哈希签名");
}

function dispatchCard(view) {
  const binding = view.binding, dispatch = view.dispatch;
  if (!binding) return "";
  let body = kv([["项目 / 站点", binding.legacy ? binding.project_id + " · 历史任务（只读）" : `${binding.project_id} / ${binding.site_id}`],
    binding.legacy ? null : ["机场 / 机器人", `${binding.dock_id} / ${binding.robot_id}`],
    binding.legacy ? null : ["执行后端", raw(sourceChip(binding.execution_backend))]]);
  if (dispatch) {
    const latest = {};
    for (const d of dispatch.decisions) latest[d.version + ":" + d.stage] = d;
    body += table(["版本", "阶段", "判定", "原因"], Object.values(latest).map(d => ["v" + d.version, {preview: "预览", prepare: "准备", claim: "领取"}[d.stage] || d.stage,
      raw(badge(VERDICT[d.verdict] || d.verdict, VTONE[d.verdict])), reasons(d.reasons) || "—"]));
    body += table(["预约", "状态", "原因"], dispatch.reservations.map(r => ["v" + r.mission_version, RESERVATION[r.state] || r.state, RELEASE[r.reason] || r.reason]));
    if (dispatch.claims.length) body += `<p class="hint">交付：${dispatch.claims.map(c => `v${esc(c.mission_version)} ${c.state === "claimed" ? "已领取" : "已作废（" + esc(c.reason) + "）"}`).join("；")}</p>`;
    if (dispatch.cancel) body += `<p class="hint">取消意图：${esc(dispatch.cancel.requested_by)} · ${esc(stamp(dispatch.cancel.requested_at))}${dispatch.cancel.relayed_request_id ? " · 已转发到飞行" : ""}</p>`;
  }
  return card("派遣（P1）", body, "可派遣是有时效的判断，不是授权");
}

function cloudCard(view) {
  const cloud = view.cloud;
  if (!cloud) return host ? card("云端飞行", '<p class="hint">批准后由云端仿真飞行：每个版本前飞控断电重启（地勤换电），与其他仿真和部署互斥排队。任务到达终态后，监管者用仿真真值运行独立裁判（在线与回放）。</p>') : "";
  const flights = table(["版本", "状态", "代次", "开始 / 结束", "说明"], (cloud.flights || []).map(f => ["v" + f.version,
    (FLIGHT[f.status] || f.status) + (f.manual_cleanup ? " · 仿真操作员降落" : ""), f.epoch ?? "—", `${stamp(f.started_at)} / ${stamp(f.ended_at)}`, f.detail || ""]), "尚无飞行记录。");
  const judge = cloud.judge;
  const verdict = judge ? `<h4 class="label" style="margin-top:14px">独立裁判</h4><div class="verdict ${judge.passed ? "ok" : "bad"}"><b>${esc(JUDGE[judge.classification] || judge.classification)}</b>${badge(judge.passed ? "通过" : "未通过", judge.passed ? "ok" : "bad")}</div>
    ${kv([["错误成功报告", judge.false_success_reports], ["在线 / 回放", judge.replay_agrees ? "一致" : "不一致"], ["飞行版本", (judge.flown_versions || []).join(", ") || "—"],
      (judge.problems || []).length ? ["问题", judge.problems.join(", ")] : null])}<p class="hint">裁判用仿真真值独立核对飞行与报告；页面只展示它的记录。</p>`
    : '<p class="hint">任务到达终态后，监管者用仿真真值运行独立裁判（在线与回放）。</p>';
  return card("云端飞行与独立裁判", flights + verdict);
}

// P5 vendor-managed dock (D072): what the platform can see of a vendor task, and what it cannot.
// P5 厂商托管机场（D072）：平台对厂商任务能看到的内容，以及看不到的部分。
function vendorCard(view, v) {
  const vendor = view.vendor;
  if (!vendor) return "";
  const entry = (vendor.versions || []).find(x => x.version === v.version) || {}, s = entry.summary || {};
  const commands = Object.entries(s.commands || {}).map(([method, c]) => [raw(`<code>${esc(method)}</code>`), c.attempts,
    c.reply === null || c.reply === undefined ? "未答复" : c.reply === 0 ? "已受理（不代表完成）" : "拒绝 " + c.reply]);
  const media = Object.values(s.media || {}).map(m => (m.accepted ? "已入账 " : "拒收 ") + (m.evidence_id || m.reason || ""));
  const terminal = s.terminal ? (VENDOR_STATUS[s.terminal.status] || s.terminal.status) + (s.terminal.physical ? "" : "（没有起飞）") : "尚无终态";
  return card("厂商任务", kv([["协议档案", `${vendor.profile.profile_id} · ${vendor.profile.protocol}`],
    ["固件核对", "未对照任何厂商固件核对（协议模拟）"], ["安全判定", "厂商本地系统负责，平台不可见"],
    ["飞行 ID", entry.flight_id || "—"], ["任务摘要", short(entry.task_sha256, 16)], ["进度", (VENDOR_STATUS[s.status] || s.status || "—") + (s.reached ? " · 第 " + s.reached + " 步" : "")],
    ["终态", terminal], ["回到机场", s.grounded_at ? when(s.grounded_at) : "未确认"], ["媒体", media.join("；") || (s.media_missing ? "宽限期内未收到" : "—")],
    ["链路", s.link === "down" ? "断开" : "正常"]]) + (commands.length ? table(["命令", "发送次数", "答复"], commands) : ""),
    "ACK 只是受理；完成以终态与服务复核过的媒体为准");
}

function eventsCard(v) {
  const events = v.events || [];
  const journals = Object.entries(v.journals || {}).map(([k, j]) => tag(`${k} ${j.rows} · ${j.chain}`)).join(" ") || tag("尚未同步");
  const body = events.length ? `<ol class="timeline" data-k="events-${esc(current.mission.mission_id)}-${esc(v.version)}">${events.slice().reverse().map(e => `<li class="${ALERTS.includes(e.kind) ? "alert" : ""}">${show(when(e.timestamp))}
    <span>${eventName(e.kind)} <small>${esc(e.journal)} · ${esc(e.data.step_id || e.data.state || e.data.method || "")} ${esc(e.data.status || "")} ${e.data.outcome ? esc(e.data.outcome.execution_status + " / " + e.data.outcome.effect_verdict) : ""}</small></span></li>`).join("")}</ol>`
    : '<div class="empty">飞行开始后显示来自 executive 与 guardian 账本的事件。</div>';
  return card("机载事件", body, raw(journals));
}

function operationsCard(view) {
  if (!view.operations.length) return "";
  return card("操作请求", table(["动作", "请求人", "状态"], view.operations.map(o => [o.action, o.requested_by,
    o.acked ? (o.accepted ? "已转入信箱" : "被拒：" + o.reason) : "待拉取"])));
}

function reportCard(view) {
  const report = view.report;
  if (!report) return card("报告", '<div class="empty">飞行结束后生成三列报告：已完成只接受「执行成功 ∧ 效果已证实」。</div>');
  const targets = Object.entries(report.targets || {});
  // While the mission is still going the report is the service's running record, not the final one.
  // 任务未结束时，报告是服务的当前记录，不是最终报告。
  const running = bucket(view.mission.status) !== "done" ? '<p class="hint" style="margin-top:0">任务尚未结束：以下为当前记录，随飞行与证据复核更新。</p>' : "";
  const columns = running + `<div class="report-cols">${Object.keys(COLUMN).map(c => `<div class="rc ${c}${report.summary?.[c] ? "" : " zero"}"><small>${COLUMN[c]}</small><b>${esc(report.summary?.[c] ?? 0)}</b>
    <ul>${targets.filter(([, col]) => col === c).map(([t]) => `<li>${esc(t)}</li>`).join("")}</ul></div>`).join("")}</div>`;
  const rows = table(["步骤", "列", "判定"], (report.rows || []).map(r => [`v${r.mission_version} ${r.step_id}`, COLUMN[r.column] || r.column,
    raw(`<code>${esc(r.execution_status || "—")} / ${esc(r.effect_verdict || "—")}${r.service_verdict ? " · 服务 " + esc(r.service_verdict) : ""}</code>`)]));
  const facts = (report.facts || []).length ? `<p class="hint">模型备注（不改变任何判定）：${report.facts.map(f => esc(f.predicate + "=" + JSON.stringify(f.value) + " @" + f.confidence)).join("；")}</p>` : "";
  const verdicts = (key, names, tones) => (report.rows || []).map(r => `<li><span class="mono">v${esc(r.mission_version)} · ${esc(r.step_id)}</span>${badge(names[r[key]] || r[key] || "服务未提供", tones[r[key]] || (!r[key] ? "unknown" : ""))}</li>`).join("") || `<li>${badge("服务未提供", "unknown")}</li>`;
  const triad = `<div class="triad" aria-label="执行、效果与安全记录"><section><h4>执行 · 逐步骤</h4><ul>${verdicts("execution_status", EXECUTION, EXECUTION_TONE)}</ul></section>
    <section><h4>效果 · 逐步骤</h4><ul>${verdicts("effect_verdict", EFFECT, EFFECT_TONE)}</ul></section><section class="unknown"><h4>安全 · 任务级</h4>${badge("服务未提供", "unknown")}<p class="hint">不从机载事件汇总安全结论。</p></section></div>`;
  return card("报告", triad + columns + rows + facts, "已完成 = 执行成功 ∧ 效果已证实");
}

function evidenceCard(view) {
  const items = view.evidence || [];
  // P4: a verified capture of this project can become a reference (admin) or be analysed again (reuse-v1).
  // P4：本项目已证实的采集可登记为参考外观（管理员）或再次分析（reuse-v1）。
  const project = view.binding?.project_id, reuse = business && business.project_id === project ? business.reuse.analyzers : null;
  const body = items.length ? `<div class="gallery">${items.map(e => {
    const png = photos[e.evidence_id], verdict = e.verification?.final_verdict;
    const state = e.verification ? badge(verdict + (e.verification.agrees ? "" : " · 机载/服务不一致"), EFFECT_TONE[verdict] === "unknown" ? "unknown" : verdict === "verified" && e.verification.agrees ? "ok" : "warn") : badge("待复核", "warn");
    const tools = reuse && verdict === "verified" ? `<div class="tools"><select data-analyzer="${esc(e.evidence_id)}" data-k="reuse-${esc(e.evidence_id)}" aria-label="复用分析器">${reuse.map(a => `<option value="${esc(a)}">${esc(a)}</option>`).join("")}</select>
      <button class="btn small" type="button" data-act="reuse" data-id="${esc(e.evidence_id)}" data-mission="${esc(view.mission.mission_id)}" ${can("operator", business.roles) ? "" : "disabled"}>复用分析</button>
      <button class="btn small" type="button" data-act="reference" data-id="${esc(e.evidence_id)}" data-mission="${esc(view.mission.mission_id)}" ${can("admin", business.roles) ? "" : "disabled"}>登记为参考外观</button></div>` : "";
    const frame = png ? `<img alt="机载相机证据 ${esc(e.step_id)}" src="${esc(png)}">` : e.media === true ? "加载图像…"
      : `<button class="btn small" type="button" data-act="media" data-id="${esc(e.evidence_id)}" data-mission="${esc(view.mission.mission_id)}">加载图像</button>`;
    return `<figure class="shot" style="margin:0"><div class="frame">${frame}</div><figcaption class="cap"><span class="badges">${state}</span>
      <span class="item-meta">v${esc(e.version)} ${esc(e.step_id)} · ${esc(stamp(e.captured_at))}</span>${sourceChip(e.provenance?.source)}${tools}</figcaption></figure>`;
  }).join("")}</div>` : '<div class="empty">暂无证据。</div>';
  return card("证据", body, items.length ? items.length + " 份" : "");
}

function provenanceCard(v) {
  const provenance = v.provenance || {}, runInfo = provenance.run || {};
  return card("运行来源", kv([["执行后端", raw(sourceChip(provenance.execution_backend))],
    ["规划来源", raw(sourceChip(provenance.planning?.source))],
    ["分析来源", raw((provenance.analysis_sources || ["legacy_unknown"]).map(sourceChip).join(" "))],
    ["软件版本", runInfo.software_sha ? raw(`${show(short(runInfo.software_sha, 12))}${runInfo.dirty_sha256 ? " · 含未提交代码" : ""}`) : "未知"]]), "受信后端生成");
}

function issuesCard(view) {
  const issues = view.issues || [];
  return card("问题", issues.length ? table(["代码", "说明"], issues.map(i => [raw(`<code>${esc(i.code)}</code>`), i.message])) : '<div class="empty">暂无问题。</div>');
}

// Images are fetched once per evidence and only when the service says there is one. / 每份证据只取一次，且只在服务表明有图像时获取。
function requestMedia(missionId, evidenceId, force = false) {
  const key = missionId + "/" + evidenceId;
  if (photos[evidenceId] || (asked.has(key) && !force)) return;
  asked.add(key);
  send({type: "media", mission_id: missionId, evidence_id: evidenceId}, !force);
}
function loadMedia(view) {
  for (const e of view.evidence || []) if (e.media === true) requestMedia(view.mission.mission_id, e.evidence_id);
}

function submitMission() {
  const text = byId("text").value.trim();
  if (!text) { notice("请先写下任务目标。"); return; }
  notice("");
  const assets = [...(byId("assets").querySelectorAll?.("input:checked") || [])].map(i => i.value);
  const bound = !byId("robotRow").hidden && byId("robot").value ? {project_id: projectId, robot_id: byId("robot").value} : {};
  expect = {kind: "mission", known: new Set([...missions.map(m => m.mission_id), current?.mission.mission_id].filter(Boolean))};
  if (!send({type: "text", rid: rid(), text, volume_id: byId("volume").value, asset_ids: assets, ...bound})) { expect = null; return; }
  byId("submit").disabled = true;
  byId("submit").textContent = "规划中…（实调模型可能需要数十秒）";
  // Re-enable on the planned mission or an error; a live model can take a while. / 收到规划结果或错误时恢复按钮。
  planning = setTimeout(planned, 180000);
}

function planned() {
  if (planning) clearTimeout(planning);
  planning = null;
  byId("submit").disabled = !hello?.can_write;
  byId("submit").textContent = "规划并提交审批";
}

// ── workflows / 工作流 ──
function why(node) {
  if (node.state === "waiting") return WAIT[node.reason] || node.reason || "";
  if (node.state === "skipped" || node.state === "cancelled") return SKIP[node.reason] || node.reason || "";
  return node.reason || "";
}
function templateTitle(t) { return String(t.title || t.workflow_id); }

function renderWorkflows() {
  if (!workflows) {
    paint("wfStart", '<div class="empty">加载工作流模板中。</div>');
    renderShell();
    return;
  }
  const operator = can("operator", workflows.roles);
  byId("wfCatalog").textContent = workflows.catalog?.catalog_id || "";
  const manual = workflows.templates.filter(t => t.triggers.some(x => x.kind === "manual"));
  if (!manual.some(t => t.workflow_id === wfChoice)) wfChoice = manual[0]?.workflow_id || null;
  const chosen = manual.find(t => t.workflow_id === wfChoice);
  paint("wfStart", chosen ? `<div class="field-row"><label for="wfTemplate">模板</label><select id="wfTemplate">${manual.map(t =>
    `<option value="${esc(t.workflow_id)}"${t.workflow_id === wfChoice ? " selected" : ""}>${esc(t.workflow_id)} v${esc(t.version)}</option>`).join("")}</select></div>
    <p class="hint" title="${esc(templateTitle(chosen))}">${esc(zh(templateTitle(chosen)))}</p>
    ${Object.entries(chosen.inputs || {}).map(([name, spec]) => `<div class="field-row"><label>${esc(name === "asset" ? "资产" : name)}</label><select data-input="${esc(name)}" data-wf="${esc(chosen.workflow_id)}" data-k="wfin-${esc(chosen.workflow_id)}-${esc(name)}">${spec.choices.map(c =>
      `<option value="${esc(c)}">${esc(c)}</option>`).join("")}</select></div>`).join("")}
    <div class="buttons"><button class="btn primary block" type="button" data-act="start" data-once data-id="${esc(chosen.workflow_id)}" ${operator ? "" : "disabled"}>启动运行</button></div>`
    : '<div class="empty">本项目没有可手动启动的模板。</div>');
  const triggers = workflows.templates.flatMap(t => t.triggers.filter(x => x.kind === "schedule" || x.kind === "event").map(x => [t, x]));
  byId("wfSchedulesCard").hidden = !triggers.length;
  paint("wfSchedules", triggers.map(([t, x]) => {
    if (x.kind === "event") return `<div class="item"><span class="item-main"><span class="item-title">${esc(t.workflow_id)}</span><span class="why">事件触发：${esc(x.source)} · ${esc(x.event_type)}</span></span></div>`;
    const on = x.state?.state === "enabled";
    const every = x.schedule.every === "day" ? `每天 ${x.schedule.at} ${x.schedule.timezone}` : `每 ${x.schedule.minutes} 分钟`;
    return `<div class="item"><span class="item-main"><span class="item-title">${esc(t.workflow_id)}</span><span class="item-meta">排班 ${esc(every)} · ${on ? "已启用，下次 " + esc(stamp(x.state.cursor_at)) : "已停用"}</span></span>
      <button class="btn small" type="button" data-act="schedule" data-id="${esc(t.workflow_id)}" data-trigger="${esc(x.trigger_id)}" data-action="${on ? "disable" : "enable"}" ${operator ? "" : "disabled"}>${on ? "停用排班" : "启用排班"}</button></div>`;
  }).join(""));
  renderWorkflowRuns();
  byId("wfOrdersCard").hidden = !workflows.orders.length;
  paint("workflowOrders", workflows.orders.map(o => `<div class="item"><span class="item-main"><span class="item-title">${esc(o.order_id)} · ${esc(o.asset_id)}</span>
    <span class="item-meta">${esc(ORDER[o.state] || o.state)}</span>${o.reinspection_run ? `<a class="mono" href="#workflows/${esc(o.reinspection_run)}">复检运行 ${esc(o.reinspection_run)}</a>` : ""}</span>
    ${o.state === "open" ? `<button class="btn small" type="button" data-act="wfRepair" data-id="${esc(o.order_id)}" ${operator ? "" : "disabled"}>记录维修反馈</button>` : ""}</div>`).join(""));
  byId("draftButton").disabled = !operator;
  paint("workflowTemplates", workflows.templates.map(t => `<div class="res-card"><div class="res-head"><b>${esc(t.workflow_id)} v${esc(t.version)}</b>
    <span class="badges">${t.triggers.map(x => badge(TRIGGER[x.kind] || x.kind, x.kind === "manual" ? "info" : "")).join("")}</span></div>
    <div class="flow-note" title="${esc(templateTitle(t))}">${esc(zh(templateTitle(t)))}</div>
    <div class="chain">${(t.nodes || []).map(n => `<span title="${esc(n.node_id)}">${esc(ACTIVITY[n.activity] || n.activity)}</span>`).join("<i>→</i>")}</div></div>`).join("")
    || '<div class="empty">本项目没有工作流模板。</div>');
  renderShell();
}

function renderWorkflowRuns() {
  if (!workflows) return;
  byId("wfRunsSub").textContent = workflows.runs.length ? workflows.runs.length + " 个" : "";
  paint("workflowRuns", workflows.runs.map(r => `<a class="item" href="#workflows/${esc(r.run_id)}" aria-current="${r.run_id === nav.id && nav.ws === "workflows"}"><span class="item-main">
    <span class="item-title">${esc(r.workflow_id)} v${esc(r.version)}</span><span class="item-meta">${esc(r.run_id)} · ${esc(stamp(r.updated_at))}</span>
    ${r.waiting.length ? `<span class="why">${esc(r.waiting.map(w => WAIT[w] || w).join(" / "))}</span>` : ""}</span>${wchip(r.state)}</a>`).join("")
    || '<div class="empty">暂无运行。</div>');
}

function renderRun() {
  const open = nav.ws === "workflows" && !!nav.id;
  byId("runOverview").hidden = open;
  byId("runDetail").hidden = !open;
  if (!open) return;
  if (!run || run.run.run_id !== nav.id) { paint("runDetail", blank("加载运行 " + nav.id + " …")); return; }
  const r = run.run, final = FINAL.includes(r.state), operator = can("operator", workflows?.roles), reviewer = can("reviewer", workflows?.roles);
  let html = `<header class="d-head"><div class="eyebrow">工作流运行 ${esc(r.run_id)} · ${esc(r.trigger_source)} · 发起 ${esc(r.started_by)}</div>
    <div class="title-row"><h1 title="${esc(run.template.title)}">${esc(zh(run.template.title))}</h1>${!final && !r.cancel ? `<button class="btn ghost small" type="button" id="cancelRun" data-act="cancelRun" data-id="${esc(r.run_id)}" ${operator ? "" : "disabled"}>取消本次运行</button>` : ""}</div><div class="badges">${wchip(r.state)}${tag(r.workflow_id + " v" + r.version)}
    ${run.waiting.length ? badge(run.waiting.map(w => WAIT[w] || w).join(" / "), "warn") : ""}</div></header>`;
  if (r.cancel) html += `<section class="callout warn"><h3>已请求取消</h3><p>取消：${esc(r.cancel.requested_by)} · ${esc(stamp(r.cancel.requested_at))} · ${esc(r.cancel.reason)}；已开始的飞行经原通道收尾，对账前显示“取消收尾中”。</p></section>`;
  if (!r.cancel) html += run.nodes.filter(n => n.activity === "human_review" && n.state === "waiting").map(n => `<section class="decision warn"><h3>等待人工复核 · ${esc(n.node_id)}</h3><p>分析结果只是候选，请结合本次证据确认或驳回。</p>
    <div class="buttons"><button class="btn primary" type="button" data-act="review" data-id="${esc(r.run_id)}" data-node="${esc(n.node_id)}" data-decision="confirmed" ${reviewer ? "" : "disabled"}>确认异常</button>
    <button class="btn danger" type="button" data-act="review" data-id="${esc(r.run_id)}" data-node="${esc(n.node_id)}" data-decision="dismissed" ${reviewer ? "" : "disabled"}>驳回</button></div></section>`).join("");
  const nodes = `<ol class="flow">${run.nodes.map((n, i) => {
    const result = n.result || {};
    let detail = esc(why(n));
    if (result.mission_id && n.activity === "submit_mission") detail = show(link("missions/" + result.mission_id, "任务 " + result.mission_id));
    if (n.activity === "await_mission" && n.state === "completed") detail = `已证实 · 证据 ${esc(String(result.evidence_id || "").slice(0, 18))}…`;
    if (n.activity === "analyze_evidence" && n.state === "completed") detail = `${result.suspected ? "疑似异常" : "未见异常"} · ${sourceChip(result.source)} · ${esc(result.confidence)}`
      + (result.finding_id ? ` · <a class="mono" href="#business/finding/${esc(result.finding_id)}">${esc(result.finding_id)}</a>` : "");
    if (n.activity === "human_review" && n.state === "completed") detail = `${result.decision === "confirmed" ? "确认" : "驳回"} · ${esc(result.reviewer)}`;
    if (result.order_id) detail = `工单 ${esc(result.order_id)}`;
    if (n.activity === "request_reinspection" && result.run_id) detail = `<a class="mono" href="#workflows/${esc(result.run_id)}">复检 ${esc(result.run_id)}</a>`;
    if (n.detail?.late_result) detail += ` · 取消后到达的结果：${esc(WSTATE[n.detail.late_result.state] || n.detail.late_result.state)}（只记录）`;
    const symbol = n.state === "completed" ? "✓" : ["failed", "cancelled"].includes(n.state) ? "×" : n.state === "outcome_unknown" ? "?" : i + 1;
    return `<li class="${NODE_TONE[n.state] || ""}"><span class="dot" aria-hidden="true">${symbol}</span><div class="flow-body"><div class="flow-top"><b>${esc(ACTIVITY[n.activity] || n.activity)}</b><code>${esc(n.node_id)}</code>${wchip(n.state)}</div>
      ${detail ? `<div class="flow-note">${detail}</div>` : ""}</div></li>`;
  }).join("")}</ol>`;
  const main = [card("节点", nodes, "每个等待与失败都有原因")];
  if (run.missions.length) main.push(card("子任务（每个仍需人工审批）", table(["任务", "节点", "状态", "预约"], run.missions.map(m => [link("missions/" + m.mission_id, m.mission_id), m.node_id, raw(chip(m.status)), m.reservations.map(x => RESERVATION[x.state] || x.state).join(", ") || "—"]))));
  if (run.analyses.length) main.push(card("分析", table(["资产", "分析器", "来源", "结论"], run.analyses.map(a => [a.asset_id, a.analyzer, raw(sourceChip(a.source)), a.verdict])), "来源已标注；脚本 / 确定性结果不是模型识别"));
  if (run.orders.length) main.push(card("工单", table(["工单", "资产", "状态"], run.orders.map(o => [o.order_id, o.asset_id, ORDER[o.state] || o.state]))));
  if (run.children.length) main.push(card("复检运行", run.children.map(c => `<div class="item"><span class="item-main"><a class="mono" href="#workflows/${esc(c.run_id)}">${esc(c.run_id)}</a></span>${wchip(c.state)}</div>`).join("")));
  main.push(card("时间线", `<ol class="timeline" data-k="run-events-${esc(r.run_id)}">${run.events.slice().reverse().slice(0, 60).map(e => `<li>${show(when(e.created_at))}
    <span>${eventName(e.kind)} ${esc(e.body.node_id || "")} ${esc(e.body.state || e.body.to || "")} ${esc(e.body.reason || "")}</span></li>`).join("")}</ol>`));
  paint("runDetail", detailBack("workflows", "工作流列表") + html + `<div class="stack" style="margin-top:14px">${main.join("")}</div>`);
}

function renderDraft(result) {
  if (!result) return;
  const spec = result.spec;
  paint("draftResult", `<p class="hint">草案 ${esc(result.status === "planned" ? "已生成（未生效）" : result.status === "refused" ? "被拒答" : "无效")} · 来源 ${sourceChip(result.use?.source)}
    ${result.errors?.length ? " · " + esc(result.errors.join("；")) : ""}${result.decline_reason ? " · " + esc(result.decline_reason) : ""}</p>
    ${spec ? table(["节点", "活动"], spec.nodes.map(n => [n.node_id, ACTIVITY[n.activity] || n.activity])) + `<p class="hint">摘要 ${esc(String(result.spec_sha256).slice(0, 16))}…；${esc(result.note)}</p>` : ""}`);
}

// ── scheduling / 调度 ──
function waiting(t) {
  if (t.state !== "queued" || !t.waiting) return "";
  return Object.entries(t.waiting).map(([robot, list]) => `${robot}：${reasons(list) || "可派遣"}`).join("；");
}

function renderTasks() {
  if (!tasks) { renderShell(); return; }
  const operator = can("operator", tasks.roles);
  const assets = Object.entries(tasks.assets || {});
  const chosen = byId("taskAsset").value;
  byId("taskAsset").innerHTML = assets.map(([id, a]) => `<option value="${esc(id)}" data-volume="${esc(a.volume_id)}">${esc(id)} · ${esc(a.robots.join("/"))}</option>`).join("");
  if (assets.some(([id]) => id === chosen)) byId("taskAsset").value = chosen;
  byId("taskSubmit").disabled = !operator || !assets.length;
  byId("taskCatalog").textContent = tasks.catalog ? `${tasks.catalog.catalog_id} · ${tasks.catalog.ranking} / ${tasks.catalog.policy}` : "";
  renderTaskQueue();
  paint("taskRobots", `<div class="list">${tasks.robots.map(r => `<div class="item"><span class="item-main"><span class="item-title">${esc(r.robot_id)} · ${esc(r.site_id || "")} / ${esc(r.dock_id)}</span>
    ${r.eligibility.reasons.length ? `<span class="why">${esc(reasons(r.eligibility.reasons))}</span>` : ""}
    ${r.assignments.map(a => `<span class="item-meta"><a href="#tasks/${esc(a.task_id)}">${esc(a.task_id)}</a> 代次 ${esc(a.epoch)}${a.mission_id ? ` · <a href="#missions/${esc(a.mission_id)}">${esc(a.mission_id)}</a>` : ""}</span>`).join("")}</span>
    ${badge(VERDICT[r.eligibility.verdict] || r.eligibility.verdict, VTONE[r.eligibility.verdict])}</div>`).join("")}</div>`
    + (tasks.robots.length ? "" : '<div class="empty">本项目没有机器人。</div>'));
  const holds = tasks.airspace.holds, envelopes = tasks.airspace.envelopes;
  byId("taskAirspaceSub").textContent = `${tasks.airspace.frame} · 单元 ${tasks.airspace.cell_m} m`;
  paint("taskAirspace", `<div class="airspace">${airspaceSketch(tasks.airspace)}</div>
    <div class="list" style="margin-top:10px">${holds.map(h => `<div class="item"><span class="item-main"><span class="item-title">${esc(h.robot_id)} · <a class="mono" href="#missions/${esc(h.mission_id)}">${esc(h.mission_id)}</a></span>
      <span class="item-meta">${esc(h.cells.length)} 个单元（${esc(tasks.airspace.cell_m)} m）</span></span>${badge(PAD[h.state] || h.state, h.state === "uncertain" ? "bad" : "info")}</div>`).join("")}
    ${envelopes.map(e => `<div class="item"><span class="item-main"><span class="item-title">失联包络 · ${esc(e.activity)}</span><span class="why bad">${esc(e.cells.length)} 个单元，只增不减，直到对账</span></span></div>`).join("")}</div>`
    + (!holds.length && !envelopes.length ? '<div class="empty">无持有。</div>' : "")
    + `<div class="map-stations"><b>机位</b>${tasks.robots.map(r => `<span>${esc(r.dock_id || "来源未记录")} · ${esc(r.robot_id)}</span>`).join("")}<small>服务未提供机位坐标，图中不绘制机位点。</small></div>`);
  renderShell();
}

function renderTaskQueue() {
  if (!tasks) return;
  byId("taskQueueSub").textContent = tasks.tasks.length ? tasks.tasks.length + " 个" : "";
  paint("taskQueue", tasks.tasks.slice().reverse().map(t => `<a class="item" href="#tasks/${esc(t.task_id)}" aria-current="${t.task_id === nav.id && nav.ws === "tasks"}"><span class="item-main">
    <span class="item-title">${esc(t.asset_id)} · P${esc(t.priority)}</span><span class="item-meta">${esc(t.task_id)} · ${esc(stamp(t.updated_at))}${t.robot_id ? " · " + esc(t.robot_id) + " 代次 " + esc(t.epoch) : ""}</span>
    ${waiting(t) ? `<span class="why">${esc(waiting(t))}</span>` : ""}${t.reason ? `<span class="why bad">${esc(reasons(t.reason.split(",")))}</span>` : ""}</span>${tchip(t.state)}</a>`).join("")
    || '<div class="empty">暂无任务单。</div>');
}

// Held cells drawn on the site grid, north up; a sketch of the service's holds, not a map. / 以北为上在站点网格上画出持有单元；是服务持有记录的示意，不是地图。
function cellXY(cell) {
  const parts = String(cell).split(".");
  const x = Number(parts[parts.length - 2]), y = Number(parts[parts.length - 1]);
  return Number.isInteger(x) && Number.isInteger(y) ? {x, y} : null;
}
function airspaceSketch(airspace) {
  const palette = ["#8ec4e5", "#c6e58a", "#e7b46c", "#c9a8ea", "#9fe0cf"];
  const cells = [];
  airspace.holds.forEach((h, i) => h.cells.forEach(c => { const p = cellXY(c); if (p) cells.push({...p, fill: palette[i % palette.length]}); }));
  airspace.envelopes.forEach(e => e.cells.forEach(c => { const p = cellXY(c); if (p) cells.push({...p, envelope: true}); }));
  const xs = [-2, 2, ...cells.map(c => c.x)], ys = [-2, 2, ...cells.map(c => c.y)];
  const minX = Math.max(Math.min(...xs) - 1, -30), maxX = Math.min(Math.max(...xs) + 1, 30);
  const minY = Math.max(Math.min(...ys) - 1, -30), maxY = Math.min(Math.max(...ys) + 1, 30);
  const size = 22, w = (maxX - minX + 1) * size, h = (maxY - minY + 1) * size;
  const px = x => (x - minX) * size, py = y => (maxY - y) * size;
  let grid = "";
  for (let x = minX; x <= maxX + 1; x++) grid += `<line x1="${px(x)}" y1="0" x2="${px(x)}" y2="${h}"/>`;
  for (let y = minY - 1; y <= maxY; y++) grid += `<line x1="0" y1="${py(y)}" x2="${w}" y2="${py(y)}"/>`;
  const rects = cells.filter(c => c.x >= minX && c.x <= maxX && c.y >= minY && c.y <= maxY).map(c => c.envelope
    ? `<rect x="${px(c.x) + 2}" y="${py(c.y) + 2}" width="${size - 4}" height="${size - 4}" rx="3" fill="url(#envelope)" stroke="#f19a87"/>`
    : `<rect x="${px(c.x) + 2}" y="${py(c.y) + 2}" width="${size - 4}" height="${size - 4}" rx="3" fill="${c.fill}" fill-opacity=".85"/>`).join("");
  const origin = `<circle cx="${px(0) + size / 2}" cy="${py(0) + size / 2}" r="4" fill="none" stroke="#e6ecde" stroke-width="1.5"/>`;
  const legend = airspace.holds.map((hold, i) => `<span><i style="background:${palette[i % palette.length]}"></i>${esc(hold.robot_id)} · ${esc(hold.mission_id)}</span>`).join("")
    + (airspace.envelopes.length ? '<span><i style="background:#f19a87"></i>失联包络</span>' : "") + "<span>○ 坐标原点</span>";
  return `<div class="sky"><svg width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" role="img" aria-label="空域网格持有示意"><defs><pattern id="envelope" width="6" height="6" patternUnits="userSpaceOnUse" patternTransform="rotate(45)"><rect width="6" height="6" fill="#3a1f1a"/><line x1="0" y1="0" x2="0" y2="6" stroke="#f19a87" stroke-width="2"/></pattern></defs>
    <g stroke="var(--field-line)" stroke-width="1">${grid}</g>${rects}${origin}</svg></div><div class="legend"><span>↑ 北</span>${legend}</div><p class="scale">${esc(airspace.frame)} · 每格 ${esc(airspace.cell_m)} m · 网格持有示意</p>`;
}

function renderTask() {
  const open = nav.ws === "tasks" && !!nav.id;
  byId("taskOverview").hidden = open;
  byId("taskDetail").hidden = !open;
  if (!open) return;
  if (!task || task.task.task_id !== nav.id) { paint("taskDetail", blank("加载任务单 " + nav.id + " …")); return; }
  const t = task.task, live = !TASK_FINAL.includes(t.state);
  let html = `<header class="d-head"><div class="eyebrow">任务单 ${esc(t.task_id)} · 提交 ${esc(t.requested_by)} · 来源 ${esc(t.source)}</div>
    <h1>${esc(t.asset_id)} · ${esc(t.volume_id)}</h1><div class="badges">${tchip(t.state)}${tag("优先级 " + t.priority)}${t.robot_id ? tag(t.robot_id + " · 代次 " + t.epoch) : ""}</div></header>`;
  if (t.reason) html += `<section class="callout bad"><h3>原因</h3><p>${esc(reasons(t.reason.split(",")))}</p></section>`;
  if (waiting(t)) html += `<section class="callout warn"><h3>排队中</h3><p>${esc(waiting(t))}</p></section>`;
  if (t.outcome) html += `<section class="callout info"><h3>结果</h3><p>${esc(t.outcome.robot_id || "")} · ${esc(t.outcome.mission_id || "")} · ${esc(t.outcome.status || t.outcome.reason || "")}</p></section>`;
  if (t.cancel) html += `<section class="callout warn"><h3>已请求取消</h3><p>取消：${esc(t.cancel.requested_by)} · ${esc(stamp(t.cancel.requested_at))} · ${esc(t.cancel.reason)}；已开始的飞行经原通道收尾，对账后才显示已取消。</p></section>`;
  if (live && !t.cancel) html += `<section class="callout"><h3>取消任务单</h3><p>已领取或在飞的任务按 P1 规则取消与对账。</p><div class="buttons"><button class="btn danger" type="button" id="cancelTask" data-act="cancelTask" data-id="${esc(t.task_id)}" ${can("operator", tasks?.roles) ? "" : "disabled"}>取消任务单</button></div></section>`;
  const assignments = table(["代次", "机器人", "任务", "状态"], task.assignments.map(a => [a.epoch, a.robot_id,
    a.mission_id ? raw(`<a class="mono" href="#missions/${esc(a.mission_id)}">${esc(a.mission_id)}</a> ${a.mission_status ? chip(a.mission_status) : ""}`) : "—",
    (ASTATE[a.state] || a.state) + (a.reason ? " · " + reasons(a.reason.split(",")) : "")]), "尚未分配。");
  const decision = d => `<div class="res-card"><div class="res-head"><b>${esc(TVERDICT[d.verdict] || d.verdict)}${d.robot_id ? " → " + esc(d.robot_id) : ""}</b>
    <span class="sub">${esc(stamp(d.created_at))} · 代次 ${esc(d.epoch)} · ${esc(d.ranking_version)} / ${esc(d.policy_version)} · 快照 ${esc(String(d.snapshot_sha256).slice(0, 12))}…</span></div>
    ${table(["排序", "机器人", "站点 / 机场", "预计到场 s", "近期使用", "判定", "原因"], d.candidates.map(c => {
      const rank = (d.order || []).indexOf(c.robot_id), robot = (tasks?.robots || []).find(r => r.robot_id === c.robot_id);
      return [rank >= 0 ? rank + 1 : "—", c.robot_id, robot ? `${robot.site_id || "—"} / ${robot.dock_id || "—"}` : "—", c.eta_s ?? "—", c.usage ?? "—", raw(badge(VERDICT[c.verdict] || c.verdict, VTONE[c.verdict])), reasons(c.reasons) || "—"];
    }))}<p class="hint">排序来自本次服务判定；站点 / 机场为当前资源目录。</p></div>`;
  const decisions = task.decisions.slice().reverse();
  const decided = decisions.length ? decision(decisions[0]) + (decisions.length > 1 ? `<details class="more" data-k="older-${esc(t.task_id)}"><summary>更早的 ${decisions.length - 1} 次判定</summary><div class="stack" style="margin-top:10px">${decisions.slice(1).map(decision).join("")}</div></details>` : "")
    : '<div class="empty">暂无判定。</div>';
  const events = `<ol class="timeline" data-k="task-events-${esc(t.task_id)}">${task.events.slice().reverse().slice(0, 60).map(e => `<li>${show(when(e.created_at))}
    <span>${eventName(e.kind)} ${esc(e.body.robot_id || "")} ${esc(e.body.epoch ?? "")} ${esc(e.body.reason || e.body.state || "")}</span></li>`).join("")}</ol>`;
  paint("taskDetail", detailBack("tasks", "调度列表") + html + `<div class="stack" style="margin-top:14px">${card("分配（代次递增；撤回只发生在领取之前）", assignments)}${card("判定（按录制快照可重放）", decided, "最新在前")}${card("时间线", events)}</div>`);
}

// ── fleet / 机队 ──
function dockActionTrack(action) {
  const states = ["requested", "acked", "completed"];
  const reached = {requested: !!action.requested_at, acked: !!action.acked_at && [true, 1].includes(action.ack_accepted),
    completed: action.state === "completed"};
  return `<div class="action-track" aria-label="${esc(ACTION_STATE[action.state] || action.state)}">${states.map(s =>
    `<span class="${action.state === s ? "now" : reached[s] ? "done" : "pending"}">${reached[s] ? "✓ " : "○ "}${esc(ACTION_STATE[s])}</span>`).join('<span aria-hidden="true">→</span>')}
    ${["rejected", "failed"].includes(action.state) ? badge(ACTION_STATE[action.state], "bad") : ""}</div>`;
}
function dims(d) {
  const s = d.status;
  if (!s) return '<div class="why bad">尚无机场报告：不可派遣。</div>';
  const charge = s.energy.charge_fraction == null ? null : Math.round(s.energy.charge_fraction * 100);
  const energyTone = {ready: "ok", charging: "warn", cooling: "warn", fault: "bad", unknown: "unknown"}[s.energy.state] || "";
  const tile = (label, value, tone, extra = "") => `<div class="dim ${tone || ""}"><small>${esc(label)}</small><b>${esc(value)}</b>${extra}</div>`;
  return `<div class="dims">${[
    tile("链路", SHORT[s.link] || s.link, s.link === "unknown" ? "unknown" : s.link === "online" && s.fresh ? "ok" : "bad"),
    tile("舱盖", SHORT[s.lid] || s.lid, s.lid === "jammed" ? "bad" : s.lid === "unknown" ? "unknown" : ["opening", "closing"].includes(s.lid) ? "info" : "ok"),
    tile("在位", SHORT[s.aircraft] || s.aircraft, s.aircraft === "present" ? "ok" : s.aircraft === "unknown" ? "unknown" : ""),
    tile("补能", (SHORT[s.energy.state] || s.energy.state) + (charge == null ? "" : " " + charge + "%"), energyTone, charge == null ? "" : `<div class="meter"><i style="width:${Math.max(0, Math.min(100, charge))}%"></i></div>`),
    tile("环境", SHORT[s.environment.state] || s.environment.state, s.environment.state === "unknown" ? "unknown" : s.environment.state === "permitted" ? "ok" : "warn", s.environment.wind_mps == null ? "" : `<em>风 ${esc(s.environment.wind_mps)} m/s</em>`),
    tile("运维", s.lock ? "锁定：" + (DIM[s.lock.state] || s.lock.state) : SHORT[s.upkeep] || s.upkeep, s.lock ? "bad" : s.upkeep === "unknown" ? "unknown" : s.upkeep !== "normal" ? "bad" : "ok"),
    tile("机位", PAD[d.pad] || d.pad, d.pad === "uncertain" ? "unknown" : d.pad === "free" ? "" : "warn", d.holder ? `<em>${esc(d.holder)}</em>` : ""),
    tile("新鲜度", (s.fresh ? "" : "过期 · ") + s.age_s.toFixed(1) + " s 前", s.fresh ? "ok" : "bad", `<em>${s.session === "active" ? "会话有效" : "会话对账中"}</em>`),
  ].join("")}</div>`;
}

function robotLine(r) {
  const status = r.status ? `${r.status.flight_phase === "grounded" ? "机体在地面" : r.status.flight_phase} · ${Math.round(r.status.age_s)} s 前${r.status.energy == null ? "" : " · 电量 " + pct(r.status.energy)}` : "机体无状态";
  return status;
}

function renderResources() {
  if (!resources) {
    paint("resourceList", project() ? '<div class="empty">加载资源中。</div>' : '<div class="empty">选择项目后显示站点、机场与机器人。</div>');
    paint("fleetOverview", "");
    renderShell();
    return;
  }
  const admin = can("admin", resources.roles);
  byId("fleetSub").textContent = `${resources.catalog.catalog_id} · 判定于 ${stamp(resources.evaluated_at)}`;
  paint("resourceList", resources.sites.map(site => `<div class="site"><div class="site-head">${esc(site.site_id)}</div><div class="list">
    ${site.docks.map(d => `<a class="item" href="#fleet/${esc(d.dock_id)}" aria-current="${d.dock_id === nav.id && nav.ws === "fleet"}"><span class="item-main"><span class="item-title">${esc(d.dock_id)}</span>${sourceChip(d.source)}
      <span class="item-meta">${d.status ? `${esc(DIM[d.status.link] || d.status.link)} · ${esc(PAD[d.pad] || d.pad)} · ${esc((d.status.fresh ? "" : "过期 · ") + d.status.age_s.toFixed(1) + " s 前")}` : "尚无机场报告"}</span>
      ${d.status?.lock ? `<span class="why bad">锁定：${esc(DIM[d.status.lock.state] || d.status.lock.state)}</span>` : ""}</span>${d.status ? badge(d.status.link === "online" && d.status.fresh ? "在线" : DIM[d.status.link] || d.status.link, d.status.link === "online" && d.status.fresh ? "ok" : "bad") : badge("无状态", "bad")}</a>`).join("")}
    ${site.robots.map(r => `<a class="item" href="#fleet/${esc(r.robot_id)}" aria-current="${r.robot_id === nav.id && nav.ws === "fleet"}"><span class="item-main"><span class="item-title">${esc(r.robot_id)}</span>${sourceChip(r.execution_backend)}
      <span class="item-meta">${esc(robotLine(r))}</span>${r.eligibility.reasons.length ? `<span class="why">${esc(reasons(r.eligibility.reasons))}</span>` : ""}</span>${badge(VERDICT[r.eligibility.verdict] || r.eligibility.verdict, VTONE[r.eligibility.verdict])}</a>`).join("")}</div></div>`).join(""));
  paint("fleetOverview", resources.sites.map(site => site.docks.map(d => `<section class="res-card"><div class="res-head"><a href="#fleet/${esc(d.dock_id)}">${esc(d.dock_id)}</a>
      <span class="sub">${esc(site.site_id)} · ${sourceChip(d.source)} · 服务 ${esc(d.serves.join(", "))}</span></div>${dims(d)}
      ${d.status?.lock && admin ? `<div><button class="btn danger small" type="button" data-act="release" data-id="${esc(d.dock_id)}">解除维护锁（管理员）</button></div>` : ""}</section>`).join("")
    + site.robots.map(r => `<section class="res-card"><div class="res-head"><a href="#fleet/${esc(r.robot_id)}">${esc(r.robot_id)}</a><span class="sub">${esc(site.site_id)} · ${sourceChip(r.execution_backend)} · ${esc(robotLine(r))}</span>
      ${badge(VERDICT[r.eligibility.verdict] || r.eligibility.verdict, VTONE[r.eligibility.verdict])}</div>${r.eligibility.reasons.length ? `<div class="why">${esc(reasons(r.eligibility.reasons))}</div>` : ""}</section>`).join("")).join("")
    + `<p class="hint">目录 ${esc(resources.catalog.catalog_id)} · 策略 ${esc(resources.catalog.policy.version)} · 判定于 ${esc(stamp(resources.evaluated_at))}。“在线”不等于可派遣：可派遣是带有效期的判断，不是授权。</p>`);
  renderShell();
  renderResourceDetail();
}

function renderResourceDetail() {
  const open = nav.ws === "fleet" && !!nav.id;
  byId("fleetOverview").hidden = open;
  byId("fleetDetail").hidden = !open;
  if (!open) return;
  const d = resourceDetail;
  if (!d || (d.dock_id || d.robot_id) !== nav.id) { paint("fleetDetail", blank("加载 " + nav.id + " …")); return; }
  let html = `<header class="d-head"><div class="eyebrow">资源 · ${esc(d.kind === "dock" ? "机场" : "机器人")} · ${esc(d.site_id || "")}</div><h1>${esc(d.dock_id || d.robot_id)}</h1></header>`;
  if (d.kind === "dock") {
    const admin = can("admin", resources?.roles);
    html += `<div class="stack" style="margin-top:14px">${card("状态", dims(d) + (d.status?.lock && admin ? `<div class="buttons"><button class="btn danger" type="button" data-act="release" data-id="${esc(d.dock_id)}">解除维护锁（管理员）</button></div>` : ""),
      `${esc(d.vendor)} / ${esc(d.model)} · ${esc(d.source === "logical_sim" ? "逻辑模拟机场" : d.source)} · 服务 ${esc(d.serves.join(", "))}`)}`;
    html += card("动作", table(["动作", "进度", "活动", "请求", "回执"], d.actions.slice().reverse().map(a => [DOCK_ACTION[a.kind] || a.kind, raw(dockActionTrack(a)), raw(`<code>${esc(a.activity_key)}</code>`), when(a.requested_at),
      a.acked_at ? (a.ack_accepted ? "受理" : "拒绝 " + (a.ack_reason || "")) : "—"]), "暂无动作。"), "ACK 只表示受理，完成以后续状态报告为准");
    html += card("预约", table(["活动", "状态", "原因"], d.reservations.slice().reverse().map(r => [raw(`<code>${esc(r.activity_key)}</code>`), RESERVATION[r.state] || r.state, RELEASE[r.reason] || r.reason]), "暂无预约。"));
    const reports = d.events.filter(e => e.kind === "status.accepted").length;
    const others = d.events.filter(e => e.kind !== "status.accepted").slice().reverse();
    html += card("审计事件", `<p class="hint" style="margin-top:0">最近 ${d.events.length} 条中有 ${reports} 条状态报告已被接受。</p><ol class="timeline" data-k="dock-events-${esc(d.dock_id)}">${others.map(e => `<li class="${e.kind === "status.rejected" ? "alert" : ""}">${show(when(e.created_at))}
      <span>${eventName(e.kind)} <small>${esc(JSON.stringify(e.body).slice(0, 160))}</small></span></li>`).join("") || "<li><span>暂无其他事件。</span></li>"}</ol>`);
    html += "</div>";
  } else {
    html += `<div class="stack" style="margin-top:14px">${card("可派遣判定", `<div class="verdict ${VTONE[d.eligibility.verdict] || ""}"><b>${esc(VERDICT[d.eligibility.verdict] || d.eligibility.verdict)}</b></div>
      ${d.eligibility.reasons.length ? `<p class="why">${esc(reasons(d.eligibility.reasons))}</p>` : ""}
      ${kv([["能力来源", d.capability_source], ["执行后端", raw(sourceChip(d.execution_backend))], ["判定有效至", when(d.eligibility.valid_until)],
        ["快照摘要", short(d.eligibility.snapshot_sha256, 16)], ["策略", d.eligibility.policy_version || "—"]])}<p class="hint">可派遣是带有效期的判断，不是授权；每次飞行仍需人工审批签名，机载仍会复核。</p>`)}
      ${card("机体状态", kv([["飞行阶段", d.status ? (d.status.flight_phase === "grounded" ? "机体在地面" : d.status.flight_phase) : "机体无状态"], ["状态年龄", d.status ? Math.round(d.status.age_s) + " s" : "—"],
        ["电量", d.status ? pct(d.status.energy) : "—"], ["机场", d.dock_id || "—"]]))}
      ${card("技能", `<div class="badges">${(d.skills || []).map(s => tag(s)).join("")}</div>`)}</div>`;
  }
  paint("fleetDetail", detailBack("fleet", "机队列表") + html);
}

// ── business / 业务 ──
function renderBusiness() {
  if (!business) { renderShell(); return; }
  const columns = business.report.columns;
  byId("businessCatalog").textContent = business.catalog?.catalog_id || "";
  paint("businessReport", `<div class="report-cols">${[["closed", "已关单", "completed"], ["open", "未关单", "not_completed"], ["unknown", "复检不确定", "uncertain"]].map(([key, label, tone]) =>
    `<div class="rc ${tone}${columns[key].length ? "" : " zero"}"><small>${esc(label)}</small><b>${esc(columns[key].length)}</b></div>`).join("")}</div>
    <p class="hint">已关单只接受：维修反馈之后的新的已证实采集、允许来源的未疑似分析，以及 reviewer 对本轮的确认。</p>`);
  byId("findingsSub").textContent = business.findings.length ? business.findings.length + " 个" : "";
  byId("ordersSub").textContent = business.orders.length ? business.orders.length + " 张" : "";
  paint("businessFindings", business.findings.map(f => `<a class="item" href="#business/finding/${esc(f.finding_id)}" aria-current="${nav.kind === "finding" && f.finding_id === nav.id}"><span class="item-main">
    <span class="item-title">${esc(f.asset_key)} · ${esc(f.family)}</span><span class="item-meta">${esc(f.finding_id)} · ${esc(f.jobs)} 次分析 · ${esc(stamp(f.updated_at))}</span></span>${bchip(FSTATE, FTONE, f.state)}</a>`).join("")
    || '<div class="empty">暂无发现。</div>');
  paint("businessOrders", business.orders.map(o => `<a class="item" href="#business/order/${esc(o.order_id)}" aria-current="${nav.kind === "order" && o.order_id === nav.id}"><span class="item-main">
    <span class="item-title">${esc(o.asset_key)}</span><span class="item-meta">${esc(o.order_id)} · 第 ${esc(o.round)} 轮 · ${esc(stamp(o.updated_at))}</span></span>${bchip(ORDER, OTONE, o.state)}</a>`).join("")
    || '<div class="empty">暂无工单。</div>');
  paint("businessJobs", table(["分析器", "状态", "结论", "来源", "分数", "原因"], business.jobs.slice(0, 20).map(j => [raw(`<code>${esc(j.analyzer)}</code>`), JSTATE[j.state] || j.state,
    raw(j.verdict ? bchip(BVERDICT, BTONE, j.verdict) : "—"), raw(sourceChip(j.source)), j.score ?? "—", reasons(j.reasons) || "—"]), "暂无分析。"));
  paint("businessReferences", business.references.map(r => `<div class="item"><span class="item-main"><span class="item-title">${esc(r.asset_key)} · ${esc(r.state)}</span>
    <span class="item-meta">${esc(r.registered_by)} · ${esc(stamp(r.registered_at))} · ${esc(String(r.evidence_id).slice(0, 24))}…</span></span></div>`).join("") || '<div class="empty">未登记。</div>');
  renderShell();
}

function renderSubject() {
  const open = nav.ws === "business" && !!nav.id && !!nav.kind;
  byId("bizOverview").hidden = open;
  byId("bizDetail").hidden = !open;
  if (!open) return;
  const id = subject?.kind === "finding" ? subject.finding?.finding_id : subject?.order?.order_id;
  if (!subject || subject.kind !== nav.kind || id !== nav.id) { paint("bizDetail", blank("加载 " + nav.id + " …")); return; }
  paint("bizDetail", detailBack("business", "发现与工单") + (subject.kind === "finding" ? findingHtml() : orderHtml()));
  if (subject.kind === "finding") for (const j of subject.jobs) if (j.mission_id && j.evidence_id) requestMedia(j.mission_id, j.evidence_id);
}

function eventsList(events, key) {
  return card("时间线", `<ol class="timeline" data-k="${esc(key)}">${(events || []).slice().reverse().slice(0, 60).map(e => `<li>${show(when(e.created_at))}
    <span>${eventName(e.kind)} ${esc(e.actor || "")} <small>${esc(JSON.stringify(e.body).slice(0, 140))}</small></span></li>`).join("")}</ol>`);
}

function findingHtml() {
  const f = subject.finding, body = f.body || {}, reviewer = can("reviewer", business?.roles);
  let html = `<header class="d-head"><div class="eyebrow">发现 ${esc(f.finding_id)} · 聚合键 ${esc(f.cluster_key)}</div><h1>${esc(f.asset_key)} · ${esc(f.family)}</h1>
    <div class="badges">${bchip(FSTATE, FTONE, f.state)}${tag(f.jobs + " 次分析")}</div>${body.description ? `<p class="lead">首次描述（来自分析器，只是候选）：${esc(body.description)}</p>` : ""}</header>`;
  if (subject.review) html += `<section class="callout info"><h3>复核：${esc(subject.review.decision === "confirmed" ? "确认" : "驳回")}</h3><p>${esc(subject.review.reviewer)} · ${esc(stamp(subject.review.created_at))}${subject.review.note ? " · " + esc(subject.review.note) : ""}</p></section>`;
  if (f.state === "candidate") html += `<section class="decision warn"><h3>待复核</h3><p>分析结论只是候选。确认后按「每个发现一张工单」建单；驳回后该发现关闭，同一缺陷再次出现会开新发现。</p>
    <div class="buttons"><button class="btn primary" type="button" data-act="decide" data-id="${esc(f.finding_id)}" data-decision="confirmed" ${reviewer ? "" : "disabled"}>确认异常并建单</button>
    <button class="btn danger" type="button" data-act="decide" data-id="${esc(f.finding_id)}" data-decision="dismissed" ${reviewer ? "" : "disabled"}>驳回</button></div></section>`;
  if (subject.order) html += `<section class="callout"><h3>工单</h3><p><a class="mono" href="#business/order/${esc(subject.order.order_id)}">${esc(subject.order.order_id)}</a> ${bchip(ORDER, OTONE, subject.order.state)}</p></section>`;
  const jobs = `<div class="analysis-list">${subject.jobs.map(j => `<section class="analysis-row"><div class="badges"><code>${esc(j.analyzer)}</code>${sourceChip(j.source)}${j.verdict ? bchip(BVERDICT, BTONE, j.verdict) : badge(JSTATE[j.state] || j.state)}
    ${tag("分数 " + (j.score ?? "—"))}</div><p>${esc(PURPOSE[j.purpose] || j.purpose)} · ${esc(reasons(j.reasons) || "无原因记录")}</p>${j.description ? `<p>${esc(j.description)}</p>` : ""}
    ${j.mission_id ? show(link("missions/" + j.mission_id, j.mission_id)) : ""}</section>`).join("")}</div>`;
  const facts = card("发现记录", kv([["资产", f.asset_key], ["缺陷族", f.family], ["分析记录", f.jobs], ["状态", raw(bchip(FSTATE, FTONE, f.state))],
    ["复核", subject.review ? `${subject.review.reviewer} · ${subject.review.decision === "confirmed" ? "确认" : "驳回"}` : raw(badge("待复核", "warn"))]]));
  return html + `<div class="d-grid"><div class="stack">${findingGallery(subject.jobs)}${card("分析", jobs, "来源已标注；脚本 / 确定性结果不是模型识别")}${eventsList(subject.events, "finding-events-" + f.finding_id)}</div>
    <aside class="stack facts">${facts}${subject.order ? card("工单状态", kv([["工单", link("business/order/" + subject.order.order_id, subject.order.order_id)], ["状态", raw(bchip(ORDER, OTONE, subject.order.state))], ["轮次", subject.order.round]]) + '<p class="hint">关单条件与本轮记录见工单详情。</p>') : ""}</aside></div>`;
}

function findingGallery(jobs) {
  const seen = new Set(), items = jobs.filter(j => j.evidence_id && !seen.has(j.evidence_id) && seen.add(j.evidence_id));
  if (!items.length) return "";
  const active = items.find(j => j.evidence_id === selectedEvidence) || items[0];
  const image = j => photos[j.evidence_id] ? `<img alt="分析所用的采集 ${esc(j.evidence_id)}" src="${esc(photos[j.evidence_id])}">` : "加载图像…";
  return card("采集", `<figure class="shot evidence-hero"><div class="frame">${image(active)}</div><figcaption class="cap"><span class="mono">${esc(active.evidence_id)}</span>${active.mission_id ? show(link("missions/" + active.mission_id, active.mission_id)) : ""}</figcaption></figure>
    <div class="thumbnails">${items.map((j, i) => `<button type="button" data-act="inspectEvidence" data-id="${esc(j.evidence_id)}" aria-label="查看第 ${i + 1} 份采集" aria-pressed="${j === active}">${image(j)}</button>`).join("")}</div>`, "只读原始证据");
}

function closureFacts(order, rounds) {
  const latest = rounds.find(r => r.round === order.round), conclusion = latest?.conclusion || order.closure;
  const missing = raw(badge("服务未提供", "unknown"));
  return card("关单条件 · 服务记录", kv([["工单状态", raw(bchip(ORDER, OTONE, order.state))], ["维修反馈", latest?.feedback ? `${latest.feedback.reported_by} · ${stamp(latest.feedback.reported_at)}` : missing],
    ["复检采集", conclusion?.evidence_id || missing], ["采集时间", conclusion?.captured_at ? when(conclusion.captured_at) : missing],
    ["分析来源", raw(sourceChip(conclusion?.job_source))], ["本轮复核", latest?.review ? `${latest.review.reviewer} · ${latest.review.decision === "confirmed" ? "确认修复" : "驳回"}` : missing],
    ["结算", conclusion ? raw(badge(RSTATE[conclusion.status] || conclusion.status, {passed: "ok", failed: "bad", unknown: "unknown"}[conclusion.status])) : missing],
    conclusion?.reasons?.length ? ["原因", reasons(conclusion.reasons)] : null]) + '<p class="hint">服务核对反馈后的新采集、允许来源的未疑似分析与本轮复核；页面不推导条件是否通过。</p>');
}

function orderHtml() {
  const o = subject.order, operator = can("operator", business?.roles), reviewer = can("reviewer", business?.roles);
  let html = `<header class="d-head"><div class="eyebrow">工单 ${esc(o.order_id)} · 建单 ${esc(o.created_by)}</div><h1>${esc(o.asset_key)}</h1>
    <div class="badges">${bchip(ORDER, OTONE, o.state)}${tag("第 " + o.round + " 轮")}</div>
    <p class="lead">发现：<a class="mono" href="#business/finding/${esc(subject.finding.finding_id)}">${esc(subject.finding.finding_id)}</a> ${bchip(FSTATE, FTONE, subject.finding.state)}</p></header>`;
  if (REPAIRABLE.includes(o.state)) html += `<section class="decision warn"><h3>记录维修反馈</h3><p>维修反馈不关单：复检飞行仍需逐次审批签名；关单需要反馈之后的新采集、未疑似的分析与 reviewer 对本轮的确认。</p>
    <div class="buttons"><button class="btn primary" type="button" id="repairOrder" data-act="orderRepair" data-id="${esc(o.order_id)}" ${operator ? "" : "disabled"}>记录维修反馈并启动复检</button></div></section>`;
  if (o.closure) html += `<section class="callout info"><h3>关单</h3><p>${esc(o.closure.rule)} · 证据 ${esc(String(o.closure.evidence_id || "").slice(0, 24))}… · 采集 ${esc(stamp(o.closure.captured_at))} · 分析来源 ${sourceChip(o.closure.job_source)}</p></section>`;
  html += subject.rounds.filter(r => r.state === "reinspecting" && !r.review).map(r => `<section class="decision warn"><h3>第 ${esc(r.round)} 轮 · 待复核</h3><p>确认本轮新的复检采集。服务仍会核对分析结论与关单条件。</p>
    <div class="buttons"><button class="btn primary" type="button" data-act="orderReview" data-id="${esc(o.order_id)}" data-round="${esc(r.round)}" data-decision="confirmed" ${reviewer ? "" : "disabled"}>确认修复</button>
    <button class="btn danger" type="button" data-act="orderReview" data-id="${esc(o.order_id)}" data-round="${esc(r.round)}" data-decision="dismissed" ${reviewer ? "" : "disabled"}>驳回</button></div></section>`).join("");
  const rounds = table(["轮", "维修反馈", "复检运行", "状态", "本轮复核", "结论原因"], subject.rounds.map(r => {
    return [r.round, raw(`${esc(r.feedback.reported_by)}<br><small>${esc(stamp(r.feedback.reported_at))} · ${esc(r.feedback.note)}</small>`),
      r.reinspection_run ? raw(`<a class="mono" href="#workflows/${esc(r.reinspection_run)}">${esc(r.reinspection_run)}</a> ${r.run_state ? wchip(r.run_state) : ""}`) : "—",
      RSTATE[r.state] || r.state, r.review ? (r.review.decision === "confirmed" ? "确认修复" : "驳回") + " · " + r.review.reviewer : "尚无复核记录", reasons((r.conclusion || {}).reasons || []) || "—"];
  }), "尚无维修反馈。");
  return html + `<div class="d-grid"><div class="stack">${card("轮次", rounds + '<p class="hint">本轮复核只接受未疑似的复检采集；复检仍疑似时本轮直接判为未通过。</p>')}${eventsList(subject.events, "order-events-" + o.order_id)}</div><aside class="stack facts">${closureFacts(o, subject.rounds)}</aside></div>`;
}

// ── project audit (P5, D070) / 项目审计（P5，D070）──
function renderAudit() {
  if (!byId("auditList")) return;
  const entries = audit?.entries || [];
  const total = {all: entries.length};
  for (const e of entries) total[auditGroup(e)] = (total[auditGroup(e)] || 0) + 1;
  paint("auditFilters", AUDIT_GROUPS.map(([key, label]) =>
    `<button type="button" class="${key === "denied" ? "denied" : ""}" data-act="auditFilter" data-v="${key}" aria-pressed="${auditFilter === key}">${label}<b>${total[key] || 0}</b></button>`).join(""));
  const shown = entries.filter(e => auditFilter === "all" || auditGroup(e) === auditFilter);
  byId("auditSub").textContent = audit ? (auditBefore ? "更早的记录" : "最新记录") + " · 已加载页 " + entries.length + " 条（筛选仅统计本页）" : "";
  paint("auditList", audit ? table(["时间", "身份", "动作", "对象", "细节"], shown.map(e => [raw(`<time class="nowrap" datetime="${esc(e.at)}" title="${esc(e.at)}">${esc(stamp(e.at))}</time>`), raw(`<span class="nowrap">${esc(e.actor || "—")}</span>`),
    raw(badge(AUDIT[e.action] || e.action, e.action === "access.denied" ? "bad" : e.action.startsWith("mission.approved") ? "ok" : "")),
    auditObject(e.object), raw(`<small>${esc(Object.entries(e.detail || {}).filter(([, v]) => v !== null && v !== "").map(([k, v]) => k + "=" + v).join(" · ").slice(0, 200))}</small>`)]),
    entries.length ? "没有符合筛选的记录。" : "该项目还没有审计记录。", i => shown[i].action === "access.denied" ? "denied" : "") : blank("加载审计记录…", "只有本项目成员能看到本项目的记录。"));
  paint("auditPager", `<div class="buttons" style="margin:0"><button class="btn small" type="button" data-act="auditLatest" ${auditBefore ? "" : "disabled"}>回到最新</button>
    <button class="btn small" type="button" data-act="auditOlder" ${audit?.next ? "" : "disabled"}>更早的记录</button></div>`);
}

// ── actions: every click becomes a named frame the service checks / 操作：每次点击都变成由服务检查的具名帧 ──
const ACTIONS = {
  dismiss: () => notice(""),
  copyHash: async d => {
    try { await navigator.clipboard.writeText(d.hash); notice("已复制完整任务包哈希。"); }
    catch { notice("无法写入剪贴板，请选择并复制上方完整哈希。"); }
  },
  inspectEvidence: d => { selectedEvidence = d.id; renderSubject(); },
  auditFilter: d => { auditFilter = d.v; renderAudit(); },
  auditOlder: () => { if (audit?.next && project()) { auditBefore = audit.next; send({type: "audit_watch", project_id: projectId, before: auditBefore}); } },
  auditLatest: () => { if (project()) { auditBefore = null; send({type: "audit_watch", project_id: projectId, before: null}); } },
  submit: () => submitMission(),
  filter: d => { filter = d.v; renderMissionList(); },
  version: d => { selected = Number(d.v); renderMission(); },
  approve: d => send({type: "approve", mission_id: d.id, version: Number(d.v), package_hash: d.hash}),
  decline: async d => {
    const reason = await ask({title: "驳回 v" + d.v, hint: "原因可选，写入审批记录。", required: false, ok: "驳回"});
    if (reason !== null) send({type: "decline", mission_id: d.id, version: Number(d.v), reason});
  },
  op: d => send({type: "operate", mission_id: d.id, action: d.op, request_id: "op-" + rid()}),
  cancelMission: d => send({type: "operate", mission_id: d.id, action: "cancel", request_id: "op-" + rid()}),
  media: d => requestMedia(d.mission, d.id, true),
  reuse: d => {
    const select = [...(byId("detail").querySelectorAll?.("[data-analyzer]") || [])].find(s => s.dataset.analyzer === d.id);
    if (!select || !business) return;
    send({type: "analysis_submit", project_id: business.project_id, mission_id: d.mission, evidence_id: d.id, analyzer: select.value, request_id: "ui-" + rid()});
    notice("已提交复用分析；结果以候选结论出现在发现与分析列表中。");
  },
  reference: async d => {
    const note = await ask({title: "登记参考外观", hint: "这张已证实的采集代表该资产的正常外观（写入审计）。"});
    if (note && business) send({type: "reference_register", project_id: business.project_id, mission_id: d.mission, evidence_id: d.id, note});
  },
  release: async d => {
    const reason = await ask({title: "解除 " + d.id + " 的维护锁", hint: "原因写入审计。", ok: "解除"});
    if (reason && resources) send({type: "maintenance", project_id: resources.project_id, dock_id: d.id, action: "release", reason});
  },
  start: d => {
    if (!workflows) return;
    const inputs = {};
    for (const select of byId("wfStart").querySelectorAll?.("[data-input]") || []) if (select.dataset.wf === d.id) inputs[select.dataset.input] = select.value;
    expect = {kind: "run", known: new Set(workflows.runs.map(r => r.run_id))};
    send({type: "workflow_start", project_id: workflows.project_id, workflow_id: d.id, request_id: "ui-" + rid(), inputs});
  },
  schedule: async d => {
    const reason = await ask({title: d.action === "enable" ? "启用排班" : "停用排班", hint: "原因写入审计。"});
    if (reason && workflows) send({type: "workflow_schedule", project_id: workflows.project_id, workflow_id: d.id, trigger_id: d.trigger, action: d.action, reason});
  },
  cancelRun: async d => {
    const reason = await ask({title: "取消本次运行", hint: "原因写入审计；已开始的飞行经原通道收尾。", ok: "取消运行"});
    if (reason && workflows) send({type: "workflow_cancel", project_id: workflows.project_id, run_id: d.id, request_id: "ui-" + rid(), reason});
  },
  review: async d => {
    const note = await ask({title: d.decision === "confirmed" ? "确认异常" : "驳回", hint: "说明写入复核记录（可选）。", required: false, ok: d.decision === "confirmed" ? "确认" : "驳回"});
    if (note !== null && workflows) send({type: "workflow_review", project_id: workflows.project_id, run_id: d.id, node_id: d.node, decision: d.decision, request_id: "ui-" + rid(), note});
  },
  wfRepair: async d => {
    const note = await ask({title: "记录维修反馈", hint: "只转为待复检，不关单。"});
    if (note && workflows) send({type: "workflow_repair", project_id: workflows.project_id, order_id: d.id, request_id: "ui-" + rid(), note});
  },
  draft: () => {
    const text = byId("draftText").value.trim();
    if (!text || !workflows) { notice("请先写下流程需求并选择项目。"); return; }
    if (send({type: "workflow_draft", project_id: workflows.project_id, text})) paint("draftResult", '<p class="hint">生成草案中…</p>');
  },
  taskSubmit: () => {
    const select = byId("taskAsset"), option = select.selectedOptions?.[0];
    if (!option || !tasks) { notice("请先选择项目与资产。"); return; }
    expect = {kind: "task", known: new Set(tasks.tasks.map(t => t.task_id))};
    send({type: "task_submit", project_id: tasks.project_id, asset_id: option.value, volume_id: option.dataset.volume, candidates: [],
      priority: Number(byId("taskPriority").value), request_id: "ui-" + rid()});
  },
  cancelTask: async d => {
    const reason = await ask({title: "取消任务单", hint: "原因写入审计。", ok: "取消任务单"});
    if (reason && tasks) send({type: "task_cancel", project_id: tasks.project_id, task_id: d.id, request_id: "ui-" + rid(), reason});
  },
  decide: async d => {
    const note = await ask({title: d.decision === "confirmed" ? "确认异常并建单" : "驳回发现", hint: "说明写入复核记录（可选）。", required: false, ok: d.decision === "confirmed" ? "确认" : "驳回"});
    if (note !== null && business) send({type: "finding_review", project_id: business.project_id, finding_id: d.id, decision: d.decision, request_id: "ui-" + rid(), note});
  },
  orderRepair: async d => {
    const note = await ask({title: "记录维修反馈", hint: "只启动复检，不关单；复检飞行仍逐次审批。"});
    if (note && business) send({type: "order_repair", project_id: business.project_id, order_id: d.id, request_id: "ui-" + rid(), note});
  },
  orderReview: async d => {
    const note = await ask({title: d.decision === "confirmed" ? "确认修复" : "驳回修复", hint: "说明写入复核记录（可选）。", required: false, ok: d.decision === "confirmed" ? "确认" : "驳回"});
    if (note !== null && business) send({type: "order_review", project_id: business.project_id, order_id: d.id, round: Number(d.round), decision: d.decision, request_id: "ui-" + rid(), note});
  },
};
function act(name, data = {}) { const handler = ACTIONS[name]; return handler ? handler(data) : undefined; }

document.addEventListener("click", event => {
  const target = event.target?.closest?.("[data-act]");
  if (!target || target.disabled) return;
  event.preventDefault();
  // One click, one frame: a repeated click would only earn a stale-version refusal. / 一次点击一帧：重复点击只会得到过期版本的拒绝。
  if (target.hasAttribute("data-once")) { target.disabled = true; setTimeout(() => { target.disabled = false; }, 2500); }
  act(target.dataset.act, target.dataset);
});
document.addEventListener("change", event => {
  const id = event.target?.id;
  if (id === "project") selectProject();
  else if (id === "missionScope") { renderMissionList(); renderOverview(); }
  else if (id === "wfTemplate") { wfChoice = event.target.value; renderWorkflows(); }
});
document.addEventListener("keydown", event => {
  if (event.key === "Enter" && (event.ctrlKey || event.metaKey) && event.target?.id === "text") { event.preventDefault(); submitMission(); }
});
window.addEventListener("hashchange", () => route());
// The mission list is not pushed; refresh it with the existing list frame. / 任务列表不推送，用已有的 list 帧刷新。
setInterval(() => { if (hello && document.visibilityState !== "hidden") send({type: "list"}, true); }, 5000);
setInterval(() => { if (hello && nav.ws === "fleet" && nav.id && project()) send({type: "resource", project_id: projectId, resource_id: nav.id}, true); }, 3000);

route();
connect();
