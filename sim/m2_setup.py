"""Add the M2 blue inspection asset to the simulation world; the M1 image and world stay unchanged.

在仿真世界中加入 M2 的蓝色巡检资产；M1 镜像与世界保持不变。
"""

from pathlib import Path
from xml.etree import ElementTree as ET

world = Path("/opt/PX4-Autopilot/Tools/simulation/gz/worlds/default.sdf")
tree = ET.parse(world)
body = tree.getroot().find("world")
if body.find("model[@name='asset_blue']") is None:
    # Position and size match configs/scenarios/m2_campus_v2.yaml. / 位置与尺寸与 m2_campus_v2 登记表一致。
    body.append(
        ET.fromstring("""<model name="asset_blue"><static>true</static><pose>-4 6 0.025 0 0 0</pose><link name="asset">
  <visual name="blue_marker"><geometry><box><size>2 2 0.05</size></box></geometry>
  <material><ambient>0 0 1 1</ambient><diffuse>0 0 1 1</diffuse></material></visual>
  <collision name="asset_collision"><geometry><box><size>2 2 0.05</size></box></geometry></collision>
  </link></model>""")
    )
    tree.write(world, encoding="utf-8", xml_declaration=True)
