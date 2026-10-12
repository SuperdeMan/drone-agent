"""WP-P6-04 asset archive tables: the migration, the backfill from the ledger, the drill and every archive read and
write (D087).

Three append-only tables join the business tables in the same SQLite file. `ar_revisions` snapshots each asset's
definition per site-map digest, `ar_observations` keeps one row per service-verified capture of an asset with its
measured viewpoint, and `ar_events` is the per-asset copy of every authoritative business transition, each row tagged
`fact`, `decision` or `belief`. The migration adds them with one index on `bz_jobs (evidence_id)`, backed up and
verified first like D056-D085, and backfills them in the same transaction from the existing ledger. The backfill
depends only on the ledger, the current catalogs and site maps, and the migration instant: event ids are digests of
their source keys, so two copies migrated at the same instant hold the same archive, and the same business
transition written live or backfilled is the same event. Nothing outside the `ar_` objects and the `asset_` meta keys
changes.

WP-P6-04 资产档案表：迁移、从账本回填、演练以及档案的全部读写（D087）。

三张只追加的表与业务表位于同一个 SQLite 文件。`ar_revisions` 按站点地图摘要快照资产定义，`ar_observations` 为服务核验过的
每次资产采集保存一行及其实测视角，`ar_events` 是每次权威业务转移按资产索引的副本，每行标明 `fact`、`decision` 或
`belief`。迁移新增这三张表与 `bz_jobs (evidence_id)` 上的一个索引，与 D056–D085 一样先备份并校验，并在同一事务内从既有
账本回填。回填只依赖账本、当前目录与站点地图以及迁移时刻：事件 id 是来源键的摘要，因此两份副本在同一时刻迁移得到相同的
档案，同一次业务转移无论在线写入还是回填都是同一条事件。`ar_` 对象与 `asset_` 元数据键之外的任何内容都不改变。
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from drone_agent.contracts import utcnow
from drone_agent.fleet.ledger import BusinessLedger
from drone_agent.fleet.operations_store import SCHEMA_VERSION as OPERATIONS_SCHEMA
from drone_agent.fleet.operations_store import MigrationError, schema_version
from drone_agent.fleet.provenance import NAMESPACE, RunHeader, unseal

ASSET_SCHEMA = "1"
DDL: tuple[str, ...] = (
    "CREATE TABLE IF NOT EXISTS ar_revisions (asset_key TEXT NOT NULL, registry_sha256 TEXT NOT NULL, "
    "project_id TEXT NOT NULL, scope TEXT NOT NULL, asset_id TEXT NOT NULL, map_version TEXT NOT NULL, "
    "definition TEXT NOT NULL, first_seen_at TEXT NOT NULL, PRIMARY KEY (asset_key, registry_sha256))",
    "CREATE INDEX IF NOT EXISTS ar_revisions_asset ON ar_revisions (project_id, asset_id)",
    "CREATE TABLE IF NOT EXISTS ar_observations (evidence_id TEXT PRIMARY KEY, asset_key TEXT NOT NULL, "
    "project_id TEXT NOT NULL, mission_id TEXT NOT NULL, mission_version INTEGER NOT NULL, step_id TEXT NOT NULL, "
    "registry_sha256 TEXT, captured_at TEXT NOT NULL, media_sha256 TEXT NOT NULL, geometry TEXT NOT NULL, "
    "verification TEXT NOT NULL, recapture_of TEXT, created_at TEXT NOT NULL)",
    "CREATE INDEX IF NOT EXISTS ar_observations_asset ON ar_observations (asset_key, captured_at)",
    "CREATE TABLE IF NOT EXISTS ar_events (event_id TEXT PRIMARY KEY, asset_key TEXT NOT NULL, "
    "project_id TEXT NOT NULL, kind TEXT NOT NULL, authority TEXT NOT NULL, at TEXT NOT NULL, actor TEXT NOT NULL, "
    "refs TEXT NOT NULL, body TEXT NOT NULL, source_key TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL)",
    "CREATE INDEX IF NOT EXISTS ar_events_asset ON ar_events (asset_key, at)",
    # The image measures of an observation are read from its analysis jobs, never copied. / 观测的图像测量从其分析作业读取，从不复制。
    "CREATE INDEX IF NOT EXISTS ar_jobs_evidence ON bz_jobs (evidence_id)",
)
TABLES = ("ar_revisions", "ar_observations", "ar_events")
INDEXES = ("ar_revisions_asset", "ar_observations_asset", "ar_events_asset", "ar_jobs_evidence")
# Every event kind and its authority (design §3.4). / 每种事件及其权威等级（设计 §3.4）。
AUTHORITY = {
    "finding_opened": "belief", "finding_confirmed": "fact", "finding_dismissed": "fact", "order_opened": "fact",
    "repair_reported": "fact", "round_reviewed": "fact", "round_concluded": "decision",
    "maintenance_recorded": "fact", "reference_registered": "fact", "reference_revoked": "fact",
    "revision_seen": "fact", "verification_changed": "fact",
}
BUSINESS_TABLES = ("bz_jobs", "bz_findings", "bz_reviews", "bz_orders", "bz_rounds", "bz_references")


def _dump(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def stamp(value) -> str:
    """A time as fixed-width UTC ISO text, so that text order is time order (evidence times end in `Z`, service
    times in `+00:00`).

    定宽的 UTC ISO 文本，使文本顺序即时间顺序（证据时间以 `Z` 结尾，服务时间以 `+00:00` 结尾）。
    """
    moment = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _load(text):
    return None if text is None else json.loads(text)


# ── the catalog side / 目录信息 ──


def shared_scope(catalog, scheduling, project_id: str, site_id: str) -> str:
    """The scope of an asset key: `*` when the scheduling catalog places every site of the project in one shared frame
    (P3), otherwise the site. The business index and the archive use this one rule.

    资产键的范围：调度目录把项目的每个站点放在同一共享坐标中（P3）时为 `*`，否则为站点。业务索引与档案共用这一条规则。
    """
    if scheduling is not None and catalog is not None and project_id in catalog.projects:
        if set(catalog.projects[project_id].sites) <= set(scheduling.airspace.sites):
            return "*"
    return site_id


@dataclass(frozen=True)
class ArchiveContext:
    """What the archive takes from the catalogs: the operations catalog, the scheduling catalog (or None) and each
    current site's map. The service builds it from its own objects; the drill from the catalog files.

    档案从目录中取用的内容：运营目录、调度目录（或 None）与每个当前站点的地图。服务用自身对象构建它，演练用目录文件构建。
    """

    catalog: object
    scheduling: object | None
    registries: dict

    def key(self, project_id: str, site_id: str, asset_id: str) -> str:
        return f"{project_id}/{shared_scope(self.catalog, self.scheduling, project_id, site_id)}/{asset_id}"

    def current(self):
        """(site, project, scope, registry) of every current site, in site order. / 每个当前站点的（站点、项目、范围、地图）。"""
        for site_id in sorted(self.catalog.sites):
            project_id = self.catalog.sites[site_id].project_id
            yield site_id, project_id, shared_scope(self.catalog, self.scheduling, project_id, site_id), \
                self.registries[site_id]


def definition(registry, site_id: str, asset_id: str) -> dict | None:
    """The snapshot of one asset's definition in a site map; None when the map has no such asset.

    站点地图中某资产定义的快照；地图中没有该资产时为 None。
    """
    data = registry.data
    entry = (data.get("assets") or {}).get(asset_id)
    if not isinstance(entry, dict):
        return None
    route_id = entry.get("observation_route")
    return {"registry_id": data.get("registry_id", ""), "site_id": site_id, "frame": data.get("frame"),
            "kind": entry.get("kind"), "position": entry.get("position"), "size_m": entry.get("size_m"),
            "camera_id": entry.get("camera_id"), "visual_signature": entry.get("visual_signature"),
            "volume": entry.get("volume"), "observation_route": route_id,
            "route": (data.get("routes") or {}).get(route_id) if route_id else None,
            "above_asset_m": (data.get("thresholds") or {}).get("above_asset_m"),
            "description": entry.get("description")}


def _number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def declared_heading(definition_: dict) -> float | None:
    """The direction of the asset's approach route's last segment (degrees, map ENU, counter-clockwise from +x); a
    declared value, never measured.

    资产观测航线末段的方向（度，地图 ENU，自 +x 逆时针）；只是声明值，从未实测。
    """
    route = definition_.get("route")
    if not isinstance(route, list) or len(route) < 2:
        return None
    (x0, y0, *_), (x1, y1, *_) = route[-2], route[-1]
    if (x0, y0) == (x1, y1):
        return None
    return round(math.degrees(math.atan2(y1 - y0, x1 - x0)) % 360, 2)


def viewpoint(pose: dict | None, definition_: dict | None) -> dict:
    """The measured viewpoint of a capture: distance to the asset centre and the bearing and elevation of the capture
    point seen from the asset, in the map frame. Unknown, with the reason, when the definition, the pose or a common
    frame is missing. Evidence carries no attitude: the camera is assumed to look at the asset (`inspect_asset`).

    采集的实测视角：到资产中心的距离，以及从资产看采集点的方位角与俯仰角（地图坐标）。定义、位姿或共同坐标系缺失时为
    unknown 并给出原因。证据不带姿态：假定相机对准资产（`inspect_asset`）。
    """
    if definition_ is None:
        return {"status": "unknown", "reason": "definition_unknown"}
    position = (pose or {}).get("position") or {}
    frame = (pose or {}).get("frame") or {}
    if not all(_number(position.get(axis)) for axis in "xyz"):
        return {"status": "unknown", "reason": "pose_missing"}
    expected = definition_.get("frame") or {}
    if (frame.get("frame_id"), frame.get("map_version")) != (expected.get("frame_id"), expected.get("map_version")):
        return {"status": "unknown", "reason": "frame_mismatch", "frame": frame, "map_frame": expected}
    asset = definition_.get("position")
    if not isinstance(asset, list) or len(asset) != 3 or not all(_number(v) for v in asset):
        return {"status": "unknown", "reason": "definition_incomplete"}
    capture = [position["x"], position["y"], position["z"]]
    dx, dy, dz = (capture[i] - asset[i] for i in range(3))
    horizontal = math.hypot(dx, dy)
    return {"status": "measured", "frame": frame, "capture": [round(v, 4) for v in capture], "asset": asset,
            "distance_m": round(math.hypot(horizontal, dz), 4), "horizontal_m": round(horizontal, 4),
            "bearing_deg": round(math.degrees(math.atan2(dy, dx)) % 360, 2) if horizontal > 0 else None,
            "elevation_deg": round(math.degrees(math.atan2(dz, horizontal)), 2),
            "declared_heading_deg": declared_heading(definition_)}


def run_header(decision: dict | None) -> RunHeader | None:
    """The sealed run header of a mission version, or None for a legacy or damaged record. / 任务版本封存的运行头。"""
    sealed = ((decision or {}).get(NAMESPACE) or {}).get("run")
    if sealed is None:
        return None
    try:
        return unseal(sealed, RunHeader)
    except ValueError:
        return None


def step_assets(package: dict | None) -> dict[str, str]:
    """step id -> the asset the package's step inspects. / 步骤 id -> 任务包中该步骤检查的资产。"""
    return {node["task_id"]: node["params"]["asset_id"] for node in (package or {}).get("nodes", [])
            if isinstance(node.get("params"), dict) and isinstance(node["params"].get("asset_id"), str)}


