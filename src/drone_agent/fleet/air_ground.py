"""X1 assembly (D088): the handoff tables, store and engine, wired into the hub, the scheduler and the service.

`--air-ground` needs the scheduling and the business catalogs: the handoff tables join a ledger that already has the
scheduling and business tables (migrated with a verified backup first, D088). The hub hands offers out and takes
replies through the engine; the scheduler ticks the engine, acks accepted offers by assigning the ground mission and
ends refused or expired tasks; a settled ground task completes its offer. Nothing here signs, approves or connects to a
robot.

X1 装配（D088）：交接表、存储与引擎，接入 hub、调度器与服务。

`--air-ground` 需要调度目录与业务目录：交接表加入已有调度表与业务表的账本（有数据时先做经校验的备份，D088）。hub 经引擎
交付提议并接收回复；调度器驱动引擎，以分配地面任务来确认已接受的提议，并终结被拒或过期的任务；地面任务结算后完成其提议。
这里不签名、不审批，也不连接任何机器人。
"""

from __future__ import annotations

from pathlib import Path

from drone_agent.contracts import utcnow
from drone_agent.fleet.handoff import HandoffEngine
from drone_agent.fleet.handoff_store import HandoffStore, migrate


def settle_offer(engine: HandoffEngine, task: dict, live: dict, kind: str) -> None:
    """The ground mission of an acked offer settled, inside the scheduler's transaction: the offer completes.

    已确认提议的地面任务已在调度器的事务内结算：提议完成。
    """
    for handoff in engine.store.for_task(task["task_id"]):
        if handoff["state"] == "acked":
            engine.complete(handoff["handoff_id"], mission_id=live["mission_id"], outcome=kind)


def build_air_ground(service, *, backups: Path | None, clock=utcnow) -> HandoffEngine:
    """Migrate the handoff tables and wire the engine into the hub, the scheduler and the service.

    迁移交接表，并把引擎接入 hub、调度器与服务。
    """
    scheduler = getattr(service, "scheduler", None)
    if scheduler is None:
        raise ValueError("--air-ground needs --scheduling (and --business)")
    report = migrate(service.ledger, backups=backups, clock=clock)
    store = HandoffStore(service.ledger, clock=clock)
    engine = HandoffEngine(store, reject_task=scheduler.reject_handoff_task, assign=scheduler.assign_handoff,
                           clock=clock)
    engine.migration = report
    service.hub.handoffs = engine
    service.handoffs = engine
    scheduler.handoffs = engine
    scheduler.on_handoff_settled = lambda task, live, kind, summary: settle_offer(engine, task, live, kind)
    return engine
