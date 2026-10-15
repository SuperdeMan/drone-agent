"""Launch or fetch a committed road S2 package without modifying resident services.

启动或取回已提交的道路 S2 包，不修改常驻服务。
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import tarfile
from datetime import UTC, datetime
from pathlib import Path

from dev_stack import Connection
from p6_road_s2 import digest, input_file, read, verify_package, write

REPO = Path(__file__).resolve().parents[1]
SOURCES = ("p6_road_s2.py", "p6_business_eval.py", "p6_boundary_drafts.py", "p6_road_s2_runner.py", "p6_road_s2_remote.py", "p6_candidate_remote.py")
IMAGE = "sha256:4f84962ee6b75f899ee50afa153dca439942d9bf518b25d7c780c0b17c2b2377"
RUNTIME = "6249129596b7cfe88c7322f8b425c7b432dba24b"


def ssh(connection, code: str, timeout: int = 60) -> dict:
    """Return only structured output; SSH banners never enter evidence logs.

只返回结构化结果，SSH 横幅不进入证据日志。
"""
    result = subprocess.run(["ssh", *connection.arguments(), connection.target, "python3 -"], input=code.encode(), capture_output=True, timeout=timeout)
    try:
        value = json.loads(result.stdout)
    except (ValueError, UnicodeError) as error:
        raise RuntimeError(f"SSH transport returned no structured result (exit {result.returncode}); stderr withheld") from error
    if result.returncode:
        raise RuntimeError("remote operation failed; reconcile retained artifacts before retry")
    return value


def remote_path(ident: str) -> str:
    """Restrict operations to this workflow's owned artifact names. / 将操作限定于本流程的归档名称。"""
    if re.fullmatch(r"p6-road-s2-\d{8}T\d{6}Z", ident) is None:
        raise ValueError("invalid road run id")
    return "/home/ubuntu/drone-agent/artifacts/" + ident


def committed_bytes(commit: str, path: str) -> bytes:
    """Read exact committed evidence without a shell-built Git command.

读取确切提交证据，不拼接 shell Git 命令。
"""
    return subprocess.check_output(["git", "show", commit + ":" + path], cwd=REPO, stderr=subprocess.PIPE)


def validate_freeze(package: Path, commit: str, registry: str) -> tuple[dict, dict, dict]:
    """Bind code and all formal source metadata to the same reviewed Git freeze.

将代码和全部正式源元数据绑定到同一可审查 Git 冻结。
"""
    if re.fullmatch(r"[0-9a-f]{40}", commit) is None or re.fullmatch(r"eval/s2/road_business_v[0-9]+/frozen/[A-Za-z0-9_-]+", registry) is None:
        raise ValueError("exact commit and controlled frozen registry required")
    profile, inputs, plan = verify_package(package)
    if any(r["task_origin"] == "qa_fixture" for r in inputs["rows"]):
        raise ValueError("QA package cannot be launched against a provider")
    subprocess.run(["git", "merge-base", "--is-ancestor", commit, "HEAD"], cwd=REPO, check=True, capture_output=True)
    for name in (*SOURCES, "p6_road_s2_cloud.py"):
        if (REPO / "scripts" / name).read_bytes() != committed_bytes(commit, "scripts/" + name):
            raise ValueError("cloud source differs from freeze")
    for name in ("profile.json", "inputs.json", "plan.json"):
        if (package / name).read_bytes() != committed_bytes(commit, registry + "/" + name):
            raise ValueError("package metadata differs from freeze")
    bound = {"source-manifest.json": plan["source_manifest_sha256"], "tasks.json": plan["tasks_sha256"]}
    if plan["split"] != "development":
        bound.update({"labels.json": plan["data_admission"]["labels_sha256"], "protocol.json": plan["data_admission"]["protocol_sha256"],
                      "exposure.json": plan["data_admission"]["exposure_sha256"]})
    if plan["split"] == "test":
        bound["calibration-selection.json"] = profile["calibration_record_sha256"]
    import hashlib

    for name, expected in bound.items():
        if hashlib.sha256(committed_bytes(commit, registry + "/" + name)).hexdigest() != expected:
            raise ValueError("source metadata differs from frozen admission")
    attempts = package / "attempts"
    if attempts.exists():
        for previous in attempts.iterdir():
            receipt = previous / "cloud/execution.json"
            if not receipt.is_file():
                raise ValueError("prior attempt needs reconciliation; no automatic duplicate call")
            old = read(receipt)
            if not (old.get("format") == "drone.p6-road-cloud-execution/v1" and old.get("status") == "blocked_lock_busy"
                    and old.get("run_id") == previous.name and old.get("data_plan_sha256") == digest(package / "plan.json")
                    and old.get("execution_plan_sha256") == digest(previous / "execution-plan.json")
                    and old.get("project_lock_acquired") is False and old.get("attempts_started") == 0
                    and old.get("responses_recorded") == 0 and old.get("container_started") is False):
                raise ValueError("prior attempt may have invoked the model; do not repeat")
    return profile, inputs, plan


