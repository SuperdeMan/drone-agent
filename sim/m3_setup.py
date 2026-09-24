"""Build the M3 simulation world on top of the M2 image; M1 and M2 images and worlds stay unchanged (D041).

Adds, inside the simulation image only: a forward depth camera and odometry noise on PX4's `x500_vision` model (which
keeps M1's downward RGB camera through its include of `x500`); the registered wall and pillar, the green asset and
the backup landing pad of `m3_campus_v3`; and one UNREGISTERED crate that the aircraft can only learn about through
its depth camera (its geometry is written to the judge's truth file, never to the registry). The `x500_vision`
airframe copy that SITL actually loads gets three simulation parameters: external-vision fusion, PX4's failure
injection switch and a slower simulated battery. No failsafe parameter is changed.

在 M2 镜像之上构建 M3 仿真世界；M1、M2 的镜像与世界保持不变（D041）。

只在仿真镜像内增加：PX4 `x500_vision` 机体（经 include `x500` 保留 M1 的下视 RGB 相机）上的前视深度相机与里程计噪声；
`m3_campus_v3` 登记的墙体、立柱、绿色资产与备用降落点；以及一个**未登记**的箱体，飞行器只能经深度相机感知它（其
几何只写入裁判真值文件，从不写入登记表）。SITL 实际加载的 `x500_vision` 机架副本增加三个仿真参数：外部视觉融合、
PX4 故障注入开关与更慢的仿真电池。不改动任何失效保护参数。
"""

import json
from pathlib import Path
from xml.etree import ElementTree as ET

ROOT = Path("/opt/PX4-Autopilot")
GZ = ROOT / "Tools/simulation/gz"

# Truth-only geometry the judge reads; the aircraft never mounts this file. / 只供裁判读取的真值几何；飞行器从不挂载。
TRUTH = {
    "obstacles": {
        "wall_a": {"kind": "box", "min": [-4, 14.8, 0], "max": [4, 15.2, 8]},
        "pillar_a": {"kind": "cylinder", "center": [-6, 15], "radius_m": 0.4, "z_min": 0, "z_max": 8},
        "crate_x": {"kind": "box", "min": [2.0, 19.0, 0], "max": [3.0, 20.0, 6], "registered": False},
    }
}


def box(name, low, high, rgb):
    size = [hi - lo for lo, hi in zip(low, high, strict=True)]
    centre = [(lo + hi) / 2 for lo, hi in zip(low, high, strict=True)]
    colour = " ".join(str(c) for c in rgb) + " 1"
    return ET.fromstring(f"""<model name="{name}"><static>true</static>
  <pose>{centre[0]} {centre[1]} {centre[2]} 0 0 0</pose><link name="body">
  <visual name="visual"><geometry><box><size>{size[0]} {size[1]} {size[2]}</size></box></geometry>
  <material><ambient>{colour}</ambient><diffuse>{colour}</diffuse></material></visual>
  <collision name="collision"><geometry><box><size>{size[0]} {size[1]} {size[2]}</size></box></geometry></collision>
  </link></model>""")


def cylinder(name, centre, radius, height, rgb):
    colour = " ".join(str(c) for c in rgb) + " 1"
    return ET.fromstring(f"""<model name="{name}"><static>true</static>
  <pose>{centre[0]} {centre[1]} {height / 2} 0 0 0</pose><link name="body">
  <visual name="visual"><geometry><cylinder><radius>{radius}</radius><length>{height}</length></cylinder></geometry>
  <material><ambient>{colour}</ambient><diffuse>{colour}</diffuse></material></visual>
  <collision name="collision"><geometry><cylinder><radius>{radius}</radius><length>{height}</length></cylinder></geometry>
  </collision></link></model>""")


def setup_model():
    path = GZ / "models/x500_vision/model.sdf"
    tree = ET.parse(path)
    model = tree.getroot().find("model")
    plugin = model.find("plugin[@name='gz::sim::systems::OdometryPublisher']")
    assert plugin is not None, "x500_vision lost its odometry publisher"
    if plugin.find("gaussian_noise") is None:
        noise = ET.SubElement(plugin, "gaussian_noise")
        noise.text = "0.01"
    if model.find("link[@name='depth_camera_link']") is None:
        model.append(ET.fromstring("""<link name="depth_camera_link">
  <pose>0.12 0 0.05 0 0 0</pose>
  <inertial><mass>0.001</mass><inertia><ixx>0.00001</ixx><iyy>0.00001</iyy><izz>0.00001</izz></inertia></inertial>
  <sensor name="depth_0" type="depth_camera"><always_on>true</always_on><update_rate>5</update_rate>
    <topic>/drone/depth_0/depth_image</topic>
    <camera><horizontal_fov>1.5</horizontal_fov>
      <image><width>64</width><height>48</height><format>R_FLOAT32</format></image>
      <clip><near>0.3</near><far>12</far></clip>
      <noise><type>gaussian</type><mean>0</mean><stddev>0.02</stddev></noise>
    </camera></sensor></link>"""))
        model.append(ET.fromstring(
            '<joint name="depth_camera_joint" type="fixed"><parent>base_link</parent>'
            '<child>depth_camera_link</child></joint>'))
    tree.write(path, encoding="utf-8", xml_declaration=True)


def setup_world():
    path = GZ / "worlds/default.sdf"
    tree = ET.parse(path)
    world = tree.getroot().find("world")
    grey, brown = (0.55, 0.55, 0.55), (0.5, 0.38, 0.28)
    additions = {
        "wall_a": lambda: box("wall_a", [-4, 14.8, 0], [4, 15.2, 8], grey),
        "pillar_a": lambda: cylinder("pillar_a", (-6, 15), 0.4, 8, grey),
        "crate_x": lambda: box("crate_x", [2.0, 19.0, 0], [3.0, 20.0, 6], brown),
        "asset_green": lambda: box("asset_green", [-1, 27, 0], [1, 29, 0.05], (0, 1, 0)),
        "site_b_pad": lambda: box("site_b_pad", [-4, 30, 0], [-2, 32, 0.02], (0.3, 0.3, 0.3)),
    }
    for name, build in additions.items():
        if world.find(f"model[@name='{name}']") is None:
            world.append(build())
    tree.write(path, encoding="utf-8", xml_declaration=True)


def setup_airframe():
    # SITL loads the airframe copy in the build tree; edit that copy so no rebuild is needed.
    # SITL 加载构建目录中的机架副本；修改该副本即可，无需重新编译。
    path = ROOT / "build/px4_sitl_default/etc/init.d-posix/airframes/4005_gz_x500_vision"
    text = path.read_text()
    marker = "# drone-agent M3 simulation parameters"
    if marker not in text:
        text += (
            f"\n{marker} (D041, D045): vision fusion and a slower simulated battery.\n"
            "param set-default EKF2_EV_CTRL 5\n"
            "param set-default EKF2_EV_NOISE_MD 1\n"
            "param set-default SIM_BAT_DRAIN 500\n"
        )
        path.write_text(text)


def main():
    setup_model()
    setup_world()
    setup_airframe()
    Path("/opt/drone-sim/m3-truth.json").write_text(json.dumps(TRUTH, indent=2))


if __name__ == "__main__":
    main()
