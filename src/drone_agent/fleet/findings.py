"""P4 candidate findings: clustering suspected results and the human reviews that confirm or dismiss them (D063 §6–7).

A suspected result joins the one open finding of its cluster (project, asset scope, asset, defect family), or opens a
new `candidate`; `bz_open_finding` makes a second open finding of a cluster impossible. A finding changes state only
through a review by a first-party identity holding the reviewer role in the finding's project, and the first
decision wins. A model never confirms anything: its result only ever adds evidence to a candidate.

P4 候选发现：聚合疑似结果，以及确认或驳回它们的人工复核（D063 §6–7）。

疑似结果归入其聚合键（项目、资产范围、资产、缺陷族）唯一的未结发现，或新开一个 `candidate`；`bz_open_finding` 使同一聚合
键不可能出现第二个未结发现。发现只经在其项目中持有 reviewer 角色的第一方身份的复核改变状态，首个决定有效。模型从不
确认任何东西：它的结果只是给候选增加证据。
"""

from __future__ import annotations

from drone_agent.fleet.business_models import (
    DECISIONS,
    REQUEST_ID,
    FindingState,
    JobResult,
    ReviewSubject,
    cluster_key,
)
from drone_agent.fleet.service import ServiceError
from drone_agent.runtime.permission import ANALYSIS_READ, WORKFLOW_REVIEW

EVIDENCE_KEPT = 20


class Findings:
    """Clustering, reviews and views of findings. / 发现的聚合、复核与视图。"""

    def __init__(self, engine):
        self.engine = engine

    @property
    def store(self):
        return self.engine.store

    def attach(self, job: dict, result: JobResult) -> str:
        """Join the cluster's open finding or open a candidate; called inside the job's result transaction.

        归入聚合键的未结发现或新开候选；在作业结果事务内调用。
        """
        cluster = cluster_key(job["asset_key"], result.family)
        evidence = {"job_id": job["job_id"], "evidence_id": job["evidence_id"], "media_sha256": job["media_sha256"],
                    "source": result.source, "score": result.score, "defect_type": result.defect_type,
                    "description": result.description}
        found = self.store.open_finding(cluster)
        if found is not None:
            body = dict(found["body"])
            body["evidence"] = ([*body.get("evidence", []), evidence])[-EVIDENCE_KEPT:]
            body["defect_types"] = sorted({*body.get("defect_types", []), result.defect_type})
            body["sources"] = sorted({*body.get("sources", []), result.source})
            body["max_score"] = max(body.get("max_score") or 0.0, result.score or 0.0)
            self.store.attach(found["finding_id"], job["job_id"], body)
            self.store.event(f"finding:{found['finding_id']}", "finding.attached", self.engine.worker,
                             {"job_id": job["job_id"], "evidence_id": job["evidence_id"]})
            return found["finding_id"]
        asset_id = job["inputs"]["evidence"]["asset_id"]
        body = {"asset_id": asset_id, "evidence": [evidence], "defect_types": [result.defect_type],
                "sources": [result.source], "max_score": result.score or 0.0}
        created = self.store.create_finding(project_id=job["project_id"], asset=job["asset_key"], family=result.family,
                                            cluster=cluster, job_id=job["job_id"], body=body)
        self.store.event(f"finding:{created['finding_id']}", "finding.opened", self.engine.worker,
                         {"job_id": job["job_id"], "cluster": cluster})
        return created["finding_id"]

    def _finding(self, caller, project_id, finding_id, scope: str) -> dict:
        self.engine.require(caller, project_id, ANALYSIS_READ)
        finding = self.store.finding(finding_id) if isinstance(finding_id, str) else None
        if finding is None or finding["project_id"] != project_id:
            raise ServiceError("service.not_found", str(finding_id)[:40])
        self.engine.require(caller, project_id, scope)
        return finding

    def review(self, caller, project_id, finding_id, decision, request_id, note) -> dict:
        """A reviewer confirms or dismisses a candidate; the first decision wins (D063 §7).

        reviewer 确认或驳回候选；首个决定有效（D063 §7）。
        """
        finding = self._finding(caller, project_id, finding_id, WORKFLOW_REVIEW)
        if decision not in DECISIONS:
            raise ServiceError("service.invalid_request", "decision must be confirmed or dismissed")
        if not isinstance(request_id, str) or not REQUEST_ID.fullmatch(request_id):
            raise ServiceError("service.invalid_request", "request_id must be 1-120 safe characters")
        self.decide(finding["finding_id"], project_id=project_id, reviewer=caller.identity, decision=decision,
                    request_id=request_id, note=str(note or ""))
        return self.view(self.store.finding(finding["finding_id"]))

    def decide(self, finding_id: str, *, project_id: str, reviewer: str, decision: str, request_id: str,
               note: str) -> dict:
        """Record the one decision on a candidate and move it; a repeat of the same request returns the record.

        记录候选的唯一决定并改变其状态；同一请求的重复返回原记录。
        """
        with self.store.transaction():
            known = self.store.review(ReviewSubject.FINDING.value, finding_id)
            if known is not None:
                if known["request_id"] == request_id and known["reviewer"] == reviewer:
                    return known
                raise ServiceError("workflow.already_decided", f"the finding was already {known['decision']}")
            finding = self.store.finding(finding_id)
            if finding["state"] != FindingState.CANDIDATE.value:
                raise ServiceError("workflow.not_waiting", f"the finding is {finding['state']}")
            basis = {"jobs": [e["job_id"] for e in finding["body"].get("evidence", [])],
                     "evidence": [e["evidence_id"] for e in finding["body"].get("evidence", [])],
                     "sources": finding["body"].get("sources", []), "finding_version": finding["state_version"]}
            review, _ = self.store.record_review(subject_kind=ReviewSubject.FINDING.value, subject_id=finding_id,
                                                 project_id=project_id, reviewer=reviewer, request_id=request_id,
                                                 decision=decision, note=note[:300], basis=basis)
            state = FindingState.CONFIRMED if decision == "confirmed" else FindingState.DISMISSED
            self.store.set_finding(finding_id, version=finding["state_version"], state=state,
                                   review_id=review["review_id"])
            self.store.event(f"finding:{finding_id}", "finding.reviewed", reviewer, {"decision": decision})
        return review

    # ── views / 视图 ──

    def view(self, finding: dict) -> dict:
        review = self.store.review(ReviewSubject.FINDING.value, finding["finding_id"])
        order = self.store.order_by_finding(finding["finding_id"])
        jobs = self.store.jobs(finding_id=finding["finding_id"], limit=50)
        return {"finding": finding, "review": review,
                "order": {k: order[k] for k in ("order_id", "state", "round", "closed_at")} if order else None,
                "jobs": [self.engine.jobs_view_summary(job) for job in jobs],
                "events": self.store.events(f"finding:{finding['finding_id']}", 60)}

    def get(self, caller, project_id, finding_id) -> dict:
        return self.view(self._finding(caller, project_id, finding_id, ANALYSIS_READ))

    def list(self, caller, project_id) -> list[dict]:
        self.engine.require(caller, project_id, ANALYSIS_READ)
        return [{k: f[k] for k in ("finding_id", "asset_key", "family", "state", "jobs", "review_id", "order_id",
                                   "created_at", "updated_at")} | {"body": f["body"]}
                for f in self.store.findings(project_id=project_id, limit=100)]
