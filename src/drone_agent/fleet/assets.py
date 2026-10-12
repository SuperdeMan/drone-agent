"""WP-P6-04 asset archive engine: the write hooks, the per-asset reads and the deterministic comparison (D087).

`open_archive` runs once the service is assembled (the business engine and the scheduler attached, because the
scope of an asset key depends on the scheduling catalog): it migrates and backfills the archive, records a revision
for every asset of the current site maps, and attaches an `AssetArchive` to the business engine. The service and the
business loop then call its hooks inside the transactions of the transitions they record. Reads answer by authority:
`fact` (configuration, service checks, people), `decision` (deterministic rules, inputs recorded) and `belief` (models
and analyzers). `assets.compare` lists the factors between two captures, each with its evidence ids, computed only
from facts and decisions; model judgements are a separate column and the summary never says whether a difference is
a repair, the angle or a new anomaly.

WP-P6-04 资产档案引擎：写入挂钩、逐资产读取与确定性比较（D087）。

`open_archive` 在服务装配完成后运行（业务引擎与调度器都已挂上，因为资产键的范围取决于调度目录）：迁移并回填档案，为当前
站点地图中的每个资产记录修订，并把 `AssetArchive` 挂到业务引擎上。之后服务与业务闭环在其所记录转移的事务内调用这些挂钩。
读取按权威等级作答：`fact`（配置、服务核验、人工操作）、`decision`（确定性规则，输入一并记录）与 `belief`（模型与分析器）。
`assets.compare` 列出两次采集之间的因素，每项附证据 id，只由事实与决定计算；模型判断单列，摘要从不断言差异是维修、角度还是
新异常。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Literal

import yaml
from pydantic import Field

from drone_agent.contracts import utcnow
from drone_agent.fleet.asset_store import (
    ArchiveContext,
    AssetStore,
    definition,
    event,
    finding_opened,
    migrate,
    observation_row,
    order_opened,
    recapture_source,
    recover_maps,
    reference_registered,
    reference_revoked,
    repair_reported,
    repository_maps,
    reviewed,
    round_concluded,
    run_header,
    stamp,
    step_assets,
    viewpoint,
)
from drone_agent.fleet.business_models import REPAIR_ACTIONS, REQUEST_ID, BusinessModel
from drone_agent.fleet.service import ServiceError
from drone_agent.runtime.permission import ANALYSIS_READ, WORKFLOW_RUN

COMPARE_CONFIG = "configs/analysis/compare_v1.yaml"
# Fields a model or analyzer produces; none may appear in the fact column or in a comparison factor (design §4).
# 模型或分析器产出的字段；事实栏与比较因素中都不得出现（设计 §4）。
MODEL_FIELDS = frozenset({"score", "defect_type", "family", "verdict", "job_verdict", "anomaly_score", "model_id",
                          "reported_model_id", "description"})
DEFINITION_FIELDS = ("position", "size_m", "route", "observation_route", "kind", "camera_id", "visual_signature",
                     "volume", "frame", "above_asset_m")
CHECK_FIELDS = ("final_verdict", "service_verdict", "onboard_verdict", "agrees", "checks", "verified_at")


class CompareConfig(BusinessModel):
    """`compare-v1` tolerances (design §3.6). / `compare-v1` 容差（设计 §3.6）。"""

    format: Literal["drone.asset-compare/v1"]
    version: str = Field(pattern=r"^compare-v[0-9]+$")
    bearing_max_deg: float = Field(gt=0, le=180)
    bearing_min_horizontal_m: float = Field(ge=0, le=50)
    elevation_max_deg: float = Field(gt=0, le=90)
    distance_ratio: tuple[float, float]
    scale_ratio: tuple[float, float]
    offset_max_side_fraction: float = Field(gt=0, le=10)


def load_compare(root: Path) -> tuple[CompareConfig, str]:
    raw = (root / COMPARE_CONFIG).read_bytes()
    return CompareConfig.model_validate(yaml.safe_load(raw)), hashlib.sha256(raw).hexdigest()


def open_archive(service, *, backups: Path | None, clock=utcnow) -> dict:
    """Migrate and backfill the archive, snapshot the current site maps and attach the archive (D087).

    迁移并回填档案、快照当前站点地图并挂上档案（D087）。
    """
    engine = service.business
    scheduler = getattr(service, "scheduler", None)
    context = ArchiveContext(service.ops.catalog, scheduler.catalog if scheduler is not None else None,
                             service.ops.registries, repository_maps(service.root))
    migration = migrate(service.ledger, context, backups=backups, clock=clock)
    config, sha = load_compare(service.root)
    archive = AssetArchive(engine, AssetStore(service.ledger, clock=clock), context, config, sha)
    # A ledger migrated earlier gains the maps the repository still holds; revisions only (design §3.2).
    # 早先迁移的账本补上仓库中仍有正文的地图；只追加修订（设计 §3.2）。
    with service.ledger.transaction() as db:
        recovered = recover_maps(db, context, stamp(clock()))
    added = archive.snapshot_maps()
    engine.archive = archive
    return {"migration": migration, "maps": {"revisions_added": added, "recovered": recovered},
            "compare": {"version": config.version, "sha256": sha}}


def _json(text):
    return None if text is None else json.loads(text)


def _angle(a: float, b: float) -> float:
    difference = abs(a - b) % 360
    return min(difference, 360 - difference)


def _cursor(value) -> tuple[str, str] | None:
    if value is None:
        return None
    if not isinstance(value, str) or value.count("|") != 1 or len(value) > 200:
        raise ServiceError("service.invalid_request", "before is a cursor from a previous page or null")
    at, identity = value.split("|")
    try:
        return stamp(at), identity
    except ValueError as error:
        raise ServiceError("service.invalid_request", "before is a cursor from a previous page or null") from error


class AssetArchive:
    """The asset archive of one business engine. / 一个业务引擎的资产档案。"""

    def __init__(self, engine, store: AssetStore, context: ArchiveContext, config: CompareConfig, config_sha: str):
        self.engine, self.store, self.context, self.config, self.config_sha = engine, store, context, config, \
            config_sha

    @property
    def service(self):
        return self.engine.service

    @property
    def ledger(self):
        return self.engine.service.ledger

    def now(self) -> str:
        return stamp(self.engine.clock())

    # ── writes / 写入 ──

    def snapshot_maps(self) -> int:
        """A revision for every asset of the current site maps; a digest already recorded writes nothing.

        为当前站点地图的每个资产记录修订；已记录的摘要不再写入。
        """
        added = 0
        for site_id, project_id, scope, registry in self.context.current():
            for asset_id in sorted(registry.data.get("assets") or {}):
                revision = {"asset_key": f"{project_id}/{scope}/{asset_id}", "registry_sha256": registry.sha256,
                            "project_id": project_id, "scope": scope, "asset_id": asset_id,
                            "map_version": str((registry.data.get("frame") or {}).get("map_version", "")),
                            "definition": definition(registry, site_id, asset_id), "first_seen_at": self.now()}
                added += self.store.add_revision(revision)
        return added

    def observe(self, *, mission_id: str, version: int, evidence_row: dict, verification: dict,
                checked_with: str) -> None:
        """The observation of a capture the service just verified, inside the verification's transaction; a later
        check against the same map that changes the result appends `verification_changed`.

        服务刚核验的采集的观测，在核验的事务内写入；之后用同一张地图的核验若改变结果，追加 `verification_changed`。
        """
        binding = self.service._binding(mission_id)
        record = self.ledger.version(mission_id, version) if binding is not None else None
        asset_id = step_assets((record or {}).get("package")).get(evidence_row["step_id"])
        if asset_id is None:
            return
        key = self.context.key(binding["project_id"], binding["site_id"], asset_id)
        known = self.store.observation(evidence_row["evidence_id"])
        with self.store.transaction() as db:
            if known is None:
                header = run_header(record.get("decision"))
                header_sha = header.registry_sha256 if header else None
                revision = self.store.revision(key, header_sha)
                mission = self.ledger.mission(mission_id)
                self.store.add_observation(observation_row(
                    evidence=evidence_row, asset_key=key, project_id=binding["project_id"], header_sha=header_sha,
                    known=revision["definition"] if revision else None, verification=verification,
                    checked_with=checked_with, recapture_of=recapture_source(db, mission["request_id"]),
                    created_at=self.now()))
                return
            if checked_with != known["registry_sha256"]:
                return  # a check against another map is not this capture's / 用别的地图做的核验不属于这次采集
            current = self.verification(known)
            if all(current.get(k) == verification.get(k) for k in ("final_verdict", "checks")):
                return
            changes = self.store.verification_changes(known["evidence_id"], key)
            self.store.add_event(event(
                "verification_changed", asset_key=key, project_id=binding["project_id"],
                at=verification.get("verified_at") or self.now(), actor="service",
                refs={"evidence_id": known["evidence_id"], "mission_id": mission_id},
                body={"verification": {**{k: verification.get(k) for k in CHECK_FIELDS},
                                       "registry_sha256": checked_with}},
                source_key=f"verification:{known['evidence_id']}:{len(changes) + 1}"))

    def finding_opened(self, job_id: str) -> None:
        job = self.engine.store.job(job_id)
        self.store.add_event(finding_opened(job, self.engine.store.finding(job["finding_id"])))

    def reviewed(self, review: dict) -> None:
        if review["subject_kind"] == "finding":
            self.store.add_event(reviewed(review, finding=self.engine.store.finding(review["subject_id"])))
        else:
            order = self.engine.store.order(review["subject_id"].rsplit(":r", 1)[0])
            self.store.add_event(reviewed(review, order=order))

    def order_opened(self, order: dict) -> None:
        self.store.add_event(order_opened(order))

    def repair_reported(self, order: dict, number: int) -> None:
        self.store.add_event(repair_reported(order, self.engine.store.round(order["order_id"], number)))

    def round_concluded(self, order: dict, number: int, conclusion: dict) -> None:
        job = self.engine.store.job(conclusion["job_id"]) if conclusion.get("job_id") else None
        self.store.add_event(round_concluded(order, number, conclusion, job["verdict"] if job else None))

    def reference_changed(self, reference: dict) -> None:
        self.store.add_event(reference_registered(reference) if reference["revoked_at"] is None
                             else reference_revoked(reference))

    # ── reads / 读取 ──

    def _require(self, caller, project_id, asset, scope: str = ANALYSIS_READ) -> str:
        self.engine.require(caller, project_id, ANALYSIS_READ)
        if not isinstance(asset, str) or not asset.startswith(project_id + "/") or len(asset) > 200 \
                or not self.known(asset):
            raise ServiceError("service.not_found", str(asset)[:80])
        if scope != ANALYSIS_READ:
            self.engine.require(caller, project_id, scope)
        return asset

    def known(self, asset: str) -> bool:
        return self.ledger._one("SELECT 1 AS found FROM ar_revisions WHERE asset_key=? LIMIT 1", (asset,)) is not None \
            or self.ledger._one("SELECT 1 AS found FROM ar_observations WHERE asset_key=? LIMIT 1", (asset,)) is not None

    def geometry(self, observation: dict) -> dict:
        """The stored viewpoint; for an observation written while its map's body was unknown, the viewpoint measured
        now from the evidence's capture pose once the definition was recovered (design §3.2).

        存储的视角；对写入时地图正文未知的观测，在定义找回之后，用证据中的拍摄位姿当场计算视角（设计 §3.2）。
        """
        stored = observation["geometry"]
        if stored.get("status") == "measured" or stored.get("reason") != "definition_unknown":
            return stored
        revision = self.store.revision(observation["asset_key"], observation["registry_sha256"])
        if revision is None:
            return stored
        row = next((r for r in self.ledger.evidence(observation["mission_id"], observation["mission_version"])
                    if r["evidence_id"] == observation["evidence_id"]), None)
        measured = viewpoint(((row or {}).get("body") or {}).get("captured_pose"), revision["definition"])
        return {**measured, "definition_recovered": True} if measured.get("status") == "measured" else stored

    def verification(self, observation: dict) -> dict:
        """An observation's verification, the last `verification_changed` winning. / 观测的核验，以最后一次变化为准。"""
        changes = self.store.verification_changes(observation["evidence_id"], observation["asset_key"])
        return changes[-1]["body"]["verification"] if changes else observation["verification"]

    def measures(self, evidence_id: str) -> dict | None:
        """The image measures of a capture from its first quality-v2 analysis; deterministic, a fact.

        取自首个 quality-v2 分析的采集图像测量；确定性的，属于事实。
        """
        for job in self.store.jobs_of(evidence_id):
            quality = (job["result"] or {}).get("quality") or {}
            if "target_side_px" in quality:
                return {"side_px": quality["target_side_px"], "offset_px": quality.get("target_offset_px"),
                        "visible_min": quality.get("target_visible_min"), "depth_m": quality.get("target_depth_m"),
                        "job_id": job["job_id"], "quality_sha256": (job["inputs"] or {}).get("quality_sha256")}
        return None

    @staticmethod
    def belief(job: dict) -> dict:
        """What a model or analyzer said about a capture (belief). / 模型或分析器对采集的判断（belief）。"""
        result = job["result"] if isinstance(job["result"], dict) else _json(job["result"]) or {}
        return {"job_id": job["job_id"], "purpose": job["purpose"], "analyzer": job["analyzer"], "state": job["state"],
                "verdict": job["verdict"], "source": job["source"], "score": result.get("score"),
                "defect_type": result.get("defect_type"), "reasons": result.get("reasons", []),
                "finding_id": job["finding_id"], "at": job["updated_at"]}

    def beliefs(self, evidence_id: str) -> list[dict]:
        return [self.belief(job) for job in self.store.jobs_of(evidence_id)]

    def _observation_view(self, observation: dict) -> dict:
        return {k: observation[k] for k in ("evidence_id", "mission_id", "mission_version", "step_id", "captured_at",
                                            "registry_sha256", "media_sha256", "recapture_of")} | {
            "geometry": self.geometry(observation), "verification": self.verification(observation),
            "measures": self.measures(observation["evidence_id"])}

    def list(self, caller, project_id) -> dict:
        """The project's assets, each with its latest observation and open findings. / 项目的资产及其最近观测与未结发现。"""
        self.engine.require(caller, project_id, ANALYSIS_READ)
        open_findings = {row["asset_key"]: row["n"] for row in self.ledger._rows(
            "SELECT asset_key, COUNT(*) AS n FROM bz_findings WHERE project_id=? AND state IN ('candidate', 'confirmed') "
            "GROUP BY asset_key", (project_id,))}
        revisions: dict[str, list[dict]] = {}
        for revision in self.store.revisions(project_id=project_id):
            revisions.setdefault(revision["asset_key"], []).append(revision)
        assets = []
        for key in self.store.project_assets(project_id):
            latest = (self.store.observations(key, limit=1) or [None])[0]
            current = (revisions.get(key) or [None])[-1]
            found = (current or {}).get("definition") or {}
            assets.append({"asset_key": key, "asset_id": key.rsplit("/", 1)[1], "scope": key.split("/")[1],
                           "definition": {k: found.get(k) for k in ("kind", "position", "size_m", "registry_id",
                                                                     "site_id")} if current else None,
                           "revisions": len(revisions.get(key, [])),
                           "latest_observation": None if latest is None else {
                               "evidence_id": latest["evidence_id"], "mission_id": latest["mission_id"],
                               "captured_at": latest["captured_at"],
                               "final_verdict": self.verification(latest).get("final_verdict"),
                               "viewpoint": self.geometry(latest).get("status")},
                           "open_findings": open_findings.get(key, 0)})
        return {"project_id": project_id, "assets": assets,
                "compare": {"version": self.config.version, "sha256": self.config_sha}}

    def timeline(self, caller, project_id, asset, before, limit) -> dict:
        """Observations, events and analyses of one asset, newest first, by authority, with a cursor.

        一个资产的观测、事件与分析，按时间倒序、按权威等级分栏，带游标。
        """
        key = self._require(caller, project_id, asset)
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 200:
            raise ServiceError("service.invalid_request", "limit is an integer from 1 to 200")
        cursor = _cursor(before)
        items = []
        for observation in self.store.observations(key, before=cursor, limit=limit + 1):
            items.append({"at": observation["captured_at"], "id": observation["evidence_id"], "kind": "observation",
                          "authority": "fact", "refs": {"evidence_id": observation["evidence_id"],
                                                        "mission_id": observation["mission_id"]},
                          "body": self._observation_view(observation)})
        for item in self.store.events(key, before=cursor, limit=limit + 1):
            items.append({"at": item["at"], "id": item["event_id"], "kind": item["kind"],
                          "authority": item["authority"], "actor": item["actor"], "refs": item["refs"],
                          "body": item["body"]})
        extra = (cursor[0], cursor[0], cursor[1]) if cursor else ()
        where = "asset_key=?" + (" AND (created_at < ? OR (created_at = ? AND job_id < ?))" if cursor else "")
        for job in self.ledger._rows(f"SELECT * FROM bz_jobs WHERE {where} ORDER BY created_at DESC, job_id DESC "
                                     "LIMIT ?", (key, *extra, limit + 1)):
            items.append({"at": stamp(job["created_at"]), "id": job["job_id"], "kind": "analysis",
                          "authority": "belief", "refs": {"job_id": job["job_id"], "evidence_id": job["evidence_id"],
                                                          "finding_id": job["finding_id"]}, "body": self.belief(job)})
        items.sort(key=lambda item: (item["at"], item["id"]), reverse=True)
        page = items[:limit]
        columns: dict[str, list[dict]] = {"fact": [], "decision": [], "belief": []}
        for item in page:
            columns[item["authority"]].append(item)
        return {"asset_key": key, "columns": columns, "items": len(page),
                "next": f"{page[-1]['at']}|{page[-1]['id']}" if len(items) > limit else None}

    def maintenance(self, caller, project_id, asset, request_id, action, part, performed_at, note) -> dict:
        """Maintenance outside a work order: one `maintenance_recorded` event, no order, no reinspection.

        工单之外的维护：只写一条 `maintenance_recorded` 事件，不开工单、不启动复检。
        """
        key = self._require(caller, project_id, asset, WORKFLOW_RUN)
        if not isinstance(request_id, str) or not REQUEST_ID.fullmatch(request_id):
            raise ServiceError("service.invalid_request", "request_id must be 1-120 safe characters")
        if action not in REPAIR_ACTIONS:
            raise ServiceError("service.invalid_request", "action is one of " + ", ".join(REPAIR_ACTIONS))
        if not isinstance(part, str) or len(part) > 80 or not isinstance(note, str) or len(note) > 300:
            raise ServiceError("service.invalid_request", "part is at most 80 characters and note at most 300")
        try:
            moment = datetime.fromisoformat(str(performed_at))
        except ValueError as error:
            raise ServiceError("service.invalid_request", "performed_at is an ISO time with a zone") from error
        if moment.tzinfo is None or moment > self.engine.clock():
            raise ServiceError("service.invalid_request", "performed_at has a zone and is not in the future")
        source = "maintenance:" + hashlib.sha256(f"{caller.identity}\n{request_id}".encode()).hexdigest()[:32]
        self.store.add_event(event("maintenance_recorded", asset_key=key, project_id=project_id, at=moment,
                                   actor=caller.identity, refs={"request_id": request_id},
                                   body={"action": action, "part": part, "performed_at": stamp(moment),
                                         "note": note}, source_key=source))
        return self.store.event_by_source(source)

    # ── comparison / 比较 ──

    def compare(self, caller, project_id, asset, evidence, against) -> dict:
        """The factors between two captures of one asset (design §3.6). / 同一资产两次采集之间的因素（设计 §3.6）。"""
        key = self._require(caller, project_id, asset)
        observations = self.store.observations(key, limit=2000)
        verified = [o for o in observations if self.verification(o).get("final_verdict") == "verified"]
        current = self._pick(key, evidence, verified[0] if verified else None, "evidence")
        role = "given"
        other = None
        if against is not None:
            other = self._pick(key, against, None, "against")
        else:
            reference = self._reference_at(key, current["captured_at"])
            if reference is not None:
                other, role = self.store.observation(reference["evidence_id"]), "reference"
            if other is None:
                role = "previous"
                other = next((o for o in verified if o["captured_at"] < current["captured_at"]), None)
        if other is None:
            raise ServiceError("service.not_found", "no earlier verified observation to compare against")
        if other["evidence_id"] == current["evidence_id"]:
            raise ServiceError("service.invalid_request", "evidence and against are the same capture")
        earlier, later = sorted((current, other), key=lambda o: (o["captured_at"], o["evidence_id"]))
        factors = {"definition": self._definition_factor(key, current, other),
                   "viewpoint": self._viewpoint_factor(current, other),
                   "events": self._events_factor(key, earlier, later),
                   "reference": self._reference_factor(key, other, earlier, later)}
        beliefs = {"evidence": self.beliefs(current["evidence_id"]), "against": self.beliefs(other["evidence_id"])}
        return {"asset_key": key, "compare": {"version": self.config.version, "sha256": self.config_sha},
                "evidence": self._observation_view(current), "against": {**self._observation_view(other),
                                                                          "role": role},
                "factors": factors, "beliefs": beliefs, "summary": self._summary(factors, beliefs["evidence"])}

    def _pick(self, key: str, evidence_id, default: dict | None, name: str) -> dict:
        if evidence_id is None:
            if default is None:
                raise ServiceError("service.not_found", "the asset has no verified observation")
            return default
        found = self.store.observation(evidence_id) if isinstance(evidence_id, str) else None
        if found is None or found["asset_key"] != key:
            raise ServiceError("service.not_found", f"{name} {str(evidence_id)[:80]}")
        return found

    def _reference_at(self, key: str, captured_at: str) -> dict | None:
        """The newest reference registered before the capture and not revoked by then. / 采集前登记且届时未撤销的最新参考。"""
        for reference in self.engine.store.references(asset=key):
            if stamp(reference["registered_at"]) <= captured_at and (
                    reference["revoked_at"] is None or stamp(reference["revoked_at"]) > captured_at):
                return reference
        return None

    def _definition_factor(self, key: str, current: dict, other: dict) -> dict:
        shas = [current["registry_sha256"], other["registry_sha256"]]
        evidence = [current["evidence_id"], other["evidence_id"]]
        found = [self.store.revision(key, sha) for sha in shas]
        if None in found:
            return {"status": "unknown", "registry_sha256": shas, "evidence": evidence,
                    "reason": "the definition of a capture's map is not in the archive"}
        if shas[0] == shas[1]:
            return {"status": "same", "registry_sha256": shas, "evidence": evidence}
        changed = [field for field in DEFINITION_FIELDS
                   if found[0]["definition"].get(field) != found[1]["definition"].get(field)]
        return {"status": "changed" if changed else "unchanged", "registry_sha256": shas, "evidence": evidence,
                "changed_fields": changed,
                "values": {field: {"evidence": found[0]["definition"].get(field),
                                   "against": found[1]["definition"].get(field)} for field in changed}}

    def _viewpoint_factor(self, current: dict, other: dict) -> dict:
        """Whether both captures saw the asset from the same viewpoint, within `compare-v1`. / 两次采集是否同一视角。"""
        evidence = [current["evidence_id"], other["evidence_id"]]
        a, b = self.geometry(current), self.geometry(other)
        if a.get("status") != "measured" or b.get("status") != "measured":
            reason = a.get("reason") if a.get("status") != "measured" else b.get("reason")
            return {"status": "unknown", "reason": reason, "evidence": evidence}
        if a["frame"] != b["frame"]:
            return {"status": "unknown", "reason": "frame_mismatch", "evidence": evidence}
        config = self.config
        measures: dict = {"distance_ratio": round(a["distance_m"] / b["distance_m"], 4) if b["distance_m"] else None,
                          "elevation_diff_deg": round(abs(a["elevation_deg"] - b["elevation_deg"]), 2),
                          "horizontal_m": [a["horizontal_m"], b["horizontal_m"]], "bearing_diff_deg": None}
        exceeded = []
        low, high = config.distance_ratio
        if measures["distance_ratio"] is None or not low <= measures["distance_ratio"] <= high:
            exceeded.append("distance_ratio")
        if measures["elevation_diff_deg"] > config.elevation_max_deg:
            exceeded.append("elevation_diff_deg")
        if min(a["horizontal_m"], b["horizontal_m"]) >= config.bearing_min_horizontal_m \
                and a["bearing_deg"] is not None and b["bearing_deg"] is not None:
            measures["bearing_diff_deg"] = round(_angle(a["bearing_deg"], b["bearing_deg"]), 2)
            if measures["bearing_diff_deg"] > config.bearing_max_deg:
                exceeded.append("bearing_diff_deg")
        image = [self.measures(current["evidence_id"]), self.measures(other["evidence_id"])]
        if all(m and m.get("side_px") and m.get("offset_px") is not None for m in image):
            measures["scale_ratio"] = round(image[0]["side_px"] / image[1]["side_px"], 4)
            measures["offset_diff_side"] = round(abs(image[0]["offset_px"] / image[0]["side_px"]
                                                     - image[1]["offset_px"] / image[1]["side_px"]), 4)
            low, high = config.scale_ratio
            if not low <= measures["scale_ratio"] <= high:
                exceeded.append("scale_ratio")
            if measures["offset_diff_side"] > config.offset_max_side_fraction:
                exceeded.append("offset_diff_side")
        return {"status": "different" if exceeded else "consistent", "measures": measures, "exceeded": exceeded,
                "evidence": evidence, "jobs": [m["job_id"] for m in image if m]}

    def _events_factor(self, key: str, earlier: dict, later: dict) -> dict:
        """Facts and decisions between the two capture times; a decision's model inputs stay out.

        两次拍摄时刻之间的事实与决定；决定的模型输入不进入因素。
        """
        found = []
        for item in reversed(self.store.events(key, since=earlier["captured_at"], until=later["captured_at"],
                                               limit=500)):
            if item["authority"] == "belief":
                continue
            body = {k: v for k, v in item["body"].items() if k not in MODEL_FIELDS}
            found.append({"kind": item["kind"], "authority": item["authority"], "at": item["at"],
                          "actor": item["actor"], "refs": item["refs"], "body": body, "event_id": item["event_id"]})
        kinds = [item["kind"] for item in found]
        return {"between": [earlier["captured_at"], later["captured_at"]], "items": found,
                "repairs": kinds.count("repair_reported"), "maintenance": kinds.count("maintenance_recorded"),
                "reviews": sum(kinds.count(k) for k in ("finding_confirmed", "finding_dismissed", "round_reviewed")),
                "settlements": kinds.count("round_concluded"), "definition_changes": kinds.count("revision_seen")}

    def _reference_factor(self, key: str, other: dict, earlier: dict, later: dict) -> dict:
        references = [r for r in self.engine.store.references(asset=key) if r["evidence_id"] == other["evidence_id"]]
        if not references:
            return {"against_is_reference": False}
        reference = references[0]
        revoked = reference["revoked_at"] is not None and earlier["captured_at"] < stamp(reference["revoked_at"]) \
            <= later["captured_at"]
        return {"against_is_reference": True, "reference_id": reference["reference_id"], "state": reference["state"],
                "revoked_between": revoked}

    @staticmethod
    def _summary(factors: dict, beliefs: list[dict]) -> dict:
        """A deterministic statement of the factors; it never names a cause. / 因素的确定性陈述；从不指明原因。"""
        events = factors["events"]
        repairs = [i for i in events["items"] if i["kind"] in ("repair_reported", "maintenance_recorded")]
        zh, en = [], []
        if repairs:
            parts_zh, parts_en = [], []
            for item in repairs:
                repair = item["body"].get("repair") if item["kind"] == "repair_reported" else item["body"]
                what = f"{repair['action']} {repair.get('part') or ''}".strip() if repair else ""
                if item["kind"] == "repair_reported":
                    parts_zh.append(f"工单 {item['refs']['order_id']} 第 {item['refs']['round']} 轮" +
                                    (f"，{what}" if what else ""))
                    parts_en.append(f"order {item['refs']['order_id']} round {item['refs']['round']}" +
                                    (f", {what}" if what else ""))
                else:
                    parts_zh.append(f"维护，{what}")
                    parts_en.append(f"maintenance, {what}")
            zh.append(f"两次采集之间登记了 {len(repairs)} 次维修或维护（{'；'.join(parts_zh)}）")
            en.append(f"{len(repairs)} repair or maintenance record(s) between the captures ({'; '.join(parts_en)})")
        else:
            zh.append("两次采集之间没有维修或维护登记")
            en.append("no repair or maintenance recorded between the captures")
        if events["reviews"] or events["settlements"]:
            zh.append(f"期间复核 {events['reviews']} 次、结算 {events['settlements']} 次")
            en.append(f"{events['reviews']} review(s) and {events['settlements']} settlement(s) in between")
        view = factors["viewpoint"]
        if view["status"] == "unknown":
            zh.append(f"视角未知（{view.get('reason')}）")
            en.append(f"viewpoint unknown ({view.get('reason')})")
        else:
            m = view["measures"]
            numbers_zh = [f"距离比 {m['distance_ratio']}", f"俯仰差 {m['elevation_diff_deg']}°"]
            numbers_en = [f"distance ratio {m['distance_ratio']}", f"elevation difference {m['elevation_diff_deg']}°"]
            if m.get("bearing_diff_deg") is not None:
                numbers_zh.append(f"方位差 {m['bearing_diff_deg']}°")
                numbers_en.append(f"bearing difference {m['bearing_diff_deg']}°")
            if m.get("scale_ratio") is not None:
                numbers_zh.append(f"尺度比 {m['scale_ratio']}")
                numbers_en.append(f"scale ratio {m['scale_ratio']}")
            state_zh = "视角一致" if view["status"] == "consistent" else "视角不同，超出：" + "、".join(view["exceeded"])
            state_en = "same viewpoint" if view["status"] == "consistent" else \
                "different viewpoint, beyond: " + ", ".join(view["exceeded"])
            zh.append(f"{state_zh}（{'，'.join(numbers_zh)}）")
            en.append(f"{state_en} ({', '.join(numbers_en)})")
        definition_ = factors["definition"]
        zh.append({"same": "地图未变", "unchanged": "地图已换，资产定义未变",
                   "unknown": "资产定义未知"}.get(definition_["status"],
                                                  "资产定义已变：" + "、".join(definition_.get("changed_fields", []))))
        en.append({"same": "same site map", "unchanged": "new site map, same asset definition",
                   "unknown": "asset definition unknown"}.get(
            definition_["status"], "asset definition changed: " + ", ".join(definition_.get("changed_fields", []))))
        done = [b for b in beliefs if b["state"] in ("completed", "refused")]
        latest = done[-1] if done else None
        verdict_zh = {"suspected": "疑似异常", "normal": "正常", "refused": "拒判"}
        if latest is None:
            zh.append("模型推测：无")
            en.append("model belief: none")
        else:
            zh.append(f"模型推测：本次{verdict_zh.get(latest['verdict'], latest['verdict'])}（{latest['analyzer']}）")
            en.append(f"model belief: this capture {latest['verdict']} ({latest['analyzer']})")
        return {"zh": "；".join(zh) + "。", "en": "; ".join(en) + "."}
