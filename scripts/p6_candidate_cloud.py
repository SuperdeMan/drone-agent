"""Launch a frozen proposal review only after acquiring the shared project lock.

只在取得共享项目锁后启动冻结的逐提案复核。
"""
from __future__ import annotations

import argparse
import json
import subprocess
import tarfile
from datetime import UTC, datetime
from pathlib import Path

from dev_stack import Connection
from p6_candidate_review import VERSION, digest, input_file, read, write

REPO = Path(__file__).resolve().parents[1]
SOURCES = ["p6_candidate_cloud.py", "p6_candidate_review.py", "p6_candidate_runner.py", "p6_candidate_remote.py"]


def check_attempts(package: Path) -> None:
    """Retry only a conclusively zero-call busy-lock attempt, never an ambiguous run.

只允许锁忙且确证零调用的后续尝试，不重跑状态不明的批次。
"""
    folder = package / "attempts"
    if not folder.exists():
        return
    for previous in folder.iterdir():
        if not previous.is_dir():
            raise ValueError("unexpected attempt artifact")
        receipt = previous / "cloud/execution.json"
        if not receipt.exists():
            raise ValueError("prior attempt requires reconciliation")
        value = read(receipt)
        if not (value.get("status") == "blocked_lock_busy" and type(value.get("provider_invocations")) is int and value.get("provider_invocations") == 0
                and value.get("container_started") is False and value.get("project_lock_acquired") is False):
            raise ValueError("prior attempt may have invoked the model; no automatic repeat")


def ssh(connection, code, timeout=60):
    """Keep SSH banners and raw error streams out of receipts. / 不将 SSH 横幅或原始错误流写入回执。"""
    result = subprocess.run(["ssh", *connection.arguments(), connection.target, "python3 -"],
                            input=code.encode(), capture_output=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f"remote transport failed: exit {result.returncode}; inspect retained attempt")
    return result.stdout


def launch(package: Path, freeze: str) -> dict:
    """Bind code and inputs, retaining append-only attempt and execution evidence.

绑定代码及输入，保留只追加的尝试与执行证据。
"""
    check_attempts(package)
    subprocess.run(["git", "merge-base", "--is-ancestor", freeze, "HEAD"], cwd=REPO, check=True, capture_output=True)
    for name in SOURCES:
        if (REPO / "scripts" / name).read_bytes() != subprocess.check_output(["git", "show", f"{freeze}:scripts/{name}"], cwd=REPO):
            raise ValueError("tool source differs from frozen commit")
    plan = read(package / "plan.json")
    if plan["prompt_version"] != VERSION or plan["planned_calls"] != 6 or plan["proposals"] != 33:
        raise ValueError("launcher is restricted to the frozen six-view, 33-proposal task")
    for local, name in [(package / "config.json", "config.json"), (package / "inputs/manifest.json", "input-manifest.json"),
                        (package / "plan.json", "plan.json")]:
        if local.read_bytes() != subprocess.check_output(["git", "show", f"{freeze}:eval/s2/p6_candidates_v8/{name}"], cwd=REPO):
            raise ValueError("package differs from frozen metadata")
    for name, key in [("p6_candidate_review.py", "contract_sha256"), ("p6_candidate_runner.py", "runner_sha256"),
                      ("p6_candidate_remote.py", "remote_worker_sha256")]:
        if digest(REPO / "scripts" / name) != plan[key]:
            raise ValueError("helper source hash mismatch")
    manifest = read(package / "inputs/manifest.json")
    for row in manifest:
        for item in row["images"]:
            input_file(package / "inputs", item)
    ident = "p6-candidate-review-dev-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    attempt = package / "attempts" / ident
    attempt.mkdir(parents=True, exist_ok=False)
    remote = "/home/ubuntu/drone-agent/artifacts/" + ident
    packet = attempt / "packet.tar"
    with tarfile.open(packet, "x") as archive:
        for name in ("config.json", "plan.json"):
            archive.add(package / name, arcname=name)
        for name in SOURCES:
            if name != "p6_candidate_cloud.py":
                archive.add(REPO / "scripts" / name, arcname=name)
        archive.add(package / "inputs/manifest.json", arcname="input/manifest.json")
        for name in sorted({i["file"] for row in manifest for i in row["images"]}):
            archive.add(package / "inputs" / name, arcname="input/" + name)
    packet_sha = digest(packet)
    write(attempt / "launch.json", {"run_id": ident, "freeze_commit": freeze, "launcher_sha256": digest(Path(__file__)),
          "created_at": datetime.now(UTC).isoformat(), "remote_directory": remote, "packet_sha256": packet_sha,
          "packet_bytes": packet.stat().st_size, "config_sha256": plan["config_sha256"],
          "manifest_sha256": plan["manifest_sha256"], "formal_release_eligible": False})
    connection = Connection.from_environment()
    ssh(connection, f"from pathlib import Path\nPath({remote!r}).mkdir(parents=True, exist_ok=False)\n")
    result = subprocess.run(["scp", *connection.arguments(), str(packet), connection.target + ":" + remote + "/packet.tar"],
                            capture_output=True, timeout=180)
    if result.returncode:
        raise RuntimeError("packet upload failed; attempt retained")
    code = f'''import hashlib,subprocess,tarfile
from pathlib import Path
p=Path({remote!r})
assert hashlib.sha256((p/'packet.tar').read_bytes()).hexdigest()=={packet_sha!r}
with tarfile.open(p/'packet.tar') as archive:
 for item in archive.getmembers():
  target=(p/item.name).resolve()
  assert item.isfile() and not item.issym() and target.is_relative_to(p)
  target.parent.mkdir(parents=True,exist_ok=True)
  with target.open('xb') as stream:stream.write(archive.extractfile(item).read())
result=subprocess.run(['python3',str(p/'p6_candidate_remote.py'),'--workspace','/home/ubuntu/drone-agent','--run-dir',str(p)],capture_output=True,timeout=1900)
if result.returncode:raise RuntimeError('remote worker failed; inspect retained artifacts')
print(result.stdout.decode(),end='')
'''
    print(json.dumps({"status": "uploaded_awaiting_lock_check", "run_id": ident}), flush=True)
    receipt = json.loads(ssh(connection, code, timeout=1950))
    names = json.loads(ssh(connection, f"import json\nfrom pathlib import Path\np=Path({remote!r})\nprint(json.dumps([n for n in ['execution.json','precall.json','diagnostic.log','output'] if (p/n).exists()]))\n"))
    destination = attempt / "cloud"
    destination.mkdir()
    for name in names:
        if name not in {"execution.json", "precall.json", "diagnostic.log", "output"}:
            raise ValueError("unexpected remote artifact")
        copied = subprocess.run(["scp", *connection.arguments(), "-r", connection.target + ":" + remote + "/" + name, str(destination)],
                                capture_output=True, timeout=180)
        if copied.returncode:
            raise RuntimeError("artifact download failed; reconcile before any retry")
    if read(destination / "execution.json") != receipt:
        raise ValueError("execution receipt changed during transfer")
    print(json.dumps({"run_id": ident, **receipt}), flush=True)
    return receipt


def main() -> None:
    """Require explicit frozen metadata; blocked attempts never trigger a retry loop.

要求明确的冻结元数据；受阻尝试不会触发重试循环。
"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--freeze", required=True)
    args = parser.parse_args()
    result = launch(args.package.resolve(), args.freeze)
    if result["status"] != "completed":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
