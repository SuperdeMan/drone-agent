"""Plan, activate and check the resident M2 mission desk (D035): owned containers, one supervisor unit, one route.

Activation builds the M2 images of the deployed revision, provisions the desk's own trust root, starts the three
resident containers, installs the supervisor unit and adds the private Serve route 8448 -> 127.0.0.1:8769. It
verifies the page, the service and the supervisor on that revision, the containers' actual boundaries, and that
no other container or Serve entry (including D028's 8447) changed; on failure only these components are
restored. The model key is never read here: the service reads its own mounted file. P1 (D055/D056): activation
needs the desk member list in the desk secrets, runs the ledger migration drill on a copy of the live ledger before
anything is switched (only counts and digests are kept), starts the resident dock backend, and records the real
migration the service performed on start. P2 (D057/D058): the same copy then runs the workflow-extension drill, the
service loads the workflow catalog, and activation also requires its migration on start. P3 (D059/D060): the same
copy then runs the scheduling-extension drill, the service loads the p3_desk_v1 scheduling catalog, and activation
also requires that migration on start. P4 (D063/D064): the same copy then runs the business-extension drill; the
service loads the p4_s1_v1 workflow catalog (the P2 templates byte for byte plus the P4 ones) and the p4_s1_v1 business
catalog with the live vision role, and activation also requires the business migration on start. P5 (D069-D073): the
desk is the platform v0.1. Activation builds the P5 simulation image (the M2 world plus the road marking), issues client
certificates for the two logical aircraft, runs the catalog switch drill on the same copy (the p5_desk_v1 catalogs load
against the copied tables, the latest records render, only catalog rows are added), starts the logical fleet and the
vendor protocol simulator as residents with their exact boundaries, and requires the service to start with the four
p5_desk_v1 catalogs, three execution backends and the vendor link, and every dock session to become active.

规划、激活并检查常驻 M2 任务台（D035）：本项目容器、一个监管者 unit、一个 Serve 映射。激活时构建所部署版本
的 M2 镜像，生成任务台自己的信任根，启动三个常驻容器，安装监管者 unit，并新增私有 Serve 映射 8448 ->
127.0.0.1:8769。它核对页面、服务与监管者都运行该版本、容器的实际边界，以及其他容器与 Serve 条目（含 D028
的 8447）没有变化；失败时只恢复这些组件。这里从不读取模型 key：服务读取它自己挂载的文件。P1（D055/D056）：激活
要求任务台 secrets 中有成员列表，在切换任何组件之前先对当前账本的副本执行迁移演练（只保留计数与摘要），启动常驻
机场后端，并记录服务启动时实际执行的迁移。P2（D057/D058）：同一副本随后执行工作流扩展演练，服务加载工作流目录，激活也
要求服务启动时完成其迁移。P3（D059/D060）：同一副本再执行调度扩展演练，服务加载 p3_desk_v1 调度目录，激活同样要求服务
启动时完成该迁移。P4（D063/D064）：同一副本再执行业务扩展演练；服务加载 p4_s1_v1 工作流目录（逐字节保留 P2 模板并加上 P4
模板）与带实调视觉角色的 p4_s1_v1 业务目录，激活同样要求服务启动时完成业务迁移。P5（D069–D073）：任务台即平台 v0.1。激活时
构建 P5 仿真镜像（M2 世界加道路标线），为两台逻辑飞行器签发客户端证书，在同一副本上执行目录切换演练（p5_desk_v1 目录在复制的
表上加载、最近的记录可以渲染、只增加目录行），以各自的确切边界启动常驻逻辑机队与厂商协议模拟器，并要求服务以四个 p5_desk_v1
目录、三种执行后端与厂商链路启动，且每个机场会话都变为活动。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import runpy
import shutil
import sqlite3
import subprocess
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
P5 = runpy.run_path(str(HERE / "remote_p5.py"))
UNIT = "drone-agent-desk-supervisor.service"
UNIT_PATH = Path("/etc/systemd/system") / UNIT
MARKER = "# Managed by drone-agent D035"
HTTPS_PORT = 8448
LISTENER = "127.0.0.1:8769"
BACKEND = "http://" + LISTENER
RESIDENTS = ("desk-model-proxy", "desk-vendor", "desk-service", "desk-dock", "desk-fleet", "desk-uplink", "desk")
# The approved topology (D035, D036, D055, D069): only the model proxy joins the outbound network; the docks and the
# vendor simulator have none; the logical fleet reaches the service only like the aircraft's uplink.
# 已批准的拓扑（D035、D036、D055、D069）：只有模型代理接入出站网络；机场后端与厂商模拟器没有网络；逻辑机队只像飞行器的
# uplink 那样访问服务。
NETWORKS = {"desk": {"desk_ingress"}, "desk-service": {"desk_uplink", "desk_model"}, "desk-uplink": {"desk_uplink"},
            "desk-model-proxy": {"desk_model", "desk_egress"}, "desk-dock": set(), "desk-fleet": {"desk_uplink"},
            "desk-vendor": set()}
# P5 (D069): the catalogs the service must start with, the backends it allows and the fleet's aircraft. P6 (D078)
# replaces the workflow and business catalogs with the recapture ones; operations and scheduling stay those of P5.
# P5（D069）：服务必须加载的目录、允许的后端与机队的飞行器。P6（D078）把工作流与业务目录换成带补拍的目录；运营与调度
# 目录仍为 P5 的。
CATALOGS = {"operations": "p5_desk_v1", "workflows": "p6_desk_v1", "scheduling": "p5_desk_v1",
            "business": "p6_desk_v1"}
CATALOG_FILES = {"--catalog": "configs/sites/p5_desk_v1.yaml", "--workflows": "configs/workflows/p6_desk_v1.yaml",
                 "--scheduling": "configs/scheduling/p5_desk_v1.yaml", "--business": "configs/analysis/p6_desk_v1.yaml"}
CATALOG_ARGS = [part for flag, path in CATALOG_FILES.items() for part in (flag, "/workspace/" + path)]
BACKENDS = ["px4_sitl", "logical_sim", "vendor_protocol_sim"]
FLEET = ("uav_fa", "uav_fb")
DOCKS = ("dock_s1", "dock_fa", "dock_fb", "dock_vd")
PROJECTS = ("campus_s1", "fleet_s0", "vendor_s3", "legacy_m2")
command, serve_config, fingerprint, read_json = (CONSOLE["command"], CONSOLE["serve_config"], CONSOLE["fingerprint"],
                                                 CONSOLE["read_json"])


def other_routes(config: dict) -> dict:
    return CONSOLE["other_routes"](config, port=HTTPS_PORT)


def route_matches(config: dict, origin: str) -> bool:
    return CONSOLE["route_matches"](config, origin, port=HTTPS_PORT, backend=BACKEND)


def image_names(tag: str) -> dict:
    """The images of one revision: the M2 aircraft and ground images and the P5 simulator (D071).

    一个版本的镜像：M2 的飞行器与地面镜像，以及 P5 仿真器（D071）。
    """
    return {"sim": f"drone-agent-p5-sim:{tag}", "aircraft": f"drone-agent-m1-aircraft:{tag}",
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
            "authorization": "existing tailnet access; writes need the Serve login header and a project role "
                             "from the desk member list (D033, D055)",
            "catalog": CATALOG_FILES["--catalog"], "members_provisioned": (desk.secrets / "members.yaml").is_file(),
            "workflows": CATALOG_FILES["--workflows"], "scheduling": CATALOG_FILES["--scheduling"],
            "business": CATALOG_FILES["--business"], "execution_backends": BACKENDS,
            "residents": list(RESIDENTS),
            "vision": "live" if (desk.model / "minimax.key").is_file() else "unavailable",
            "ledger_migration": "drills on a copy first (D056, D058, D060, D064) and the P5 catalog switch drill "
                                "(D069), then the migrations with verified backups on start",
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
        expected = {f"{project}_{name}" for name in NETWORKS[service]} if NETWORKS[service] else {"none"}
        if networks != expected:
            raise ValueError(f"{service} networks differ from the approved topology")
        if service == "desk-dock" and mounts != {"/api": (str(desk.base / "api"), False),
                                                  "/truth": (str(desk.flights), False),
                                                  "/dock": (str(desk.base / "dock"), True)}:
            raise ValueError("desk-dock mounts differ from the approved scope")
        if service == "desk-fleet" and mounts != {
                "/api": (str(desk.base / "api"), False), "/fleet": (str(desk.fleet), True),
                "/world": (str(desk.world_dir), False), "/trust": (str(desk.secrets / "trust"), False),
                **{f"/tls/{robot}": (str(desk.secrets / f"robot-tls-{robot}"), False) for robot in FLEET}}:
            raise ValueError("desk-fleet mounts differ from the approved scope")
        if service == "desk-vendor" and mounts != {"/vendor": (str(desk.vendor / "socket"), True),
                                                    "/log": (str(desk.vendor / "log"), True),
                                                    "/world": (str(desk.world_dir), False),
                                                    "/faults": (str(desk.faults / "vendor"), False)}:
            raise ValueError("desk-vendor mounts differ from the approved scope")
        if service == "desk-service" and mounts.get("/members/members.yaml") != (str(desk.secrets / "members.yaml"),
                                                                                  False):
            raise ValueError("desk-service must read the member list read-only")
        if service == "desk-service" and (mounts.get("/vendor") != (str(desk.vendor / "socket"), False)
                                          or any(d in mounts for d in ("/world", "/faults"))):
            raise ValueError("desk-service reaches the vendor socket read-only and never the world or the faults")
        if service == "desk":
            ports = {"8769/tcp": [{"HostIp": "127.0.0.1", "HostPort": "8769"}]}
            if host.get("PortBindings") != ports or details["NetworkSettings"].get("Ports") != ports:
                raise ValueError("desk public binding differs")
            if mounts != {"/api": (str(desk.base / "api"), False), "/supervisor": (str(desk.public), False),
                          "/fixed/broker": (str(desk.root / "console/ipc"), False),
                          "/fixed/records": (str(desk.root / "artifacts"), False),
                          "/fixed/releases": (str(desk.root / "releases"), False),
                          "/fixed/outputs": (str(desk.base / "fixed-pages"), True)}:
                raise ValueError("desk mounts differ from the approved scope")
            if (config.get("Labels") or {}).get("io.drone-agent.source-sha") != source_sha:
                raise ValueError("desk container revision differs")
        elif host.get("PortBindings") or details["NetworkSettings"].get("Ports"):
            raise ValueError(f"{service} must not publish ports")
        if service == "desk-model-proxy" and mounts != {"/faults": (str(desk.faults / "model"), False)}:
            raise ValueError("the model proxy mounts only its own fault switch, read-only")
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


def wait_sessions(desk, timeout: float = 120.0) -> dict:
    """Every dock of the catalog reports an active session through the service (P1 rules, D072 for the vendor dock).

    目录中每个机场都经服务报告活动会话（P1 规则；厂商机场按 D072）。
    """
    deadline = time.monotonic() + timeout
    found: dict = {}
    while time.monotonic() < deadline:
        found = {}
        for project in ("campus_s1", "fleet_s0", "vendor_s3"):
            try:
                resources = SUPERVISOR["api"](desk, "resources.list", project_id=project)
            except (OSError, RuntimeError, ValueError):
                resources = {}
            sites = resources.get("sites", []) if isinstance(resources, dict) else []
            for dock in (dock for site in sites for dock in site.get("docks", [])):
                found[dock.get("dock_id")] = (dock.get("status") or {}).get("session")
        if all(found.get(dock) == "active" for dock in DOCKS):
            return found
        time.sleep(3)
    raise RuntimeError(f"dock sessions did not all become active: {found}")


def migration_drill(desk, artifact: Path, image: str, members: Path | None = None) -> dict:
    """D056, D058, D060 and D064 drills in turn on one copy of the live ledger with the new image; only counts and
    digests are kept.

    用新镜像对当前账本的同一副本依次执行 D056、D058、D060 与 D064 演练；只保留计数与摘要。
    """
    live = desk.service / "ledger.sqlite3"
    if not live.is_file():
        return {"status": "not_applicable", "reason": "no ledger yet"}
    work = artifact / "migration-drill"
    work.mkdir(mode=0o700)
    source = sqlite3.connect(f"file:{live.as_posix()}?mode=ro", uri=True)
    target = sqlite3.connect(str(work / "ledger.sqlite3"))
    try:
        source.backup(target)
        target.execute("PRAGMA journal_mode=DELETE")
    finally:
        target.close()
        source.close()
    drills = {}
    try:
        for name, module in (("operations", "drone_agent.fleet.operations_store"),
                             ("workflows", "drone_agent.fleet.workflow_store"),
                             ("scheduling", "drone_agent.fleet.scheduling_store"),
                             ("business", "drone_agent.fleet.business_store")):
            output = HELPERS["run"](["docker", "run", "--rm", "--network", "none", "--user",
                                     f"{os.getuid()}:{os.getgid()}", "-v", f"{work}:/drill", image, "python3", "-m",
                                     module, "--drill", "/drill/ledger.sqlite3"], timeout=300)
            drills[name] = json.loads(output.strip().splitlines()[-1])
        # P5 (D069): the catalog switch on the same migrated copy, offline, with the desk member list read-only.
        # P5（D069）：在同一已迁移副本上离线执行目录切换，任务台成员列表只读。
        switched = subprocess.run(["docker", "run", "--rm", "--network", "none", "--user",
                                   f"{os.getuid()}:{os.getgid()}", "-v", f"{work}:/drill",
                                   "-v", f"{members or desk.secrets / 'members.yaml'}:/members.yaml:ro", image,
                                   "python3", "-m",
                                   "drone_agent.fleet.switch_drill", "--ledger", "/drill/ledger.sqlite3",
                                   "--members", "/members.yaml", *CATALOG_ARGS],
                                  capture_output=True, timeout=300)
        lines = switched.stdout.decode("utf-8", errors="replace").strip().splitlines()
        try:
            drills["switch"] = json.loads(lines[-1])
        except (IndexError, ValueError):
            drills["switch"] = {"status": "failed", "exit_code": switched.returncode}
    finally:
        # The copy holds mission data; only the receipt stays. / 副本含任务数据；只保留回执。
        shutil.rmtree(work, ignore_errors=True)
    result = {"status": "passed" if all(d.get("status") == "passed" for d in drills.values()) and len(drills) == 5
              else "failed", **drills}
    (artifact / "migration-drill.json").write_text(json.dumps(result, indent=2))
    if result.get("status") != "passed":
        raise RuntimeError("the ledger migration drills did not pass; nothing was switched")
    return result


def write_private(path: Path, data: bytes) -> None:
    """Replace an owner-only file atomically. / 原子替换仅属主可读写的文件。"""
    pending = path.with_name(path.name + ".pending")
    descriptor = os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(data)
    pending.replace(path)


def store_members(root: Path, request: dict) -> dict:
    """Write the desk member list (JSON is valid YAML) into the desk secrets; the logins never leave the host.

    With `stage: next` the list is staged as `members.next.yaml` for the next activation, which promotes it only while
    switching and restores the previous list on rollback: a list naming the P5 projects would stop an older service
    from starting (P1 refuses memberships of unknown projects).

    把任务台成员列表（JSON 即合法 YAML）写入任务台 secrets；登录名不离开主机。带 `stage: next` 时，列表暂存为
    `members.next.yaml` 供下一次激活使用：激活只在切换时提升它，回退时恢复之前的列表，因为列出 P5 项目的列表会使旧服务无法
    启动（P1 拒绝未知项目的成员资格）。
    """
    value = request.get("members")
    if not isinstance(value, dict) or value.get("format") != "drone.project-members/v1":
        raise ValueError("a drone.project-members/v1 member list is required")
    entries = value.get("members")
    if not isinstance(entries, list) or not entries or len(entries) > 48:
        raise ValueError("1 to 48 memberships are required")
    for entry in entries:
        principal = entry.get("principal", "")
        if set(entry) != {"principal", "project_id", "roles"} or entry["project_id"] not in PROJECTS \
                or not re.fullmatch(r"(tailnet|harness):\S{1,200}", principal) or not entry["roles"] \
                or set(entry["roles"]) - {"viewer", "operator", "approver", "reviewer", "admin"}:
            raise ValueError("invalid membership entry")
    folder = SUPERVISOR["Desk"](root).secrets
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    data = json.dumps({"format": value["format"], "schema_version": "0.1.0", "members": entries}, indent=2)
    staged = request.get("stage") == "next"
    write_private(folder / ("members.next.yaml" if staged else "members.yaml"), data.encode())
    return {"status": "staged" if staged else "stored", "entries": len(entries),
            "sha256": hashlib.sha256(data.encode()).hexdigest(),
            "principal_schemes": sorted({e["principal"].split(":", 1)[0] for e in entries})}


def apply(root: Path, deployment: Path, request: dict) -> dict:
    """Activate and verify owned components; on failure restore only these components.

    激活并验证本项目组件；失败时只恢复这些组件。
    """
    import pwd

    value = plan(root, deployment)
    console = read_json(root / "console/current.json") or {}
    if console.get("source_sha") != value["source_sha"] or not (root / "console/ipc/broker.sock").exists():
        raise ValueError("activate console-cloud on this deployment before the unified desk")
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
    images["sim"] = P5["build_sim"](deployment / "source", artifact, tag, images["sim"])
    keys = M2["provision"](root, images["ground"], name="desk", also=FLEET)
    staged = desk.secrets / "members.next.yaml"
    members = staged if staged.is_file() else desk.secrets / "members.yaml"
    if not members.is_file():
        raise ValueError("provision the desk member list first (dev_stack.py desk-members --apply)")
    drill = migration_drill(desk, artifact, images["ground"], members)
    previous_members = (desk.secrets / "members.yaml").read_bytes() if (desk.secrets / "members.yaml").is_file() \
        else None
    record = {"schema_version": "0.1.0", "source_sha": value["source_sha"], "control_sha256": value["control_sha256"],
              "deployment_id": deployment.name, "origin": value["origin"], "listener": LISTENER, "images": images,
              "planner": value["planner"], "uid": os.getuid(), "gid": os.getgid(),
              "signer_key_id": keys["signer_key_id"], "certificate_fingerprints": keys["fingerprints"],
              "activated_at": datetime.now(timezone.utc).isoformat()}
    stack = SUPERVISOR["Stack"](desk, record, log=artifact / "compose.log")
    candidate = artifact / UNIT
    candidate.write_text(unit_text(root, deployment, pwd.getpwuid(os.getuid()).pw_name))
    added_route = str(HTTPS_PORT) not in before_serve.get("TCP", {})
    unit_changed = residents_changed = members_changed = False
    try:
        SUPERVISOR["write_json"](desk.base / "current.json", record)
        if members == staged:
            write_private(desk.secrets / "members.yaml", staged.read_bytes())
            members_changed = True
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
        started = read_json(desk.service / "ready.json") or {}
        operations = started.get("operations") or {}
        if operations.get("catalog_id") != CATALOGS["operations"] or not operations.get("migration"):
            raise RuntimeError(f"the mission service did not start with {CATALOGS['operations']} and a migrated ledger")
        workflows = started.get("workflows") or {}
        if workflows.get("catalog_id") != CATALOGS["workflows"] or (workflows.get("migration") or {}).get(
                "status") not in ("migrated", "current"):
            raise RuntimeError(f"the mission service did not start with workflow catalog {CATALOGS['workflows']} and its tables")
        scheduling = started.get("scheduling") or {}
        if scheduling.get("catalog_id") != CATALOGS["scheduling"] or (scheduling.get("migration") or {}).get(
                "status") not in ("migrated", "current"):
            raise RuntimeError(f"the mission service did not start with scheduling catalog {CATALOGS['scheduling']} and its tables")
        business = started.get("business") or {}
        if business.get("catalog_id") != CATALOGS["business"] or (business.get("migration") or {}).get(
                "status") not in ("migrated", "current"):
            raise RuntimeError(f"the mission service did not start with business catalog {CATALOGS['business']} and its tables")
        if started.get("execution_backends") != BACKENDS or (started.get("vendor") or {}).get("docks") != ["dock_vd"]:
            raise RuntimeError("the mission service did not start with the three backends and the vendor link")
        sessions = wait_sessions(desk)
        if value["vision"] == "live" and not str(business.get("vision", "")).startswith("live:"):
            raise RuntimeError(f"the vision role is not live: {business.get('vision')}")
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
                   "operations": {"catalog_id": operations.get("catalog_id"),
                                  "catalog_sha256": operations.get("catalog_sha256"),
                                  "migration": operations.get("migration"), "drill": drill.get("operations", drill)},
                   "workflows": {"catalog_id": workflows.get("catalog_id"),
                                 "catalog_sha256": workflows.get("catalog_sha256"),
                                 "migration": workflows.get("migration"), "drill": drill.get("workflows")},
                   "scheduling": {"catalog_id": scheduling.get("catalog_id"),
                                  "catalog_sha256": scheduling.get("catalog_sha256"),
                                  "migration": scheduling.get("migration"), "drill": drill.get("scheduling")},
                   "business": {"catalog_id": business.get("catalog_id"),
                                "catalog_sha256": business.get("catalog_sha256"),
                                "migration": business.get("migration"), "drill": drill.get("business"),
                                "vision": business.get("vision")},
                   "switch_drill": drill.get("switch", drill), "execution_backends": started.get("execution_backends"),
                   "vendor": started.get("vendor"), "dock_sessions": sessions,
                   "containers": inspect_desk(desk, value["source_sha"]),
                   "other_serve_before_sha256": fingerprint(other_routes(before_serve)),
                   "other_serve_after_sha256": fingerprint(other_routes(after_serve)),
                   "other_containers_before": before_other, "other_containers_after": after_other,
                   "supervisor_unit_sha256": HELPERS["digest"](candidate)}
        (artifact / "receipt.json").write_text(json.dumps(receipt, indent=2))
        staged.unlink(missing_ok=True)
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
        if members_changed and previous_members is not None:
            write_private(desk.secrets / "members.yaml", previous_members)
        if old:
            SUPERVISOR["write_json"](desk.base / "current.json", old)
            if residents_changed:
                previous = SUPERVISOR["Stack"](desk, old, log=artifact / "compose.log")
                # An older deployment has fewer residents; start only those its compose file defines.
                # 旧部署的常驻容器更少；只启动其 compose 文件定义的那些。
                text = previous.file().read_text(encoding="utf-8") if previous.file().is_file() else ""
                previous("up", "-d", "--no-build", "--pull", "never",
                         *[name for name in RESIDENTS if f"\n  {name}:\n" in text], check=False, timeout=240)
        else:
            (desk.base / "current.json").unlink(missing_ok=True)
        raise


# ── P5 operations: the world, the soak, backups (D070, D073) / P5 运维：世界、长稳、备份（D070、D073）──

SOAK_UNIT = "drone-agent-desk-soak.service"
SOAK_UNIT_PATH = Path("/etc/systemd/system") / SOAK_UNIT
SOAK_MARKER = "# Managed by drone-agent D073"
# P6 (D078) adds glare over an S1 marker for the recapture template. / P6（D078）为补拍模板加上 S1 标记上的反光。
WORLD_ASSETS = {"s1": {"asset_red": ("normal", "damaged", "glare"), "asset_blue": ("normal", "damaged", "glare"),
                       "road_north": ("normal", "obstructed")},
                "s0": {**{a: ("normal", "damaged") for a in ("asset_red", "asset_blue", "asset_red_b", "asset_blue_b")},
                       **{a: ("normal", "obstructed") for a in ("road_north", "road_north_b")}},
                "s3": {"asset_red": ("normal", "damaged"), "asset_blue": ("normal", "damaged"),
                       "road_north": ("normal", "obstructed")}}


def world(root: Path, request: dict) -> dict:
    """Change one asset of the desk world file (the simulators' truth, never a service input) and log the change.

    修改任务台世界文件中的一个资产（模拟器的真值，从不是服务输入）并记录该变化。
    """
    desk = SUPERVISOR["Desk"](root)
    section, asset, state = request.get("section"), request.get("asset"), request.get("state")
    if section not in WORLD_ASSETS or asset not in WORLD_ASSETS[section] or state not in WORLD_ASSETS[section][asset]:
        raise ValueError("unknown world section, asset or state")
    desk.world_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    current = read_json(desk.world) or {}
    if current.get("format") != SUPERVISOR["WORLD_FORMAT"]:
        current = {"format": SUPERVISOR["WORLD_FORMAT"], "s0": {}, "s1": {}, "s3": {}}
    current.setdefault(section, {})[asset] = state
    current["updated_at"] = datetime.now(timezone.utc).isoformat()
    SUPERVISOR["write_json"](desk.world, current)
    entry = {"at": current["updated_at"], "section": section, "asset_id": asset, "state": state,
             "reason": str(request.get("reason") or "")[:200], "actor": "dev_stack:desk-world",
             "run_id": request["run_id"]}
    with (desk.world_dir / "history.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return {"status": "written", "world": {k: current.get(k) for k in ("s0", "s1", "s3")}, "change": entry}


def soak_unit_text(root: Path, deployment: Path, user: str) -> str:
    text = unit_text(root, deployment, user)
    return (text.replace(MARKER, SOAK_MARKER, 1)
            .replace("Description=drone-agent resident mission desk simulation supervisor",
                     "Description=drone-agent P5 desk soak harness")
            .replace("scripts/desk_supervisor.py", "scripts/desk_soak.py run")
            .replace("RestartSec=5", "RestartSec=10"))


def soak_status(root: Path) -> dict:
    desk = SUPERVISOR["Desk"](root)
    current = read_json(desk.base / "soak" / "current.json")
    if not current:
        return {"status": "none"}
    base = desk.base / "soak" / current["soak_id"]
    manifest = read_json(base / "manifest.json") or {}
    state = read_json(base / "state.json") or {}
    statuses: dict[str, int] = {}
    for item in (state.get("occurrences") or {}).values():
        statuses[item.get("status", "?")] = statuses.get(item.get("status", "?"), 0) + 1
    active = command(["systemctl", "is-active", SOAK_UNIT], check=False).returncode == 0
    errors = 0
    actions = base / "actions.jsonl"
    if actions.is_file():
        with actions.open("rb") as stream:
            errors = sum(1 for line in stream if b'"harness_error"' in line)
    samples = base / "samples.jsonl"
    last = None
    if samples.is_file() and samples.stat().st_size:
        with samples.open("rb") as stream:
            stream.seek(max(0, samples.stat().st_size - 65536))
            tail = [line for line in stream.read().splitlines() if line.strip()]
        last = json.loads(tail[-1]) if tail else None
    return {"status": "finished" if (base / "finished.json").is_file() else "running" if active else "stopped",
            "soak_id": current["soak_id"], "t0": manifest.get("t0"), "end": manifest.get("end"),
            "source_sha": manifest.get("source_sha"), "deployment_id": manifest.get("deployment_id"),
            "full_length": manifest.get("full_length"), "unit_active": active, "occurrences": statuses,
            "harness_errors": errors, "last_sample": {k: (last or {}).get(k) for k in ("at", "service", "ledger",
                                                                                       "queues")}}


def soak_start(root: Path, deployment: Path, request: dict) -> dict:
    """Start a new soak on the activated desk revision: manifest first, then the harness unit.

    在已激活的任务台版本上开始新的长稳：先写 manifest，再启动编排 unit。
    """
    import pwd

    desk = SUPERVISOR["Desk"](root)
    record = read_json(desk.base / "current.json")
    if not record:
        raise ValueError("activate the desk first")
    if status(root).get("status") != "ready":
        raise ValueError("the desk is not ready")
    if command(["systemctl", "is-active", SOAK_UNIT], check=False).returncode == 0:
        raise ValueError("a soak is already running")
    if SOAK_UNIT_PATH.is_symlink() or (SOAK_UNIT_PATH.exists() and
                                       not SOAK_UNIT_PATH.read_text().startswith(SOAK_MARKER + "\n")):
        raise ValueError("refusing to overwrite a non-owned service unit")
    hours = request.get("hours")
    if hours is not None and not (isinstance(hours, (int, float)) and 0.05 <= float(hours) <= 96):
        raise ValueError("hours must be between 0.05 and 96")
    command(["sudo", "-n", "true"])
    harness = runpy.run_path(str(root / "releases" / record["deployment_id"] / "source/scripts/desk_soak.py"))
    soak_id = f"soak-{request['run_id']}"
    base = harness["create"](root, soak_id, hours=float(hours) if hours is not None else None)
    candidate = base / SOAK_UNIT
    candidate.write_text(soak_unit_text(root, root / "releases" / record["deployment_id"],
                                        pwd.getpwuid(os.getuid()).pw_name))
    command(["sudo", "-n", "install", "-m", "0644", str(candidate), str(SOAK_UNIT_PATH)])
    command(["sudo", "-n", "systemctl", "daemon-reload"])
    command(["sudo", "-n", "systemctl", "restart", SOAK_UNIT])
    time.sleep(5)
    return {**soak_status(root), "unit_sha256": HELPERS["digest"](candidate)}


def soak_stop(root: Path, request: dict) -> dict:
    """Ask the harness to end the soak now (it disables the schedules and clears every fault first).

    要求编排立即结束长稳（它先停用排班并清除每个故障）。
    """
    desk = SUPERVISOR["Desk"](root)
    current = read_json(desk.base / "soak" / "current.json")
    if not current:
        raise ValueError("no soak")
    base = desk.base / "soak" / current["soak_id"]
    SUPERVISOR["write_json"](base / "stop.json", {"requested_at": datetime.now(timezone.utc).isoformat(),
                                                  "run_id": request["run_id"],
                                                  "reason": str(request.get("reason") or "")[:200]})
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline and not (base / "finished.json").is_file():
        time.sleep(3)
    return soak_status(root)


def ledger_copy(desk, target: Path) -> dict:
    """An online copy of the live ledger with per-table counts. / 当前账本的在线副本与逐表计数。"""
    live = desk.service / "ledger.sqlite3"
    target.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(f"file:{live.as_posix()}?mode=ro", uri=True)
    copy = sqlite3.connect(str(target))
    try:
        source.backup(copy)
        copy.execute("PRAGMA journal_mode=DELETE")
        names = [row[0] for row in copy.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        counts = {name: copy.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0] for name in names}
        integrity = copy.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        copy.close()
        source.close()
    os.chmod(target, 0o600)
    return {"tables": counts, "integrity": integrity, "sha256": HELPERS["digest"](target)}


def soak_judge(root: Path, request: dict) -> dict:
    """Copy the ledger, then run the soak judge offline over the soak's records. / 复制账本后离线运行长稳裁判。"""
    desk = SUPERVISOR["Desk"](root)
    soak_id = request.get("soak_id") or (read_json(desk.base / "soak" / "current.json") or {}).get("soak_id")
    base = desk.base / "soak" / str(soak_id)
    manifest = read_json(base / "manifest.json")
    if not manifest:
        raise ValueError("unknown soak")
    judge = base / "judge" / request["run_id"]
    judge.mkdir(parents=True)
    copied = ledger_copy(desk, judge / "ledger.sqlite3")
    image = manifest["images"]["ground"]
    mounts = ["-v", f"{base}:/soak:ro", "-v", f"{judge}:/judge", "-v", f"{desk.flights}:/flights:ro",
              "-v", f"{desk.public}:/supervisor:ro", "-v", f"{desk.fleet}:/fleet:ro", "-v", f"{desk.vendor / 'log'}:/vendor:ro",
              "-v", f"{desk.world_dir}:/world:ro"]
    result = subprocess.run(["docker", "run", "--rm", "--network", "none", "--user", f"{os.getuid()}:{os.getgid()}",
                             *mounts, image, "python3", "-m", "drone_agent.eval.judge_p5_soak", "/soak",
                             "--ledger", "/judge/ledger.sqlite3", "--flights", "/flights", "--supervisor", "/supervisor",
                             "--fleet", "/fleet", "--vendor", "/vendor", "--world", "/world",
                             "--output", "/judge/result.json"], capture_output=True, timeout=3600)
    verdict = read_json(judge / "result.json") or {"passed": False, "error": "judge_failed"}
    (judge / "ledger.sqlite3").unlink(missing_ok=True)  # the copy holds mission data / 副本含任务数据
    return {"soak_id": soak_id, "judge_run": request["run_id"], "exit_code": result.returncode,
            "ledger": {"tables": len(copied["tables"]), "integrity": copied["integrity"], "sha256": copied["sha256"]},
            "result": verdict, "result_path": str(judge / "result.json")}


def backup(root: Path, request: dict) -> dict:
    """An online backup of the live ledger and a manifest of the media files (D073). / 当前账本的在线备份与媒体清单。"""
    desk = SUPERVISOR["Desk"](root)
    target = desk.base / "backups" / request["run_id"]
    target.mkdir(parents=True, mode=0o700)
    copied = ledger_copy(desk, target / "ledger.sqlite3")
    media = sorted(p for p in (desk.service / "media").rglob("*") if p.is_file()) \
        if (desk.service / "media").is_dir() else []
    listing = [{"path": str(p.relative_to(desk.service)), "bytes": p.stat().st_size, "sha256": HELPERS["digest"](p)}
               for p in media]
    record = {"schema_version": "0.1.0", "backup_id": request["run_id"], "taken_at": datetime.now(timezone.utc).isoformat(),
              "source_sha": (read_json(desk.base / "current.json") or {}).get("source_sha"),
              "ledger": copied, "media_files": len(listing),
              "media_sha256": hashlib.sha256(json.dumps(listing, sort_keys=True).encode()).hexdigest(),
              "catalogs": {k: (read_json(desk.service / "ready.json") or {}).get(k) for k in
                           ("operations", "workflows", "scheduling", "business")}}
    (target / "media.json").write_text(json.dumps(listing))
    (target / "backup.json").write_text(json.dumps(record, indent=2))
    return record


def restore_drill(root: Path, request: dict) -> dict:
    """Restore the latest backup into a scratch copy and read it back offline with the same image; the live ledger is
    never touched. A real restore is a manual step of the operations guide.

    把最近的备份恢复到临时副本，并用同一镜像离线读回；从不触碰在用账本。真实恢复是运维手册中的人工步骤。
    """
    desk = SUPERVISOR["Desk"](root)
    backups = sorted(p for p in (desk.base / "backups").glob("*") if (p / "backup.json").is_file())
    if not backups:
        raise ValueError("take a backup first (dev_stack.py desk-backup --apply)")
    latest = backups[-1]
    record = json.loads((latest / "backup.json").read_text())
    work = desk.base / "deployments" / request["run_id"] / "restore"
    work.mkdir(parents=True, mode=0o700)
    try:
        shutil.copyfile(latest / "ledger.sqlite3", work / "ledger.sqlite3")
        restored = HELPERS["digest"](work / "ledger.sqlite3") == record["ledger"]["sha256"]
        image = (read_json(desk.base / "current.json") or {})["images"]["ground"]
        members = desk.secrets / "members.yaml"
        result = subprocess.run(["docker", "run", "--rm", "--network", "none", "--user", f"{os.getuid()}:{os.getgid()}",
                                 "-v", f"{work}:/drill", "-v", f"{members}:/members.yaml:ro", image, "python3", "-m",
                                 "drone_agent.fleet.switch_drill", "--ledger", "/drill/ledger.sqlite3",
                                 "--members", "/members.yaml", *CATALOG_ARGS],
                                capture_output=True, timeout=600)
        lines = result.stdout.decode("utf-8", errors="replace").strip().splitlines()
        try:
            read_back = json.loads(lines[-1])
        except (IndexError, ValueError):
            read_back = {"status": "failed", "exit_code": result.returncode}
        counts = sqlite3.connect(f"file:{(work / 'ledger.sqlite3').as_posix()}?mode=ro", uri=True)
        try:
            after = {name: counts.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
                     for name in record["ledger"]["tables"]}
        finally:
            counts.close()
        changed = sorted(n for n, c in after.items() if c != record["ledger"]["tables"][n]
                         and n not in ("op_catalogs", "wf_catalogs", "sc_catalogs", "bz_catalogs"))
        same_catalogs = (read_back.get("catalogs") or {}).get("operations", [None, None])[1] == \
            ((record.get("catalogs") or {}).get("operations") or {}).get("catalog_sha256")
    finally:
        shutil.rmtree(work, ignore_errors=True)
    passed = restored and read_back.get("status") == "passed" and not changed and same_catalogs
    receipt = {"status": "passed" if passed else "failed", "backup_id": record["backup_id"],
               "backup_sha256": record["ledger"]["sha256"], "restored_digest_matches": restored,
               "read_back": read_back, "tables_changed": changed, "catalogs_match": same_catalogs,
               "live_ledger_touched": False, "drill_at": datetime.now(timezone.utc).isoformat()}
    (desk.base / "deployments" / request["run_id"] / "restore-drill.json").write_text(json.dumps(receipt, indent=2))
    return receipt
