"""Hold the existing project mutation lock around an isolated model-only batch.

在隔离的模型批次全过程持有既有项目变更锁。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

IMAGE = "sha256:4f84962ee6b75f899ee50afa153dca439942d9bf518b25d7c780c0b17c2b2377"


def write(path, value):
    """Retain receipts without overwriting. / 保留回执，不覆盖旧记录。"""
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, indent=2)
        stream.write("\n")


def try_lock(root: Path):
    """Use the same nonblocking flock as deployment and simulation; never steal it.

使用部署与仿真同一把非阻塞 flock，不抢占已有持有者。
"""
    import fcntl

    path = root / "stack.lock"
    if not path.is_file() or path.is_symlink():
        raise ValueError("existing project lock required")
    stream = path.open("r")
    try:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        stream.close()
        return None
    except BaseException:
        stream.close()
        raise
    return stream


def identities() -> list[dict]:
    """Record only public lifecycle fields, never container environment variables.

只记录容器生命周期字段，不记录环境变量。
"""
    ids = subprocess.check_output(["docker", "ps", "--filter", "name=drone-agent-cloud", "--format", "{{.ID}}"]).decode().split()
    if not ids:
        return []
    details = json.loads(subprocess.check_output(["docker", "inspect", *ids]))
    return sorted([{"name": d["Name"].lstrip("/"), "id": d["Id"], "image": d["Image"],
                    "started_at": d["State"]["StartedAt"], "restart_count": d["RestartCount"]} for d in details], key=lambda d: d["name"])


def preflight() -> dict:
    """Apply existing workspace capacity minima before provider invocation.

模型调用前应用既有工作区容量下限。
"""
    network = json.loads(subprocess.check_output(["docker", "network", "inspect", "drone-agent-cloud_desk_model"]))[0]
    if network["Internal"] is not True:
        raise ValueError("model network must remain internal")
    subprocess.run(["docker", "image", "inspect", IMAGE], check=True, capture_output=True)
    available = next(int(line.split()[1]) * 1024 for line in Path("/proc/meminfo").read_text().splitlines() if line.startswith("MemAvailable:"))
    disk = shutil.disk_usage(Path.home() / "drone-agent").free
    if available < 3 * 1024**3 or disk < 8 * 1024**3:
        raise ValueError("workspace capacity below existing minima")
    return {"image": IMAGE, "model_network_internal": True, "memory_available_bytes": available, "disk_free_bytes": disk}


def run_container(root: Path, folder: Path, name: str) -> int:
    """Mount only model inputs, source helpers, output and the existing read-only key.

仅挂载模型输入、辅助源码、输出及已有只读密钥。
"""
    plan = json.loads((folder / "plan.json").read_text())
    (folder / "output").mkdir(exist_ok=False)
    args = ["docker", "run", "--rm", "--pull", "never", "--name", name, "--read-only", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges:true", "--memory", "1g", "--cpus", "1", "--pids-limit", "64",
            "--tmpfs", "/tmp:size=64m,mode=1777", "--user", f"{os.getuid()}:{os.getgid()}", "--network", "drone-agent-cloud_desk_model"]
    env = {"DRONE_SOURCE_SHA": plan["expected_runtime"]["runtime_source_sha"], "VISION_PROVIDER": "minimax-vl",
           "MINIMAX_API_KEY_FILE": "/model/minimax.key", "HTTPS_PROXY": "http://desk-model-proxy:3128",
           "NO_PROXY": "localhost,127.0.0.1", "LLM_HTTP_READ_CAP_S": "600", "REQUIRE_REAL_PROVIDERS": "on",
           "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUNBUFFERED": "1"}
    for key, value in env.items():
        args += ["-e", key + "=" + value]
    mounts = [(folder / "input", "/input:ro"), (folder / "config.json", "/config.json:ro"),
              (folder / "plan.json", "/plan.json:ro"), (folder / "p6_candidate_review.py", "/experiment/p6_candidate_review.py:ro"),
              (folder / "p6_candidate_runner.py", "/experiment/p6_candidate_runner.py:ro"), (folder / "output", "/output"),
              (root / "secrets/m2-model/minimax.key", "/model/minimax.key:ro")]
    for src, dst in mounts:
        args += ["-v", str(src) + ":" + dst]
    args += [IMAGE, "python3", "/experiment/p6_candidate_runner.py"]
    try:
        with (folder / "diagnostic.log").open("xb") as stream:
            return subprocess.run(args, stdout=stream, stderr=subprocess.STDOUT, timeout=1800).returncode
    except subprocess.TimeoutExpired:
        # Only this newly named diagnostic may be stopped. / 只停止本次新命名的诊断容器。
        subprocess.run(["docker", "stop", "--time", "10", name], capture_output=True, timeout=30)
        raise


def execute(root: Path, folder: Path) -> dict:
    """Busy means zero calls; only a held lock permits preflight and model execution.