# ── events / 事件 ──


def event_id(source_key: str) -> str:
    return "ae-" + hashlib.sha256(source_key.encode()).hexdigest()[:24]


def event(kind: str, *, asset_key: str, project_id: str, at: str, actor: str, refs: dict, body: dict,
          source_key: str) -> dict:
    """One archive event; the id follows from the source key. / 一条档案事件；id 由来源键得出。"""
    return {"event_id": event_id(source_key), "asset_key": asset_key, "project_id": project_id, "kind": kind,
            "authority": AUTHORITY[kind], "at": stamp(at), "actor": actor, "refs": refs, "body": body,
            "source_key": source_key}


def finding_opened(job: dict, finding: dict) -> dict:
    """A suspected result that opened or joined a finding (a model's judgement). / 开出或并入发现的疑似结果（模型判断）。"""
    result = job.get("result") or {}
    inputs = job.get("inputs") or {}
    return event("finding_opened", asset_key=job["asset_key"], project_id=job["project_id"], at=job["updated_at"],
                 actor=str(result.get("source") or "unknown"),
                 refs={"finding_id": finding["finding_id"], "job_id": job["job_id"], "evidence_id": job["evidence_id"],
                       "mission_id": (inputs.get("evidence") or {}).get("mission_id"),
                       "order_id": job.get("order_id"), "round": job.get("round")},
                 body={"new": finding["first_job"] == job["job_id"], "purpose": job["purpose"],
                       "analyzer": job["analyzer"], "source": result.get("source"), "score": result.get("score"),
                       "defect_type": result.get("defect_type"), "family": result.get("family")},
                 source_key=f"job:{job['job_id']}")


