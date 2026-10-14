"""X1 air-ground world (D088), applied inside the X1 simulation image only; the M1, M2 and P3 images stay unchanged.

1. The x500 model gets the P3 downward RGB camera with a scoped topic (`x500_0`).
2. The world gets the P3 markers west (red), mid (green) and east (blue) at the positions of
   configs/scenarios/p3_s1_a_v1.yaml, and the building of configs/scenarios/x1_ground_s1_v1.yaml as a static box: the
   ground robot's map declares it, and its walls give the forward camera something to see.
3. The rover_differential model gets a forward RGB camera, level, its optical centre 0.5 m above the ground over the
   robot's reference point, 160x120 with an 80 degree field of view, as configs/scenarios/x1_ground_s1_v1.yaml declares
   (the map declares no forward offset); the topic is scoped to the instance (`rover_differential_1`).
4. The rover airframe drains its simulated battery in 1800 s instead of PX4's default 60 s (the X1 rover probe), and
   its speed limit RO_SPEED_LIM is the platform's declared 1.0 m/s instead of PX4's 1.8: mission speed items take
   hold only after the first waypoint, and the guardian's look-ahead is derived from 1.0 m/s (X1 S1 probes). Its
   flight log keeps PX4's default of one file per armed period, whose span the judge reads as the armed truth.
5. The API/offboard link and the MAVLink system id follow `PX4_OFFBOARD_PARTNER` and `PX4_SYS_ID_OVERRIDE` as in P3, so
   each guardian addresses its flight controller as system 1 on its own link.
6. Shadows are off, as in P3.

X1 空地世界（D088），只在 X1 仿真镜像内应用；M1、M2 与 P3 镜像保持不变。
1. x500 模型加上 P3 的下视 RGB 相机，话题按实例限定（`x500_0`）。
2. 世界加上 P3 的 west（红）、mid（绿）、east（蓝）标记，位置与 configs/scenarios/p3_s1_a_v1.yaml 一致；并加上
   configs/scenarios/x1_ground_s1_v1.yaml 中的楼作为静态方块：地面车的地图声明了它，它的墙也让前视相机有东西可看。
3. rover_differential 模型加一台前视 RGB 相机：水平朝前，光心位于车体参考点正上方、离地 0.5 m，160x120，视场角 80°，与
   configs/scenarios/x1_ground_s1_v1.yaml 的声明一致（地图未声明前向偏移）；话题按实例限定（`rover_differential_1`）。
4. 地面车机架把仿真电池放电时间从 PX4 默认的 60 s 改为 1800 s（X1 地面车实测），限速 RO_SPEED_LIM 取平台声明的 1.0 m/s
   而不是 PX4 的 1.8：任务改速项要过了第一个航点才生效，而 guardian 的前瞻按 1.0 m/s 推导（X1 S1 探针）。其飞行日志保持
   PX4 默认：每个解锁期一个文件，裁判把文件的时间跨度当作解锁真值。
5. API / offboard 链路与 MAVLink 系统 ID 与 P3 相同，按 `PX4_OFFBOARD_PARTNER` 与 `PX4_SYS_ID_OVERRIDE` 设置，每个 guardian 在
   自己的链路上把其飞控当作系统 1 寻址。
6. 与 P3 一样关闭阴影。
"""

from pathlib import Path
from xml.etree import ElementTree as ET

root = Path("/opt/PX4-Autopilot/Tools/simulation/gz")
build = Path("/opt/PX4-Autopilot/build/px4_sitl_default/etc/init.d-posix")


def add_link(model_path: Path, name: str, link: str, joint: str) -> None:
    tree = ET.parse(model_path)
    body = tree.getroot().find("model")
    if body.find(f"link[@name='{name}']") is None:
        body.append(ET.fromstring(link))
        body.append(ET.fromstring(joint))
        tree.write(model_path, encoding="utf-8", xml_declaration=True)


add_link(root / "models/x500/model.sdf", "inspection_camera", """<link name="inspection_camera">
  <pose>0 0 -0.15 0 1.57079632679 0</pose>
  <inertial><mass>0.001</mass><inertia><ixx>0.00001</ixx><iyy>0.00001</iyy><izz>0.00001</izz></inertia></inertial>
  <sensor name="cam_0" type="camera"><always_on>true</always_on><update_rate>5</update_rate>
    <camera><horizontal_fov>1.4</horizontal_fov>
      <image><width>160</width><height>120</height><format>R8G8B8</format></image>
      <clip><near>0.05</near><far>100</far></clip>
    </camera></sensor></link>""",
         '<joint name="inspection_camera_joint" type="fixed"><parent>base_link</parent>'
         '<child>inspection_camera</child></joint>')
