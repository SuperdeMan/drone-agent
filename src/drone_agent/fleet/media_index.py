"""P4 image index: read-only views of acquisitions over the existing evidence, verification, binding and source records.

The index adds no table (D063 §10). An acquisition is one evidence row of a bound mission: its identity (evidence,
mission, version), the asset it shows, its media digest, its capture time, the image source recorded by the trusted
deployment (D054) and the service's own verification verdict. Analyses read only acquisitions the service verified;
reuse eligibility and replay checks are computed here from the same records, never from what a client says.

P4 影像索引：基于既有证据、复核、绑定与来源记录的只读采集视图。

索引不新增表（D063 §10）。一次采集是一个已绑定任务的证据行：其身份（证据、任务、版本）、所示资产、媒体摘要、采集时刻、
受信部署记录的影像来源（D054）以及服务自己的复核结论。分析只读取服务复核为已证实的采集；复用资格与重放检查都在这里
根据同样的记录计算，从不采信客户端的说法。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from drone_agent.fleet.business_models import EvidenceRef, asset_key
from drone_agent.fleet.provenance import NAMESPACE, EvidenceOrigin, unseal


class MediaIndex:
    """Acquisitions of one mission service; every lookup is scoped to a project by the caller.

    一个任务服务的采集视图；每次查询都由调用方限定项目。
    """

    def __init__(self, service):
        self.service = service

    @property
    def ledger(self):
        return self.service.ledger

    def scope(self, project_id: str, site_id: str) -> str:
        """`*` when the scheduling catalog places every site of the project in one shared frame (P3), else the site.

        调度目录把项目的每个站点放在同一共享坐标中（P3）时为 `*`，否则为站点。
        """
        scheduler = self.service.scheduler
        catalog = self.service.ops.catalog if self.service.ops is not None else None
        if scheduler is not None and catalog is not None and project_id in catalog.projects:
            placed = set(scheduler.catalog.airspace.sites)
            if set(catalog.projects[project_id].sites) <= placed:
                return "*"
        return site_id

    def asset_of(self, mission_id: str, asset_id: str) -> str | None:
        binding = self.service._binding(mission_id)
        if binding is None:
            return None
        return asset_key(binding["project_id"], self.scope(binding["project_id"], binding["site_id"]), asset_id)

    def image_source(self, mission_id: str, version: int, evidence_id: str) -> str:
        """The trusted per-evidence source, `unknown` when none was sealed. / 受信的逐证据来源；未封存时为 `unknown`。"""
        record = self.ledger.version(mission_id, version)
        saved = ((record or {}).get("decision") or {}).get(NAMESPACE, {})
        sealed = saved.get(f"evidence:{evidence_id}")
        if sealed is None:
            return "unknown"
        try:
            return unseal(sealed, EvidenceOrigin).source
        except ValueError:
            return "unknown"

    def acquisition(self, mission_id: str, evidence_id: str) -> dict | None:
        """One acquisition with its verdict; None when the evidence does not exist. / 一次采集及其复核结论；不存在时为 None。"""
        row = next((r for r in self.ledger.evidence(mission_id) if r["evidence_id"] == evidence_id), None)
        if row is None:
            return None
        verdicts = {v["evidence_id"]: v["body"]["final_verdict"] for v in self.ledger.verifications(mission_id)}
        body = row["body"] or {}
        captured = (body.get("time_window") or {}).get("timestamp")
        subjects = body.get("subject_ids") or []
        return {"evidence_id": evidence_id, "mission_id": mission_id, "mission_version": row["mission_version"],
                "robot_id": row["robot_id"], "asset_id": subjects[0] if len(subjects) == 1 else None,
                "media_sha256": row["sha256"], "media_path": row["media_path"], "width": row["width"],
                "height": row["height"], "captured_at": captured, "verdict": verdicts.get(evidence_id, "unknown"),
                "image_source": self.image_source(mission_id, row["mission_version"], evidence_id)}

    def ref(self, acquisition: dict) -> EvidenceRef | None:
        """The pinned input of a job, only for a verified acquisition with its media. / 只对带媒体的已证实采集给出作业输入。"""
        if acquisition is None or acquisition["verdict"] != "verified" or not acquisition["media_path"] \
                or not acquisition["asset_id"] or not acquisition["width"] or not acquisition["height"]:
            return None
        captured = datetime.fromisoformat(acquisition["captured_at"]) if acquisition["captured_at"] else None
        return EvidenceRef(evidence_id=acquisition["evidence_id"], mission_id=acquisition["mission_id"],
                           mission_version=acquisition["mission_version"], asset_id=acquisition["asset_id"],
                           media_sha256=acquisition["media_sha256"], captured_at=captured,
                           image_source=acquisition["image_source"], width=acquisition["width"],
                           height=acquisition["height"])

    def media(self, acquisition: dict) -> bytes | None:
        return self.service.hub.media(acquisition["media_path"]) if acquisition and acquisition["media_path"] else None

    def missions_of(self, project_id: str) -> list[str]:
        return [m["mission_id"] for m in self.ledger.missions(-1) if self.service.project_of(m["mission_id"]) == project_id]

    def captures(self, project_id: str, asset: str, limit: int = 200) -> list[dict]:
        """Every acquisition of one asset key in a project, newest first. / 项目中某资产键的全部采集，按时间倒序。"""
        found = []
        for mission_id in self.missions_of(project_id):
            for row in self.ledger.evidence(mission_id):
                subjects = (row["body"] or {}).get("subject_ids") or []
                if len(subjects) != 1 or self.asset_of(mission_id, subjects[0]) != asset:
                    continue
                item = self.acquisition(mission_id, row["evidence_id"])
                if item is not None:
                    found.append(item)
        found.sort(key=lambda a: (a["captured_at"] or "", a["evidence_id"]), reverse=True)
        return found[:limit]

    def seen(self, project_id: str, asset: str, *, exclude_missions: set[str]) -> tuple[set[str], set[str]]:
        """Evidence IDs and media digests of the asset outside the given missions (replay and novelty checks).

        给定任务之外该资产的证据 ID 与媒体摘要（用于重放与新采集检查）。
        """
        evidence, media = set(), set()
        for item in self.captures(project_id, asset, limit=10_000):
            if item["mission_id"] in exclude_missions:
                continue
            evidence.add(item["evidence_id"])
            media.add(item["media_sha256"])
        return evidence, media

    def reuse_refusal(self, acquisition: dict, *, modality: str, max_age_s: int, min_width: int, min_height: int,
                      now: datetime) -> str | None:
        """`reuse-v1`: why an existing acquisition may not be analysed again, or None. Every camera of the current
        platforms is a visible-light camera, so a profile for another modality never reuses them.

        `reuse-v1`：现有采集为何不能再分析，或 None。现有平台的相机都是可见光相机，因此其他模态的画像从不复用它们。
        """
        if acquisition["image_source"] not in ("sim_render", "recorded_real", "live_sensor", "test_fixture"):
            return "reuse.source_unknown"
        if modality != "visible":
            return "reuse.modality_mismatch"
        if (acquisition["width"] or 0) < min_width or (acquisition["height"] or 0) < min_height:
            return "reuse.resolution"
        captured = datetime.fromisoformat(acquisition["captured_at"]) if acquisition["captured_at"] else None
        if captured is None or now - captured > timedelta(seconds=max_age_s):
            return "reuse.stale"
        return None
