"""Cloud-first development commands for the isolated drone simulation workspace.

面向隔离无人机仿真工作区的云端优先开发命令。
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import re
import runpy
import shutil
import subprocess
import sys
import tarfile
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKER_PATH = ROOT / "scripts/remote_dev_stack.py"
WORKER_CODE = WORKER_PATH.read_bytes()
REMOTE = runpy.run_path(str(WORKER_PATH))
IMAGE_REFS = REMOTE["IMAGE_REFS"]


@dataclass(frozen=True)
class Connection:
    host: str
    user: str
    identity: Path
    source: str

    @classmethod
    def from_environment(cls) -> Connection:
        """Reuse SSH parameters only; never read or copy a sibling .env file.

        仅复用 SSH 参数，永不读取或复制姊妹项目的 .env 文件。
        """
        prefix = "DRONE_AGENT" if os.getenv("DRONE_AGENT_DEPLOY_HOST") else "CAR_AGENT"
        host = os.getenv(prefix + "_DEPLOY_HOST", "")
        user = os.getenv(prefix + "_DEPLOY_USER", "ubuntu")
        identity = Path(os.getenv(prefix + "_SSH_IDENTITY", ""))
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.:-]*", host):
            raise ValueError("set a valid DRONE_AGENT_DEPLOY_HOST (or existing CAR_AGENT_DEPLOY_HOST)")
        if not re.fullmatch(r"[a-z_][a-z0-9_-]*", user) or not identity.is_file():
            raise ValueError("set a valid SSH user and existing identity path")
        return cls(host, user, identity.resolve(), prefix)

    def arguments(self) -> list[str]:
        return [
            "-i",
            str(self.identity),
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            "ConnectTimeout=15",
            "-o",
            "ServerAliveInterval=20",
            "-o",
            "ServerAliveCountMax=3",
        ]

    @property
    def target(self) -> str:
        return self.user + "@" + self.host


def ssh(connection: Connection, request: dict, *, timeout: int = 1800) -> dict:
    suffix = (
        "\ntry:\n    result = dispatch(json.loads(" + repr(json.dumps(request)) + "))\n"
        "    print(json.dumps(result))\n"
        "except Exception as exc:\n    print(json.dumps({'status': 'error', 'reason': str(exc)}))\n    raise SystemExit(1)\n"
    )
    result = subprocess.run(
        ["ssh", *connection.arguments(), connection.target, "python3 -"],
        input=WORKER_CODE + suffix.encode(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
    )
    # SSH banners can contain transient login URLs; never print raw stderr.
    # SSH banner 可能带临时登录链接；不打印原始 stderr。
    try:
        payload = json.loads(result.stdout)
    except (ValueError, UnicodeError) as error:
        raise RuntimeError(
            f"cloud command returned no valid JSON (exit {result.returncode}); SSH stderr withheld"
        ) from error
    if result.returncode:
        raise RuntimeError(payload.get("reason", "cloud command failed"))
    return payload


def new_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]


def local(argv: list[str], *, cwd: Path = ROOT, env: dict | None = None) -> bytes:
    result = subprocess.run(argv, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode:
        raise RuntimeError(f"local tool failed: {argv[0]} (exit {result.returncode})")
    return result.stdout


def prepare_packet(sha_ref: str, artifact_root: Path, images: dict) -> tuple[Path, dict]:
    sha = local(["git", "rev-parse", "--verify", "--end-of-options", sha_ref + "^{commit}"]).decode().strip()
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("invalid resolved commit")
    local(["git", "merge-base", "--is-ancestor", sha, "main"])
    if not str(artifact_root.resolve()).isascii():
        raise ValueError("transport artifacts need an ASCII path")
    packet = artifact_root.resolve() / new_run_id()
    packet.mkdir(parents=True, exist_ok=False)
    archive = packet / "source.tar"
    archive.write_bytes(local(["git", "archive", "--format=tar", sha]))
    source = packet / "source"
    source.mkdir()
    with tarfile.open(archive) as stream:
        REMOTE["validate_archive"](stream)
        stream.extractall(source, filter="data")
    env = dict(os.environ, UV_DEFAULT_INDEX="https://pypi.tuna.tsinghua.edu.cn/simple")
    requirements = local(
        [
            "uv",
            "export",
            "--frozen",
            "--group",
            "dev",
            "--group",
            "flight",
            "--no-emit-project",
            "--no-header",
            "--format",
            "requirements.txt",
        ],
        cwd=source,
        env=env,
    )
    (packet / "requirements.txt").write_bytes(requirements)
    for name in ("compose.cloud.yaml", "checks.Dockerfile"):
        shutil.copyfile(ROOT / "sim" / name, packet / name)
    files = {name: REMOTE["digest"](packet / name) for name in ("source.tar", *REMOTE["CONTROL_FILES"])}
    control_inputs = {name: files[name] for name in REMOTE["CONTROL_FILES"]}
    control_inputs["remote_worker"] = hashlib.sha256(WORKER_CODE).hexdigest()
    manifest = {
        "schema_version": "0.1.0",
        "source_sha": sha,
        "run_id": packet.name,
        "files": files,
        "control_sha256": hashlib.sha256(json.dumps(control_inputs, sort_keys=True).encode()).hexdigest(),
        "images": images,
        "target": "cloud",
    }
    (packet / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return packet, manifest


def export_images(packet: Path, manifest: dict) -> None:
    """Bootstrap from already built M0 images; do not build or start a local stack.

    从已构建的 M0 镜像引导，不构建或启动本地真栈。
    """
    path = packet / "images.tar.gz"
    print("Exporting existing bootstrap images...", file=sys.stderr, flush=True)
    process = subprocess.Popen(["docker", "image", "save", *IMAGE_REFS], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    with path.open("wb") as output, gzip.GzipFile(fileobj=output, mode="wb", compresslevel=1, mtime=0) as compressed:
        shutil.copyfileobj(process.stdout, compressed, length=1024 * 1024)
    process.stdout.close()
    if process.wait() != 0:
        raise RuntimeError("bootstrap image export failed")
    manifest["files"][path.name] = REMOTE["digest"](path)
    (packet / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"Bootstrap archive ready: {path.stat().st_size} bytes", file=sys.stderr, flush=True)


def sftp_literal(value: str) -> str:
    """Quote a path in an SFTP batch without permitting another command.

    为 SFTP 批处理引用路径，不允许注入其他命令。
    """
    if any(character in value for character in ('"', "\n", "\r")):
        raise ValueError("unsupported SFTP path")
    return '"' + value.replace("\\", "/") + '"'


def upload_packet(connection: Connection, packet: Path, manifest: dict, destination: str, *, resume: bool) -> None:
    if not re.fullmatch(r"/home/[a-z_][a-z0-9_-]*/drone-agent/incoming/" + re.escape(packet.name), destination):
        raise ValueError("unexpected remote upload destination")
    commands = []
    for name in [*manifest["files"], "manifest.json"]:
        # Only the immutable large archive is resumed; small manifests are replaced as a whole.
        # 仅对不可变的大归档续传；小型清单整体重新传输。
        operation = "put -a" if resume and name == "images.tar.gz" else "put"
        commands.append(f"{operation} {sftp_literal(str(packet / name))} {sftp_literal(destination + '/' + name)}")
    print("Uploading packet via resumable SFTP...", file=sys.stderr, flush=True)
    result = subprocess.run(
        ["sftp", "-b", "-", *connection.arguments(), connection.target],
        input=("\n".join(commands) + "\n").encode(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=7200,
    )
    if result.returncode:
        raise RuntimeError(f"SFTP upload failed; retry deploy --resume {packet} --apply; SSH stderr withheld")


CASE_NAME = re.compile(r"^[a-z][a-z0-9_]*-[0-9]+$")


def download_files(connection: Connection, transfers: list[tuple[str, Path]]) -> None:
    """Pull files in one SFTP batch; the first failed get aborts the batch. / 用一个 SFTP 批处理拉取文件；首个失败即中止。"""
    commands = []
    for remote, local_path in transfers:
        local_path.parent.mkdir(parents=True, exist_ok=True)
        commands.append(f"get {sftp_literal(remote)} {sftp_literal(str(local_path))}")
    print(f"Downloading {len(transfers)} files via SFTP...", file=sys.stderr, flush=True)
    result = subprocess.run(
        ["sftp", "-b", "-", *connection.arguments(), connection.target],
        input=("\n".join(commands) + "\n").encode(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=7200,
    )
    if result.returncode:
        raise RuntimeError("SFTP download failed; partial files are kept for inspection; SSH stderr withheld")


def fetch_command(connection: Connection, args: argparse.Namespace, *, transport=None, download=None) -> dict:
    """Copy one recorded cloud run into a local ASCII directory, verify every digest, build the viewer.

    Without --apply this only returns the plan. Digests come from the remote listing and, for the files
    the judge hashed, from the run receipt; any difference is an error, never a warning.

    把一次云端记录的运行复制到本地 ASCII 目录，核对每个摘要，并生成证据浏览器。

    不带 --apply 只返回计划。摘要来自远端清单，裁判散列过的文件另与回执比对；任何差异都是错误，不是警告。
    """
    transport = transport or ssh
    download = download or download_files
    if not REMOTE["RUN_DIR"].fullmatch(args.run):
        raise ValueError("invalid run directory name")
    deployment = args.deployment or transport(connection, {"action": "status"}).get("deployment_id", "")
    if not REMOTE["RUN_ID"].fullmatch(deployment or ""):
        raise ValueError("invalid deployment identifier")
    wanted = None if args.cases == "all" else args.cases.split(",")
    if wanted is not None and any(not CASE_NAME.fullmatch(name) for name in wanted):
        raise ValueError("invalid case name")
    if not str(args.artifacts.resolve()).isascii():
        raise ValueError("transport artifacts need an ASCII path")
    listing = transport(
        connection,
        {"action": "inspect", "run_id": new_run_id(), "deployment": deployment, "run": args.run, "cases": wanted},
    )
    remote_root = listing["path"]
    pattern = r"/home/[a-z_][a-z0-9_-]*/drone-agent/artifacts/" + re.escape(deployment) + "/" + re.escape(args.run)
    if not re.fullmatch(pattern, remote_root):
        raise ValueError("unexpected remote artifact directory")
    excluded = {part for part in args.exclude.split(",") if part}
    local_root = args.artifacts.resolve() / "runs" / deployment / args.run
    transfers, expected = [], {}
    for case, files in sorted(listing["cases"].items()):
        if not CASE_NAME.fullmatch(case):
            raise ValueError("unexpected case directory")
        for name, meta in sorted(files.items()):
            parts = name.split("/")
            if name.startswith("/") or any(part in {"", ".", ".."} for part in parts):
                raise ValueError("unexpected artifact path")
            if parts[0] in excluded or name in excluded:
                continue
            transfers.append((f"{remote_root}/{case}/{name}", local_root / case / name))
            expected[f"{case}/{name}"] = meta
    receipt = listing.get("receipt")
    rows = {f"{row.get('scenario')}-{row.get('seed')}": row for row in (receipt or {}).get("results", [])}
    archived_agrees = None
    if args.receipt:
        archived = json.loads(args.receipt.read_text(encoding="utf-8-sig"))
        archived_rows = {f"{row.get('scenario')}-{row.get('seed')}": row for row in archived.get("results", [])}
        archived_agrees = bool(receipt) and archived.get("source_sha") == receipt.get("source_sha") and all(
            archived_rows.get(case, {}).get("artifacts") == rows.get(case, {}).get("artifacts")
            for case in listing["cases"]
        )
    plan = {
        "status": "plan",
        "target": "cloud",
        "deployment_id": deployment,
        "run": args.run,
        "source_sha": (receipt or {}).get("source_sha"),
        "cases": sorted(listing["cases"]),
        "files": len(transfers),
        "bytes": sum(meta["size"] for meta in expected.values()),
        "excluded": sorted(excluded),
        "local_directory": str(local_root),
        "archived_receipt_agrees": archived_agrees,
    }
    if not args.apply:
        return plan
    download(connection, transfers)
    mismatched = sorted(name for name, meta in expected.items() if REMOTE["digest"](local_root / name) != meta["sha256"])
    # The judge's own digest map is a second witness for the files it hashed. / 裁判自己的摘要图是它散列过的文件的第二见证。
    for case, row in rows.items():
        for name, value in (row.get("artifacts") or {}).items():
            key = f"{case}/{name}"
            if key in expected and key not in mismatched and REMOTE["digest"](local_root / key) != value:
                mismatched.append(key)
    local_root.mkdir(parents=True, exist_ok=True)
    if receipt is not None:
        (local_root / "receipt.json").write_text(json.dumps(receipt, indent=2), encoding="utf-8", newline="\n")
    record = plan | {
        "status": "fetched" if not mismatched else "digest_mismatch",
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "digests": {name: meta["sha256"] for name, meta in expected.items()},
        "mismatched": sorted(mismatched),
    }
    (local_root / "fetch.json").write_text(json.dumps(record, indent=2), encoding="utf-8", newline="\n")
    if mismatched:
        raise RuntimeError(f"{len(mismatched)} downloaded files differ from the recorded digests: {sorted(mismatched)[:5]}")
    if not args.no_viewer:
        from drone_agent.eval.viewer import build_page

        cases = [local_root / case for case in sorted(listing["cases"]) if (local_root / case / "input/scenario.json").is_file()]
        try:
            viewer_receipt = {**receipt, "deployment_id": deployment} if receipt else None
            record["viewer"] = str(build_page(cases, root=ROOT, output=local_root / "viewer.html", receipt=viewer_receipt))
        except Exception as error:  # noqa: BLE001 - a viewer defect must not undo a verified fetch / 浏览器缺陷不能推翻已核对的拉取
            record["viewer_error"] = f"{type(error).__name__}: {error}"
    return record


def desk_members(connection: Connection, *, apply: bool, stage: str | None = None) -> dict:
    """The desk member list: this tailnet user as operator, approver, admin and (P2) reviewer of every desk project
    (campus_s1 and, from P5, fleet_s0 and vendor_s3), the supervisor's read-only identity, and the P5 soak harness
    identities (one role each, plus a member of another project only). The login comes from the desk's own hello over
    the tailnet and is never printed. `stage="next"` stages the list for the next activation.

    任务台成员列表：当前 tailnet 用户为每个任务台项目（campus_s1，P5 起另有 fleet_s0 与 vendor_s3）的 operator、approver、
    admin 与（P2）reviewer，监管者的只读身份，以及 P5 长稳编排身份（各一个角色，另有一个只属于其他项目的成员）。登录名取自经
    tailnet 的任务台 hello，从不打印。`stage="next"` 把列表暂存给下一次激活。
    """
    status = ssh(connection, {"action": "desk_status"}, timeout=120)
    origin = status.get("origin") or ""
    if not re.fullmatch(r"https://[a-zA-Z0-9.-]+\.ts\.net:8448", origin):
        raise ValueError("the resident desk has no tailnet origin yet")
    probe = runpy.run_path(str(ROOT / "scripts/desk_probe.py"))
    client = probe["Client"](origin)
    try:
        identity = client.next("hello", 30).get("identity") or ""
    finally:
        client.close()
    if not identity.startswith("tailnet:") or len(identity) <= len("tailnet:"):
        raise ValueError("this device has no tailnet identity at the desk")
    projects = ("campus_s1", "fleet_s0", "vendor_s3")
    entries = [{"principal": identity, "project_id": p, "roles": ["operator", "approver", "admin", "reviewer"]}
               for p in projects]
    entries += [{"principal": identity, "project_id": "legacy_m2", "roles": ["viewer"]}]
    entries += [{"principal": "harness:desk-supervisor", "project_id": p, "roles": ["viewer"]}
                for p in (*projects, "legacy_m2")]
    for role in ("operator", "approver", "reviewer", "viewer"):
        entries += [{"principal": f"harness:soak-{role}", "project_id": p, "roles": [role]} for p in projects]
    entries += [{"principal": "harness:soak-other", "project_id": "vendor_s3", "roles": ["viewer"]}]
    members = {"format": "drone.project-members/v1", "members": entries}
    target = "secrets/desk/members.next.yaml" if stage == "next" else "secrets/desk/members.yaml"
    if not apply:
        return {"status": "plan", "entries": len(entries), "writes": f"{target} (0600)",
                "principal_schemes": ["harness", "tailnet"]}
    request = {"action": "desk_members", "run_id": new_run_id(), "members": members}
    if stage:
        request["stage"] = stage
    return ssh(connection, request, timeout=120)


def desk_operation(connection: Connection, args: argparse.Namespace) -> dict:
    """The P5 desk operations (D070, D073): a world change, the soak, a backup and the restore drill.

    P5 任务台运维（D070、D073）：世界变更、长稳、备份与恢复演练。
    """
    if args.command == "desk-soak" and args.status:
        return ssh(connection, {"action": "desk_soak_status"}, timeout=120)
    if args.command == "desk-world":
        request = {"action": "desk_world", "section": args.section, "asset": args.asset, "state": args.state,
                   "reason": args.reason}
    elif args.command == "desk-soak":
        action = "desk_soak_start" if args.start else "desk_soak_stop" if args.stop else "desk_soak_judge"
        request = {"action": action, "reason": args.reason}
        if args.hours is not None:
            request["hours"] = args.hours
        if args.soak_id:
            request["soak_id"] = args.soak_id
    else:
        request = {"action": "desk_backup" if args.command == "desk-backup" else "desk_restore_drill"}
    if not args.apply:
        return {"status": "plan", "request": request}
    return ssh(connection, {**request, "run_id": new_run_id()}, timeout=3600)


S2_DATASET = "visa_pcb_v1"


def data_command(connection: Connection, args: argparse.Namespace) -> dict:
    """Upload and install the frozen S2 dataset (D065): verified here against the committed manifest, packed as a
    deterministic archive of exactly the manifest's files, resumably uploaded, verified again file by file in the cloud.

    上传并安装冻结的 S2 数据集（D065）：先在本机按已提交清单核对，打包为只含清单文件的确定性归档，可续传上传，在云端再逐文件
    核对。
    """
    body = json.loads((ROOT / "eval/s2" / S2_DATASET / "manifest.json").read_text(encoding="utf-8"))
    data = args.data.resolve()
    files = sorted({sample["file"]: sample["sha256"] for sample in body["samples"]}.items())
    bad = [name for name, expected in files
           if not (data / name).is_file() or REMOTE["digest"](data / name) != expected]
    if bad:
        raise ValueError(f"the local dataset differs from the manifest ({len(bad)} files, e.g. {bad[0]})")
    run_id = args.resume or new_run_id()
    if not REMOTE["RUN_ID"].fullmatch(run_id):
        raise ValueError("invalid upload identity")
    if not str(args.artifacts.resolve()).isascii():
        raise ValueError("transport artifacts need an ASCII path")
    packet = args.artifacts.resolve() / f"p4-data-{run_id}"
    archive = packet / f"{S2_DATASET}.tar"
    if not archive.is_file():
        if args.resume:
            raise ValueError("nothing to resume: the local archive is missing")
        packet.mkdir(parents=True, exist_ok=False)
        with tarfile.open(archive, "w", format=tarfile.PAX_FORMAT) as stream:
            for name, _ in files:
                info = stream.gettarinfo(str(data / name), arcname=name)
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                info.mtime = 0
                info.mode = 0o644
                with (data / name).open("rb") as handle:
                    stream.addfile(info, handle)
    digest = REMOTE["digest"](archive)
    plan = {"status": "plan", "dataset": S2_DATASET, "files": len(files), "archive": str(archive),
            "bytes": archive.stat().st_size, "sha256": digest, "upload_id": run_id}
    if not args.apply:
        return plan
    state = ssh(connection, {"action": "status"})
    destination = state["workspace"] + "/incoming/" + run_id
    if not re.fullmatch(r"/home/[a-z_][a-z0-9_-]*/drone-agent/incoming/" + re.escape(run_id), destination):
        raise ValueError("unexpected remote upload destination")
    target = destination + "/" + archive.name
    if not args.resume:
        ssh(connection, {"action": "prepare", "run_id": run_id})
    # Resume appends to a partial copy; a fresh upload, or a resume before any byte arrived, starts from the top.
    # 续传在部分副本之后追加；全新上传或尚未收到任何字节时的续传从头开始。
    partial = bool(args.resume) and subprocess.run(
        ["sftp", "-b", "-", *connection.arguments(), connection.target], input=f"ls {sftp_literal(target)}\n".encode(),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=300).returncode == 0
    command = f"{'put -a' if partial else 'put'} {sftp_literal(str(archive))} {sftp_literal(target)}\n"
    print("Uploading the S2 dataset via resumable SFTP...", file=sys.stderr, flush=True)
    result = subprocess.run(["sftp", "-b", "-", *connection.arguments(), connection.target], input=command.encode(),
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=7200)
    if result.returncode:
        raise RuntimeError(f"SFTP upload failed; retry p4-data --resume {run_id} --apply; SSH stderr withheld")
    print("Verifying and installing the dataset...", file=sys.stderr, flush=True)
    return ssh(connection, {"action": "p4_data", "run_id": run_id, "sha256": digest}, timeout=1800)


def deploy_command(connection: Connection, args: argparse.Namespace) -> dict:
    state = ssh(connection, {"action": "status"})
    if args.resume:
        packet = args.resume.resolve()
        manifest = json.loads((packet / "manifest.json").read_text(encoding="utf-8"))
        REMOTE["validate_manifest"](manifest)
        if packet.name != manifest["run_id"]:
            raise ValueError("resume directory does not match run identity")
        for name, expected in manifest["files"].items():
            if REMOTE["digest"](packet / name) != expected:
                raise ValueError(f"resume file checksum mismatch: {name}")
        inputs = {name: manifest["files"][name] for name in REMOTE["CONTROL_FILES"]}
        inputs["remote_worker"] = hashlib.sha256(WORKER_CODE).hexdigest()
        if hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest() != manifest["control_sha256"]:
            raise ValueError("remote worker changed; prepare a new deployment")
    else:
        images = state["images"]
        if args.bootstrap_images:
            images = {}
            for name in IMAGE_REFS:
                details = json.loads(local(["docker", "image", "inspect", name]))[0]
                images[name] = {"id": details["Id"], "fingerprint": REMOTE["image_fingerprint"](details)}
        if set(images) != set(IMAGE_REFS):
            raise ValueError("first deployment needs --bootstrap-images and the verified M0 images")
        packet, manifest = prepare_packet(args.sha, args.artifacts, images)
    plan = {
        "status": "plan",
        "target": "cloud",
        "source_sha": manifest["source_sha"],
        "control_sha256": manifest["control_sha256"],
        "artifact_directory": str(packet),
        "workspace": state["workspace"],
        "capacity": state["capacity"],
        "resources": {"sitl_cpus": 1.5, "sitl_memory_gib": 2, "checks_cpus": 1, "checks_memory_gib": 1},
        "published_ports": [],
        "bootstrap_images": args.bootstrap_images,
    }
    if not args.apply:
        return plan
    if args.bootstrap_images and not args.resume:
        export_images(packet, manifest)
    destination = (
        (state["workspace"] + "/incoming/" + packet.name)
        if args.resume
        else ssh(
            connection,
            {"action": "prepare", "run_id": packet.name},
        )["incoming"]
    )
    upload_packet(connection, packet, manifest, destination, resume=bool(args.resume))
    print("Verifying and starting cloud workspace...", file=sys.stderr, flush=True)
    receipt = ssh(connection, {"action": "deploy", "run_id": packet.name})
    (packet / "deployment.json").write_text(json.dumps(receipt, indent=2), encoding="utf-8", newline="\n")
    return receipt | {"local_receipt": str(packet / "deployment.json")}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("target")
    for name in ("status", "verify", "test", "start", "stop", "logs", "runs"):
        commands.add_parser(name)
    console_parser = commands.add_parser("console", help="open a loopback browser entry to the cloud simulation")
    console_parser.add_argument("--port", type=int, default=8768)
    console_parser.add_argument("--artifacts", type=Path, default=Path(tempfile.gettempdir()) / "drone-agent-cloud")
    cloud_console_parser = commands.add_parser("console-cloud", help="plan or activate the private Tailnet console")
    cloud_console_action = cloud_console_parser.add_mutually_exclusive_group()
    cloud_console_action.add_argument("--apply", action="store_true")
    cloud_console_action.add_argument("--status", action="store_true")
    members_parser = commands.add_parser("desk-members", help="store the desk member list in cloud secrets (P1, D055)")
    members_parser.add_argument("--next", action="store_true",
                                help="stage the list for the next activation (needed when it names new projects)")
    members_parser.add_argument("--apply", action="store_true")
    world_parser = commands.add_parser("desk-world", help="change one asset of the desk world file (P5, D070)")
    world_parser.add_argument("--section", choices=["s0", "s1", "s3"], required=True)
    world_parser.add_argument("--asset", required=True)
    world_parser.add_argument("--state", choices=["normal", "damaged", "obstructed", "glare"], required=True)
    world_parser.add_argument("--reason", default="")
    world_parser.add_argument("--apply", action="store_true")
    soak_parser = commands.add_parser("desk-soak", help="start, follow, stop or judge the P5 soak (D073)")
    soak_action = soak_parser.add_mutually_exclusive_group(required=True)
    soak_action.add_argument("--start", action="store_true")
    soak_action.add_argument("--status", action="store_true")
    soak_action.add_argument("--stop", action="store_true")
    soak_action.add_argument("--judge", action="store_true")
    soak_parser.add_argument("--hours", type=float, help="a shorter rehearsal; the gate only accepts the full plan")
    soak_parser.add_argument("--soak-id", help="judge this soak instead of the current one")
    soak_parser.add_argument("--reason", default="")
    soak_parser.add_argument("--apply", action="store_true")
    backup_parser = commands.add_parser("desk-backup", help="take an online backup of the desk ledger (D073)")
    backup_parser.add_argument("--apply", action="store_true")
    drill_parser = commands.add_parser("desk-restore-drill", help="restore the latest backup offline and read it back")
    drill_parser.add_argument("--apply", action="store_true")
    desk_parser = commands.add_parser("desk-cloud", help="plan or activate the resident M2 mission desk (D035)")
    desk_action = desk_parser.add_mutually_exclusive_group()
    desk_action.add_argument("--apply", action="store_true")
    desk_action.add_argument("--status", action="store_true")
    fetch_parser = commands.add_parser("fetch", help="copy a recorded run locally and build the evidence viewer")
    fetch_parser.add_argument("--run", required=True, help="run directory name, e.g. m1-20260919T171218Z-db465377")
    fetch_parser.add_argument("--deployment", default=None, help="deployment id; defaults to the current one")
    fetch_parser.add_argument("--cases", default="all", help="comma-separated <scenario>-<seed> names or all")
    fetch_parser.add_argument("--exclude", default="", help="comma-separated case subfolders to skip, e.g. ulog,sensor")
    fetch_parser.add_argument("--receipt", type=Path, default=None, help="archived receipt to cross-check against")
    fetch_parser.add_argument("--artifacts", type=Path, default=Path(tempfile.gettempdir()) / "drone-agent-cloud")
    fetch_parser.add_argument("--apply", action="store_true")
    fetch_parser.add_argument("--no-viewer", action="store_true")
    m1_parser = commands.add_parser("m1")
    m1_parser.add_argument("--scenario", default="nominal")
    m1_parser.add_argument("--seeds", default="7,19,41")
    m1_parser.add_argument("--speed-factor", type=int, choices=[1, 2], default=1)
    key_parser = commands.add_parser("m2-key", help="store MINIMAX_API_KEY from this environment in cloud secrets")
    key_parser.add_argument("--apply", action="store_true")
    m2_parser = commands.add_parser("m2", help="run M2 end-to-end cases (natural language to report) in the cloud")
    m2_parser.add_argument("--scenario", default="all")
    m2_parser.add_argument("--seeds", default="7,19,41")
    m2_parser.add_argument("--planner", choices=["scripted", "live"], default="scripted",
                           help="scripted fixtures are labelled test doubles; live needs the model key in cloud secrets")
    m2_parser.add_argument("--transport", choices=["grpc", "zenoh"], default="grpc",
                           help="FleetTransport between the mission service and the uplink (D046)")
    p1_parser = commands.add_parser("p1", help="run P1 S1 cases (catalog, logical dock, claim gate, PX4 SITL) in the cloud")
    p1_parser.add_argument("--scenario", default="all", help="all or comma-separated S1 case ids")
    p1_parser.add_argument("--seeds", default="", help="comma-separated seeds; defaults to each case's own seeds")
    p1_parser.add_argument("--keep-going", action="store_true", help="run every selected case even after a failure")
    p2_parser = commands.add_parser("p2", help="run P2 S1 cases (workflows on the formal service, PX4 SITL) in the cloud")
    p2_parser.add_argument("--scenario", default="all", help="all or comma-separated S1 case ids")
    p2_parser.add_argument("--seeds", default="", help="comma-separated seeds; defaults to each case's own seeds")
    p2_parser.add_argument("--keep-going", action="store_true", help="run every selected case even after a failure")
    p3_parser = commands.add_parser("p3", help="run P3 S1 cases (two PX4 SITL aircraft under the scheduler) in the cloud")
    p3_parser.add_argument("--scenario", default="all", help="all or comma-separated S1 case ids")
    p3_parser.add_argument("--seeds", default="", help="comma-separated seeds; defaults to each case's own seeds")
    p3_parser.add_argument("--keep-going", action="store_true", help="run every selected case even after a failure")
    p4_parser = commands.add_parser("p4", help="run P4 S1 cases (the business loop on PX4 SITL) in the cloud")
    p4_parser.add_argument("--scenario", default="all", help="all or comma-separated S1 case ids")
    p4_parser.add_argument("--seeds", default="", help="comma-separated seeds; defaults to each case's own seeds")
    p4_parser.add_argument("--keep-going", action="store_true", help="run every selected case even after a failure")
    p5_parser = commands.add_parser("p5", help="run P5 S1 cases (the road obstacle template on PX4 SITL) in the cloud")
    p5_parser.add_argument("--scenario", default="all", help="all or comma-separated S1 case ids")
    p5_parser.add_argument("--seeds", default="", help="comma-separated seeds; defaults to each case's own seeds")
    p5_parser.add_argument("--keep-going", action="store_true", help="run every selected case even after a failure")
    p6_parser = commands.add_parser("p6", help="run P6 S1 cases (recapture on PX4 SITL, D078) in the cloud")
    p6_parser.add_argument("--scenario", default="all", help="all or comma-separated S1 case ids")
    p6_parser.add_argument("--seeds", default="", help="comma-separated seeds; defaults to each case's own seeds")
    p6_parser.add_argument("--keep-going", action="store_true", help="run every selected case even after a failure")
    data_parser = commands.add_parser("p4-data", help="upload and install the frozen S2 dataset in the cloud (D065)")
    data_parser.add_argument("--data", type=Path, required=True, help="the locally built dataset directory")
    data_parser.add_argument("--resume", help="upload id of an interrupted upload")
    data_parser.add_argument("--artifacts", type=Path, default=Path(tempfile.gettempdir()) / "drone-agent-cloud")
    data_parser.add_argument("--apply", action="store_true")
    s2_parser = commands.add_parser("p4-s2", help="run one S2 model evaluation in the cloud (D065)")
    s2_parser.add_argument("--split", choices=["calibration", "test"], required=True)
    s2_parser.add_argument("--mode", choices=["live", "replay", "scripted", "retrieval"], required=True,
                           help="live calls the model once per sample and records it; replay needs --replay-of; "
                                "retrieval reports the pinned CLIP on the test split")
    s2_parser.add_argument("--threshold", type=float, help="tau for a calibration run; test runs use the profile's")
    s2_parser.add_argument("--profile", help="configs/analysis/vlm_change_v<N>.yaml; defaults to the frozen v3")
    s2_parser.add_argument("--replay-of", help="<deployment id>/p4-<run id> of the live run to replay")
    s2_parser.add_argument("--limit", type=int, default=0, help="first N samples only (smoke runs)")
    s2_parser.add_argument("--concurrency", type=int, default=3)
    s2_parser.add_argument("--currency", default="CNY")
    s2_parser.add_argument("--price-input", type=float, default=0.0, help="per million input tokens")
    s2_parser.add_argument("--price-output", type=float, default=0.0, help="per million output tokens")
    s2_parser.add_argument("--price-source", default="", help="where the prices come from; empty reports no cost")
    m3_parser = commands.add_parser("m3", help="run M3-SITL scenarios (external mode, autonomy, recovery v2) in the cloud")
    m3_parser.add_argument("--scenario", default="ext_inspect", help="all, class:<name> or comma-separated ids")
    m3_parser.add_argument("--seeds", default="7,19,41")
    m3_parser.add_argument("--keep-going", action="store_true",
                           help="run every selected case even after a failure (diagnosis and measurement batches)")
    m3_parser.add_argument("--edge", choices=["on", "off"], default="on", help="event detector (D040 load tiers)")
    m3_parser.add_argument("--edge-period", type=float, help="event detector period in seconds; 0 = back to back")
    m3_parser.add_argument("--isolation", choices=["separate", "shared"], default="separate",
                           help="guardian on its own quota, or sharing one pinned core with the autonomy layer")
    deploy_parser = commands.add_parser("deploy")
    source = deploy_parser.add_mutually_exclusive_group()
    source.add_argument("--sha", default="HEAD")
    source.add_argument("--resume", type=Path)
    deploy_parser.add_argument("--apply", action="store_true")
    deploy_parser.add_argument("--bootstrap-images", action="store_true")
    deploy_parser.add_argument("--artifacts", type=Path, default=Path(tempfile.gettempdir()) / "drone-agent-cloud")
    args = parser.parse_args()
    try:
        if args.command == "target":
            result = {"target": "cloud", "policy": "D023", "local_stack_fallback": False}
        else:
            connection = Connection.from_environment()
            if args.command == "console-cloud":
                action = "console_status" if args.status else "console_apply" if args.apply else "console_plan"
                result = ssh(connection, {"action": action, "run_id": new_run_id()}, timeout=300)
                print(json.dumps(result, ensure_ascii=False, indent=2))
                return
            if args.command == "desk-members":
                result = desk_members(connection, apply=args.apply, stage="next" if args.next else None)
                print(json.dumps(result, ensure_ascii=False, indent=2))
                return
            if args.command in ("desk-world", "desk-soak", "desk-backup", "desk-restore-drill"):
                print(json.dumps(desk_operation(connection, args), ensure_ascii=False, indent=2))
                return
            if args.command == "desk-cloud":
                action = "desk_status" if args.status else "desk_apply" if args.apply else "desk_plan"
                # Activation may build the M2 images of the revision first. / 激活可能先构建该版本的 M2 镜像。
                result = ssh(connection, {"action": action, "run_id": new_run_id()}, timeout=3600)
                print(json.dumps(result, ensure_ascii=False, indent=2))
                return
            if args.command == "console":
                from drone_agent.console.server import serve_console

                def fetch_live(job):
                    return fetch_command(connection, argparse.Namespace(
                        run="m1-" + job["run_id"], deployment=job["deployment_id"], cases=job["case"],
                        exclude="ulog,sensor", receipt=None, artifacts=args.artifacts, apply=True, no_viewer=False,
                    ))

                serve_console(port=args.port, request=lambda value: ssh(connection, value, timeout=25), fetch=fetch_live)
                return
            if args.command == "deploy":
                result = deploy_command(connection, args)
            elif args.command == "fetch":
                result = fetch_command(connection, args)
            elif args.command == "m1":
                result = ssh(
                    connection,
                    {
                        "action": "m1",
                        "run_id": new_run_id(),
                        "scenario": args.scenario,
                        "seeds": [int(seed) for seed in args.seeds.split(",")],
                        "speed_factor": args.speed_factor,
                    },
                    timeout=14400,
                )
            elif args.command == "m3":
                result = ssh(
                    connection,
                    {
                        "action": "m3",
                        "run_id": new_run_id(),
                        "scenario": args.scenario,
                        "seeds": [int(seed) for seed in args.seeds.split(",")],
                        "keep_going": args.keep_going,
                        "edge": args.edge == "on",
                        "edge_period_s": args.edge_period,
                        "isolation": args.isolation,
                    },
                    timeout=21600,
                )
            elif args.command == "p4-data":
                result = data_command(connection, args)
            elif args.command == "p4-s2":
                request = {"action": "p4_s2", "run_id": new_run_id(), "split": args.split, "mode": args.mode,
                           "threshold": args.threshold, "replay_of": args.replay_of, "limit": args.limit,
                           "concurrency": args.concurrency, "profile": args.profile}
                if args.price_source:
                    request.update(currency=args.currency, price_input=args.price_input,
                                   price_output=args.price_output, price_source=args.price_source)
                result = ssh(connection, request, timeout=5 * 3600)
            elif args.command in ("p1", "p2", "p3", "p4", "p5", "p6"):
                result = ssh(
                    connection,
                    {
                        "action": args.command,
                        "run_id": new_run_id(),
                        "scenario": args.scenario,
                        "seeds": [int(seed) for seed in args.seeds.split(",") if seed],
                        "keep_going": args.keep_going,
                    },
                    timeout=14400,
                )
            elif args.command == "m2-key":
                # Read only from the process environment; never from a sibling project's .env (CLAUDE.md).
                # 只从进程环境读取；绝不读取姊妹项目的 .env（CLAUDE.md）。
                key = os.environ.get("MINIMAX_API_KEY", "")
                if not key:
                    raise ValueError("MINIMAX_API_KEY is not set in this environment")
                if not args.apply:
                    result = {"status": "plan", "target": "cloud", "writes": "secrets/m2-model/minimax.key (0600)"}
                else:
                    result = ssh(connection, {"action": "m2_key", "run_id": new_run_id(), "key": key})
            elif args.command == "m2":
                result = ssh(
                    connection,
                    {
                        "action": "m2",
                        "run_id": new_run_id(),
                        "scenario": args.scenario,
                        "seeds": [int(seed) for seed in args.seeds.split(",")],
                        "planner": args.planner,
                        "transport": args.transport,
                    },
                    timeout=14400,
                )
            else:
                result = ssh(connection, {"action": args.command, "run_id": new_run_id()})
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (ValueError, RuntimeError, OSError, subprocess.TimeoutExpired) as error:
        print(json.dumps({"status": "error", "reason": str(error)}, ensure_ascii=False))
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