def reviewed(review: dict, finding: dict | None = None, order: dict | None = None) -> dict:
    """A reviewer's decision on a finding or on a reinspection round. / 复核人对发现或复检轮次的决定。"""
    if review["subject_kind"] == "finding":
        kind = "finding_confirmed" if review["decision"] == "confirmed" else "finding_dismissed"
        refs = {"finding_id": review["subject_id"], "review_id": review["review_id"]}
        asset = finding["asset_key"]
    else:
        order_id, number = review["subject_id"].rsplit(":r", 1)
        basis = review.get("basis") or {}
        kind = "round_reviewed"
        refs = {"order_id": order_id, "round": int(number), "review_id": review["review_id"],
                "job_id": basis.get("job_id"), "evidence_id": basis.get("evidence_id")}
        asset = order["asset_key"]
    return event(kind, asset_key=asset, project_id=review["project_id"], at=review["created_at"],
                 actor=review["reviewer"], refs=refs, body={"decision": review["decision"], "note": review["note"]},
                 source_key=f"review:{review['review_id']}")


def order_opened(order: dict) -> dict:
    # No defect type: it is the model's label of the finding. / 不含缺陷类型：那是模型给发现的标签。
    return event("order_opened", asset_key=order["asset_key"], project_id=order["project_id"],
                 at=order["created_at"], actor=order["created_by"],
                 refs={"order_id": order["order_id"], "finding_id": order["finding_id"],
                       "review_id": order["review_id"]}, body={}, source_key=f"order:{order['order_id']}")


def repair_reported(order: dict, round_row: dict) -> dict:
    feedback = round_row["feedback"]
    return event("repair_reported", asset_key=order["asset_key"], project_id=order["project_id"],
                 at=feedback["reported_at"], actor=feedback["reported_by"],
                 refs={"order_id": order["order_id"], "round": round_row["round"],
                       "feedback_id": feedback["feedback_id"], "run_id": round_row.get("reinspection_run")},
                 body={"note": feedback.get("note", ""), "repair": feedback.get("repair")},
                 source_key=f"feedback:{feedback['feedback_id']}")


def round_concluded(order: dict, number: int, conclusion: dict, job_verdict: str | None) -> dict:
    """The settlement of a round: a deterministic decision whose inputs may include a model verdict, kept with it.

    轮次结算：确定性的决定，其输入可能含模型判定，一并记录。
    """
    return event("round_concluded", asset_key=order["asset_key"], project_id=order["project_id"],
                 at=conclusion["decided_at"], actor="service",
                 refs={"order_id": order["order_id"], "round": number, "feedback_id": conclusion.get("feedback_id"),
                       "evidence_id": conclusion.get("evidence_id"), "mission_id": conclusion.get("mission_id"),
                       "job_id": conclusion.get("job_id"), "review_id": conclusion.get("review_id")},
                 body={"rule": conclusion.get("rule"), "status": conclusion["status"],
                       "reasons": list(conclusion.get("reasons") or []), "job_source": conclusion.get("job_source"),
                       "job_verdict": job_verdict},
                 source_key=f"round:{order['order_id']}:r{number}")


