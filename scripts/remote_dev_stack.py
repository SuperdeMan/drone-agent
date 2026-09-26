"""Own one isolated cloud simulation workspace; communicate using JSON over SSH stdin.

管理一套隔离的云端仿真工作区；通过 SSH 标准输入中的 JSON 通信。
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import subprocess
import tarfile
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

PROJECT = "drone-agent-cloud"
OWNER = "drone-agent-cloud/v1"
IMAGE_REFS = ("drone-agent-sitl:px4-1.17.0-m0", "px4io/px4-dev-ros2:drone-m0-pinned")
CONTROL_FILES = ("compose.cloud.yaml", "checks.Dockerfile", "requirements.txt")
SHA = re.compile(r"^[0-9a-f]{40}$")
RUN_ID = re.compile(r"^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}$")
# Recorded run directories (`m1-<run_id>`, `p1-<run_id>`) and case directories (`<scenario>-<seed>`) under artifacts/.
# artifacts/ 下的运行目录（`m1-<run_id>`、`p1-<run_id>`）与用例目录（`<scenario>-<seed>`）。
RUN_DIR = re.compile(r"^[mp][0-9]+-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}$")
CASE_DIR = re.compile(r"^[a-z][a-z0-9_]*-[0-9]+$")
READ_ONLY_ACTIONS = frozenset({"status", "runs", "inspect", "live_status", "console_plan", "console_status",
                               "desk_plan", "desk_status"})


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def run(argv: list[str], *, cwd: Path | None = None, env: dict | None = None, timeout: int = 300) -> str:
    result = subprocess.run(argv, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    if result.returncode:
        detail = result.stderr.decode("utf-8", errors="replace")[-2500:]
        raise RuntimeError(f"{argv[0]} failed ({result.returncode}): {detail}")
    return result.stdout.decode("utf-8", errors="replace")


def image_fingerprint(details: dict) -> dict:
    """Compare filesystem layers and runtime configuration across Docker storage backends.

    跨 Docker 存储后端比较文件系统层与运行配置。
    """
    config = details["Config"]
    runtime = {key: config.get(key) for key in ("Env", "Entrypoint", "Cmd", "User", "WorkingDir", "Healthcheck")}
    return {
        "architecture": details["Architecture"], "os": details["Os"],
        "layers": details["RootFS"]["Layers"],
        "runtime_sha256": hashlib.sha256(json.dumps(runtime, sort_keys=True).encode()).hexdigest(),
    }


def inspect_image(name: str) -> dict | None:
    result = subprocess.run(["docker", "image", "inspect", name], capture_output=True)
    return json.loads(result.stdout)[0] if result.returncode == 0 else None


def workspace(*, create: bool = False) -> Path:
    root = Path.home() / "drone-agent"
    if not str(root).isascii() or root.is_symlink():
        raise ValueError("cloud workspace must be a real ASCII directory")
    marker = root / "owner.json"
    if root.exists():
        if not marker.is_file() or json.loads(marker.read_text()) != {"owner": OWNER, "uid": os.getuid()}:
            raise ValueError("cloud workspace exists without the expected ownership marker")
    elif create:
        root.mkdir(mode=0o700)
        marker.write_text(json.dumps({"owner": OWNER, "uid": os.getuid()}))
    return root


@contextlib.contextmanager
def locked(root: Path):
    """Serialize this project's mutations without taking another application's lock.

    串行化本项目的变更，不占用其他应用的锁。
    """
    import fcntl

    with (root / "stack.lock").open("a") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def capacity() -> dict:
    memory = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith(("MemTotal:", "MemAvailable:")):
            memory[line.split(":")[0]] = int(line.split()[1]) * 1024
    return {"cpu_count": os.cpu_count(), "load": os.getloadavg(), "memory": memory,
            "disk_free_bytes": shutil.disk_usage(Path.home()).free}


def foreign_identity() -> dict:
    """Read container identities only; never read another application's environment.

    仅读取其他容器身份，不读取其他应用的环境变量。
    """
    ids = run(["docker", "ps", "-q"]).split()
    template = ('{"id":{{json .Id}},"image":{{json .Image}},"started_at":{{json .State.StartedAt}},'
                '"restart_count":{{.RestartCount}},"project":{{json (index .Config.Labels "com.docker.compose.project")}}}')
    details = [json.loads(line) for line in run(["docker", "inspect", "--format", template, *ids]).splitlines()] if ids else []
    rows = []
    for item in details:
        if item["project"] == PROJECT:
            continue
        rows.append({key: item[key] for key in ("id", "image", "started_at", "restart_count")})
    rows.sort(key=lambda row: row["id"])
    return {"count": len(rows), "sha256": hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()}


def current(root: Path) -> Path:
    record = json.loads((root / "current.json").read_text())
    deployment_id = record["deployment_id"]
    if not RUN_ID.fullmatch(deployment_id):
        raise ValueError("invalid deployment identifier")
    return root / "releases" / deployment_id


def status(root: Path) -> dict:
    result = {"target": "cloud", "workspace": str(root), "capacity": capacity(), "deployed": False,
              "other_containers": foreign_identity(), "images": {}}
    for name in IMAGE_REFS:
        details = inspect_image(name)
        if details:
            result["images"][name] = {"id": details["Id"], "fingerprint": image_fingerprint(details)}
    if (root / "current.json").is_file():
        location = current(root)
        result.update(deployed=True, deployment_id=location.name,
                      source_sha=json.loads((location / "manifest.json").read_text())["source_sha"])
        result["containers"] = compose(root, location, ["ps", "--format", "json"])
    return result


def validate_archive(archive: tarfile.TarFile) -> None:
    """Reject traversal, links, devices and secret files before extraction.

    在解包之前拒绝目录穿越、链接、设备与密钥文件。
    """
    for member in archive.getmembers():
        path = PurePosixPath(member.name)
        if path.is_absolute() or ".." in path.parts or not (member.isfile() or member.isdir()):
            raise ValueError("unsafe source archive member")
        if any(part == ".git" or part.startswith(".env") for part in path.parts):
            raise ValueError("source archive contains excluded configuration")


def validate_manifest(manifest: dict) -> None:
    if not SHA.fullmatch(manifest.get("source_sha", "")) or not RUN_ID.fullmatch(manifest.get("run_id", "")):
        raise ValueError("invalid source or run identifier")
    if not re.fullmatch(r"[0-9a-f]{64}", manifest.get("control_sha256", "")):
        raise ValueError("invalid control digest")
    allowed = {"source.tar", *CONTROL_FILES, "images.tar.gz"}
    files = manifest.get("files", {})
    if set(files) - allowed or not {"source.tar", *CONTROL_FILES} <= set(files):
        raise ValueError("unexpected deployment files")
    if any(not re.fullmatch(r"[0-9a-f]{64}", value) for value in files.values()):
        raise ValueError("invalid file digest")
    if set(manifest.get("images", {})) != set(IMAGE_REFS):
        raise ValueError("unexpected image references")


def compose(root: Path, deployment: Path, arguments: list[str], *, run_id: str | None = None, timeout: int = 300) -> str:
    manifest = json.loads((deployment / "manifest.json").read_text())
    artifacts = root / "artifacts" / deployment.name
    checks = artifacts / (run_id or "checks")
    env = dict(os.environ, DRONE_SITL_IMAGE=IMAGE_REFS[0],
               DRONE_CHECKS_IMAGE=f"drone-agent-checks:{manifest['source_sha']}-{manifest['control_sha256'][:12]}",
               DRONE_ARTIFACTS=str(artifacts), DRONE_CHECK_ARTIFACTS=str(checks))
    return run(["docker", "compose", "-p", PROJECT, "-f", str(deployment / "control/compose.cloud.yaml"), *arguments],
               cwd=deployment, env=env, timeout=timeout)


def smoke(root: Path, deployment: Path, run_id: str) -> dict:
    output = f"smoke-{run_id}.json"
    deadline = time.monotonic() + 120
    while True:
        try:
            raw = compose(root, deployment, ["exec", "-T", "sitl", "python3", "/opt/drone-sim/smoke.py",
                                           "--timeout", "20", "--output", f"/artifacts/{output}"], timeout=40)
            report = json.loads(raw)
            if report.get("status") != "passed" or report.get("scope") != "unarmed_environment_smoke":
                raise ValueError("smoke did not pass")
            return report
        except RuntimeError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(2)


def checks(root: Path, deployment: Path, run_id: str) -> dict:
    (root / "artifacts" / deployment.name / run_id).mkdir(parents=True, exist_ok=False)
    compose(root, deployment, ["run", "-T", "--rm", "--no-deps", "--name", f"drone-agent-checks-{run_id}", "checks"],
            run_id=run_id, timeout=300)
    xml = root / "artifacts" / deployment.name / run_id / "contracts.xml"
    suites = ET.parse(xml).findall(".//testsuite")
    counts = {key: sum(int(item.attrib.get(key, 0)) for item in suites) for key in ("tests", "failures", "errors", "skipped")}
    if not counts["tests"] or counts["failures"] or counts["errors"] or counts["skipped"]:
        raise ValueError("cloud contract checks did not pass without skips")
    return counts


def operation_receipt(deployment: Path, run_id: str, result: dict) -> dict:
    """Bind a check to the deployment captured under the lock, not a later status query.

    将检查绑定到持锁时取得的部署，而不是之后状态查询读到的部署。
    """
    manifest = json.loads((deployment / "manifest.json").read_text())
    return {"status": "verified", "target": "cloud", "source_sha": manifest["source_sha"],
            "control_sha256": manifest["control_sha256"], "deployment_id": deployment.name,
            "run_id": run_id, "result": result}


def deploy(root: Path, request: dict) -> dict:
    incoming = root / "incoming" / request["run_id"]
    manifest = json.loads((incoming / "manifest.json").read_text())
    validate_manifest(manifest)
    if manifest["run_id"] != request["run_id"]:
        raise ValueError("run identity mismatch")
    for name, expected in manifest["files"].items():
        if digest(incoming / name) != expected:
            raise ValueError(f"transport checksum mismatch: {name}")
    resources = capacity()
    if resources["memory"]["MemAvailable"] < 3 * 1024**3 or resources["disk_free_bytes"] < 8 * 1024**3:
        raise RuntimeError("insufficient shared-server headroom")
    before = foreign_identity()
    with tarfile.open(incoming / "source.tar") as stream:
        validate_archive(stream)
    for name, expected in manifest["images"].items():
        existing = inspect_image(name)
        if existing is not None and image_fingerprint(existing) != expected["fingerprint"]:
            raise ValueError(f"refusing to overwrite a different existing bootstrap image: {name}")
    archive = incoming / "images.tar.gz"
    if archive.exists():
        # No unrelated image tags may be imported into the shared Docker daemon.
        # 不允许向共享 Docker 导入无关的镜像标签。
        with tarfile.open(archive) as stream:
            tags = {tag for item in json.load(stream.extractfile("manifest.json")) for tag in item.get("RepoTags", [])}
            if tags != set(IMAGE_REFS):
                raise ValueError("bootstrap archive contains unexpected image tags")
        run(["docker", "load", "-i", str(archive)], timeout=600)
    for name, expected in manifest["images"].items():
        details = inspect_image(name)
        if details is None or image_fingerprint(details) != expected["fingerprint"]:
            raise ValueError(f"image content mismatch: {name}")
    deployment = root / "releases" / request["run_id"]
    deployment.mkdir(parents=True, exist_ok=False)
    source = deployment / "source"
    source.mkdir()
    with tarfile.open(incoming / "source.tar") as stream:
        validate_archive(stream)
        stream.extractall(source, filter="data")
    control = deployment / "control"
    control.mkdir()
    for name in CONTROL_FILES:
        shutil.copyfile(incoming / name, control / name)
    shutil.copyfile(incoming / "manifest.json", deployment / "manifest.json")
    checks_image = f"drone-agent-checks:{manifest['source_sha']}-{manifest['control_sha256'][:12]}"
    build_log = root / "artifacts" / deployment.name / "checks-build.log"
    build_log.parent.mkdir(parents=True)
    build = subprocess.run(["docker", "build", "--pull=false", "--build-arg", f"SITL_IMAGE={IMAGE_REFS[0]}",
                            "-f", str(control / "checks.Dockerfile"), "-t", checks_image, str(deployment)],
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=900)
    build_log.write_bytes(build.stdout)
    if build.returncode:
        raise RuntimeError("checks image build failed; inspect the owned artifact log")
    previous = current(root) if (root / "current.json").exists() else None
    try:
        compose(root, deployment, ["up", "-d", "--no-build", "--pull", "never", "sitl"])
        report = smoke(root, deployment, request["run_id"])
        test_counts = checks(root, deployment, request["run_id"])
    except Exception:
        if previous:
            compose(root, previous, ["up", "-d", "--no-build", "--pull", "never", "sitl"])
        else:
            compose(root, deployment, ["stop", "sitl"])
        raise
    after = foreign_identity()
    if before != after:
        raise RuntimeError("other container identities changed during verification; do not claim isolation verified")
    receipt = {"status": "verified", "target": "cloud", "timestamp": datetime.now(timezone.utc).isoformat(),
               "source_sha": manifest["source_sha"], "control_sha256": manifest["control_sha256"],
               "deployment_id": deployment.name, "capacity_before": resources, "smoke": report,
               "tests": test_counts, "other_containers_before": before, "other_containers_after": after,
               "images": {name: inspect_image(name)["Id"] for name in IMAGE_REFS}}
    (build_log.parent / "deployment.json").write_text(json.dumps(receipt, indent=2))
    pending = root / "current.pending.json"
    pending.write_text(json.dumps({"deployment_id": deployment.name}))
    pending.replace(root / "current.json")
    return receipt


def artifact_runs(root: Path) -> dict:
    """List recorded runs with their receipt summaries; read-only. / 只读列出已记录的运行及其回执摘要。"""
    rows = []
    base = root / "artifacts"
    for deployment in sorted(base.iterdir()) if base.is_dir() else []:
        if deployment.is_symlink() or not deployment.is_dir() or not RUN_ID.fullmatch(deployment.name):
            continue
        for run in sorted(deployment.iterdir()):
            if run.is_symlink() or not run.is_dir() or not RUN_DIR.fullmatch(run.name):
                continue
            summary = {"deployment_id": deployment.name, "run": run.name, "source_sha": None, "cases": [],
                       "passed": None, "total": 0, "has_receipt": False}
            progress = run / "progress.json"
            if progress.is_file():
                data = json.loads(progress.read_text())
                results = data.get("results", [])
                summary.update(source_sha=data.get("source_sha"), has_receipt=True, total=len(results),
                               passed=sum(1 for row in results if row.get("passed") is True),
                               cases=[f"{row.get('scenario')}-{row.get('seed')}" for row in results])
            rows.append(summary)
    return {"target": "cloud", "runs": rows}


def inspect_run(root: Path, request: dict) -> dict:
    """Receipt plus per-file size and digest for one run so the client can verify what it downloads.

    返回一次运行的回执与逐文件大小、摘要，供客户端核对下载内容。
    """
    deployment, run = request.get("deployment", ""), request.get("run", "")
    if not RUN_ID.fullmatch(deployment) or not RUN_DIR.fullmatch(run):
        raise ValueError("invalid run reference")
    wanted = request.get("cases")
    if wanted is not None and (not isinstance(wanted, list) or any(not CASE_DIR.fullmatch(str(c)) for c in wanted)):
        raise ValueError("invalid case selection")
    path = root / "artifacts" / deployment / run
    if path.is_symlink() or not path.is_dir():
        raise ValueError("run directory not found")
    receipt = json.loads((path / "progress.json").read_text()) if (path / "progress.json").is_file() else None
    cases = {}
    for case in sorted(path.iterdir()):
        if case.is_symlink() or not case.is_dir() or not CASE_DIR.fullmatch(case.name):
            continue
        if wanted is not None and case.name not in wanted:
            continue
        files = {}
        for file in sorted(case.rglob("*")):
            if file.is_file() and not file.is_symlink():
                files[file.relative_to(case).as_posix()] = {"size": file.stat().st_size, "sha256": digest(file)}
        cases[case.name] = files
    return {"target": "cloud", "deployment_id": deployment, "run": run, "path": str(path),
            "receipt": receipt, "cases": cases}


def store_model_key(root: Path, request: dict) -> dict:
    """Write the planner model key owner-only into this project's secrets; never echo it (M2 plan).

    把规划模型 key 以仅属主权限写入本项目 secrets；从不回显（M2 计划）。
    """
    key = request.get("key")
    if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9._\-]{20,400}", key):
        raise ValueError("unexpected model key shape")
    folder = root / "secrets" / "m2-model"
    folder.mkdir(parents=True, exist_ok=True)
    os.chmod(root / "secrets", 0o700)
    os.chmod(folder, 0o700)
    pending = folder / "minimax.key.pending"
    descriptor = os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        stream.write(key)
    pending.replace(folder / "minimax.key")
    return {"status": "stored", "path": str(folder / "minimax.key"), "mode": "0600"}


def dispatch(request: dict) -> dict:
    action = request.get("action")
    if action not in {*READ_ONLY_ACTIONS, "prepare", "deploy", "verify", "test", "start", "stop", "logs", "m1", "m2",
                       "m3", "p1", "p2", "m2_key", "live_start", "live_operate", "console_apply", "desk_apply",
                       "desk_members"}:
        raise ValueError("unsupported cloud action")
    if action not in READ_ONLY_ACTIONS and not RUN_ID.fullmatch(request.get("run_id", "")):
        raise ValueError("invalid run identity")
    root = workspace(create=action == "prepare")
    # Read-only queries never take the mutation lock: they neither block nor wait for a running batch.
    # 只读查询不占用变更锁：既不阻塞运行中的批次，也不等待它。
    if action == "status":
        return status(root)
    if action == "runs":
        return artifact_runs(root)
    if action == "inspect":
        return inspect_run(root, request)
    if action in {"console_plan", "console_status", "console_apply"}:
        import runpy

        deployment = current(root)
        script = deployment / "source/scripts/remote_console.py"
        if not script.is_file():
            raise ValueError("deploy a committed cloud-console version first")
        console = runpy.run_path(str(script))
        if action == "console_status":
            return console["status"](root)
        if action == "console_plan":
            return console["plan"](root, deployment)
        with locked(root):
            return console["apply"](root, deployment, request)
    if action in {"desk_plan", "desk_status", "desk_apply", "desk_members"}:
        import runpy

        deployment = current(root)
        script = deployment / "source/scripts/remote_desk.py"
        if not script.is_file():
            raise ValueError("deploy a committed mission-desk version first")
        desk = runpy.run_path(str(script))
        if action == "desk_status":
            return desk["status"](root)
        if action == "desk_plan":
            return desk["plan"](root, deployment)
        with locked(root):
            if action == "desk_members":
                return desk["store_members"](root, request)
            return desk["apply"](root, deployment, request)
    if action in {"live_status", "live_start", "live_operate"}:
        import runpy

        deployment = current(root)
        script = deployment / "source/scripts/remote_live.py"
        if not script.is_file():
            raise ValueError("deploy a version with the live simulation entry first")
        live = runpy.run_path(str(script))
        if action == "live_status":
            return live["snapshot"](root, deployment)
        return live["start" if action == "live_start" else "operate"](root, deployment, request)
    with locked(root):
        if action == "prepare":
            owned = root / "incoming" / request["run_id"]
            owned.mkdir(parents=True, exist_ok=False)
            return {"status": "prepared", "incoming": str(owned)}
        if action == "deploy":
            return deploy(root, request)
        deployment = current(root)
        if action == "m1":
            import runpy

            return runpy.run_path(str(deployment / "source/scripts/remote_m1.py"))["run_m1"](root, deployment, request)
        if action == "m2_key":
            return store_model_key(root, request)
        if action == "m3":
            import runpy

            script = deployment / "source/scripts/remote_m3.py"
            if not script.is_file():
                raise ValueError("deploy a version with the M3 runner first")
            return runpy.run_path(str(script))["run_m3"](root, deployment, request)
        if action == "m2":
            import runpy

            script = deployment / "source/scripts/remote_m2.py"
            if not script.is_file():
                raise ValueError("deploy a version with the M2 end-to-end runner first")
            return runpy.run_path(str(script))["run_m2"](root, deployment, request)
        if action == "p1":
            import runpy

            script = deployment / "source/scripts/remote_p1.py"
            if not script.is_file():
                raise ValueError("deploy a version with the P1 S1 runner first")
            return runpy.run_path(str(script))["run_p1"](root, deployment, request)
        if action == "p2":
            import runpy

            script = deployment / "source/scripts/remote_p2.py"
            if not script.is_file():
                raise ValueError("deploy a version with the P2 S1 runner first")
            return runpy.run_path(str(script))["run_p2"](root, deployment, request)
        if action == "verify":
            return operation_receipt(deployment, request["run_id"], smoke(root, deployment, request["run_id"]))
        if action == "test":
            return operation_receipt(deployment, request["run_id"], checks(root, deployment, request["run_id"]))
        if action == "logs":
            return {"logs": compose(root, deployment, ["logs", "--no-color", "--tail", "80", "sitl"])}
        args = ["up", "-d", "--no-build", "--pull", "never", "sitl"] if action == "start" else ["stop", "sitl"]
        return {"status": action, "output": compose(root, deployment, args)}
