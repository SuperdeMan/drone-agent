"""Resident mission desk (D035): container boundaries, the Serve route and the simulation supervisor.

常驻任务台（D035）：容器边界、Serve 映射与仿真监管者。
"""

from __future__ import annotations

import copy
import json
import runpy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
DESK = runpy.run_path(str(ROOT / "scripts/remote_desk.py"))
SUP = runpy.run_path(str(ROOT / "scripts/desk_supervisor.py"))
COMPOSE = yaml.safe_load((ROOT / "sim/compose.desk.yaml").read_text(encoding="utf-8"))
ORIGIN = "https://example.test.ts.net:8448"
DEPLOYMENT = "20260923T120000Z-1234abcd"
MISSION = "m-0123456789ab"
RECORD = {"source_sha": "a" * 40, "deployment_id": DEPLOYMENT, "origin": ORIGIN, "uid": 1000, "gid": 1000,
          "images": {"sim": "sim:test", "aircraft": "aircraft:test", "ground": "ground:test"}}


# ── compose, unit and route boundaries / compose、unit 与映射边界 ──

def test_page_is_loopback_only_read_only_and_holds_no_secret_or_control_path():
    web = COMPOSE["services"]["desk"]
    assert web["ports"] == ["127.0.0.1:8769:8769"] and web["networks"] == ["desk_ingress"]
    assert web["read_only"] is True and web["cap_drop"] == ["ALL"] and web["security_opt"] == ["no-new-privileges:true"]
    assert web["user"] == "${DRONE_DESK_UID:?}:${DRONE_DESK_GID:?}" and web["restart"] == "unless-stopped"
    assert web["volumes"] == ["${DRONE_DESK_ROOT:?}/api:/api:ro", "${DRONE_DESK_ROOT:?}/supervisor/public:/supervisor:ro",
                              "${DRONE_DESK_WORKSPACE:?}/console/ipc:/fixed/broker:ro",
                              "${DRONE_DESK_WORKSPACE:?}/artifacts:/fixed/records:ro",
                              "${DRONE_DESK_WORKSPACE:?}/releases:/fixed/releases:ro",
                              "${DRONE_DESK_ROOT:?}/fixed-pages:/fixed/outputs"]
    assert "--tailnet" in web["command"] and "--a2a-clients" not in web["command"]


def test_residents_share_only_internal_networks_and_mount_secrets_read_only():
    services = COMPOSE["services"]
    assert services["desk-service"]["networks"] == ["desk_uplink", "desk_model"]
    assert services["desk-uplink"]["networks"] == ["desk_uplink"]
    for name in ("desk-service", "desk-uplink", "desk-model-proxy"):
        service = services[name]
        assert "ports" not in service and "profiles" not in service
        assert service["read_only"] is True and service["cap_drop"] == ["ALL"]
    assert all(v.endswith(":ro") for v in services["desk-service"]["volumes"] if "SECRETS" in v or "MODEL" in v)
    assert services["desk-service"]["environment"]["MINIMAX_API_KEY_FILE"] == "/model/minimax.key"
    assert "--planner" in services["desk-service"]["command"] and "auto" in services["desk-service"]["command"]
    assert "${DRONE_DESK_ROOT:?}/robot/aircraft:/aircraft:ro" in services["desk-uplink"]["volumes"]
    assert not any("/robot/ipc" in v for v in services["desk-uplink"]["volumes"])
    networks = COMPOSE["networks"]
    assert networks["desk_uplink"] == {"internal": True} and networks["desk_simulation"] == {"internal": True}
    assert networks["desk_ingress"] == {"driver": "bridge"}
    text = (ROOT / "sim/compose.desk.yaml").read_text(encoding="utf-8")
    assert "secrets/m2" not in text and "docker.sock" not in text and ".ssh" not in text


@pytest.mark.parametrize("path,proxy,service", [("sim/compose.desk.yaml", "desk-model-proxy", "desk-service"),
                                               ("sim/compose.m2.yaml", "model-proxy", "mission-service")])
