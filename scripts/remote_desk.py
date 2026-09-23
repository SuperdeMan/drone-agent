"""Plan, activate and check the resident M2 mission desk (D035): owned containers, one supervisor unit, one route.

Activation builds the M2 images of the deployed revision, provisions the desk's own trust root, starts the three
resident containers, installs the supervisor unit and adds the private Serve route 8448 -> 127.0.0.1:8769. It
verifies the page, the service and the supervisor on that revision, the containers' actual boundaries, and that
no other container or Serve entry (including D028's 8447) changed; on failure only these components are
restored. The model key is never read here: the service reads its own mounted file.

规划、激活并检查常驻 M2 任务台（D035）：本项目容器、一个监管者 unit、一个 Serve 映射。激活时构建所部署版本
的 M2 镜像，生成任务台自己的信任根，启动三个常驻容器，安装监管者 unit，并新增私有 Serve 映射 8448 ->
127.0.0.1:8769。它核对页面、服务与监管者都运行该版本、容器的实际边界，以及其他容器与 Serve 条目（含 D028
的 8447）没有变化；失败时只恢复这些组件。这里从不读取模型 key：服务读取它自己挂载的文件。
"""

from __future__ import annotations

import json
import os
import re
import runpy
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

HERE = Path(__file__).resolve().parent
HELPERS = runpy.run_path(str(HERE / "remote_dev_stack.py"))
CONSOLE = runpy.run_path(str(HERE / "remote_console.py"))
M2 = runpy.run_path(str(HERE / "remote_m2.py"))
SUPERVISOR = runpy.run_path(str(HERE / "desk_supervisor.py"))
UNIT = "drone-agent-desk-supervisor.service"
UNIT_PATH = Path("/etc/systemd/system") / UNIT
MARKER = "# Managed by drone-agent D035"
HTTPS_PORT = 8448
LISTENER = "127.0.0.1:8769"
BACKEND = "http://" + LISTENER
RESIDENTS = ("desk-model-proxy", "desk-service", "desk-uplink", "desk")
# The approved topology (D035, D036): only the model proxy joins the outbound network. / 只有模型代理接入出站网络。
NETWORKS = {"desk": {"desk_ingress"}, "desk-service": {"desk_uplink", "desk_model"}, "desk-uplink": {"desk_uplink"},
            "desk-model-proxy": {"desk_model", "desk_egress"}}
command, serve_config, fingerprint, read_json = (CONSOLE["command"], CONSOLE["serve_config"], CONSOLE["fingerprint"],
                                                 CONSOLE["read_json"])


def other_routes(config: dict) -> dict:
    return CONSOLE["other_routes"](config, port=HTTPS_PORT)


def route_matches(config: dict, origin: str) -> bool:
    return CONSOLE["route_matches"](config, origin, port=HTTPS_PORT, backend=BACKEND)


def image_names(tag: str) -> dict:
    """The images `remote_m2.build_images` builds for one revision. / `remote_m2.build_images` 为一个版本构建的镜像。"""
    return {"sim": f"drone-agent-m2-sim:{tag}", "aircraft": f"drone-agent-m1-aircraft:{tag}",
            "ground": f"drone-agent-m1-ground:{tag}"}