# base_link sits 0.1 m above the ground, so 0.4 m above it is 0.5 m above the ground. / base_link 离地 0.1 m，因此再高
# 0.4 m 即离地 0.5 m。
add_link(root / "models/rover_differential/model.sdf", "front_camera", """<link name="front_camera">
  <pose relative_to="base_link">0 0 0.4 0 0 0</pose>
  <inertial><mass>0.001</mass><inertia><ixx>0.00001</ixx><iyy>0.00001</iyy><izz>0.00001</izz></inertia></inertial>
  <sensor name="cam_0" type="camera"><always_on>true</always_on><update_rate>5</update_rate>
    <camera><horizontal_fov>1.3962634016</horizontal_fov>
      <image><width>160</width><height>120</height><format>R8G8B8</format></image>
      <clip><near>0.05</near><far>100</far></clip>
    </camera></sensor></link>""",
         '<joint name="front_camera_joint" type="fixed"><parent>base_link</parent><child>front_camera</child></joint>')

world = root / "worlds/default.sdf"
tree = ET.parse(world)
body = tree.getroot().find("world")
MARKERS = {"asset_west": ((-4, 4), "1 0 0 1"), "asset_mid": ((5, 4), "0 1 0 1"), "asset_east": ((16, 4), "0 0 1 1")}
for name, ((x, y), colour) in MARKERS.items():
    if body.find(f"model[@name='{name}']") is None:
        body.append(ET.fromstring(f"""<model name="{name}"><static>true</static><pose>{x} {y} 0.025 0 0 0</pose>
  <link name="asset"><visual name="marker"><geometry><box><size>2 2 0.05</size></box></geometry>
  <material><ambient>{colour}</ambient><diffuse>{colour}</diffuse></material></visual>
  <collision name="asset_collision"><geometry><box><size>2 2 0.05</size></box></geometry></collision>
  </link></model>"""))
# The building of x1_ground_s1_v1 (world x 1..3, y 6..9), lower than the 4 m flight altitude. / x1_ground_s1_v1 中的楼
# （世界坐标 x 1..3、y 6..9），低于 4 m 飞行高度。
if body.find("model[@name='building']") is None:
    body.append(ET.fromstring("""<model name="building"><static>true</static><pose>2 7.5 1.25 0 0 0</pose>
  <link name="walls"><visual name="walls"><geometry><box><size>2 3 2.5</size></box></geometry>
  <material><ambient>0.45 0.42 0.38 1</ambient><diffuse>0.45 0.42 0.38 1</diffuse></material></visual>
  <collision name="walls"><geometry><box><size>2 3 2.5</size></box></geometry></collision></link></model>"""))
scene = body.find("scene")
if scene is not None and scene.find("shadows") is not None:
    scene.find("shadows").text = "false"
for light in body.findall("light"):
    if light.find("cast_shadows") is not None:
        light.find("cast_shadows").text = "false"
tree.write(world, encoding="utf-8", xml_declaration=True)

airframe = build / "airframes/50000_gz_rover_differential"
text = airframe.read_text()
if "SIM_BAT_DRAIN" not in text:
    airframe.write_text(text.rstrip("\n") + "\n\n# X1 (D088): drain the simulated battery in 1800 s, and cap the speed at the platform's "
                        "1.0 m/s. / 仿真电池 1800 s 放完；限速取平台的 1.0 m/s。\n"
                        "param set-default SIM_BAT_DRAIN 1800\nparam set-default RO_SPEED_LIM 1.0\n")

rc = build / "px4-rc.mavlink"
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

rcs = build / "rcS"
text = rcs.read_text()
line = "param set MAV_SYS_ID $((px4_instance+1))"
if "PX4_SYS_ID_OVERRIDE" not in text:
    if text.count(line) != 1:
        raise SystemExit("the PX4 v1.17 per-instance MAV_SYS_ID line was not found exactly once")
    rcs.write_text(text.replace(line, "param set MAV_SYS_ID ${PX4_SYS_ID_OVERRIDE:-$((px4_instance+1))}"))
print("x1 world ready")
