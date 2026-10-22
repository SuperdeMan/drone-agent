"""Hold the established project lock for a frozen, model-only road evaluation.

为冻结的纯模型道路评测持有既有项目锁。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import runpy
import subprocess
from datetime import UTC, datetime
from pathlib import Path

SHARED = runpy.run_path(str(Path(__file__).with_name("p6_candidate_remote.py")))


def digest(path: Path) -> str:
    """Hash exact source or input bytes. / 核对确切源码或输入字节。"""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write(path: Path, value: dict) -> None:
    """Preserve each final receipt once. / 最终回执只写一次并保留。"""
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, indent=2)
        stream.write("\n")


def envelope(root: Path, folder: Path) -> dict:
    """Reject foreign workspaces, mutable plans and unbound source helpers.

拒绝其他工作区、变化的计划及未绑定的辅助源码。
"""
    if (root != (Path.home() / "drone-agent").resolve() or root.is_symlink() or folder.is_symlink()
            or not folder.is_relative_to(root / "artifacts") or re.fullmatch(r"p6-road-s2-\d{8}T\d{6}Z", folder.name) is None
            or json.loads((root / "owner.json").read_text()) != {"owner": "drone-agent-cloud/v1", "uid": os.getuid()}):
        raise ValueError("owned project artifact directory required")
    plan = json.loads((folder / "execution-plan.json").read_text())
    expected = {"p6_road_s2.py", "p6_business_eval.py", "p6_boundary_drafts.py", "p6_road_s2_runner.py", "p6_road_s2_remote.py", "p6_candidate_remote.py"}
    if (plan.get("format") != "drone.p6-road-cloud-plan/v1" or plan.get("run_id") != folder.name
            or set(plan.get("source_sha256", {})) != expected or plan.get("runtime_image") != SHARED["IMAGE"]
            or re.fullmatch(r"[0-9a-f]{40}", plan.get("eval_source_commit", "")) is None
            or plan.get("runtime_source_sha") != "6249129596b7cfe88c7322f8b425c7b432dba24b"):
        raise ValueError("invalid frozen execution envelope")
    for name, expected_hash in plan["source_sha256"].items():
        if digest(folder / name) != expected_hash:
            raise ValueError("frozen source changed")
    if digest(folder / "input/plan.json") != plan["data_plan_sha256"]:
        raise ValueError("frozen data plan changed")
    if type(plan.get("maximum_wall_seconds")) is not int or not 60 <= plan["maximum_wall_seconds"] <= 86400:
        raise ValueError("execution wall budget invalid")
    return plan


def container_command(root: Path, folder: Path, plan: dict) -> list[str]:
    """Mount only public model inputs, helpers, outputs and the existing read-only key.

仅挂载公开模型输入、辅助源码、输出及已有只读密钥。
"""
    args = ["docker", "run", "--rm", "--pull", "never", "--name", folder.name, "--read-only", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges:true", "--memory", "1g", "--cpus", "1", "--pids-limit", "64",
            "--tmpfs", "/tmp:size=64m,mode=1777", "--user", f"{os.getuid()}:{os.getgid()}", "--network", "drone-agent-cloud_desk_model"]
    env = {"DRONE_P6_ROAD_CLOUD": "v1", "DRONE_SOURCE_SHA": plan["runtime_source_sha"], "DRONE_EVAL_SOURCE_SHA": plan["eval_source_commit"],
           "VISION_PROVIDER": "minimax-vl", "MINIMAX_API_KEY_FILE": "/model/minimax.key", "HTTPS_PROXY": "http://desk-model-proxy:3128",
           "NO_PROXY": "localhost,127.0.0.1", "REQUIRE_REAL_PROVIDERS": "on", "LLM_HTTP_READ_CAP_S": "600",
           "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUNBUFFERED": "1"}
    for key, value in env.items():
        args += ["-e", key + "=" + value]
    mounts = [(folder / "input", "/input:ro"), (folder / "output", "/output"),
              (root / "secrets/m2-model/minimax.key", "/model/minimax.key:ro")]
    for name in ("p6_road_s2.py", "p6_business_eval.py", "p6_boundary_drafts.py", "p6_road_s2_runner.py"):
        mounts.append((folder / name, "/experiment/" + name + ":ro"))
    for source, destination in mounts:
        args += ["-v", str(source) + ":" + destination]
    return args + [plan["runtime_image"], "python3", "/experiment/p6_road_s2_runner.py", "--package", "/input",
                   "--output", "/output/batch", "--plan-sha256", plan["data_plan_sha256"]]


def reserve_test_scope(root: Path, folder: Path, plan: dict) -> dict:
    """Prevent another path or profile from reusing exposed test images and groups.