def launch(package: Path, commit: str, registry: str, *, apply: bool = False) -> dict:
    """Upload a frozen packet and detach a bounded worker, never a resident service.

上传冻结包并启动有界后台执行器，不创建常驻服务。
"""
    profile, inputs, plan = validate_freeze(package, commit, registry)
    if not apply:
        return {"action": "launch_road_s2", "planned_calls": plan["planned_calls"], "split": plan["split"], "freeze": commit,
                "runtime_image": IMAGE, "applied": False, "formal_release_eligible": False}
    ident = "p6-road-s2-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    remote = remote_path(ident)
    attempt = package / "attempts" / ident
    attempt.mkdir(parents=True, exist_ok=False)
    execution_plan = {"format": "drone.p6-road-cloud-plan/v1", "run_id": ident, "eval_source_commit": commit,
                      "registry": registry, "runtime_source_sha": RUNTIME, "runtime_image": IMAGE,
                      "data_plan_sha256": digest(package / "plan.json"), "source_sha256": {name: digest(REPO / "scripts" / name) for name in SOURCES},
                      "maximum_wall_seconds": min(86400, int(plan["planned_calls"] * (profile["timeout_s"] + profile["minimum_start_interval_s"] + 30) + 120))}
    write(attempt / "execution-plan.json", execution_plan)
    packet = attempt / "packet.tar"
    with tarfile.open(packet, "x") as archive:
        archive.add(attempt / "execution-plan.json", arcname="execution-plan.json")
        for name in SOURCES:
            archive.add(REPO / "scripts" / name, arcname=name)
        for name in ("profile.json", "inputs.json", "plan.json"):
            archive.add(package / name, arcname="input/" + name)
        images = {item["file"]: item for row in inputs["rows"] for item in row["images"]}
        for name in sorted(images):
            archive.add(input_file(package, images[name]), arcname="input/" + name)
    packet_sha = digest(packet)
    write(attempt / "launch.json", {"run_id": ident, "remote_directory": remote, "freeze": commit, "packet_sha256": packet_sha,
          "packet_bytes": packet.stat().st_size, "data_plan_sha256": execution_plan["data_plan_sha256"], "formal_release_eligible": False})
    connection = Connection.from_environment()
    ssh(connection, f"import json\nfrom pathlib import Path\np=Path({remote!r})\np.mkdir(parents=True,exist_ok=False)\nprint(json.dumps({{'created':True}}))\n")
    transfer = subprocess.run(["scp", *connection.arguments(), str(packet), connection.target + ":" + remote + "/packet.tar"], capture_output=True, timeout=600)
    if transfer.returncode:
        raise RuntimeError("packet transfer failed; attempt retained, reconcile before retry")
    code = f'''import hashlib,json,subprocess,tarfile
from pathlib import Path
p=Path({remote!r})
assert hashlib.sha256((p/'packet.tar').read_bytes()).hexdigest()=={packet_sha!r}
with tarfile.open(p/'packet.tar') as archive:
 for item in archive.getmembers():
  target=(p/item.name).resolve()
  assert item.isfile() and not item.issym() and target.is_relative_to(p)
  target.parent.mkdir(parents=True,exist_ok=True)
  with target.open('xb') as stream:stream.write(archive.extractfile(item).read())
with (p/'worker-started.json').open('x') as f:json.dump({{'status':'worker_start_requested','formal_release_eligible':False}},f)
with (p/'worker.log').open('xb') as log:
 worker=subprocess.Popen(['python3',str(p/'p6_road_s2_remote.py'),'--workspace','/home/ubuntu/drone-agent','--run-dir',str(p)],stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
print(json.dumps({{'run_id':{ident!r},'pid':worker.pid,'status':'worker_started'}}))
'''
    started = ssh(connection, code)
    write(attempt / "worker-started.json", started)
    return {**started, "attempt_directory": str(attempt), "formal_release_eligible": False}