锁忙意味着零调用；只有持锁才进入预检及模型执行。
"""
    started = datetime.now(UTC).isoformat()
    lock = try_lock(root)
    if lock is None:
        receipt = {"status": "blocked_lock_busy", "project_lock_acquired": False, "container_started": False,
                   "provider_invocations": 0, "started_at": started, "formal_release_eligible": False}
        write(folder / "execution.json", receipt)
        return receipt
    receipt = {"status": "failed_preflight", "project_lock_acquired": True, "container_started": False,
               "provider_invocations": 0, "started_at": started, "formal_release_eligible": False}
    try:
        receipt["preflight"] = preflight()
        receipt["before"] = identities()
        if not receipt["before"]:
            raise ValueError("resident container inventory missing")
        write(folder / "precall.json", {"started_at": datetime.now(UTC).isoformat(), "project_lock_acquired": True,
              "config_sha256": hashlib.sha256((folder / "config.json").read_bytes()).hexdigest(),
              "manifest_sha256": hashlib.sha256((folder / "input/manifest.json").read_bytes()).hexdigest()})
        receipt["container_started"] = True
        receipt["provider_invocations"] = None
        receipt["container_exit_code"] = run_container(root, folder, folder.name)
        receipt["after"] = identities()
        receipt["resident_container_identities_unchanged"] = receipt["before"] == receipt["after"]
        header = folder / "output/run.json"
        if header.exists():
            receipt["provider_invocations"] = json.loads(header.read_text()).get("cases_attempted")
        receipt["status"] = "completed" if receipt["container_exit_code"] == 0 and receipt["resident_container_identities_unchanged"] else "not_qualified_execution_or_environment"
    except Exception as exc:
        receipt["status"] = "failed_execution" if receipt["container_started"] else "failed_preflight"
        receipt["error_type"] = type(exc).__name__
    finally:
        receipt["finished_at"] = datetime.now(UTC).isoformat()
        receipt["lock_held_through_checks"] = "after" in receipt
        try:
            write(folder / "execution.json", receipt)
        finally:
            lock.close()
    return receipt


def main() -> None:
    """Require an owned project and a frozen artifact directory. / 要求已有项目所有权及冻结产物目录。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    args = parser.parse_args()
    root, folder = args.workspace.resolve(), args.run_dir.resolve()
    if (args.workspace.is_symlink() or args.run_dir.is_symlink() or root != (Path.home() / "drone-agent").resolve()
            or json.loads((root / "owner.json").read_text()) != {"owner": "drone-agent-cloud/v1", "uid": os.getuid()}):
        raise ValueError("workspace ownership mismatch")
    if not folder.is_relative_to(root / "artifacts") or re.fullmatch(r"p6-candidate-review-dev-\d{8}T\d{6}Z", folder.name) is None:
        raise ValueError("invalid diagnostic directory")
    plan = json.loads((folder / "plan.json").read_text())
    for name, key in [("p6_candidate_review.py", "contract_sha256"), ("p6_candidate_runner.py", "runner_sha256"),
                      ("p6_candidate_remote.py", "remote_worker_sha256"), ("config.json", "config_sha256"), ("input/manifest.json", "manifest_sha256")]:
        if hashlib.sha256((folder / name).read_bytes()).hexdigest() != plan[key]:
            raise ValueError("frozen source or input mismatch")
    receipt = execute(root, folder)
    print(json.dumps(receipt), flush=True)


if __name__ == "__main__":
    main()
