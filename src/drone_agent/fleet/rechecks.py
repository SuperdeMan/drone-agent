"""X1 ground recheck (D088): from an aerial finding to the ground robot's own relocation, by deterministic code.

`start` is the effect of a workflow's `ground_recheck` activity, exactly once by its key. It reads the analysed aerial
capture, locates the declared anomaly colour (`fleet/locator`), projects the centroid to the ground with the capture
position, the declared heading of the inspection route and the node's camera (`fleet/spatial`), moves the estimate
into the ground robot's map and decides a point or a search region; then, in one transaction with the activity's
guard, it records the recheck, the handoff task and its offer. A capture without the anomaly, or an alignment too
poor for any region, records the recheck as not attempted, with the reason, and offers nothing.

`poll` is the `await_recheck` wait: the offer, the task and the ground mission explain it. Once the task has ended the
conclusion is computed once and kept: the verified ground view is read by the same locator and projected forward from
the face point along the face leg (its declared heading) with the ground robot's camera; inside the offered point's
95 % ellipse or the search region, plus the frozen relocation tolerance, is `relocated`; a verified view without it is
`not_found`, which is uncertain, never normal; a refused, expired or withdrawn offer is `not_attempted`; a ground
mission that did not complete is `unknown`. Nothing here signs, approves, flies or drives, and no model takes part: a
relocation is an observation, and confirming a defect stays with a person.

X1 地面复核（D088）：用确定性代码从空中发现走到地面车自己的重新定位。

`start` 是工作流 `ground_recheck` 活动的效果，按其键只执行一次。它读取被分析的空中采集，用确定性定位器找声明颜色的异常物
（`fleet/locator`），用采集位置、巡检航线的声明航向与节点给出的相机把质心投影到地面（`fleet/spatial`），转换到地面车的地图并
判定交付点或搜索区域；随后在带活动守卫的一个事务内记录复核、交接任务及其提议。采集中没有异常物，或对齐差到任何区域都不够
时，复核记为未尝试并写明原因，不发出任何提议。

`poll` 是 `await_recheck` 的等待：由提议、任务与地面任务说明等待原因。任务结束后只计算一次结论并保存：用同一个定位器读取已
核验的地面画面，以地面车的相机从正对点沿正对段（其声明航向）向前投影；落在所提供点的 95% 椭圆或搜索区域内（各加冻结的
重新定位容差）为 `relocated`；画面核验通过却没有异常物为 `not_found`，表示不确定，从不表示正常；提议被拒、过期或撤回为
`not_attempted`；地面任务未完成为 `unknown`。这里不签名、不审批、不飞行也不驾驶，模型不参与：重新定位只是一次观测，确认
缺陷仍由人决定。
"""

from __future__ import annotations

import hashlib
import math
from pathlib import Path

import numpy as np

from drone_agent.contracts import EffectVerdict, Evidence, Frame, WorldFact
from drone_agent.fleet.locator import locate
from drone_agent.fleet.spatial import (
    AlignmentError,
    Camera,
    MapPlacement,
    decide,
    load_alignment,
    project_nadir,
    to_fact,
    to_frame,
)
from drone_agent.fleet.workflow_models import NodeState, WaitReason
from drone_agent.mission.verify import GROUND_RECHECK, INSPECT, INSPECT_LOCAL

ACTOR = "coordinator:recheck"
POSITION_SIGMA_M = 1.5  # the platform's nominal GNSS accuracy when a pose has no covariance / 位姿缺协方差时的标称精度


def recheck_id(key: str) -> str:
    """One recheck per activity key. / 每个活动键一个复核。"""
    return "rc-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def route_heading(data: dict, params: dict) -> float:
    """The declared heading of an inspection: the bearing of the last leg of its approach route, from home when the
    route is one point. / 巡检的声明航向：接近航线最后一段的方位；航线只有一个点时从 home 起算。"""
    route = [tuple(p[:2]) for p in data["routes"][params["approach_route_id"]]]
    before = route[-2] if len(route) > 1 else tuple(data["home"]["position"][:2])
    dx, dy = route[-1][0] - before[0], route[-1][1] - before[1]
    if math.hypot(dx, dy) < 1e-6:
        raise AlignmentError("heading_undeclared", "the approach route's last leg has no bearing")
    return math.atan2(dy, dx)