def status(ident: str) -> dict:
    """Read status only; unknown worker state never means safe to relaunch.

只读状态；执行器状态未知不表示可以重新调用。
"""
    remote = remote_path(ident)
    code = f'''import json
from pathlib import Path
p=Path({remote!r})
result={{'run_id':{ident!r},'status':'unknown_do_not_repeat'}}
if (p/'execution.json').is_file():result=json.loads((p/'execution.json').read_text())
elif (p/'output/batch/run.json').is_file():result={{'run_id':{ident!r},'status':'running','progress':json.loads((p/'output/batch/run.json').read_text())}}
elif (p/'worker-started.json').is_file():result['status']='worker_start_requested_or_failed_before_receipt'
print(json.dumps(result))
'''
    return ssh(Connection.from_environment(), code)


def fetch(ident: str, destination: Path) -> dict:
    """Fetch completed evidence only into a new local directory. / 仅将已结束的证据取到新的本地目录。"""
    remote = remote_path(ident)
    connection = Connection.from_environment()
    receipt = status(ident)
    if receipt.get("format") != "drone.p6-road-cloud-execution/v1":
        raise ValueError("execution has no final receipt; do not relaunch")
    names = ssh(connection, f"import json\nfrom pathlib import Path\np=Path({remote!r})\nprint(json.dumps({{'files':[n for n in ['execution-plan.json','execution.json','precall.json','test-exposure.json','model.log','output'] if (p/n).exists()]}}))\n")["files"]
    destination.mkdir(parents=True, exist_ok=False)
    for name in names:
        if name not in {"execution-plan.json", "execution.json", "precall.json", "test-exposure.json", "model.log", "output"}:
            raise ValueError("unexpected remote artifact")
        transfer = subprocess.run(["scp", *connection.arguments(), "-r", connection.target + ":" + remote + "/" + name, str(destination)], capture_output=True, timeout=600)
        if transfer.returncode:
            raise RuntimeError("evidence transfer incomplete; keep the attempt for reconciliation")
    if read(destination / "execution.json") != receipt:
        raise ValueError("execution receipt changed during transfer")
    return receipt


def main() -> None:
    """Require explicit launch application; status and fetch never invoke a provider.

启动须显式应用；状态与取回操作不调用模型。
"""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    start = commands.add_parser("launch")
    start.add_argument("--package", type=Path, required=True)
    start.add_argument("--freeze", required=True)
    start.add_argument("--registry", required=True)
    start.add_argument("--apply", action="store_true")
    for name in ("status", "fetch"):
        command = commands.add_parser(name)
        command.add_argument("--run-id", required=True)
    commands.choices["fetch"].add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = launch(args.package.resolve(), args.freeze, args.registry, apply=args.apply) if args.command == "launch" else status(args.run_id) if args.command == "status" else fetch(args.run_id, args.output.resolve())
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