def reference_registered(reference: dict) -> dict:
    body = reference.get("body") or {}
    return event("reference_registered", asset_key=reference["asset_key"], project_id=reference["project_id"],
                 at=reference["registered_at"], actor=reference["registered_by"],
                 refs={"reference_id": reference["reference_id"], "evidence_id": reference["evidence_id"],
                       "mission_id": body.get("mission_id")},
                 body={"note": body.get("note", ""), "captured_at": body.get("captured_at")},
                 source_key=f"reference:{reference['reference_id']}:registered")


def reference_revoked(reference: dict) -> dict:
    return event("reference_revoked", asset_key=reference["asset_key"], project_id=reference["project_id"],
                 at=reference["revoked_at"], actor=reference["revoked_by"],
                 refs={"reference_id": reference["reference_id"], "evidence_id": reference["evidence_id"]}, body={},
                 source_key=f"reference:{reference['reference_id']}:revoked")


def revision_seen(revision: dict) -> dict:
    found = revision["definition"]
    return event("revision_seen", asset_key=revision["asset_key"], project_id=revision["project_id"],
                 at=revision["first_seen_at"], actor="config",
                 refs={"registry_sha256": revision["registry_sha256"]},
                 body={"registry_id": found.get("registry_id"), "site_id": found.get("site_id"),
                       "map_version": revision["map_version"]},
                 source_key=f"revision:{revision['asset_key']}:{revision['registry_sha256']}")


# ── backfill / 回填 ──


def recapture_source(db: sqlite3.Connection, request_id: str) -> str | None:
    """The evidence a recapture mission replaces: the refused analysis named in its workflow activity (P6, D078).

    补拍任务所替代的证据：其工作流活动中写明的被拒分析（P6，D078）。
    """
    request = db.execute("SELECT requested_by, idempotency_key FROM requests WHERE request_id=?",
                         (request_id,)).fetchone()
    if request is None or not str(request[0]).startswith("workflow:"):
        return None
    row = db.execute("SELECT payload FROM wf_outbox WHERE idempotency_key=?", (request[1],)).fetchone() \
        if _has_table(db, "wf_outbox") else None
    job_id = ((_load(row[0]) or {}).get("recapture") or {}).get("job_id") if row else None
    found = db.execute("SELECT evidence_id FROM bz_jobs WHERE job_id=?", (job_id,)).fetchone() if job_id else None
    return found[0] if found else None


def _has_table(db: sqlite3.Connection, name: str) -> bool:
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def _rows(db: sqlite3.Connection, sql: str, args=()) -> list[dict]:
    cursor = db.execute(sql, args)
    names = [c[0] for c in cursor.description]
    return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]


def observation_row(*, evidence: dict, asset_key: str, project_id: str, header_sha: str | None,
                    known: dict | None, verification: dict, checked_with: str | None, recapture_of: str | None,
                    created_at: str) -> dict:
    """One observation of a verified capture; `known` is the revision definition of the mission's map, if any.

    一次已核验采集的观测；`known` 为任务所用地图的资产修订定义（如有）。
    """
    body = evidence["body"] or {}
    captured = (body.get("time_window") or {}).get("timestamp") or verification.get("verified_at") or created_at
    return {"evidence_id": evidence["evidence_id"], "asset_key": asset_key, "project_id": project_id,
            "mission_id": evidence["mission_id"], "mission_version": evidence["mission_version"],
            "step_id": evidence["step_id"], "registry_sha256": header_sha, "captured_at": stamp(captured),
            "media_sha256": evidence["sha256"], "geometry": viewpoint(body.get("captured_pose"), known),
            "verification": {**{k: verification.get(k) for k in ("final_verdict", "service_verdict",
                                                                    "onboard_verdict", "agrees", "checks",
                                                                    "verified_at")},
                             "registry_sha256": checked_with},
            "recapture_of": recapture_of, "created_at": stamp(created_at)}


