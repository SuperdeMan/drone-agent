"""Project audit trail (P5, D070): who did what in one project, projected from records the service already keeps.

Nothing here writes or decides. The trail merges the project's mission requests, approvals and declines, operator
requests, and the operations audit rows (`op_events`) whose subject belongs to the project: workflow starts, cancels,
schedule switches, reviews and repair feedback, scheduler tasks and assignments, findings, orders and rounds, reference
appearances and reuse analyses, dock maintenance locks and actions, delivery claims, voids and releases, and the
refused attempts of project members (`access.denied`). The dock's once-a-second status reports and the workers' own
bookkeeping (leases, node transitions) are left out. Each page scans a bounded window of the newest rows, so a call
costs the same however long the history is; the cursor continues where the window stopped.

项目审计记录（P5，D070）：一个项目里谁做了什么，由服务已保存的记录投影而来。

这里不写入也不判定。记录合并该项目的任务请求、审批与驳回、操作请求，以及主题属于该项目的运营审计行（`op_events`）：工作流的
启动、取消、排班启停、复核与维修反馈，调度任务单与分配，发现、工单与轮次，参考外观与复用分析，机场维护锁与动作，投递的领取、
作废与释放，以及项目成员被拒的尝试（`access.denied`）。机场每秒一次的状态报告与工作进程自身的记账（租约、节点迁移）不列入。
每页只扫描最新的有限窗口，因此无论历史多长，单次调用代价相同；游标从窗口停止处继续。
"""

from __future__ import annotations

import json
from datetime import datetime

from drone_agent.runtime.permission import AUDIT_READ, Caller

MAX_LIMIT = 200
EVENT_WINDOW = 20_000
NOISE = frozenset({"status.accepted", "lease.taken", "node.state"})
FINAL_RUN = frozenset({"completed", "failed", "outcome_unknown", "cancelled"})
OWNED_TABLES = {"workflow": ("wf_runs", "run_id"), "task": ("sc_tasks", "task_id"),
                "finding": ("bz_findings", "finding_id"), "order": ("bz_orders", "order_id"),
                "job": ("bz_jobs", "job_id"), "reference": ("bz_references", "reference_id")}


def _load(text):
    return json.loads(text) if isinstance(text, str) else (text or {})


def _small(value: dict, limit: int = 8) -> dict:
    """At most a few short fields of a record body. / 记录体中至多几个短字段。"""
    found = {}
    for key, item in sorted((value or {}).items()):
        if len(found) >= limit:
            break
        if isinstance(item, (str, int, float, bool)) or item is None:
            found[key] = item[:160] if isinstance(item, str) else item
    return found


