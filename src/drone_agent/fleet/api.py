"""Local mission-service API: one JSON request per line over a private Unix socket.

Callers are the console, the A2A gateway, harness scripts and dock backends; each passes the identity it
authenticated (`tailnet:<login>`, `local:<user>`, `a2a:<client>`, `harness:<run>` or `dock:<backend>`) and the
trust level that identity has. The socket itself is the boundary: it lives in a private directory shared only with
those callers. Permissions are decided here with the shared scope table (D033) and again inside the service, so an
A2A identity can submit and read but never approve, decline or operate, whatever the gateway sends. With an
operations catalog (P1, D055) the service also checks the caller's project role on every mission, resource and
media call; project-scoped methods carry an explicit `project_id`, and a `dock:` identity can only report. With a
workflow catalog (P2, D057) `workflows.*` methods start, cancel and schedule runs, record reviews and repair
feedback, and an `event:` identity bound in a template can only raise that template's events.

本机任务服务 API：经私有 Unix 套接字每行一个 JSON 请求。调用方是控制台、A2A 网关与编排脚本；各自传入
已认证的身份（`tailnet:<login>`、`local:<user>`、`a2a:<client>` 或 `harness:<run>`）及其信任级别。套接字
本身就是边界：它位于只与这些调用方共享的私有目录中。权限在这里按共用 scope 表判定（D033），服务内部
再判定一次，因此无论网关发送什么，A2A 身份都只能提交与读取，永远不能审批、驳回或操作。调用方还包括机场后端
（`dock:<backend>`）。带运营目录时（P1，D055），服务还对每个任务、资源与媒体调用检查调用方的项目角色；按项目
的方法显式携带 `project_id`，`dock:` 身份只能报告。带工作流目录时（P2，D057），`workflows.*` 方法启动、取消与排班运行，
记录复核与维修反馈；模板中绑定的 `event:` 身份只能触发该模板的事件。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import uuid
from pathlib import Path

from drone_agent.admission.models import MissionRequest, RequestChannel
from drone_agent.contracts import utcnow
from drone_agent.eval.viewer import png_data_uri
from drone_agent.fleet.service import MissionService, ServiceError
from drone_agent.runtime.issues import ISSUE_CODES, IssueLayer, issue
from drone_agent.runtime.permission import (
    MISSION_APPROVE,
    MISSION_OPERATE,
    MISSION_READ,
    MISSION_SUBMIT,
    RESOURCE_MAINTAIN,
    RESOURCE_READ,
    RESOURCE_REPORT,
    TRUST_LEVEL_CAPS,
    WORKFLOW_DRAFT,
    WORKFLOW_EVENT,
    WORKFLOW_READ,
    WORKFLOW_REVIEW,
    WORKFLOW_RUN,
    Caller,
    TrustLevel,
    authorize,
)

MAX_REQUEST = 64 * 1024
MAX_RESPONSE = 16 * 1024 * 1024
# Method -> (required scope, exact parameter set). / 方法 -> （所需 scope，精确参数集）。
METHODS: dict[str, tuple[str, frozenset[str]]] = {
    "submit": (MISSION_SUBMIT, frozenset({"text", "volume_id", "asset_ids", "idempotency_key"})),
    "approve": (MISSION_APPROVE, frozenset({"mission_id", "version", "package_hash"})),
    "decline": (MISSION_APPROVE, frozenset({"mission_id", "version", "reason"})),
    "operate": (MISSION_OPERATE, frozenset({"mission_id", "action", "request_id"})),
    "view": (MISSION_READ, frozenset({"mission_id"})),
    "summary": (MISSION_READ, frozenset({"mission_id"})),
    "list": (MISSION_READ, frozenset()),
    "robots": (MISSION_READ, frozenset()),
    "media": (MISSION_READ, frozenset({"mission_id", "evidence_id"})),
    "health": (MISSION_READ, frozenset()),
    # Gateways record the requests they refused before reaching the service. / 网关记录在到达服务前拒绝的请求。
    "audit": (MISSION_READ, frozenset({"code", "message"})),
    # P1 (D055): explicit project scope; the service checks the caller's role in that project.
    # P1（D055）：显式项目范围；服务检查调用方在该项目中的角色。
    "projects": (MISSION_READ, frozenset()),
    "missions.submit": (MISSION_SUBMIT, frozenset({"project_id", "robot_id", "text", "volume_id", "asset_ids",
                                                   "idempotency_key"})),
    "resources.list": (RESOURCE_READ, frozenset({"project_id"})),
    "resources.get": (RESOURCE_READ, frozenset({"project_id", "resource_id"})),
    "resources.eligibility": (RESOURCE_READ, frozenset({"project_id", "robot_id"})),
    "resources.maintenance": (RESOURCE_MAINTAIN, frozenset({"project_id", "dock_id", "action", "reason"})),
    # Dock backends only; the catalog binds each `dock:` identity to its docks. / 仅机场后端；目录把每个 `dock:` 身份绑定到其机场。
    "docks.report": (RESOURCE_REPORT, frozenset({"report"})),
    "docks.actions": (RESOURCE_REPORT, frozenset({"dock_id"})),
    "docks.ack": (RESOURCE_REPORT, frozenset({"action_id", "accepted", "reason"})),
    # P2 (D057): the engine checks the project role; `workflows.event` only for the template's bound `event:` source.
    # P2（D057）：引擎检查项目角色；`workflows.event` 只给模板绑定的 `event:` 来源。
    "workflows.list": (WORKFLOW_READ, frozenset({"project_id"})),
    "workflows.get": (WORKFLOW_READ, frozenset({"project_id", "run_id"})),
    "workflows.start": (WORKFLOW_RUN, frozenset({"project_id", "workflow_id", "request_id", "inputs"})),
    "workflows.cancel": (WORKFLOW_RUN, frozenset({"project_id", "run_id", "request_id", "reason"})),
    "workflows.schedule": (WORKFLOW_RUN, frozenset({"project_id", "workflow_id", "trigger_id", "action", "reason"})),
    "workflows.review": (WORKFLOW_REVIEW, frozenset({"project_id", "run_id", "node_id", "decision", "request_id",
                                                     "note"})),
    "workflows.repair": (WORKFLOW_RUN, frozenset({"project_id", "order_id", "request_id", "note"})),
    "workflows.draft": (WORKFLOW_DRAFT, frozenset({"project_id", "text"})),
    "workflows.event": (WORKFLOW_EVENT, frozenset({"project_id", "workflow_id", "trigger_id", "event_type", "event_id",
                                                   "payload"})),
}
CHANNELS = {"tailnet": RequestChannel.CONSOLE, "local": RequestChannel.CONSOLE, "a2a": RequestChannel.A2A,
            "harness": RequestChannel.HARNESS}
# The only methods a backend may call, by the kind of backend. / 后端只能调用的方法，按后端种类区分。
BACKEND_METHODS = frozenset({"docks.report", "docks.actions", "docks.ack", "workflows.event"})
BACKEND_PREFIXES = {"docks.report": "dock", "docks.actions": "dock", "docks.ack": "dock", "workflows.event": "event"}
# Third-party callers submit, read the summary of their own missions and nothing else.
# 第三方调用方只能提交并读取自己任务的摘要，别无其他。
THIRD_PARTY_METHODS = {"submit", "summary", "health", "audit"}


def caller(actor: str, trust: str) -> Caller:
    """The caller for an authenticated actor; the prefix decides the trust ceiling. / 由身份前缀决定信任上限。"""
    prefix = actor.split(":", 1)[0] if ":" in actor else ""
    if prefix in ("dock", "event") and actor.split(":", 1)[1]:
        # A dock backend or an event source is never a person, whatever trust it claims.
        # 机场后端或事件源永远不是人，无论它声明何种信任。
        return Caller(actor, TrustLevel.BACKEND, TRUST_LEVEL_CAPS[TrustLevel.BACKEND])
    if prefix not in CHANNELS or not actor.split(":", 1)[1]:
        return Caller(actor or "anonymous", TrustLevel.ANONYMOUS, frozenset({MISSION_READ}))
    level = TrustLevel(trust)
    if level is TrustLevel.BACKEND:
        raise ValueError("only dock identities are backends")
    if prefix == "a2a" and level is not TrustLevel.THIRD_PARTY:
        level = TrustLevel.THIRD_PARTY
    return Caller(actor, level, TRUST_LEVEL_CAPS[level])


def _error(code: str, message: str = "") -> dict:
    return {"ok": False, "issue": issue(code, message).model_dump(mode="json")}


async def dispatch(service: MissionService, request: dict) -> dict:
    """Validate, authorize and execute one API request. / 校验、授权并执行一个 API 请求。"""
    if not isinstance(request, dict) or set(request) != {"method", "actor", "trust", "params"}:
        return _error("service.invalid_request", "expected method, actor, trust and params")
    method, actor, params = request["method"], request["actor"], request["params"]
    if method not in METHODS or not isinstance(params, dict) or set(params) != METHODS[method][1]:
        return _error("service.invalid_request", f"unknown method or parameters for {method}")
    try:
        who = caller(str(actor), str(request["trust"]))
    except ValueError:
        return _error("auth.identity_missing", "unknown trust level")
    decision = authorize(who, [METHODS[method][0]])
    if not decision.allowed:
        return _error(decision.code, decision.reason)
    if who.trust_level is TrustLevel.THIRD_PARTY and method not in THIRD_PARTY_METHODS:
        return _error("auth.method_not_allowed", f"{method} is not available to external agents")
    if method != "health" and (who.trust_level is TrustLevel.BACKEND) != (method in BACKEND_METHODS):
        return _error("auth.method_not_allowed", f"{method} is not available to this identity")
    if method in BACKEND_PREFIXES and who.identity.split(":", 1)[0] != BACKEND_PREFIXES[method]:
        return _error("auth.method_not_allowed", f"{method} is not available to this kind of backend")
    try:
        if method == "summary" and who.trust_level is TrustLevel.THIRD_PARTY:
            mission = service.ledger.mission(params["mission_id"])
            owner = mission and service.ledger.request(mission["request_id"])["requested_by"]
            if owner != who.identity:
                raise ServiceError("service.not_found", params["mission_id"])
        return {"ok": True, "result": await _call(service, method, params, who)}
    except ServiceError as error:
        return {"ok": False, "issue": error.issue.model_dump(mode="json")}
    except (ValueError, KeyError, TypeError) as error:
        return _error("service.invalid_request", str(error)[:300])


async def _call(service: MissionService, method: str, params: dict, who: Caller):
    if method == "submit":
        request = MissionRequest(
            request_id="req-" + uuid.uuid4().hex[:16], text=params["text"], requested_by=who.identity,
            trust_level=who.trust_level, channel=CHANNELS[who.identity.split(":", 1)[0]],
            approved_volume_id=params["volume_id"], asset_ids=list(params["asset_ids"] or []),
            idempotency_key=params["idempotency_key"], received_at=utcnow())
        view = await service.submit(request, caller=who)
        mission_id = view["mission"]["mission_id"]
        return service.summary(mission_id, caller=who) if who.trust_level is TrustLevel.THIRD_PARTY else view
    if method == "missions.submit":
        request = MissionRequest(
            request_id="req-" + uuid.uuid4().hex[:16], text=params["text"], requested_by=who.identity,
            trust_level=who.trust_level, channel=CHANNELS[who.identity.split(":", 1)[0]],
            approved_volume_id=params["volume_id"], asset_ids=list(params["asset_ids"] or []),
            idempotency_key=params["idempotency_key"], received_at=utcnow())
        return await service.submit_bound(request, project_id=str(params["project_id"]),
                                          robot_id=str(params["robot_id"]), caller=who)
    if method == "approve":
        return service.approve(params["mission_id"], int(params["version"]), approver=who.identity,
                               package_hash=params["package_hash"], caller=who)
    if method == "decline":
        return service.decline(params["mission_id"], int(params["version"]), approver=who.identity,
                               reason=str(params["reason"]), caller=who)
    if method == "operate":
        return service.operate(params["mission_id"], params["action"], requested_by=who.identity,
                               request_id=params["request_id"], caller=who)
    if method == "view":
        return service.view(params["mission_id"], caller=who)
    if method == "summary":
        return service.summary(params["mission_id"], caller=who)
    if method == "list":
        return service.missions(caller=who)
    if method == "robots":
        return service.robots_view(caller=who)
    if method == "projects":
        return service.projects(caller=who)
    if method == "resources.list":
        return service.resources(str(params["project_id"]), caller=who)
    if method == "resources.get":
        return service.resource(str(params["project_id"]), str(params["resource_id"]), caller=who)
    if method == "resources.eligibility":
        return service.eligibility(str(params["project_id"]), str(params["robot_id"]), caller=who)
    if method == "resources.maintenance":
        return service.maintenance(str(params["project_id"]), str(params["dock_id"]), str(params["action"]),
                                   str(params["reason"])[:300], caller=who)
    if method == "docks.report":
        if not isinstance(params["report"], dict):
            raise ServiceError("service.invalid_request", "report must be an object")
        return service.dock_report(who.identity, params["report"])
    if method == "docks.actions":
        return service.dock_actions(who.identity, str(params["dock_id"]))
    if method == "docks.ack":
        return service.dock_ack(who.identity, str(params["action_id"]), params["accepted"] is True,
                                str(params["reason"])[:300])
    if method.startswith("workflows."):
        return await _workflow_call(service, method, params, who)
    if method == "audit":
        if ISSUE_CODES.get(params["code"]) is not IssueLayer.AUTH:
            raise ServiceError("service.invalid_request", "only authentication issues are audited here")
        service.ledger.record_issue(issue(params["code"], f"{who.identity}: {params['message']}"[:300]))
        return {"recorded": params["code"]}
    if method == "media":
        row = service.media_row(params["mission_id"], params["evidence_id"], caller=who)
        media = service.hub.media(row["media_path"]) if row and row["media_path"] else None
        if media is None:
            raise ServiceError("service.not_found", params["evidence_id"])
        return {"evidence_id": row["evidence_id"], "png": png_data_uri(media, row["width"], row["height"])}
    return {"status": "ready", "signer_key_id": service.key.key_id, "robot_id": service.robot_id,
             "planner": getattr(service.planner, "label", None) if service.planner else None,
             "execution_backend": service.source.execution_backend,
             "source_sha": os.environ.get("DRONE_SOURCE_SHA", "uncommitted"),
             "catalog": {"catalog_id": service.ops.catalog.catalog_id, "sha256": service.ops.catalog.sha256}
             if service.ops is not None else None,
             "workflows": {"catalog_id": service.workflows.catalog.catalog_id,
                           "sha256": service.workflows.catalog.sha256}
             if getattr(service, "workflows", None) is not None else None}


async def _workflow_call(service: MissionService, method: str, params: dict, who: Caller):
    engine = getattr(service, "workflows", None)
    if engine is None:
        raise ServiceError("service.invalid_request", "no workflow catalog is configured")
    if method == "workflows.list":
        return engine.list_view(who, params["project_id"])
    if method == "workflows.get":
        return engine.run_view(who, params["project_id"], params["run_id"])
    if method == "workflows.start":
        return engine.start(who, params["project_id"], params["workflow_id"], params["request_id"], params["inputs"])
    if method == "workflows.cancel":
        return engine.cancel(who, params["project_id"], params["run_id"], params["request_id"], params["reason"])
    if method == "workflows.schedule":
        return engine.set_schedule(who, params["project_id"], params["workflow_id"], params["trigger_id"],
                                   params["action"], params["reason"])
    if method == "workflows.review":
        return engine.review(who, params["project_id"], params["run_id"], params["node_id"], params["decision"],
                             params["request_id"], params["note"])
    if method == "workflows.repair":
        return engine.repair(who, params["project_id"], params["order_id"], params["request_id"], params["note"])
    if method == "workflows.draft":
        return await engine.draft(who, params["project_id"], params["text"])
    return engine.event(who.identity, params["project_id"], params["workflow_id"], params["trigger_id"],
                        params["event_type"], params["event_id"], params["payload"])


async def serve_api(service: MissionService, path: Path):
    """Serve the API on a Unix socket readable only by the owner and group. / 在仅属主与属组可读写的 Unix 套接字上服务。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            line = await asyncio.wait_for(reader.readline(), 10)
            if len(line) > MAX_REQUEST or not line.endswith(b"\n"):
                response = _error("service.invalid_request", "invalid frame")
            else:
                response = await dispatch(service, json.loads(line))
        except (ValueError, TimeoutError, asyncio.LimitOverrunError) as error:
            response = _error("service.invalid_request", type(error).__name__)
        writer.write(json.dumps(response, ensure_ascii=False).encode() + b"\n")
        try:
            await writer.drain()
        finally:
            writer.close()

    server = await asyncio.start_unix_server(handle, path=str(path), limit=MAX_REQUEST + 1)
    os.chmod(path, 0o660)
    return server