def backfill(db: sqlite3.Connection, context: ArchiveContext, at: str) -> dict:
    """Fill the archive from the existing ledger inside the migration transaction; deterministic for one `at`.

    在迁移事务内从既有账本填充档案；对同一个 `at` 是确定性的。
    """
    counts = {"revisions": 0, "observations": 0, "events": 0, "definition_unknown": 0}
    bindings = {row["mission_id"]: row for row in _rows(db, "SELECT mission_id, project_id, site_id FROM op_bindings")}
    # When each site-map digest was first used: the earliest run header that names it. / 每个地图摘要最早被使用的时刻。
    first_use: dict[str, str] = {}
    headers: dict[tuple[str, int], str | None] = {}
    packages: dict[tuple[str, int], dict[str, str]] = {}
    for row in db.execute("SELECT mission_id, version, decision, package FROM versions ORDER BY mission_id, version"):
        if row[0] not in bindings:
            continue
        header = run_header(_load(row[2]))
        headers[(row[0], row[1])] = header.registry_sha256 if header else None
        packages[(row[0], row[1])] = step_assets(_load(row[3]))
        if header is not None:
            used = stamp(header.timestamp)
            if header.registry_sha256 not in first_use or used < first_use[header.registry_sha256]:
                first_use[header.registry_sha256] = used
    known: dict[tuple[str, str], dict] = {}
    for site_id, project_id, scope, registry in context.current():
        for asset_id in sorted((registry.data.get("assets") or {})):
            found = definition(registry, site_id, asset_id)
            key = f"{project_id}/{scope}/{asset_id}"
            revision = {"asset_key": key, "registry_sha256": registry.sha256, "project_id": project_id,
                        "scope": scope, "asset_id": asset_id,
                        "map_version": str((registry.data.get("frame") or {}).get("map_version", "")),
                        "definition": found, "first_seen_at": first_use.get(registry.sha256, at)}
            if insert_revision(db, revision):
                counts["revisions"] += 1
                counts["events"] += insert_event(db, revision_seen(revision), at)
            known[(key, registry.sha256)] = found
    missions = {row["mission_id"]: row["request_id"] for row in _rows(db, "SELECT mission_id, request_id FROM missions")}
    rows = _rows(db, "SELECT e.evidence_id, e.mission_id, e.mission_version, e.step_id, e.sha256, e.body, "
                     "v.body AS verification FROM evidence e JOIN verifications v ON v.evidence_id = e.evidence_id "
                     "ORDER BY e.evidence_id")
    for row in rows:
        binding = bindings.get(row["mission_id"])
        asset_id = packages.get((row["mission_id"], row["mission_version"]), {}).get(row["step_id"])
        if binding is None or asset_id is None:
            continue
        key = context.key(binding["project_id"], binding["site_id"], asset_id)
        header_sha = headers.get((row["mission_id"], row["mission_version"]))
        found = known.get((key, header_sha)) if header_sha else None
        counts["definition_unknown"] += found is None
        observation = observation_row(
            evidence={**row, "body": _load(row["body"])}, asset_key=key, project_id=binding["project_id"],
            header_sha=header_sha, known=found, verification=_load(row["verification"]) or {}, checked_with=None,
            recapture_of=recapture_source(db, missions.get(row["mission_id"], "")), created_at=at)
        counts["observations"] += insert_observation(db, observation)
    for item in backfill_events(db):
        counts["events"] += insert_event(db, item, at)
    return counts


def backfill_events(db: sqlite3.Connection) -> list[dict]:
    """The events the business tables' current state and times imply. / 由业务表现有状态与时间推出的事件。"""
    found: list[dict] = []
    findings = {row["finding_id"]: row for row in _rows(db, "SELECT finding_id, asset_key, first_job FROM bz_findings")}
    orders = {row["order_id"]: row for row in _rows(db, "SELECT * FROM bz_orders")}
    # Only a suspected result is ever linked to a finding. / 只有疑似结果会挂到发现上。
    for job in _rows(db, "SELECT * FROM bz_jobs WHERE finding_id IS NOT NULL ORDER BY job_id"):
        if job["finding_id"] in findings:
            found.append(finding_opened({**job, "inputs": _load(job["inputs"]), "result": _load(job["result"])},
                                        findings[job["finding_id"]]))
    for review in _rows(db, "SELECT * FROM bz_reviews ORDER BY review_id"):
        review = {**review, "basis": _load(review["basis"])}
        if review["subject_kind"] == "finding" and review["subject_id"] in findings:
            found.append(reviewed(review, finding=findings[review["subject_id"]]))
        elif review["subject_kind"] == "round" and review["subject_id"].rsplit(":r", 1)[0] in orders:
            found.append(reviewed(review, order=orders[review["subject_id"].rsplit(":r", 1)[0]]))
    for order_id in sorted(orders):
        found.append(order_opened(orders[order_id]))
    for row in _rows(db, "SELECT * FROM bz_rounds ORDER BY order_id, round"):
        order = orders.get(row["order_id"])
        if order is None:
            continue
        row = {**row, "feedback": _load(row["feedback"]), "conclusion": _load(row["conclusion"])}
        found.append(repair_reported(order, row))
        if row["conclusion"] is not None:
            job_id = row["conclusion"].get("job_id")
            verdict = db.execute("SELECT verdict FROM bz_jobs WHERE job_id=?", (job_id,)).fetchone() if job_id else None
            found.append(round_concluded(order, row["round"], row["conclusion"], verdict[0] if verdict else None))
    for reference in _rows(db, "SELECT * FROM bz_references ORDER BY reference_id"):
        reference = {**reference, "body": _load(reference["body"])}
        found.append(reference_registered(reference))
        if reference["revoked_at"] is not None:
            found.append(reference_revoked(reference))
    return found


# ── row writes shared by the backfill and the store / 回填与存储共用的行写入 ──


def insert_revision(db, revision: dict) -> bool:
    return db.execute("INSERT OR IGNORE INTO ar_revisions VALUES (?,?,?,?,?,?,?,?)",
                      (revision["asset_key"], revision["registry_sha256"], revision["project_id"], revision["scope"],
                       revision["asset_id"], revision["map_version"], _dump(revision["definition"]),
                       revision["first_seen_at"])).rowcount == 1


