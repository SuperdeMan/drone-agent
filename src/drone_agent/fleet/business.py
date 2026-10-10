"""P4 business engine: the analysis jobs, findings, reviews, work orders and references of one mission service (D063).

`build_business` loads and checks the business catalog against the operations and workflow catalogs, migrates the
ledger (D064) and opens the store. `BusinessEngine` lives in the mission-service process next to the workflow engine:
the workflow engine calls it for P4 nodes (queue an analysis, wait for a review, create an order, settle a round),
the API calls it for findings, orders, analyses and references, and the service loop awaits its `tick` to run
jobs. It never builds or signs a package, approves a mission or talks to a robot, and none of its records is a
flight verdict.

P4 业务引擎：一个任务服务的分析作业、发现、复核、工单与参考外观（D063）。

`build_business` 加载业务目录并与运营、工作流目录核对，迁移账本（D064）并打开存储。`BusinessEngine` 与工作流引擎一同位于
任务服务进程内：工作流引擎为 P4 节点调用它（排队分析、等待复核、建单、结算轮次），API 为发现、工单、分析与参考外观调用
它，服务循环等待它的 `tick` 来运行作业。它从不构造或签名任务包、审批任务或与机器人通信，它的记录也都不是飞行判定。
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from drone_agent.contracts import utcnow
from drone_agent.fleet.analysis import analyze_model
from drone_agent.fleet.analysis_jobs import AnalysisJobs, redact_images
from drone_agent.fleet.business_models import (
    REQUEST_ID,
    BusinessCatalog,
    JobPurpose,
    JobState,
    ModelProfile,
    OrderState,
    QualityProfile,
    ReviewSubject,
    load_catalog,
    load_profile,
    load_quality,
    round_subject,
)
from drone_agent.fleet.business_store import BusinessStore, migrate
from drone_agent.fleet.findings import Findings
from drone_agent.fleet.media_index import MediaIndex
from drone_agent.fleet.service import ServiceError
from drone_agent.fleet.work_orders import WorkOrders
from drone_agent.fleet.workflow_models import (
    AnalysisOutput,
    ModelAnalyzer,
    NodeState,
    ReviewOutput,
    WaitReason,
    activity_key,
    run_principal,
)
from drone_agent.runtime.permission import (
    ANALYSIS_READ,
    ANALYSIS_REFERENCE,
    ANALYSIS_RUN,
    TRUST_LEVEL_CAPS,
    WORKFLOW_REVIEW,
    Caller,
    TrustLevel,
)


@dataclass
class Business:
    """Everything the service needs for the business loop. / 服务运行业务闭环所需的全部内容。"""

    catalog: BusinessCatalog
    store: BusinessStore
    default_quality: QualityProfile
    default_quality_sha: str
    migration: dict = field(default_factory=dict)


def build_business(root: Path, ledger, path: Path, operations, workflows, *, backups: Path | None,
                   clock=utcnow) -> Business:
    """Load and check the business catalog, migrate the ledger (D064) and open the store.

    加载并核对业务目录、迁移账本（D064）并打开存储。
    """
    catalog = load_catalog(path)
    if catalog.operations_catalog != operations.catalog.catalog_id:
        raise ValueError(f"written for {catalog.operations_catalog}, loaded with {operations.catalog.catalog_id}")
    if catalog.workflow_catalog != workflows.catalog.catalog_id:
        raise ValueError(f"written for {catalog.workflow_catalog}, loaded with {workflows.catalog.catalog_id}")
    quality, quality_sha = load_quality(root, catalog.default_quality)
    unknown = [name for name in catalog.reuse.analyzers if name not in workflows.catalog.analyzers]
    if unknown:
        raise ValueError(f"reuse names unknown analyzers {unknown}")
    migration = migrate(ledger, backups=backups, clock=clock)
    store = BusinessStore(ledger, catalog, {catalog.default_quality: quality_sha}, clock=clock)
    return Business(catalog, store, quality, quality_sha, migration)


def first_party(identity: str) -> Caller:
    return Caller(identity, TrustLevel.FIRST_PARTY, TRUST_LEVEL_CAPS[TrustLevel.FIRST_PARTY])


class BusinessEngine:
    """The P4 business loop inside the mission service. / 任务服务内的 P4 业务闭环。"""

    def __init__(self, service, business: Business, *, root: Path, vision=None, recordings: Path | None = None,
                 worker_id: str | None = None):
        self.service, self.business, self.root = service, business, root
        self.store, self.catalog = business.store, business.catalog
        # (provider, config) of the vision role, or None: model jobs then refuse `model.unavailable`.
        # 视觉角色的（provider，配置）或 None：此时模型作业以 `model.unavailable` 拒判。
        self.vision = vision
        self.recordings = recordings
        self.worker = worker_id or "bzw-" + uuid.uuid4().hex[:12]
        self.index = MediaIndex(service)
        self.jobs = AnalysisJobs(self)
        self.findings = Findings(self)
        self.orders = WorkOrders(self)
        self._profiles: dict[tuple[str, str], object] = {}

    @property
    def ledger(self):
        return self.service.ledger

    @property
    def ops(self):
        return self.service.ops

    @property
    def workflows(self):
        return self.service.workflows

    @property
    def workflow_store(self):
        return self.service.workflows.store

    def clock(self) -> datetime:
        return self.service.clock()

    # ── permissions / 权限 ──

    def require(self, caller: Caller | None, project_id, scope: str) -> None:
        directory = self.ops.directory
        if caller is None or not isinstance(project_id, str) or project_id not in self.ops.catalog.projects \
                or not directory.allows(caller, project_id, ANALYSIS_READ):
            raise ServiceError("service.not_found", str(project_id)[:40])
        if scope != ANALYSIS_READ and not directory.allows(caller, project_id, scope):
            raise ServiceError("auth.project_denied", f"{scope} in {project_id}")

    def reviewer_holds(self, identity: str, project_id: str) -> bool:
        return self.ops.directory.allows(first_party(identity), project_id, WORKFLOW_REVIEW)

    # ── pinned profiles / 固定画像 ──

    def _pinned(self, loader, relative: str, sha: str):
        key = (relative, sha)
        if key not in self._profiles:
            try:
                value, actual = loader(self.root, relative)
            except (ValueError, OSError):
                return None
            if actual != sha:
                return None
            self._profiles[key] = value
        return self._profiles[key]

    def model_profile(self, relative: str, sha: str) -> ModelProfile | None:
        """The profile only while its file still has the pinned digest. / 只在文件仍为固定摘要时返回画像。"""
        return self._pinned(load_profile, relative, sha)

    def quality_profile(self, relative: str, sha: str) -> QualityProfile | None:
        return self._pinned(load_quality, relative, sha)

    async def ask_model(self, job: dict, profile: ModelProfile, references, image, asset: str, measures: dict):
        """One model analysis with the configured vision provider, recorded as a `recorded` fixture.

        用配置的视觉 provider 做一次模型分析，并录制为 `recorded` 夹具。
        """
        from drone_agent.providers import RecordingProvider

        provider, config = self.vision if self.vision is not None else (None, None)
        recorder = RecordingProvider(provider, redact=redact_images) if provider is not None else None
        result = await analyze_model(profile=profile, threshold=profile.threshold, provider=recorder,
                                     provider_id=config.provider_id if config else "",
                                     model=config.model if config else profile.model, references=references,
                                     current=image, asset=asset, prices=self.catalog.prices, measures=measures)
        digest = self.jobs.save_recording(recorder, provider_id=config.provider_id if config else "",
                                          model=config.model if config else "",
                                          endpoint_host=config.endpoint_host if config else "", profile=profile,
                                          label=f"{job['job_id']}-a{job['attempts']}", directory=self.recordings)
        return result.model_copy(update={"recording_sha256": digest}) if digest else result

    async def tick(self) -> set[str]:
        return await self.jobs.tick()

    # ── workflow hooks / 工作流挂钩 ──

    def asset_description(self, mission_id: str, asset_id: str) -> str:
        robot = self.ledger.mission(mission_id)["robot_id"]
        entry = self.service._registry(robot).data.get("assets", {}).get(asset_id, {})
        return str(entry.get("description") or asset_id)

    def queue_node_analysis(self, run: dict, node_spec, inspection: dict) -> dict:
        """The job of a P4 analyze node; called inside the node's transaction. / P4 分析节点的作业；在节点事务内调用。"""
        acquisition = self.index.acquisition(inspection["mission_id"], inspection["evidence_id"])
        evidence = self.index.ref(acquisition)
        if evidence is None:
            raise ServiceError("analysis.not_verified", inspection["evidence_id"])
        purpose = JobPurpose(node_spec.params.purpose)
        order_id = round_number = None
        if purpose is JobPurpose.REINSPECTION:
            found = self.orders.round_of(run)
            if found is None:
                raise ServiceError("business.not_reinspection", "the run belongs to no order round")
            order_id, round_number = found
        job, _ = self.jobs.queue(key=activity_key(run["run_id"], node_spec.node_id), project_id=run["project_id"],
                                 requested_by=run_principal(run["run_id"]), purpose=purpose,
                                 analyzer_name=node_spec.params.analyzer, workflow_catalog_sha=run["catalog_sha256"],
                                 evidence=evidence,
                                 asset=self.index.asset_of(inspection["mission_id"], inspection["asset_id"]),
                                 asset_description=self.asset_description(inspection["mission_id"],
                                                                          inspection["asset_id"]),
                                 run_id=run["run_id"], order_id=order_id, round_number=round_number)
        return job

    def poll_analysis(self, run: dict, node_spec) -> tuple[NodeState, str | None, dict | None, dict | None]:
        """(state, reason, detail, output) of a P4 analyze node from its job. / 由作业得出 P4 分析节点的状态。"""
        job = self.store.job_by_key(activity_key(run["run_id"], node_spec.node_id))
        if job is None:
            return NodeState.FAILED, "analysis.job_missing", None, None
        detail = {"job_id": job["job_id"], "state": job["state"], "attempts": job["attempts"]}
        if job["state"] in (JobState.QUEUED.value, JobState.RUNNING.value):
            return NodeState.WAITING, WaitReason.ANALYSIS.value, detail, None
        result = job["result"] or {}
        if job["state"] != JobState.COMPLETED.value or job["verdict"] not in ("suspected", "normal"):
            reasons = result.get("reasons") or ["analysis.refused"]
            detail = {**detail, "reasons": reasons, "source": job["source"]}
            policy = self.catalog.recapture
            if node_spec.params.purpose == "inspection" and policy is not None:
                # P6 (D078): decided once, when the node fails, and kept with the node. / P6（D078）：只在节点失败时判定
                # 一次，并随节点保存。
                detail.update(recapture=reasons[0] in policy.reasons, recapture_rule=policy.version,
                              business_catalog=self.catalog.sha256)
            return NodeState.FAILED, reasons[0], detail, None
        finding = self.store.finding(job["finding_id"]) if job["finding_id"] else None
        output = AnalysisOutput(analysis_id=job["job_id"], verdict=job["verdict"],
                                suspected=job["verdict"] == "suspected",
                                confidence=result.get("score") if result.get("score") is not None else 1.0,
                                source=job["source"], finding_id=job["finding_id"],
                                new_finding=(finding["first_job"] == job["job_id"]) if finding else None)
        return NodeState.COMPLETED, None, detail, output.model_dump(mode="json")

    def review_subject(self, run: dict, spec, node_spec, outputs: dict) -> tuple[str, str] | None:
        """What a P4 review node reviews: the analysis's finding, or the run's round; a selection (P6) stands for the
        analysis it took. / P4 复核节点的对象：分析的发现，或运行的轮次；选择节点（P6）代表其所取的分析。"""
        analysis_node = spec.analysis(node_spec.params.analysis_from)
        if not analysis_node.params.findings:
            return None
        if analysis_node.params.purpose == "reinspection":
            found = self.orders.round_of(run)
            return (ReviewSubject.ROUND.value, round_subject(*found)) if found else None
        finding_id = (outputs.get(node_spec.params.analysis_from) or {}).get("finding_id")
        return (ReviewSubject.FINDING.value, finding_id) if finding_id else None

    def poll_review(self, subject: tuple[str, str]) -> dict | None:
        review = self.store.review(*subject)
        if review is None:
            return None
        return ReviewOutput(review_id=review["review_id"], decision=review["decision"],
                            reviewer=review["reviewer"]).model_dump(mode="json")

    def record_node_review(self, caller: Caller, project_id: str, run: dict, subject: tuple[str, str], decision: str,
                           request_id: str, note: str) -> None:
        """`workflows.review` on a P4 node records the review of its subject. / P4 节点上的 `workflows.review` 记录其对象的复核。"""
        if subject[0] == ReviewSubject.FINDING.value:
            self.findings.decide(subject[1], project_id=project_id, reviewer=caller.identity, decision=decision,
                                 request_id=request_id, note=note)
            return
        order_id, number = subject[1].rsplit(":r", 1)
        self.orders.decide(self.store.order(order_id), int(number), reviewer=caller.identity, decision=decision,
                           request_id=request_id, note=note)

    def create_node_order(self, run: dict, payload: dict, key: str, guard) -> dict | None:
        return self.orders.create(run, payload, key, guard)

    def settle_node(self, run: dict, spec, node_spec, nodes: dict):
        return self.orders.settle(run, spec, node_spec, nodes)

    def run_ended(self, run: dict) -> None:
        self.orders.run_ended(run)

    # ── references / 参考外观 ──

    def references_list(self, caller, project_id) -> list[dict]:
        self.require(caller, project_id, ANALYSIS_READ)
        return self.store.references(project_id=project_id)

    def reference_register(self, caller, project_id, mission_id, evidence_id, note) -> dict:
        """Register one verified acquisition as the asset's reference appearance (admin). / 登记参考外观（admin）。"""
        self.require(caller, project_id, ANALYSIS_REFERENCE)
        if not isinstance(mission_id, str) or self.service.project_of(mission_id) != project_id \
                or self.ledger.mission(mission_id) is None:
            raise ServiceError("service.not_found", str(mission_id)[:40])
        acquisition = self.index.acquisition(mission_id, evidence_id) if isinstance(evidence_id, str) else None
        if acquisition is None:
            raise ServiceError("service.not_found", str(evidence_id)[:80])
        if self.index.ref(acquisition) is None:
            raise ServiceError("analysis.not_verified", "only a verified acquisition becomes a reference")
        asset = self.index.asset_of(mission_id, acquisition["asset_id"])
        body = {k: acquisition[k] for k in ("mission_id", "mission_version", "asset_id", "width", "height",
                                            "captured_at", "image_source")} | {"note": str(note or "")[:300]}
        reference, _ = self.store.register_reference(project_id=project_id, asset=asset, evidence_id=evidence_id,
                                                     media_sha256=acquisition["media_sha256"], body=body,
                                                     actor=caller.identity)
        return reference

    def reference_revoke(self, caller, project_id, reference_id) -> dict:
        self.require(caller, project_id, ANALYSIS_REFERENCE)
        reference = self.store.reference(reference_id) if isinstance(reference_id, str) else None
        if reference is None or reference["project_id"] != project_id:
            raise ServiceError("service.not_found", str(reference_id)[:40])
        self.store.revoke_reference(reference_id, caller.identity)
        return self.store.reference(reference_id)

    # ── analyses / 分析 ──

    def jobs_view_summary(self, job: dict) -> dict:
        result = job["result"] or {}
        return {"job_id": job["job_id"], "purpose": job["purpose"], "analyzer": job["analyzer"],
                "state": job["state"], "verdict": job["verdict"], "source": job["source"],
                "evidence_id": job["evidence_id"], "mission_id": job["inputs"]["evidence"]["mission_id"],
                "media_sha256": job["media_sha256"], "score": result.get("score"),
                "reasons": result.get("reasons", []), "defect_type": result.get("defect_type"),
                "description": result.get("description", ""), "model_id": result.get("model_id", ""),
                "prompt_version": result.get("prompt_version", ""), "usage": result.get("usage"),
                "latency_ms": result.get("latency_ms"), "cost": result.get("cost"), "attempts": job["attempts"],
                "finding_id": job["finding_id"], "created_at": job["created_at"], "updated_at": job["updated_at"]}

    def jobs_list(self, caller, project_id) -> list[dict]:
        self.require(caller, project_id, ANALYSIS_READ)
        return [self.jobs_view_summary(job) for job in self.store.jobs(project_id=project_id, limit=100)]

    def job_get(self, caller, project_id, job_id) -> dict:
        self.require(caller, project_id, ANALYSIS_READ)
        job = self.store.job(job_id) if isinstance(job_id, str) else None
        if job is None or job["project_id"] != project_id:
            raise ServiceError("service.not_found", str(job_id)[:40])
        return {**job, "events": self.store.events(f"job:{job_id}", 40)}

    def submit(self, caller, project_id, mission_id, evidence_id, analyzer, request_id) -> dict:
        """A reuse analysis of an existing verified acquisition (`reuse-v1`, D063 §10). / 对现有已证实采集的复用分析。"""
        self.require(caller, project_id, ANALYSIS_READ)
        if not isinstance(mission_id, str) or self.ledger.mission(mission_id) is None \
                or self.service.project_of(mission_id) != project_id:
            raise ServiceError("service.not_found", str(mission_id)[:40])
        acquisition = self.index.acquisition(mission_id, evidence_id) if isinstance(evidence_id, str) else None
        if acquisition is None:
            raise ServiceError("service.not_found", str(evidence_id)[:80])
        self.require(caller, project_id, ANALYSIS_RUN)
        if not isinstance(request_id, str) or not REQUEST_ID.fullmatch(request_id):
            raise ServiceError("service.invalid_request", "request_id must be 1-120 safe characters")
        evidence = self.index.ref(acquisition)
        if evidence is None:
            raise ServiceError("analysis.not_verified", "only a verified acquisition is analysed")
        catalog = self.workflows.catalog
        allowed = self.catalog.reuse.analyzers or tuple(sorted(catalog.analyzers))
        if analyzer not in catalog.analyzers or analyzer not in allowed:
            raise ServiceError("reuse.analyzer_not_allowed", str(analyzer)[:40])
        definition = catalog.analyzers[analyzer]
        fixtures = self.workflows.workflows.fixtures
        modality, quality = "visible", self.business.default_quality
        if getattr(definition, "quality", None) is not None and not isinstance(definition, ModelAnalyzer):
            quality = self.quality_profile(definition.quality, fixtures.get(f"{analyzer}#quality", ""))
            if quality is None:
                raise ServiceError("analysis.fixture_changed", analyzer)
        if isinstance(definition, ModelAnalyzer):
            profile = self.model_profile(definition.profile, fixtures.get(f"{analyzer}#profile", ""))
            quality = self.quality_profile(definition.quality, fixtures.get(f"{analyzer}#quality", ""))
            if profile is None or quality is None:
                raise ServiceError("analysis.fixture_changed", analyzer)
            modality = profile.modality
        refused = self.index.reuse_refusal(acquisition, modality=modality, max_age_s=self.catalog.reuse.max_age_s,
                                           min_width=quality.min_width, min_height=quality.min_height,
                                           now=self.clock())
        if refused is not None:
            self.store.event(f"reuse:{project_id}", "reuse.refused", caller.identity,
                             {"evidence_id": evidence_id, "analyzer": analyzer, "reason": refused})
            raise ServiceError(refused, f"{evidence_id} cannot be reused by {analyzer}")
        key = "api:" + hashlib.sha256(f"{caller.identity}\n{request_id}".encode()).hexdigest()[:32]
        job, _ = self.jobs.queue(key=key, project_id=project_id, requested_by=caller.identity,
                                 purpose=JobPurpose.REUSE, analyzer_name=analyzer, workflow_catalog_sha=catalog.sha256,
                                 evidence=evidence, asset=self.index.asset_of(mission_id, acquisition["asset_id"]),
                                 asset_description=self.asset_description(mission_id, acquisition["asset_id"]))
        return self.jobs_view_summary(job)

    def media(self, caller, project_id, asset) -> list[dict]:
        """The acquisitions of one asset key in a project. / 项目中某资产键的采集。"""
        self.require(caller, project_id, ANALYSIS_READ)
        if not isinstance(asset, str) or not asset.startswith(project_id + "/"):
            raise ServiceError("service.not_found", str(asset)[:80])
        return [{k: v for k, v in item.items() if k != "media_path"}
                for item in self.index.captures(project_id, asset, limit=100)]

    # ── the project view / 项目视图 ──

    def summary(self, caller, project_id) -> dict:
        """Findings, orders, recent analyses, references and the business report in three columns.

        发现、工单、近期分析、参考外观与三列业务报告。
        """
        self.require(caller, project_id, ANALYSIS_READ)
        orders = self.store.orders(project_id)
        columns = {"closed": [], "open": [], "unknown": []}
        for order in orders:
            state = OrderState(order["state"])
            column = "closed" if state is OrderState.CLOSED else \
                "unknown" if state is OrderState.REINSPECTION_UNKNOWN else "open"
            columns[column].append(order["order_id"])
        return {"project_id": project_id, "catalog": {"catalog_id": self.catalog.catalog_id,
                                                      "sha256": self.catalog.sha256},
                "roles": sorted(r.value for r in self.ops.directory.roles(caller, project_id)),
                "findings": self.findings.list(caller, project_id), "orders": self.orders.list(caller, project_id),
                "jobs": self.jobs_list(caller, project_id)[:40], "references": self.store.references(project_id=project_id),
                "reuse": self.catalog.reuse.model_dump(mode="json"),
                "report": {"format": "drone.business-report/v1", "columns": columns,
                           "note": "closed = settled passed with new verified evidence and a reviewer's confirmation"}}
