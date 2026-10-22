"""Host side of the X1 world probe (D088, S1 preparation); started with nohup, takes the project lock without blocking.

It builds a throwaway image from the pinned M1 aircraft image plus sim/x1_setup.py and sim/x1_sitl.sh, starts the
two-instance X1 world, relays both cameras and both truths with sim/collect.py, places four calibration blocks in front
of the rover (orange and magenta, each with the declared colour as linear emissive and as sRGB-compensated emissive) and
a plain lit orange one, captures the rover's frame, drives the rover once through the probe client, stops everything so
the rover's flight log closes, extracts it, and removes its containers and image. Not a gate.

X1 世界探针的主机侧（D088，S1 准备）；用 nohup 启动，以非阻塞方式获取项目锁。它用固定的 M1 机载镜像加 sim/x1_setup.py
与 sim/x1_sitl.sh 构建一次性镜像，启动双实例 X1 世界，用 sim/collect.py 转接两路相机与两路真值，在地面车前方放置四个
校准方块（橙色与品红，各以声明颜色作线性自发光、以及按 sRGB 补偿的自发光）和一个普通受光的橙色方块，采集地面车画面，
通过探针客户端让地面车行驶一次，全部停止使地面车飞行日志关闭，提取日志，最后删除其容器与镜像。不是门禁。
"""

import base64
import fcntl
import json
import re
import shutil
import subprocess
import time
from pathlib import Path

HOME = Path.home()
WORK = HOME / "drone-agent/scratch/x1worldprobe"
LOCK = HOME / "drone-agent/stack.lock"
IMAGE = "drone-agent-x1probe:dev"
SITL, COL_A, COL_G = "x1probe-sitl", "x1probe-col-a", "x1probe-col-g"
FILES = {}  # filled in by make_probe.py / 由 make_probe.py 填入
BUILD = "/opt/PX4-Autopilot/build/px4_sitl_default"
CAMERA_A = "/world/default/model/x500_0/link/inspection_camera/sensor/cam_0/image"
CAMERA_G = "/world/default/model/rover_differential_1/link/front_camera/sensor/cam_0/image"


def run(cmd, timeout=120):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as error:
        return subprocess.CompletedProcess(cmd, 124, str(error.stdout or ""), f"timeout: {error}")


def block(name: str, x: float, y: float, size: float, emissive, lit) -> str:
    e = " ".join(f"{v:.4f}" for v in emissive) + " 1"
    d = " ".join(f"{v:.4f}" for v in lit) + " 1"
    return (f'<?xml version="1.0"?><sdf version="1.9"><model name="{name}"><static>true</static>'
            f'<pose>{x} {y} {size / 2} 0 0 0</pose><link name="block"><visual name="v"><geometry><box>'
            f'<size>{size} {size} {size}</size></box></geometry><material><ambient>{d}</ambient><diffuse>{d}</diffuse>'
            f'<specular>0 0 0 1</specular><emissive>{e}</emissive></material></visual></link></model></sdf>')


BLOCKS = {
    "o_lin": (0.8, -1.0, (1.0, 140 / 255, 0.0), (0, 0, 0)),
    "o_cmp": (1.6, -1.0, (1.0, (140 / 255) ** 2.2, 0.0), (0, 0, 0)),
    "m_lin": (2.4, -1.0, (230 / 255, 0.0, 230 / 255), (0, 0, 0)),
    "m_cmp": (3.2, -1.0, ((230 / 255) ** 2.2, 0.0, (230 / 255) ** 2.2), (0, 0, 0)),
    "o_lit": (2.0, 0.5, (0, 0, 0), (1.0, 140 / 255, 0.0)),
}


def write(report: dict) -> None:
    (WORK / "report.json").write_text(json.dumps(report, indent=1), encoding="utf-8")


def tag_of(repository: str) -> str:
    tags = run(["docker", "images", "--format", "{{.Repository}}:{{.Tag}}", repository]).stdout.split()
    pinned = [t for t in tags if t.split(":", 1)[1].startswith("6249129")]
    return (pinned or tags)[0]