def insert_observation(db, observation: dict) -> bool:
    o = observation
    return db.execute("INSERT OR IGNORE INTO ar_observations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (o["evidence_id"], o["asset_key"], o["project_id"], o["mission_id"], o["mission_version"],
                       o["step_id"], o["registry_sha256"], o["captured_at"], o["media_sha256"],
                       _dump(o["geometry"]), _dump(o["verification"]), o["recapture_of"],
                       o["created_at"])).rowcount == 1


def insert_event(db, item: dict, created_at: str) -> bool:
    return db.execute("INSERT OR IGNORE INTO ar_events VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                      (item["event_id"], item["asset_key"], item["project_id"], item["kind"], item["authority"],
                       item["at"], item["actor"], _dump(item["refs"]), _dump(item["body"]), item["source_key"],
                       stamp(created_at))).rowcount == 1


# ── migration / 迁移 ──


def asset_schema(ledger: BusinessLedger) -> str | None:
    row = ledger._one("SELECT value FROM meta WHERE key='asset_schema'")
    return row["value"] if row else None


def _counts(db: sqlite3.Connection) -> dict[str, int]:
    names = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' "
                                          "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    return {name: db.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0] for name in names}


def _present(ledger: BusinessLedger, kind: str) -> set[str]:
    return {row["name"] for row in ledger._rows("SELECT name FROM sqlite_master WHERE type=?", (kind,))}


def migrate(ledger: BusinessLedger, context: ArchiveContext, *, backups: Path | None, clock=utcnow) -> dict:
    """Add the asset archive to a ledger with the business tables and backfill it; a ledger with data is backed up
    and verified first (D087). A migrated ledger is left as it is.

    为已有业务表的账本加上资产档案并回填；有数据的库先备份并校验（D087）。已迁移的账本保持原样。
    """
    with ledger._lock:
        if schema_version(ledger) != OPERATIONS_SCHEMA:
            raise MigrationError("the asset archive needs the P1 operations schema v2 first")
        missing = [name for name in BUSINESS_TABLES if name not in _present(ledger, "table")]
        if missing:
            raise MigrationError(f"the asset archive needs the P4 business tables first (missing {missing})")
        version = asset_schema(ledger)
        if version == ASSET_SCHEMA:
            gone = [name for name in (*TABLES, *INDEXES) if name not in _present(ledger, "table") | _present(
                ledger, "index")]
            if gone:
                raise MigrationError(f"asset schema {ASSET_SCHEMA} is missing {gone}")
            return {"status": "current", "asset_schema": ASSET_SCHEMA}
        if version is not None:
            raise MigrationError(f"unexpected asset schema version {version!r}")
        counts = _counts(ledger._db)
        backup = None
        has_data = any(counts.get(name, 0) for name in ("requests", "missions", "deliveries", "events", "op_events"))
        if ledger.path is not None and has_data:
            if backups is None:
                raise MigrationError("a ledger with data needs a backup directory before migrating")
            label = clock().strftime("%Y%m%dT%H%M%S%fZ")
            target = backups / f"ledger-v2-ar-{label}.sqlite3"
            ledger.backup(target)
            copy = sqlite3.connect(str(target))
            try:
                integrity = copy.execute("PRAGMA integrity_check").fetchone()[0]
                copied = _counts(copy)
            finally:
                copy.close()
            if integrity != "ok" or copied != counts:
                raise MigrationError("the ledger backup did not verify; nothing was migrated")
            digest = hashlib.sha256()
            with target.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1 << 20), b""):
                    digest.update(chunk)
            backup = {"path": target.name, "sha256": digest.hexdigest(), "rows": counts}
        at = stamp(clock())
        try:
            with ledger.transaction() as db:
                for statement in DDL:
                    db.execute(statement)
                filled = backfill(db, context, at)
                db.execute("INSERT INTO meta(key, value) VALUES ('asset_schema', ?)", (ASSET_SCHEMA,))
                db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('asset_migrated_at', ?)", (at,))
                db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('asset_backup', ?)",
                           (json.dumps(backup, sort_keys=True),))
        except sqlite3.Error as error:
            raise MigrationError(f"asset archive migration rolled back: {error}") from error
        return {"status": "migrated", "asset_schema": ASSET_SCHEMA, "backup": backup, "backfill": filled,
                "rows_before": counts}


def other_digest(path: Path) -> str:
    """SHA-256 over the schema and every row outside the archive, streamed. / 档案以外的模式与每一行的 SHA-256，流式计算。"""
    digest = hashlib.sha256()
    db = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        for row in db.execute("SELECT type, name, tbl_name, sql FROM sqlite_master WHERE substr(name, 1, 3) != 'ar_' "
                              "AND substr(tbl_name, 1, 3) != 'ar_' ORDER BY type, name"):
            digest.update(repr(tuple(row)).encode())
        tables = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' "
                                               "AND substr(name, 1, 3) != 'ar_' ORDER BY name")]
        for table in tables:
            digest.update(f"\x00{table}\x00".encode())
            for row in db.execute(f'SELECT * FROM "{table}" ORDER BY rowid'):
                if table == "meta" and str(row[0]).startswith("asset_"):
                    continue  # the migration's own markers / 迁移自己的标记
                digest.update(repr(row).encode())
    finally:
        db.close()
    return digest.hexdigest()


