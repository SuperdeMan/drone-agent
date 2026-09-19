"""Add an actual downward RGB sensor and a visible inspection asset inside the simulation image.

在仿真镜像内增加真实下视 RGB 传感器与可见巡检资产。
"""

from pathlib import Path
from xml.etree import ElementTree as ET

root = Path("/opt/PX4-Autopilot/Tools/simulation/gz")
model = root / "models/x500/model.sdf"
tree = ET.parse(model)
body = tree.getroot().find("model")
body.append(
    ET.fromstring("""<link name="inspection_camera">
  <pose>0 0 -0.15 0 1.57079632679 0</pose>
  <inertial><mass>0.001</mass><inertia><ixx>0.00001</ixx><iyy>0.00001</iyy><izz>0.00001</izz></inertia></inertial>
  <sensor name="cam_0" type="camera"><always_on>true</always_on><update_rate>5</update_rate>
    <topic>/drone/cam_0/image</topic><camera><horizontal_fov>1.4</horizontal_fov>
      <image><width>160</width><height>120</height><format>R8G8B8</format></image>
      <clip><near>0.05</near><far>100</far></clip>
    </camera></sensor></link>""")
)
body.append(
    ET.fromstring(
        '<joint name="inspection_camera_joint" type="fixed"><parent>base_link</parent><child>inspection_camera</child></joint>'
    )
)
tree.write(model, encoding="utf-8", xml_declaration=True)
world = root / "worlds/default.sdf"
tree = ET.parse(world)
body = tree.getroot().find("world")
body.append(
    ET.fromstring("""<model name="asset_red"><static>true</static><pose>4 4 0.025 0 0 0</pose><link name="asset">
  <visual name="red_marker"><geometry><box><size>2 2 0.05</size></box></geometry>
  <material><ambient>1 0 0 1</ambient><diffuse>1 0 0 1</diffuse></material></visual>
  <collision name="asset_collision"><geometry><box><size>2 2 0.05</size></box></geometry></collision>
  </link></model>""")
)
tree.write(world, encoding="utf-8", xml_declaration=True)
