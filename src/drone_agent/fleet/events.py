"""Onboard journal rows <-> ExecutionEvent, preserving the hash chain (02-contracts §10, D031).

The uplink forwards every row of the executive and guardian journals as an ExecutionEvent: `event_id` is
derived from the row hash, the event type maps the row kind where the vocabulary has one (otherwise
`runtime_record`), and `data` keeps the original kind, sequence, previous hash, timestamps and payload.
The service can therefore rebuild the exact rows, re-verify their hash chain and find gaps, so the
business ledger is a checked mirror of the aircraft's authority log, not a paraphrase of it.

机载账本行 ↔ ExecutionEvent，保留哈希链（02-contracts §10，D031）。

uplink 把 executive 与 guardian 账本的每一行都作为 ExecutionEvent 转发：`event_id` 由行哈希导出；词表中有
对应类型的行映射到该类型，否则为 `runtime_record`；`data` 保留原始种类、序号、前项哈希、时间戳与载荷。
服务因此能重建确切的行、复核哈希链并发现缺口，业务账本是飞行器权威日志的受检镜像，而不是转述。
"""

from __future__ import annotations

from datetime import datetime

from drone_agent.contracts import EventType, ExecutionEvent, StepOutcome
from drone_agent.runtime.ledger import content_hash

_KINDS = {
    "mission_accepted": EventType.MISSION_ACCEPTED,
    "mission_result": EventType.MISSION_FINISHED,
    "operator_request": EventType.OPERATOR_REQUEST_ACCEPTED,
    "operator_rejected": EventType.OPERATOR_REQUEST_REJECTED,
    "command_rejected": EventType.COMMAND_REJECTED,
    "safety_intervention": EventType.SAFETY_INTERVENTION,
    "recovered_to": EventType.RECOVERED_TO,
    "lease": EventType.LEASE_ISSUED,
    "revoked": EventType.LEASE_REVOKED,
    "cancel_requested": EventType.SKILL_CANCEL_REQUESTED,
}
_SKILL_STATES = {"running": EventType.SKILL_STARTED, "paused": EventType.SKILL_PAUSED,
                 "cancel_requested": EventType.SKILL_CANCEL_REQUESTED}


def event_id(robot_id: str, mission_id: str, version: int, journal: str, row: dict) -> str:
    return f"{robot_id}:{mission_id}:v{version}:{journal}:{row['seq']}:{row['sha256'][:16]}"


def journal_row_to_event(row: dict, *, journal: str, robot_id: str, mission_id: str, version: int) -> ExecutionEvent:
    """One journal row as an ExecutionEvent that still carries the whole row. / 仍携带整行的 ExecutionEvent。"""
    kind, data = row["kind"], row["data"]
    event_type = _KINDS.get(kind, EventType.RUNTIME_RECORD)
    fields = {}
    if kind == "skill_state":
        event_type = _SKILL_STATES.get(data.get("state"), EventType.RUNTIME_RECORD)
        fields["step_id"] = data.get("step_id")
    elif kind == "step_outcome":
        outcome = StepOutcome.model_validate(data["outcome"])
        event_type = EventType.SKILL_COMPLETED if outcome.counts_as_completed else EventType.SKILL_FAILED
        fields.update(step_id=outcome.step_id, execution_status=outcome.execution_status,
                      effect_verdict=outcome.effect_verdict, safety_verdict=outcome.safety_verdict)
    elif kind in ("command_submitted",):
        fields["step_id"] = data.get("envelope", {}).get("key", {}).get("step_id")
    return ExecutionEvent(
        event_id=event_id(robot_id, mission_id, version, journal, row),
        event_type=event_type,
        timestamp=datetime.fromisoformat(row["timestamp"]),
        mission_id=mission_id,
        mission_version=version,
        robot_id=robot_id,
        reason=str(data.get("reason", ""))[:200] if isinstance(data, dict) else "",
        data={"journal": journal, "seq": row["seq"], "kind": kind, "sha256": row["sha256"],
              "previous": row["previous"], "row_timestamp": row["timestamp"], "monotonic_ns": row["monotonic_ns"],
              "data": data},
        **fields,
    )


def event_to_row(event: ExecutionEvent | dict) -> dict:
    """Rebuild the original journal row. / 重建原始账本行。"""
    data = event.data if isinstance(event, ExecutionEvent) else event["data"]
    return {"seq": data["seq"], "previous": data["previous"], "timestamp": data["row_timestamp"],
            "monotonic_ns": data["monotonic_ns"], "kind": data["kind"], "data": data["data"], "sha256": data["sha256"]}


def verify_chain(rows: list[dict]) -> tuple[bool, str]:
    """Whether rebuilt rows form one complete chain from seq 0. / 重建的行是否从序号 0 起构成完整链。"""
    previous = "0" * 64
    for index, row in enumerate(sorted(rows, key=lambda r: r["seq"])):
        if row["seq"] != index:
            return False, f"gap before seq {row['seq']}"
        claimed = row["sha256"]
        body = {k: v for k, v in row.items() if k != "sha256"}
        if row["previous"] != previous or content_hash(body) != claimed:
            return False, f"hash chain broken at seq {row['seq']}"
        previous = claimed
    return True, ""


def outcomes_from_rows(rows: list[dict]) -> dict[str, StepOutcome]:
    """The last recorded outcome per step. / 每个步骤最后记录的结果。"""
    outcomes = {}
    for row in sorted(rows, key=lambda r: r["seq"]):
        if row["kind"] == "step_outcome":
            outcome = StepOutcome.model_validate(row["data"]["outcome"])
            outcomes[outcome.step_id] = outcome
    return outcomes