def main() -> None:
    report: dict = {"started_at": time.time()}
    lock = open(LOCK, "a+")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        report["status"] = "lock_busy"
        write(report)
        return
    try:
        aircraft, ground = tag_of("drone-agent-m1-aircraft"), tag_of("drone-agent-m1-ground")
        report["images"] = {"aircraft": aircraft, "ground": ground}
        context = WORK / "ctx"
        context.mkdir(parents=True, exist_ok=True)
        for name, data in FILES.items():
            (WORK / name if name.endswith((".py", ".json")) and not name.startswith("x1_") else context / name
             ).write_bytes(base64.b64decode(data))
        (context / "Dockerfile").write_text(
            f"FROM {aircraft}\n"
            "COPY x1_setup.py /workspace/sim/x1_setup.py\nCOPY x1_sitl.sh /workspace/sim/x1_sitl.sh\n"
            "RUN no_proxy=mirrors.tuna.tsinghua.edu.cn apt-get "
            "-o Dir::Etc::sourcelist=/etc/apt/sources.list.d/ubuntu.sources -o Dir::Etc::sourceparts=- "
            "-o Acquire::Retries=1 -o Acquire::https::Timeout=20 update "
            "&& no_proxy=mirrors.tuna.tsinghua.edu.cn apt-get install -y --no-install-recommends bc "
            "&& rm -rf /var/lib/apt/lists/*\n"
            "RUN /usr/bin/python3 /workspace/sim/x1_setup.py\n"
            "ENV HEADLESS=1 PX4_SIM_SPEED_FACTOR=1 PX4_GZ_HEADLESS_RENDERING=1\n"
            'ENTRYPOINT ["bash", "/workspace/sim/x1_sitl.sh"]\n')
        built = run(["docker", "build", "--pull=false", "-t", IMAGE, str(context)], timeout=1500)
        (WORK / "build.log").write_text(built.stdout[-20000:] + built.stderr[-20000:])
        report["build"] = built.returncode
        if built.returncode:
            raise RuntimeError("probe image build failed")
        for folder in ("sitl", "ulog0", "ulog1", "sensor_a", "truth_a", "sensor_g", "truth_g", "frames"):
            (WORK / folder).mkdir(exist_ok=True)
            (WORK / folder).chmod(0o777)
        run(["docker", "rm", "-f", SITL, COL_A, COL_G])
        started = run(["docker", "run", "-d", "--name", SITL, "--network", "none", "--cpus", "2.5", "--memory", "3g",
                       "--pids-limit", "1024", "--init", "-e", "DRONE_X1_PARTNER_0=127.0.0.2",
                       "-e", "DRONE_X1_PARTNER_1=127.0.0.1", "-v", f"{WORK / 'sitl'}:/sitl-logs",
                       "-v", f"{WORK / 'ulog0'}:{BUILD}/rootfs/0/log", "-v", f"{WORK / 'ulog1'}:{BUILD}/rootfs/1/log",
                       IMAGE])
        report["sitl_start"] = {"returncode": started.returncode, "stderr": started.stderr[-400:]}
        homes = {}
        for second in range(240):
            for instance in (0, 1):
                log = WORK / "sitl" / f"px4-{instance}.log"
                if instance not in homes and log.exists() and "home set" in log.read_text(errors="replace"):
                    homes[instance] = second
            if len(homes) == 2 or run(["docker", "inspect", "-f", "{{.State.Running}}", SITL]).stdout.strip() != "true":
                break
            time.sleep(1)
        report["home_set_after_s"] = homes
        for name, model, topic, sensor, truth in ((COL_A, "x500_0", CAMERA_A, "sensor_a", "truth_a"),
                                                  (COL_G, "rover_differential_1", CAMERA_G, "sensor_g", "truth_g")):
            run(["docker", "run", "-d", "--name", name, "--network", f"container:{SITL}", "--init",
                 "-e", "PYTHONPATH=/usr/lib/python3/dist-packages", "-e", f"DRONE_TRUTH_MODEL={model}",
                 "-e", f"DRONE_CAMERA_TOPIC={topic}", "-v", f"{WORK / sensor}:/sensor", "-v", f"{WORK / truth}:/truth",
                 ground, "/usr/bin/python3", "/workspace/sim/collect.py"])
        topics = run(["docker", "exec", SITL, "gz", "topic", "-l"], timeout=60).stdout.split()
        report["image_topics"] = [t for t in topics if "image" in t or "camera" in t]
        poses = run(["docker", "exec", SITL, "gz", "topic", "-e", "-t", "/world/default/pose/info", "-n", "1"],
                    timeout=60).stdout
        report["pose_names"] = sorted(set(re.findall(r'name: "([^"]+)"', poses)))
        for second in range(60):
            if (WORK / "sensor_g/latest.json").exists() and (WORK / "sensor_a/latest.json").exists():
                break
            time.sleep(1)
        report["frames_after_s"] = second
        shutil.copy(WORK / "sensor_g/latest.json", WORK / "frames/rover_before.json") \
            if (WORK / "sensor_g/latest.json").exists() else None
        placed = {}
        for name, (x, y, emissive, lit) in BLOCKS.items():
            reply = run(["docker", "exec", SITL, "gz", "service", "-s", "/world/default/create", "--reqtype",
                         "gz.msgs.EntityFactory", "--reptype", "gz.msgs.Boolean", "--timeout", "5000", "--req",
                         f"sdf: '{block(name, x, y, 0.6, emissive, lit)}'"], timeout=60)
            placed[name] = reply.returncode == 0 and "data: true" in reply.stdout
        report["placed"] = placed
        time.sleep(4)
        for camera in ("g", "a"):
            source = WORK / f"sensor_{camera}/latest.json"
            if source.exists():
                shutil.copy(source, WORK / f"frames/{camera}_blocks.json")
        client = run(["docker", "run", "--rm", "--network", f"container:{SITL}", "-v", f"{WORK}:/probe",
                      "--entrypoint", "python3", aircraft, "/probe/world_client.py", "/probe/client.json"], timeout=400)
        report["client"] = {"returncode": client.returncode, "stdout": client.stdout[-3000:],
                            "stderr": client.stderr[-3000:]}
        time.sleep(3)
        run(["docker", "stop", "-t", "5", COL_A, COL_G], timeout=60)
        run(["docker", "stop", "-t", "20", SITL], timeout=90)
        logs = run(["docker", "logs", "--tail", "60", SITL])
        report["sitl_tail"] = (logs.stdout + logs.stderr)[-4000:]
        for instance in (0, 1):
            log = WORK / "sitl" / f"px4-{instance}.log"
            text = log.read_text(errors="replace") if log.exists() else ""
            report[f"px4_{instance}_notable"] = [line for line in text.splitlines()
                                                 if re.search(r"ERROR|WARN|home set|SIM_BAT|logger|Ready", line)][-40:]
        extracted = run(["docker", "run", "--rm", "--network", "none", "-v", f"{WORK}:/probe", "--entrypoint",
                         "/usr/bin/python3", ground, "/probe/ulog_extract.py", "/probe/ulog1", "/probe/ulog1.json"],
                        timeout=300)
        report["ulog_extract"] = {"returncode": extracted.returncode, "stderr": extracted.stderr[-1500:]}
        report["truth_rows"] = {k: sum(1 for _ in (WORK / f"truth_{k}/truth.jsonl").open())
                                if (WORK / f"truth_{k}/truth.jsonl").exists() else 0 for k in ("a", "g")}
        report["status"] = "done"
    except Exception as error:  # noqa: BLE001 - always clean up and report / 总是清理并报告
        report["status"] = f"error: {error!r}"[:400]
    finally:
        run(["docker", "rm", "-f", SITL, COL_A, COL_G])
        report["image_removed"] = run(["docker", "rmi", IMAGE]).returncode == 0
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()
        report["finished_at"] = time.time()
        write(report)


if __name__ == "__main__":
    main()