class ProjectAudit:
    """Read-only audit projection over the service's ledger. / 服务账本上的只读审计投影。"""

    def __init__(self, service):
        self.service = service
        self.ledger = service.ledger

    def _rows(self, sql: str, args=()) -> list[dict]:
        return self.ledger._rows(sql, args)

    def _has(self, table: str) -> bool:
        return bool(self._rows("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)))

    # ── sources / 来源 ──

    def _requests(self, project: str, before: str, limit: int) -> list[dict]:
        rows = self._rows(
            "SELECT r.requested_by, r.mission_id, r.body, r.received_at FROM requests r "
            "JOIN op_bindings b ON b.mission_id = r.mission_id WHERE b.project_id=? AND r.received_at < ? "
            "ORDER BY r.received_at DESC LIMIT ?", (project, before, limit))
        found = []
        for row in rows:
            body = _load(row["body"])
            found.append({"at": row["received_at"], "actor": row["requested_by"], "action": "mission.submitted",
                          "object": {"kind": "mission", "id": row["mission_id"]}, "source": "requests",
                          "detail": {"channel": body.get("channel"), "text": str(body.get("text", ""))[:160]}})
        return found

    def _approvals(self, project: str, before: str, limit: int) -> list[dict]:
        rows = self._rows(
            "SELECT v.mission_id, v.version, v.approval, v.decision, v.updated_at FROM versions v "
            "JOIN op_bindings b ON b.mission_id = v.mission_id WHERE b.project_id=? "
            "AND (v.approval IS NOT NULL OR v.status='declined') ORDER BY v.updated_at DESC LIMIT ?",
            (project, limit * 4))
        found = []
        for row in rows:
            approval, decision = _load(row["approval"]), _load(row["decision"])
            target = {"kind": "mission", "id": row["mission_id"], "version": row["version"]}
            if approval and approval.get("approved_at", "") < before:
                actor = approval.get("approver", "")
                found.append({"at": approval["approved_at"], "actor": actor, "object": target, "source": "versions",
                              "action": "mission.auto_approved" if actor.startswith("policy:") else "mission.approved",
                              "detail": {"package_hash": approval.get("package_hash"),
                                         "expires_at": approval.get("expires_at")}})
            if decision.get("declined_by") and row["updated_at"] < before:
                found.append({"at": row["updated_at"], "actor": decision["declined_by"], "object": target,
                              "action": "mission.declined", "source": "versions",
                              "detail": {"reason": str(decision.get("reason", ""))[:160]}})
        return found

    def _operations(self, project: str, before: str, limit: int) -> list[dict]:
        rows = self._rows(
            "SELECT d.mission_id, d.version, d.payload, d.created_at, d.acked_at, d.ack_accepted, d.ack_reason "
            "FROM deliveries d JOIN op_bindings b ON b.mission_id = d.mission_id WHERE b.project_id=? "
            "AND d.kind='operator_request' AND d.created_at < ? ORDER BY d.created_at DESC LIMIT ?",
            (project, before, limit))
        found = []
        for row in rows:
            payload = _load(row["payload"])
            found.append({"at": row["created_at"], "actor": payload.get("requested_by", ""),
                          "action": f"mission.{payload.get('action', 'operate')}", "source": "deliveries",
                          "object": {"kind": "mission", "id": row["mission_id"], "version": row["version"]},
                          "detail": {"request_id": payload.get("request_id"), "acked": row["acked_at"] is not None,
                                     "accepted": row["ack_accepted"], "reason": row["ack_reason"]}})
        return found

    def _owner(self, subject: str, cache: dict) -> tuple[str | None, dict | None]:
        """(project, object) of an audit subject, or (None, None) when it belongs to no project.

        审计主题的（项目，对象）；不属于任何项目时为（None, None）。
        """
        if subject in cache:
            return cache[subject]
        kind, _, rest = subject.partition(":")
        found: tuple[str | None, dict | None] = (None, None)
        catalog = self.service.ops.catalog
        if kind == "mission":
            mission = rest.split(":", 1)[0]
            binding = self.service.ops.store.binding(mission)
            if binding is not None:
                found = (binding["project_id"], {"kind": "mission", "id": mission})
        elif kind == "dock" and rest in catalog.docks:
            found = (catalog.sites[catalog.docks[rest].site_id].project_id, {"kind": "dock", "id": rest})
        elif kind in ("schedule", "drafts", "reuse", "audit"):
            project = rest.split(":", 1)[0]
            found = (project, {"kind": kind, "id": rest})
        elif kind in OWNED_TABLES:
            table, key = OWNED_TABLES[kind]
            if self._has(table):
                row = self._rows(f"SELECT project_id FROM {table} WHERE {key}=?", (rest,))
                if row:
                    found = (row[0]["project_id"], {"kind": kind, "id": rest})
        cache[subject] = found
        return found

    def _events(self, project: str, before_id: int | None, limit: int) -> tuple[list[dict], int | None]:
        """The newest audit rows of this project within one bounded window, and where the window stopped.

        一个有限窗口内本项目最新的审计行，以及窗口停止的位置。
        """
        top = before_id if before_id is not None else (self._rows("SELECT MAX(id) AS top FROM op_events")[0]["top"] or 0) + 1
        rows = self._rows("SELECT id, subject, kind, actor, body, created_at FROM op_events WHERE id < ? AND id >= ? "
                          "ORDER BY id DESC", (top, max(0, top - EVENT_WINDOW)))
        found, cache = [], {}
        stopped = max(0, top - EVENT_WINDOW) if len(rows) else 0
        for row in rows:
            if row["kind"] in NOISE:
                continue
            body = _load(row["body"])
            if row["kind"] == "run.state" and body.get("to") not in FINAL_RUN:
                continue
            owner, target = self._owner(row["subject"], cache)
            if owner != project:
                continue
            found.append({"at": row["created_at"], "actor": row["actor"], "action": row["kind"], "object": target,
                          "source": "op_events", "event_id": row["id"], "detail": _small(body)})
            if len(found) >= limit:
                stopped = row["id"]
                break
        return found, (stopped if stopped > 0 else None)

    # ── the call / 调用 ──

    def list(self, caller: Caller | None, project_id: str, *, before: str | None = None, limit: int = 100) -> dict:
        """One page of a project's audit trail, newest first. / 一个项目审计记录的一页，最新在前。"""
        self.service._require(caller, project_id, AUDIT_READ)
        limit = max(1, min(int(limit or 100), MAX_LIMIT))
        cursor = _load(before) if before else {}
        if before and not isinstance(cursor, dict):
            cursor = {}
        at = str(cursor.get("at") or "9999-12-31T23:59:59+00:00")
        event_id = cursor.get("event_id")
        try:
            datetime.fromisoformat(at)
            event_id = int(event_id) if event_id is not None else None
        except (TypeError, ValueError):
            from drone_agent.fleet.service import ServiceError

            raise ServiceError("service.invalid_request", "malformed audit cursor") from None
        events, stopped = self._events(project_id, event_id, limit)
        others = self._requests(project_id, at, limit) + self._approvals(project_id, at, limit) + \
            self._operations(project_id, at, limit)
        floor = min((e["at"] for e in events), default=None) if stopped else None
        merged = events + [e for e in others if floor is None or e["at"] >= floor]
        merged.sort(key=lambda e: (e["at"], e.get("event_id", 0)), reverse=True)
        page = merged[:limit]
        oldest = page[-1]["at"] if page else None
        more = len(merged) > limit or stopped is not None
        return {"project_id": project_id, "entries": page,
                "next": json.dumps({"at": oldest, "event_id": stopped}) if more and oldest else None,
                "window": EVENT_WINDOW, "generated_at": self.service.clock().isoformat()}
