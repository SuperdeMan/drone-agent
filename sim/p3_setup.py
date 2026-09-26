"""P3 two-aircraft world (D061), applied inside the P3 simulation image only; the M1 and M2 images stay unchanged.

1. The x500 model gets the same downward RGB camera as M1, but without an absolute topic, so each spawned instance
   (`x500_0`, `x500_1`) publishes on its own scoped topic.
2. The world gets the three P3 markers: west (red), mid (green) and east (blue), at the shared-frame positions of
   configs/scenarios/p3_s1_*.yaml.
3. The API/offboard MAVLink link of an instance goes to `PX4_OFFBOARD_PARTNER` on remote port 14540 when that variable
   is set, so each guardian keeps the default port in its own network namespace and no onboard code changes.
4. `PX4_SYS_ID_OVERRIDE` replaces the per-instance MAVLink system id (`instance + 1`) when set: the onboard adapter
   addresses its flight controller as system 1, like every real aircraft on its own link, and each instance's link
   reaches only its own guardian.
5. Shadows are off: on the CPU-only host every camera frame otherwise renders shadow maps, two cameras keep the
   simulator near two cores and each lift-off stalls the world close to the guardian's 0.5 s observation limit. The
   inspection check uses marker colours, not shading.

P3 双机世界（D061），只在 P3 仿真镜像内应用；M1 与 M2 镜像保持不变。

1. x500 模型加上与 M1 相同的下视 RGB 相机，但不写绝对话题，因此每个生成的实例（`x500_0`、`x500_1`）在各自的限定话题上发布。
2. 世界加上三个 P3 标记：west（红）、mid（绿）与 east（蓝），位于 configs/scenarios/p3_s1_*.yaml 的共享坐标位置。
3. 设置了 `PX4_OFFBOARD_PARTNER` 时，实例的 API / offboard MAVLink 链路发往该地址的远端端口 14540，因此每个 guardian 在
   自己的网络命名空间里保持默认端口，机载代码不变。
4. 设置了 `PX4_SYS_ID_OVERRIDE` 时替换按实例的 MAVLink 系统 ID（`instance + 1`）：机载适配器把其飞控当作系统 1 寻址，
   与每架真实飞行器在自己链路上一样，而每个实例的链路只通向自己的 guardian。
5. 关闭阴影：纯 CPU 主机上每个相机帧都要渲染阴影图，两路相机使仿真器接近两核，每次离地时世界停顿接近 guardian 的 0.5 s
   观测上限。巡检核验只看标记颜色，不依赖明暗。
"""

from pathlib import Path
from xml.etree import ElementTree as ET

root = Path("/opt/PX4-Autopilot/Tools/simulation/gz")
model = root / "models/x500/model.sdf"
tree = ET.parse(model)
body = tree.getroot().find("model")
if body.find("link[@name='inspection_camera']") is None:
    body.append(
        ET.fromstring("""<link name="inspection_camera">
  <pose>0 0 -0.15 0 1.57079632679 0</pose>
  <inertial><mass>0.001</mass><inertia><ixx>0.00001</ixx><iyy>0.00001</iyy><izz>0.00001</izz></inertia></inertial>
  <sensor name="cam_0" type="camera"><always_on>true</always_on><update_rate>5</update_rate>
    <camera><horizontal_fov>1.4</horizontal_fov>
      <image><width>160</width><height>120</height><format>R8G8B8</format></image>
      <clip><near>0.05</near><far>100</far></clip>
    </camera></sensor></link>""")
    )
    body.append(ET.fromstring(
        '<joint name="inspection_camera_joint" type="fixed"><parent>base_link</parent><child>inspection_camera</child>'
        '</joint>'))
    tree.write(model, encoding="utf-8", xml_declaration=True)

world = root / "worlds/default.sdf"
tree = ET.parse(world)
body = tree.getroot().find("world")
# Shared-frame positions and colours match configs/scenarios/p3_s1_a_v1.yaml and p3_s1_b_v1.yaml.
# 共享坐标位置与颜色与 configs/scenarios/p3_s1_a_v1.yaml、p3_s1_b_v1.yaml 一致。
MARKERS = {"asset_west": ((-4, 4), "1 0 0 1"), "asset_mid": ((5, 4), "0 1 0 1"), "asset_east": ((16, 4), "0 0 1 1")}
for name, ((x, y), colour) in MARKERS.items():
    if body.find(f"model[@name='{name}']") is None:
        body.append(ET.fromstring(f"""<model name="{name}"><static>true</static><pose>{x} {y} 0.025 0 0 0</pose>
  <link name="asset"><visual name="marker"><geometry><box><size>2 2 0.05</size></box></geometry>
  <material><ambient>{colour}</ambient><diffuse>{colour}</diffuse></material></visual>
  <collision name="asset_collision"><geometry><box><size>2 2 0.05</size></box></geometry></collision>
  </link></model>"""))
scene = body.find("scene")
if scene is not None and scene.find("shadows") is not None:
    scene.find("shadows").text = "false"
for light in body.findall("light"):
    if light.find("cast_shadows") is not None:
        light.find("cast_shadows").text = "false"
tree.write(world, encoding="utf-8", xml_declaration=True)

rc = Path("/opt/PX4-Autopilot/build/px4_sitl_default/etc/init.d-posix/px4-rc.mavlink")
text = rc.read_text()
line = "mavlink start -x -u $udp_offboard_port_local -r 4000000 -f -m onboard -o $udp_offboard_port_remote"
if "PX4_OFFBOARD_PARTNER" not in text:
    if text.count(line) != 1:
        raise SystemExit("the PX4 v1.17 API/offboard MAVLink line was not found exactly once")
    text = text.replace(line, (
        'if [ -n "${PX4_OFFBOARD_PARTNER:-}" ]; then\n'
        '\tmavlink start -x -u $udp_offboard_port_local -r 4000000 -f -m onboard -o 14540 -t $PX4_OFFBOARD_PARTNER\n'
        'else\n'
        f'\t{line}\n'
        'fi'))
    rc.write_text(text)

rcs = Path("/opt/PX4-Autopilot/build/px4_sitl_default/etc/init.d-posix/rcS")
text = rcs.read_text()
line = "param set MAV_SYS_ID $((px4_instance+1))"
if "PX4_SYS_ID_OVERRIDE" not in text:
    if text.count(line) != 1:
        raise SystemExit("the PX4 v1.17 per-instance MAV_SYS_ID line was not found exactly once")
    rcs.write_text(text.replace(line, "param set MAV_SYS_ID ${PX4_SYS_ID_OVERRIDE:-$((px4_instance+1))}"))
print("p3 world ready")