防止换目录或画像后重复使用已曝光的测试影像和关联组。
"""
    data = json.loads((folder / "input/plan.json").read_text())
    if data["split"] != "test":
        return {"required": False}
    keys = data.get("test_exposure_keys")
    if (not isinstance(keys, list) or not keys or len(keys) > 20000 or len(keys) != len(set(keys))
            or any(not isinstance(k, str) or re.fullmatch(r"(?:image|group)_[0-9a-f]{64}", k) is None for k in keys)):
        raise ValueError("formal test exposure identities missing")
    directory = root / "data/p6-road-test-exposure-v1"
    if not directory.resolve().is_relative_to(root.resolve()) or directory.is_symlink():
        raise ValueError("exposure registry leaves owned workspace")
    directory.mkdir(parents=True, exist_ok=True)
    if any((directory / (key + ".json")).exists() for key in keys):
        raise ValueError("test image or related group already reserved or exposed; do not repeat")
    claim = {"format": "drone.p6-road-test-exposure/v1", "run_id": folder.name, "eval_source_commit": plan["eval_source_commit"],
             "data_plan_sha256": plan["data_plan_sha256"], "state": "reserved_before_execution_do_not_repeat",
             "image_and_group_keys": keys, "created_at": datetime.now(UTC).isoformat()}
    for key in keys:
        write(directory / (key + ".json"), {"run_id": folder.name, "data_plan_sha256": plan["data_plan_sha256"], "state": claim["state"]})
    write(folder / "test-exposure.json", claim)
    return {"required": True, "reserved_keys": len(keys), "claim_sha256": digest(folder / "test-exposure.json"),
            "state": claim["state"]}


def execute(root: Path, folder: Path, plan: dict) -> dict:
    """Keep zero-call busy attempts and all execution/environment failures explicit.

明确保留锁忙零调用及全部执行 / 环境失败。
"""
    receipt = {"format": "drone.p6-road-cloud-execution/v1", "status": "blocked_lock_busy", "run_id": folder.name,
               "data_plan_sha256": plan["data_plan_sha256"], "execution_plan_sha256": digest(folder / "execution-plan.json"),
               "eval_source_commit": plan["eval_source_commit"], "runtime_source_sha": plan["runtime_source_sha"], "runtime_image": plan["runtime_image"],
               "started_at": datetime.now(UTC).isoformat(), "project_lock_acquired": False, "container_started": False,
               "attempts_started": 0, "responses_recorded": 0, "formal_release_eligible": False}
    if any((folder / name).exists() for name in ("execution.json", "precall.json", "output")):
        raise ValueError("an existing or ambiguous execution cannot be repeated")
    lock = SHARED["try_lock"](root)
    if lock is None:
        write(folder / "execution.json", receipt)
        return receipt
    receipt.update(project_lock_acquired=True, status="failed_preflight")
    try:
        receipt["preflight"] = SHARED["preflight"]()
        receipt["before"] = SHARED["identities"]()
        if not receipt["before"] or receipt["preflight"]["image"] != plan["runtime_image"]:
            raise ValueError("resident inventory or immutable image missing")
        receipt["test_exposure"] = reserve_test_scope(root, folder, plan)
        (folder / "output").mkdir(exist_ok=False)
        write(folder / "precall.json", {"project_lock_acquired": True, "started_at": datetime.now(UTC).isoformat(),
                                      "data_plan_sha256": plan["data_plan_sha256"]})
        receipt.update(container_started=True, attempts_started=None, responses_recorded=None)
        with (folder / "model.log").open("xb") as log:
            try:
                result = subprocess.run(container_command(root, folder, plan), stdout=log, stderr=subprocess.STDOUT, timeout=plan["maximum_wall_seconds"])
                receipt["container_exit_code"] = result.returncode
            except subprocess.TimeoutExpired:
                subprocess.run(["docker", "stop", "--time", "10", folder.name], capture_output=True, timeout=30)
                raise
        header = folder / "output/batch/run.json"
        if header.is_file():
            run = json.loads(header.read_text())
            receipt.update(attempts_started=run.get("attempts_started"), responses_recorded=run.get("responses_recorded"))
        receipt["status"] = "completed" if receipt["container_exit_code"] == 0 else "failed_execution"
    except Exception as error:
        receipt.update(status="failed_execution" if receipt["container_started"] else "failed_preflight", error_type=type(error).__name__)
    finally:
        try:
            if receipt.get("before"):
                receipt["after"] = SHARED["identities"]()
                receipt["resident_container_identities_unchanged"] = receipt["before"] == receipt["after"]
                if not receipt["resident_container_identities_unchanged"]:
                    receipt["status"] = "not_qualified_environment_changed"
            receipt["lock_held_through_checks"] = "after" in receipt
        except Exception as error:
            receipt.update(status="not_qualified_environment_check_failed", postcheck_error_type=type(error).__name__, lock_held_through_checks=False)
        receipt["finished_at"] = datetime.now(UTC).isoformat()
        try:
            write(folder / "execution.json", receipt)
        finally:
            lock.close()
    return receipt


def main() -> None:
    """Execute only a validated project-owned frozen envelope. / 只执行已核对且属于本项目的冻结外壳。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    args = parser.parse_args()
    if args.workspace.is_symlink() or args.run_dir.is_symlink():
        raise ValueError("symlink workspace or run directory rejected")
    root, folder = args.workspace.resolve(), args.run_dir.resolve()
    plan = envelope(root, folder)
    print(json.dumps(execute(root, folder, plan)), flush=True)


if __name__ == "__main__":
    main()
