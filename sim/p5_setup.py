"""Add the P5 road segment marking to the M2 simulation world; the M1 and M2 images and worlds stay unchanged (D071).

在 M2 仿真世界中加入 P5 道路段标线；M1 与 M2 的镜像与世界保持不变（D071）。
"""

from pathlib import Path
from xml.etree import ElementTree as ET

world = Path("/opt/PX4-Autopilot/Tools/simulation/gz/worlds/default.sdf")
tree = ET.parse(world)
body = tree.getroot().find("world")
if body.find("model[@name='road_north']") is None:
    # Position and size match `road_north` in configs/scenarios/p5_site_s1_v1.yaml (eval/p5_layout.py): a 4 m x 2 m
    # green marking centred at (2, 8), thin and without collision, away from the M2 routes and markers.
    # 位置与尺寸与 p5_site_s1_v1 中的 `road_north` 一致（eval/p5_layout.py）：中心 (2, 8)、4 m × 2 m 的绿色标线，薄、无碰撞体，
    # 远离 M2 的航线与标记。
    body.append(
        ET.fromstring("""<model name="road_north"><static>true</static><pose>2 8 0.02 0 0 0</pose><link name="road">
  <visual name="road_marking"><geometry><box><size>4 2 0.04</size></box></geometry>
  <material><ambient>0 1 0 1</ambient><diffuse>0 1 0 1</diffuse></material></visual>
  </link></model>""")
    )
    tree.write(world, encoding="utf-8", xml_declaration=True)