def test_only_the_allowlisted_proxy_reaches_the_outbound_network(path, proxy, service):
    compose = yaml.safe_load((ROOT / path).read_text(encoding="utf-8"))
    prefix = "desk_" if "desk" in path else ""
    egress, model = f"{prefix}egress", f"{prefix}model"
    assert compose["networks"][egress] == {"driver": "bridge"} and compose["networks"][model] == {"internal": True}
    on = {name: set(s.get("networks", [])) for name, s in compose["services"].items()}
    assert [name for name, nets in on.items() if egress in nets] == [proxy]
    assert sorted(name for name, nets in on.items() if model in nets) == sorted([proxy, service])
    command = compose["services"][proxy]["command"]
    assert [command[i + 1] for i, part in enumerate(command) if part == "--allow"] == ["api.minimaxi.com:443"]
    assert "volumes" not in compose["services"][proxy] and "ports" not in compose["services"][proxy]
    assert compose["services"][service]["environment"]["HTTPS_PROXY"] == f"http://{proxy}:3128"
    assert compose["services"][service]["depends_on"] == [proxy]


def test_flight_services_only_start_under_the_supervisor_profile_without_fault_injection():
    services = COMPOSE["services"]
    flight = {"desk-sitl", "desk-collector", "desk-guardian", "desk-executive", "desk-judge"}
    assert {name for name, s in services.items() if s.get("profiles") == ["flight"]} == flight
    assert services["desk-executive"]["network_mode"] == "none"
    assert services["desk-guardian"]["network_mode"] == "service:desk-sitl"
    assert "--fault" not in services["desk-guardian"]["command"] and "--trust" in services["desk-guardian"]["command"]
    assert "${DRONE_DESK_ROOT:?}/robot/mailbox:/mailbox:ro" in services["desk-executive"]["volumes"]
    assert services["desk-judge"]["network_mode"] == "none"
    assert services["desk-judge"]["volumes"][0] == "${DRONE_DESK_CASE:?}:/run:ro"


def test_supervisor_unit_survives_restarts_without_killing_flights():
    unit = DESK["unit_text"](Path("/home/ubuntu/drone-agent"), Path("/home/ubuntu/drone-agent/releases") / DEPLOYMENT,
                             "ubuntu")
    assert unit.startswith(DESK["MARKER"] + "\n") and "KillMode=process" in unit and "ProtectHome=read-only" in unit
    assert "Environment=DOCKER_CONFIG=/home/ubuntu/drone-agent/desk/supervisor/docker-client" in unit
    assert f"releases/{DEPLOYMENT}/source/scripts/desk_supervisor.py" in unit
    with pytest.raises(ValueError):
        DESK["unit_text"](Path("/home/ubuntu/drone-agent"), Path("/tmp/elsewhere"), "ubuntu")