def unit_text(root: Path, deployment: Path, user: str) -> str:
    if not re.fullmatch(r"[a-z_][a-z0-9_-]*", user) or not re.fullmatch(r"/home/[a-z_][a-z0-9_-]*/drone-agent",
                                                                         root.as_posix()):
        raise ValueError("unexpected service owner or workspace")
    if deployment != root / "releases" / deployment.name or not HELPERS["RUN_ID"].fullmatch(deployment.name):
        raise ValueError("invalid service deployment")
    return f"""{MARKER}
[Unit]
Description=drone-agent resident mission desk simulation supervisor
After=docker.service
Requires=docker.service

[Service]
Type=simple
User={user}
WorkingDirectory={root.as_posix()}
Environment=PYTHONDONTWRITEBYTECODE=1
Environment=DOCKER_CONFIG={root.as_posix()}/desk/supervisor/docker-client
ExecStart=/usr/bin/python3 {deployment.as_posix()}/source/scripts/desk_supervisor.py
Restart=on-failure
RestartSec=5
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
    desk = SUPERVISOR["Desk"](root)
    previous = read_json(desk.base / "current.json")
    config = serve_config()
    occupied = str(HTTPS_PORT) in config.get("TCP", {}) or any(
        name.rsplit(":", 1)[-1] == str(HTTPS_PORT) for name in config.get("Web", {}))
    if occupied and (not previous or previous.get("origin") != origin or not route_matches(config, origin)):
        raise ValueError("the selected Serve port is not owned by the mission desk")
    if UNIT_PATH.is_symlink() or (UNIT_PATH.exists() and not UNIT_PATH.read_text().startswith(MARKER + "\n")):
        raise ValueError("refusing to overwrite a non-owned service unit")
    listeners = command(["ss", "-ltnH"]).stdout.splitlines()
    bound = [line.split()[3] for line in listeners if line.split()[3].endswith(":8769")]
    if bound and (not previous or bound != [LISTENER]):
        raise ValueError("backend port is already used or publicly bound")
    if not occupied and any(line.split()[3].endswith(f":{HTTPS_PORT}") for line in listeners):
        raise ValueError("the selected HTTPS port is already in use")
    if not (deployment / "source/sim/compose.desk.yaml").is_file():
        raise ValueError("deploy a committed mission-desk version first")
    manifest = read_json(deployment / "manifest.json")
    tag = manifest["source_sha"] + "-" + manifest["control_sha256"][:12]
    return {"status": "plan", "source_sha": manifest["source_sha"], "control_sha256": manifest["control_sha256"],
            "deployment_id": deployment.name, "origin": origin, "listener": LISTENER, "service_unit": UNIT,
            "images": image_names(tag), "planner": "live" if (desk.model / "minimax.key").is_file() else "scripted",
            "serve_other_routes_sha256": fingerprint(other_routes(config)), "funnel": False, "a2a_clients": 0,
            "authorization": "existing tailnet access; writes need the Serve login header (D033)",
            "apply_required": True}


def probe(origin: str) -> dict:
    request = urllib.request.Request(BACKEND + "/health", headers={"Host": urlsplit(origin).netloc})
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        return json.load(error)


def planner_matches(expected: str, label: str | None) -> bool:
    return (label or "").startswith("live:") if expected == "live" else label == "scripted"


def inspect_desk(desk, source_sha: str) -> dict:
    """The resident containers' actual publication, networks, mounts and privileges. / 常驻容器的实际发布、网络、挂载与权限。"""
    project, found = HELPERS["PROJECT"], {}
    for service in RESIDENTS:
        ids = command(["docker", "ps", "-aq", "--filter", f"label=com.docker.compose.project={project}",
                       "--filter", f"label=com.docker.compose.service={service}"]).stdout.split()
        if len(ids) != 1:
            raise ValueError(f"expected exactly one owned {service} container")
        details = json.loads(command(["docker", "inspect", ids[0]]).stdout)[0]
        config, host = details["Config"], details["HostConfig"]
        if not details["State"].get("Running"):
            raise ValueError(f"{service} is not running")
        if (config.get("User") != f"{os.getuid()}:{os.getgid()}" or host.get("CapDrop") != ["ALL"]
                or not host.get("ReadonlyRootfs") or host.get("Privileged")):
            raise ValueError(f"{service} privilege boundary differs")
        networks = set(details["NetworkSettings"].get("Networks", {}))
        mounts = {m["Destination"]: (m["Source"], m["RW"]) for m in details["Mounts"] if m["Type"] == "bind"}
        if any("docker.sock" in source or "/.ssh" in source for source, _ in mounts.values()):
            raise ValueError(f"{service} mounts a host control path")
        if networks != {f"{project}_{name}" for name in NETWORKS[service]}:
            raise ValueError(f"{service} networks differ from the approved topology")
        if service == "desk":
            ports = {"8769/tcp": [{"HostIp": "127.0.0.1", "HostPort": "8769"}]}
            if host.get("PortBindings") != ports or details["NetworkSettings"].get("Ports") != ports:
                raise ValueError("desk public binding differs")
            if mounts != {"/api": (str(desk.base / "api"), False), "/supervisor": (str(desk.public), False)}:
                raise ValueError("desk mounts differ from the approved scope")
            if (config.get("Labels") or {}).get("io.drone-agent.source-sha") != source_sha:
                raise ValueError("desk container revision differs")
        elif host.get("PortBindings") or details["NetworkSettings"].get("Ports"):
            raise ValueError(f"{service} must not publish ports")
        if service == "desk-model-proxy" and mounts:
            raise ValueError("the model proxy mounts nothing")
        found[service] = {"container_id": details["Id"], "image_id": details["Image"], "user": config["User"],
                          "networks": sorted(networks), "read_only": True, "docker_access": False}
    return found


