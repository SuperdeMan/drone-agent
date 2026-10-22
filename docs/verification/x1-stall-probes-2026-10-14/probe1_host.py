"""Host side of the X1 rover stall probe (D088, S1 diagnosis); started with nohup, takes the project lock without
blocking, uses the current deployment's X1 simulation and aircraft images, and removes its containers. Not a gate.

X1 地面车停滞探针的主机侧（D088，S1 诊断）；用 nohup 启动，以非阻塞方式获取项目锁，使用当前部署的 X1 仿真与机载镜像，
结束后删除其容器。不是门禁。
"""

import base64
import fcntl
import json
import subprocess
import time
from pathlib import Path

HOME = Path.home()
WORK = HOME / "drone-agent/scratch/x1stallprobe"
LOCK = HOME / "drone-agent/stack.lock"
SITL, CLIENT = "x1stall-sitl", "x1stall-client"
CLIENT_CODE = ""  # filled in by the launcher / 由启动脚本填入
ROUNDS = "30"


def run(cmd, timeout=120):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as error:
        return subprocess.CompletedProcess(cmd, 124, str(error.stdout or ""), f"timeout: {error}")


def image(repository: str, tag_prefix: str) -> str:
    tags = run(["docker", "images", "--format", "{{.Repository}}:{{.Tag}}", repository]).stdout.split()
    return next(t for t in tags if t.split(":", 1)[1].startswith(tag_prefix))


def main() -> None:
    report: dict = {"started_at": time.time()}
    lock = open(LOCK, "a+")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        report["status"] = "lock_busy"
        (WORK / "report.json").write_text(json.dumps(report))
        return
    try:
        current = json.loads((HOME / "drone-agent/current.json").read_text())
        sha = current["source_sha"] if "source_sha" in current else current.get("deployment", {}).get("source_sha", "")
        prefix = sha or "2710869"
        sim, aircraft = image("drone-agent-x1-sim", prefix[:7]), image("drone-agent-m1-aircraft", prefix[:7])
        report["images"] = {"sim": sim, "aircraft": aircraft}
        (WORK / "client.py").write_bytes(base64.b64decode(CLIENT_CODE))
        for folder in ("sitl",):
            (WORK / folder).mkdir(exist_ok=True)
            (WORK / folder).chmod(0o777)
        WORK.chmod(0o777)
        run(["docker", "rm", "-f", SITL, CLIENT])
        started = run(["docker", "run", "-d", "--name", SITL, "--network", "none", "--cpus", "2.5", "--memory", "3g",
                       "--pids-limit", "1024", "--init", "-e", "DRONE_X1_PARTNER_0=127.0.0.2",
                       "-e", "DRONE_X1_PARTNER_1=127.0.0.1", "-v", f"{WORK / 'sitl'}:/sitl-logs", sim])
        report["sitl_start"] = started.returncode
        homes = {}
        for second in range(240):
            for instance in (0, 1):
                log = WORK / "sitl" / f"px4-{instance}.log"
                if instance not in homes and log.exists() and "home set" in log.read_text(errors="replace"):
                    homes[instance] = second
            if len(homes) == 2:
                break
            time.sleep(1)
        report["home_set_after_s"] = homes
        time.sleep(5)
        client = run(["docker", "run", "--rm", "--name", CLIENT, "--network", f"container:{SITL}", "-v",
                      f"{WORK}:/probe", "--entrypoint", "python3", aircraft, "/probe/client.py", "/probe/result.json",
                      ROUNDS], timeout=3200)
        report["client"] = {"returncode": client.returncode, "stdout_tail": client.stdout[-4000:],
                            "stderr_tail": client.stderr[-2000:]}
        log = WORK / "sitl" / "px4-1.log"
        report["px4_1_tail"] = log.read_text(errors="replace")[-3000:] if log.exists() else None
        report["status"] = "done"
    except Exception as error:  # noqa: BLE001 - always clean up and report / 总是清理并报告
        report["status"] = f"error: {error!r}"[:400]
    finally:
        run(["docker", "rm", "-f", SITL, CLIENT])
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()
        report["finished_at"] = time.time()
        (WORK / "report.json").write_text(json.dumps(report, indent=1))


if __name__ == "__main__":
    main()