def test_desk_route_is_its_own_and_never_hides_changes_to_other_entries():
    original = {"TCP": {"443": {"HTTPS": True}, "8447": {"HTTPS": True}},
                "Web": {"example.test.ts.net:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:5173"}}},
                        "example.test.ts.net:8447": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8768"}}}}}
    installed = copy.deepcopy(original)
    installed["TCP"]["8448"] = {"HTTPS": True}
    installed["Web"]["example.test.ts.net:8448"] = {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8769"}}}
    assert DESK["route_matches"](installed, ORIGIN) and not DESK["route_matches"](original, ORIGIN)
    assert DESK["other_routes"](installed) == DESK["other_routes"](original)
    installed["Web"]["example.test.ts.net:8447"]["Handlers"]["/"]["Proxy"] = "http://127.0.0.1:9999"
    assert DESK["other_routes"](installed) != DESK["other_routes"](original)
    installed["AllowFunnel"] = {"example.test.ts.net:8448": True}
    assert not DESK["route_matches"](installed, ORIGIN)


def test_status_is_unhealthy_when_page_and_service_revisions_differ(tmp_path, monkeypatch):
    function = DESK["status"]
    monkeypatch.setitem(function.__globals__, "read_json", lambda path: {"source_sha": "a" * 40, "origin": ORIGIN})
    monkeypatch.setitem(function.__globals__, "serve_config", lambda: {})
    monkeypatch.setitem(function.__globals__, "route_matches", lambda *_: True)
    monkeypatch.setitem(function.__globals__, "command", lambda *_a, **_k: SimpleNamespace(returncode=0))
    monkeypatch.setitem(function.__globals__["HELPERS"], "current", lambda root: root / "releases" / DEPLOYMENT)
    monkeypatch.setitem(function.__globals__, "probe", lambda _origin: {
        "ok": True, "console_source_sha": "a" * 40, "result": {"status": "ready", "source_sha": "b" * 40}})
    assert function(tmp_path)["status"] == "unhealthy"
    monkeypatch.setitem(function.__globals__, "probe", lambda _origin: {
        "ok": True, "console_source_sha": "a" * 40, "result": {"status": "ready", "source_sha": "a" * 40}})
    assert function(tmp_path)["status"] == "ready"


@pytest.mark.parametrize("defect", [None, "public", "extra_network", "writable_api", "root_user", "service_egress",
                                    "proxy_mount", "dock_network", "dock_writes_truth", "members_writable"])
def test_container_verification_reads_actual_publication_mounts_and_user(tmp_path, monkeypatch, defect):
    desk = SUP["Desk"](tmp_path)
    ports = {"8769/tcp": [{"HostIp": "127.0.0.1", "HostPort": "8769"}]}

    def container(service):
        web = service == "desk"
        mounts = {"desk": [("/api", desk.base / "api", False), ("/supervisor", desk.public, False),
                            ("/fixed/broker", desk.root / "console/ipc", False),
                            ("/fixed/records", desk.root / "artifacts", False),
                            ("/fixed/releases", desk.root / "releases", False),
                            ("/fixed/outputs", desk.base / "fixed-pages", True)],
                  "desk-model-proxy": [],
                  "desk-dock": [("/api", desk.base / "api", False), ("/truth", desk.flights, False),
                                ("/dock", desk.base / "dock", True)],
                  "desk-service": [("/state", desk.service, True),
                                   ("/members/members.yaml", desk.secrets / "members.yaml", False)],
                  }.get(service, [("/state", desk.service, True)])
        networks = {f"drone-agent-cloud_{name}": {} for name in DESK["NETWORKS"][service]} or {"none": {}}
        value = {
            "Id": service, "Image": "sha256:test", "State": {"Running": True},
            "Config": {"User": "1000:1001", "Labels": {"io.drone-agent.source-sha": "a" * 40}},
            "HostConfig": {"PortBindings": copy.deepcopy(ports) if web else {}, "ReadonlyRootfs": True,
                           "Privileged": False, "CapDrop": ["ALL"]},
            "NetworkSettings": {"Ports": copy.deepcopy(ports) if web else {},
                                "Networks": networks},
            "Mounts": [{"Destination": d, "Source": str(s), "RW": rw, "Type": "bind"} for d, s, rw in mounts],
        }
        if web and defect == "public":
            value["NetworkSettings"]["Ports"]["8769/tcp"][0]["HostIp"] = "0.0.0.0"
        if web and defect == "extra_network":
            value["NetworkSettings"]["Networks"]["drone-agent-cloud_desk_simulation"] = {}
        if web and defect == "writable_api":
            value["Mounts"][0]["RW"] = True
        if service == "desk-uplink" and defect == "root_user":
            value["Config"]["User"] = ""
        if service == "desk-service" and defect == "service_egress":
            value["NetworkSettings"]["Networks"]["drone-agent-cloud_desk_egress"] = {}
        if service == "desk-dock" and defect == "dock_network":
            value["NetworkSettings"]["Networks"] = {"drone-agent-cloud_desk_uplink": {}}
        if service == "desk-dock" and defect == "dock_writes_truth":
            value["Mounts"][1]["RW"] = True
        if service == "desk-service" and defect == "members_writable":
            value["Mounts"][1]["RW"] = True
        if service == "desk-model-proxy" and defect == "proxy_mount":
            value["Mounts"] = [{"Destination": "/secrets", "Source": str(desk.secrets), "RW": False, "Type": "bind"}]
        return value

    def command(argv, **_kwargs):
        if argv[1] == "ps":
            return SimpleNamespace(stdout=argv[-1].rsplit("=", 1)[1] + "\n")
        return SimpleNamespace(stdout=json.dumps([container(argv[-1])]))

    function = DESK["inspect_desk"]
    monkeypatch.setitem(function.__globals__, "os", SimpleNamespace(getuid=lambda: 1000, getgid=lambda: 1001))
    monkeypatch.setitem(function.__globals__, "command", command)
    if defect:
        with pytest.raises(ValueError):
            function(desk, "a" * 40)
    else:
        assert set(function(desk, "a" * 40)) == {"desk-model-proxy", "desk-service", "desk-dock", "desk-uplink", "desk"}


# ── the simulation supervisor / 仿真监管者 ──

class Clock:
    """Deterministic time: every monotonic read advances half a second; sleeping does nothing.

    确定性时间：每次读取单调时钟前进半秒；睡眠不做任何事。
    """

    def __init__(self):
        self.value = 0.0

    def monotonic(self) -> float:
        self.value += 0.5
        return self.value

    def sleep(self, _seconds) -> None:
        return None


class FakeStack:
    """Records compose calls and plays the simulator, guardian and judge. / 记录 compose 调用并扮演仿真器、guardian 与裁判。"""

    calls: list = []
    guardian_running = True
    land_grounds = True

    def __init__(self, desk, record, log=None):
        self.desk = desk

    def __call__(self, *args, timeout=180, check=True, quiet=False, **flight):
        FakeStack.calls.append((args, flight))
        done = SimpleNamespace(stdout=b"", returncode=0)
        if args[0] == "up" and "desk-sitl" in args:
            (flight["flight"] / "sensor/latest.json").write_text("{}")
            (flight["flight"] / "truth/truth.jsonl").write_text('{"position": [0, 0, 0]}\n')
        elif args[0] == "logs":
            return SimpleNamespace(stdout=b"INFO [commander] home set\n", returncode=0)
        elif args[0] == "up" and "desk-guardian" in args:
            (self.desk.ipc / "guardian.sock").write_text("")
        elif args[0] == "up" and "desk-executive" in args:
            status(flight["aircraft"], in_air=False, armed=False)
            (flight["aircraft"] / "result.json").write_text("{}")
        elif args[0] == "exec" and FakeStack.land_grounds:
            status(flight["aircraft"], in_air=False, armed=False)
            (flight["aircraft"] / "result.json").write_text("{}")
        elif args[0] == "ps":
            return SimpleNamespace(stdout=b"container\n" if FakeStack.guardian_running else b"", returncode=0)
        elif args[0] == "run":
            case = flight["case"]
            verdict = {"classification": "completed", "passed": True, "false_success_reports": 0, "problems": [],
                       "flown_versions": [1]}
            name = "replay.json" if "--replay" in args else "result.json"
            (case / "judge" / name).write_text(json.dumps(verdict))
        return done


def status(aircraft: Path, *, in_air: bool, armed: bool, safety: str = "proceed") -> None:
    aircraft.mkdir(parents=True, exist_ok=True)
    (aircraft / "status.json").write_text(json.dumps({
        "observation": {"in_air": in_air, "armed": armed}, "safety": safety, "reason": "test"}))


def package(desk, mission=MISSION, version=1, expires_in=timedelta(hours=1)) -> Path:
    path = desk.history / f"{mission}-v{version}.json"
    expires = (datetime.now(timezone.utc) + expires_in).isoformat()
    path.write_text(json.dumps({"mission_id": mission, "mission_version": version,
                                "approval": {"expires_at": expires}}))
    return path


@pytest.fixture
def supervisor(tmp_path, monkeypatch):
    desk = SUP["Desk"](tmp_path)
    for folder in desk.folders():
        folder.mkdir(parents=True, exist_ok=True)
    globals_ = SUP["Supervisor"].fly.__globals__
    m0 = []
    monkeypatch.setitem(globals_, "Stack", FakeStack)
    monkeypatch.setitem(globals_, "time", Clock())
    monkeypatch.setitem(globals_["HELPERS"], "current", lambda root: root / "releases" / "m0")
    monkeypatch.setitem(globals_["HELPERS"], "compose", lambda root, deployment, args, **_k: m0.append(args[0]))
    monkeypatch.setitem(globals_["HELPERS"], "capacity", lambda: {"memory": {"MemAvailable": 8 * 1024**3}})
    FakeStack.calls, FakeStack.guardian_running, FakeStack.land_grounds = [], True, True
    value = SUP["Supervisor"](desk, RECORD)
    value.try_lock = lambda: SimpleNamespace(close=lambda: None)
    value.m0_calls = m0
    return value


def test_queue_holds_the_latest_unflown_version_of_each_mission_oldest_first(supervisor):
    desk = supervisor.desk
    package(desk, "m-bbbbbbbbbbbb", 1)
    package(desk, MISSION, 1)
    package(desk, MISSION, 2)
    (desk.history / "m-ZZ-v1.json").write_text("{}")
    (desk.flights / "m-bbbbbbbbbbbb-v1").mkdir(parents=True)
    (desk.flights / "m-bbbbbbbbbbbb-v1/flight.json").write_text("{}")
    assert [(m, v) for m, v, _ in supervisor.pending()] == [(MISSION, 2)]


def test_a_package_that_cannot_finish_before_its_approval_lapses_is_not_flown(supervisor):
    desk = supervisor.desk
    assert supervisor.unflyable(MISSION, 1, package(desk)) is None
    short = package(desk, expires_in=timedelta(minutes=5))
    assert supervisor.unflyable(MISSION, 1, short) == "approval_expires_before_the_flight_could_finish"
    assert supervisor.unflyable(MISSION, 2, short) == "package_identity_mismatch"
    supervisor.step()
    record = json.loads((desk.public / f"missions/{MISSION}.json").read_text())
    assert record["flights"][0]["status"] == "skipped" and FakeStack.calls == []


def test_one_flight_boots_a_fresh_simulator_uses_the_next_epoch_and_restores_the_idle_one(supervisor):
    desk = supervisor.desk
    path = package(desk)
    (desk.state / "authority.json").write_text(json.dumps({"epoch_watermark": 4}))
    (desk.mailbox / "operator.json").write_text(json.dumps({"mission_id": "m-bbbbbbbbbbbb", "mission_version": 1}))
    supervisor.step()
    flight = json.loads((desk.flights / f"{MISSION}-v1/flight.json").read_text())
    assert (flight["status"], flight["epoch"], flight["package"]) == ("finished", 5, path.name)
    assert supervisor.m0_calls == ["stop", "up"]
    started = [args[-1] for args, _ in FakeStack.calls if args[0] == "up"]
    assert started == ["desk-sitl", "desk-collector", "desk-guardian", "desk-executive"]
    executive = next(flight for args, flight in FakeStack.calls if args[0] == "up" and args[-1] == "desk-executive")
    assert executive["epoch"] == 5 and executive["package"] == path.name
    stopped = [args[-1] for args, _ in FakeStack.calls if args[0] == "stop"]
    assert stopped == ["desk-guardian", "desk-collector", "desk-sitl"]
    assert not (desk.mailbox / "operator.json").exists() and not (desk.supervisor / "active.json").exists()
    assert (desk.flights / f"{MISSION}-v1/sitl.log").read_bytes().endswith(b"home set\n")
    assert supervisor.pending() == []


def test_a_surrendered_flight_is_landed_by_the_simulation_operator(supervisor):
    aircraft = supervisor.desk.aircraft / MISSION / "v1"
    status(aircraft, in_air=True, armed=True, safety="abort")
    result = supervisor.watch(FakeStack(supervisor.desk, RECORD), {"aircraft": aircraft}, aircraft, 1e9)
    assert result == {"status": "finished", "manual_cleanup": True}
    assert json.loads((aircraft / "manual-cleanup.json").read_text())["reason"] == "test"
    assert [args[0] for args, _ in FakeStack.calls].count("exec") == 1


def test_an_overrun_or_a_vanished_guardian_ends_the_flight_without_success(supervisor):
    aircraft = supervisor.desk.aircraft / MISSION / "v1"
    status(aircraft, in_air=True, armed=True)
    overrun = supervisor.watch(FakeStack(supervisor.desk, RECORD), {"aircraft": aircraft}, aircraft, 0)
    assert overrun["status"] == "overrun" and overrun["manual_cleanup"] is True
    status(aircraft, in_air=False, armed=True)
    (aircraft / "result.json").unlink()
    FakeStack.guardian_running = False
    failed = supervisor.watch(FakeStack(supervisor.desk, RECORD), {"aircraft": aircraft}, aircraft, 1e9)
    assert failed["status"] == "failed" and "guardian exited" in failed["detail"]


def test_a_restarted_supervisor_finishes_the_flight_in_progress_instead_of_flying_again(supervisor):
    desk = supervisor.desk
    path = package(desk)
    flight = desk.flights / f"{MISSION}-v1"
    flight.mkdir(parents=True)
    (flight / "flight.json").write_text(json.dumps({"version": 1, "status": "flying", "epoch": 3,
                                                    "started_at": datetime.now(timezone.utc).isoformat()}))
    (desk.supervisor / "active.json").write_text(json.dumps({"mission_id": MISSION, "version": 1, "package": path.name,
                                                             "started_at": datetime.now(timezone.utc).isoformat()}))
    # The flight landed while no supervisor was watching; the guardian container still runs.
    # 没有监管者监视时飞行已落地；guardian 容器仍在运行。
    aircraft = desk.aircraft / MISSION / "v1"
    status(aircraft, in_air=False, armed=False)
    (aircraft / "result.json").write_text("{}")
    FakeStack.guardian_running = True
    supervisor.adopt()
    record = json.loads((flight / "flight.json").read_text())
    assert record["status"] == "finished" and "adopted" in record["detail"] and record["epoch"] == 3
    assert not any(args[0] == "up" for args, _ in FakeStack.calls) and supervisor.m0_calls == ["up"]
    assert not (desk.supervisor / "active.json").exists() and supervisor.pending() == []


def test_a_final_mission_is_judged_from_its_own_copied_case(supervisor, monkeypatch):
    desk = supervisor.desk
    package(desk)
    package(desk, "m-bbbbbbbbbbbb", 1)
    (desk.service / "ready.json").write_text(json.dumps({"signer_key_id": "ed25519:test"}))
    supervisor.step()
    other = desk.aircraft / "m-bbbbbbbbbbbb" / "v1"
    status(other, in_air=False, armed=False)
    views = {MISSION: {"mission": {"status": "completed"}, "request": {"text": "inspect the red marker"},
                       "versions": [{"version": 1, "journals": {}}]}}
    monkeypatch.setitem(SUP["Supervisor"].judge_ready.__globals__, "api", lambda desk, method, mission_id: views[mission_id])
    supervisor.judge_ready()
    case = desk.cases / MISSION / "after-v1"
    assert json.loads((case / "input/scenario.json").read_text())["expected"] == {"classification": "any"}
    assert sorted(p.name for p in (case / "aircraft").iterdir()) == [MISSION]
    assert (case / "inbox/history" / f"{MISSION}-v1.json").exists()
    assert (case / "truth/truth.jsonl").read_text() == '{"position": [0, 0, 0]}\n'
    judge = json.loads((desk.public / f"missions/{MISSION}.json").read_text())["judge"]
    assert (judge["passed"], judge["replay_agrees"], judge["after_version"]) == (True, True, 1)
    before = len(FakeStack.calls)
    supervisor.checked.clear()
    supervisor.judge_ready()
    assert len(FakeStack.calls) == before


def test_a_mission_that_has_not_ended_or_is_not_mirrored_is_not_judged(supervisor, monkeypatch):
    desk = supervisor.desk
    package(desk)
    supervisor.step()
    (desk.aircraft / MISSION / "v1/executive.jsonl").write_text('{"seq": 0}\n{"seq": 1}\n')
    view = {"mission": {"status": "running"}, "request": {"text": "x"},
            "versions": [{"version": 1, "journals": {"executive": {"rows": 2, "chain": "ok"}}}]}
    monkeypatch.setitem(SUP["Supervisor"].judge_ready.__globals__, "api", lambda desk, method, mission_id: view)
    supervisor.judge_ready()
    assert not (desk.cases / MISSION).exists()
    view["mission"]["status"] = "completed"
    view["versions"][0]["journals"]["executive"]["rows"] = 1
    supervisor.checked.clear()
    supervisor.judge_ready()
    assert not (desk.cases / MISSION).exists()
