"""Install only the owned console service and one private Tailscale Serve route.

仅安装本项目控制台服务和一个私有 Tailscale Serve 映射。
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import runpy
import subprocess
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

HELPERS = runpy.run_path(str(Path(__file__).with_name("remote_dev_stack.py")))
UNIT = "drone-agent-console-broker.service"
UNIT_PATH = Path("/etc/systemd/system") / UNIT
MARKER = "# Managed by drone-agent D028"
HTTPS_PORT = 8447
BACKEND = "http://127.0.0.1:8768"


def command(argv: list[str], *, env=None, check=True, timeout=60):
    result = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=timeout)
    if check and result.returncode:
        # Management errors report the operation, not raw daemon output or URLs. / 管理错误仅报告动作，不输出守护进程原文或链接。
        raise RuntimeError(f"console management command failed: {argv[0]} (exit {result.returncode})")
    return result


def read_json(path: Path):
    return json.loads(path.read_text()) if path.is_file() else None


def fingerprint(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def serve_config() -> dict:
    return json.loads(command(["tailscale", "serve", "status", "--json"]).stdout)


def other_routes(config: dict, port: int = HTTPS_PORT) -> dict:
    """Every Serve entry except the one on `port` (the mission desk passes 8448). / 除 `port` 之外的全部 Serve 条目。"""
    value = copy.deepcopy(config)
    value.get("TCP", {}).pop(str(port), None)
    for section in ("Web", "AllowFunnel"):
        for name in list(value.get(section, {})):
            if name.rsplit(":", 1)[-1] == str(port):
                value[section].pop(name)
        if not value.get(section):
            value.pop(section, None)
    return value


def route_matches(config: dict, origin: str, port: int = HTTPS_PORT, backend: str = BACKEND) -> bool:
    authority = urlsplit(origin).netloc
    return (
        config.get("TCP", {}).get(str(port), {}).get("HTTPS") is True
        and config.get("Web", {}).get(authority, {}).get("Handlers") == {"/": {"Proxy": backend}}
        and config.get("AllowFunnel", {}).get(authority, False) is False
    )


def environment(root: Path, deployment: Path, origin: str) -> dict:
    manifest = read_json(deployment / "manifest.json")
    tag = manifest["source_sha"] + "-" + manifest["control_sha256"][:12]
    return dict(os.environ, DRONE_CONSOLE_ROOT=str(root), DRONE_CONSOLE_IMAGE="drone-agent-checks:" + tag,
                DRONE_CONSOLE_SHA=manifest["source_sha"], DRONE_CONSOLE_ORIGIN=origin,
                DRONE_CONSOLE_UID=str(os.getuid()), DRONE_CONSOLE_GID=str(os.getgid()))


def compose(root: Path, deployment: Path, origin: str, *args):
    return command(["docker", "compose", "-p", "drone-agent-cloud", "-f",
                    str(deployment / "source/sim/compose.console.yaml"), *args],
                   env=environment(root, deployment, origin), timeout=120)


def inspect_console(root: Path, *, source_sha: str | None = None) -> dict:
    ids = command(["docker", "ps", "-aq", "--filter", "label=com.docker.compose.project=drone-agent-cloud",
                   "--filter", "label=com.docker.compose.service=console"]).stdout.split()
    if len(ids) != 1:
        raise ValueError("expected exactly one owned console container")
    details = json.loads(command(["docker", "inspect", ids[0]]).stdout)[0]
    config, host = details["Config"], details["HostConfig"]
    if not details["State"].get("Running"):
        raise ValueError("console container is not running")
    labels = config.get("Labels") or {}
    if labels.get("io.drone-agent.component") != "tailnet-console":
        raise ValueError("console container ownership marker differs")
    if source_sha and labels.get("io.drone-agent.source-sha") != source_sha:
        raise ValueError("console container revision differs")
    expected_ports = {"8768/tcp": [{"HostIp": "127.0.0.1", "HostPort": "8768"}]}
    if (
        host.get("PortBindings") != expected_ports
        or details["NetworkSettings"].get("Ports") != expected_ports
        or not host.get("ReadonlyRootfs") or host.get("Privileged")
    ):
        raise ValueError("console public binding or filesystem boundary differs")
    if set(details["NetworkSettings"].get("Networks", {})) != {"drone-agent-cloud_console_ingress"}:
        raise ValueError("console must use only its dedicated ingress network")
    if config.get("User") != f"{os.getuid()}:{os.getgid()}" or host.get("CapDrop") != ["ALL"]:
        raise ValueError("console process privilege boundary differs")
    expected_mounts = {
        "/broker": (str(root / "console/ipc"), False), "/records": (str(root / "artifacts"), False),
        "/releases": (str(root / "releases"), False), "/outputs": (str(root / "console/pages"), True),
    }
    mounts = {m["Destination"]: (m["Source"], m["RW"]) for m in details["Mounts"] if m["Type"] == "bind"}
    if mounts != expected_mounts:
        raise ValueError("console data mounts differ from the approved scope")
    return {"container_id": details["Id"], "image_id": details["Image"], "running": details["State"]["Running"],
            "published_ports": expected_ports, "read_only": True, "user": config["User"], "docker_access": False}


def unit_text(root: Path, deployment: Path, user: str) -> str:
    if not re.fullmatch(r"[a-z_][a-z0-9_-]*", user) or not re.fullmatch(r"/home/[a-z_][a-z0-9_-]*/drone-agent", root.as_posix()):
        raise ValueError("unexpected service owner or workspace")
    if deployment != root / "releases" / deployment.name or not HELPERS["RUN_ID"].fullmatch(deployment.name):
        raise ValueError("invalid service deployment")
    return f"""{MARKER}