def archive_digest(path: Path) -> str:
    """SHA-256 over every archive row in key order: two copies migrated at the same instant must agree.

    按键顺序对每一行档案计算 SHA-256：同一时刻迁移的两份副本必须一致。
    """
    digest = hashlib.sha256()
    db = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        for table, order in (("ar_revisions", "asset_key, registry_sha256"), ("ar_observations", "evidence_id"),
                             ("ar_events", "event_id")):
            digest.update(f"\x00{table}\x00".encode())
            for row in db.execute(f"SELECT * FROM {table} ORDER BY {order}"):
                digest.update(repr(tuple(row)).encode())
    finally:
        db.close()
    return digest.hexdigest()


def eligible_captures(path: Path) -> int:
    """Captures the backfill must turn into observations, counted on their own: verified evidence of a bound mission
    whose step inspects an asset.

    回填必须转成观测的采集，单独计数：已绑定任务中、其步骤检查某资产、且有服务核验的证据。
    """
    db = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        steps: dict[tuple[str, int], set[str]] = {}
        for mission_id, version, package in db.execute(
                "SELECT v.mission_id, v.version, v.package FROM versions v JOIN op_bindings b "
                "ON b.mission_id = v.mission_id"):
            nodes = (json.loads(package) if package else {}).get("nodes", [])
            steps[(mission_id, version)] = {n["task_id"] for n in nodes if "asset_id" in (n.get("params") or {})}
        return sum(1 for mission_id, version, step in db.execute(
            "SELECT e.mission_id, e.mission_version, e.step_id FROM evidence e JOIN verifications v "
            "ON v.evidence_id = e.evidence_id") if step in steps.get((mission_id, version), set()))
    finally:
        db.close()


def drill(path: Path, context: ArchiveContext, *, clock=utcnow) -> dict:
    """D087 drill on a copy of a ledger: migrate it, migrate it again, and prove the other data unchanged and the
    observations complete; counts and digests only, never content.

    在账本副本上做 D087 演练：迁移、再迁移一次，证明其他数据不变且观测完整；只报告计数与摘要，从不报告内容。
    """
    started = time.monotonic()
    before = other_digest(path)
    expected = eligible_captures(path)
    ledger = BusinessLedger(path)
    try:
        report = migrate(ledger, context, backups=path.parent / "backups", clock=clock)
        again = migrate(ledger, context, backups=path.parent / "backups", clock=clock)
        with sqlite3.connect(str(path)) as db:
            integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
            counts = {name: db.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0] for name in TABLES}
            indexes = sorted(row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='index'")
                             if row[0].startswith("ar_"))
            unknown = db.execute("SELECT COUNT(*) FROM ar_observations WHERE json_extract(geometry, '$.status') "
                                 "!= 'measured'").fetchone()[0]
    finally:
        ledger.close()
    after = other_digest(path)
    complete = counts["ar_observations"] == expected
    return {"status": "passed" if report["status"] in ("migrated", "current") and again["status"] == "current"
            and integrity == "ok" and before == after and indexes == sorted(INDEXES) and complete else "failed",
            "migration": report["status"], "again": again["status"], "asset_schema": ASSET_SCHEMA,
            "integrity": integrity, "other_sha256_before": before, "other_sha256_after": after,
            "archive_sha256": archive_digest(path), "rows": counts, "eligible_captures": expected,
            "viewpoint_unknown": unknown, "backfill": report.get("backfill"), "backup": report.get("backup"),
            "elapsed_s": round(time.monotonic() - started, 1)}


def context_from_files(root: Path, catalog_path: Path, scheduling_path: Path | None) -> ArchiveContext:
    """The drill's context, read from the catalog files the service loads. / 演练的上下文，读取服务加载的同一批目录文件。"""
    from drone_agent.fleet.resources import load_catalog
    from drone_agent.fleet.scheduling_models import load_scheduling
    from drone_agent.mission.registry import Registry

    catalog = load_catalog(catalog_path)
    scheduling = load_scheduling(scheduling_path) if scheduling_path is not None else None
    registries = {site_id: Registry(root, scene=root / site.scene) for site_id, site in catalog.sites.items()}
    return ArchiveContext(catalog, scheduling, registries)


# ── the store / 存储 ──


