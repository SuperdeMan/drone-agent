"""The P3 S1 two-aircraft runner and its compose file (D061): isolation, caps and the pure helpers.

P3 S1 双机执行器及其 compose 文件（D061）：隔离、资源上限与纯函数。
"""

from __future__ import annotations

import json
import runpy
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
RUNNER = runpy.run_path(str(ROOT / "scripts/remote_p3.py"))
COMPOSE = yaml.safe_load((ROOT / "sim/compose.p3.yaml").read_text(encoding="utf-8"))
ONBOARD = ("uplink-a", "uplink-b", "guardian-a", "guardian-b", "executive-a", "executive-b")


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


def test_truth_and_the_simulator_never_reach_an_onboard_process():
    for name in ONBOARD:
        assert not any("/truth" in mount for mount in mounts(name)), name
        assert "sim_p3" not in json.dumps(COMPOSE["services"][name].get("networks", [])) or name.startswith("guardian")
    for name in ("executive-a", "executive-b", "dock-sim-a", "dock-sim-b", "judge"):
        assert COMPOSE["services"][name]["network_mode"] == "none", name
    for name in ("dock-sim-a", "dock-sim-b"):
        assert COMPOSE["services"][name]["cap_drop"] == ["ALL"]
        assert all(mount.endswith(":ro") for mount in mounts(name) if "/truth" in mount or "/control" in mount)
    # Only the guardians reach PX4, each at its own fixed address. / 只有 guardian 连到 PX4，各有固定地址。
    guardians = {name: COMPOSE["services"][name]["networks"]["sim_p3"]["ipv4_address"] for name in ("guardian-a",
                                                                                                    "guardian-b")}
    assert len(set(guardians.values())) == 2
    assert COMPOSE["services"]["mission-service"]["networks"] == ["uplink"]


def test_each_aircraft_keeps_its_own_certificate_identity_and_directories():
    assert any("/robot-tls:/tls:ro" in mount for mount in mounts("uplink-a"))
    assert any("/robot-tls-uav_02:/tls:ro" in mount for mount in mounts("uplink-b"))
    for name in ("uplink-b", "guardian-b", "executive-b"):
        assert any(mount.endswith("platform-b.yaml:/workspace/configs/platforms/px4_sitl_multirotor.yaml:ro")
                   for mount in mounts(name)), name
    for letter in ("a", "b"):
        other = "b" if letter == "a" else "a"
        for role in ("uplink", "guardian", "executive"):
            joined = " ".join(mounts(f"{role}-{letter}"))
            assert f"_{other}:" not in joined and f"_{other.upper()}" not in joined, f"{role}-{letter}"
    secret_mounts = [mount for name in COMPOSE["services"] for mount in mounts(name) if "SECRETS" in mount]
    assert secret_mounts and all(mount.endswith(":ro") for mount in secret_mounts)


def test_the_second_platform_changes_only_the_robot_identity():
    original = yaml.safe_load((ROOT / "configs/platforms/px4_sitl_multirotor.yaml").read_text(encoding="utf-8"))
    changed = yaml.safe_load(RUNNER["platform_for"](ROOT, "uav_02"))
    assert changed["capability"]["robot_id"] == "uav_02"
    base = original["capability"]["robot_id"]
    for before, after in zip(original["capability"]["sensors"], changed["capability"]["sensors"], strict=True):
        assert after["frame_id"] == before["frame_id"].replace(base + "/", "uav_02/", 1)
        assert {k: v for k, v in after.items() if k != "frame_id"} == {k: v for k, v in before.items()
                                                                        if k != "frame_id"}
    changed["capability"]["robot_id"] = base
    changed["capability"]["sensors"] = original["capability"]["sensors"]
    assert changed == original


def test_resource_summary_and_real_time_factor(tmp_path):
    samples = tmp_path / "resources.jsonl"
    rows = []
    for index in range(5):
        rows.append({"wall": 100.0 + 2 * index, "service": "sitl-p3", "usage_usec": index * 3_000_000,
                     "nr_periods": index * 20, "nr_throttled": index, "throttled_usec": 0,
                     "memory_bytes": (1000 + index) * 2**20})
        rows.append({"wall": 100.0 + 2 * index, "service": "_host", "load1": 2.0 + index, "cpu_some_avg10": 1.0})
    samples.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    summary = RUNNER["resource_summary"](samples)
    assert summary["sitl-p3"]["cpu_cores_mean"] == 1.5 and summary["sitl-p3"]["memory_max_mib"] == 1004.0
    assert summary["sitl-p3"]["throttled_fraction"] == 0.05
    assert summary["_host"]["load1_max"] == 6.0 and summary["_host"]["samples"] == 5
    truth = tmp_path / "truth.jsonl"
    truth.write_text("".join(json.dumps({"timestamp": f"2026-09-26T00:00:{second:02d}+00:00",
                                         "sim_time": second * 0.9}) + "\n" for second in range(0, 41)),
                     encoding="utf-8")
    factor = RUNNER["real_time_factor"](truth)
    assert factor["windows"] == 4 and factor["min"] == 0.9 and factor["median"] == 0.9


def test_capacity_is_sufficient_only_within_both_d061_limits():
    verdict = RUNNER["capacity_verdict"]
    good = {"uav_01": {"min": 0.8}, "uav_02": {"min": 0.7}}
    fine = {"uav_01:m-1:v1": {"p99_s": 0.11}, "uav_02:m-2:v1": {"p99_s": 0.10}}
    assert verdict(good, fine)["sufficient"] and verdict(good, fine)["real_time_factor_min"] == 0.7
    assert not verdict({**good, "uav_02": {"min": 0.45}}, fine)["sufficient"]
    assert not verdict(good, {**fine, "uav_02:m-2:v1": {"p99_s": 0.13}})["sufficient"]
    # Missing evidence is never enough. / 缺少证据从不算足够。
    assert not verdict({"uav_01": None, "uav_02": None}, fine)["sufficient"]
    assert not verdict(good, {"uav_01:m-1:v1": None})["sufficient"]
