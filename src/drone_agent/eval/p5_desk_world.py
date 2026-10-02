"""The desk platform's catalogs in S0 (D069): the resident desk's configuration, every robot logical, in one process.

The world is the P5 S0 world loaded with the four p5_desk_v1 catalogs and the scheduler, so the three projects of the
platform (the PX4 SITL site, the logical fleet whose inspections the scheduler assigns, and the vendor-managed dock)
run together under one service exactly as configured on the desk. `uav_01` is a logical stand-in for the PX4 SITL
aircraft here; nothing in this world is evidence about PX4. The desk world file can drive the logical cameras and the
vendor simulator as on the desk (`sync_world`). Used by the desk smoke test, the soak harness test and the
authorization matrix (`eval/p5_authz.py`).

任务台平台目录的 S0 形态（D069）：常驻任务台的配置，所有机器人均为逻辑的，运行在一个进程中。世界即加载四个 p5_desk_v1 目录与
调度器的 P5 S0 世界，因此平台的三个项目（PX4 SITL 站点、由调度器分配巡检的逻辑机队、厂商托管机场）与任务台上的配置完全
相同地在同一服务下运行。这里的 `uav_01` 是 PX4 SITL 飞行器的逻辑替身；本世界的任何内容都不是关于 PX4 的证据。任务台世界
文件可以像在任务台上那样驱动逻辑相机与厂商模拟器（`sync_world`）。供任务台冒烟测试、长稳编排测试与授权矩阵
（`eval/p5_authz.py`）使用。
"""

from __future__ import annotations

import json
from pathlib import Path

from drone_agent.contracts import utcnow
from drone_agent.eval.p5_world import P5World
from drone_agent.fleet.scheduler import Scheduler, build_scheduling

FLEET_ROBOTS = {"uav_fa": ("asset_red", "asset_blue", "road_north"),
                "uav_fb": ("asset_red_b", "asset_blue_b", "road_north_b")}


class P5DeskWorld(P5World):
    """The p5_desk_v1 platform in S0. / S0 中的 p5_desk_v1 平台。"""

    extra_backends = ("px4_sitl", "vendor_protocol_sim")
    catalog_file = "configs/sites/p5_desk_v1.yaml"
    workflows_file = "configs/workflows/p5_desk_v1.yaml"
    business_file = "configs/analysis/p5_desk_v1.yaml"
    members_file = "configs/sites/p5_members_desk_s0.yaml"
    scheduling_file = "configs/scheduling/p5_desk_v1.yaml"
    signatures = {"asset_red": "red", "asset_blue": "blue", "road_north": "green", "asset_red_b": "red",
                  "asset_blue_b": "blue", "road_north_b": "green"}

    def __init__(self, case: Path, repo: Path, *, seed: int):
        super().__init__(case, repo, seed=seed)
        # Capture records like the resident fleet's, for the soak judge. / 与常驻机队相同的拍摄记录，供长稳裁判使用。
        self.captures: list[dict] = []
        for robot_id, uav in self.uavs.items():
            uav.camera = self._logged(robot_id, uav.camera)

    def _logged(self, robot_id: str, camera):
        def capture(asset_id: str) -> bytes:
            raw = camera(asset_id)
            self.captures.append({"at": utcnow().isoformat(), "robot_id": robot_id, "asset_id": asset_id,
                                  "state": self.appearance.history[-1]["state"]})
            return raw
        return capture

    def export_truth(self, fleet: Path, vendor: Path) -> None:
        """The fleet captures and the vendor truth in the resident containers' log formats.

        以常驻容器的日志格式写出机队拍摄与厂商真值。
        """
        from drone_agent.eval.s0_fleet import append_rows

        append_rows(fleet / "captures.jsonl", self.captures)
        rows = [{"kind": kind, **entry} for kind, entries in self.vendor_sim.truth.items() for entry in entries]
        append_rows(vendor / "truth.jsonl", sorted(rows, key=lambda row: row["at"]))

    def _service(self):
        service = super()._service()
        scheduling = build_scheduling(self.ledger, self.repo / self.scheduling_file, service.ops,
                                      backups=self.case / "service/backups", clock=self.clock)
        service.scheduler = Scheduler(service, scheduling, worker_id=f"sch-desk-{len(self.injections)}")
        return service

    async def step(self, dt: float = 0.1) -> None:
        await super().step(dt)
        self.service.tick_scheduler()
        for mission_id in sorted(self.service.dirty):
            self.service.dirty.discard(mission_id)
            self.service.refresh(mission_id)

    def sync_world(self, path: Path) -> None:
        """Apply the desk world file's S0 and S3 sections to the logical cameras and the vendor simulator.

        把任务台世界文件的 S0 与 S3 两节应用到逻辑相机与厂商模拟器。
        """
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if data.get("format") != "drone.desk-world/v1":
            return
        fleet = data.get("s0") or {}
        for robot, assets in FLEET_ROBOTS.items():
            for asset in assets:
                state = "damaged" if fleet.get(asset, "normal") != "normal" else "normal"
                self.appearance.set(robot, asset, state)
        self.vendor_sim.damaged = {a for a, s in (data.get("s3") or {}).items() if s != "normal"}