def forward_ground(camera: dict, capture, heading: float, u: float, bottom: float, *,
                   half_depth_m: float = 0.0) -> tuple[float, float] | None:
    """Where a block standing on flat ground lies, from a level forward camera: its column gives the bearing and the
    row where it meets the ground (`bottom`) the range of its near face, which `half_depth_m` (half the declared size)
    moves back to the centre. None when that row is not below the horizon.

    由水平前视相机求立在平地上的方块的位置：列号给出方位，与地面相接的行号（`bottom`）给出其近侧面的距离，再加
    `half_depth_m`（声明尺寸的一半）回到中心；该行不在地平线以下时为 None。
    """
    width, height, h = camera["width_px"], camera["height_px"], camera["height_m"]
    focal = (width / 2) / math.tan(math.radians(camera["hfov_deg"]) / 2)
    below = bottom - height / 2
    if below <= 0.5:
        return None
    near = focal * h / below
    left = near * (width / 2 - u) / focal
    forward = near + half_depth_m
    c, s = math.cos(heading), math.sin(heading)
    return capture[0] + forward * c - left * s, capture[1] + forward * s + left * c


def within(target: WorldFact, point: tuple[float, float], tolerance_m: float) -> bool:
    """Inside the offered point's 95 % ellipse or the search region, plus the tolerance. / 在所提供点的 95% 椭圆或搜索
    区域之内（加容差）。"""
    value = target.value or {}
    centre = (target.position.x, target.position.y)
    if value.get("kind") == "search_region":
        return math.dist(point, centre) <= float(value["radius_m"]) + tolerance_m
    return math.dist(point, centre) <= float(value.get("semi_major_95_m", 0.0)) + tolerance_m


