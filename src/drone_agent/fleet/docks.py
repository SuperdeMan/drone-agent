"""Dock status ingestion and the dock action exchange for P1 backends (D055).

A report is accepted only from the backend principal the catalog binds to that dock, and only if it is complete,
not from the future (> 1 s), not already stale on arrival (> 3 s), strictly newer within its boot session and not
from a boot session that was already replaced. A new session is reconciling until enough reports arrived. The
source label is the catalog binding. A reported maintenance or fault sets a sticky lock that later telemetry
cannot clear. Actions are requested by the service and pulled by the backend: an ACK only records receipt, and only
a later status report can complete or fail an action.

P1 后端的机场状态入账与机场动作交换（D055）。

报告只从目录为该机场绑定的后端身份接受，并且必须完整、不来自未来（> 1 s）、到达时未过期（> 3 s）、在其 boot
会话内严格更新、不来自已被替代的 boot 会话。新会话在收到足够报告前处于对账状态。来源标签取自目录绑定。报告
的维护或故障会设置粘滞锁，之后的遥测不能解除。动作由服务请求、后端拉取：ACK 只记录受理，只有之后的状态报告
才能使动作完成或失败。
"""

from __future__ import annotations

from datetime import datetime

from pydantic import ValidationError

from drone_agent.fleet.operations_store import OperationsStore
from drone_agent.fleet.resources import ActionResult, DockStatusReport, SessionState, Upkeep


class ReportRejected(ValueError):
    """A dock report that must not change the projection. / 不得改变投影的机场报告。"""

    def __init__(self, reason: str, detail: str = ""):
        self.reason = reason
        super().__init__(f"{reason}: {detail}" if detail else reason)


def ingest(store: OperationsStore, principal: str, data: dict, now: datetime) -> dict:
    """Validate and apply one backend report inside one transaction; rejections only reach the audit log.

    在一个事务内校验并应用一份后端报告；被拒的报告只进入审计日志。
    """
    catalog, policy = store.catalog, store.catalog.policy
    dock_id = data.get("dock_id") if isinstance(data, dict) else None
    subject = f"dock:{dock_id}" if isinstance(dock_id, str) else "dock:unknown"
    try:
        if not isinstance(dock_id, str) or dock_id not in catalog.docks:
            raise ReportRejected("unknown_dock", str(dock_id)[:80])
        if catalog.docks[dock_id].backend.principal != principal:
            raise ReportRejected("backend_mismatch", principal[:80])
        try:
            report = DockStatusReport.model_validate(data)
        except ValidationError as error:
            raise ReportRejected("invalid_report", str(error.errors()[0].get("loc", ""))[:120]) from error
        skew = (report.observed_at - now).total_seconds()
        if skew > policy.future_skew_s:
            raise ReportRejected("future_timestamp", f"{skew:.3f} s ahead")
        if -skew > policy.freshness_s:
            raise ReportRejected("stale_on_arrival", f"{-skew:.3f} s old")
        with store.transaction():
            row = store.dock_row(dock_id)
            if row is not None and row["boot_id"] == report.boot_id:
                if report.seq <= row["seq"]:
                    raise ReportRejected("reordered", f"seq {report.seq} after {row['seq']}")
                complete = row["complete_reports"] + 1
            else:
                if report.boot_id in store.known_boots(dock_id):
                    raise ReportRejected("replaced_boot", report.boot_id)
                complete = 1
            session = SessionState.ACTIVE if complete >= policy.reconcile_reports else SessionState.RECONCILING
            store.put_dock_state(dock_id, boot_id=report.boot_id, seq=report.seq, session=session.value,
                                 complete_reports=complete, source=catalog.docks[dock_id].backend.kind,
                                 report=report, observed_at=report.observed_at.isoformat())
            locked = None
            if report.upkeep is not Upkeep.NORMAL and store.set_lock(dock_id, report.upkeep.value,
                                                                     f"reported by {principal}", principal):
                locked = report.upkeep.value
            finished = []
            for progress in report.actions:
                action = store.action(progress.action_id)
                if action is None or action["dock_id"] != dock_id or progress.result is ActionResult.IN_PROGRESS:
                    continue
                if store.finish_action(progress.action_id, progress.result.value):
                    finished.append({"action_id": progress.action_id, "kind": action["kind"],
                                     "result": progress.result.value})
            store.event(subject, "status.accepted", principal,
                        {"boot_id": report.boot_id, "seq": report.seq, "session": session.value, "locked": locked,
                         "finished": finished, "observed_at": report.observed_at.isoformat()})
            if session is SessionState.RECONCILING and complete == 1 and row is not None:
                store.event(subject, "session.new", principal, {"boot_id": report.boot_id,
                                                                "replaces": row["boot_id"]})
        return {"accepted": True, "session": session.value, "finished": finished}
    except ReportRejected as rejected:
        store.event(subject, "status.rejected", principal, {"reason": rejected.reason, "detail": str(rejected)[:300]})
        return {"accepted": False, "reason": rejected.reason}


def pending_actions(store: OperationsStore, principal: str, dock_id: str) -> list[dict]:
    """Requested actions not yet acknowledged, for a dock bound to this backend. / 该后端所绑定机场尚未确认的请求动作。"""
    dock = store.catalog.docks.get(dock_id)
    if dock is None or dock.backend.principal != principal:
        raise ReportRejected("backend_mismatch", dock_id)
    return [{"action_id": row["action_id"], "dock_id": dock_id, "kind": row["kind"],
             "requested_at": row["requested_at"]} for row in store.actions(dock_id, ("requested",))]


def acknowledge(store: OperationsStore, principal: str, action_id: str, accepted: bool, reason: str) -> dict:
    """Record receipt of an action; a duplicate or late ACK is reported, never applied twice.

    记录动作受理；重复或迟到的 ACK 如实报告，从不应用两次。
    """
    action = store.action(action_id)
    if action is None or store.catalog.docks[action["dock_id"]].backend.principal != principal:
        raise ReportRejected("backend_mismatch", action_id[:80])
    applied = store.ack_action(action_id, bool(accepted), reason)
    store.event(f"dock:{action['dock_id']}", "action.ack" if applied else "action.ack_ignored", principal,
                {"action_id": action_id, "kind": action["kind"], "accepted": bool(accepted), "reason": reason[:200],
                 "state_before": action["state"]})
    return {"applied": applied, "state": store.action(action_id)["state"]}


def release_lock(store: OperationsStore, dock_id: str, actor: str, reason: str) -> bool:
    """Only an authorized admin action clears a maintenance or fault lock. / 只有经授权的 admin 操作能解除维护或故障锁。"""
    with store.transaction():
        lock = store.lock(dock_id)
        if lock is None or not store.clear_lock(dock_id):
            return False
        store.event(f"dock:{dock_id}", "lock.released", actor,
                    {"state": lock.state, "set_by": lock.set_by, "reason": reason[:300]})
        return True


def set_lock(store: OperationsStore, dock_id: str, actor: str, reason: str) -> bool:
    """An admin may lock a dock for maintenance. / admin 可以把机场锁定为维护。"""
    with store.transaction():
        if not store.set_lock(dock_id, "maintenance", reason, actor):
            return False
        store.event(f"dock:{dock_id}", "lock.set", actor, {"state": "maintenance", "reason": reason[:300]})
        return True
