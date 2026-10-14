"""Host side of the X1 rover probe (D088): take the project lock, run the rover SITL and the probe client, collect,
clean up. Started with nohup so an SSH drop never leaves containers behind; the lock is taken without blocking.

X1 地面车探针的主机侧（D088）：取项目锁，运行地面车 SITL 与探针客户端，收集结果并清理。用 nohup 启动，SSH 断开也不会
留下容器；项目锁以非阻塞方式获取。
"""

import fcntl
import json
import subprocess
import sys
import time
from pathlib import Path

HOME = Path.home()
WORK = HOME / "drone-agent/scratch/x1probe"
LOCK = HOME / "drone-agent/stack.lock"
SITL_IMAGE = "drone-agent-sitl:px4-1.17.0-m0"
SITL, CLIENT = "x1probe-sitl", "x1probe-client"
# Instance 0 starts the default (empty) world itself, as p3_sitl.sh does; nothing in PX4 is patched.
# 实例 0 自行启动默认（空）世界，与 p3_sitl.sh 相同；PX4 不做任何修改。
SITL_CMD = ("BUILD=/opt/PX4-Autopilot/build/px4_sitl_default; mkdir -p $BUILD/rootfs/0 && cd $BUILD/rootfs/0 && "
            "export HEADLESS=1 PX4_GZ_WORLD=default PX4_SIM_MODEL=gz_rover_differential && exec $BUILD/bin/px4 -i 0 -d")


def run(cmd, timeout=120):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as error:
        return subprocess.CompletedProcess(cmd, 124, error.stdout or "", f"timeout: {error}")


def write(report: dict) -> None:
    (WORK / "report.json").write_text(json.dumps(report, indent=1), encoding="utf-8")


def main() -> None:
    client_image = sys.argv[1]
    report: dict = {"started_at": time.time(), "sitl_image": SITL_IMAGE, "client_image": client_image}
    lock = open(LOCK, "a+")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        report["status"] = "lock_busy"
        write(report)
        return
    try:
        run(["docker", "rm", "-f", SITL, CLIENT])
        started = run(["docker", "run", "-d", "--name", SITL, "--network", "none", "--cpus", "2", "--memory", "3g",
                       "--pids-limit", "512", "--entrypoint", "bash", SITL_IMAGE, "-lc", SITL_CMD])
        report["sitl_start"] = {"returncode": started.returncode, "stderr": started.stderr[-300:]}
        ready = None
        for second in range(150):
            logs = run(["docker", "logs", SITL])
            text = logs.stdout + logs.stderr
            if "Startup script returned successfully" in text or "Ready for takeoff" in text:
                ready = second
                break
            if run(["docker", "inspect", "-f", "{{.State.Running}}", SITL]).stdout.strip() != "true":
                break
            time.sleep(1)
        report["sitl_ready_after_s"] = ready
        time.sleep(5)
        WORK.chmod(0o777)
        client = run(["docker", "run", "--rm", "--name", CLIENT, "--network", f"container:{SITL}",
                      "-v", f"{WORK}:/probe", "--entrypoint", "python3", client_image, "/probe/rover_probe.py",
                      "--out", "/probe/result.json"], timeout=900)
        report["client"] = {"returncode": client.returncode, "stdout_tail": client.stdout[-6000:],
                            "stderr_tail": client.stderr[-3000:]}
        logs = run(["docker", "logs", "--tail", "250", SITL])
        report["px4_log_tail"] = (logs.stdout + logs.stderr)[-8000:]
        report["status"] = "done"
    except Exception as error:  # noqa: BLE001 - always clean up and report / 总是清理并报告
        report["status"] = f"error: {error!r}"[:300]
    finally:
        run(["docker", "rm", "-f", SITL, CLIENT])
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()
        report["finished_at"] = time.time()
        write(report)


if __name__ == "__main__":
    main()
