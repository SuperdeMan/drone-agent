"""The X1 S0 scene (D088): declared anomaly blocks in the shared frame, seen by the logical cameras of both robots.

A block is a square of a declared colour standing on the ground. The aircraft's logical nadir camera draws, from the
capture position at the inspection route's declared heading, the inspected asset's marker in its registered signature
colour and every block in view; the ground robot's forward camera draws the blocks in its own map. A block may be
seen from the air only (a false positive the aircraft reported) or from the ground only. The frames are test fixtures:
they prove the handoff and the alignment, never recognition, and the scene decides nothing about pass or fail.

X1 S0 场景（D088）：共享坐标中声明的异常物方块，由两台机器人的逻辑相机看到。

方块是立在地面上、具有声明颜色的正方形。飞行器的逻辑下视相机从采集位置、按巡检航线的声明航向，画出被巡检资产的登记特征色
标记与视野内的每个方块；地面车的前视相机在其自己的地图中画出方块。方块可以只从空中看到（飞行器报告的误报），也可以只从地面
看到。这些画面是测试素材：它们证明交接与对齐，从不证明识别；场景不对通过与否做任何决定。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from drone_agent.eval.logical_ground import Block
from drone_agent.fleet.locator import COLOURS
from drone_agent.fleet.rechecks import route_heading

# The S0 logical nadir camera of P6 (quality_target_s0_v1): 160x120, focal 80 px. / P6 的 S0 逻辑下视相机。
NADIR_CAMERA = {"width": 160, "height": 120, "hfov_rad": math.pi / 2}
SIGNATURES = {"red": (230, 20, 20), "green": (20, 230, 20), "blue": (20, 20, 230)}
BACKGROUND = (128, 128, 128)


@dataclass(frozen=True)
class Anomaly:
    """A declared block in the shared frame and who can see it. / 共享坐标中声明的方块，以及谁能看到它。"""

    x: float
    y: float
    size_m: float
    colour: str
    from_air: bool = True
    from_ground: bool = True


def nadir_view(camera: dict, position, heading: float, squares) -> bytes:
    """A level nadir RGB frame: image up is `heading` (radians from +x towards +y), image left is 90 degrees further.

    Each square is (x, y, size_m, rgb) on the ground in the same frame as `position`, drawn in order as the bounding
    box of its corners, which is exact at headings that are multiples of 90 degrees.

    水平下视 RGB 帧：画面上方是 `heading`（自 +x 向 +y 的弧度），画面左方再转 90°。每个方块为与 `position` 同坐标系中地面上的
    (x, y, 边长, rgb)，按顺序以其各角的外包矩形绘制；航向为 90° 的整数倍时这是精确的。
    """
    width, height = camera["width"], camera["height"]
    focal = width / 2 / math.tan(camera["hfov_rad"] / 2)
    altitude = position[2]
    frame = np.empty((height, width, 3), dtype=np.uint8)
    frame[:, :] = BACKGROUND
    c, s = math.cos(heading), math.sin(heading)
    for x, y, size, rgb in squares:
        us, vs = [], []
        for dx in (-size / 2, size / 2):
            for dy in (-size / 2, size / 2):
                ox, oy = x + dx - position[0], y + dy - position[1]
                forward, left = ox * c + oy * s, -ox * s + oy * c
                us.append(width / 2 - left * focal / altitude)
                vs.append(height / 2 - forward * focal / altitude)
        left_px, right_px = max(0, round(min(us))), min(width, round(max(us)))
        top_px, bottom_px = max(0, round(min(vs))), min(height, round(max(vs)))
        if left_px < right_px and top_px < bottom_px:
            frame[top_px:bottom_px, left_px:right_px] = rgb
    return frame.tobytes()


class X1Scene:
    """The blocks of one S0 case and the cameras that see them. / 一个 S0 用例中的方块及看到它们的相机。"""

    def __init__(self, origins: dict[str, tuple[float, float]]):
        self.origins = origins  # site id -> map origin in the shared frame / 站点 -> 地图原点在共享坐标中的位置
        self.anomalies: list[Anomaly] = []

    def place(self, site_id: str, x: float, y: float, *, size_m: float, colour: str, from_air: bool = True,
              from_ground: bool = True) -> Anomaly:
        """A block at (x, y) of `site_id`'s map. / 在 `site_id` 地图的 (x, y) 处放一个方块。"""
        if colour not in COLOURS:
            raise ValueError(f"unknown anomaly colour {colour!r}")
        ox, oy = self.origins[site_id]
        anomaly = Anomaly(ox + x, oy + y, size_m, colour, from_air, from_ground)
        self.anomalies.append(anomaly)
        return anomaly

    def remove(self, anomaly: Anomaly) -> None:
        self.anomalies.remove(anomaly)

    def local(self, site_id: str, *, ground: bool) -> list[Anomaly]:
        """The visible blocks in `site_id`'s map. / `site_id` 地图中可见的方块。"""
        ox, oy = self.origins[site_id]
        return [Anomaly(a.x - ox, a.y - oy, a.size_m, a.colour, a.from_air, a.from_ground) for a in self.anomalies
                if (a.from_ground if ground else a.from_air)]

    def ground_blocks(self, site_id: str) -> list[Block]:
        return [Block(a.x, a.y, a.size_m, COLOURS[a.colour]) for a in self.local(site_id, ground=True)]

    def aerial_camera(self, uav, site_id: str, data: dict):
        """The camera hook of one logical aircraft: its last observed position, the declared heading of the asset's
        observation route, the asset's marker and the blocks seen from the air.

        一架逻辑飞行器的相机钩子：其最后观测到的位置、资产观察航线的声明航向、资产标记与空中可见的方块。
        """
        def capture(asset_id: str) -> bytes:
            asset = data["assets"][asset_id]
            heading = route_heading(data, {"approach_route_id": asset["observation_route"]})
            marker = (float(asset["position"][0]), float(asset["position"][1]), float(asset.get("size_m", 2)),
                      SIGNATURES[asset["visual_signature"]])
            blocks = [(a.x, a.y, a.size_m, COLOURS[a.colour]) for a in self.local(site_id, ground=False)]
            return nadir_view(NADIR_CAMERA, uav.position, heading, [marker, *blocks])
        return capture