class Rechecks:
    """The X1 recheck activity's service side over the handoff store, the scheduler and the business tables.

    X1 复核活动的服务一侧，基于交接存储、调度器与业务表。
    """

    def __init__(self, service, engine, *, root: Path):
        self.service, self.engine, self.root = service, engine, root
        self.store = engine.store
        self.config, self.config_digest = load_alignment(root)

    @property
    def ledger(self):
        return self.service.ledger

    def clock(self):
        return self.engine.clock()

    # ── the effect / 效果 ──

    @staticmethod
    def output(recheck: dict) -> dict:
        return {"recheck_id": recheck["recheck_id"], "task_id": recheck["ground_task_id"],
                "handoff_id": recheck["handoff_id"]}

    def by_key(self, key: str) -> dict | None:
        found = self.store.recheck(recheck_id(key))
        return self.output(found) if found is not None else None

    def _maps(self) -> dict[str, MapPlacement]:
        """Every map in force by version, placed where the scheduling catalog puts its site. / 按版本列出在用的地图。"""
        ops, scheduling = self.service.ops, self.service.scheduler.catalog
        maps = {}
        for site_id in ops.catalog.sites:
            data = ops.registries[site_id].data
            maps[data["registry_id"]] = MapPlacement(frame_id=data["frame"]["frame_id"],
                                                     origin=scheduling.origin(site_id))
        return maps

    def _capture(self, job: dict) -> tuple[Evidence, bytes | None, dict]:
        evidence_ref = job["inputs"]["evidence"]
        row = next((r for r in self.ledger.evidence(evidence_ref["mission_id"], evidence_ref["mission_version"])
                    if r["evidence_id"] == evidence_ref["evidence_id"]), None)
        if row is None:
            raise AlignmentError("evidence_missing", evidence_ref["evidence_id"])
        evidence = Evidence.model_validate(row["body"])
        media = self.service.hub.media(row["media_path"]) if row["media_path"] else None
        return evidence, media, row

    def _target(self, payload: dict, job: dict) -> tuple[WorldFact, dict]:
        """The offered target and what it was derived from; raises AlignmentError with the reason when there is none.

        所提供的目标及其来源；无法给出时以原因抛出 AlignmentError。
        """
        evidence, media, row = self._capture(job)
        if media is None or hashlib.sha256(media).hexdigest() != evidence.sha256:
            raise AlignmentError("media_missing", evidence.evidence_id)
        image = np.frombuffer(media, dtype=np.uint8).reshape(row["height"], row["width"], 3)
        found = locate(image, payload["signature"])
        if found is None:
            raise AlignmentError("not_located", f"no {payload['signature']} anomaly in {evidence.evidence_id}")
        record = self.ledger.version(evidence_ref_mission(job), evidence_ref_version(job))
        node = next(n for n in record["package"]["nodes"] if n["task_id"] == evidence.produced_by_skill_instance)
        if node["skill_id"] not in (INSPECT, INSPECT_LOCAL):
            raise AlignmentError("not_an_inspection", node["skill_id"])
        binding = self.service.ops.store.binding(evidence_ref_mission(job))
        aerial = self.service.ops.registry(binding["robot_id"]).data
        heading = route_heading(aerial, node["params"])
        pose = evidence.captured_pose
        if pose is None:
            raise AlignmentError("pose_missing", evidence.evidence_id)
        camera = payload["camera"]
        estimate = project_nadir(frame=pose.frame, capture=(pose.position.x, pose.position.y, pose.position.z),
                                 position_cov=pose.position.covariance, position_sigma_m=POSITION_SIGMA_M,
                                 pixel=(found.u, found.v),
                                 camera=Camera(camera["width"], camera["height"], camera["hfov_rad"]),
                                 heading_rad=heading, ground_z=0.0, config=self.config)
        ground = self.service.ops.registry(payload["robot_id"]).data
        moved = to_frame(estimate, Frame(**ground["frame"]), maps=self._maps(), config=self.config)
        description = f"{payload['signature']} block, about {payload['size_m']} m"
        target = decide(moved, description, self.config)
        fact = to_fact(target, fact_id=f"target-{recheck_id(payload['key'])}", subject=job["inputs"]["evidence"]["asset_id"],
                       timestamp=self.clock(), ground_z=0.0, evidence_refs=[evidence.evidence_id], config=self.config,
                       config_digest=self.config_digest)
        derived = {"located": {"u": found.u, "v": found.v, "pixels": found.pixels},
                   "heading_deg": round(math.degrees(heading), 3), "aerial_frame": pose.frame.model_dump(mode="json"),
                   "estimate": {"x": estimate.x, "y": estimate.y, "cov": list(estimate.cov)},
                   "kind": target.kind, "alignment": self.config.version, "alignment_sha256": self.config_digest}
        return fact, derived

    def start(self, run: dict, payload: dict, key: str, guard) -> dict | None:
        """Record the recheck and, when a target exists, its handoff task and offer; None when the guard refused.

        记录复核，存在目标时一并记录交接任务与提议；守卫拒绝时返回 None。
        """
        known = self.by_key(key)
        if known is not None:
            return known
        business, scheduler = self.service.business, self.service.scheduler
        job = business.store.job(payload["analysis_id"])
        finding_id = job["finding_id"] if job is not None else None
        evidence_id = job["inputs"]["evidence"]["evidence_id"] if job is not None else ""
        fact, derived, reason = None, {}, None
        if job is None or finding_id is None:
            reason = "no_finding"
        else:
            try:
                fact, derived = self._target({**payload, "key": key}, job)
            except AlignmentError as error:
                reason = error.code
        identifier = recheck_id(key)
        detail = {"run_id": run["run_id"], "key": key, "signature": payload["signature"], "size_m": payload["size_m"],
                  "robot_id": payload["robot_id"], **derived}
        with self.store.transaction() as db:
            if not guard(db):
                return None
            if fact is None:
                self.store.insert_recheck(recheck_id=identifier, project_id=run["project_id"],
                                          finding_id=finding_id or "", aerial_evidence=[evidence_id] if evidence_id
                                          else [], ground_task_id=None, detail={**detail, "reason": reason})
                self.store.update_recheck(identifier, outcome="not_attempted")
                return self.output(self.store.recheck(identifier))
            asset_id = job["inputs"]["evidence"]["asset_id"]
            ground_map = self.service.ops.registry(payload["robot_id"]).data["registry_id"]
            task, _ = scheduler.submit_handoff(requested_by=f"recheck:{identifier}", key=key,
                                               project_id=run["project_id"], asset_id=asset_id, volume_id=ground_map,
                                               robot_id=payload["robot_id"],
                                               detail={"asset_id": asset_id, "signature": payload["signature"],
                                                       "recheck_id": identifier})
            self.store.insert_recheck(recheck_id=identifier, project_id=run["project_id"], finding_id=finding_id,
                                      aerial_evidence=[evidence_id], ground_task_id=task["task_id"],
                                      detail={**detail, "target": fact.model_dump(mode="json")})
            handoff = self.engine.offer(task_id=task["task_id"], project_id=run["project_id"],
                                        robot_id=payload["robot_id"], target=fact, evidence_refs=[evidence_id],
                                        ttl_s=payload["ttl_s"], actor=ACTOR)
            self.store.update_recheck(identifier, handoff_id=handoff["handoff_id"])
        return self.output(self.store.recheck(identifier))

    # ── the wait and the conclusion / 等待与结论 ──

    def poll(self, identifier: str) -> tuple[NodeState, str | None, dict | None, dict | None]:
        """(state, reason, detail, output) of an `await_recheck` node. / `await_recheck` 节点的状态、原因、详情与输出。"""
        recheck = self.store.recheck(identifier)
        if recheck is None:
            return NodeState.FAILED, "recheck.missing", None, None
        if recheck["outcome"] == "pending":
            recheck = self._advance(recheck)
        if recheck["outcome"] != "pending":
            return NodeState.COMPLETED, None, None, self.result(recheck)
        task = self.service.scheduler.store.task(recheck["ground_task_id"])
        handoff = self.store.handoff(recheck["handoff_id"]) if recheck["handoff_id"] else None
        detail = {"recheck_id": identifier, "task_state": task["state"] if task else None,
                  "handoff_state": handoff["state"] if handoff else None}
        if task is not None and task["mission_id"]:
            mission = self.ledger.mission(task["mission_id"])
            reason, more = self.service.workflows._mission_wait(mission)
            return NodeState.WAITING, reason.value, {**detail, **more}, None
        return NodeState.WAITING, WaitReason.HANDOFF.value, detail, None

    def result(self, recheck: dict) -> dict:
        detail = recheck["detail"]
        return {"recheck_id": recheck["recheck_id"], "outcome": recheck["outcome"],
                "reason": str(detail.get("reason") or ""), "aerial_evidence": list(recheck["aerial_evidence"]),
                "ground_evidence": list(recheck["ground_evidence"]),
                "ground_mission_id": recheck["ground_mission_id"],
                "relocated": detail.get("relocated")}

    def _advance(self, recheck: dict) -> dict:
        """Conclude once the ground task has ended; otherwise leave it pending. / 地面任务结束后得出结论；否则保持待定。"""
        task = self.service.scheduler.store.task(recheck["ground_task_id"]) if recheck["ground_task_id"] else None
        if task is None:
            return self.store.update_recheck(recheck["recheck_id"], outcome="unknown",
                                             detail={**recheck["detail"], "reason": "task_missing"})
        state = task["state"]
        if state in ("queued", "assigned"):
            return recheck
        mission_id = task["mission_id"] or (task["outcome"] or {}).get("mission_id")
        if state == "rejected" or (state == "failed" and mission_id is None):
            return self.store.update_recheck(recheck["recheck_id"], outcome="not_attempted",
                                             detail={**recheck["detail"], "reason": task["reason"] or state})
        if state != "completed" or mission_id is None:
            return self.store.update_recheck(recheck["recheck_id"], outcome="unknown", ground_mission_id=mission_id,
                                             detail={**recheck["detail"], "reason": task["reason"] or state})
        return self._conclude(recheck, mission_id)

    def _conclude(self, recheck: dict, mission_id: str) -> dict:
        """A completed ground mission: read its verified view. / 已完成的地面任务：读取其已核验的画面。"""
        detail = recheck["detail"]
        mission = self.ledger.mission(mission_id)
        record = self.ledger.version(mission_id, mission["current_version"])
        node = next(n for n in record["package"]["nodes"] if n["skill_id"] == GROUND_RECHECK)
        verified = {v["evidence_id"] for v in self.ledger.verifications(mission_id)
                    if v["body"].get("final_verdict") == EffectVerdict.VERIFIED.value
                    and v["body"].get("step_id") == node["task_id"]}
        rows = [r for r in self.ledger.evidence(mission_id, mission["current_version"])
                if r["evidence_id"] in verified and r["media_path"]]
        if not rows:
            return self.store.update_recheck(recheck["recheck_id"], outcome="unknown", ground_mission_id=mission_id,
                                             detail={**detail, "reason": "no_verified_view"})
        row = rows[-1]
        evidence = Evidence.model_validate(row["body"])
        media = self.service.hub.media(row["media_path"])
        image = np.frombuffer(media, dtype=np.uint8).reshape(row["height"], row["width"], 3)
        found = locate(image, detail["signature"])
        evidence_ids = [evidence.evidence_id]
        if found is None:
            return self.store.update_recheck(recheck["recheck_id"], outcome="not_found", ground_mission_id=mission_id,
                                             ground_evidence=evidence_ids,
                                             detail={**detail, "reason": "not_in_ground_view"})
        params = node["params"]
        heading = math.atan2(params["face_point"][1] - params["goal"][1], params["face_point"][0] - params["goal"][0])
        ground = self.service.ops.registry(recheck_robot(detail)).data
        pose = evidence.captured_pose.position
        point = forward_ground(ground["camera"], (pose.x, pose.y), heading, found.u, found.bottom,
                               half_depth_m=float(detail["size_m"]) / 2)
        target = WorldFact.model_validate(detail["target"])
        relocated = None if point is None else [round(point[0], 3), round(point[1], 3)]
        inside = point is not None and within(target, point, self.config.relocation_tolerance_m)
        return self.store.update_recheck(
            recheck["recheck_id"], outcome="relocated" if inside else "not_found", ground_mission_id=mission_id,
            ground_evidence=evidence_ids,
            detail={**detail, "relocated": relocated, "ground_located": {"u": found.u, "v": found.v,
                                                                         "bottom": found.bottom,
                                                                         "pixels": found.pixels},
                    "reason": "" if inside else "located_outside_target"})


def evidence_ref_mission(job: dict) -> str:
    return job["inputs"]["evidence"]["mission_id"]


def evidence_ref_version(job: dict) -> int:
    return int(job["inputs"]["evidence"]["mission_version"])


def recheck_robot(detail: dict) -> str:
    return detail["robot_id"]