class AssetStore:
    """Reads and writes of the archive tables; writes join the caller's transaction.

    档案表的读写；写入并入调用方的事务。
    """

    def __init__(self, ledger: BusinessLedger, *, clock=utcnow):
        if asset_schema(ledger) != ASSET_SCHEMA:
            raise MigrationError("run migrate() before opening the asset store")
        self.ledger, self.clock = ledger, clock

    def _now(self) -> str:
        return self.clock().isoformat()

    def transaction(self):
        return self.ledger.transaction()

    # writes / 写入

    def add_revision(self, revision: dict) -> bool:
        with self.transaction() as db:
            inserted = insert_revision(db, revision)
            if inserted:
                insert_event(db, revision_seen(revision), self._now())
        return inserted

    def add_observation(self, observation: dict) -> bool:
        with self.transaction() as db:
            return insert_observation(db, observation)

    def add_event(self, item: dict) -> bool:
        with self.transaction() as db:
            return insert_event(db, item, self._now())

    # reads / 读取

    @staticmethod
    def _revision(row: dict | None) -> dict | None:
        return None if row is None else {**row, "definition": _load(row["definition"])}

    @staticmethod
    def _observation(row: dict | None) -> dict | None:
        return None if row is None else {**row, "geometry": _load(row["geometry"]),
                                         "verification": _load(row["verification"])}

    @staticmethod
    def _event(row: dict | None) -> dict | None:
        return None if row is None else {**row, "refs": _load(row["refs"]), "body": _load(row["body"])}

    def revision(self, asset_key: str, registry_sha256: str | None) -> dict | None:
        if registry_sha256 is None:
            return None
        return self._revision(self.ledger._one("SELECT * FROM ar_revisions WHERE asset_key=? AND registry_sha256=?",
                                               (asset_key, registry_sha256)))

    def revisions(self, *, project_id: str | None = None, asset_key: str | None = None) -> list[dict]:
        clause, args = ("asset_key=?", (asset_key,)) if asset_key is not None else ("project_id=?", (project_id,))
        return [self._revision(r) for r in self.ledger._rows(
            f"SELECT * FROM ar_revisions WHERE {clause} ORDER BY first_seen_at, registry_sha256", args)]

    def observation(self, evidence_id: str) -> dict | None:
        return self._observation(self.ledger._one("SELECT * FROM ar_observations WHERE evidence_id=?", (evidence_id,)))

    def observations(self, asset_key: str, *, before: tuple[str, str] | None = None, limit: int = 200) -> list[dict]:
        """An asset's observations, newest capture first. / 资产的观测，按采集时间倒序。"""
        if before is None:
            rows = self.ledger._rows("SELECT * FROM ar_observations WHERE asset_key=? "
                                     "ORDER BY captured_at DESC, evidence_id DESC LIMIT ?", (asset_key, limit))
        else:
            rows = self.ledger._rows("SELECT * FROM ar_observations WHERE asset_key=? AND (captured_at < ? OR "
                                     "(captured_at = ? AND evidence_id < ?)) "
                                     "ORDER BY captured_at DESC, evidence_id DESC LIMIT ?",
                                     (asset_key, before[0], before[0], before[1], limit))
        return [self._observation(r) for r in rows]

    def project_assets(self, project_id: str) -> list[str]:
        keys = {r["asset_key"] for r in self.ledger._rows("SELECT DISTINCT asset_key FROM ar_revisions "
                                                          "WHERE project_id=?", (project_id,))}
        keys |= {r["asset_key"] for r in self.ledger._rows("SELECT DISTINCT asset_key FROM ar_observations "
                                                           "WHERE project_id=?", (project_id,))}
        return sorted(keys)

    def events(self, asset_key: str, *, before: tuple[str, str] | None = None, limit: int = 200,
               since: str | None = None, until: str | None = None) -> list[dict]:
        """An asset's events, newest first; `since` < at <= `until` when given. / 资产的事件，按时间倒序。"""
        clauses, args = ["asset_key=?"], [asset_key]
        if before is not None:
            clauses.append("(at < ? OR (at = ? AND event_id < ?))")
            args += [before[0], before[0], before[1]]
        if since is not None:
            clauses.append("at > ?")
            args.append(since)
        if until is not None:
            clauses.append("at <= ?")
            args.append(until)
        rows = self.ledger._rows(f"SELECT * FROM ar_events WHERE {' AND '.join(clauses)} "
                                 "ORDER BY at DESC, event_id DESC LIMIT ?", (*args, limit))
        return [self._event(r) for r in rows]

    def verification_changes(self, evidence_id: str, asset_key: str) -> list[dict]:
        """The `verification_changed` events of one observation, oldest first. / 一条观测的核验变化事件，按时间顺序。"""
        rows = self.ledger._rows("SELECT * FROM ar_events WHERE asset_key=? AND kind='verification_changed' "
                                 "AND json_extract(refs, '$.evidence_id')=? ORDER BY at, event_id",
                                 (asset_key, evidence_id))
        return [self._event(r) for r in rows]

    def event_by_source(self, source_key: str) -> dict | None:
        return self._event(self.ledger._one("SELECT * FROM ar_events WHERE source_key=?", (source_key,)))

    def jobs_of(self, evidence_id: str) -> list[dict]:
        """The analysis jobs of one capture, oldest first (read through `ar_jobs_evidence`). / 一次采集的分析作业。"""
        rows = self.ledger._rows("SELECT * FROM bz_jobs WHERE evidence_id=? ORDER BY created_at, job_id",
                                 (evidence_id,))
        return [{**r, "inputs": _load(r["inputs"]), "result": _load(r["result"])} for r in rows]


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--drill", type=Path, required=True, help="a copy of the ledger to migrate (D087 drill)")
    parser.add_argument("--root", type=Path, default=Path("/workspace"))
    parser.add_argument("--catalog", type=Path, required=True, help="the operations catalog the service loads")
    parser.add_argument("--scheduling", type=Path, default=None, help="the scheduling catalog the service loads")
    args = parser.parse_args()
    result = drill(args.drill, context_from_files(args.root, args.catalog, args.scheduling))
    print(json.dumps(result))
    raise SystemExit(0 if result["status"] == "passed" else 1)


if __name__ == "__main__":
    main()
