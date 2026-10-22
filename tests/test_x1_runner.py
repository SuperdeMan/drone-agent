"""The X1 S1 runner and its compose file (D088): isolation, caps, the rover's observer and identities.

X1 S1 执行器及其 compose 文件（D088）：隔离、资源上限、地面车的只读观测与身份。
"""

from __future__ import annotations

import json
import runpy
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
RUNNER = runpy.run_path(str(ROOT / "scripts/remote_x1.py"))
COMPOSE = yaml.safe_load((ROOT / "sim/compose.x1.yaml").read_text(encoding="utf-8"))
ONBOARD = ("uplink-a", "uplink-g", "guardian-a", "guardian-g", "executive-a", "executive-g", "observer-g")


def mounts(service: str) -> list[str]:
    return COMPOSE["services"][service].get("volumes", [])


def test_every_service_is_capped_and_has_no_host_entry_point():
    assert COMPOSE["name"] == "drone-agent-cloud"
    assert set(COMPOSE["services"]) == {*RUNNER["SERVICES"], "judge"}
    for name, service in COMPOSE["services"].items():
        assert service["pull_policy"] == "never", name
        assert not service.get("ports") and not service.get("devices") and not service.get("privileged"), name
        assert service.get("cpus") and service.get("mem_limit") and service.get("pids_limit"), name
        assert service["memswap_limit"] == service["mem_limit"], name
        assert all("docker.sock" not in mount for mount in service.get("volumes", [])), name
    assert all(network.get("internal") is True for network in COMPOSE["networks"].values())
    weights = {name: service.get("cpu_shares", 1024) for name, service in COMPOSE["services"].items()}
    assert weights["sitl-x1"] == 4096
    favoured = ("guardian-a", "guardian-g", "observer-g", "collector-a", "collector-g")
    assert {weights[n] for n in favoured} == {2048}
    assert all(w == 1024 for n, w in weights.items() if n not in ("sitl-x1", *favoured))


def test_truth_and_the_simulator_never_reach_an_onboard_process():
    for name in ONBOARD:
        assert not any("/truth" in mount for mount in mounts(name)), name
        assert "sim_x1" not in json.dumps(COMPOSE["services"][name].get("networks", [])) \
            or name.startswith(("guardian", "observer")), name
    for name in ("executive-a", "executive-g", "dock-sim-a", "judge"):
        assert COMPOSE["services"][name]["network_mode"] == "none", name
    assert COMPOSE["services"]["dock-sim-a"]["cap_drop"] == ["ALL"]
    assert COMPOSE["services"]["mission-service"]["networks"] == ["uplink"]
    # Only a guardian reaches PX4. The rover's observer and its mission guardian share one address and the runner
    # never runs them together. / 只有 guardian 连到 PX4；地面车的观测与任务 guardian 共用一个地址，执行器从不同时运行两者。
    address = {name: COMPOSE["services"][name]["networks"]["sim_x1"]["ipv4_address"]
               for name in ("guardian-a", "guardian-g", "observer-g")}
    assert address["guardian-g"] == address["observer-g"] != address["guardian-a"]
    sitl = COMPOSE["services"]["sitl-x1"]["environment"]
    assert sitl["DRONE_X1_PARTNER_0"] == address["guardian-a"] and sitl["DRONE_X1_PARTNER_1"] == address["guardian-g"]


def test_the_observer_only_observes_and_writes_the_idle_status():
    command = COMPOSE["services"]["observer-g"]["command"]
    assert command[:5] == ["python3", "-m", "drone_agent.runtime.launch", "guardian", "--observe-only"]
    assert "--package" not in command and "--trust" not in command and "--robot-state" not in command
    assert any(mount.endswith("/robots/ugv_01/aircraft/idle:/artifacts") for mount in mounts("observer-g"))
    assert any(mount.endswith("/robots/ugv_01/aircraft:/aircraft:ro") for mount in mounts("uplink-g"))


def test_each_robot_keeps_its_own_certificate_identity_directories_and_map():
    assert any("/robot-tls:/tls:ro" in mount for mount in mounts("uplink-a"))
    assert any("/robot-tls-ugv_01:/tls:ro" in mount for mount in mounts("uplink-g"))
    for letter, robot, other in (("a", "uav_01", "ugv_01"), ("g", "ugv_01", "uav_01")):
        for role in ("uplink", "guardian", "executive"):
            joined = " ".join(mounts(f"{role}-{letter}"))
            assert f"robots/{robot}/" in joined and f"robots/{other}/" not in joined, f"{role}-{letter}"
    for name in ("uplink-g", "guardian-g", "executive-g", "observer-g"):
        command = COMPOSE["services"][name]["command"]
        assert command[command.index("--scene") + 1] == "/workspace/configs/scenarios/x1_ground_s1_v1.yaml", name
        assert command[command.index("--platform") + 1] == "/workspace/configs/platforms/px4_sitl_rover.yaml", name
    secret_mounts = [mount for name in COMPOSE["services"] for mount in mounts(name) if "SECRETS" in mount]
    assert secret_mounts and all(mount.endswith(":ro") for mount in secret_mounts)


def test_the_service_runs_the_x1_s1_catalogs_with_air_ground():
    command = COMPOSE["services"]["mission-service"]["command"]
    for flag, path in (("--catalog", "configs/sites/x1_s1_v1.yaml"), ("--members", "configs/sites/x1_members_s1.yaml"),
                       ("--workflows", "configs/workflows/x1_s1_v1.yaml"),
                       ("--scheduling", "configs/scheduling/x1_s1_v1.yaml"),
                       ("--business", "configs/analysis/x1_s1_v1.yaml")):
        assert command[command.index(flag) + 1] == f"/workspace/{path}"
    assert "--air-ground" in command and command[command.index("--vision") + 1] == "none"
    judge = COMPOSE["services"]["judge"]["command"]
    assert judge[:3] == ["python3", "-m", "drone_agent.eval.judge_x1"] and "s1" in judge


def test_the_simulation_image_keeps_px4s_default_logging_and_sets_the_rover_battery():
    setup = (ROOT / "sim/x1_setup.py").read_text(encoding="utf-8")
    assert "SDLOG" not in setup and "param set-default SIM_BAT_DRAIN 1800" in setup
    # The rover's own speed limit is the platform's maximum, which the look-ahead is derived from. / 地面车飞控限速即平台
    # 最大速度，前瞻由它推导。
    platform = yaml.safe_load((ROOT / "configs/platforms/px4_sitl_rover.yaml").read_text(encoding="utf-8"))
    assert f"param set-default RO_SPEED_LIM {platform['capability']['limits']['max_speed_mps']}" in setup
    assert "SDLOG" not in (ROOT / "sim/compose.x1.yaml").read_text(encoding="utf-8")


def test_the_simulation_image_has_real_line_continuations():
    dockerfile = (ROOT / "sim/x1.Dockerfile").read_text(encoding="utf-8")
    assert chr(92) + "n" not in dockerfile
    run = next(line for line in dockerfile.splitlines() if line.startswith("RUN no_proxy"))
    assert run.endswith(chr(92))