def status(root: Path) -> dict:
    desk = SUPERVISOR["Desk"](root)
    current = read_json(desk.base / "current.json")
    if current is None:
        return {"status": "not_deployed"}
    try:
        health = probe(current["origin"])
    except (OSError, ValueError):
        health = {"ok": False}
    result = health.get("result") or {}
    active = command(["systemctl", "is-active", UNIT], check=False).returncode == 0
    route = route_matches(serve_config(), current["origin"])
    revision = health.get("console_source_sha") == current["source_sha"] == result.get("source_sha")
    ready = bool(health.get("ok")) and result.get("status") == "ready" and active and route and revision
    return {**{k: current.get(k) for k in ("source_sha", "control_sha256", "deployment_id", "origin", "listener",
                                          "planner", "signer_key_id")},
            "status": "ready" if ready else "unhealthy", "planner_label": result.get("planner"),
            "supervisor_active": active, "tailnet_only_route": route, "revision_matches": revision,
            "supervisor": read_json(desk.public / "status.json"),
            "project_deployment_id": HELPERS["current"](root).name}


def apply(root: Path, deployment: Path, request: dict) -> dict:
    """Activate and verify owned components; on failure restore only these components.

    激活并验证本项目组件；失败时只恢复这些组件。
    """
    import pwd

    value = plan(root, deployment)
    command(["sudo", "-n", "true"])
    desk = SUPERVISOR["Desk"](root)
    old = read_json(desk.base / "current.json")
    old_unit = UNIT_PATH.read_text() if UNIT_PATH.exists() else None
    old_active = command(["systemctl", "is-active", UNIT], check=False).returncode == 0
    before_serve, before_other = serve_config(), HELPERS["foreign_identity"]()
    if desk.base.is_symlink() or (desk.base.exists() and not desk.base.resolve().is_relative_to(root.resolve())):
        raise ValueError("invalid desk workspace")
    for folder in desk.folders():
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    artifact = desk.base / "deployments" / request["run_id"]
    artifact.mkdir()
    (artifact / "serve-before.json").write_text(json.dumps(before_serve))
    manifest = read_json(deployment / "manifest.json")
    tag = manifest["source_sha"] + "-" + manifest["control_sha256"][:12]
    checks = f"drone-agent-checks:{tag}"
    if HELPERS["inspect_image"](checks) is None:
        raise ValueError("the deployment's checks image is missing; deploy the revision again")
    images = M2["build_images"](deployment / "source", artifact, tag, checks)
    keys = M2["provision"](root, images["ground"], name="desk")
    record = {"schema_version": "0.1.0", "source_sha": value["source_sha"], "control_sha256": value["control_sha256"],
              "deployment_id": deployment.name, "origin": value["origin"], "listener": LISTENER, "images": images,
              "planner": value["planner"], "uid": os.getuid(), "gid": os.getgid(),
              "signer_key_id": keys["signer_key_id"], "certificate_fingerprints": keys["fingerprints"],
              "activated_at": datetime.now(timezone.utc).isoformat()}
    stack = SUPERVISOR["Stack"](desk, record, log=artifact / "compose.log")
    candidate = artifact / UNIT
    candidate.write_text(unit_text(root, deployment, pwd.getpwuid(os.getuid()).pw_name))
    added_route = str(HTTPS_PORT) not in before_serve.get("TCP", {})
    unit_changed = residents_changed = False
    try:
        SUPERVISOR["write_json"](desk.base / "current.json", record)
        command(["sudo", "-n", "install", "-m", "0644", str(candidate), str(UNIT_PATH)])
        unit_changed = True
        command(["sudo", "-n", "systemctl", "daemon-reload"])
        command(["sudo", "-n", "systemctl", "enable", UNIT])
        residents_changed = True
        stack("up", "-d", "--no-build", "--pull", "never", "--force-recreate", *RESIDENTS, timeout=240)
        command(["sudo", "-n", "systemctl", "restart", UNIT])
        deadline = time.monotonic() + 120
        while True:
            try:
                health = probe(value["origin"])
                result = health.get("result") or {}
                if health.get("ok") and health.get("console_source_sha") == value["source_sha"] == result.get("source_sha"):
                    break
            except (OSError, ValueError):
                pass
            if time.monotonic() >= deadline:
                raise RuntimeError("desk page and mission service did not become ready on the requested revision")
            time.sleep(2)
        if not planner_matches(value["planner"], result.get("planner")):
            raise RuntimeError(f"planner mode differs from the plan: {value['planner']}")
        if command(["systemctl", "is-active", UNIT], check=False).returncode:
            raise RuntimeError("the desk supervisor is not active")
        command(["sudo", "-n", "tailscale", "serve", "--bg", f"--https={HTTPS_PORT}", BACKEND])
        after_serve = serve_config()
        if not route_matches(after_serve, value["origin"]) or other_routes(after_serve) != other_routes(before_serve):
            raise RuntimeError("Serve boundary or unrelated routes changed")
        after_other = HELPERS["foreign_identity"]()
        if before_other != after_other:
            raise RuntimeError("unrelated container identities changed")
        receipt = {"status": "verified", **{k: record[k] for k in ("source_sha", "control_sha256", "deployment_id",
                                                                    "origin", "listener", "images", "planner",
                                                                    "signer_key_id", "certificate_fingerprints")},
                   "planner_label": result.get("planner"), "health": health, "funnel": False,
                   "containers": inspect_desk(desk, value["source_sha"]),
                   "other_serve_before_sha256": fingerprint(other_routes(before_serve)),
                   "other_serve_after_sha256": fingerprint(other_routes(after_serve)),
                   "other_containers_before": before_other, "other_containers_after": after_other,
                   "supervisor_unit_sha256": HELPERS["digest"](candidate)}
        (artifact / "receipt.json").write_text(json.dumps(receipt, indent=2))
        return receipt
    except Exception:
        if added_route and route_matches(serve_config(), value["origin"]):
            command(["sudo", "-n", "tailscale", "serve", f"--https={HTTPS_PORT}", "off"], check=False)
        if residents_changed:
            stack("stop", *RESIDENTS, check=False)
        if unit_changed and old_unit:
            candidate.write_text(old_unit)
            command(["sudo", "-n", "install", "-m", "0644", str(candidate), str(UNIT_PATH)], check=False)
            command(["sudo", "-n", "systemctl", "daemon-reload"], check=False)
            command(["sudo", "-n", "systemctl", "restart" if old_active else "stop", UNIT], check=False)
        elif unit_changed:
            command(["sudo", "-n", "systemctl", "disable", "--now", UNIT], check=False)
        if old:
            SUPERVISOR["write_json"](desk.base / "current.json", old)
            if residents_changed:
                SUPERVISOR["Stack"](desk, old, log=artifact / "compose.log")(
                    "up", "-d", "--no-build", "--pull", "never", *RESIDENTS, check=False, timeout=240)
        else:
            (desk.base / "current.json").unlink(missing_ok=True)
        raise
