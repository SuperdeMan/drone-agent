"""P4 analysis jobs: queueing, a leased asynchronous runner and the one transaction that records a result (D063 §3).

A job is created with every input pinned (acquisition, references, analyzer and profile digests, catalog digests).
The runner lives in the mission-service process: it takes queued jobs, and running ones whose lease expired, under a
new fencing epoch, runs deterministic and scripted analyzers inline and model analyzers as bounded concurrent tasks,
and records each result with a compare-and-set on its epoch, so a stalled or restarted runner can never write a
second result. In the same transaction a suspected result joins its finding and the analysis joins the mission
version's sealed sources (D054). Nothing here touches a flight verdict.

P4 分析作业：排队、带租约的异步执行器，以及记录结果的单一事务（D063 §3）。

作业创建时固定全部输入（采集、参考图、分析器与画像摘要、目录摘要）。执行器位于任务服务进程内：以新的 fencing 代次领取
排队中与租约过期的作业，确定性与脚本分析器内联运行，模型分析器作为有并发上限的任务运行，并按代次比较并交换写入结果，
因此停顿或重启的执行器永远写不进第二个结果。同一事务中，疑似结果归入发现，分析记录进入任务版本的封存来源（D054）。
这里不触碰任何飞行判定。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, time, timezone
from pathlib import Path

from drone_agent.fleet.analysis import (
    analyze,
    check_quality,
    check_target_quality,
    job_result,
    pixels,
    route_heading,
)
from drone_agent.fleet.business_models import (
    EvidenceRef,
    JobInputs,
    JobPurpose,
    JobResult,
    JobState,
    ReferenceRef,
    TargetQualityProfile,
    refusal,
)
from drone_agent.fleet.business_store import StaleRecord
from drone_agent.fleet.provenance import AnalysisOrigin, ModelUse, digest
from drone_agent.fleet.workflow_models import CANCELLING_RUN, ModelAnalyzer, RunState, ScriptedAnalyzer
from drone_agent.runtime.issues import issue


def redact_images(body: dict) -> dict:
    """A stored copy of a request with every image replaced by the digest of its data URI.

    请求的存储副本：每张图像替换为其 data URI 的摘要。
    """
    stored = json.loads(json.dumps(body))
    for message in stored.get("messages", []):
        for part in message.get("content") if isinstance(message.get("content"), list) else []:
            image = part.get("image_url") if isinstance(part, dict) else None
            if isinstance(image, dict) and isinstance(image.get("url"), str):
                image["url"] = "sha256:" + hashlib.sha256(image["url"].encode()).hexdigest()
    return stored


class AnalysisJobs:
    """Job creation, the runner and result recording of one business engine. / 一个业务引擎的作业创建、执行器与结果记录。"""

    def __init__(self, engine):
        self.engine = engine
        self.tasks: dict[str, tuple[asyncio.Task, int]] = {}

    def stop(self) -> None:
        """Cancel every running task, as a process exit would; their jobs keep their leases until they expire.

        像进程退出一样取消全部运行中的任务；其作业保留租约直到过期。
        """
        for task, _ in self.tasks.values():
            task.cancel()
        self.tasks.clear()

    @property
    def store(self):
        return self.engine.store

    # ── creation / 创建 ──

    def queue(self, *, key: str, project_id: str, requested_by: str, purpose: JobPurpose, analyzer_name: str,
              workflow_catalog_sha: str, evidence: EvidenceRef, asset: str, asset_description: str,
              run_id: str | None = None, order_id: str | None = None, round_number: int | None = None) \
            -> tuple[dict, bool]:
        """Create (or return) the job of one key with every input pinned. / 创建（或返回）某键的作业并固定全部输入。"""
        known = self.store.job_by_key(key)
        if known is not None:
            return known, False
        workflows = self.engine.workflow_store
        catalog = workflows.pinned(workflow_catalog_sha)
        analyzer = catalog.analyzers[analyzer_name]
        fixtures = workflows.fixtures_of(workflow_catalog_sha)
        files = {name: sha for name, sha in fixtures.items() if name == analyzer_name
                 or name.startswith(analyzer_name + "#")}
        references: tuple[ReferenceRef, ...] = ()
        # A model analyzer always pins its quality profile; a deterministic one may (P6). / 模型分析器总是固定其质量画像；
        # 确定性分析器可以固定（P6）。
        quality_sha = fixtures.get(f"{analyzer_name}#quality") or self.engine.business.default_quality_sha
        if isinstance(analyzer, ModelAnalyzer):
            profile = self.engine.model_profile(analyzer.profile, fixtures[f"{analyzer_name}#profile"])
            wanted = min(profile.references, self.engine.catalog.max_references) if profile else 0
            references = tuple(ReferenceRef(reference_id=r["reference_id"], evidence_id=r["evidence_id"],
                                            media_sha256=r["media_sha256"])
                               for r in self.store.references(asset=asset, active=True)[:wanted])
        inputs = JobInputs(purpose=purpose, analyzer=analyzer_name, analyzer_kind=analyzer.kind,
                           analyzer_sha256=digest({"definition": analyzer.model_dump(mode="json"), "files": files}),
                           workflow_catalog_sha256=workflow_catalog_sha, quality_sha256=quality_sha, evidence=evidence,
                           references=references, asset_description=asset_description[:300], run_id=run_id,
                           order_id=order_id, round=round_number)
        return self.store.create_job(key=key, project_id=project_id, requested_by=requested_by, inputs=inputs,
                                     asset=asset)

    # ── the runner / 执行器 ──

    async def tick(self) -> set[str]:
        """Reap finished tasks, refuse exhausted jobs, start runnable ones; returns missions whose sources changed.

        回收已结束的任务、拒判耗尽尝试的作业、启动可运行的作业；返回来源发生变化的任务。
        """
        policy = self.engine.catalog.runner
        touched: set[str] = set()
        for job_id, (task, epoch) in list(self.tasks.items()):
            if task.done():
                self.tasks.pop(job_id)
                error = None if task.cancelled() else task.exception()
                if error is not None:
                    self.engine.ledger.record_issue(issue("service.degraded", f"analysis job {job_id}: "
                                                          f"{type(error).__name__}: {error}"[:300]))
                elif task.result():
                    touched.add(task.result())
            else:
                # A live job keeps its lease, so no other runner takes it while it still works.
                # 进行中的作业保持租约，因此它仍在工作时不会被其他执行器领取。
                self.store.renew(job_id, owner=self.engine.worker, epoch=epoch, lease_s=policy.lease_s)
        for job in self.store.runnable(limit=64):
            if job["job_id"] in self.tasks:
                continue
            if job["state"] == JobState.RUNNING.value and job["attempts"] >= policy.max_attempts:
                with self.store.transaction():
                    if self.store.abandon(job["job_id"], max_attempts=policy.max_attempts,
                                          result=refusal("analysis.attempts_exhausted")):
                        self.store.event(f"job:{job['job_id']}", "job.refused", self.engine.worker,
                                         {"reason": "analysis.attempts_exhausted"})
                continue
            model = job["inputs"]["analyzer_kind"] == "model"
            if model and len(self.tasks) >= policy.concurrency:
                continue
            epoch = self.store.claim(job["job_id"], owner=self.engine.worker, lease_s=policy.lease_s,
                                     max_attempts=policy.max_attempts)
            if epoch is None:
                continue
            if model:
                self.tasks[job["job_id"]] = (asyncio.create_task(self._run(job["job_id"], epoch)), epoch)
            else:
                mission = await self._run(job["job_id"], epoch)
                if mission:
                    touched.add(mission)
        return touched

    async def _run(self, job_id: str, epoch: int) -> str | None:
        job = self.store.job(job_id)
        try:
            result = await self._analyze(job)
        except Exception as error:  # a job always ends with a recorded outcome / 作业总以记录的结果结束
            result = refusal("analysis.error", description=f"{type(error).__name__}: {error}"[:400])
        return self.complete(job_id, epoch, result)

    def _captured_pose(self, evidence: EvidenceRef) -> dict | None:
        """The capture pose of the stored evidence contract; None when it has none. / 已存证据契约中的拍摄位姿；没有时为 None。"""
        row = next((r for r in self.engine.ledger.evidence(evidence.mission_id)
                    if r["evidence_id"] == evidence.evidence_id), None)
        return (row["body"] or {}).get("captured_pose") if row is not None else None

    def _run_cancelled(self, inputs: JobInputs) -> bool:
        if inputs.run_id is None:
            return False
        run = self.engine.workflow_store.run(inputs.run_id)
        return run is None or RunState(run["state"]) in CANCELLING_RUN | {RunState.CANCELLED}

    async def _analyze(self, job: dict) -> JobResult:
        """Compute one job's result; every missing or mismatching input is a refusal. / 计算作业结果；输入缺失或不符即拒判。"""
        inputs = JobInputs.model_validate(job["inputs"])
        if self._run_cancelled(inputs):
            return refusal("analysis.run_cancelled")
        index = self.engine.index
        acquisition = index.acquisition(inputs.evidence.mission_id, inputs.evidence.evidence_id)
        if acquisition is None or acquisition["verdict"] != "verified":
            return refusal("analysis.not_verified")
        media = index.media(acquisition)
        image = pixels(media, acquisition["width"], acquisition["height"])
        if image is None:
            return refusal("quality.media_missing")
        if hashlib.sha256(media).hexdigest() != inputs.evidence.media_sha256:
            return refusal("quality.media_mismatch")
        workflows = self.engine.workflow_store
        catalog = workflows.pinned(inputs.workflow_catalog_sha256)
        analyzer = catalog.analyzers[inputs.analyzer]
        if getattr(analyzer, "quality", None) is not None:
            quality = self.engine.quality_profile(analyzer.quality, inputs.quality_sha256)
        else:
            quality = self.engine.business.default_quality \
                if inputs.quality_sha256 == self.engine.business.default_quality_sha else None
        if quality is None:
            return refusal("analysis.fixture_changed")
        robot = self.engine.ledger.mission(inputs.evidence.mission_id)["robot_id"]
        registry = self.engine.service._registry(robot).data
        entry = registry["assets"].get(inputs.evidence.asset_id, {})
        if isinstance(quality, TargetQualityProfile):
            reason, measures = check_target_quality(image, quality, pose=self._captured_pose(inputs.evidence),
                                                    asset=entry, heading=route_heading(registry, entry))
        else:
            reason, measures = check_quality(image, quality)
        if reason is not None:
            return refusal(reason, quality=measures)
        if inputs.evidence.captured_at is None:
            return refusal("quality.capture_time_unknown", quality=measures)
        if not isinstance(analyzer, ModelAnalyzer):
            fixtures = workflows.fixtures_of(inputs.workflow_catalog_sha256)
            fixture_sha = fixtures.get(inputs.analyzer, "")
            fixture = self.engine.workflows._fixture(analyzer.fixture, fixture_sha) \
                if isinstance(analyzer, ScriptedAnalyzer) else None
            if isinstance(analyzer, ScriptedAnalyzer) and fixture is None:
                return refusal("analysis.fixture_changed", source="scripted", quality=measures)
            result = analyze(inputs.analyzer, analyzer, asset_id=inputs.evidence.asset_id,
                             evidence_id=inputs.evidence.evidence_id, evidence_sha256=inputs.evidence.media_sha256,
                             media=media, width=acquisition["width"], height=acquisition["height"], asset=entry,
                             fixture=fixture, fixture_sha256=fixture_sha)
            generic = self.engine.catalog.generic_defect
            converted = job_result(result, generic_defect=generic, family=self.engine.catalog.family_of(generic))
            return converted.model_copy(update={"quality": measures})
        fixtures = workflows.fixtures_of(inputs.workflow_catalog_sha256)
        profile = self.engine.model_profile(analyzer.profile, fixtures.get(f"{inputs.analyzer}#profile", ""))
        if profile is None:
            return refusal("analysis.fixture_changed", quality=measures)
        if profile.threshold is None:
            return refusal("model.uncalibrated", quality=measures)
        references = []
        for ref in inputs.references:
            row = self.store.reference(ref.reference_id)
            found = index.acquisition(row["body"]["mission_id"], ref.evidence_id) if row else None
            data = index.media(found) if found else None
            if data is None or hashlib.sha256(data).hexdigest() != ref.media_sha256:
                continue
            references.append(pixels(data, found["width"], found["height"]))
        if len(references) < profile.references:
            return refusal("analysis.no_reference", quality=measures)
        policy = self.engine.catalog.runner
        day = datetime.combine(self.engine.clock().astimezone(timezone.utc).date(), time(0), tzinfo=timezone.utc)
        if policy.daily_tokens == 0 or self.store.tokens_since(job["project_id"], day) >= policy.daily_tokens:
            return refusal("model.budget_exhausted", quality=measures)
        return await self.engine.ask_model(job, profile, references, image, inputs.asset_description, measures)

    def complete(self, job_id: str, epoch: int, result: JobResult) -> str | None:
        """Record a result once, under the runner's epoch, with its finding and sealed source; returns the mission.

        按执行器代次只记录一次结果，连同其发现与封存来源；返回所属任务。
        """
        engine = self.engine
        with self.store.transaction():
            job = self.store.job(job_id)
            if job is None or job["state"] != JobState.RUNNING.value or job["owner"] != engine.worker \
                    or job["owner_epoch"] != epoch:
                return None
            inputs = JobInputs.model_validate(job["inputs"])
            finding_id = None
            if result.verdict == "suspected" and not self._run_cancelled(inputs):
                if inputs.purpose is JobPurpose.REINSPECTION:
                    finding_id = engine.orders.persisting(inputs, job_id, result)
                else:
                    finding_id = engine.findings.attach(job, result)
            if not self.store.finish(job_id, owner=engine.worker, epoch=epoch, result=result, finding_id=finding_id):
                raise StaleRecord(job_id)
            engine.ledger.record_source(inputs.evidence.mission_id, inputs.evidence.mission_version,
                                        f"analysis:{job_id}", AnalysisOrigin(
                                            attempt_id=job_id, evidence_id=inputs.evidence.evidence_id,
                                            timestamp=engine.clock(),
                                            use=ModelUse(source=result.source, provider_id=result.provider_id,
                                                         model_id=result.model_id,
                                                         reported_model_id=result.reported_model_id,
                                                         prompt_version=result.prompt_version or inputs.analyzer_kind,
                                                         prompt_sha256=result.prompt_sha256,
                                                         input_sha256=inputs.evidence.media_sha256,
                                                         recording_sha256=result.recording_sha256,
                                                         outcome=result.verdict)))
            self.store.event(f"job:{job_id}", f"job.{'refused' if result.verdict == 'refused' else 'completed'}",
                             engine.worker, {"verdict": result.verdict, "reasons": list(result.reasons),
                                             "source": result.source, "finding_id": finding_id})
        mission_id = inputs.evidence.mission_id
        report = engine.ledger.report(mission_id)
        if report is not None:
            engine.ledger.record_report(mission_id, {**report, "provenance": engine.service.provenance(mission_id)})
        return mission_id

    def save_recording(self, recorder, *, provider_id: str, model: str, endpoint_host: str, profile, label: str,
                       directory: Path | None) -> str:
        """Save the exchanges of one analysis; returns the file's SHA-256, or '' when nothing was sent. Only a live
        model's answers are a `recorded` fixture; a scripted double's stay `scripted` when replayed.

        保存一次分析的交互；返回文件的 SHA-256，未发送时为 ''。只有实调模型的回答是 `recorded` 夹具；脚本替身的回答在
        回放时仍为 `scripted`。
        """
        import os

        from drone_agent.fleet.provenance import provider_source

        if recorder is None or not recorder.exchanges or directory is None:
            return ""
        source = "recorded" if provider_source(recorder)[0] == "live_model" else "scripted"
        recording = recorder.recording(source=source, provider_id=provider_id, model=model,
                                       endpoint_host=endpoint_host, prompt_version=profile.prompt_version,
                                       prompt_sha256=profile.prompt_sha256,
                                       software_revision=os.environ.get("DRONE_SOURCE_SHA", "uncommitted"),
                                       label=label)
        path = directory / f"{label}.json"
        recording.save(path)
        return hashlib.sha256(path.read_bytes()).hexdigest()
