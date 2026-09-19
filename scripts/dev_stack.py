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
        return ["-i", str(self.identity), "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
                "-o", "ConnectTimeout=15", "-o", "ServerAliveInterval=20", "-o", "ServerAliveCountMax=3"]

    @property
    def target(self) -> str:
        return self.user + "@" + self.host


def ssh(connection: Connection, request: dict, *, timeout: int = 1800) -> dict:
    suffix = (
        "\ntry:\n    result = dispatch(json.loads(" + repr(json.dumps(request)) + "))\n"
        "    print(json.dumps(result))\n"
        "except Exception as exc:\n    print(json.dumps({'status': 'error', 'reason': str(exc)}))\n    raise SystemExit(1)\n"
    )
    result = subprocess.run(["ssh", *connection.arguments(), connection.target, "python3 -"],
                            input=WORKER_CODE + suffix.encode(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    # SSH banners can contain transient login URLs; never print raw stderr.
    # SSH banner 可能带临时登录链接；不打印原始 stderr。
    try:
        payload = json.loads(result.stdout)
    except (ValueError, UnicodeError) as error:
        raise RuntimeError(f"cloud command returned no valid JSON (exit {result.returncode}); SSH stderr withheld") from error
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
    requirements = local(["uv", "export", "--frozen", "--group", "dev", "--group", "flight", "--no-emit-project", "--no-header",
                          "--format", "requirements.txt"], cwd=source, env=env)
    (packet / "requirements.txt").write_bytes(requirements)
    for name in ("compose.cloud.yaml", "checks.Dockerfile"):
        shutil.copyfile(ROOT / "sim" / name, packet / name)
    files = {name: REMOTE["digest"](packet / name) for name in ("source.tar", *REMOTE["CONTROL_FILES"])}
    control_inputs = {name: files[name] for name in REMOTE["CONTROL_FILES"]}
    control_inputs["remote_worker"] = hashlib.sha256(WORKER_CODE).hexdigest()
    manifest = {"schema_version": "0.1.0", "source_sha": sha, "run_id": packet.name, "files": files,
                "control_sha256": hashlib.sha256(json.dumps(control_inputs, sort_keys=True).encode()).hexdigest(),
                "images": images, "target": "cloud"}
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
    result = subprocess.run(["sftp", "-b", "-", *connection.arguments(), connection.target],
                            input=("\n".join(commands) + "\n").encode(), stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, timeout=7200)
    if result.returncode:
        raise RuntimeError(f"SFTP upload failed; retry deploy --resume {packet} --apply; SSH stderr withheld")


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
    plan = {"status": "plan", "target": "cloud", "source_sha": manifest["source_sha"],
            "control_sha256": manifest["control_sha256"], "artifact_directory": str(packet),
            "workspace": state["workspace"], "capacity": state["capacity"],
            "resources": {"sitl_cpus": 1.5, "sitl_memory_gib": 2, "checks_cpus": 1, "checks_memory_gib": 1},
            "published_ports": [], "bootstrap_images": args.bootstrap_images}
    if not args.apply:
        return plan
    if args.bootstrap_images and not args.resume:
        export_images(packet, manifest)
    destination = (state["workspace"] + "/incoming/" + packet.name) if args.resume else ssh(
        connection, {"action": "prepare", "run_id": packet.name},
    )["incoming"]
    upload_packet(connection, packet, manifest, destination, resume=bool(args.resume))
    print("Verifying and starting cloud workspace...", file=sys.stderr, flush=True)
    receipt = ssh(connection, {"action": "deploy", "run_id": packet.name})
    (packet / "deployment.json").write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    return receipt | {"local_receipt": str(packet / "deployment.json")}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("target")
    for name in ("status", "verify", "test", "start", "stop", "logs"):
        commands.add_parser(name)
    m1_parser = commands.add_parser("m1")
    m1_parser.add_argument("--scenario", default="nominal")
    m1_parser.add_argument("--seeds", default="7,19,41")
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
            if args.command == "deploy":
                result = deploy_command(connection, args)
            elif args.command == "m1":
                result = ssh(connection, {"action": "m1", "run_id": new_run_id(), "scenario": args.scenario,
                                          "seeds": [int(seed) for seed in args.seeds.split(",")]}, timeout=14400)
            else:
                result = ssh(connection, {"action": args.command, "run_id": new_run_id()})
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (ValueError, RuntimeError, OSError, subprocess.TimeoutExpired) as error:
        print(json.dumps({"status": "error", "reason": str(error)}, ensure_ascii=False))
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