[Unit]
Description=drone-agent private console broker
After=docker.service
Requires=docker.service

[Service]
Type=simple
User={user}
WorkingDirectory={root.as_posix()}
Environment=PYTHONDONTWRITEBYTECODE=1
Environment=DOCKER_CONFIG={root.as_posix()}/console/docker-client
ExecStart=/usr/bin/python3 {deployment.as_posix()}/source/scripts/console_broker.py
Restart=on-failure
RestartSec=2
KillMode=process
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths={root.as_posix()}

[Install]
WantedBy=multi-user.target
"""


def plan(root: Path, deployment: Path) -> dict:
    tailscale = json.loads(command(["tailscale", "status", "--json"]).stdout)
    if tailscale.get("BackendState") != "Running" or not tailscale.get("Self", {}).get("Online"):
        raise ValueError("Tailscale must already be connected")
    host = tailscale["Self"].get("DNSName", "").rstrip(".")
    if not re.fullmatch(r"[a-zA-Z0-9.-]+\.ts\.net", host):
        raise ValueError("valid tailnet HTTPS name required")
    origin = f"https://{host}:{HTTPS_PORT}"
    previous = read_json(root / "console/current.json")
    config = serve_config()
    occupied = str(HTTPS_PORT) in config.get("TCP", {}) or any(
        name.rsplit(":", 1)[-1] == str(HTTPS_PORT) for name in config.get("Web", {})
    )
    if occupied and (not previous or previous.get("origin") != origin or not route_matches(config, origin)):
        raise ValueError("the selected Serve port is not owned by this console")
    if UNIT_PATH.is_symlink() or (UNIT_PATH.exists() and not UNIT_PATH.read_text().startswith(MARKER + "\n")):
        raise ValueError("refusing to overwrite a non-owned service unit")
    listeners = command(["ss", "-ltnH"]).stdout.splitlines()
    bound = [line.split()[3] for line in listeners if line.split()[3].endswith(":8768")]
    if bound and (not previous or bound != ["127.0.0.1:8768"]):
        raise ValueError("backend port is already used or publicly bound")
    if not occupied and any(line.split()[3].endswith(f":{HTTPS_PORT}") for line in listeners):
        raise ValueError("the selected HTTPS port is already in use")
    manifest = read_json(deployment / "manifest.json")
    if not (deployment / "source/sim/compose.console.yaml").is_file():
        raise ValueError("deploy a committed cloud-console version first")
    return {"status": "plan", "source_sha": manifest["source_sha"], "control_sha256": manifest["control_sha256"],
            "deployment_id": deployment.name, "origin": origin, "listener": "127.0.0.1:8768",
            "service_unit": UNIT, "image": environment(root, deployment, origin)["DRONE_CONSOLE_IMAGE"],
            "serve_other_routes_sha256": fingerprint(other_routes(config)), "funnel": False,
            "authorization": "existing tailnet access; no application login", "apply_required": True}


def probe(origin: str) -> dict:
    request = urllib.request.Request(BACKEND + "/health", headers={"Host": urlsplit(origin).netloc})
    with urllib.request.urlopen(request, timeout=6) as response:
        return json.load(response)


def status(root: Path) -> dict:
    current = read_json(root / "console/current.json")
    if current is None:
        return {"status": "not_deployed"}
    config = serve_config()
    try:
        health = probe(current["origin"])
    except (OSError, ValueError):
        health = {"status": "unavailable"}
    active = command(["systemctl", "is-active", UNIT], check=False).returncode == 0
    revision_matches = health.get("console_source_sha") == current["source_sha"] == health.get("runtime_source_sha")
    return {**current, "status": "ready" if health.get("status") == "ready" and active and revision_matches and route_matches(config, current["origin"]) else "unhealthy",
            "health": health, "tailnet_only_route": route_matches(config, current["origin"]), "broker_active": active}


def apply(root: Path, deployment: Path, request: dict) -> dict:
    """Activate and verify owned components, restoring only these components on failure.

    激活并验证本项目组件；失败时仅恢复这些组件。
    """
    import pwd

    value = plan(root, deployment)
    command(["sudo", "-n", "true"])
    old = read_json(root / "console/current.json")
    old_unit = UNIT_PATH.read_text() if UNIT_PATH.exists() else None
    old_active = command(["systemctl", "is-active", UNIT], check=False).returncode == 0
    before_serve = serve_config()
    before_other = HELPERS["foreign_identity"]()
    directory = root / "console"
    if directory.is_symlink() or not directory.resolve().is_relative_to(root.resolve()):
        raise ValueError("invalid console workspace")
    for subdir in ("ipc", "pages", "deployments", "docker-client"):
        (directory / subdir).mkdir(parents=True, exist_ok=True, mode=0o700)
    artifact = directory / "deployments" / request["run_id"]
    artifact.mkdir()
    (artifact / "serve-before.json").write_text(json.dumps(before_serve))
    candidate = artifact / UNIT
    candidate.write_text(unit_text(root, deployment, pwd.getpwuid(os.getuid()).pw_name))
    added_route = str(HTTPS_PORT) not in before_serve.get("TCP", {})
    unit_changed = container_changed = False
    try:
        command(["sudo", "-n", "install", "-m", "0644", str(candidate), str(UNIT_PATH)])
        unit_changed = True
        command(["sudo", "-n", "systemctl", "daemon-reload"])
        command(["sudo", "-n", "systemctl", "enable", UNIT])
        command(["sudo", "-n", "systemctl", "restart", UNIT])
        container_changed = True
        compose(root, deployment, value["origin"], "up", "-d", "--no-build", "--pull", "never", "--no-deps", "console")
        deadline = time.monotonic() + 60
        while True:
            try:
                health = probe(value["origin"])
                if health["console_source_sha"] == value["source_sha"] and health["runtime_source_sha"] == value["source_sha"]:
                    break
            except (OSError, ValueError):
                pass
            if time.monotonic() >= deadline:
                raise RuntimeError("console and broker did not become ready on the requested revision")
            time.sleep(1)
        command(["sudo", "-n", "tailscale", "serve", "--bg", f"--https={HTTPS_PORT}", BACKEND])
        after_serve = serve_config()
        if not route_matches(after_serve, value["origin"]) or other_routes(after_serve) != other_routes(before_serve):
            raise RuntimeError("Serve boundary or unrelated routes changed")
        after_other = HELPERS["foreign_identity"]()
        if before_other != after_other:
            raise RuntimeError("unrelated container identities changed")
        container = inspect_console(root, source_sha=value["source_sha"])
        current = {k: value[k] for k in ("source_sha", "control_sha256", "deployment_id", "origin", "listener", "image")}
        pending = directory / "current.pending.json"
        pending.write_text(json.dumps(current))
        pending.replace(directory / "current.json")
        receipt = {"status": "verified", **current, "health": health, "funnel": False,
                   "container": container,
                   "other_serve_before_sha256": fingerprint(other_routes(before_serve)),
                   "other_serve_after_sha256": fingerprint(other_routes(after_serve)),
                   "other_containers_before": before_other, "other_containers_after": after_other,
                   "broker_unit_sha256": hashlib.sha256(candidate.read_bytes()).hexdigest()}
        (artifact / "receipt.json").write_text(json.dumps(receipt, indent=2))
        return receipt
    except Exception:
        if added_route and route_matches(serve_config(), value["origin"]):
            command(["sudo", "-n", "tailscale", "serve", f"--https={HTTPS_PORT}", "off"], check=False)
        if container_changed:
            compose(root, deployment, value["origin"], "stop", "console")
        if unit_changed and old_unit:
            candidate.write_text(old_unit)
            command(["sudo", "-n", "install", "-m", "0644", str(candidate), str(UNIT_PATH)])
            command(["sudo", "-n", "systemctl", "daemon-reload"])
            command(["sudo", "-n", "systemctl", "restart" if old_active else "stop", UNIT])
        elif unit_changed:
            command(["sudo", "-n", "systemctl", "disable", "--now", UNIT], check=False)
        if old and container_changed:
            compose(root, root / "releases" / old["deployment_id"], old["origin"],
                    "up", "-d", "--no-build", "--pull", "never", "--no-deps", "console")
        raise
