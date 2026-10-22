"""Host side of the X1 rover stall probe 3 (D088, S1 diagnosis): a freshly booted X1 world per trial, one route each;
started with nohup, takes the project lock without blocking, removes its containers. Not a gate.

X1 地面车停滞探针 3 的主机侧（D088，S1 诊断）：每次试验新启动 X1 世界、执行一条航线；用 nohup 启动，以非阻塞方式获取项目锁，
结束后删除其容器。不是门禁。
"""

import base64
import fcntl
import json
import subprocess
import time
from pathlib import Path

HOME = Path.home()
WORK = HOME / "drone-agent/scratch/x1stallprobe3"
LOCK = HOME / "drone-agent/stack.lock"
SITL, CLIENT = "x1stall3-sitl", "x1stall3-client"
CLIENT_CODE = ""  # filled in by the launcher / 由启动脚本填入
TRIALS = ["A", "B", "C", "A", "B", "C", "A", "C", "A", "C"]


def run(cmd, timeout=120):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as error:
        return subprocess.CompletedProcess(cmd, 124, str(error.stdout or ""), f"timeout: {error}")


def image(repository: str) -> str:
    tags = run(["docker", "images", "--format", "{{.Repository}}:{{.Tag}}", repository]).stdout.split()
    return next(t for t in tags if t.split(":", 1)[1].startswith("2710869"))


def main() -> None:
    report: dict = {"started_at": time.time(), "rows": []}
    lock = open(LOCK, "a+")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        report["status"] = "lock_busy"
        (WORK / "report.json").write_text(json.dumps(report))
        return
    try:
        sim, aircraft = image("drone-agent-x1-sim"), image("drone-agent-m1-aircraft")
        (WORK / "client.py").write_bytes(base64.b64decode(CLIENT_CODE))
        WORK.chmod(0o777)
        for trial, variant in enumerate(TRIALS):
            logs = WORK / f"sitl-{trial}"
            logs.mkdir(exist_ok=True)
            logs.chmod(0o777)
            run(["docker", "rm", "-f", SITL, CLIENT])
            run(["docker", "run", "-d", "--name", SITL, "--network", "none", "--cpus", "2.5", "--memory", "3g",
                 "--pids-limit", "1024", "--init", "-e", "DRONE_X1_PARTNER_0=127.0.0.2",
                 "-e", "DRONE_X1_PARTNER_1=127.0.0.1", "-v", f"{logs}:/sitl-logs", sim])
            for _ in range(240):
                log = logs / "px4-1.log"
                if log.exists() and "home set" in log.read_text(errors="replace"):
                    break
                time.sleep(1)
            time.sleep(5)
            client = run(["docker", "run", "--rm", "--name", CLIENT, "--network", f"container:{SITL}", "-v",
                          f"{WORK}:/probe", "--entrypoint", "python3", aircraft, "/probe/client.py", variant],
                         timeout=200)
            line = next((ln for ln in client.stdout.splitlines() if ln.startswith("ROW ")), None)
            row = json.loads(line[4:]) if line else {"variant": variant, "error": client.stderr[-400:]}
            row["trial"] = trial
            report["rows"].append(row)
            (WORK / "report.json").write_text(json.dumps(report, indent=1))
            run(["docker", "rm", "-f", SITL, CLIENT])
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