class ApiClient:
    """Async client for the API socket. / API 套接字的异步客户端。"""

    def __init__(self, path: Path, *, actor: str, trust: str):
        self.path, self.actor, self.trust = path, actor, trust

    async def call(self, method: str, **params) -> dict:
        reader, writer = await asyncio.open_unix_connection(str(self.path), limit=MAX_RESPONSE + 1)
        try:
            request = {"method": method, "actor": self.actor, "trust": self.trust, "params": params}
            writer.write(json.dumps(request, ensure_ascii=False).encode() + b"\n")
            await writer.drain()
            line = await asyncio.wait_for(reader.readline(), 120)
        finally:
            writer.close()
        if not line.endswith(b"\n"):
            raise RuntimeError("incomplete API response")
        return json.loads(line)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket", type=Path, default=Path("/run/mission/api.sock"))
    parser.add_argument("--actor", required=True)
    parser.add_argument("--trust", default=TrustLevel.FIRST_PARTY.value)
    parser.add_argument("method", choices=sorted(METHODS))
    parser.add_argument("params", nargs="?", default="{}")
    args = parser.parse_args()
    result = asyncio.run(ApiClient(args.socket, actor=args.actor, trust=args.trust).call(args.method,
                                                                                         **json.loads(args.params)))
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
