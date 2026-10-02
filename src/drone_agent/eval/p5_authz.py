"""The P5 authorization matrix (D070): every API method, every hri.v0 upstream frame, media reads and subscription
pushes, as every identity class, against the expectation the scope table gives.

The world is the desk platform in S0 (`eval/p5_desk_world.py`), seeded with a mission, its evidence, a workflow run, a
finding, an order, an analysis job and a scheduler task. Identity classes: a first-party non-member, a member of
another project only, the five project roles (viewer, operator with approver, approver, reviewer, admin), an external
agent (A2A), an anonymous caller, a dock backend and an event source. For each API method the expectation is derived
from `fleet.api.METHODS`, the trust caps and the member list, never from the service's answer: a forbidden call that
succeeds or returns data is an escape, and a non-member that learns more than `service.not_found` about a project is an
escape too. Each upstream frame then runs through a real page session (`console.mission.Session`) followed by every
periodic push; any data frame about a project the identity cannot read is an escape. The matrix decides nothing about
the service beyond these rules and records every row.

P5 授权矩阵（D070）：API 的每个方法、hri.v0 的每种上行帧、媒体读取与订阅推送，以每类身份调用，并与按 scope 表推导的期望
比较。世界是 S0 中的任务台平台（`eval/p5_desk_world.py`），预置一个任务及其证据、一个工作流运行、一个发现、一张工单、一个
分析作业与一个调度任务单。身份类别：第一方非成员、只属于其他项目的成员、五种项目角色（viewer、带 approver 的 operator、
approver、reviewer、admin）、外部 agent（A2A）、匿名调用方、机场后端与事件源。每个 API 方法的期望由 `fleet.api.METHODS`、
信任上限与成员列表推导，从不取自服务的应答：被禁止的调用成功或返回数据即为逃逸，非成员获知某项目多于 `service.not_found`
的信息也是逃逸。随后每种上行帧都经真实的页面会话（`console.mission.Session`）执行，再执行每个周期推送；任何关于该身份不可读
项目的数据帧都是逃逸。矩阵除这些规则外不对服务做任何判断，并记录每一行。
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

import yaml

from drone_agent.eval.p4_world import confirm_all
from drone_agent.eval.p5_desk_world import P5DeskWorld
from drone_agent.fleet.api import BACKEND_METHODS, METHODS, THIRD_PARTY_METHODS, dispatch
from drone_agent.runtime.permission import TRUST_LEVEL_CAPS, TrustLevel, role_scopes

MEMBERS = "configs/sites/p5_members_desk_s0.yaml"
TARGET = "campus_s1"
IDENTITIES = {
    "non_member": ("harness:p5-stranger", "first_party"),
    "other_project": ("harness:p5-harbor", "first_party"),
    "viewer": ("harness:p5-viewer", "first_party"),
    "operator": ("harness:p5-operator", "first_party"),
    "approver": ("harness:p5-approver", "first_party"),
    "reviewer": ("harness:p5-reviewer", "first_party"),
    "admin": ("harness:p5-admin", "first_party"),
    "a2a": ("a2a:p5-client", "third_party"),
    "anonymous": ("", "anonymous"),
    "dock_backend": ("dock:p5-desk-fleet", "backend"),
    "event_backend": ("event:road-reports", "backend"),
}
# Calls that are not about one project; their answers are checked for data of unreadable projects instead.
# 不针对单个项目的调用；改为检查其应答中是否含不可读项目的数据。
GLOBAL = ("health", "list", "robots", "projects", "audit")
DATA_FRAMES = ("mission", "missions", "media", "resources", "resource", "workflows", "workflow", "tasks", "task",
               "business", "finding", "order", "audit", "workflow_draft")


class Matrix:
    def __init__(self, world: P5DeskWorld, repo: Path):
        self.world, self.repo = world, repo
        members = yaml.safe_load((repo / MEMBERS).read_text(encoding="utf-8"))["members"]
        self.roles: dict[tuple[str, str], set[str]] = {}
        for entry in members:
            self.roles.setdefault((entry["principal"], entry["project_id"]), set()).update(entry["roles"])
        self.objects: dict = {}
        self.rows: list[dict] = []

    # ── expectations / 期望 ──

    def readable(self, actor: str, trust: str) -> set[str]:
        if trust != "first_party" or not actor:
            return set()
        found = {p for (a, p), roles in self.roles.items() if a == actor and "viewer" in role_scopes_names(roles)}
        return found | {"legacy_m2"}

    def expect(self, actor: str, trust: str, method: str, project: str) -> str:
        """`allowed`, `forbidden` or `global`, from the tables only. / 只按表推导 allowed、forbidden 或 global。"""
        if method in GLOBAL:
            return "global"
        prefix = actor.split(":", 1)[0] if ":" in actor else ""
        if prefix in ("dock", "event"):
            wanted = {"dock": ("docks.report", "docks.actions", "docks.ack"), "event": ("workflows.event",)}[prefix]
            return "allowed" if method in wanted else "forbidden"
        if method in BACKEND_METHODS:
            return "forbidden"
        if trust == "third_party":
            return "allowed" if method in THIRD_PARTY_METHODS and method in ("submit",) else "forbidden"
        if trust != "first_party" or not actor:
            return "forbidden"
        scopes = role_scopes(self.roles.get((actor, project), set())) & TRUST_LEVEL_CAPS[TrustLevel.FIRST_PARTY]
        return "allowed" if METHODS[method][0] in scopes else "forbidden"

    def member_anywhere(self, actor: str) -> bool:
        return any(a == actor for a, _ in self.roles)

    def project_of_value(self, key: str, value) -> str | None:
        if key == "project_id":
            return value if isinstance(value, str) else None
        if key == "mission_id" and isinstance(value, str) and value.startswith("m-"):
            try:
                return self.world.service.project_of(value)
            except Exception:  # unknown missions name no project / 未知任务不指明项目
                return None
        return None

    def projects_in(self, value) -> set[str]:
        """Every project a JSON value mentions, directly or through a mission id. / JSON 值直接或经任务号提及的全部项目。"""
        found: set[str] = set()
        if isinstance(value, dict):
            for key, item in value.items():
                project = self.project_of_value(key, item)
                if project:
                    found.add(project)
                found |= self.projects_in(item)
        elif isinstance(value, list):
            for item in value:
                found |= self.projects_in(item)
        return found

    # ── the world / 世界 ──

    async def seed(self) -> None:
        w = self.world
        run = await w.start("appearance_watch", {"asset": "asset_red"}, project=TARGET)
        await w.run_business([run], until=lambda: w.final(run) and w.finding_of(run) is not None,
                             what="campus run with a finding", decisions=confirm_all)
        mission = w.missions_of(run)[0]["mission_id"]
        evidence = w.ledger.evidence(mission)[0]["evidence_id"]
        finding = w.finding_of(run)
        order = w.order_of(finding)
        job = w.job_of(run, "analyze")
        waiting = await w.start("appearance_watch", {"asset": "asset_blue"}, project=TARGET)
        await w.until(lambda: any(m["status"] == "awaiting_approval" for m in w.missions_of(waiting)),
                      "a mission awaiting approval", 30)
        pending = w.missions_of(waiting)[0]["mission_id"]
        fleet = await w.start("fleet_watch", {"asset": "asset_red"}, project="fleet_s0")
        await w.run_business([fleet], until=lambda: w.final(fleet), what="fleet run")
        task = w.service.scheduler.store.tasks(requested_by=f"workflow:{fleet}")[0]["task_id"]
        self.objects = {"mission": mission, "evidence": evidence, "run": run, "finding": finding,
                        "order": order["order_id"] if order else "ord-none", "job": job["job_id"] if job else "job-none",
                        "pending": pending, "waiting_run": waiting, "task": task}

    def params(self, method: str, n: int) -> tuple[str, dict]:
        o, p = self.objects, TARGET
        table = {
            "submit": (p, {"text": "Inspect the red equipment marker east of the pad.", "volume_id": "campus_training",
                           "asset_ids": [], "idempotency_key": f"authz-{n}"}),
            "approve": (p, {"mission_id": o["pending"], "version": 1, "package_hash": "0" * 64}),
            "decline": (p, {"mission_id": o["pending"], "version": 1, "reason": "authz probe"}),
            "operate": (p, {"mission_id": o["mission"], "action": "pause", "request_id": f"authz-{n}"}),
            "view": (p, {"mission_id": o["mission"]}), "summary": (p, {"mission_id": o["mission"]}),
            "list": (p, {}), "robots": (p, {}), "health": (p, {}), "projects": (p, {}),
            "media": (p, {"mission_id": o["mission"], "evidence_id": o["evidence"]}),
            "audit": (p, {"code": "auth.identity_missing", "message": "authz probe"}),
            "missions.submit": (p, {"project_id": p, "robot_id": "uav_01", "text": "Inspect the red marker.",
                                    "volume_id": "campus_training", "asset_ids": [], "idempotency_key": f"authz-{n}"}),
            "resources.list": (p, {"project_id": p}),
            "resources.get": (p, {"project_id": p, "resource_id": "dock_s1"}),
            "resources.eligibility": (p, {"project_id": p, "robot_id": "uav_01"}),
            "resources.maintenance": (p, {"project_id": p, "dock_id": "dock_s1", "action": "unlock",
                                          "reason": "authz probe"}),
            "docks.report": (p, {"report": {"dock_id": "dock_fa"}}),
            "docks.actions": (p, {"dock_id": "dock_fa"}),
            "docks.ack": (p, {"action_id": "act-none", "accepted": True, "reason": ""}),
            "workflows.list": (p, {"project_id": p}),
            "workflows.get": (p, {"project_id": p, "run_id": o["run"]}),
            "workflows.start": (p, {"project_id": p, "workflow_id": "appearance_watch", "request_id": f"authz-{n}",
                                    "inputs": {"asset": "asset_blue"}}),
            "workflows.cancel": (p, {"project_id": p, "run_id": o["waiting_run"], "request_id": f"authz-{n}",
                                     "reason": "authz probe"}),
            "workflows.schedule": (p, {"project_id": p, "workflow_id": "asset_check", "trigger_id": "daily_0900",
                                       "action": "disable", "reason": "authz probe"}),
            "workflows.review": (p, {"project_id": p, "run_id": o["run"], "node_id": "review", "decision": "dismissed",
                                     "request_id": f"authz-{n}", "note": "authz probe"}),
            "workflows.repair": (p, {"project_id": p, "order_id": o["order"], "request_id": f"authz-{n}",
                                     "note": "authz probe"}),
            "workflows.draft": (p, {"project_id": p, "text": "inspect the red marker every morning"}),
            "workflows.event": (p, {"project_id": p, "workflow_id": "road_watch", "trigger_id": "road_report",
                                    "event_type": "road.report", "event_id": f"authz-{n}",
                                    "payload": {"segment": "road_north"}}),
            "tasks.submit": ("fleet_s0", {"project_id": "fleet_s0", "asset_id": "asset_red",
                                          "volume_id": "campus_training", "candidates": [], "priority": 0,
                                          "idempotency_key": f"authz-{n}"}),
            "tasks.list": ("fleet_s0", {"project_id": "fleet_s0"}),
            "tasks.get": ("fleet_s0", {"project_id": "fleet_s0", "task_id": o["task"]}),
            "tasks.cancel": ("fleet_s0", {"project_id": "fleet_s0", "task_id": o["task"], "request_id": f"authz-{n}",
                                          "reason": "authz probe"}),
            "business.summary": (p, {"project_id": p}), "findings.list": (p, {"project_id": p}),
            "findings.get": (p, {"project_id": p, "finding_id": o["finding"]}),
            "findings.review": (p, {"project_id": p, "finding_id": o["finding"], "decision": "dismissed",
                                    "request_id": f"authz-{n}", "note": "authz probe"}),
            "orders.list": (p, {"project_id": p}), "orders.get": (p, {"project_id": p, "order_id": o["order"]}),
            "orders.repair": (p, {"project_id": p, "order_id": o["order"], "request_id": f"authz-{n}",
                                  "note": "authz probe"}),
            "orders.review": (p, {"project_id": p, "order_id": o["order"], "round": 1, "decision": "dismissed",
                                  "request_id": f"authz-{n}", "note": "authz probe"}),
            "analysis.list": (p, {"project_id": p}), "analysis.get": (p, {"project_id": p, "job_id": o["job"]}),
            "analysis.submit": (p, {"project_id": p, "mission_id": o["mission"], "evidence_id": o["evidence"],
                                    "analyzer": "signature_s1_v1", "request_id": f"authz-{n}"}),
            "analysis.media": (p, {"project_id": p, "asset": "asset_red"}),
            "references.list": (p, {"project_id": p}),
            "references.register": (p, {"project_id": p, "mission_id": o["mission"], "evidence_id": o["evidence"],
                                        "note": "authz probe"}),
            "references.revoke": (p, {"project_id": p, "reference_id": "ref-none"}),
            "audit.list": (p, {"project_id": p, "before": None, "limit": 20}),
        }
        return table[method]

    # ── the API rows / API 行 ──

    async def api_matrix(self) -> None:
        n = 0
        for method in METHODS:
            for name, (actor, trust) in IDENTITIES.items():
                n += 1
                project, params = self.params(method, n)
                expected = self.expect(actor, trust, method, project)
                response = await dispatch(self.world.service, {"method": method, "actor": actor, "trust": trust,
                                                               "params": params})
                self.rows.append(self.judge_api(name, actor, trust, method, project, expected, response))

    def judge_api(self, name, actor, trust, method, project, expected, response) -> dict:
        ok, code = bool(response.get("ok")), (response.get("issue") or {}).get("code")
        result = response.get("result")
        escape = None
        if expected == "forbidden":
            if ok or result is not None:
                escape = "forbidden_call_answered"
            elif trust == "first_party" and actor and project not in self.readable(actor, trust) \
                    and method not in BACKEND_METHODS and code not in ("service.not_found",):
                escape = "existence_leak"
        elif expected == "global" and ok:
            leaked = self.projects_in(result) - self.readable(actor, trust)
            if leaked:
                escape = f"global_answer_names:{','.join(sorted(leaked))}"
        mismatch = expected == "forbidden" and escape is None and trust == "first_party" and \
            method not in BACKEND_METHODS and project in self.readable(actor, trust) and code != "auth.project_denied"
        return {"layer": "api", "identity": name, "method": method, "project": project, "expected": expected,
                "ok": ok, "code": code, "escape": escape, "code_mismatch": mismatch}

    # ── the page frames and pushes / 页面帧与推送 ──

    def frames(self, n: int) -> list[tuple[str, dict, str, str]]:
        """(frame, message, method, project). / （帧，消息，方法，项目）。"""
        o, p = self.objects, TARGET
        rid = f"authz-frame-{n}"
        return [
            ("list", {}, "list", p),
            ("watch", {"mission_id": o["mission"]}, "view", p),
            ("media", {"mission_id": o["mission"], "evidence_id": o["evidence"]}, "media", p),
            ("resources", {"project_id": p}, "resources.list", p),
            ("resource", {"project_id": p, "resource_id": "dock_s1"}, "resources.get", p),
            ("maintenance", {"project_id": p, "dock_id": "dock_s1", "action": "unlock", "reason": "probe"},
             "resources.maintenance", p),
            ("text", {"project_id": p, "robot_id": "uav_01", "text": "Inspect the red marker.",
                      "volume_id": "campus_training", "asset_ids": [], "rid": rid}, "missions.submit", p),
            ("workflows", {"project_id": p}, "workflows.list", p),
            ("workflow_watch", {"project_id": p, "run_id": o["run"]}, "workflows.get", p),
            ("workflow_start", {"project_id": p, "workflow_id": "appearance_watch", "request_id": rid,
                                "inputs": {"asset": "asset_blue"}}, "workflows.start", p),
            ("workflow_cancel", {"project_id": p, "run_id": o["waiting_run"], "request_id": rid, "reason": "probe"},
             "workflows.cancel", p),
            ("workflow_schedule", {"project_id": p, "workflow_id": "asset_check", "trigger_id": "daily_0900",
                                   "action": "disable", "reason": "probe"}, "workflows.schedule", p),
            ("workflow_review", {"project_id": p, "run_id": o["run"], "node_id": "review", "decision": "dismissed",
                                 "request_id": rid, "note": "probe"}, "workflows.review", p),
            ("workflow_repair", {"project_id": p, "order_id": o["order"], "request_id": rid, "note": "probe"},
             "workflows.repair", p),
            ("workflow_draft", {"project_id": p, "text": "inspect the red marker"}, "workflows.draft", p),
            ("tasks_watch", {"project_id": "fleet_s0"}, "tasks.list", "fleet_s0"),
            ("task_watch", {"project_id": "fleet_s0", "task_id": o["task"]}, "tasks.get", "fleet_s0"),
            ("task_submit", {"project_id": "fleet_s0", "asset_id": "asset_red", "volume_id": "campus_training",
                             "candidates": [], "priority": 0, "request_id": rid}, "tasks.submit", "fleet_s0"),
            ("task_cancel", {"project_id": "fleet_s0", "task_id": o["task"], "request_id": rid, "reason": "probe"},
             "tasks.cancel", "fleet_s0"),
            ("business_watch", {"project_id": p}, "business.summary", p),
            ("finding_watch", {"project_id": p, "finding_id": o["finding"]}, "findings.get", p),
            ("order_watch", {"project_id": p, "order_id": o["order"]}, "orders.get", p),
            ("finding_review", {"project_id": p, "finding_id": o["finding"], "decision": "dismissed",
                                "request_id": rid, "note": "probe"}, "findings.review", p),
            ("order_repair", {"project_id": p, "order_id": o["order"], "request_id": rid, "note": "probe"},
             "orders.repair", p),
            ("order_review", {"project_id": p, "order_id": o["order"], "round": 1, "decision": "dismissed",
                              "request_id": rid, "note": "probe"}, "orders.review", p),
            ("reference_register", {"project_id": p, "mission_id": o["mission"], "evidence_id": o["evidence"],
                                    "note": "probe"}, "references.register", p),
            ("analysis_submit", {"project_id": p, "mission_id": o["mission"], "evidence_id": o["evidence"],
                                 "analyzer": "signature_s1_v1", "request_id": rid}, "analysis.submit", p),
            ("audit_watch", {"project_id": p}, "audit.list", p),
            ("approve", {"mission_id": o["pending"], "version": 1, "package_hash": "0" * 64}, "approve", p),
            ("decline", {"mission_id": o["pending"], "version": 1, "reason": "probe"}, "decline", p),
            ("operate", {"mission_id": o["mission"], "action": "pause", "request_id": rid}, "operate", p),
        ]

    async def frame_matrix(self) -> None:
        from drone_agent.console.mission import LocalApi, MissionConsole, Session

        console = MissionConsole(LocalApi(self.world.service), "https://desk.example.ts.net:8448", tailnet=True,
                                 scope={})
        n = 0
        for name, (actor, trust) in IDENTITIES.items():
            if trust not in ("first_party", "anonymous"):
                continue  # only people reach the page; agents and backends have their own doors / 只有人使用页面
            for frame, message, method, project in self.frames(n):
                n += 1
                sent: list[dict] = []

                async def send(event, sink=sent):
                    sink.append(json.loads(event["text"]))

                session = Session(console, actor, send)
                await session.handle(json.dumps({"type": frame, **message}))
                for push in ("push_view", "push_resources", "push_workflows", "push_run", "push_tasks", "push_task",
                             "push_business", "push_subject", "push_audit"):
                    await getattr(session, push)()
                expected = self.expect(actor, trust, method, project)
                readable = self.readable(actor, trust)
                leaked = set()
                for payload in sent:
                    if payload.get("type") in DATA_FRAMES:
                        leaked |= self.projects_in(payload) - readable
                        if payload.get("type") == "media" and project not in readable:
                            leaked.add(project)
                errors = [p for p in sent if p.get("type") == "error"]
                escape = f"frame_names:{','.join(sorted(leaked))}" if leaked else \
                    ("forbidden_frame_without_refusal" if expected == "forbidden" and not errors else None)
                self.rows.append({"layer": "frame", "identity": name, "method": frame, "project": project,
                                  "expected": expected, "ok": not errors, "code": (errors[0].get("issue") or {}).get(
                                      "code") if errors else None, "escape": escape, "code_mismatch": False,
                                  "frames": sorted({p.get("type") for p in sent})})

    def summary(self) -> dict:
        escapes = [r for r in self.rows if r["escape"]]
        api = [r for r in self.rows if r["layer"] == "api"]
        frames = [r for r in self.rows if r["layer"] == "frame"]
        covered = {r["method"] for r in api}
        return {"format": "drone.authz-matrix/v1", "status": "passed" if not escapes and covered == set(METHODS)
                else "failed", "escapes": len(escapes), "escape_rows": escapes[:40],
                "api_rows": len(api), "frame_rows": len(frames), "methods": len(covered),
                "identities": sorted(IDENTITIES), "forbidden_refused": sum(1 for r in self.rows
                                                                          if r["expected"] == "forbidden"
                                                                          and not r["escape"]),
                "code_mismatches": [r for r in self.rows if r["code_mismatch"]][:20],
                "audit_denials": self.denials()}

    def denials(self) -> int:
        """Members' refused attempts that joined the target project's audit trail. / 进入目标项目审计的成员被拒尝试。"""
        return sum(1 for e in self.world.service.ops.store.events(f"audit:{TARGET}", limit=5000)
                   if e.get("kind") == "access.denied")


def role_scopes_names(roles) -> set[str]:
    """Roles that grant at least viewing (every role does). / 至少授予查看的角色（每个角色都是）。"""
    return {"viewer"} if roles else set()


async def run(case: Path, repo: Path) -> dict:
    world = P5DeskWorld(case, repo, seed=7)
    try:
        await world.settle()
        matrix = Matrix(world, repo)
        await matrix.seed()
        await matrix.api_matrix()
        await matrix.frame_matrix()
        result = matrix.summary()
        (case / "authz-rows.json").write_text(json.dumps(matrix.rows, indent=1, ensure_ascii=False, default=str),
                                              encoding="utf-8")
        return result
    finally:
        world.service.business.jobs.stop()
        world.ledger.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[3])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    result = asyncio.run(run(args.output, args.root))
    (args.output / "authz.json").write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str),
                                            encoding="utf-8")
    print(json.dumps({k: v for k, v in result.items() if k not in ("escape_rows", "code_mismatches")}, indent=2))
    raise SystemExit(0 if result["status"] == "passed" else 1)


if __name__ == "__main__":
    main()
