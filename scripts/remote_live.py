"""Bounded cloud simulation jobs and an identity-bound operator mailbox.

有界云端仿真任务与身份绑定的操作者信箱。
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import runpy
import subprocess
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

HELPERS = runpy.run_path(str(Path(__file__).with_name("remote_dev_stack.py")))
RUN_ID = HELPERS["RUN_ID"]
OP_ID = re.compile(r"^[a-zA-Z0-9_-]{8,80}$")
SEEDS = (7, 19, 41)


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_json(path: Path):
    if path.is_symlink():
        raise ValueError("linked live artifact is forbidden")
    return json.loads(path.read_text()) if path.is_file() else None


def atomic_json(path: Path, value: dict) -> None:
    """Use a unique sibling before replacement so readers see complete objects.

    用唯一同目录临时文件替换，读者只能看到完整对象。
    """
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".pending")
    temporary.write_text(json.dumps(value))
    temporary.replace(path)


def rows(path: Path, limit: int = 1500) -> list[dict]:
    """Read bounded complete JSONL records without treating a partial append as an event.

    读取有界完整 JSONL 记录，不把写入中的半行当作事件。
    """
    if not path.is_file():
        return []
    if path.is_symlink():
        raise ValueError("linked live artifact is forbidden")
    with path.open("rb") as stream:
        size = path.stat().st_size
        start = max(0, size - 1024 * 1024)
        stream.seek(start)
        data = stream.read(1024 * 1024)
    lines = data.splitlines(keepends=True)
    if start and lines:
        lines.pop(0)
    return [json.loads(line) for line in lines[-limit:] if line.endswith(b"\n")]


def process_identity(pid: int) -> str | None:
    try:
        parts = Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()
        return parts[19] if parts[0] != "Z" else None
    except (OSError, IndexError):
        return None


def owned_job(root: Path, run_id: str) -> tuple[Path, dict]:
    if not isinstance(run_id, str) or not RUN_ID.fullmatch(run_id):
        raise ValueError("invalid live run identity")
    directory = root / "live" / run_id
    if (root / "live").is_symlink():
        raise ValueError("linked live directory is forbidden")
    if directory.is_symlink() or not directory.resolve().is_relative_to(root.resolve()):
        raise ValueError("invalid live directory")
    job = read_json(directory / "request.json")
    if not job or job.get("run_id") != run_id or not RUN_ID.fullmatch(job.get("deployment_id", "")):
        raise ValueError("live run not found")
    return directory, job


def start(root: Path, deployment: Path, request: dict) -> dict:
    """Pass the held project lock to a detached child; closing SSH cannot abort the flight.

    把已持有的项目锁传给独立后台进程；关闭 SSH 不会中断飞行。
    """
    import fcntl

    run_id, seed = request.get("run_id", ""), request.get("seed", 7)
    if not isinstance(run_id, str) or not RUN_ID.fullmatch(run_id) or type(seed) is not int or seed not in SEEDS:
        raise ValueError("invalid fixed simulation request")
    if set(request) - {"action", "run_id", "seed"}:
        raise ValueError("unsupported live start fields")
    directory = root / "live" / run_id
    if (root / "live").is_symlink():
        raise ValueError("linked live directory is forbidden")
    if directory.exists():
        _, previous = owned_job(root, run_id)
        if previous["seed"] != seed:
            raise ValueError("run identity reused with different input")
        return {"run_id": run_id, "status": "already_submitted"}
    lock = (root / "stack.lock").open("a")
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ValueError("cloud workspace is busy with another run or deployment") from error
        active = read_json(root / "live/active.json")
        if active:
            old_dir, _ = owned_job(root, active["run_id"])
            if not (old_dir / "result.json").is_file():
                raise ValueError("previous live run requires reconciliation")
        manifest = read_json(deployment / "manifest.json")
        directory.mkdir(parents=True, exist_ok=False)
        job = {
            "schema_version": "0.1.0", "run_id": run_id, "seed": seed,
            "deployment_id": deployment.name, "source_sha": manifest["source_sha"],
            "control_sha256": manifest["control_sha256"], "started_at": now(),
            "case": f"interactive-{seed}",
        }
        atomic_json(directory / "request.json", job)
        with (directory / "worker.log").open("wb") as log:
            process = subprocess.Popen(
                ["python3", str(deployment / "source/scripts/remote_live.py"),
                 "--run-id", run_id, "--lock-fd", str(lock.fileno())],
                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                start_new_session=True, pass_fds=(lock.fileno(),),
            )
        atomic_json(directory / "worker.json", {"pid": process.pid, "identity": process_identity(process.pid)})
        atomic_json(root / "live/active.json", {"run_id": run_id})
    finally:
        # Do not LOCK_UN: the child shares this open file description. / 不主动解锁：子进程共享同一打开文件描述。
        lock.close()
    return {"run_id": run_id, "status": "submitted"}


def live_records(root: Path, job: dict) -> tuple[Path, dict]:
    base = root / "artifacts" / job["deployment_id"] / ("m1-" + job["run_id"])
    case = base / job["case"]
    if case.is_symlink() or not case.resolve().is_relative_to(root.resolve()):
        raise ValueError("invalid live case path")
    return case, {
        "runtime": read_json(case / "aircraft/status.json"),
        "executive": rows(case / "aircraft/executive.jsonl"),
        "guardian": rows(case / "aircraft/guardian.jsonl"),
        "result": read_json(case / "aircraft/result.json"),
    }


def available_actions(record: dict) -> tuple[list[str], bool, dict | None]:
    state = record.get("runtime") or {}
    obs = state.get("observation") or {}
    fresh = bool(
        state and 0 <= time.monotonic() - state["monotonic"] < 0.75
        and obs.get("valid_until") and datetime.now(timezone.utc) < datetime.fromisoformat(obs["valid_until"])
    )
    step = next((e["data"] for e in reversed(record["executive"]) if e["kind"] == "skill_state"), None)
    if not fresh or not step or record["result"] or step["step_id"] != state.get("active_step"):
        return [], fresh, step
    if state["safety"] in {"recover", "abort"} or state.get("reason") == "landed_disarmed":
        return [], fresh, step
    current = step["state"]
    actions = ["cancel"] if current in {"preparing", "running", "paused", "pause_requested", "verifying"} else []
    if (
        current == "running" and state["safety"] == "proceed" and not state.get("command_pending")
        and state["active_step"] in {"fly_route", "return_home"} and obs.get("flight_mode") == "MISSION"
    ):
        actions.append("pause")
    if current == "paused" and state.get("reason") == "user_pause" and obs.get("flight_mode") == "HOLD":
        actions.append("resume")
    return actions, fresh, step


def mailbox_receipt(case: Path, record: dict) -> dict | None:
    pending = read_json(case / "aircraft/operator.json")
    if not pending:
        return None
    response = next(
        (e for e in reversed(record["executive"])
         if e["kind"] in {"operator_request", "operator_rejected"}
         and e["data"].get("request_id") == pending["request_id"]), None,
    )
    return {
        "request_id": pending["request_id"], "action": pending["action"],
        "status": ("accepted" if response["kind"] == "operator_request" else "rejected") if response else "pending",
        "reason": response["data"].get("reason") if response else None,
    }


def snapshot(root: Path, deployment: Path) -> dict:
    """Return recorded facts only; the console never decides mission success.

    只返回已记录事实；控制台不判定任务成功。
    """
    manifest = read_json(deployment / "manifest.json")
    result = {"schema_version": "0.1.0", "observed_at": now(), "source_sha": manifest["source_sha"],
              "deployment_id": deployment.name, "job": None, "allowed_actions": [], "fresh": False}
    active = read_json(root / "live/active.json")
    if not active:
        return result
    directory, job = owned_job(root, active["run_id"])
    case, record = live_records(root, job)
    completion = read_json(directory / "result.json")
    worker = read_json(directory / "worker.json") or {}
    alive = bool(worker.get("identity") and process_identity(worker["pid"]) == worker["identity"])
    actions, fresh, step = available_actions(record)
    mailbox = mailbox_receipt(case, record)
    if completion or not alive or (mailbox and mailbox["status"] == "pending") or deployment.name != job["deployment_id"]:
        actions = []
    phase = "running" if record["runtime"] else "preparing"
    if record["result"]:
        phase = "judging"
    if completion:
        phase = "finished" if completion.get("status") == "passed" else "failed"
    elif not alive:
        phase = "interrupted"
    result.update(
        job=job | {"phase": phase, "completion": completion}, allowed_actions=actions, fresh=fresh,
        runtime=record["runtime"], step=step, mission_result=record["result"],
        events=sorted(record["executive"] + record["guardian"], key=lambda e: e["timestamp"]),
        camera=read_json(case / "sensor/latest.json"), truth=rows(case / "truth/truth.jsonl"),
        scene=read_json(case / "input/scene.json"), operation=mailbox,
    )
    evidence = read_json(case / "aircraft/evidence-capture_image.json")
    if evidence and re.fullmatch(r"[0-9a-f]{64}", evidence.get("sha256", "")):
        media = case / "aircraft/images" / (evidence["sha256"] + ".rgb")
        if media.is_file() and not media.is_symlink():
            result["capture"] = {
                "timestamp": evidence["capture_timestamp"], "width": evidence["width"], "height": evidence["height"],
                "sha256": evidence["sha256"], "rgb": base64.b64encode(media.read_bytes()).decode(),
            }
    return result


def operate(root: Path, deployment: Path, request: dict) -> dict:
    """Serialize the mailbox and bind each operation to the observed mission and step.

    串行写入信箱，将每个操作绑定到所观察的任务及步骤。
    """
    import fcntl

    if set(request) != {"action", "run_id", "request_id", "operation", "mission_id", "step_id"}:
        raise ValueError("invalid operation fields")
    op_id, action = request["request_id"], request["operation"]
    if not isinstance(op_id, str) or not OP_ID.fullmatch(op_id) or action not in {"pause", "resume", "cancel"}:
        raise ValueError("unsupported operation")
    directory, job = owned_job(root, request["run_id"])
    active = read_json(root / "live/active.json")
    if not active or active["run_id"] != job["run_id"] or deployment.name != job["deployment_id"]:
        raise ValueError("live target changed")
    with (directory / "operator.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        recorded = directory / ("operation-" + op_id + ".json")
        if recorded.exists():
            previous = read_json(recorded)
            if previous["request"] != request:
                raise ValueError("operation identity reused with different input")
            return {"request_id": op_id, "status": "already_submitted"}
        view = snapshot(root, deployment)
        if action not in view["allowed_actions"]:
            raise ValueError("operation unavailable; refresh state before retrying")
        step = view["step"]
        if request["step_id"] != step["step_id"] or request["mission_id"] != step["mission_id"]:
            raise ValueError("observed mission or step changed")
        case, record = live_records(root, job)
        lease = next((e["data"] for e in reversed(record["guardian"]) if e["kind"] == "lease"), None)
        if not lease or lease["mission_id"] != request["mission_id"]:
            raise ValueError("current lease unavailable")
        envelope = {
            "request_id": op_id, "action": action, "mission_id": lease["mission_id"],
            "mission_version": lease["mission_version"], "lease_epoch": lease["lease_epoch"],
            "step_id": step["step_id"], "valid_until": (datetime.now(timezone.utc) + timedelta(seconds=5)).isoformat(),
        }
        atomic_json(recorded, {"request": request, "envelope": envelope, "submitted_at": now()})
        atomic_json(case / "aircraft/operator.json", envelope)
    return {"request_id": op_id, "status": "submitted"}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--lock-fd", type=int, required=True)
    args = parser.parse_args()
    root = HELPERS["workspace"]()
    directory, job = owned_job(root, args.run_id)
    deployment = root / "releases" / job["deployment_id"]
    # This descriptor keeps the project lock until all validation and restoration finish.
    # 此描述符维持项目锁，直到验证与空闲环境恢复全部结束。
    with os.fdopen(args.lock_fd, "a"):
        try:
            runner = runpy.run_path(str(deployment / "source/scripts/remote_m1.py"))["run_m1"]
            result = runner(root, deployment, {
                "run_id": job["run_id"], "scenario": "nominal", "seeds": [job["seed"]],
                "speed_factor": 1, "interactive": True,
            })
        except Exception as error:
            result = {"status": "failed", "source_sha": job["source_sha"], "error": type(error).__name__ + ": " + str(error)}
        atomic_json(directory / "result.json", result)


if __name__ == "__main__":
    main()
